from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import (
    Qt, QAbstractListModel, QModelIndex, QTimer, Signal, QThread, QObject,
    QRectF, QSize,
)
from PySide6.QtGui import (
    QColor, QPainter, QFont, QFontMetrics, QPen, QBrush,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListView, QStyledItemDelegate,
    QAbstractItemView, QLabel, QFrame, QSizePolicy, QSplitter,
    QScrollArea, QTreeWidget, QTreeWidgetItem, QApplication,
)
from qfluentwidgets import (
    FluentIcon, TabBar, TabCloseButtonDisplayMode,
    PushButton, ToolButton, SearchLineEdit,
    ComboBox, CheckBox, BodyLabel, StrongBodyLabel,
    isDarkTheme, setTheme, Theme, themeColor,
    ScrollArea, TransparentPushButton,
    InfoBar, InfoBarPosition,
)

from parser import LogEntry, LogLevel, LEVEL_COLORS, LogParser, StackTraceCollector


LOG_ENTRY_ROLE = Qt.ItemDataRole.UserRole + 1
EXPANDED_ROLE   = Qt.ItemDataRole.UserRole + 2


class ProviderWorker(QObject):
    line_received = Signal(str)
    error_occurred = Signal(str)
    finished = Signal()

    def __init__(self, provider: Any) -> None:
        super().__init__()
        self._provider = provider
        self._stop_event = threading.Event()

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._consume())
        finally:
            loop.close()
            self.finished.emit()

    async def _consume(self) -> None:
        try:
            async for line in self._provider.stream():
                if self._stop_event.is_set():
                    break
                self.line_received.emit(line)
        except Exception as e:
            self.error_occurred.emit(str(e))

    def stop(self) -> None:
        self._stop_event.set()
        self._provider.stop()


class ProviderThread(QThread):
    line_received = Signal(str)
    error_occurred = Signal(str)

    def __init__(self, provider: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._worker = ProviderWorker(provider)
        self._worker.line_received.connect(self.line_received)
        self._worker.error_occurred.connect(self.error_occurred)

    def run(self) -> None:
        self._worker.run()

    def stop_provider(self) -> None:
        self._worker.stop()
        self.wait(3000)


MAX_ENTRIES = 100_000 


class LogModel(QAbstractListModel):
    error_appeared = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._entries: deque[LogEntry] = deque(maxlen=MAX_ENTRIES)
        self._expanded: set[int] = set()
        self._filter_level: LogLevel | None = None
        self._filter_text: str = ""
        self._visible: list[int] = []   
        self._has_error = False
        self._stack_collector = StackTraceCollector()


    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._visible)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= len(self._visible):
            return None
        real_idx = self._visible[index.row()]
        entry = list(self._entries)[real_idx]
        if role == LOG_ENTRY_ROLE:
            return entry
        if role == EXPANDED_ROLE:
            return real_idx in self._expanded
        if role == Qt.ItemDataRole.DisplayRole:
            return entry.message or entry.raw
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable


    def append_lines(self, lines: list[str], source_id: str) -> None:
        new_entries: list[LogEntry] = []
        for line in lines:
            parsed = LogParser.parse(line, source_id)
            flushed = self._stack_collector.feed(parsed)
            new_entries.extend(flushed)

        if not new_entries:
            return

        has_error = any(
            e.level in (LogLevel.ERROR, LogLevel.CRITICAL)
            for e in new_entries
        )
        if has_error:
            self._has_error = True
            self.error_appeared.emit()

        current_len = len(self._entries)
        to_add = new_entries

        overflow = max(0, current_len + len(to_add) - MAX_ENTRIES)

        self._entries.extend(to_add)

        if overflow > 0 or self._filter_level or self._filter_text:
            self._rebuild_visible()
        else:
            base_idx = current_len
            old_vis_count = len(self._visible)
            for i, entry in enumerate(to_add):
                if self._passes_filter(entry):
                    self._visible.append(base_idx + i)

            added = len(self._visible) - old_vis_count
            if added > 0:
                self.beginInsertRows(
                    QModelIndex(),
                    old_vis_count,
                    old_vis_count + added - 1,
                )
                self.endInsertRows()

    def toggle_expand(self, vis_row: int) -> None:
        if vis_row >= len(self._visible):
            return
        real_idx = self._visible[vis_row]
        if real_idx in self._expanded:
            self._expanded.discard(real_idx)
        else:
            self._expanded.add(real_idx)
        idx = self.index(vis_row)
        self.dataChanged.emit(idx, idx, [EXPANDED_ROLE])

    def set_filter(self, text: str = "", level: LogLevel | None = None) -> None:
        self._filter_text = text.lower()
        self._filter_level = level
        self._rebuild_visible()

    def _passes_filter(self, entry: LogEntry) -> bool:
        if self._filter_level and entry.level != self._filter_level:
            return False
        if self._filter_text and self._filter_text not in (entry.message or entry.raw).lower():
            return False
        return True

    def _rebuild_visible(self) -> None:
        self.beginResetModel()
        entries_list = list(self._entries)
        self._visible = [
            i for i, e in enumerate(entries_list)
            if self._passes_filter(e)
        ]
        self.endResetModel()

    def clear_error_flag(self) -> None:
        self._has_error = False

    @property
    def has_error(self) -> bool:
        return self._has_error

    def clear(self) -> None:
        self.beginResetModel()
        self._entries.clear()
        self._visible.clear()
        self._expanded.clear()
        self._has_error = False
        self.endResetModel()


