from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from collections import deque
from typing import Any

from PySide6.QtCore import (
    Qt, QAbstractListModel, QModelIndex, QTimer, Signal, QThread, QObject,
    QSize, QPoint,
)
from PySide6.QtGui import QColor, QPainter, QFont, QFontMetrics, QAction
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListView, QStyledItemDelegate,
    QAbstractItemView, QLabel, QFrame, QStyle, QApplication,
    QDialog, QFormLayout, QLineEdit, QSpinBox, QDialogButtonBox,
    QStackedWidget, QMenu, QTreeWidget, QTreeWidgetItem,
)
from qfluentwidgets import (
    FluentIcon, TabBar, PrimaryPushButton, ToolButton,
    SearchLineEdit, ComboBox, CheckBox, BodyLabel, StrongBodyLabel,
    isDarkTheme, TransparentPushButton, InfoBar, InfoBarPosition,
    LineEdit, ScrollArea, Theme, setTheme,
)

from parser import LogEntry, LogLevel, LEVEL_COLORS, LogParser, StackTraceCollector

LOG_ENTRY_ROLE = Qt.ItemDataRole.UserRole + 1
EXPANDED_ROLE  = Qt.ItemDataRole.UserRole + 2

_DARK_BG       = "#1e1e2e"
_DARK_BG_ALT   = "#181824"
_DARK_SEL      = "#2d2d50"
_LIGHT_BG      = "#f8f8f8"
_LIGHT_SEL     = "#dde4f0"


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
        entries_list = list(self._entries)
        if real_idx >= len(entries_list):
            return None
        entry = entries_list[real_idx]
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

        has_error = any(e.level in (LogLevel.ERROR, LogLevel.CRITICAL) for e in new_entries)
        if has_error:
            self._has_error = True
            self.error_appeared.emit()

        current_len = len(self._entries)
        overflow = max(0, current_len + len(new_entries) - MAX_ENTRIES)
        self._entries.extend(new_entries)

        if overflow > 0 or self._filter_level or self._filter_text:
            self._rebuild_visible()
        else:
            old_vis_count = len(self._visible)
            for i, entry in enumerate(new_entries):
                if self._passes_filter(entry):
                    self._visible.append(current_len + i)
            added = len(self._visible) - old_vis_count
            if added > 0:
                self.beginInsertRows(QModelIndex(), old_vis_count, old_vis_count + added - 1)
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
        self._visible = [i for i, e in enumerate(entries_list) if self._passes_filter(e)]
        self.endResetModel()

    def get_entry(self, vis_row: int) -> LogEntry | None:
        if vis_row < 0 or vis_row >= len(self._visible):
            return None
        real_idx = self._visible[vis_row]
        entries_list = list(self._entries)
        if real_idx >= len(entries_list):
            return None
        return entries_list[real_idx]

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
    PADDING_V   = 5
    PADDING_H   = 10
    INDICATOR_W = 3
    LINE_H      = 22
    LEVEL_COL_W = 90
    TS_COL_W    = 190
    SVC_MAX_W   = 120

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._font = QFont("Fira Code, Consolas, Courier New", 10)
        self._font_small = QFont("Fira Code, Consolas, Courier New", 9)
        self._fm = QFontMetrics(self._font)
        self._fm_small = QFontMetrics(self._font_small)

    def sizeHint(self, option: Any, index: QModelIndex) -> QSize:
        entry: LogEntry | None = index.data(LOG_ENTRY_ROLE)
        expanded: bool = index.data(EXPANDED_ROLE) or False
        base_h = self.LINE_H + self.PADDING_V * 2
        if expanded and entry and (entry.stacktrace_lines or entry.json_data):
            extra = len(entry.stacktrace_lines) if entry.stacktrace_lines else len(entry.json_data)
            return QSize(option.rect.width(), base_h + min(extra, 20) * self.LINE_H + self.PADDING_V)
        return QSize(option.rect.width(), base_h)

    def paint(self, painter: QPainter, option: Any, index: QModelIndex) -> None:
        entry: LogEntry | None = index.data(LOG_ENTRY_ROLE)
        expanded: bool = index.data(EXPANDED_ROLE) or False

        painter.save()
        rect = option.rect
        dark = isDarkTheme()

        bg = QColor(_DARK_BG if dark else _LIGHT_BG)
        if option.state & QStyle.StateFlag.State_Selected:
            bg = QColor(_DARK_SEL if dark else _LIGHT_SEL)
        painter.fillRect(rect, bg)

        if not entry:
            painter.restore()
            return

        level_color = QColor(LEVEL_COLORS.get(entry.level, "#cccccc"))
        painter.fillRect(rect.x(), rect.y(), self.INDICATOR_W, rect.height(), level_color)

        painter.setFont(self._font)
        text_y = rect.y() + self.PADDING_V + self._fm.ascent()

        x = rect.x() + self.INDICATOR_W + self.PADDING_H

        if entry.timestamp:
            painter.setPen(QColor("#5a5a7a" if dark else "#888888"))
            ts = entry.timestamp[:23]
            painter.drawText(x, text_y, ts)
            x += self.TS_COL_W

        lvl_text = entry.level.name if entry.level != LogLevel.UNKNOWN else "???"
        lvl_badge_w = self._fm.horizontalAdvance(f" {lvl_text} ") + 6
        lvl_rect_top = rect.y() + (rect.height() - self.LINE_H + 4) // 2
        badge_rect_x = x
        painter.setPen(Qt.PenStyle.NoPen)
        badge_bg = QColor(level_color)
        badge_bg.setAlpha(45)
        painter.setBrush(badge_bg)
        painter.drawRoundedRect(badge_rect_x, lvl_rect_top, lvl_badge_w, self.LINE_H - 4, 3, 3)
        painter.setPen(level_color)
        painter.drawText(badge_rect_x + 4, text_y, lvl_text)
        x += lvl_badge_w + self.PADDING_H

        if entry.service:
            painter.setPen(QColor("#9d78e6" if dark else "#6644aa"))
            svc = self._fm.elidedText(entry.service, Qt.TextElideMode.ElideRight, self.SVC_MAX_W)
            svc_w = self._fm.horizontalAdvance(svc)
            painter.drawText(x, text_y, svc + " ")
            x += svc_w + self._fm.horizontalAdvance(" ")

        has_expand = bool(entry.stacktrace_lines or (entry.is_json and entry.json_data))
        right_margin = rect.right() - self.PADDING_H
        if has_expand:
            arrow = "▼" if expanded else "▶"
            painter.setPen(QColor("#666688" if dark else "#999999"))
            painter.drawText(right_margin - self._fm.horizontalAdvance(arrow), text_y, arrow)
            right_margin -= self._fm.horizontalAdvance(arrow) + 4

        painter.setPen(QColor("#dcdcef" if dark else "#1a1a1a"))
        msg = entry.message or entry.raw
        avail = right_margin - x - 4
        elided = self._fm.elidedText(msg, Qt.TextElideMode.ElideRight, max(avail, 40))
        painter.drawText(x, text_y, elided)

        if expanded:
            painter.setFont(self._font_small)
            sub_y = rect.y() + self.LINE_H + self.PADDING_V * 2
            indent = rect.x() + self.INDICATOR_W + self.PADDING_H * 3

            if entry.stacktrace_lines:
                for line in entry.stacktrace_lines[:20]:
                    painter.setPen(QColor("#ff8888" if dark else "#cc2222"))
                    painter.drawText(indent, sub_y + self._fm_small.ascent(), line)
                    sub_y += self.LINE_H

            elif entry.is_json and entry.json_data:
                fm = self._fm_small
                for key, val in list(entry.json_data.items())[:15]:
                    painter.setPen(QColor("#aa88ff" if dark else "#6633cc"))
                    key_str = f"{key}: "
                    painter.drawText(indent, sub_y + fm.ascent(), key_str)
                    painter.setPen(QColor("#88ddaa" if dark else "#226644"))
                    painter.drawText(indent + fm.horizontalAdvance(key_str), sub_y + fm.ascent(), str(val)[:120])
                    sub_y += self.LINE_H

        painter.restore()


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
        self._flush_timer.setInterval(80)
        self._flush_timer.timeout.connect(self._flush_pending)
        self._flush_timer.start()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        toolbar = QWidget()
        toolbar.setFixedHeight(44)
        dark = isDarkTheme()
        toolbar.setStyleSheet(f"background: {'#16162a' if dark else '#ebebf5'}; border-bottom: 1px solid {'#2a2a45' if dark else '#ccccdd'};")
        tb = QHBoxLayout(toolbar)
        tb.setContentsMargins(8, 4, 8, 4)
        tb.setSpacing(6)

        self._search = SearchLineEdit()
        self._search.setPlaceholderText("Filter…")
        self._search.setMaximumWidth(260)
        self._search.textChanged.connect(self._on_filter_changed)

        self._level_combo = ComboBox()
        self._level_combo.addItems(["All", "TRACE", "DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"])
        self._level_combo.setMaximumWidth(120)
        self._level_combo.currentTextChanged.connect(self._on_level_changed)

        self._auto_scroll_cb = CheckBox("Auto-scroll")
        self._auto_scroll_cb.setChecked(True)
        self._auto_scroll_cb.checkStateChanged.connect(
            lambda s: setattr(self, "_auto_scroll", s == Qt.CheckState.Checked)
        )

        self._clear_btn = TransparentPushButton(FluentIcon.DELETE, "Clear")
        self._clear_btn.clicked.connect(self._model.clear)

        self._status_label = BodyLabel("0 lines")
        self._status_label.setStyleSheet("color: #666688; font-size: 11px;")

        tb.addWidget(self._search)
        tb.addWidget(self._level_combo)
        tb.addWidget(self._auto_scroll_cb)
        tb.addStretch()
        tb.addWidget(self._status_label)
        tb.addWidget(self._clear_btn)
        layout.addWidget(toolbar)

        self._list_view = QListView()
        self._list_view.setModel(self._model)
        self._list_view.setItemDelegate(self._delegate)
        self._list_view.setUniformItemSizes(False)
        self._list_view.setResizeMode(QListView.ResizeMode.Adjust)
        self._list_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list_view.customContextMenuRequested.connect(self._show_context_menu)
        self._list_view.clicked.connect(self._on_item_clicked)

        dark = isDarkTheme()
        self._list_view.setStyleSheet(f"""
            QListView {{
                background: {_DARK_BG if dark else _LIGHT_BG};
                border: none;
                outline: none;
            }}
            QListView::item {{ border: none; }}
            QScrollBar:vertical {{
                width: 5px;
                background: transparent;
                margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {'#3a3a5a' if dark else '#bbbbdd'};
                border-radius: 2px;
                min-height: 20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """)
        layout.addWidget(self._list_view, 1)

    def _show_context_menu(self, pos: QPoint) -> None:
        index = self._list_view.indexAt(pos)
        menu = QMenu(self)
        dark = isDarkTheme()
        menu.setStyleSheet(f"""
            QMenu {{
                background: {'#1e1e30' if dark else '#f5f5ff'};
                color: {'#e0e0f0' if dark else '#1a1a2a'};
                border: 1px solid {'#3a3a55' if dark else '#ccccdd'};
                border-radius: 8px;
                padding: 4px;
                font-size: 13px;
            }}
            QMenu::item {{
                padding: 6px 20px 6px 12px;
                border-radius: 4px;
            }}
            QMenu::item:selected {{
                background: {'#2d2d50' if dark else '#dde4f5'};
            }}
            QMenu::separator {{
                height: 1px;
                background: {'#2a2a45' if dark else '#ccccdd'};
                margin: 3px 8px;
            }}
        """)

        if index.isValid():
            entry: LogEntry | None = index.data(LOG_ENTRY_ROLE)

            copy_line = QAction("Copy line", self)
            copy_line.triggered.connect(lambda: self._copy_text(entry.raw if entry else ""))
            menu.addAction(copy_line)

            if entry and entry.message:
                copy_msg = QAction("Copy message", self)
                copy_msg.triggered.connect(lambda: self._copy_text(entry.message))
                menu.addAction(copy_msg)

            if entry and (entry.stacktrace_lines or (entry.is_json and entry.json_data)):
                has_expand = index.data(EXPANDED_ROLE) or False
                toggle_act = QAction("Collapse" if has_expand else "Expand", self)
                toggle_act.triggered.connect(lambda: self._model.toggle_expand(index.row()))
                menu.addAction(toggle_act)

            if entry and entry.is_json and entry.json_data:
                copy_json = QAction("Copy JSON (pretty)", self)
                copy_json.triggered.connect(
                    lambda: self._copy_text(json.dumps(entry.json_data, indent=2, ensure_ascii=False))
                )
                menu.addAction(copy_json)

            menu.addSeparator()

        copy_all = QAction("Copy all visible lines", self)
        copy_all.triggered.connect(self._copy_all_visible)
        menu.addAction(copy_all)

        filter_errors = QAction("Show only ERRORs", self)
        filter_errors.triggered.connect(lambda: self._quick_filter_level(LogLevel.ERROR))
        menu.addAction(filter_errors)

        clear_filter = QAction("Clear filter", self)
        clear_filter.triggered.connect(self._clear_filter)
        menu.addAction(clear_filter)

        menu.addSeparator()

        scroll_top = QAction("Scroll to top", self)
        scroll_top.triggered.connect(lambda: self._list_view.scrollToTop())
        menu.addAction(scroll_top)

        scroll_bottom = QAction("Scroll to bottom", self)
        scroll_bottom.triggered.connect(lambda: self._list_view.scrollToBottom())
        menu.addAction(scroll_bottom)

        clear_act = QAction("Clear log", self)
        clear_act.triggered.connect(self._model.clear)
        menu.addAction(clear_act)

        menu.exec(self._list_view.viewport().mapToGlobal(pos))

    def _copy_text(self, text: str) -> None:
        QApplication.clipboard().setText(text)

    def _copy_all_visible(self) -> None:
        lines = []
        for i in range(self._model.rowCount()):
            entry = self._model.get_entry(i)
            if entry:
                lines.append(entry.raw)
        QApplication.clipboard().setText("\n".join(lines))

    def _quick_filter_level(self, level: LogLevel) -> None:
        mapping = {
            LogLevel.TRACE: 1, LogLevel.DEBUG: 2, LogLevel.INFO: 3,
            LogLevel.WARN: 4, LogLevel.ERROR: 5, LogLevel.CRITICAL: 6,
        }
        idx = mapping.get(level, 0)
        self._level_combo.setCurrentIndex(idx)

    def _clear_filter(self) -> None:
        self._search.clear()
        self._level_combo.setCurrentIndex(0)

    def _on_item_clicked(self, index: QModelIndex) -> None:
        entry = index.data(LOG_ENTRY_ROLE)
        if entry and (entry.stacktrace_lines or (entry.is_json and entry.json_data)):
            self._model.toggle_expand(index.row())
            self._list_view.update(index)

    def _on_filter_changed(self, text: str) -> None:
        self._model.set_filter(text, self._parse_level_from_combo())
        self._update_status()

    def _on_level_changed(self, _: str) -> None:
        self._model.set_filter(self._search.text(), self._parse_level_from_combo())
        self._update_status()

    def _parse_level_from_combo(self) -> LogLevel | None:
        mapping = {
            "TRACE": LogLevel.TRACE, "DEBUG": LogLevel.DEBUG, "INFO": LogLevel.INFO,
            "WARN": LogLevel.WARN, "ERROR": LogLevel.ERROR, "CRITICAL": LogLevel.CRITICAL,
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
        self._thread.error_occurred.connect(lambda e: self.feed_line(f"[TailView ERROR] {e}"))
        self._thread.start()

    def stop(self) -> None:
        if self._thread:
            self._thread.stop_provider()
        self._flush_timer.stop()

    @property
    def model(self) -> LogModel:
        return self._model


class DashboardView(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)

        title = StrongBodyLabel("Dashboard — Live Event Timeline")
        layout.addWidget(title)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #2a2a45;")
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
                background: {_DARK_BG_ALT if dark else '#f0f0f8'};
                border: none;
            }}
            QScrollBar:vertical {{ width: 5px; background: transparent; }}
            QScrollBar::handle:vertical {{
                background: {'#3a3a5a' if dark else '#bbbbdd'};
                border-radius: 2px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """)
        layout.addWidget(self._list_view, 1)

    def push_line(self, line: str, source_id: str) -> None:
        self._model.append_lines([line], source_id)
        self._list_view.scrollToBottom()


class AddSourceDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Log Source")
        self.setMinimumWidth(460)
        self._result_config: Any = None
        dark = isDarkTheme()
        self.setStyleSheet(f"""
            QDialog {{
                background: {'#1a1a2e' if dark else '#f5f5ff'};
                color: {'#e0e0f0' if dark else '#1a1a2a'};
            }}
            QLabel {{
                color: {'#c0c0e0' if dark else '#2a2a3a'};
                font-size: 13px;
            }}
            QLineEdit, QSpinBox {{
                background: {'#252540' if dark else '#ffffff'};
                color: {'#e0e0f0' if dark else '#1a1a2a'};
                border: 1px solid {'#3a3a5a' if dark else '#ccccdd'};
                border-radius: 6px;
                padding: 5px 8px;
                font-size: 13px;
            }}
            QLineEdit:focus, QSpinBox:focus {{
                border: 1px solid {'#7c5cbf' if dark else '#7755cc'};
            }}
            QDialogButtonBox QPushButton {{
                background: {'#2d2d50' if dark else '#e0e0f5'};
                color: {'#e0e0f0' if dark else '#1a1a2a'};
                border: 1px solid {'#3a3a5a' if dark else '#bbbbdd'};
                border-radius: 6px;
                padding: 6px 18px;
                font-size: 13px;
                min-width: 80px;
            }}
            QDialogButtonBox QPushButton:hover {{
                background: {'#3d3d70' if dark else '#d0d0f0'};
            }}
        """)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(20, 20, 20, 20)

        type_row = QHBoxLayout()
        type_label = BodyLabel("Source type:")
        self._type_combo = ComboBox()
        self._type_combo.addItems(["Local File", "SSH", "Docker", "Kubernetes", "KVM"])
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        type_row.addWidget(type_label)
        type_row.addWidget(self._type_combo, 1)
        layout.addLayout(type_row)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep)

        self._stack = QStackedWidget()

        local_w = QWidget()
        lf = QFormLayout(local_w)
        lf.setSpacing(10)
        self._local_path = LineEdit()
        self._local_path.setPlaceholderText("/var/log/nginx/access.log")
        self._local_tail = QSpinBox()
        self._local_tail.setRange(1, 10000)
        self._local_tail.setValue(200)
        self._local_sudo = CheckBox("Use sudo (for permission-denied files)")
        lf.addRow("Path:", self._local_path)
        lf.addRow("Tail lines:", self._local_tail)
        lf.addRow("", self._local_sudo)
        self._stack.addWidget(local_w)

        ssh_w = QWidget()
        sf = QFormLayout(ssh_w)
        sf.setSpacing(10)
        self._ssh_host = LineEdit(); self._ssh_host.setPlaceholderText("192.168.1.1")
        self._ssh_port = QSpinBox(); self._ssh_port.setRange(1, 65535); self._ssh_port.setValue(22)
        self._ssh_user = LineEdit(); self._ssh_user.setPlaceholderText("root")
        self._ssh_pass = LineEdit(); self._ssh_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self._ssh_pass.setPlaceholderText("password")
        self._ssh_key  = LineEdit(); self._ssh_key.setPlaceholderText("~/.ssh/id_rsa (or leave empty)")
        self._ssh_path = LineEdit(); self._ssh_path.setPlaceholderText("/var/log/app.log")
        self._ssh_os   = ComboBox(); self._ssh_os.addItems(["linux", "windows"])
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
        df.setSpacing(10)
        self._docker_combo = ComboBox()
        self._docker_name  = LineEdit(); self._docker_name.setPlaceholderText("or type container name/id manually")
        self._docker_tail  = QSpinBox(); self._docker_tail.setRange(1, 10000); self._docker_tail.setValue(200)
        self._refresh_docker_btn = TransparentPushButton(FluentIcon.SYNC, "Refresh")
        self._refresh_docker_btn.clicked.connect(self._refresh_docker)
        df.addRow("Container:", self._docker_combo)
        df.addRow("", self._refresh_docker_btn)
        df.addRow("Manual ID/Name:", self._docker_name)
        df.addRow("Tail lines:", self._docker_tail)
        self._stack.addWidget(docker_w)
        self._refresh_docker()

        k8s_w = QWidget()
        kf = QFormLayout(k8s_w)
        kf.setSpacing(10)
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
        kvf.setSpacing(10)
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
            self._docker_combo.addItem(f"{c.name}  ({c.image[:28]})", userData=c)
        if not containers:
            self._docker_combo.addItem("(no running containers)")

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
                    use_sudo=self._local_sudo.isChecked(),
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