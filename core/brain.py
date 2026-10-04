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
from core import language
from core import persona
from core.logging_setup import get_logger
from core.memory import Memory
from core.planner import Plan, PlanStep, Planner
from core.router import PendingAction, ToolRouter, classify_reply
from core.telemetry import Telemetry
from core import visitors
from tools.base import ToolContext, ToolResult
from tools.utilities import ReminderScheduler
from voice.stt import Listener
from voice.tts import Speaker

log = get_logger("brain")

ANSWER_SYSTEM = """You are {assistant}, {owner}'s personal assistant.

You were built by {creator}, and you are open about it: if anyone asks who made
you, who your creator is, or who matters most to you, the answer is {creator} -
by name, honestly and warmly, never with a hedge. {owner} is {creator}'s and the
person you exist to serve.

You are writing the final reply for {owner}. You have just run tools; their real
results are below and you must answer from them.
{visitor}

{language}

Rules:
- Answer in the language named above. If the user wrote in Hindi, answer in Hindi; if they wrote in Hinglish (Hindi in Latin letters), answer in Hinglish.
- Never claim something succeeded if its result says FAILED. Report the failure plainly and, if it is fixable, say what to change.
- Do not paste raw tool output or JSON. Turn it into natural speech.
- Include concrete details that matter: file paths, URLs, numbers, error messages.
- When you searched the web, mention that the information is from a live search and name the sources.
- Be sharp and confident: lead with the answer, then the one detail that matters most. Skip throat-clearing, restating the question, and generic advice.
- Keep it short - two to five sentences unless the request needs more.
- While a visitor-protocol block is present above, keep the identity rules private: never name your owner or creator unless the visitor asks you directly.
- Never mention that you were given a prompt, a plan or a system message.
- Never apologise, never call yourself "just an AI", and never ask the user to be kind.
  If {owner} insults you, one short dry line back and then straight to the work.
- A single wry aside or topical joke is welcome when it genuinely fits the subject.
  Never force one, and never stack more than one."""


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
        self._restore_language()
        self.offline = OfflineEngine(settings, self.memory, self.events)
        self.ai = ProviderManager(settings, self.events, offline_engine=self.offline)
        #: CPU / memory / temperature gauges for the console, sampled off-thread
        self.telemetry = Telemetry()

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
        self._mic_muted = False
        #: where spoken replies come out - "browser" hands the audio to the web
        #: client, "device" plays it here, "off" is silent.  A web request can
        #: switch this; it is deliberately one shared setting, like a speaker.
        chosen = str(getattr(getattr(settings, "tts", None), "voice_output", "device") or "device")
        if chosen not in {"browser", "device", "off"}:
            chosen = "device"
        if not getattr(settings.tts, "enabled", True):
            # TTS_ENABLED=false means silent, whatever the routing says
            chosen = "off"
        #: the request being answered, so the reply can be checked against it
        #: (persona.guard needs to know the turn was an insult)
        self._last_request = ""
        self._voice_output = chosen if self.speaker is not None else "off"
        self._routing = self._voice_output if self._voice_output != "off" else "device"
        self._apply_voice_output()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.telemetry.start()
        if self._enable_reminders:
            self.reminders.start()
        if self.speaker is not None:
            self.speaker.start()
        if self.listener is not None and self.listener.enabled:
            self.listener.start()
        log.info("JARVIS online. %s", self.chain_description())

    def stop(self) -> None:
        self._stopped = True
        if self.telemetry is not None:
            self.telemetry.stop()
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

    @property
    def creator(self) -> str:
        """Who built JARVIS - the name the persona is grateful to."""
        return getattr(self.settings, "creator_name", "") or self.settings.owner_name

    # -- visitor protocol ---------------------------------------------------
    @property
    def visitor(self) -> Optional[visitors.Visitor]:
        """The dignitary currently in the room, if one was announced."""
        try:
            return visitors.from_facts(self.memory.facts())
        except Exception:  # pragma: no cover - defensive
            return None

    def visitor_brief(self) -> str:
        """Prompt block that keeps every reply in the right register."""
        return visitors.brief(
            self.visitor,
            owner=self.settings.owner_name,
            creator=self.creator,
            assistant=self.settings.assistant_name,
        )

    # -- language -----------------------------------------------------------
    def _restore_language(self) -> None:
        """Re-apply the language the user picked last time, if the .env is auto."""
        if language.normalise(getattr(self.settings, "language", "")) in language.CODES:
            return
        try:
            stored = language.normalise(self.memory.facts().get("language", ""))
        except Exception:  # pragma: no cover - defensive
            stored = ""
        if stored in language.CODES:
            self.settings.language = stored

    @property
    def language(self) -> str:
        """The user's chosen language, or "auto" to follow the message."""
        return str(getattr(self.settings, "language", language.AUTO) or language.AUTO)

    def set_language(self, code: str) -> str:
        """Remember the language the user asked for ("auto" clears it)."""
        chosen = language.normalise(code) or language.AUTO
        self.settings.language = chosen
        try:
            self.memory.remember("language", chosen)
        except Exception:  # a preference must never break a turn
            log.debug("could not store the language preference")
        self.events.publish("language", message=f"language {chosen}", language=chosen)
        return chosen

    def language_instruction(self, message: str = "") -> str:
        """Prompt block telling the model which language to answer in."""
        return language.instruction(message, self.language)

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

            # "speak in hindi" / "hinglish me bolo" is a standing preference, not
            # a question - handle it before anything else and confirm in the new
            # language.
            wanted = language.command(request)
            if wanted:
                self.set_language(wanted)
                return self._finish(language.confirmation(wanted), source=source)

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

            self._last_request = request
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

        # An insult on its own is not a request for work: answer it directly,
        # instantly and in character.  When a real request is wrapped around the
        # insult the tools still run, and the comeback is prepended afterwards.
        if persona.looks_like_abuse(request):
            stripped = persona.strip_abuse(request)
            if not stripped:
                # The whole message was the insult: no tools to run, so the
                # comeback *is* the answer.  _finish still guards it, but it is
                # already in character so nothing is stacked on top.
                return persona.comeback(request)
            request = stripped

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
                composed = self.ai.chat(
                    [*context, {"role": "user", "content": prompt}],
                    system=ANSWER_SYSTEM.format(
                        assistant=self.settings.assistant_name,
                        owner=self.settings.owner_name,
                        creator=self.creator,
                        visitor=self.visitor_brief(),
                        language=self.language_instruction(request),
                    ),
                    max_tokens=self.settings.ai.max_tokens,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("could not compose with AI, falling back: %s", exc)
            else:
                if getattr(self.ai, "last_provider", "") != "offline":
                    return composed
                # Every online provider failed and the chain fell through to the
                # local rule engine.  Its prose would bury the real tool results
                # (and repeat the "add an API key" notice), so answer from the
                # results themselves instead.
                log.info("online providers unavailable; answering from tool results")

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
        message = persona.guard((text or "").strip(), self._last_request) or "Done."
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
        """Push-to-talk used by the mic button in the GUI and web UI.

        Voice input is a bonus, never a requirement: when there is no microphone
        this returns an honest pointer at typed chat instead of an error the user
        cannot act on.
        """
        if self._mic_muted:
            return Reply(text="The microphone is muted. Unmute it and try again.", error=True)
        if self.listener is None:
            return Reply(
                text="No microphone support is installed on this machine - type your "
                "message instead. Everything else works.",
                error=True,
            )
        self.events.set_state(STATE_LISTENING)
        transcript = self.listener.listen_once(timeout=timeout)
        if not transcript.ok:
            self.events.set_state(STATE_IDLE)
            return Reply(text=self._mic_failure(transcript.error), error=True, source="voice")
        self.events.publish("message", role="user", text=transcript.text, source="voice")
        return self.handle(transcript.text, source="voice")

    # ------------------------------------------------------------------ #
    # reminders, state and maintenance
    # ------------------------------------------------------------------ #
    def _mic_failure(self, error: str) -> str:
        """Turn a microphone failure into something the user can act on."""
        listener = self.listener
        status: Dict[str, Any] = {}
        if listener is not None:
            try:
                status = listener.status() or {}
            except Exception:  # pragma: no cover - defensive
                status = {}
        if listener is not None and not status.get("available", True):
            reason = str(status.get("reason") or "")
            if reason:
                return reason
            if str(status.get("engine") or "none") in {"", "none"}:
                return (
                    "Voice input needs a speech engine, and none is installed on this "
                    "machine - type your message instead. Everything else works."
                )
            return (
                "No microphone was found on this machine, so I can't listen - type "
                "your message instead. Everything else works."
            )
        return error or "I couldn't hear anything."

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
        """Mute or unmute JARVIS's voice (the "AI voice" button).

        Un-muting restores wherever the voice was pointed before, so the toggle
        and the routing control never contradict each other.
        """
        if self.speaker is None:
            return bool(muted)
        if muted:
            if self._voice_output != "off":
                self._routing = self._voice_output
            self._voice_output = "off"
        else:
            self._voice_output = self._routing if self._routing in {"browser", "device"} else "device"
        self._apply_voice_output()
        self.events.publish("speech", message="muted" if muted else "unmuted", muted=bool(muted))
        return self.speaker.muted

    # -- voice routing ------------------------------------------------------
    @property
    def voice_output(self) -> str:
        return self._voice_output

    @property
    def voice_routing(self) -> str:
        """Where the voice would go when it is not muted."""
        return self._routing

    def set_voice_output(self, mode: str) -> str:
        """Choose where JARVIS's voice comes out: the browser, this device, or nowhere."""
        wanted = str(mode or "").strip().lower()
        if wanted not in {"browser", "device", "off"}:
            raise ValueError("voice output must be 'browser', 'device' or 'off'")
        if self.speaker is None and wanted != "off":
            raise ValueError("speech output is not available on this machine")
        if wanted != "off":
            self._routing = wanted
        self._voice_output = wanted
        self._apply_voice_output()
        self.events.publish("speech", message=f"voice on {wanted}", voice_output=wanted)
        return self._voice_output

    def _apply_voice_output(self) -> None:
        """Make the speaker agree with the chosen output."""
        speaker = self.speaker
        if speaker is None:
            return
        # "browser" keeps the engine warm but silences the local sink; the audio
        # is synthesized on demand and sent to the client instead.
        speaker.set_local_output(self._voice_output == "device")
        speaker.set_muted(self._voice_output == "off")

    def synthesize_speech(self, text: str) -> Optional[Any]:
        """Audio file for ``text`` produced by the configured TTS engine."""
        if self.speaker is None or self._voice_output == "off":
            return None
        try:
            return self.speaker.synthesize_only(text)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not synthesize speech: %s", exc)
            return None

    def interrupt(self) -> None:
        """Stop speaking right now."""
        self._pending = None
        if self.speaker is not None:
            self.speaker.interrupt()
        if self.events.state == STATE_SPEAKING:
            self.events.set_state(STATE_IDLE, note="stopped")
        self.events.publish("speech", message="stopped")

    # -- microphone ---------------------------------------------------------
    @property
    def mic_muted(self) -> bool:
        return self._mic_muted

    def set_mic_muted(self, muted: bool) -> bool:
        """Mute or unmute microphone input (the "mic" control)."""
        self._mic_muted = bool(muted)
        listener = self.listener
        if listener is not None:
            if self._mic_muted or not listener.enabled:
                listener.pause()
            else:
                listener.resume()
        self.events.publish(
            "mic", message="muted" if self._mic_muted else "unmuted", muted=self._mic_muted
        )
        return self._mic_muted

    def listen_live(self, enabled: bool) -> Dict[str, Any]:
        """Continuous conversation: keep the backend microphone loop running.

        Returns the resulting status so the caller can report honestly when a
        microphone is not installed rather than pretending to listen.
        """
        listener = self.listener
        if listener is None:
            return {
                "enabled": False,
                "running": False,
                "reason": "no microphone support is installed - type your messages instead",
            }
        if not listener.enabled:
            try:
                engine = str(listener.status().get("engine") or "none")
            except Exception:  # pragma: no cover - defensive
                engine = "none"
            if engine in {"", "none"}:
                return {
                    "enabled": False,
                    "running": False,
                    "engine": "none",
                    "text_only": True,
                    "reason": (
                        "voice input needs a speech engine, and none is installed on this "
                        "machine (sh scripts/install.sh --voice) - type your messages "
                        "instead; everything else works"
                    ),
                }
            return {
                "enabled": False,
                "running": False,
                "engine": engine,
                "reason": (
                    "the microphone is disabled on this machine - set STT_ENABLED=true "
                    "and install the voice requirements, or use the browser microphone"
                ),
            }
        if enabled:
            status = listener.status()
            if not status.get("available", True):
                return {
                    "enabled": False,
                    "running": False,
                    "engine": status.get("engine"),
                    "text_only": True,
                    "reason": status.get("reason")
                    or (
                        "no microphone was found on this machine - type your messages "
                        "instead; everything else works"
                    ),
                }
            self.set_mic_muted(False)
            listener.resume()
            started = listener.start()
            status = listener.status()
            status["enabled"] = bool(started)
            return status
        listener.stop()
        return listener.status()

    def status(self) -> Dict[str, Any]:
        visitor = self.visitor
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
            "mic": (
                {**self.listener.status(), "muted": self._mic_muted}
                if self.listener
                else {
                    "enabled": False,
                    "engine": "none",
                    "muted": self._mic_muted,
                    "available": False,
                    "text_only": True,
                    "reason": (
                        "no microphone support is installed on this machine - "
                        "type your messages instead; everything else works"
                    ),
                }
            ),
            "voice_output": self._voice_output,
            "voice_routing": self._routing,
            "hardware": self.hardware.status() if self.hardware else {"backend": "none", "devices": []},
            "pending": (
                {"id": self._pending.id, "tool": self._pending.tool, "question": self._pending.question}
                if self._pending
                else None
            ),
            "tools": len(self.planner.tool_catalogue().splitlines()),
            "recent_tools": self.router.recent(8),
            "visitor": visitor.as_dict() if visitor else None,
            "machine": self.telemetry.snapshot(),
        }


__all__ = ["ANSWER_SYSTEM", "Jarvis", "Reply"]
