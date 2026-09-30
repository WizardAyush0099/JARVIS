"""Language handling - English, Hindi and Hinglish.

Ayush talks to JARVIS in three registers: plain English, Hindi in Devanagari,
and Hinglish (Hindi written in Latin letters).  He should not have to configure
anything for the first case - if he writes in Hindi, the answer comes back in
Hindi - and "speak in hindi" / "hinglish me bolo" must stick for the rest of the
conversation.

This module is deliberately tiny and dependency-free so the deterministic intent
layer, the brain and the planner can all use it without an import cycle.  It does
three things:

* :func:`detect` guesses the language of a message (Devanagari = Hindi, common
  romanised-Hindi words = Hinglish, otherwise English)
* :func:`command` recognises an explicit "answer in <language>" instruction
* :func:`instruction` renders the prompt block that tells the model which
  language to answer in
"""

from __future__ import annotations

import re
from typing import Optional

#: language codes this assistant understands
ENGLISH = "en"
HINDI = "hi"
HINGLISH = "hinglish"
#: follow whatever language the user writes in
AUTO = "auto"

CODES = (ENGLISH, HINDI, HINGLISH)

_DEVANAGARI = re.compile(r"[\u0900-\u097f]")

#: romanised-Hindi words that almost never appear in English.  One of these is
#: enough to call a message Hinglish - they are the words Hinglish is made of.
_HINGLISH_MARKERS = frozenset(
    {
        "hai", "hain", "hoga", "hogi", "hu", "hun", "hoon", "kya", "kyaa", "kyun",
        "kyu", "kaise", "kaisa", "kaisi", "kitna", "kitne", "kahan", "kab",
        "nahi", "nahin", "nai", "haan", "ha", "ji", "mera", "meri", "mere",
        "mujhe", "mujhko", "tum", "tumhara", "tumhari", "aap", "aapka", "apna",
        "bhai", "yaar", "dost", "karo", "karna", "kar", "karlo", "batao", "bata",
        "bolo", "bol", "bolna", "chahiye", "chahta", "chahti", "theek", "thik",
        "accha", "achha", "bahut", "bohot", "abhi", "phir", "fir", "wala", "vala",
        "wali", "vali", "matlab", "samajh", "jaldi", "kal", "aaj", "naam", "kaam",
        "paisa", "ghar", "dhanyavad", "shukriya", "namaste", "pranam", "kripya",
        "sakta", "sakti", "sakte", "mat", "kuch", "koi", "log", "baat", "suno",
        "dekho", "dekh", "chalo", "jao", "gaya", "gayi", "raha", "rahi", "rahe",
        "hona", "karna", "de", "do", "dena", "lena", "bhool", "yaad",
    }
)

_WORD_RE = re.compile(r"[a-z]+")


def detect(text: str) -> str:
    """Guess the language of ``text``: ``"hi"``, ``"hinglish"`` or ``"en"``."""
    raw = str(text or "")
    if _DEVANAGARI.search(raw):
        return HINDI
    words = _WORD_RE.findall(raw.lower())
    if any(word in _HINGLISH_MARKERS for word in words):
        return HINGLISH
    return ENGLISH


def normalise(value: str) -> str:
    """Turn a user/setting value into a code, or ``""`` for "follow the user"."""
    low = str(value or "").strip().lower()
    if not low or low in {AUTO, "default", "same", "follow", "any"}:
        return ""
    if low in {ENGLISH, "eng", "english", "angrezi", "अंग्रेजी"}:
        return ENGLISH
    if low in {HINDI, "hindī", "hindi", "हिंदी", "हिन्दी"}:
        return HINDI
    if low in {HINGLISH, "hinglish", "roman", "romanised", "romanized", "hindi-english"}:
        return HINGLISH
    if "hinglish" in low:
        return HINGLISH
    if "hindi" in low:
        return HINDI
    if "english" in low:
        return ENGLISH
    return ""


def resolve(message: str, preference: str = "") -> str:
    """The language an answer should be written in.

    An explicit preference wins; otherwise JARVIS mirrors the message.  The
    result is always one of the three real codes.
    """
    chosen = normalise(preference)
    if chosen in CODES:
        return chosen
    return detect(message)


# --------------------------------------------------------------------------- #
# explicit instructions
# --------------------------------------------------------------------------- #
_TARGETS = (
    ("hinglish", HINGLISH),
    ("हिंदी", HINDI),
    ("हिन्दी", HINDI),
    ("hindi", HINDI),
    ("अंग्रेजी", ENGLISH),
    ("english", ENGLISH),
)

_SWITCH_VERB = re.compile(
    r"\b(?:speak|talk|reply|respond|answer|write|switch|change|use|"
    r"baat|bol|bolo|bolna|likho|likh|jawab|बोलो|बोल|लिखो|लिख|जवाब|बात)\b"
)
_PREPOSITION = re.compile(r"\b(?:in|into|me|mein|में|to)\b")
_REFUSAL = re.compile(r"\b(?:don'?t|dont|does ?n'?t|can'?t|cannot|never|not|no|mat|nahi|नहीं)\b")


def command(text: str) -> Optional[str]:
    """The language the user just asked for, or ``None``.

    Recognises "speak in hindi", "hinglish me bolo", "answer in english",
    "हिंदी में बोलो" and friends - but not a passing mention of a language.
    """
    low = str(text or "").lower()
    if not low:
        return None
    for word, code in _TARGETS:
        if word in low:
            break
    else:
        return None
    if _REFUSAL.search(low):
        return None
    if not _SWITCH_VERB.search(low):
        return None
    # "speak hindi" (no preposition) is still a command; "the hindi word for X"
    # is not, because it has no switch verb at all.
    return code


# --------------------------------------------------------------------------- #
# prompt + spoken confirmation
# --------------------------------------------------------------------------- #
_INSTRUCTIONS = {
    ENGLISH: (
        "Answer in English."
    ),
    HINDI: (
        "Answer in Hindi, written in Devanagari script. Use everyday spoken Hindi, "
        "not a formal literary register. Keep technical words (file names, commands, "
        "product names) as they are."
    ),
    HINGLISH: (
        "Answer in Hinglish: Hindi written in Latin letters, the way people chat in "
        "India. Mix in English technical words naturally. Do not use Devanagari script."
    ),
}

_CONFIRMATIONS = {
    ENGLISH: "Got it - I'll answer in English from now on.",
    HINDI: "ठीक है - अब मैं हिंदी में जवाब दूँगा।",
    HINGLISH: "Theek hai - ab main Hinglish mein jawab dunga.",
}

_RESET = "Okay - I'll follow whatever language you write in from now on."


def instruction(message: str, preference: str = "") -> str:
    """The prompt block telling the model which language to answer in."""
    code = resolve(message, preference)
    detail = _INSTRUCTIONS.get(code, _INSTRUCTIONS[ENGLISH])
    return (
        "LANGUAGE: "
        + detail
        + " If the user asks you to switch language, follow the newest request "
        "immediately. Never mix scripts inside one answer."
    )


def confirmation(code: str) -> str:
    """A short line acknowledging a language switch, in the new language."""
    chosen = normalise(code)
    if not chosen:
        return _RESET
    return _CONFIRMATIONS.get(chosen, _RESET)


__all__ = [
    "AUTO",
    "CODES",
    "ENGLISH",
    "HINDI",
    "HINGLISH",
    "command",
    "confirmation",
    "detect",
    "instruction",
    "normalise",
    "resolve",
]
