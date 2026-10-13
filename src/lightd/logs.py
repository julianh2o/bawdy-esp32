"""lightd's log: the usual stderr output, plus the most recent records kept in memory for GET /api/logs."""

from __future__ import annotations
import itertools
import logging
import threading
import time
from collections import deque
from typing import Any

MAX_RECORDS = 2000
FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class LogBuffer(logging.Handler):
    """Keeps the last ``capacity`` records as dicts, each with a rising ``seq`` so clients can ask for what's new."""

    def __init__(self, capacity: int = MAX_RECORDS) -> None:
        super().__init__()
        self._records: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._seq = itertools.count(1)
        self._buffer_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "seq": 0,
                "time": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            if record.exc_info:
                entry["exception"] = logging.Formatter().formatException(record.exc_info)
        except Exception:
            self.handleError(record)
            return
        with self._buffer_lock:
            entry["seq"] = next(self._seq)
            self._records.append(entry)

    def records(self, since: int = 0, level: int = logging.NOTSET, limit: int | None = None) -> list[dict[str, Any]]:
        """Records newer than ``since`` at ``level`` or above, oldest first; the newest ``limit`` of them."""
        with self._buffer_lock:
            out = [r for r in self._records if r["seq"] > since and logging.getLevelName(r["level"]) >= level]
        return out[-limit:] if limit else out

    @property
    def last_seq(self) -> int:
        with self._buffer_lock:
            return self._records[-1]["seq"] if self._records else 0


buffer = LogBuffer()


def setup(verbose: bool = False) -> LogBuffer:
    """Log to stderr and to ``buffer``. The buffer keeps DEBUG records only when ``verbose``."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format=FORMAT)
    buffer.setLevel(level)
    root = logging.getLogger()
    if buffer not in root.handlers:
        root.addHandler(buffer)
    logging.getLogger("zeroconf").setLevel(logging.WARNING)
    return buffer


def format_text(record: dict[str, Any]) -> str:
    """One record as a log line (plus its traceback, if any)."""
    t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record["time"]))
    line = f"{t} {record['level']:<7} {record['logger']}: {record['message']}"
    return line + ("\n" + record["exception"].rstrip() if record.get("exception") else "")


def parse_level(value: str | None) -> int:
    """``"warning"`` / ``"WARNING"`` / ``"30"`` -> 30. Raises ``ValueError``."""
    if not value:
        return logging.NOTSET
    if value.isdigit():
        return int(value)
    level = logging.getLevelName(value.upper())
    if not isinstance(level, int):
        raise ValueError(f"Unknown log level '{value}'")
    return level
