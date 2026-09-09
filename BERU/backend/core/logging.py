"""Structured, centralised logging for BERU.

Provides:
- a per-request correlation id (``request_id``) via a :class:`ContextVar`, so
  log lines from the same request can be traced;
- ``text`` (human-readable) and ``json`` (structured) formats, selectable via
  configuration;
- a single :func:`configure_logging` entry point and a :func:`get_logger`
  helper used throughout the codebase.

Sensitive values (API keys, tokens) must never be passed to the logger.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar

# Correlation id for the current request/task. Defaults to "-" outside a request.
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

_CONFIGURED = False


class RequestIdFilter(logging.Filter):
    """Inject the current ``request_id`` onto every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_TEXT_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | [%(request_id)s] | %(message)s"


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    """Configure the root logger. Idempotent across repeated calls.

    Args:
        level: Logging level name (e.g. ``"INFO"``).
        fmt: ``"text"`` for human-readable output or ``"json"`` for structured.
    """
    global _CONFIGURED

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())
    if fmt.lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))

    root = logging.getLogger()
    root.setLevel(level.upper())
    # Replace existing handlers so re-configuration (e.g. in tests) is clean.
    root.handlers = [handler]

    # Tame noisy third-party loggers.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger, configuring logging on first use."""
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(name)
