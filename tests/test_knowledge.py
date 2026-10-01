"""Wikipedia needs no API key, so a factual question should never be refused
just because the AI providers are down.  These tests never touch the network -
the lookups are stubbed.
"""

from __future__ import annotations

import pytest

from tools import knowledge
from tools.knowledge import (
    answer_without_a_provider,
    first_sentences,
    format_answer,
    looks_factual,
    topic_from,
    wikipedia_lookup,
)

ARTICLE = {
    "title": "Photosynthesis",
    "description": "Biological process to convert light into chemical energy",
    "extract": (
        "Photosynthesis is a system of biological processes by which plants convert "
        "light into chemical energy. Some of this chemical energy is stored in "
        "carbohydrates. Photosynthesis usually refers to oxygenic photosynthesis."
    ),
    "url": "https://en.wikipedia.org/wiki/Photosynthesis",
    "source": "wikipedia",
}


# --------------------------------------------------------------------------- #
# deciding what is worth looking up
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "question",
    ["who is Albert Einstein", "what is photosynthesis", "Tell me about the Taj Mahal",
     "when was the moon landing", "define entropy"],
)
def test_factual_questions_are_recognised(question):
    assert looks_factual(question) is True


@pytest.mark.parametrize(
    "question",
    ["play some music", "remind me to call mom", "what is 25 * 4",
     "convert 10 km to miles", "50% of 200", ""],
)
def test_commands_and_arithmetic_are_not_sent_to_a_reference_work(question):
    assert looks_factual(question) is False


@pytest.mark.parametrize(
    "question,expected",
    [
        ("who is Albert Einstein", "Albert Einstein"),
        ("what is photosynthesis?", "photosynthesis"),
        ("Tell me about the Taj Mahal", "Taj Mahal"),
        ("what's the capital of France", "capital of France"),
        ("who's Elon Musk", "Elon Musk"),
        ("please tell me about who is Ada Lovelace", "Ada Lovelace"),
    ],
)
def test_the_subject_is_pulled_out_of_the_question(question, expected):
    assert topic_from(question) == expected


# --------------------------------------------------------------------------- #
# shaping the answer
# --------------------------------------------------------------------------- #
def test_the_answer_leads_with_the_fact_and_cites_the_source():
    answer = format_answer(ARTICLE)
    assert "Photosynthesis" in answer
    assert "Source: https://en.wikipedia.org/wiki/Photosynthesis" in answer


def test_only_the_opening_sentences_are_used():
    text = "One. Two. Three. Four."
    assert first_sentences(text, count=2) == "One. Two."


def test_a_very_short_extract_is_still_returned():
    assert first_sentences("No full stops here", count=2) == "No full stops here"


# --------------------------------------------------------------------------- #
# the lookup itself (stubbed - no network in tests)
# --------------------------------------------------------------------------- #
def test_the_lookup_reports_success_with_the_source(monkeypatch):
    monkeypatch.setattr(knowledge, "wikipedia", lambda topic, timeout=8.0: dict(ARTICLE))
    result = wikipedia_lookup("photosynthesis")
    assert result.ok is True
    assert "en.wikipedia.org" in result.output
    assert result.data["title"] == "Photosynthesis"


def test_a_missing_article_is_a_failure_not_a_crash(monkeypatch):
    monkeypatch.setattr(knowledge, "wikipedia", lambda topic, timeout=8.0: None)
    result = wikipedia_lookup("qqq zzz nonexistent thing")
    assert result.ok is False
    assert "no article" in result.error


def test_a_network_error_does_not_take_the_turn_down(monkeypatch):
    def boom(topic, timeout=8.0):
        raise RuntimeError("no route to host")

    monkeypatch.setattr(knowledge, "wikipedia", boom)
    result = wikipedia_lookup("anything")
    assert result.ok is False


def test_an_empty_topic_is_asked_for():
    assert wikipedia_lookup("  ").ok is False


# --------------------------------------------------------------------------- #
# the no-provider path
# --------------------------------------------------------------------------- #
def test_a_factual_question_gets_an_answer_with_no_ai_provider(monkeypatch):
    monkeypatch.setattr(
        knowledge, "wikipedia", lambda topic, timeout=8.0: dict(ARTICLE, title=topic.title())
    )
    answer = answer_without_a_provider("who is photosynthesis")
    assert answer is not None
    assert "Source:" in answer


def test_a_command_still_gets_no_keyless_lookup(monkeypatch):
    def forbidden(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("a command must not trigger a reference lookup")

    monkeypatch.setattr(knowledge, "wikipedia", forbidden)
    assert answer_without_a_provider("play some music") is None


# --------------------------------------------------------------------------- #
# the planner: a real answer instead of a pitch about buying a key
# --------------------------------------------------------------------------- #
def _planner(settings):
    from ai.offline import OfflineEngine
    from core.memory import Memory
    from core.planner import Planner

    memory = Memory(settings)
    return Planner(settings, None, memory, None, OfflineEngine(settings, memory))


def test_a_factual_question_is_answered_rather_than_refused(settings, monkeypatch):
    monkeypatch.setenv("WIKIPEDIA_ENABLED", "true")
    monkeypatch.setattr(
        knowledge, "wikipedia", lambda topic, timeout=8.0: dict(ARTICLE, title=topic.title())
    )

    plan = _planner(settings).plan("who is photosynthesis")

    assert plan.source == "wikipedia"
    assert "Source:" in plan.reply
    assert "API key" not in plan.reply, "a real answer beats a subscription pitch"


def test_switching_the_lookup_off_restores_the_notice(settings, monkeypatch):
    monkeypatch.setenv("WIKIPEDIA_ENABLED", "false")

    def forbidden(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("must not look anything up when switched off")

    monkeypatch.setattr(knowledge, "wikipedia", forbidden)

    plan = _planner(settings).plan("who is photosynthesis")
    assert "API key" in plan.reply
