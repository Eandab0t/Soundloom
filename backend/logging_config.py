"""Structured logging configuration.

Provides JSON-formatted log output with structured context fields,
performance timing helpers, and log rotation.
"""
import json
import logging
import logging.handlers
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

_request_id: ContextVar[str] = ContextVar("request_id", default="")


class JsonFormatter(logging.Formatter):
    """Outputs log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if _request_id.get():
            log_obj["request_id"] = _request_id.get()

        if record.exc_info and record.exc_info[0]:
            log_obj["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
            }

        for key in ("job_id", "error_type", "error_code", "operation",
                     "file_path", "source_url", "attempt"):
            val = getattr(record, key, None)
            if val is not None:
                log_obj[key] = val

        return json.dumps(log_obj, default=str)


class HumanFormatter(logging.Formatter):
    """Compact human-readable format for console output."""

    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[35m",
    }
    RESET = "\033[0m"

    def __init__(self, use_color: bool = True):
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now().strftime("%H:%M:%S")
        level = record.levelname
        if self.use_color and sys.stderr.isatty():
            level = f"{self.COLORS.get(level, '')}{level}{self.RESET}"
        msg = record.getMessage()
        name = record.name.split(".")[-1]

        parts = [ts, level, f"[{name}]", msg]

        if _request_id.get():
            parts.append(f"rid={_request_id.get()[:8]}")

        if record.exc_info and record.exc_info[0]:
            parts.append(f"\n{self.formatException(record.exc_info)}")

        return " ".join(parts)


def set_request_id(rid: str):
    _request_id.set(rid)


def get_request_id() -> str:
    return _request_id.get()


def setup_logging(log_dir: Path, json_output: bool = False, level: str = "INFO"):
    """Configure application logging with file rotation and optional JSON output."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    for handler in root.handlers[:]:
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stderr)
    if json_output:
        console.setFormatter(JsonFormatter())
    else:
        console.setFormatter(HumanFormatter(use_color=True))
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        str(log_dir / "app.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    error_handler = logging.handlers.RotatingFileHandler(
        str(log_dir / "error.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(JsonFormatter())
    root.addHandler(error_handler)
