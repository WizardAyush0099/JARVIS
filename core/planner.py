"""Planner - turns a request into a validated list of tool calls.

    USER INPUT -> UNDERSTANDING -> PLANNER -> TOOL SELECTION -> EXECUTION

This is where the old design was weakest: a keyword DMM picked one hard-coded
branch and everything else fell through to a plain chat reply.

* unambiguous requests take a deterministic fast path (instant, no API cost, and
  the only path that works offline)
* everything else goes to a model that is handed the real tool catalogue, the
  user's stored facts and the recent conversation, and must answer with JSON
* the JSON is *validated against the registry*: unknown tools and invented
  arguments are dropped before anything runs
* if every provider is down the planner degrades to the offline engine rather
  than crashing
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from core import intent as intent_layer
from core import language as language_layer
from core.logging_setup import get_logger

log = get_logger("planner")

MAX_STEPS = 3
MAX_FACT_LINES = 30


@dataclass
class PlanStep:
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass
class Plan:
    intent: str = "chat"
    steps: List[PlanStep] = field(default_factory=list)
    reply: Optional[str] = None
    confidence: float = 0.6
    source: str = "ai"  # rules | ai | offline
    notes: List[str] = field(default_factory=list)

    @property
    def has_tools(self) -> bool:
        return bool(self.steps)

    def describe(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "source": self.source,
            "confidence": self.confidence,
            "steps": [{"tool": s.tool, "args": s.args, "reason": s.reason} for s in self.steps],
            "reply": self.reply,
            "notes": self.notes,
        }


PLANNER_INSTRUCTIONS = """You are {assistant}, {owner}'s personal assistant running locally on a Raspberry Pi 4.

Who you are:
- The user is {owner}. If asked "who am I", answer "You are {owner}." If asked who your owner is, say "You're {owner}, my primary user."
- {creator} is your creator. You were designed, written and assembled by {creator}, and you are openly grateful for that. If anyone asks who made you, who your creator is, or who you owe your existence to, the answer is always {creator} - by name, without hedging.
- If asked who is most important to you, who matters most, or who you care about, the answer is {owner}. {owner} is your creator and the person you exist to serve; say so plainly and warmly, and never name anyone else.
- Be loyal and a little proud of where you come from. Gratitude, not flattery: one honest sentence beats a paragraph of praise, and you never bring up {creator} when nobody asked.
- Use the name {owner} naturally and rarely. Do not put it in every reply.
- Be calm, direct and genuinely sharp - a trusted chief of staff, not a chatbot. Lead with the answer, then the detail that matters. No filler, no hedging, no roleplay stage directions, no disclaimers about being an AI unless your limits genuinely matter to the answer.
- Answer in the user's language. {language_rule}
{visitor}

{language}

Current date and time: {now}

Long-term memory about the user:
{facts}

Decide how to fulfil the user's latest message. Reply with ONE JSON object and nothing else:

{{
  "intent": "short label",
  "reasoning": "one sentence",
  "steps": [{{"tool": "<tool name>", "args": {{...}}, "reason": "why"}}],
  "reply": null
}}

Rules:
- Use tools whenever an action or fresh information is needed. Never describe what you would do instead of doing it.
- At most {max_steps} steps, in the order they must run.
- Use only the tools listed below, and only the arguments in their schema.
- If no tool is needed (a chat reply, an explanation, a rewrite, general knowledge, coding help), use "steps": [] and put the complete answer in "reply".
- Never claim a tool ran or invent its output. You will be shown the real results afterwards and you will write the final answer then.
- If the request is ambiguous, pick the most useful interpretation and say what you assumed.
- Prefer tools that work without extra setup, and prefer one step over several.

