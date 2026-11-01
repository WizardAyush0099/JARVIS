"""JARVIS teaches itself about its owner - carefully.

A wrong memory is worse than no memory, so these tests pin the guardrails: no
secrets, no one-off task state, no churn, and nothing learnt from a stranger.
"""

from __future__ import annotations

import pytest

from core import learning
from core.memory import Memory


class FakeAI:
    """An AI that returns whatever ``payload`` says, or raises."""

    def __init__(self, payload=None, raises: bool = False) -> None:
        self.payload = payload
        self.raises = raises
        self.calls = 0

    def chat_json(self, messages, system=None, temperature=0.0, max_tokens=300):  # noqa: D102
        self.calls += 1
        if self.raises:
            raise RuntimeError("provider down")
        return self.payload


@pytest.fixture
def memory(settings):
    return Memory(settings)


# --------------------------------------------------------------------------- #
# the cheap pre-filter
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "my name is Ayush",
        "i live in Shimla",
        "mera project Athena hai",
        "my favourite language is python",
    ],
)
def test_personal_statements_are_worth_reading(text):
    assert learning.worth_reading(text) is True


@pytest.mark.parametrize(
    "text",
    ["what is the time", "play some music", "hi", "", "open spotify please"],
)
def test_commands_and_impersonal_turns_are_skipped(text):
    assert learning.worth_reading(text) is False


# --------------------------------------------------------------------------- #
# secrets never reach memory
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "my api key is sk-abcdefghijklmnop1234",
        "password is hunter2",
        "my token: bearer abc",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_secret_shaped_values_are_refused(text):
    assert learning.looks_like_secret(text) is True


def test_ordinary_values_are_not_mistaken_for_secrets():
    assert learning.looks_like_secret("Shimla") is False
    assert learning.looks_like_secret("works at a hospital") is False


# --------------------------------------------------------------------------- #
# parsing the model's answer
# --------------------------------------------------------------------------- #
def test_keys_are_normalised_so_one_fact_is_not_two():
    facts = learning.parse_facts({"facts": {"My City": "Shimla", "the city": "Shimla"}})
    assert facts == {"city": "Shimla"}


def test_transient_and_secret_keys_are_dropped():
    facts = learning.parse_facts(
        {"facts": {"now": "x", "question": "y", "password": "hunter2", "city": "Shimla"}}
    )
    assert facts == {"city": "Shimla"}


def test_at_most_three_facts_are_taken_per_turn():
    raw = {f"key {i}": f"value {i}" for i in range(10)}
    assert len(learning.parse_facts({"facts": raw})) == learning.MAX_FACTS_PER_TURN


@pytest.mark.parametrize("payload", [None, {}, {"facts": "nope"}, {"facts": []}, "string"])
def test_malformed_payloads_produce_nothing(payload):
    assert learning.parse_facts(payload) == {}


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #
def test_extraction_reads_the_model_answer():
    ai = FakeAI({"facts": {"city": "Shimla"}})
    assert learning.extract(ai, "i live in Shimla", "Noted.") == {"city": "Shimla"}


def test_extraction_is_skipped_when_the_turn_is_not_personal():
    ai = FakeAI({"facts": {"city": "Shimla"}})
    assert learning.extract(ai, "what is the time") == {}
    assert ai.calls == 0, "an API call was spent on a turn with no personal content"


def test_a_failing_provider_is_harmless():
    assert learning.extract(FakeAI(raises=True), "my name is Ayush") == {}


# --------------------------------------------------------------------------- #
# storing
# --------------------------------------------------------------------------- #
def test_new_facts_are_stored(memory):
    stored = learning.apply({"city": "Shimla", "job": "student"}, memory)
    assert set(stored) == {"city", "job"}
    assert memory.facts()["city"] == "Shimla"


def test_unchanged_facts_are_not_rewritten(memory):
    memory.remember("city", "Shimla")
    stored = learning.apply({"city": "Shimla"}, memory)
    assert stored == []


def test_a_changed_fact_is_updated(memory):
    memory.remember("city", "Shimla")
    assert learning.apply({"city": "Delhi"}, memory) == ["city"]
    assert memory.facts()["city"] == "Delhi"


def test_learn_runs_end_to_end(settings, memory):
    ai = FakeAI({"facts": {"main project": "Athena", "city": "Shimla"}})
    stored = learning.learn(ai, "i am building Athena in Shimla", "Good.", memory)
    assert set(stored) == {"main project", "city"}
    assert memory.facts()["main project"] == "Athena"
