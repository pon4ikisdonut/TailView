from __future__ import annotations

import sys
import os
from typing import Any

from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QIcon, QColor
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTreeWidget, QTreeWidgetItem,
)
from qfluentwidgets import (
    FluentWindow, NavigationItemPosition, FluentIcon,
    setTheme, Theme, setThemeColor,
    NavigationInterface, NavigationTreeWidget,
    SubtitleLabel, BodyLabel, PrimaryPushButton,
    TransparentPushButton, InfoBar, InfoBarPosition,
    TabBar, TabCloseButtonDisplayMode,
    isDarkTheme, SplashScreen,
    NavigationAvatarWidget,
)
from qfluentwidgets import FluentWindow

from ui_core import (
    VirtualLogView, DashboardView, AddSourceDialog,
    LogModel, LogLevel,
)
from providers import discover_local_logs, DiscoveredLog


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
        self._tab_bar.setTabShadowEnabled(True)
        self._tab_bar.setMovable(True)
        self._tab_bar.setScrollable(True)
        self._tab_bar.tabCloseRequested.connect(self._close_tab)
        self._tab_bar.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self._tab_bar)

        self._stack: dict[str, QWidget] = {}
        self._content_area = QWidget()
        self._content_layout = QVBoxLayout(self._content_area)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._content_area, 1)

        self._active_widget: QWidget | None = None

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

        view.model.error_appeared.connect(
            lambda: None  
        )

        self._tabs[tab_id] = view
        self._stack[tab_id] = view
        self._tab_bar.addTab(tab_id, label, FluentIcon.DOCUMENT)

        idx = self._find_tab_index(tab_id)
        if idx >= 0:
            self._tab_bar.setCurrentIndex(idx)

    def _on_tab_changed(self, index: int) -> None:
        if index < 0:
            return
        tab_id = self._tab_bar.tabText(index)
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
            self._update_tab_style(index, error=False)
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
        self._update_tab_style(idx, error=True)

    def _update_tab_style(self, index: int, error: bool) -> None:
        tab_bar = self._tab_bar
        if error:
            self._pulse_tab(index)

    def _pulse_tab(self, index: int) -> None:
        """Простой мигающий эффект через изменение иконки."""
        original_icon = self._tab_bar.tabIcon(index)
        pulse_count = [0]

        def toggle() -> None:
            if pulse_count[0] >= 6:
                self._tab_bar.setTabIcon(index, FluentIcon.DOCUMENT)
                return
            icon = FluentIcon.INFO if pulse_count[0] % 2 == 0 else FluentIcon.DOCUMENT
            try:
                self._tab_bar.setTabIcon(index, icon)
            except Exception:
                pass
            pulse_count[0] += 1

        timer = QTimer(self)
        timer.setInterval(300)
        timer.timeout.connect(toggle)
        timer.start()
        QTimer.singleShot(1800, timer.stop)

    def _find_tab_index(self, tab_id: str) -> int:
        for i in range(self._tab_bar.count()):
            if self._tab_bar.tabText(i) == tab_id:
                return i
            try:
                if self._tab_bar.tabData(i) == tab_id:
                    return i
            except Exception:
                pass
        return -1

    def _get_tab_id_by_index(self, index: int) -> str:
        if index < 0 or index >= self._tab_bar.count():
            return ""
        item = self._tab_bar.tabItem(index)
        if item:
            return item.routeKey()
        return self._tab_bar.tabText(index)



class LogTreePanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._setup_ui()
        self.on_open_log: Any = None  # callback(path, label)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(4)

        header = SubtitleLabel("Log Sources")
        layout.addWidget(header)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setColumnCount(1)
        dark = isDarkTheme()
        self._tree.setStyleSheet(f"""
            QTreeWidget {{
                background: {'#1a1a2e' if dark else '#f0f0f0'};
                border: none;
                color: {'#e0e0e0' if dark else '#1a1a1a'};
                font-size: 13px;
            }}
            QTreeWidget::item:selected {{
                background: {'#2d2d50' if dark else '#c0d0f0'};
                border-radius: 4px;
            }}
            QTreeWidget::item:hover {{
                background: {'#252540' if dark else '#dde4f8'};
                border-radius: 4px;
            }}
        """)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self._tree, 1)

    def populate_from_discovery(self, logs: list[DiscoveredLog]) -> None:
        self._tree.clear()
        groups: dict[str, QTreeWidgetItem] = {}

        for log in logs:
            if log.group not in groups:
                group_item = QTreeWidgetItem([log.group])
                group_item.setData(0, Qt.ItemDataRole.UserRole, None)
                self._tree.addTopLevelItem(group_item)
                groups[log.group] = group_item

            child = QTreeWidgetItem([log.name])
            child.setData(0, Qt.ItemDataRole.UserRole, log.path)
            child.setToolTip(0, log.path)
            groups[log.group].addChild(child)

        self._tree.expandAll()

    def add_source_item(self, label: str, source_id: str, group: str = "Custom") -> None:
        root = self._tree.invisibleRootItem()
        group_item: QTreeWidgetItem | None = None

        for i in range(root.childCount()):
            item = root.child(i)
            if item and item.text(0) == group:
                group_item = item
                break

        if group_item is None:
            group_item = QTreeWidgetItem([group])
            self._tree.addTopLevelItem(group_item)

        child = QTreeWidgetItem([label])
        child.setData(0, Qt.ItemDataRole.UserRole, source_id)
        group_item.addChild(child)
        group_item.setExpanded(True)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, col: int) -> None:
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if path and self.on_open_log:
            self.on_open_log(path, item.text(0))



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
        self._log_tree.setMinimumWidth(200)
        self._log_tree.setMaximumWidth(300)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._log_tree)
        splitter.addWidget(self._tab_area)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 1000])
        splitter.setHandleWidth(1)

        central = QWidget()
        central.setObjectName("tailViewCentral")
        cv_layout = QVBoxLayout(central)
        cv_layout.setContentsMargins(0, 0, 0, 0)
        cv_layout.setSpacing(0)

        toolbar = QWidget()
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(8, 6, 8, 6)

        self._add_btn = PrimaryPushButton(FluentIcon.ADD, "Add Source")
        self._add_btn.clicked.connect(self._show_add_dialog)

        self._rescan_btn = TransparentPushButton(FluentIcon.SYNC, "Rescan")
        self._rescan_btn.clicked.connect(self._run_discovery)

        tb_layout.addWidget(self._add_btn)
        tb_layout.addWidget(self._rescan_btn)
        tb_layout.addStretch()

        cv_layout.addWidget(toolbar)
        cv_layout.addWidget(splitter, 1)

        self.addSubInterface(
            interface=central,
            icon=FluentIcon.HOME,
            text="TailView",
            position=NavigationItemPosition.TOP,
        )

    def _run_discovery(self) -> None:
        logs = discover_local_logs()
        self._log_tree.populate_from_discovery(logs)

        if logs:
            InfoBar.success(
                title="Discovery complete",
                content=f"Found {len(logs)} log files",
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
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("TailView")

    window = TailViewWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()