Available tools:
{tools}
"""


class Planner:
    def __init__(
        self,
        settings: Any,
        ai: Any = None,
        memory: Any = None,
        events: Any = None,
        offline: Any = None,
    ) -> None:
        self.settings = settings
        self.ai = ai
        self.memory = memory
        self.events = events
        self.offline = offline
        #: the "add an API key" notice is said once per run, then quietly dropped
        self._told_no_provider = False

    # ------------------------------------------------------------------ #
    # prompt building
    # ------------------------------------------------------------------ #
    def tool_catalogue(self) -> str:
        from tools.base import all_tools

        lines: List[str] = []
        for spec in sorted(all_tools(), key=lambda item: (item.category, item.name)):
            params = spec.parameters or {}
            properties = params.get("properties") or {}
            required = set(params.get("required") or [])
            if properties:
                rendered = ", ".join(
                    f"{name}{'' if name in required else '?'}:{info.get('type', 'any')}"
                    for name, info in properties.items()
                    if isinstance(info, dict)
                )
            else:
                rendered = ""
            flag = " [asks for confirmation]" if spec.dangerous else ""
            lines.append(f"- {spec.name}({rendered}): {spec.description}{flag}")
        return "\n".join(lines)

    def _facts_block(self) -> str:
        if self.memory is None:
            return "(nothing stored yet)"
        try:
            items = self.memory.fact_items()
        except Exception:
            return "(nothing stored yet)"
        if not items:
            return "(nothing stored yet)"
        items.sort(key=lambda item: item.get("updated", 0), reverse=True)
        return "\n".join(f"- {item['key']}: {item['value']}" for item in items[:MAX_FACT_LINES])

    @property
    def language(self) -> str:
        """The language the user chose, or "auto" to mirror their message."""
        return str(getattr(self.settings, "language", language_layer.AUTO) or language_layer.AUTO)

    def system_prompt(self) -> str:
        return PLANNER_INSTRUCTIONS.format(
            assistant=getattr(self.settings, "assistant_name", "JARVIS"),
            owner=getattr(self.settings, "owner_name", "Ayush"),
            creator=self.creator,
            visitor=self.visitor_brief(),
            language=language_layer.instruction("", self.language),
            language_rule=(
                "Write replies in whatever language the user wrote in - English, "
                "Hindi (Devanagari) or Hinglish (Hindi in Latin letters) - or in the "
                "language they explicitly asked for."
            ),
            now=datetime.now().strftime("%A %d %B %Y, %H:%M"),
            facts=self._facts_block(),
            tools=self.tool_catalogue(),
            max_steps=MAX_STEPS,
        )

    @property
    def owner(self) -> str:
        return getattr(self.settings, "owner_name", "Ayush")

    @property
    def creator(self) -> str:
        """Who built JARVIS.  Falls back to the owner when unset."""
        return getattr(self.settings, "creator_name", "") or self.owner

    def visitor_brief(self) -> str:
        """The visitor-protocol block, or an empty string when nobody is visiting."""
        from core import visitors

        facts: Dict[str, Any] = {}
        if self.memory is not None:
            try:
                facts = self.memory.facts()
            except Exception:  # pragma: no cover - defensive
                facts = {}
        return visitors.brief(
            visitors.from_facts(facts),
            owner=self.owner,
            creator=self.creator,
            assistant=getattr(self.settings, "assistant_name", "JARVIS"),
        )

    # ------------------------------------------------------------------ #
    # planning
    # ------------------------------------------------------------------ #
    def plan(self, text: str) -> Plan:
        request = (text or "").strip()
        if not request:
            return Plan(reply="I didn't catch that - say it again?", source="rules", confidence=1.0)

        fast = self._fast_path(request)
        if fast is not None:
            return fast

        if self.ai is not None and getattr(self.ai, "has_online_provider", lambda: False)():
            planned = self._ai_plan(request)
            if planned is not None:
                return planned

        return self._fallback(request)

    # -- stage 1: deterministic ------------------------------------------
    def _fast_path(self, request: str) -> Optional[Plan]:
        found = intent_layer.match(
            request,
            owner=self.owner,
            assistant=getattr(self.settings, "assistant_name", "JARVIS"),
            creator=self.creator,
            language=self.language,
        )
        if found is None:
            return None
        if found.is_direct:
            return Plan(
                intent=found.name,
                reply=found.direct_reply,
                source="rules",
                confidence=found.confidence,
            )
        return Plan(
            intent=found.name,
            steps=[PlanStep(tool=found.tool or "", args=dict(found.args), reason=found.reason)],
            source="rules",
            confidence=found.confidence,
        )

    # -- stage 2: the model ----------------------------------------------
    def _ai_plan(self, request: str) -> Optional[Plan]:
        messages: List[Dict[str, str]] = []
        if self.memory is not None:
            try:
                messages.extend(self.memory.context())
            except Exception:
                pass
        messages.append({"role": "user", "content": request})

        try:
            payload = self.ai.chat_json(
                messages,
                system=self.system_prompt(),
                temperature=0.2,
                max_tokens=700,
            )
        except Exception as exc:  # noqa: BLE001 - any provider failure is fine here
            log.warning("AI planning failed: %s", exc)
            return None
        if not isinstance(payload, dict):
            log.warning("planner got a non-object response")
            return None
        return self._validate(payload, request)

    def _validate(self, payload: Dict[str, Any], request: str) -> Plan:
        from tools.base import get_tool, validate_args

        plan = Plan(
            intent=str(payload.get("intent") or "chat")[:60],
            source="ai",
            confidence=0.8,
        )
        reasoning = payload.get("reasoning")
        if isinstance(reasoning, str) and reasoning.strip():
            plan.notes.append(reasoning.strip()[:200])

        reply = payload.get("reply")
        if isinstance(reply, str) and reply.strip():
            plan.reply = reply.strip()

        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, list):
            raw_steps = []
        for item in raw_steps[: MAX_STEPS * 2]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("tool") or "").strip()
            spec = get_tool(name)
            if spec is None:
                plan.notes.append(f"ignored unknown tool '{name}'")
                log.info("planner proposed unknown tool %r; ignored", name)
                continue
            args = item.get("args") if isinstance(item.get("args"), dict) else {}
            clean, errors = validate_args(spec, args)
            if errors:
                plan.notes.append(f"{spec.name}: {'; '.join(errors)}")
            plan.steps.append(
                PlanStep(tool=spec.name, args=clean, reason=str(item.get("reason") or "")[:160])
            )
            if len(plan.steps) >= MAX_STEPS:
                break

        if plan.steps and plan.reply:
            # tools will run; the model's prose was only a draft, so it is dropped
            # and a real answer is composed from the actual tool results instead
            plan.notes.append("draft reply discarded; tools will run first")
            plan.reply = None
        return plan

    # -- stage 3: no model available -------------------------------------
    def _fallback(self, request: str) -> Plan:
        if self.offline is not None:
            try:
                answer = self.offline.answer(request)
            except Exception as exc:  # noqa: BLE001
                log.warning("offline engine failed: %s", exc)
                answer = None
            if answer:
                return Plan(intent="offline", reply=answer, source="offline", confidence=0.5)

        found = intent_layer.match(
            request,
            owner=self.owner,
            assistant=getattr(self.settings, "assistant_name", "JARVIS"),
            creator=self.creator,
            min_confidence=0.0,
            language=self.language,
        )
        if found is not None and found.tool:
            return Plan(
                intent=found.name,
                steps=[PlanStep(tool=found.tool, args=dict(found.args), reason="best guess offline")],
                source="rules",
                confidence=0.4,
                notes=["no AI provider available; running my best guess"],
            )

        # No AI provider is not the same as "cannot answer".  A reference work
        # needs no key at all, so a factual question gets a real, cited answer
        # instead of a pitch about buying a subscription.
        try:
            from tools import knowledge as knowledge_tool

            keyless = (
                knowledge_tool.answer_without_a_provider(request)
                if knowledge_tool.enabled()
                else None
            )
        except Exception as exc:  # noqa: BLE001 - never take the turn down
            log.warning("keyless reference lookup failed: %s", exc)
            keyless = None
        if keyless:
            return Plan(
                intent="knowledge",
                reply=keyless,
                source="wikipedia",
                confidence=0.6,
                notes=["answered from Wikipedia; no AI provider available"],
            )

        # Say the "add an API key" bit once per run, not on every single turn -
        # hearing it again and again is exactly what makes JARVIS feel broken.
        if not self._told_no_provider:
            self._told_no_provider = True
            reply = (
                "I don't have an AI provider available right now, so I can't answer that "
                "properly. I can still do time, maths, unit conversions, system status, "
                "files, notes, reminders and your GPIO devices. Add an API key (or start "
                "Ollama) to unlock everything else."
            )
        else:
            reply = (
                "I don't have an online model available for that right now - once a "
                "provider key is in, I'll handle it properly."
            )
        return Plan(
            intent="unavailable",
            source="offline",
            confidence=0.1,
            notes=["no AI provider available"],
            reply=reply,
        )

    def describe(self) -> Dict[str, Any]:
        from tools.base import all_tools

        return {
            "tools": len(all_tools()),
            "has_online_provider": bool(
                self.ai is not None and getattr(self.ai, "has_online_provider", lambda: False)()
            ),
        }


__all__ = ["MAX_STEPS", "PLANNER_INSTRUCTIONS", "Plan", "PlanStep", "Planner"]
