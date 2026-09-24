"""Planning: deterministic fast path, AI validation and graceful fallback."""

from __future__ import annotations

import pytest

from ai.offline import OfflineEngine
from core.memory import Memory
from core.planner import Planner


class StubManager:
    """Stands in for ProviderManager during planning tests."""

    def __init__(self, payload=None, fail=False, online=True) -> None:
        self.payload = payload
        self.fail = fail
        self._online = online
        self.calls = 0

    def has_online_provider(self) -> bool:
        return self._online

    def chat_json(self, messages, system=None, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("providers are down")
        return self.payload

    def complete(self, prompt, system="", **kwargs):
        return "stub"


@pytest.fixture
def memory(settings):
    return Memory(settings)


def make_planner(settings, memory, manager=None):
    offline = OfflineEngine(settings, memory)
    return Planner(settings, manager, memory, None, offline)


# --------------------------------------------------------------------------- #
# fast path
# --------------------------------------------------------------------------- #
def test_fast_path_answers_identity_without_a_model(settings, memory):
    manager = StubManager(payload={"steps": []})
    plan = make_planner(settings, memory, manager).plan("who am I")
    assert plan.source == "rules"
    assert plan.reply == "You are Ayush."
    assert manager.calls == 0, "unambiguous requests must not cost an API call"


def test_fast_path_produces_a_single_tool_step(settings, memory):
    plan = make_planner(settings, memory, StubManager()).plan("what time is it")
    assert plan.source == "rules" and len(plan.steps) == 1
    assert plan.steps[0].tool == "current_time"


# --------------------------------------------------------------------------- #
# AI path
# --------------------------------------------------------------------------- #
def test_ai_plan_is_used_for_open_ended_requests(settings, memory):
    manager = StubManager(
        payload={
            "intent": "research",
            "reasoning": "needs fresh information",
            "steps": [{"tool": "web_research", "args": {"query": "pi 5"}, "reason": "fresh facts"}],
            "reply": None,
        }
    )
    plan = make_planner(settings, memory, manager).plan("why is my raspberry pi so slow")
    assert manager.calls == 1
    assert plan.source == "ai"
    assert plan.steps[0].tool == "web_research"
    assert plan.steps[0].args == {"query": "pi 5"}


def test_ai_can_answer_without_tools(settings, memory):
    manager = StubManager(payload={"intent": "chat", "reasoning": "no tools needed", "steps": [], "reply": "A haiku: ..."})
    plan = make_planner(settings, memory, manager).plan("write me a haiku")
    assert plan.reply.startswith("A haiku")
    assert plan.steps == []


def test_unknown_tools_are_dropped(settings, memory):
    manager = StubManager(
        payload={"steps": [{"tool": "delete_the_universe", "args": {}}, {"tool": "current_time", "args": {}}]}
    )
    plan = make_planner(settings, memory, manager).plan("do something odd")
    assert [step.tool for step in plan.steps] == ["current_time"]
    assert any("unknown tool" in note for note in plan.notes)


def test_invented_arguments_are_stripped(settings, memory):
    manager = StubManager(
        payload={"steps": [{"tool": "calculate", "args": {"expression": "2+2", "sneaky": "x"}}]}
    )
    plan = make_planner(settings, memory, manager).plan("add them up")
    assert plan.steps[0].args == {"expression": "2+2"}


def test_steps_are_capped(settings, memory):
    manager = StubManager(
        payload={"steps": [{"tool": "current_time", "args": {}} for _ in range(9)]}
    )
    plan = make_planner(settings, memory, manager).plan("do many things")
    assert len(plan.steps) <= 3


def test_draft_reply_is_discarded_when_tools_will_run(settings, memory):
    manager = StubManager(
        payload={"steps": [{"tool": "current_time", "args": {}}], "reply": "I already did it"}
    )
    plan = make_planner(settings, memory, manager).plan("what time is it right now")
    assert plan.steps and plan.reply is None


def test_broken_ai_response_falls_back(settings, memory):
    plan = make_planner(settings, memory, StubManager(payload="not a dict")).plan("explain quantum computing")
    assert plan.source == "offline"
    assert plan.reply


def test_provider_failure_does_not_crash_planning(settings, memory):
    plan = make_planner(settings, memory, StubManager(fail=True)).plan("explain quantum computing")
    assert plan.reply
    assert "AI provider" in plan.reply


# --------------------------------------------------------------------------- #
# prompt
# --------------------------------------------------------------------------- #
def test_system_prompt_contains_identity_facts_and_tools(settings, memory):
    memory.remember("project", "Athena")
    planner = make_planner(settings, memory, StubManager())
    prompt = planner.system_prompt()
    assert "Ayush" in prompt
    assert "Athena" in prompt
    assert "calculate" in prompt
    assert "JSON" in prompt


def test_tool_catalogue_lists_every_registered_tool(settings, memory):
    from tools.base import all_tools

    catalogue = make_planner(settings, memory, StubManager()).tool_catalogue()
    for spec in all_tools():
        assert spec.name in catalogue


def test_describe_reports_tool_count(settings, memory):
    described = make_planner(settings, memory, StubManager()).describe()
    assert described["tools"] > 10
    assert described["has_online_provider"] is True
