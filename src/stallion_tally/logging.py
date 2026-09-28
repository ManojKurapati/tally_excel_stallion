"""Structured logging.

Loggers accept keyword context, e.g. ``log.info("Azure upload successful",
file="part-001.parquet")``. Console output renders ``LEVEL message key=value``
and file output renders JSON lines. Values for secret-looking keys are masked.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SECRET_KEY_RE = re.compile(
    r"(secret|password|passwd|token|connection_string|account_key|sas|credential|api_key|apikey)",
    re.IGNORECASE,
)
_RESERVED = {"exc_info", "stack_info", "stacklevel", "extra"}
_CONFIGURED = False


def _mask(key: str, value: Any) -> Any:
    if _SECRET_KEY_RE.search(key):
        return "***"
    return value


def _fmt_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value)
    if not text or re.search(r"[\s\"=]", text):
        return json.dumps(text, ensure_ascii=False)
    return text


class StructuredLogger(logging.LoggerAdapter):
    """LoggerAdapter that turns keyword arguments into structured context."""

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        ctx: dict[str, Any] = dict(self.extra or {})
        for key in list(kwargs):
            if key not in _RESERVED:
                ctx[key] = kwargs.pop(key)
        extra = dict(kwargs.get("extra") or {})
        extra["ctx"] = {k: _mask(k, v) for k, v in ctx.items()}
        kwargs["extra"] = extra
        return msg, kwargs

    def bind(self, **context: Any) -> StructuredLogger:
        return StructuredLogger(self.logger, {**(self.extra or {}), **context})


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = getattr(record, "ctx", {}) or {}
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
        parts = [ts, f"{record.levelname:<5}", record.getMessage()]
        if ctx:
            parts.append(" ".join(f"{k}={_fmt_value(v)}" for k, v in ctx.items()))
        line = " ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        ctx = getattr(record, "ctx", {}) or {}
        for key, value in ctx.items():
            payload[key] = (
                value if isinstance(value, (str, int, float, bool)) or value is None else str(value)
            )
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    level: str = "INFO",
    fmt: str = "console",
    log_dir: Path | None = None,
    log_to_file: bool = True,
) -> None:
    """Configure the root logger once. Safe to call repeatedly."""
    global _CONFIGURED
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    # A Windows service / scheduled task may have no console at all.
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
        root.addHandler(console)

    if log_to_file and log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "stallion_tally.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=10,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)

    # Third-party libraries are noisy at INFO.
    for noisy in ("azure", "httpx", "httpcore", "urllib3", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str, **context: Any) -> StructuredLogger:
    if not _CONFIGURED and not logging.getLogger().handlers:
        configure_logging(log_to_file=False)
    return StructuredLogger(logging.getLogger(name), context)