class LogItemDelegate(QStyledItemDelegate):
    PADDING_V = 4
    PADDING_H = 8
    INDICATOR_W = 4
    LINE_HEIGHT = 20

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._font = QFont("Consolas, Fira Code, Courier New", 10)
        self._fm = QFontMetrics(self._font)

    def sizeHint(self, option: Any, index: QModelIndex) -> QSize:
        entry: LogEntry | None = index.data(LOG_ENTRY_ROLE)
        expanded: bool = index.data(EXPANDED_ROLE) or False
        if not entry:
            return QSize(100, self.LINE_HEIGHT + self.PADDING_V * 2)

        base_h = self.LINE_HEIGHT + self.PADDING_V * 2

        if expanded and (entry.stacktrace_lines or entry.json_data):
            extra_lines = len(entry.stacktrace_lines) if entry.stacktrace_lines else len(entry.json_data)
            extra_h = min(extra_lines, 20) * self.LINE_HEIGHT
            return QSize(option.rect.width(), base_h + extra_h + self.PADDING_V)

        return QSize(option.rect.width(), base_h)

    def paint(self, painter: QPainter, option: Any, index: QModelIndex) -> None:
        entry: LogEntry | None = index.data(LOG_ENTRY_ROLE)
        expanded: bool = index.data(EXPANDED_ROLE) or False

        painter.save()
        rect = option.rect

        bg_color = QColor("#1e1e2e") if isDarkTheme() else QColor("#f8f8f8")
        from PySide6.QtWidgets import QStyle
        if option.state & QStyle.StateFlag.State_Selected:
            bg_color = QColor("#2d2d44") if isDarkTheme() else QColor("#dde4f0")
        painter.fillRect(rect, bg_color)

        if not entry:
            painter.restore()
            return

        level_color = QColor(LEVEL_COLORS.get(entry.level, "#cccccc"))
        painter.fillRect(rect.x(), rect.y(), self.INDICATOR_W, rect.height(), level_color)

        painter.setFont(self._font)

        x = rect.x() + self.INDICATOR_W + self.PADDING_H
        y = rect.y() + self.PADDING_V + self.LINE_HEIGHT - 4

        if entry.timestamp:
            painter.setPen(QColor("#666699"))
            ts_text = entry.timestamp[:23] + " "
            ts_w = self._fm.horizontalAdvance(ts_text)
            painter.drawText(x, y, ts_text)
            x += ts_w

        if entry.level != LogLevel.UNKNOWN:
            painter.setPen(level_color)
            lvl_text = f"[{entry.level.name:<8}] "
            lvl_w = self._fm.horizontalAdvance(lvl_text)
            painter.drawText(x, y, lvl_text)
            x += lvl_w

        if entry.service:
            painter.setPen(QColor("#aa88ff"))
            svc_text = f"{entry.service}: "
            svc_w = self._fm.horizontalAdvance(svc_text)
            painter.drawText(x, y, svc_text)
            x += svc_w

        painter.setPen(QColor("#e0e0e0") if isDarkTheme() else QColor("#1a1a1a"))
        avail_w = rect.right() - x - self.PADDING_H - 16 

        has_expand = bool(entry.stacktrace_lines or (entry.is_json and entry.json_data))
        if has_expand:
            arrow = "▼" if expanded else "▶"
            painter.setPen(QColor("#888888"))
            painter.drawText(rect.right() - 20, y, arrow)
            painter.setPen(QColor("#e0e0e0") if isDarkTheme() else QColor("#1a1a1a"))

        msg = entry.message or entry.raw
        elided = self._fm.elidedText(msg, Qt.TextElideMode.ElideRight, max(avail_w, 50))
        painter.drawText(x, y, elided)

        if expanded:
            sub_y = rect.y() + self.LINE_HEIGHT + self.PADDING_V * 2
            painter.setPen(QColor("#888888"))
            sub_font = QFont(self._font)
            sub_font.setPointSize(9)
            painter.setFont(sub_font)

            if entry.stacktrace_lines:
                for line in entry.stacktrace_lines[:20]:
                    painter.drawText(
                        rect.x() + self.INDICATOR_W + self.PADDING_H * 2,
                        sub_y + self.LINE_HEIGHT - 4,
                        line,
                    )
                    sub_y += self.LINE_HEIGHT

            elif entry.is_json and entry.json_data:
                self._paint_json(painter, entry.json_data, rect.x() + self.INDICATOR_W + self.PADDING_H * 2, sub_y)

        painter.restore()

    def _paint_json(self, painter: QPainter, data: dict, x: int, start_y: int) -> None:
        fm = QFontMetrics(painter.font())
        y = start_y + self.LINE_HEIGHT - 4
        for key, val in list(data.items())[:15]:
            painter.setPen(QColor("#aa88ff"))
            key_text = f"{key}: "
            painter.drawText(x, y, key_text)
            painter.setPen(QColor("#88ddaa"))
            painter.drawText(x + fm.horizontalAdvance(key_text), y, str(val)[:120])
            y += self.LINE_HEIGHT


