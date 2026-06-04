"""
Structured JSON logging for the Unified Social Media Tool.
Writes logs to both console and rotating log files in the logs/ directory.
Each log entry is a JSON object with timestamp, level, module, and message.
"""

import json
import logging
import logging.handlers
import os
from datetime import datetime, timezone

from backend.core.config import settings


class JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record):
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }

        # attach exception info when present
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)

        # pass through any extra fields the caller attached
        if hasattr(record, "extra_data"):
            entry["data"] = record.extra_data

        return json.dumps(entry, default=str)


class WindowsSafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """
    Subclass of RotatingFileHandler that handles PermissionError on Windows.
    This prevents tracebacks and logging crashes when the file is locked by
    other processes (e.g. background server / local script runs).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rollover_failed = False
        self._next_attempt_size = 0

    def shouldRollover(self, record):
        if self._rollover_failed:
            try:
                self.stream.seek(0, 2)
                if self.stream.tell() < self._next_attempt_size:
                    return 0
            except Exception:
                pass
        return super().shouldRollover(record)

    def doRollover(self):
        try:
            super().doRollover()
            self._rollover_failed = False
        except PermissionError:
            self._rollover_failed = True
            # Reopen the stream if it was closed by doRollover before raising
            if self.stream is None:
                try:
                    self.stream = self._open()
                except Exception:
                    pass
            try:
                if self.stream:
                    self.stream.seek(0, 2)
                    self._next_attempt_size = self.stream.tell() + 1024 * 1024
            except Exception:
                self._next_attempt_size = 0


def _setup_root_logger():
    """
    Configure the root logger once at import time.
    Sets up both a console handler and a rotating file handler.
    """
    os.makedirs(settings.LOG_PATH, exist_ok=True)

    root = logging.getLogger("unified_tool")
    root.setLevel(logging.DEBUG if settings.DEBUG else logging.INFO)

    # don't double-add handlers on reimport
    if root.handlers:
        return root

    # ---- Console handler (human readable for dev) ----
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console_fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)-24s  %(message)s",
        datefmt="%H:%M:%S",
    )
    console.setFormatter(console_fmt)
    root.addHandler(console)

    # ---- File handler (structured JSON for tooling) ----
    log_file = os.path.join(settings.LOG_PATH, "unified_tool.jsonl")
    file_handler = WindowsSafeRotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    return root


# Initialize on first import
_setup_root_logger()


def get_logger(name: str) -> logging.Logger:
    """
    Get a child logger under the unified_tool namespace.
    Usage: logger = get_logger("facebook.discovery")
    """
    return logging.getLogger(f"unified_tool.{name}")
