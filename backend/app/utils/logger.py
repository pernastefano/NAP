"""
logger.py – Structured JSON logging + in-memory ring buffer for the /logs endpoint.
"""

from __future__ import annotations

import collections
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

_buffer_lock = threading.Lock()
_log_buffer: collections.deque[dict[str, Any]] = collections.deque(maxlen=500)


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc)


class _BufferHandler(logging.Handler):
    """Appends formatted log records to the in-memory ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            doc = {
                "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "msg": self.format(record),
            }
            with _buffer_lock:
                _log_buffer.append(doc)
        except Exception:  # noqa: BLE001
            self.handleError(record)


def configure_logging(level: str = "INFO", max_lines: int = 500) -> None:
    """Install structured JSON logging on the root logger.

    Call once from main.py on startup.
    """
    global _log_buffer
    with _buffer_lock:
        _log_buffer = collections.deque(maxlen=max_lines)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))

    # Console handler – JSON output for easy log aggregation
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, _BufferHandler)
               for h in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(_JSONFormatter())
        root.addHandler(console)

    # In-memory buffer handler – powers the /logs API endpoint
    if not any(isinstance(h, _BufferHandler) for h in root.handlers):
        buf_handler = _BufferHandler()
        buf_handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(buf_handler)


def get_recent_logs(n: int | None = None) -> list[dict[str, Any]]:
    """Return the most-recent *n* log entries (all entries if n is None)."""
    with _buffer_lock:
        entries = list(_log_buffer)
    if n is not None:
        entries = entries[-n:]
    return entries
