"""The brain.

    USER INPUT -> UNDERSTANDING -> PLANNER -> TOOL SELECTION -> EXECUTION
              -> RESULT VALIDATION -> AI RESPONSE -> VOICE + GUI

:class:`Jarvis` owns the whole pipeline in one place: memory, provider fallback,
planning, tool execution, confirmation handling, speech in and speech out.

The rule that shapes every method here is **never fake a result**.  Tool output
is passed to the model verbatim (including failures), and when there is no model
the answer is composed from the real results rather than invented.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ai.manager import ProviderManager
from ai.offline import OfflineEngine
from config.settings import Settings
from core.events import (
    STATE_ERROR,
    STATE_IDLE,
    STATE_LISTENING,
    STATE_SPEAKING,
    STATE_THINKING,
    STATE_WORKING,
    EventBus,
)
from core.logging_setup import get_logger
from core.memory import Memory
from core.planner import Plan, PlanStep, Planner
from core.router import PendingAction, ToolRouter, classify_reply
from tools.base import ToolContext, ToolResult
from tools.utilities import ReminderScheduler
from voice.stt import Listener
from voice.tts import Speaker

log = get_logger("brain")

ANSWER_SYSTEM = """You are {assistant}, {owner}'s personal assistant.

You are writing the final reply for {owner}. You have just run tools; their real
results are below and you must answer from them.

