"""Logging with rotation, secret redaction and an in-memory ring buffer.

Two problems this solves:

* the old project printed everything to a terminal that vanished when the GUI
  was launched from a desktop launcher - so failures were invisible;
* logs are easy to leak API keys through.

All handlers get :class:`RedactionFilter`, and the ring buffer lets the GUI /
web API show recent activity without re-reading the log file.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- #
# redaction
# --------------------------------------------------------------------------- #
_SECRET_PATTERNS = (
    re.compile(r"(sk-[A-Za-z0-9_\-]{8,})"),
    re.compile(r"(AIza[0-9A-Za-z_\-]{10,})"),
    re.compile(r"(gsk_[A-Za-z0-9]{8,})"),
    re.compile(r"(tvly-[A-Za-z0-9]{8,})"),
    re.compile(r"((?:Bearer|Basic)\s+)[A-Za-z0-9._\-+/=]{8,}", re.IGNORECASE),
    re.compile(
        r"((?:[A-Za-z0-9_]*(?:KEY|TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIAL)[A-Za-z0-9_]*)"
        r"\s*[=:]\s*)([^\s,;\"']+)",
        re.IGNORECASE,
    ),
)

MASK = "***"


def redact(text: Any) -> str:
    """Mask anything that looks like a credential.  Never raises."""
    try:
        result = str(text)
    except Exception:  # pragma: no cover - defensive
        return "<unprintable>"
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            result = pattern.sub(lambda m: f"{m.group(1)}{MASK}", result)
        else:
            result = pattern.sub(MASK, result)
    return result


class RedactionFilter(logging.Filter):
    """Scrub credentials out of every record before it reaches a handler."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            message = record.getMessage()
            cleaned = redact(message)
            if cleaned != message:
                record.msg = cleaned
                record.args = ()
        except Exception:  # pragma: no cover - defensive
            pass
        return True


# --------------------------------------------------------------------------- #
# ring buffer
# --------------------------------------------------------------------------- #
class RingBufferHandler(logging.Handler):
    """Keeps the last N records in memory so the GUI can display them."""

    def __init__(self, capacity: int = 400, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self._lock = threading.RLock()
        self._capacity = max(20, int(capacity))
        self._records: List[Dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": record.created,
                "time": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "message": redact(record.getMessage()),
            }
        except Exception:  # pragma: no cover - defensive
            return
        with self._lock:
            self._records.append(entry)
            if len(self._records) > self._capacity:
                del self._records[: len(self._records) - self._capacity]

    def snapshot(self, limit: int = 100, level: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            records = list(self._records)
        if level:
            wanted = level.upper()
            records = [item for item in records if item["level"] == wanted]
        return records[-max(1, int(limit)) :]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


_LOGGER_NAME = "jarvis"
_CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"


def setup_logging(
    logs_dir: Path,
    level: str = "INFO",
    capacity: int = 400,
    console: bool = True,
) -> RingBufferHandler:
    """Configure the ``jarvis`` logger tree.  Safe to call more than once."""
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False

    existing = getattr(logger, "_jarvis_ring", None)
    if existing is not None:
        return existing

    formatter_console = logging.Formatter(_CONSOLE_FORMAT)
    formatter_file = logging.Formatter(_FILE_FORMAT)
    redaction = RedactionFilter()

    ring = RingBufferHandler(capacity=capacity)
    ring.setFormatter(formatter_console)
    ring.addFilter(redaction)
    logger.addHandler(ring)

    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            filename=str(logs_dir / "jarvis.log"),
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter_file)
        file_handler.addFilter(redaction)
        logger.addHandler(file_handler)
    except OSError:  # read-only filesystem: keep going with the ring buffer
        pass

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter_console)
        stream.addFilter(redaction)
        logger.addHandler(stream)

    logger._jarvis_ring = ring  # type: ignore[attr-defined]
    return ring


def get_logger(name: str = "") -> logging.Logger:
    """Return a child of the ``jarvis`` logger."""
    if not name:
        return logging.getLogger(_LOGGER_NAME)
    if name.startswith(_LOGGER_NAME):
        return logging.getLogger(name)
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")


def ring_handler() -> Optional[RingBufferHandler]:
    return getattr(logging.getLogger(_LOGGER_NAME), "_jarvis_ring", None)


__all__ = [
    "MASK",
    "RedactionFilter",
    "RingBufferHandler",
    "get_logger",
    "redact",
    "ring_handler",
    "setup_logging",
]
