"""Memory.

Three separate kinds of remembering, deliberately kept apart:

* **conversation** - a bounded rolling window used as model context
* **facts** - durable, explicitly stored things about the user ("my project is
  called Athena"), capped and forgettable
* **chat log** - an on-disk transcript that keeps the old ``ChatLog.json`` name
  so any tooling the user already has keeps working

The old file was loaded in one place with no size limit and no locking, and grew
forever.  This version bounds everything, migrates the old shapes it finds (the
original tutorial wrote ``{"messages": [[role, text], ...]}``), and writes
atomically so a power cut cannot corrupt it.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.logging_setup import get_logger
from core.storage import JsonFile

log = get_logger("memory")

MAX_MESSAGES = 300  # kept in memory / on disk
MAX_FACTS = 300
MAX_FACT_VALUE = 500
MAX_MESSAGE_CHARS = 20000


@dataclass
class Message:
    role: str
    content: str
    ts: float = field(default_factory=time.time)
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def time(self) -> str:
        return datetime.fromtimestamp(self.ts).strftime("%H:%M:%S")

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"role": self.role, "content": self.content, "ts": self.ts}
        if self.meta:
            payload["meta"] = self.meta
        return payload

    def to_pair(self) -> Dict[str, str]:
        """Model-facing shape (no timestamps, no tool noise)."""
        return {"role": self.role, "content": self.content}


class Memory:
    def __init__(self, settings: Any, events: Any = None) -> None:
        self.settings = settings
        self.events = events
        self._lock = threading.RLock()
        self._messages: List[Message] = []
        self._facts: Dict[str, Dict[str, Any]] = {}
        self._counter = itertools.count(1)

        paths = getattr(settings, "paths", None)
        self._store = JsonFile(paths.memory_file, dict) if paths else None
        self._chat_log = JsonFile(paths.chat_log_file, dict) if paths else None
        self._dirty = False
        self.load()

    # ------------------------------------------------------------------ #
    # conversation
    # ------------------------------------------------------------------ #
    def add(
        self, role: str, content: str, meta: Optional[Dict[str, Any]] = None
    ) -> Message:
        text = str(content or "")
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS] + "\n[truncated]"
        message = Message(role=role, content=text, ts=time.time(), meta=dict(meta or {}))
        with self._lock:
            self._messages.append(message)
            if len(self._messages) > MAX_MESSAGES:
                del self._messages[: len(self._messages) - MAX_MESSAGES]
            self._dirty = True
        self.save()
        return message

    def add_user(self, text: str, **meta: Any) -> Message:
        return self.add("user", text, meta)

    def add_assistant(self, text: str, **meta: Any) -> Message:
        return self.add("assistant", text, meta)

    def add_notice(self, text: str, **meta: Any) -> Message:
        return self.add("system", text, {**meta, "notice": True})

    def conversation(self, limit: Optional[int] = None) -> List[Message]:
        with self._lock:
            messages = list(self._messages)
        if limit is not None:
            messages = messages[-max(0, int(limit)) :]
        return messages

    def context(
        self,
        turns: Optional[int] = None,
        char_budget: int = 6000,
        include_notices: bool = False,
    ) -> List[Dict[str, str]]:
        """Recent user/assistant turns, newest kept, bounded by characters."""
        if turns is None:
            turns = int(getattr(getattr(self.settings, "ai", None), "history_turns", 12) or 12)
        with self._lock:
            relevant = [
                message
                for message in self._messages
                if message.role in {"user", "assistant"}
                or (include_notices and message.role == "system" and message.meta.get("notice"))
            ]
        window = relevant[-max(1, int(turns)) * 2 :]

        selected: List[Dict[str, str]] = []
        used = 0
        for message in reversed(window):
            cost = len(message.content)
            if used + cost > char_budget and selected:
                break
            used += cost
            selected.append(message.to_pair())
        selected.reverse()
        return selected

    def clear_conversation(self) -> int:
        with self._lock:
            removed = len(self._messages)
            self._messages = []
            self._dirty = True
        self.save()
        if self.events is not None:
            self.events.publish("memory", message="Conversation cleared", cleared=removed)
        log.info("conversation cleared (%d messages)", removed)
        return removed

    # ------------------------------------------------------------------ #
    # durable facts
    # ------------------------------------------------------------------ #
    def facts(self) -> Dict[str, str]:
        with self._lock:
            return {key: item["value"] for key, item in self._facts.items()}

    def fact_items(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {"key": key, "value": item["value"], "updated": item.get("updated", 0)}
                for key, item in self._facts.items()
            ]

    def remember(self, key: str, value: str) -> Dict[str, Any]:
        clean_key = str(key or "note").strip().lower()[:80] or "note"
        clean_value = str(value or "").strip()[:MAX_FACT_VALUE]
        with self._lock:
            if len(self._facts) >= MAX_FACTS and clean_key not in self._facts:
                oldest = min(self._facts, key=lambda k: self._facts[k].get("updated", 0))
                self._facts.pop(oldest, None)
                log.info("fact limit reached; dropped '%s'", oldest)
            self._facts[clean_key] = {"value": clean_value, "updated": time.time()}
            self._dirty = True
        self.save()
        if self.events is not None:
            self.events.publish("memory", message=f"Remembered {clean_key}", key=clean_key)
        return {"key": clean_key, "value": clean_value}

    def search_facts(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        needle = str(query or "").strip().lower()
        with self._lock:
            items = list(self._facts.items())
        if not needle:
            return [{"key": k, "value": v["value"]} for k, v in items][:limit]

        words = {word for word in needle.replace("?", " ").split() if len(word) > 2}
        scored: List[tuple] = []
        for key, item in items:
            haystack = f"{key} {item['value']}".lower()
            score = 0
            if needle in haystack:
                score += 3
            for word in words:
                if word in haystack:
                    score += 1
            if score:
                scored.append((score, key, item["value"]))
        scored.sort(key=lambda row: row[0], reverse=True)
        return [{"key": key, "value": value} for _, key, value in scored[:limit]]

    def forget(self, key: str) -> bool:
        clean = str(key or "").strip().lower()
        with self._lock:
            removed = self._facts.pop(clean, None) is not None
            self._dirty = removed
        if removed:
            self.save()
        return removed

    def clear_facts(self) -> int:
        with self._lock:
            count = len(self._facts)
            self._facts = {}
            self._dirty = True
        self.save()
        return count

    def clear_all(self) -> None:
        self.clear_conversation()
        self.clear_facts()

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #
    def _serialise(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "version": 2,
                "updated": time.time(),
                "messages": [message.to_dict() for message in self._messages],
                "facts": [
                    {"key": key, "value": item["value"], "updated": item.get("updated", 0)}
                    for key, item in self._facts.items()
                ],
            }

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            payload = self._serialise()
            self._dirty = False
        if self._store is not None:
            self._store.save(payload)
        # legacy-visible transcript, written in the old friendly shape
        if self._chat_log is not None:
            self._chat_log.save(
                {
                    "messages": [
                        [item.get("role", "user"), item.get("content", "")]
                        for item in payload["messages"]
                    ],
                    "updated": payload["updated"],
                    "note": "Written by JARVIS 2.0. Kept in the original shape for compatibility.",
                }
            )

    def load(self) -> None:
        data = self._store.load() if self._store else {}
        if isinstance(data, dict) and data.get("messages") is not None:
            self._load_v2(data)
        elif isinstance(data, dict) and data.get("facts"):
            self._load_v2({"messages": [], **data})
        else:
            self._migrate_legacy()

    def _load_v2(self, data: Dict[str, Any]) -> None:
        with self._lock:
            self._messages = []
            for item in data.get("messages") or []:
                if not isinstance(item, dict) or "content" not in item:
                    continue
                self._messages.append(
                    Message(
                        role=str(item.get("role", "user")),
                        content=str(item.get("content", "")),
                        ts=float(item.get("ts", time.time())),
                        meta=item.get("meta") or {},
                    )
                )
            self._messages = self._messages[-MAX_MESSAGES:]
            self._facts = {}
            for item in data.get("facts") or []:
                if isinstance(item, dict) and item.get("key"):
                    self._facts[str(item["key"])] = {
                        "value": str(item.get("value", "")),
                        "updated": float(item.get("updated", 0)),
                    }
            self._dirty = False
        log.info(
            "memory loaded: %d messages, %d facts", len(self._messages), len(self._facts)
        )

    def _migrate_legacy(self) -> None:
        """Import an old ChatLog.json (or nothing at all)."""
        raw = self._chat_log.load() if self._chat_log else None
        imported = 0
        if raw is not None:
            pairs = self._legacy_pairs(raw)
            with self._lock:
                for role, content in pairs[-MAX_MESSAGES:]:
                    self._messages.append(
                        Message(role=role, content=str(content)[:MAX_MESSAGE_CHARS], ts=time.time())
                    )
                imported = len(self._messages)
        if imported:
            log.info("migrated %d messages from the old ChatLog.json", imported)
            self._dirty = True
            self.save()

    @staticmethod
    def _legacy_pairs(raw: Any) -> List[tuple]:
        """Accept every shape the old project (or a hand edit) may have left."""
        candidates: Any = raw
        if isinstance(raw, dict):
            for key in ("messages", "chat", "history", "log"):
                if key in raw:
                    candidates = raw[key]
                    break
            else:
                candidates = []
        if not isinstance(candidates, list):
            return []

        pairs: List[tuple] = []
        for item in candidates:
            if isinstance(item, dict):
                role = str(item.get("role") or item.get("from") or "user").lower()
                content = item.get("content") or item.get("text") or item.get("message") or ""
                if role in {"model", "bot", "jarvis"}:
                    role = "assistant"
                if role not in {"user", "assistant"}:
                    continue
                if str(content).strip():
                    pairs.append((role, content))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                role = str(item[0]).lower()
                if role in {"model", "bot", "jarvis"}:
                    role = "assistant"
                if role in {"user", "assistant"} and str(item[1]).strip():
                    pairs.append((role, item[1]))
        return pairs

    # ------------------------------------------------------------------ #
    # views
    # ------------------------------------------------------------------ #
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "messages": len(self._messages),
                "facts": len(self._facts),
                "turns": sum(1 for m in self._messages if m.role == "user"),
            }

    def export(self, limit: int = 200) -> List[Dict[str, Any]]:
        return [
            {"role": m.role, "content": m.content, "time": m.time, "meta": m.meta}
            for m in self.conversation(limit)
        ]


__all__ = ["MAX_FACTS", "MAX_MESSAGES", "Memory", "Message"]
