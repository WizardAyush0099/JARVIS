"""End-to-end brain behaviour, including the "never fake it" guarantee."""

from __future__ import annotations

import pytest

from core.planner import Plan, PlanStep
from core.router import classify_reply
from tools import base


# --------------------------------------------------------------------------- #
# basic conversation
# --------------------------------------------------------------------------- #
def test_identity_and_time_work_with_no_api_keys(jarvis):
    assert "Ayush" in jarvis.handle("who am I").text
    assert ":" in jarvis.handle("what time is it").text


def test_tools_actually_run(jarvis):
    reply = jarvis.handle("calculate 27 x 43")
    assert reply.text.strip().endswith("1,161")
    assert reply.steps[0]["tool"] == "calculate"
    assert reply.steps[0]["ok"] is True


def test_conversation_is_remembered_and_clearable(jarvis):
    jarvis.handle("hello")
    assert jarvis.memory.stats()["messages"] >= 2
    removed = jarvis.clear_history()
    assert removed >= 2
    assert jarvis.memory.conversation() == []


def test_facts_persist_across_turns(jarvis):
    jarvis.handle("remember my project is called Athena")
    reply = jarvis.handle("what was my project called")
    assert "athena" in reply.text.lower()


def test_empty_input_is_handled(jarvis):
    assert jarvis.handle("   ").text


# --------------------------------------------------------------------------- #
# honesty
# --------------------------------------------------------------------------- #
def test_failure_is_reported_and_not_fabricated(jarvis, monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("simulated hardware failure")

    monkeypatch.setattr(jarvis.planner, "plan", lambda text: Plan(
        intent="test", steps=[PlanStep(tool="system_status", args={"metric": "cpu"})]
    ))
    monkeypatch.setitem(_registry(), "system_status", base.Tool(
        name="system_status",
        description="broken",
        parameters={"type": "object", "properties": {}},
        func=broken,
        category="system",
    ))
    reply = jarvis.handle("check the cpu")
    assert "simulated hardware failure" in reply.text
    assert reply.steps[0]["ok"] is False


def _registry():
    return base._REGISTRY


def test_success_is_only_claimed_when_the_tool_succeeded(jarvis, monkeypatch):
    monkeypatch.setattr(jarvis.planner, "plan", lambda text: Plan(
        intent="test", steps=[PlanStep(tool="read_text_file", args={"path": "definitely-missing.txt"})]
    ))
    reply = jarvis.handle("read that file")
    assert "no file" in reply.text.lower() or "couldn't" in reply.text.lower()
    assert "here is the content" not in reply.text.lower()


# --------------------------------------------------------------------------- #
# confirmation flow
# --------------------------------------------------------------------------- #
def test_dangerous_action_asks_before_running(jarvis, settings, monkeypatch):
    victim = settings.paths.root / "precious.txt"
    victim.write_text("keep me", encoding="utf-8")

    monkeypatch.setattr(jarvis.planner, "plan", lambda text: Plan(
        intent="delete", steps=[PlanStep(tool="delete_file", args={"path": str(victim)})]
    ))

    reply = jarvis.handle("delete precious.txt")
    assert reply.pending is not None
    assert "yes or no" in reply.text.lower()
    assert "deleted" not in reply.text.lower()
    assert victim.exists(), "nothing may happen before confirmation"

    confirmed = jarvis.confirm(True)
    assert confirmed.pending is None
    assert "precious.txt" in confirmed.text
    assert not victim.exists()
    assert list(settings.paths.trash_dir.iterdir())


def test_replying_no_cancels(jarvis, settings, monkeypatch):
    victim = settings.paths.root / "precious.txt"
    victim.write_text("keep me", encoding="utf-8")
    monkeypatch.setattr(jarvis.planner, "plan", lambda text: Plan(
        intent="delete", steps=[PlanStep(tool="delete_file", args={"path": str(victim)})]
    ))

    jarvis.handle("delete precious.txt")
    reply = jarvis.handle("no")
    assert "cancel" in reply.text.lower()
    assert victim.exists()
    assert jarvis.pending is None


def test_yes_in_plain_text_confirms(jarvis, settings, monkeypatch):
    victim = settings.paths.root / "precious.txt"
    victim.write_text("keep me", encoding="utf-8")
    monkeypatch.setattr(jarvis.planner, "plan", lambda text: Plan(
        intent="delete", steps=[PlanStep(tool="delete_file", args={"path": str(victim)})]
    ))

    jarvis.handle("delete precious.txt")
    reply = jarvis.handle("yes please")
    assert not victim.exists()
    assert reply.pending is None


def test_an_unrelated_reply_abandons_the_pending_action(jarvis, settings, monkeypatch):
    victim = settings.paths.root / "precious.txt"
    victim.write_text("keep me", encoding="utf-8")

    def plan_for(text):
        if "delete" in text:
            return Plan(intent="delete", steps=[PlanStep(tool="delete_file", args={"path": str(victim)})])
        return Plan(intent="time", steps=[PlanStep(tool="current_time", args={})])

    monkeypatch.setattr(jarvis.planner, "plan", plan_for)

    jarvis.handle("delete precious.txt")
    assert jarvis.pending is not None
    jarvis.handle("what time is it")
    assert jarvis.pending is None
    assert victim.exists()


def test_email_confirmation_question_is_human(jarvis, settings, monkeypatch):
    monkeypatch.setattr(jarvis.planner, "plan", lambda text: Plan(
        intent="email",
        steps=[PlanStep(tool="send_email", args={"to": "rahul@example.com", "subject": "Project", "body": "it's ready"})],
    ))
    reply = jarvis.handle("email rahul that the project is ready")
    assert reply.pending is not None
    assert "rahul@example.com" in reply.text
    assert "send it" in reply.text.lower()


@pytest.mark.parametrize(
    "text,expected",
    [
        ("yes", True), ("yep", True), ("go ahead", True), ("do it", True), ("haan", True),
        ("no", False), ("cancel", False), ("nope", False), ("nahi", False),
        ("what time is it", None), ("", None), ("yes but also tell me the weather in Delhi tomorrow", None),
    ],
)
def test_classify_reply(text, expected):
    assert classify_reply(text) == expected


# --------------------------------------------------------------------------- #
# state + events
# --------------------------------------------------------------------------- #
def test_state_events_are_published(jarvis):
    jarvis.handle("hello")
    kinds = {event["kind"] for event in jarvis.events.recent(50)}
    assert "message" in kinds
    assert "state" in kinds


def test_status_snapshot_is_complete(jarvis):
    status = jarvis.status()
    for key in ("state", "providers", "memory", "speech", "mic", "tools", "recent_tools"):
        assert key in status
    assert status["providers"], "the status panel needs the provider list"
    for provider in status["providers"]:
        assert {"slug", "label", "status", "model"} <= set(provider)
        assert "api_key" not in provider


def test_tool_activity_is_tracked(jarvis):
    jarvis.handle("calculate 2+2")
    recent = jarvis.router.recent(5)
    assert recent and recent[-1]["tool"] == "calculate"
    assert recent[-1]["ok"] is True


def test_muting_voice_is_reported(jarvis):
    assert jarvis.speaker is None  # TTS disabled in tests
    assert jarvis.set_muted(True) is True