class VirtualLogView(QWidget):
    def __init__(self, source_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.source_id = source_id
        self._model = LogModel()
        self._model.error_appeared.connect(self._on_error_appeared)
        self._delegate = LogItemDelegate()
        self._pending: list[str] = []
        self._auto_scroll = True
        self._thread: ProviderThread | None = None

        self._setup_ui()
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(80)  # 80ms батч
        self._flush_timer.timeout.connect(self._flush_pending)
        self._flush_timer.start()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        toolbar = QWidget()
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(8, 4, 8, 4)

        self._search = SearchLineEdit()
        self._search.setPlaceholderText("Filter logs...")
        self._search.setMaximumWidth(280)
        self._search.textChanged.connect(self._on_filter_changed)

        self._level_combo = ComboBox()
        self._level_combo.addItems(["All levels", "TRACE", "DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"])
        self._level_combo.setCurrentIndex(0)
        self._level_combo.currentTextChanged.connect(self._on_level_changed)
        self._level_combo.setMaximumWidth(140)

        self._auto_scroll_cb = CheckBox("Auto-scroll")
        self._auto_scroll_cb.setChecked(True)
        self._auto_scroll_cb.checkStateChanged.connect(
            lambda s: setattr(self, "_auto_scroll", s == Qt.CheckState.Checked)
        )

        self._clear_btn = TransparentPushButton(FluentIcon.DELETE, "Clear")
        self._clear_btn.clicked.connect(self._model.clear)

        self._status_label = BodyLabel("0 lines")

        tb_layout.addWidget(self._search)
        tb_layout.addWidget(self._level_combo)
        tb_layout.addWidget(self._auto_scroll_cb)
        tb_layout.addStretch()
        tb_layout.addWidget(self._status_label)
        tb_layout.addWidget(self._clear_btn)

        layout.addWidget(toolbar)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        layout.addWidget(sep)

        self._list_view = QListView()
        self._list_view.setModel(self._model)
        self._list_view.setItemDelegate(self._delegate)
        self._list_view.setUniformItemSizes(False)
        self._list_view.setResizeMode(QListView.ResizeMode.Adjust)
        self._list_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list_view.clicked.connect(self._on_item_clicked)

        dark = isDarkTheme()
        self._list_view.setStyleSheet(f"""
            QListView {{
                background-color: {'#1e1e2e' if dark else '#f8f8f8'};
                border: none;
                outline: none;
            }}
            QScrollBar:vertical {{
                width: 6px;
                background: transparent;
            }}
            QScrollBar::handle:vertical {{
                background: {'#444466' if dark else '#aaaacc'};
                border-radius: 3px;
                min-height: 20px;
            }}
        """)

        layout.addWidget(self._list_view, 1)

        self._error_banner = InfoBar.error(
            title="Error detected",
            content="An ERROR/CRITICAL entry appeared in this log.",
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.BOTTOM,
            duration=5000,
            parent=self,
        )
        self._error_banner.close()

    def _on_item_clicked(self, index: QModelIndex) -> None:
        entry = index.data(LOG_ENTRY_ROLE)
        if entry and (entry.stacktrace_lines or (entry.is_json and entry.json_data)):
            self._model.toggle_expand(index.row())
            self._list_view.update(index)

    def _on_filter_changed(self, text: str) -> None:
        level = self._parse_level_from_combo()
        self._model.set_filter(text, level)
        self._update_status()

    def _on_level_changed(self, text: str) -> None:
        level = self._parse_level_from_combo()
        self._model.set_filter(self._search.text(), level)
        self._update_status()

    def _parse_level_from_combo(self) -> LogLevel | None:
        from parser import LogLevel as LL
        mapping = {
            "TRACE": LL.TRACE, "DEBUG": LL.DEBUG, "INFO": LL.INFO,
            "WARN": LL.WARN, "ERROR": LL.ERROR, "CRITICAL": LL.CRITICAL,
        }
        return mapping.get(self._level_combo.currentText())

    def _flush_pending(self) -> None:
        if not self._pending:
            return
        batch = self._pending[:]
        self._pending.clear()
        self._model.append_lines(batch, self.source_id)
        self._update_status()
        if self._auto_scroll:
            self._list_view.scrollToBottom()

    def _on_error_appeared(self) -> None:
        pass  

    def _update_status(self) -> None:
        total = len(self._model._entries)
        visible = self._model.rowCount()
        self._status_label.setText(f"{visible:,} / {total:,} lines")

    def feed_line(self, line: str) -> None:
        self._pending.append(line)

    def attach_provider(self, provider: Any) -> None:
        if self._thread:
            self._thread.stop_provider()

        self._thread = ProviderThread(provider, self)
        self._thread.line_received.connect(self.feed_line)
        self._thread.error_occurred.connect(
            lambda e: self.feed_line(f"[TailView ERROR] {e}")
        )
        self._thread.start()

    def stop(self) -> None:
        if self._thread:
            self._thread.stop_provider()
        self._flush_timer.stop()

    @property
    def model(self) -> LogModel:
        return self._model


_DASHBOARD_MAX = 500


class DashboardView(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)

        title = StrongBodyLabel("Dashboard — Live Event Timeline")
        layout.addWidget(title)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep)

        self._model = LogModel()
        self._delegate = LogItemDelegate()

        self._list_view = QListView()
        self._list_view.setModel(self._model)
        self._list_view.setItemDelegate(self._delegate)
        self._list_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._list_view.setUniformItemSizes(True)
        dark = isDarkTheme()
        self._list_view.setStyleSheet(f"""
            QListView {{
                background-color: {'#181824' if dark else '#f0f0f8'};
                border: none;
            }}
            QScrollBar:vertical {{ width: 6px; background: transparent; }}
            QScrollBar::handle:vertical {{
                background: {'#444466' if dark else '#aaaacc'};
                border-radius: 3px;
            }}
        """)
        layout.addWidget(self._list_view, 1)

    def push_line(self, line: str, source_id: str) -> None:
        self._model.append_lines([line], source_id)
        self._list_view.scrollToBottom()


