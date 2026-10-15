"""Structured (JSON lines) logging for journald.

Usage::

    log = get_logger(__name__)
    log.info("session opened", extra=kv(peer="user:42:abc", viewers=1))
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {"message", "asctime"}


def kv(**fields: Any) -> dict[str, Any]:
    """Helper to build the ``extra`` mapping for structured fields."""
    return {"fields": fields}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": round(record.created, 3),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for k, v in fields.items():
                out.setdefault(k, v)
        for k, v in record.__dict__.items():
            if k not in _RESERVED and k != "fields" and not k.startswith("_"):
                out.setdefault(k, v)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = "%s %-5s %s: %s" % (
            time.strftime("%H:%M:%S", time.localtime(record.created)),
            record.levelname,
            record.name,
            record.getMessage(),
        )
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict) and fields:
            base += " " + " ".join(f"{k}={v!r}" for k, v in fields.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Quiet chatty libraries.
    for noisy in ("aioice", "aiortc", "asyncio", "PIL", "picamera2", "bless", "dbus_next"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
