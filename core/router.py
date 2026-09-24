"""Tool router.

Sits between the planner and the tool registry and owns everything that should
*not* be duplicated inside individual tools:

* confirmation gating for dangerous capabilities
* timeouts (a hung tool must not freeze the GUI)
* structured logging of what ran, what it returned and how long it took
* events so the GUI/web UI can show live tool activity
* the "pending action" object that survives between two user turns
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.logging_setup import get_logger, redact
from tools.base import ToolContext, ToolResult, execute, get_tool

log = get_logger("router")

AFFIRMATIVE = {
    "yes", "y", "yeah", "yep", "sure", "ok", "okay", "do it", "go ahead", "confirm",
    "send it", "yes please", "proceed", "affirmative", "haan", "ha", "kar do",
}
NEGATIVE = {
    "no", "n", "nope", "cancel", "stop", "don't", "dont", "abort", "forget it",
    "never mind", "nevermind", "nahi", "na", "rehne do",
}


def classify_reply(text: str) -> Optional[bool]:
    """Map a short user reply to True (go), False (cancel) or None (unrelated)."""
    cleaned = (text or "").strip().lower().strip("!.?,")
    if not cleaned or len(cleaned) > 40:
        return None
    if cleaned in AFFIRMATIVE:
        return True
    if cleaned in NEGATIVE:
        return False
    first = cleaned.split()[0] if cleaned.split() else ""
    if first in AFFIRMATIVE:
        return True
    if first in NEGATIVE:
        return False
    return None


@dataclass
class PendingAction:
    tool: str
    args: Dict[str, Any]
    question: str
    created: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    reason: str = ""
    #: plan steps that still have to run once this action is approved
    remaining: List[Any] = field(default_factory=list)

    @property
    def age(self) -> float:
        return time.time() - self.created

    def matches(self, text: str) -> bool:
        return self.id in (text or "")


class ToolRouter:
    def __init__(self, ctx: ToolContext, events: Any = None) -> None:
        self.ctx = ctx
        self.events = events or getattr(ctx, "events", None)
        self._lock = threading.RLock()
        self.history: List[Dict[str, Any]] = []
        self.history_limit = 60

    # -- helpers -----------------------------------------------------------
    def _publish(self, kind: str, **payload: Any) -> None:
        if self.events is None:
            return
        try:
            self.events.publish(kind, **payload)
        except Exception:  # pragma: no cover - UI plumbing must never break a tool
            pass

    def tool_snapshot(self) -> Dict[str, Any]:
        spec_names = {tool.name for tool in _all_specs()}
        return {"count": len(spec_names), "names": sorted(spec_names)}

    def auto_confirmed(self, tool_name: str) -> bool:
        """Tools the user has explicitly trusted to run without asking."""
        settings = getattr(self.ctx, "settings", None)
        if settings is None:
            return False
        if tool_name == "send_email" and getattr(settings.email, "auto_send", False):
            return True
        return False

    # -- execution ---------------------------------------------------------
    def run(
        self,
        tool_name: str,
        args: Optional[Dict[str, Any]] = None,
        force: bool = False,
        timeout: Optional[float] = None,
    ) -> ToolResult:
        spec = get_tool(tool_name)
        if spec is None:
            return ToolResult.failure(f"unknown tool '{tool_name}'")

        safe_args = {key: redact(value) if isinstance(value, str) else value for key, value in (args or {}).items()}
        started = time.time()
        self._publish(
            "tool_start",
            message=f"running {spec.name}",
            tool=spec.name,
            args={k: (v if not isinstance(v, str) else v[:200]) for k, v in safe_args.items()},
            category=spec.category,
        )

        use_force = force or self.auto_confirmed(spec.name)
        result = execute(spec.name, safe_args, ctx=self.ctx, timeout=timeout, force=use_force)
        elapsed = round(time.time() - started, 3)

        entry = {
            "tool": spec.name,
            "category": spec.category,
            "args": safe_args,
            "ok": result.ok,
            "elapsed": elapsed,
            "output": (result.output or "")[:2000],
            "error": (result.error or "")[:800],
            "confirmation": bool(result.needs_confirmation),
            "at": time.time(),
        }
        with self._lock:
            self.history.append(entry)
            if len(self.history) > self.history_limit:
                del self.history[: len(self.history) - self.history_limit]

        if result.needs_confirmation:
            self._publish("confirm", message=result.needs_confirmation, tool=spec.name, args=safe_args)
            log.info("awaiting confirmation for %s", spec.name)
        elif result.ok:
            log.info("%s ok in %.2fs", spec.name, elapsed)
            self._publish(
                "tool_result",
                message=f"{spec.name} finished",
                tool=spec.name,
                ok=True,
                elapsed=elapsed,
                output=(result.output or "")[:500],
                data=result.data,
            )
        else:
            log.warning("%s failed in %.2fs: %s", spec.name, elapsed, result.error)
            self._publish(
                "tool_result",
                message=f"{spec.name} failed",
                tool=spec.name,
                ok=False,
                elapsed=elapsed,
                error=result.error,
            )
        return result

    def make_pending(self, tool_name: str, args: Dict[str, Any], question: str, reason: str = "") -> PendingAction:
        return PendingAction(tool=tool_name, args=dict(args or {}), question=question, reason=reason)

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.history[-max(1, int(limit)) :])


def _all_specs() -> List[Any]:
    from tools.base import all_tools

    return all_tools()


__all__ = ["AFFIRMATIVE", "NEGATIVE", "PendingAction", "ToolRouter", "classify_reply"]
