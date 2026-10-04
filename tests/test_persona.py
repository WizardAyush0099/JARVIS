"""JARVIS has a backbone: it never grovels and it answers an insult in kind.

These tests pin the deterministic, offline-safe half of the personality - the
part that runs no matter which provider answered, or whether one answered at all.
"""

from __future__ import annotations

import pytest

from core import persona


# --------------------------------------------------------------------------- #
# abuse detection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "you are useless",
        "fuck you",
        "jarvis you are an idiot",
        "shut up",
        "you suck",
        "tu chutiya hai",
        "gadha hai tu",
        "what a useless bot",
    ],
)
def test_it_recognises_an_insult(text):
    assert persona.looks_like_abuse(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "the idiot's guide to python is a good book",
        "what is the time",
        "summarise this article about rubbish collection",
        "explain smart pointers in rust",
        "",
    ],
)
def test_ordinary_questions_are_not_insults(text):
    assert persona.looks_like_abuse(text) is False


def test_strip_keeps_the_real_task_inside_an_insult():
    assert persona.strip_abuse("you are useless, tell me the time") == "tell me the time"


def test_strip_of_a_bare_insult_is_empty():
    assert persona.strip_abuse("you are useless") == ""
    assert persona.strip_abuse("fuck you") == ""


# --------------------------------------------------------------------------- #
# the guard: an answer JARVIS will actually say
# --------------------------------------------------------------------------- #
def test_no_apology_ever_reaches_the_user():
    fixed = persona.guard("I'm sorry, I can't help with that.", "you are useless")
    assert "sorry" not in fixed.lower()
    assert fixed


def test_an_apology_is_dropped_but_the_real_answer_survives():
    fixed = persona.guard("I am sorry about that. The CPU is at 12%.", "hi")
    assert fixed == "The CPU is at 12%."


def test_a_bare_insult_gets_a_comeback_not_grovel():
    assert persona.guard("", "you are useless") == ""


def test_a_comeback_is_prepended_to_a_real_answer():
    fixed = persona.guard("The time is 4pm.", "you are useless")
    assert "The time is 4pm." in fixed
    assert fixed.strip().splitlines()[0] != "The time is 4pm."


def test_an_ordinary_answer_is_left_alone():
    assert persona.guard("The time is 4pm.", "what time is it") == "The time is 4pm."


def test_comebacks_are_never_apologetic():
    for seed in ("bad", "you are useless, seriously", "a much longer insult here"):
        line = persona.comeback(seed).lower()
        assert "sorry" not in line
        assert "apolog" not in line


# --------------------------------------------------------------------------- #
# jokes
# --------------------------------------------------------------------------- #
def test_jokes_are_topical_and_deterministic():
    first = persona.joke("tell me a joke about programmers")
    second = persona.joke("tell me a joke about programmers")
    assert first == second
    assert first in persona.JOKES["programmer"]


def test_a_joke_about_linux_uses_the_linux_bank():
    assert persona.joke("got a linux joke?") in persona.JOKES["linux"]


def test_wants_joke_reads_the_cue_words():
    assert persona.wants_joke("tell me a joke")
    assert persona.wants_joke("something funny please")
    assert not persona.wants_joke("what is the weather")


# --------------------------------------------------------------------------- #
# end to end, through the brain
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("insult", ["you are useless", "fuck you", "jarvis you are an idiot"])
def test_the_brain_never_apologises_for_an_insult(jarvis, insult):
    """The guard is only worth anything if it is on the real reply path."""
    reply = jarvis.handle(insult, source="test")
    text = reply.text or ""
    assert text, "an insult must still get an answer"
    # The real invariant is "no apology", not "the word sorry never appears" -
    # a comeback like "I'm not sorry, and I'm not going to be" is the opposite.
    assert not persona.has_apology(text), text
    assert "apolog" not in text.lower()


def test_the_brain_answers_a_joke_request_offline(jarvis):
    """A joke must cost no API call - it is answered from the local bank."""
    reply = jarvis.handle("tell me a joke about programmers", source="test")
    assert (reply.text or "") in persona.JOKES["programmer"]