Rules:
- Never claim something succeeded if its result says FAILED. Report the failure plainly and, if it is fixable, say what to change.
- Do not paste raw tool output or JSON. Turn it into natural speech.
- Include concrete details that matter: file paths, URLs, numbers, error messages.
- When you searched the web, mention that the information is from a live search and name the sources.
- Keep it short - two to five sentences unless the request needs more.
- Never mention that you were given a prompt, a plan or a system message."""


@dataclass
class Reply:
    text: str
    pending: Optional[PendingAction] = None
    steps: List[Dict[str, Any]] = field(default_factory=list)
    provider: str = ""
    images: List[str] = field(default_factory=list)
    error: bool = False
    source: str = ""
    #: stable id so a front-end that receives the same reply over both HTTP and
    #: the event socket renders it once
    message_id: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.message_id,
            "text": self.text,
            "pending": (
                {
                    "id": self.pending.id,
                    "tool": self.pending.tool,
                    "args": self.pending.args,
                    "question": self.pending.question,
                }
                if self.pending
                else None
            ),
            "steps": self.steps,
            "provider": self.provider,
            "images": self.images,
            "error": self.error,
            "source": self.source,
        }


class Jarvis:
    """The assistant. Construct one and call :meth:`handle`."""

    def __init__(
        self,
        settings: Settings,
        events: Optional[EventBus] = None,
        with_tts: bool = True,
        with_stt: bool = False,
        hardware: Any = None,
        enable_reminders: bool = True,
    ) -> None:
        self.settings = settings
        self.events = events or EventBus()
        self._lock = threading.RLock()
        self._stopped = False

        self.memory = Memory(settings, self.events)
        self.offline = OfflineEngine(settings, self.memory, self.events)
        self.ai = ProviderManager(settings, self.events, offline_engine=self.offline)

        if hardware is None:
            try:
                from hardware.gpio import HardwareManager

                hardware = HardwareManager(settings.paths.hardware_config, self.events)
            except Exception as exc:  # noqa: BLE001 - hardware is optional
                log.warning("hardware layer unavailable: %s", exc)
                hardware = None
        self.hardware = hardware

        self.reminders = ReminderScheduler(
            settings.paths.reminders_file, self.events, on_fire=self._on_reminder
        )
        self.ctx = ToolContext(
            settings=settings,
            memory=self.memory,
            events=self.events,
            ai=self.ai,
            hardware=self.hardware,
            reminders=self.reminders,
        )
        self.router = ToolRouter(self.ctx, self.events)
        self.planner = Planner(settings, self.ai, self.memory, self.events, self.offline)

        self.speaker: Optional[Speaker] = None
        if with_tts:
            try:
                from voice.tts import build_speaker

                self.speaker = build_speaker(settings, self.events)
            except Exception as exc:  # noqa: BLE001
                log.warning("speech output unavailable: %s", exc)

        self.listener: Optional[Listener] = None
        if with_stt:
            try:
                self.listener = Listener(settings, self.events, on_transcript=self._on_speech)
            except Exception as exc:  # noqa: BLE001
                log.warning("speech input unavailable: %s", exc)

        self._pending: Optional[PendingAction] = None
        self._enable_reminders = enable_reminders
        self._started = False
        self._turn = 0

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        if self._enable_reminders:
            self.reminders.start()
        if self.speaker is not None:
            self.speaker.start()
        if self.listener is not None and self.listener.enabled:
            self.listener.start()
        log.info("JARVIS online. %s", self.chain_description())

    def stop(self) -> None:
        self._stopped = True
        if self.reminders is not None:
            self.reminders.stop()
        if self.listener is not None:
            self.listener.stop()
        if self.speaker is not None:
            self.speaker.stop()
        try:
            self.memory.save()
        except Exception:
            pass
        if self.hardware is not None:
            try:
                self.hardware.close()
            except Exception:
                pass
        log.info("JARVIS stopped")

    def chain_description(self) -> str:
        try:
            return "brain chain: " + self.ai.describe_chain()
        except Exception:
            return "brain chain: offline only"

    # ------------------------------------------------------------------ #
    # main entry point
    # ------------------------------------------------------------------ #
    def handle(self, text: str, source: str = "text") -> Reply:
        request = (text or "").strip()
        if not request:
            return Reply(text="I didn't catch that - try again?", source=source)

        with self._lock:
            self._turn += 1
            self.events.set_state(STATE_THINKING)
            self.memory.add_user(request, source=source)
            self.events.publish("message", role="user", text=request, source=source)

            pending = self._pending
            if pending is not None:
                decision = classify_reply(request)
                if decision is True:
                    self._pending = None
                    return self._resume_pending(pending, source=source)
                if decision is False:
                    self._pending = None
                    return self._finish(
                        "Cancelled - I haven't done anything.", source=source, pending=None
                    )
                # anything else: abandon it and treat this as a fresh request
                self._pending = None
                self.events.publish("confirm_cancelled", message="pending action abandoned")

            try:
                plan = self.planner.plan(request)
            except Exception as exc:  # noqa: BLE001
                log.exception("planning failed")
                return self._finish(
                    f"Something went wrong while I was planning that: {exc}",
                    source=source,
                    error=True,
                )
            return self._run_plan(plan, request, source=source)

    # ------------------------------------------------------------------ #
    # execution
    # ------------------------------------------------------------------ #
    def _run_plan(self, plan: Plan, request: str, source: str) -> Reply:
        if not plan.steps:
            if plan.reply:
                return self._finish(plan.reply, source=source, provider=plan.source)
            return self._finish(
                "I'm not sure how to help with that yet.", source=source, provider=plan.source
            )
        return self._execute(plan.steps, request, source=source, plan=plan)

    def _execute(
        self,
        steps: Sequence[PlanStep],
        request: str,
        source: str,
        plan: Optional[Plan] = None,
        results: Optional[List[Tuple[PlanStep, ToolResult]]] = None,
    ) -> Reply:
        collected: List[Tuple[PlanStep, ToolResult]] = list(results or [])
        images: List[str] = []
        for index, step in enumerate(steps):
            self.events.set_state(STATE_WORKING, note=step.tool)
            result = self.router.run(step.tool, step.args)
            if result.needs_confirmation:
                pending = self.router.make_pending(step.tool, step.args, result.output, step.reason)
                pending.remaining = list(steps[index + 1 :])
                self._pending = pending
                question = self._confirm_question(step, result)
                reply = self._finish(question, source=source, pending=pending)
                reply.steps = self._step_records(collected)
                return reply
            collected.append((step, result))
            if result.ok and isinstance(result.data, dict) and result.data.get("url"):
                if step.tool == "generate_image":
                    images.append(str(result.data["url"]))

        answer = self._answer(collected, request, plan)
        reply = self._finish(answer, source=source, provider=self.ai.last_provider)
        reply.steps = self._step_records(collected)
        reply.images = images
        return reply

    def _resume_pending(self, pending: PendingAction, source: str) -> Reply:
        """The user said yes: run the confirmed tool, then any remaining steps."""
        self.events.set_state(STATE_WORKING, note=pending.tool)
        result = self.router.run(pending.tool, pending.args, force=True)
        step = PlanStep(tool=pending.tool, args=pending.args, reason=pending.reason)
        collected: List[Tuple[PlanStep, ToolResult]] = [(step, result)]

        remaining = list(getattr(pending, "remaining", []) or [])
        if remaining and result.ok:
            return self._execute(remaining, "continue the task", source=source, results=collected)

        answer = self._answer(collected, "the action I just confirmed", None)
        reply = self._finish(answer, source=source, provider=self.ai.last_provider)
        reply.steps = self._step_records(collected)
        return reply

    def confirm(self, approved: bool, source: str = "ui") -> Reply:
        with self._lock:
            pending = self._pending
            if pending is None:
                return Reply(text="There's nothing waiting for confirmation.", source=source)
            self._pending = None
        if approved:
            return self._resume_pending(pending, source=source)
        return self._finish("Cancelled - I haven't done anything.", source=source)

    @property
    def pending(self) -> Optional[PendingAction]:
        return self._pending

    # ------------------------------------------------------------------ #
    # answering
    # ------------------------------------------------------------------ #
    def _answer(
        self,
        results: Sequence[Tuple[PlanStep, ToolResult]],
        request: str,
        plan: Optional[Plan],
    ) -> str:
        if not results:
            return "I didn't need to do anything for that."

        blocks = self._result_blocks(results)
        failures = [r for _, r in results if not r.ok]

        if self.ai is not None and self.ai.has_online_provider():
            context = self.memory.context()
            if context and context[-1].get("role") == "user":
                context = context[:-1]  # the current request goes in the rich prompt
            prompt = (
                f"{self.settings.owner_name} asked: {request}\n\n"
                f"Tool results (these are real, use them):\n{blocks}\n\n"
                "Write the reply now."
            )
            try:
                return self.ai.chat(
                    [*context, {"role": "user", "content": prompt}],
                    system=ANSWER_SYSTEM.format(
                        assistant=self.settings.assistant_name, owner=self.settings.owner_name
                    ),
                    max_tokens=self.settings.ai.max_tokens,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("could not compose with AI, falling back: %s", exc)

        return self._answer_offline(results, bool(failures))

    def _result_blocks(self, results: Sequence[Tuple[PlanStep, ToolResult]]) -> str:
        blocks: List[str] = []
        for step, result in results:
            if result.needs_confirmation:
                continue
            if result.ok:
                body = (result.output or "(no output)")[:2000]
                blocks.append(f"- {step.tool}: SUCCEEDED\n  {body}")
            else:
                blocks.append(f"- {step.tool}: FAILED\n  {result.error[:600]}")
        return "\n".join(blocks) or "(no results)"

    def _answer_offline(self, results: Sequence[Tuple[PlanStep, ToolResult]], any_failure: bool) -> str:
        successes = [r.output for _, r in results if r.ok and (r.output or "").strip()]
        failures = [r for _, r in results if not r.ok]
        if failures and not successes:
            return "I couldn't do that: " + failures[0].error
        text = "\n".join(successes)
        if failures:
            text += "\n\nOne part didn't work: " + failures[0].error
        return text.strip() or "Done."

    def _confirm_question(self, step: PlanStep, result: ToolResult) -> str:
        args = step.args or {}
        if step.tool == "send_email":
            to = args.get("to", "that address")
            subject = args.get("subject") or "(no subject)"
            return (
                f"Your email to {to} is ready - subject \"{subject}\". "
                "Should I send it? (yes or no)"
            )
        if step.tool == "system_power":
            action = str(args.get("action", "shut down")).lower()
            return f"You've asked me to {action} this machine. Confirm? (yes or no)"
        if step.tool == "delete_file":
            return f"I'm about to move {args.get('path', 'that file')} to my trash folder. Confirm? (yes or no)"
        if step.tool == "run_python_file":
            return f"You want me to run {args.get('path', 'that script')}. Confirm? (yes or no)"
        if step.tool == "close_app":
            return f"Close {args.get('app', 'that application')}? (yes or no)"
        question = (result.needs_confirmation or result.output or "").strip()
        if question and not question.lower().startswith("confirm"):
            return f"{question} (yes or no)"
        return f"I need your go-ahead to run {step.tool}. Confirm? (yes or no)"

    def _step_records(self, results: Sequence[Tuple[PlanStep, ToolResult]]) -> List[Dict[str, Any]]:
        return [
            {
                "tool": step.tool,
                "args": step.args,
                "ok": result.ok,
                "output": (result.output or "")[:400],
                "error": result.error,
            }
            for step, result in results
        ]

    # ------------------------------------------------------------------ #
    # finishing a turn
    # ------------------------------------------------------------------ #
    def _finish(
        self,
        text: str,
        source: str = "text",
        pending: Optional[PendingAction] = None,
        provider: str = "",
        error: bool = False,
    ) -> Reply:
        message = (text or "").strip() or "Done."
        message_id = uuid.uuid4().hex[:12]
        self.memory.add_assistant(
            message,
            provider=provider or self.ai.last_provider,
            pending=bool(pending),
            message_id=message_id,
        )
        self.events.publish(
            "message",
            role="assistant",
            text=message,
            message_id=message_id,
            provider=provider or self.ai.last_provider,
            pending=bool(pending),
        )
        if error:
            self.events.set_state(STATE_ERROR, note="last turn failed")
        self._speak(message)
        return Reply(
            text=message,
            pending=pending,
            provider=provider,
            error=error,
            source=source,
            message_id=message_id,
        )

    def _speak(self, text: str) -> None:
        speaker = self.speaker
        if speaker is None or speaker.muted or not speaker.available:
            if self.events.state != STATE_ERROR:
                self.events.set_state(STATE_IDLE)
            return

        listener = self.listener

        def done() -> None:
            if listener is not None and listener.enabled:
                listener.resume()
            if self.events.state not in {STATE_THINKING, STATE_WORKING}:
                self.events.set_state(STATE_IDLE)

        if listener is not None and listener.enabled:
            listener.pause()  # never listen to our own voice
        self.events.set_state(STATE_SPEAKING)
        if not speaker.speak(text, on_done=done):
            if listener is not None and listener.enabled:
                listener.resume()
            if self.events.state == STATE_SPEAKING:
                self.events.set_state(STATE_IDLE)

    def speak(self, text: str) -> bool:
        if self.speaker is None:
            return False
        return self.speaker.speak(text)

    # ------------------------------------------------------------------ #
    # voice input
    # ------------------------------------------------------------------ #
    def _on_speech(self, text: str, transcript: Any = None) -> None:
        """Called from the listener thread when a phrase is recognised."""
        try:
            self.handle(text, source="voice")
        except Exception:  # pragma: no cover - never kill the listener
            log.exception("voice turn failed")

    def listen_once(self, timeout: float = 6.0) -> Reply:
        """Push-to-talk used by the mic button in the GUI and web UI."""
        if self.listener is None:
            return Reply(text="Microphone support isn't installed on this machine.", error=True)
        self.events.set_state(STATE_LISTENING)
        transcript = self.listener.listen_once(timeout=timeout)
        if not transcript.ok:
            self.events.set_state(STATE_IDLE)
            return Reply(text=transcript.error or "I couldn't hear anything.", error=True, source="voice")
        self.events.publish("message", role="user", text=transcript.text, source="voice")
        return self.handle(transcript.text, source="voice")

    # ------------------------------------------------------------------ #
    # reminders, state and maintenance
    # ------------------------------------------------------------------ #
    def _on_reminder(self, message: str) -> None:
        self.memory.add_assistant(message, kind="reminder")
        self.events.publish("message", role="assistant", text=message, kind="reminder")
        self._speak(message)

    def clear_history(self) -> int:
        removed = self.memory.clear_conversation()
        self._pending = None
        self.events.publish("cleared", message="Conversation cleared", removed=removed)
        return removed

    def reset_providers(self) -> None:
        self.ai.reset_cooldowns()
        self.events.publish("provider", message="Provider cooldowns cleared")

    def set_muted(self, muted: bool) -> bool:
        if self.speaker is None:
            return bool(muted)
        self.speaker.set_muted(bool(muted))
        self.events.publish("speech", message="muted" if muted else "unmuted")
        return self.speaker.muted

    def status(self) -> Dict[str, Any]:
        return {
            "state": self.events.state,
            "state_note": self.events.state_note,
            "turn": self._turn,
            "provider": self.ai.last_provider or (self.settings.ai.providers[0].slug if self.settings.ai.providers else ""),
            "providers": self.ai.status(),
            "chain": [p.slug for p in self.settings.ai.providers],
            "memory": self.memory.stats(),
            "facts": self.memory.facts(),
            "speech": self.speaker.status() if self.speaker else {"enabled": False, "engine": "none"},
            "mic": self.listener.status() if self.listener else {"enabled": False, "engine": "none"},
            "hardware": self.hardware.status() if self.hardware else {"backend": "none", "devices": []},
            "pending": (
                {"id": self._pending.id, "tool": self._pending.tool, "question": self._pending.question}
                if self._pending
                else None
            ),
            "tools": len(self.planner.tool_catalogue().splitlines()),
            "recent_tools": self.router.recent(8),
        }


__all__ = ["ANSWER_SYSTEM", "Jarvis", "Reply"]
