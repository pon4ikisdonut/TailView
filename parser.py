from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


class LogLevel(Enum):
    TRACE = auto()
    DEBUG = auto()
    INFO = auto()
    WARN = auto()
    ERROR = auto()
    CRITICAL = auto()
    UNKNOWN = auto()


_LEVEL_MAP: dict[str, LogLevel] = {
    "trace": LogLevel.TRACE,
    "debug": LogLevel.DEBUG,
    "info": LogLevel.INFO,
    "information": LogLevel.INFO,
    "warn": LogLevel.WARN,
    "warning": LogLevel.WARN,
    "error": LogLevel.ERROR,
    "err": LogLevel.ERROR,
    "critical": LogLevel.CRITICAL,
    "fatal": LogLevel.CRITICAL,
    "crit": LogLevel.CRITICAL,
}

LEVEL_COLORS: dict[LogLevel, str] = {
    LogLevel.TRACE:    "#888888",
    LogLevel.DEBUG:    "#8888ff",
    LogLevel.INFO:     "#44cc88",
    LogLevel.WARN:     "#ffcc00",
    LogLevel.ERROR:    "#ff4444",
    LogLevel.CRITICAL: "#ff0044",
    LogLevel.UNKNOWN:  "#cccccc",
}

_PLAIN_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?"
    r"\s*"
    r"(?:\[?(?P<level>TRACE|DEBUG|INFO|INFORMATION|WARN(?:ING)?|ERROR|ERR|CRITICAL|FATAL|CRIT)\]?)?"
    r"\s*"
    r"(?:\[(?P<service>[^\]]{1,32})\])?"
    r"\s*"
    r"(?P<message>.*)",
    re.IGNORECASE,
)

_TRACEBACK_START = re.compile(
    r"^(?:Traceback \(most recent call last\)|"
    r"Exception in thread|"
    r"\s+at\s+[\w\.$<>]+\(|"
    r"^\s+File \")"
)
_TRACEBACK_CONTINUATION = re.compile(r"^\s+(?:at |File |in )")


@dataclass
class LogEntry:
    raw: str
    level: LogLevel = LogLevel.UNKNOWN
    timestamp: str = ""
    service: str = ""
    message: str = ""
    is_json: bool = False
    json_data: dict[str, Any] = field(default_factory=dict)
    is_stacktrace_head: bool = False
    is_stacktrace_body: bool = False
    stacktrace_lines: list[str] = field(default_factory=list)
    source_id: str = ""


def _detect_level(raw_level: str | None, data: dict[str, Any] | None = None) -> LogLevel:
    if raw_level:
        return _LEVEL_MAP.get(raw_level.lower(), LogLevel.UNKNOWN)
    if data:
        for key in ("level", "severity", "lvl", "loglevel"):
            val = data.get(key)
            if isinstance(val, str):
                lvl = _LEVEL_MAP.get(val.lower())
                if lvl:
                    return lvl
    return LogLevel.UNKNOWN


def _extract_json_fields(data: dict[str, Any]) -> tuple[str, str, str]:
    ts = ""
    for key in ("timestamp", "time", "ts", "@timestamp", "date"):
        val = data.get(key)
        if isinstance(val, (str, int, float)):
            ts = str(val)
            break

    svc = ""
    for key in ("service", "logger", "module", "component", "app"):
        val = data.get(key)
        if isinstance(val, str):
            svc = val
            break

    msg = ""
    for key in ("message", "msg", "text", "body", "log"):
        val = data.get(key)
        if isinstance(val, str):
            msg = val
            break
    if not msg:
        msg = json.dumps(data, ensure_ascii=False)

    return ts, svc, msg


class StackTraceCollector:
    def __init__(self) -> None:
        self._collecting = False
        self._head_entry: LogEntry | None = None
        self._lines: list[str] = []

    def feed(self, entry: LogEntry) -> list[LogEntry]:
        line = entry.raw
        if self._collecting:
            is_continuation = bool(_TRACEBACK_CONTINUATION.match(line)) or line.strip() == ""
            if is_continuation and line.strip():
                self._lines.append(line.rstrip())
                return []
            else:
                assert self._head_entry is not None
                self._head_entry.stacktrace_lines = list(self._lines)
                finished = self._head_entry
                self._collecting = False
                self._head_entry = None
                self._lines = []
                if _TRACEBACK_START.match(line):
                    self._collecting = True
                    entry.is_stacktrace_head = True
                    self._head_entry = entry
                    self._lines = [line.rstrip()]
                    return [finished]
                return [finished, entry]
        else:
            if _TRACEBACK_START.match(line):
                self._collecting = True
                entry.is_stacktrace_head = True
                self._head_entry = entry
                self._lines = [line.rstrip()]
                return []
            return [entry]

    def flush(self) -> list[LogEntry]:
        if self._collecting and self._head_entry:
            self._head_entry.stacktrace_lines = list(self._lines)
            finished = self._head_entry
            self._collecting = False
            self._head_entry = None
            self._lines = []
            return [finished]
        return []


class LogParser:
    @staticmethod
    def parse(raw: str, source_id: str = "") -> LogEntry:
        stripped = raw.strip()
        if not stripped:
            return LogEntry(raw=raw, source_id=source_id)

        if stripped.startswith("{"):
            try:
                data = json.loads(stripped)
                if isinstance(data, dict):
                    level = _detect_level(None, data)
                    ts, svc, msg = _extract_json_fields(data)
                    return LogEntry(
                        raw=raw,
                        level=level,
                        timestamp=ts,
                        service=svc,
                        message=msg,
                        is_json=True,
                        json_data=data,
                        source_id=source_id,
                    )
            except json.JSONDecodeError:
                pass

        m = _PLAIN_RE.match(stripped)
        if m:
            ts = m.group("ts") or ""
            raw_level = m.group("level")
            svc = m.group("service") or ""
            msg = m.group("message") or stripped
            level = _detect_level(raw_level)
            return LogEntry(
                raw=raw,
                level=level,
                timestamp=ts,
                service=svc,
                message=msg,
                source_id=source_id,
            )

        return LogEntry(raw=raw, message=stripped, source_id=source_id)