from PySide6.QtWidgets import (
    QDialog, QFormLayout, QLineEdit, QSpinBox,
    QDialogButtonBox, QStackedWidget, QGroupBox,
)
from qfluentwidgets import ComboBox as FComboBox, PrimaryPushButton, LineEdit


class AddSourceDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Log Source")
        self.setMinimumWidth(440)
        self._result_config: Any = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        type_row = QHBoxLayout()
        type_label = BodyLabel("Source type:")
        self._type_combo = FComboBox()
        self._type_combo.addItems(["Local File", "SSH", "Docker", "Kubernetes", "KVM"])
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        type_row.addWidget(type_label)
        type_row.addWidget(self._type_combo, 1)
        layout.addLayout(type_row)

        self._stack = QStackedWidget()

        local_w = QWidget()
        lf = QFormLayout(local_w)
        self._local_path = LineEdit()
        self._local_path.setPlaceholderText("/var/log/nginx/access.log")
        self._local_tail = QSpinBox()
        self._local_tail.setRange(1, 10000)
        self._local_tail.setValue(200)
        lf.addRow("Path:", self._local_path)
        lf.addRow("Tail lines:", self._local_tail)
        self._stack.addWidget(local_w)

        ssh_w = QWidget()
        sf = QFormLayout(ssh_w)
        self._ssh_host = LineEdit(); self._ssh_host.setPlaceholderText("192.168.1.1")
        self._ssh_port = QSpinBox(); self._ssh_port.setRange(1, 65535); self._ssh_port.setValue(22)
        self._ssh_user = LineEdit(); self._ssh_user.setPlaceholderText("root")
        self._ssh_pass = LineEdit(); self._ssh_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self._ssh_key  = LineEdit(); self._ssh_key.setPlaceholderText("~/.ssh/id_rsa")
        self._ssh_path = LineEdit(); self._ssh_path.setPlaceholderText("/var/log/app.log")
        self._ssh_os   = FComboBox(); self._ssh_os.addItems(["linux", "windows"])
        self._ssh_tail = QSpinBox(); self._ssh_tail.setRange(1, 10000); self._ssh_tail.setValue(200)
        sf.addRow("Host:", self._ssh_host)
        sf.addRow("Port:", self._ssh_port)
        sf.addRow("Username:", self._ssh_user)
        sf.addRow("Password:", self._ssh_pass)
        sf.addRow("Key path:", self._ssh_key)
        sf.addRow("Remote path:", self._ssh_path)
        sf.addRow("Remote OS:", self._ssh_os)
        sf.addRow("Tail lines:", self._ssh_tail)
        self._stack.addWidget(ssh_w)

        docker_w = QWidget()
        df = QFormLayout(docker_w)
        self._docker_combo = FComboBox()
        self._docker_name  = LineEdit(); self._docker_name.setPlaceholderText("or type container name/id")
        self._docker_tail  = QSpinBox(); self._docker_tail.setRange(1, 10000); self._docker_tail.setValue(200)
        self._refresh_docker_btn = TransparentPushButton(FluentIcon.SYNC, "Refresh containers")
        self._refresh_docker_btn.clicked.connect(self._refresh_docker)
        df.addRow("Container:", self._docker_combo)
        df.addRow("Manual ID/Name:", self._docker_name)
        df.addRow("Tail lines:", self._docker_tail)
        df.addRow("", self._refresh_docker_btn)
        self._stack.addWidget(docker_w)
        self._refresh_docker()

        k8s_w = QWidget()
        kf = QFormLayout(k8s_w)
        self._k8s_ns    = LineEdit(); self._k8s_ns.setPlaceholderText("default")
        self._k8s_pod   = LineEdit(); self._k8s_pod.setPlaceholderText("my-pod-abc123")
        self._k8s_cont  = LineEdit(); self._k8s_cont.setPlaceholderText("(optional)")
        self._k8s_kubeconfig = LineEdit(); self._k8s_kubeconfig.setPlaceholderText("~/.kube/config")
        self._k8s_tail  = QSpinBox(); self._k8s_tail.setRange(1, 10000); self._k8s_tail.setValue(200)
        kf.addRow("Namespace:", self._k8s_ns)
        kf.addRow("Pod name:", self._k8s_pod)
        kf.addRow("Container:", self._k8s_cont)
        kf.addRow("Kubeconfig:", self._k8s_kubeconfig)
        kf.addRow("Tail lines:", self._k8s_tail)
        self._stack.addWidget(k8s_w)

        kvm_w = QWidget()
        kvf = QFormLayout(kvm_w)
        self._kvm_domain  = LineEdit(); self._kvm_domain.setPlaceholderText("my-vm")
        self._kvm_logpath = LineEdit(); self._kvm_logpath.setPlaceholderText("(auto: /var/log/libvirt/qemu/<name>.log)")
        kvf.addRow("Domain name:", self._kvm_domain)
        kvf.addRow("Log path:", self._kvm_logpath)
        self._stack.addWidget(kvm_w)

        layout.addWidget(self._stack)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _on_type_changed(self, idx: int) -> None:
        self._stack.setCurrentIndex(idx)

    def _refresh_docker(self) -> None:
        from providers import discover_docker_containers
        self._docker_combo.clear()
        containers = discover_docker_containers()
        for c in containers:
            self._docker_combo.addItem(f"{c.name} ({c.image[:30]})", userData=c)
        if not containers:
            self._docker_combo.addItem("(no containers found)")

    def _on_accept(self) -> None:
        from providers import (
            LocalFileConfig, SSHConfig, DockerConfig, K8sConfig, KVMConfig,
            LocalFileProvider, SSHProvider, DockerProvider, K8sProvider, KVMProvider,
        )
        idx = self._type_combo.currentIndex()
        try:
            if idx == 0:
                cfg = LocalFileConfig(
                    path=self._local_path.text().strip(),
                    tail_lines=self._local_tail.value(),
                )
                self._result_config = (LocalFileProvider(cfg), cfg.path)

            elif idx == 1:
                cfg = SSHConfig(
                    host=self._ssh_host.text().strip(),
                    port=self._ssh_port.value(),
                    username=self._ssh_user.text().strip(),
                    password=self._ssh_pass.text(),
                    key_path=self._ssh_key.text().strip(),
                    remote_path=self._ssh_path.text().strip(),
                    remote_os=self._ssh_os.currentText(),
                    tail_lines=self._ssh_tail.value(),
                )
                self._result_config = (SSHProvider(cfg), f"SSH:{cfg.host}:{cfg.remote_path}")

            elif idx == 2:
                manual = self._docker_name.text().strip()
                if manual:
                    cfg = DockerConfig(container_id=manual, container_name=manual, tail_lines=self._docker_tail.value())
                else:
                    container_info = self._docker_combo.currentData()
                    if container_info is None:
                        return
                    cfg = DockerConfig(
                        container_id=container_info.container_id,
                        container_name=container_info.name,
                        tail_lines=self._docker_tail.value(),
                    )
                self._result_config = (DockerProvider(cfg), f"Docker:{cfg.container_name or cfg.container_id}")

            elif idx == 3:
                cfg = K8sConfig(
                    namespace=self._k8s_ns.text().strip() or "default",
                    pod_name=self._k8s_pod.text().strip(),
                    container_name=self._k8s_cont.text().strip(),
                    kubeconfig=self._k8s_kubeconfig.text().strip(),
                    tail_lines=self._k8s_tail.value(),
                )
                self._result_config = (K8sProvider(cfg), f"K8s:{cfg.namespace}/{cfg.pod_name}")

            elif idx == 4:
                cfg = KVMConfig(
                    domain_name=self._kvm_domain.text().strip(),
                    log_path=self._kvm_logpath.text().strip(),
                )
                self._result_config = (KVMProvider(cfg), f"KVM:{cfg.domain_name}")

        except Exception as e:
            InfoBar.error("Config error", str(e), parent=self)
            return

        self.accept()

    @property
    def result_config(self) -> tuple[Any, str] | None:
        return self._result_config