"""Tiny atomic JSON store.

ChatLog.json in the old project was written with a plain ``open(...,'w')`` and
``json.dump``.  A power cut or a crash mid-write left a truncated file that then
crashed the app on the next start - on a Raspberry Pi that happens more often
than you would think.

This writes to a temporary file, flushes it to disk, then ``os.replace``s it, so
readers only ever see a complete document.  A corrupt file is quarantined rather
than losing the process.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from core.logging_setup import get_logger

log = get_logger("storage")


class JsonFile:
    """Thread-safe, atomic JSON document on disk."""

    def __init__(self, path: Path, default_factory: Callable[[], Any] = dict) -> None:
        self.path = Path(path)
        self._default_factory = default_factory
        self._lock = threading.RLock()

    # -- helpers -----------------------------------------------------------
    def _default(self) -> Any:
        try:
            return self._default_factory()
        except Exception:  # pragma: no cover - defensive
            return {}

    def exists(self) -> bool:
        return self.path.exists()

    def quarantine(self) -> Optional[Path]:
        """Move an unreadable file aside so the app can start cleanly."""
        if not self.path.exists():
            return None
        target = self.path.with_suffix(f".corrupt-{int(time.time())}{self.path.suffix}")
        try:
            os.replace(self.path, target)
            log.warning("quarantined unreadable file %s -> %s", self.path.name, target.name)
            return target
        except OSError:
            return None

    # -- io ----------------------------------------------------------------
    def load(self) -> Any:
        with self._lock:
            if not self.path.exists():
                return self._default()
            try:
                raw = self.path.read_text(encoding="utf-8")
            except OSError as exc:
                log.warning("could not read %s: %s", self.path.name, exc)
                return self._default()
            if not raw.strip():
                return self._default()
            try:
                return json.loads(raw)
            except ValueError:
                self.quarantine()
                return self._default()

    def save(self, data: Any) -> bool:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(self.path.suffix + ".tmp")
                with open(temporary, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, ensure_ascii=False, indent=1, default=str)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                return True
            except (OSError, TypeError, ValueError) as exc:
                log.error("could not save %s: %s", self.path.name, exc)
                return False

    def update(self, mutator: Callable[[Any], Any]) -> Any:
        """Load, mutate and save while holding the lock."""
        with self._lock:
            data = self.load()
            result = mutator(data)
            self.save(data if result is None else result)
            return data


__all__ = ["JsonFile"]
