"""Offline engine - JARVIS with no internet and no API keys.

The spec's requirement 20 says the assistant must stay useful without a
connection.  Rather than a second, thinner chatbot, the offline engine *reuses
the same intent table as the online brain* and simply executes the read-only
subset of tools locally.

It deliberately refuses to run anything that is not flagged ``offline_safe``, so
no destructive action can ever be triggered by a rule match.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from config.settings import env
from core.logging_setup import get_logger
from core import intent as intent_layer

log = get_logger("offline")

#: Categories an offline rule may execute.  All of them are local: they read the
#: machine, the disk or the GPIO header, so they keep working with no internet.
#: (Destructive tools are excluded separately, by their ``dangerous`` flag.)
OFFLINE_CATEGORIES = frozenset(
    {"utilities", "system", "files", "memory", "identity", "hardware"}
)

OFFLINE_HINT = (
    "I'm running offline right now, so I can only handle time, dates, calculations, "
    "unit conversions, system status, notes, reminders, your GPIO devices and anything "
    "I already remember. Add an AI provider key (or start a local Ollama model) and "
    "I'll be fully back."
)


class OfflineEngine:
    """Deterministic, network-free answering."""

    def __init__(self, settings: Any = None, memory: Any = None, events: Any = None) -> None:
        self.settings = settings
        self.memory = memory
        self.events = events

    # -- identity ----------------------------------------------------------
    @property
    def owner(self) -> str:
        if self.settings is not None:
            return getattr(self.settings, "owner_name", "Ayush")
        return env("JARVIS_OWNER", "Ayush")

    @property
    def assistant(self) -> str:
        if self.settings is not None:
            return getattr(self.settings, "assistant_name", "JARVIS")
        return env("JARVIS_NAME", "JARVIS")

    def capabilities(self) -> List[str]:
        return [
            "time and date",
            "calculations and unit conversions",
            "system status (CPU, RAM, disk, temperature, network)",
            "GPIO devices (listing, reading sensors, switching outputs)",
            "reading and searching local files",
            "notes, reminders and local memory",
            "the GUI, the web interface and local voice I/O",
        ]

    def summary(self) -> Dict[str, Any]:
        return {"engine": "offline", "owner": self.owner, "capabilities": self.capabilities()}

    # -- answering ---------------------------------------------------------
    def answer(self, text: str, ctx: Any = None) -> Optional[str]:
        """Best-effort offline reply, or ``None`` if this needs a real model."""
        if not text or not text.strip():
            return None

        found = intent_layer.match(text, owner=self.owner, assistant=self.assistant)
        if found is None:
            return self._memory_fallback(text)

        if found.direct_reply:
            return found.direct_reply

        if not found.tool:
            return None

        # Local import keeps the import graph flat and cycle-free.
        from tools import base as tool_base

        spec = tool_base.get_tool(found.tool)
        if spec is None:
            log.debug("offline intent %s wants missing tool %s", found.name, found.tool)
            return None
        if not spec.offline_safe or found.category not in OFFLINE_CATEGORIES:
            return None
        if spec.dangerous:
            return None

        context = ctx
        if context is None:
            context = tool_base.ToolContext(
                settings=self.settings, memory=self.memory, events=self.events
            )

        result = tool_base.execute(spec.name, found.args, ctx=context)
        if result.needs_confirmation:
            return result.needs_confirmation
        if not result.ok:
            return f"I couldn't do that offline: {result.error}"
        return result.output or None

    def _memory_fallback(self, text: str) -> Optional[str]:
        """Answer simple recall questions straight from stored facts."""
        if self.memory is None:
            return None
        try:
            facts = self.memory.facts()
        except Exception:  # pragma: no cover - defensive
            return None
        if not facts:
            return None

        lowered = text.lower()
        if not any(word in lowered for word in ("remember", "recall", "my ", "what did i")):
            return None

        words = {word for word in lowered.replace("?", " ").split() if len(word) > 3}
        if not words:
            return None
        best_key, best_score = None, 0
        for key in facts:
            key_words = set(str(key).lower().replace("_", " ").split())
            score = len(words & key_words)
            if score > best_score:
                best_key, best_score = key, score
        if best_key is None or best_score == 0:
            return None
        return f"You told me {best_key} is {facts[best_key]}."


__all__ = ["OFFLINE_CATEGORIES", "OFFLINE_HINT", "OfflineEngine"]
