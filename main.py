from __future__ import annotations

import os
import sys
import webbrowser
from typing import Any

from PySide6.QtCore import Qt, QTimer, QSize, QUrl
from PySide6.QtGui import QIcon, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTreeWidget, QTreeWidgetItem, QFrame,
    QScrollArea, QFormLayout, QSpinBox, QGroupBox,
    QLabel,
)
from qfluentwidgets import (
    FluentWindow, NavigationItemPosition, FluentIcon,
    setTheme, Theme, setThemeColor,
    SubtitleLabel, BodyLabel, StrongBodyLabel, TitleLabel,
    PrimaryPushButton, TransparentPushButton, PushButton,
    InfoBar, InfoBarPosition,
    TabBar, TabCloseButtonDisplayMode,
    isDarkTheme, ComboBox, CheckBox, SpinBox,
    SwitchButton, HyperlinkButton,
    ScrollArea, SettingCard, ExpandLayout,
    CardWidget, SimpleCardWidget,
    CaptionLabel,
)
from qfluentwidgets import FluentWindow

from ui_core import (
    VirtualLogView, DashboardView, AddSourceDialog,
    LogModel, LogLevel, _DARK_BG, _DARK_BG_ALT,
)
from providers import discover_local_logs, DiscoveredLog, IGNORED_PATTERNS


class LogTabArea(QWidget):
    def __init__(self, dashboard: DashboardView, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dashboard = dashboard
        self._tabs: dict[str, VirtualLogView] = {}
        self._error_tabs: set[str] = set()
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._tab_bar = TabBar(self)
        self._tab_bar.setTabMaximumWidth(200)
        self._tab_bar.setMovable(True)
        self._tab_bar.setScrollable(True)
        self._tab_bar.tabCloseRequested.connect(self._close_tab)
        self._tab_bar.currentChanged.connect(self._on_tab_changed)

        dark = isDarkTheme()
        self._tab_bar.setStyleSheet(f"""
            TabBar {{
                background: {'#14142a' if dark else '#e8e8f5'};
                border-bottom: 1px solid {'#2a2a45' if dark else '#ccccdd'};
            }}
        """)
        layout.addWidget(self._tab_bar)

        self._content_area = QWidget()
        self._content_layout = QVBoxLayout(self._content_area)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._content_area, 1)

        self._active_widget: QWidget | None = None
        self._stack: dict[str, QWidget] = {}
        self._add_builtin_tab("__dashboard__", "Dashboard", self._dashboard, FluentIcon.HOME)

    def _add_builtin_tab(self, tab_id: str, label: str, widget: QWidget, icon: Any) -> None:
        self._tab_bar.addTab(tab_id, label, icon)
        self._stack[tab_id] = widget
        if self._active_widget is None:
            self._content_layout.addWidget(widget)
            self._active_widget = widget

    def add_log_tab(self, tab_id: str, label: str, provider: Any) -> None:
        if tab_id in self._stack:
            idx = self._find_tab_index(tab_id)
            if idx >= 0:
                self._tab_bar.setCurrentIndex(idx)
            return

        view = VirtualLogView(source_id=tab_id)
        view.model.error_appeared.connect(lambda tid=tab_id: self._mark_error(tid))
        view.attach_provider(provider)

        self._tabs[tab_id] = view
        self._stack[tab_id] = view
        self._tab_bar.addTab(tab_id, label, FluentIcon.DOCUMENT)

        idx = self._find_tab_index(tab_id)
        if idx >= 0:
            self._tab_bar.setCurrentIndex(idx)

    def _on_tab_changed(self, index: int) -> None:
        if index < 0:
            return
        tab_id = self._get_tab_id_by_index(index)
        if not tab_id or tab_id not in self._stack:
            return

        widget = self._stack[tab_id]
        if self._active_widget is widget:
            return

        if self._active_widget:
            self._active_widget.setVisible(False)
            self._content_layout.removeWidget(self._active_widget)

        self._content_layout.addWidget(widget)
        widget.setVisible(True)
        self._active_widget = widget

        if tab_id in self._error_tabs:
            self._error_tabs.discard(tab_id)
        if tab_id in self._tabs:
            self._tabs[tab_id].model.clear_error_flag()

    def _close_tab(self, index: int) -> None:
        tab_id = self._get_tab_id_by_index(index)
        if not tab_id or tab_id == "__dashboard__":
            return

        if tab_id in self._tabs:
            self._tabs[tab_id].stop()
            del self._tabs[tab_id]

        widget = self._stack.pop(tab_id, None)
        if widget:
            widget.setVisible(False)
            self._content_layout.removeWidget(widget)
            widget.deleteLater()

        self._error_tabs.discard(tab_id)
        self._tab_bar.removeTab(index)

    def _mark_error(self, tab_id: str) -> None:
        idx = self._find_tab_index(tab_id)
        if idx < 0:
            return
        current_id = self._get_tab_id_by_index(self._tab_bar.currentIndex())
        if tab_id == current_id:
            return
        self._error_tabs.add(tab_id)
        self._pulse_tab(idx)

    def _pulse_tab(self, index: int) -> None:
        pulse_count = [0]

        def toggle() -> None:
            if pulse_count[0] >= 6:
                try:
                    self._tab_bar.setTabIcon(index, FluentIcon.DOCUMENT)
                except Exception:
                    pass
                return
            icon = FluentIcon.CANCEL_MEDIUM if pulse_count[0] % 2 == 0 else FluentIcon.DOCUMENT
            try:
                self._tab_bar.setTabIcon(index, icon)
            except Exception:
                pass
            pulse_count[0] += 1

        timer = QTimer(self)
        timer.setInterval(280)
        timer.timeout.connect(toggle)
        timer.start()
        QTimer.singleShot(1700, timer.stop)

    def _find_tab_index(self, tab_id: str) -> int:
        for i in range(self._tab_bar.count()):
            if self._get_tab_id_by_index(i) == tab_id:
                return i
        return -1

    def _get_tab_id_by_index(self, index: int) -> str:
        if index < 0 or index >= self._tab_bar.count():
            return ""
        item = self._tab_bar.tabItem(index)
        if item:
            return item.routeKey()
        return ""


class LogTreePanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.on_open_log: Any = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(4)

        header = QWidget()
        hl = QHBoxLayout(header)
        hl.setContentsMargins(10, 0, 10, 4)
        title = SubtitleLabel("Log Sources")
        hl.addWidget(title)
        layout.addWidget(header)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setColumnCount(1)
        self._tree.setIndentation(14)
        dark = isDarkTheme()
        self._tree.setStyleSheet(f"""
            QTreeWidget {{
                background: {'#13132a' if dark else '#ededf8'};
                border: none;
                color: {'#d0d0e8' if dark else '#1a1a2a'};
                font-size: 12px;
                outline: none;
            }}
            QTreeWidget::item {{
                padding: 3px 6px;
                border-radius: 4px;
            }}
            QTreeWidget::item:selected {{
                background: {'#2d2d50' if dark else '#ccd4f0'};
                color: {'#ffffff' if dark else '#000000'};
            }}
            QTreeWidget::item:hover:!selected {{
                background: {'#1e1e3a' if dark else '#dde4f5'};
            }}
            QTreeWidget::branch:has-children:closed {{
                image: none;
            }}
            QTreeWidget::branch:has-children:open {{
                image: none;
            }}
            QScrollBar:vertical {{
                width: 4px;
                background: transparent;
            }}
            QScrollBar::handle:vertical {{
                background: {'#3a3a5a' if dark else '#bbbbdd'};
                border-radius: 2px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self._tree, 1)

    def populate_from_discovery(self, logs: list[DiscoveredLog]) -> None:
        self._tree.clear()
        groups: dict[str, QTreeWidgetItem] = {}

        for log in logs:
            if log.group not in groups:
                group_item = QTreeWidgetItem([f"  {log.group}"])
                group_item.setData(0, Qt.ItemDataRole.UserRole, None)
                group_item.setExpanded(False)
                self._tree.addTopLevelItem(group_item)
                groups[log.group] = group_item

            child = QTreeWidgetItem([f"  {log.name}"])
            child.setData(0, Qt.ItemDataRole.UserRole, log.path)
            child.setToolTip(0, log.path)
            if not log.readable:
                child.setForeground(0, QColor("#ff6666"))
                child.setToolTip(0, f"{log.path}\n⚠ Permission denied — re-add with 'Use sudo'")
            groups[log.group].addChild(child)

    def add_source_item(self, label: str, source_id: str, group: str = "Custom") -> None:
        root = self._tree.invisibleRootItem()
        group_item: QTreeWidgetItem | None = None

        for i in range(root.childCount()):
            item = root.child(i)
            if item and item.text(0).strip() == group:
                group_item = item
                break

        if group_item is None:
            group_item = QTreeWidgetItem([f"  {group}"])
            self._tree.addTopLevelItem(group_item)

        child = QTreeWidgetItem([f"  {label}"])
        child.setData(0, Qt.ItemDataRole.UserRole, source_id)
        group_item.addChild(child)
        group_item.setExpanded(True)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _: int) -> None:
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if path and self.on_open_log:
            self.on_open_log(path, item.text(0).strip())


class SettingsPage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self._setup_ui()

    def _setup_ui(self) -> None:
        dark = isDarkTheme()
        self.setStyleSheet(f"background: {'#13132a' if dark else '#f0f0fa'};")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent; border: none;")
        outer.addWidget(scroll)

        container = QWidget()
        scroll.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(40, 24, 40, 40)
        layout.setSpacing(20)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        page_title = TitleLabel("Settings")
        layout.addWidget(page_title)

        layout.addWidget(self._make_section(
            "Appearance",
            [
                self._make_row("Theme", self._make_theme_switch()),
                self._make_row("Accent color", self._make_accent_info()),
            ]
        ))

        layout.addWidget(self._make_section(
            "Log Behaviour",
            [
                self._make_row("Max lines per tab", self._make_spinbox(100_000, 1000, 1_000_000, 10000)),
                self._make_row("Flush interval (ms)", self._make_spinbox(80, 16, 2000, 10)),
                self._make_row("Auto-scroll new tabs", self._make_switch(True)),
                self._make_row("Auto-expand tracebacks", self._make_switch(False)),
            ]
        ))

        layout.addWidget(self._make_section(
            "Discovery — Ignored file patterns",
            [self._make_ignored_list()]
        ))

        layout.addStretch()

        layout.addWidget(self._make_about_card())

    def _make_section(self, title: str, children: list[QWidget]) -> QWidget:
        dark = isDarkTheme()
        card = QWidget()
        card.setStyleSheet(f"""
            QWidget#section {{
                background: {'#1a1a2e' if dark else '#ffffff'};
                border: 1px solid {'#2a2a45' if dark else '#ddddf0'};
                border-radius: 10px;
            }}
        """)
        card.setObjectName("section")
        vl = QVBoxLayout(card)
        vl.setContentsMargins(18, 14, 18, 14)
        vl.setSpacing(2)

        hdr = StrongBodyLabel(title)
        hdr.setStyleSheet(f"color: {'#a090d0' if dark else '#5544aa'}; font-size: 12px; text-transform: uppercase; letter-spacing: 1px;")
        vl.addWidget(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {'#2a2a45' if dark else '#e0e0f0'}; margin-bottom: 4px;")
        vl.addWidget(sep)

        for child in children:
            vl.addWidget(child)

        return card

    def _make_row(self, label: str, widget: QWidget) -> QWidget:
        dark = isDarkTheme()
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(2, 6, 2, 6)
        lbl = BodyLabel(label)
        lbl.setStyleSheet(f"color: {'#c0c0e0' if dark else '#2a2a3a'};")
        rl.addWidget(lbl, 1)
        rl.addWidget(widget)
        return row

    def _make_theme_switch(self) -> QWidget:
        combo = ComboBox()
        combo.addItems(["Dark", "Light", "System"])
        combo.setCurrentIndex(0)
        combo.setMaximumWidth(120)
        combo.currentTextChanged.connect(self._on_theme_changed)
        return combo

    def _on_theme_changed(self, text: str) -> None:
        mapping = {"Dark": Theme.DARK, "Light": Theme.LIGHT, "System": Theme.AUTO}
        setTheme(mapping.get(text, Theme.DARK))

    def _make_accent_info(self) -> QWidget:
        lbl = CaptionLabel("#7c5cbf  (hardcoded, edit source to change)")
        lbl.setStyleSheet("color: #7c5cbf;")
        return lbl

    def _make_spinbox(self, default: int, mn: int, mx: int, step: int) -> QWidget:
        sb = QSpinBox()
        sb.setRange(mn, mx)
        sb.setValue(default)
        sb.setSingleStep(step)
        sb.setMaximumWidth(120)
        return sb

    def _make_switch(self, default: bool) -> QWidget:
        sw = SwitchButton()
        sw.setChecked(default)
        return sw

    def _make_ignored_list(self) -> QWidget:
        dark = isDarkTheme()
        container = QWidget()
        vl = QVBoxLayout(container)
        vl.setContentsMargins(0, 4, 0, 0)
        vl.setSpacing(4)

        note = CaptionLabel(
            "Files matching these patterns are skipped during auto-discovery:"
        )
        note.setStyleSheet(f"color: {'#888899' if dark else '#666677'}; font-size: 11px;")
        vl.addWidget(note)

        grid = QWidget()
        gl = QHBoxLayout(grid)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.setSpacing(8)
        gl.setAlignment(Qt.AlignmentFlag.AlignLeft)

        for pattern in IGNORED_PATTERNS:
            tag = QLabel(pattern)
            tag.setStyleSheet(f"""
                QLabel {{
                    background: {'#252545' if dark else '#e8e8fa'};
                    color: {'#aaaacc' if dark else '#444466'};
                    border: 1px solid {'#3a3a5a' if dark else '#ccccee'};
                    border-radius: 5px;
                    padding: 2px 8px;
                    font-size: 11px;
                    font-family: 'Fira Code', monospace;
                }}
            """)
            gl.addWidget(tag)

        vl.addWidget(grid)
        return container

    def _make_about_card(self) -> QWidget:
        dark = isDarkTheme()
        card = QWidget()
        card.setStyleSheet(f"""
            QWidget#aboutCard {{
                background: {'#1a1a2e' if dark else '#ffffff'};
                border: 1px solid {'#2a2a45' if dark else '#ddddf0'};
                border-radius: 10px;
            }}
        """)
        card.setObjectName("aboutCard")
        vl = QVBoxLayout(card)
        vl.setContentsMargins(18, 18, 18, 18)
        vl.setSpacing(8)

        title = StrongBodyLabel("About TailView")
        title.setStyleSheet(f"font-size: 15px; color: {'#e0e0f0' if dark else '#1a1a2a'};")
        vl.addWidget(title)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {'#2a2a45' if dark else '#e0e0f0'};")
        vl.addWidget(sep)

        lines = [
            ("Version", "1.2.0"),
            ("Developer", "pon4ikisdonut"),
            ("Year", "2026"),
            ("License", "MIT"),
            ("Platform", "Windows · Linux (incl. openSUSE Tumbleweed)"),
        ]
        for key, val in lines:
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            k = BodyLabel(f"{key}:")
            k.setStyleSheet(f"color: {'#888899' if dark else '#666677'}; min-width: 90px;")
            v = BodyLabel(val)
            v.setStyleSheet(f"color: {'#d0d0e8' if dark else '#1a1a2a'};")
            rl.addWidget(k)
            rl.addWidget(v, 1)
            vl.addWidget(row)

        github_btn = HyperlinkButton("https://github.com/pon4ikisdonut", "github.com/pon4ikisdonut")
        github_btn.setMaximumWidth(240)
        vl.addWidget(github_btn)

        return card


class TailViewWindow(FluentWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("TailView")
        self.setMinimumSize(1100, 680)
        self.resize(1400, 860)
        setTheme(Theme.DARK)
        setThemeColor("#7c5cbf")
        self._setup_ui()
        self._run_discovery()

    def _setup_ui(self) -> None:
        self._dashboard = DashboardView()
        self._tab_area = LogTabArea(self._dashboard)

        self._log_tree = LogTreePanel()
        self._log_tree.on_open_log = self._open_local_log
        self._log_tree.setMinimumWidth(190)
        self._log_tree.setMaximumWidth(290)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._log_tree)
        splitter.addWidget(self._tab_area)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 1000])
        splitter.setHandleWidth(1)
        dark = isDarkTheme()
        splitter.setStyleSheet(f"QSplitter::handle {{ background: {'#2a2a45' if dark else '#ccccdd'}; }}")

        central = QWidget()
        central.setObjectName("tailViewCentral")
        cv = QVBoxLayout(central)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)

        toolbar = QWidget()
        toolbar.setFixedHeight(48)
        toolbar.setStyleSheet(f"background: {'#11112a' if dark else '#e8e8f8'}; border-bottom: 1px solid {'#2a2a45' if dark else '#ccccdd'};")
        tb = QHBoxLayout(toolbar)
        tb.setContentsMargins(12, 6, 12, 6)
        tb.setSpacing(8)

        self._add_btn = PrimaryPushButton(FluentIcon.ADD, "Add Source")
        self._add_btn.setFixedHeight(32)
        self._add_btn.clicked.connect(self._show_add_dialog)

        self._rescan_btn = TransparentPushButton(FluentIcon.SYNC, "Rescan")
        self._rescan_btn.setFixedHeight(32)
        self._rescan_btn.clicked.connect(self._run_discovery)

        app_label = BodyLabel("TailView")
        app_label.setStyleSheet(f"color: {'#7c5cbf'}; font-size: 15px; font-weight: 600; letter-spacing: 1px;")

        tb.addWidget(app_label)
        tb.addSpacing(16)
        tb.addWidget(self._add_btn)
        tb.addWidget(self._rescan_btn)
        tb.addStretch()

        cv.addWidget(toolbar)
        cv.addWidget(splitter, 1)

        self.addSubInterface(
            interface=central,
            icon=FluentIcon.HOME,
            text="TailView",
            position=NavigationItemPosition.TOP,
        )

        self._settings_page = SettingsPage()
        self.addSubInterface(
            interface=self._settings_page,
            icon=FluentIcon.SETTING,
            text="Settings",
            position=NavigationItemPosition.BOTTOM,
        )

    def _run_discovery(self) -> None:
        logs = discover_local_logs()
        self._log_tree.populate_from_discovery(logs)
        readable = sum(1 for l in logs if l.readable)
        locked  = len(logs) - readable
        msg = f"Found {len(logs)} log files"
        if locked:
            msg += f" ({locked} require sudo)"
        InfoBar.success(
            title="Discovery complete",
            content=msg,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=3000,
            parent=self,
        )

    def _open_local_log(self, path: str, label: str) -> None:
        from providers import LocalFileConfig, LocalFileProvider
        cfg = LocalFileConfig(path=path, tail_lines=200)
        provider = LocalFileProvider(cfg)
        tab_id = f"local:{path}"
        self._tab_area.add_log_tab(tab_id, label, provider)

    def _show_add_dialog(self) -> None:
        dlg = AddSourceDialog(self)
        if dlg.exec():
            result = dlg.result_config
            if result:
                provider, label = result
                tab_id = provider.source_id
                self._tab_area.add_log_tab(tab_id, label, provider)
                self._log_tree.add_source_item(
                    label=label,
                    source_id=tab_id,
                    group=label.split(":")[0] if ":" in label else "Custom",
                )


def main() -> None:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("TailView")
    app.setApplicationVersion("1.2.0")
    app.setOrganizationName("TailView")
    window = TailViewWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()