"""Facts JARVIS picks up without being told to remember them.

The explicit memory tools only fire when the user says "remember X".  That
misses the ordinary things people state in passing: their city, their job, a
project name, the tools they use, what they are building.  This module reads a
finished turn and stores what is durable, so the assistant gets to know its
owner instead of starting from zero every conversation.

A wrong memory is worse than no memory, so the rules are deliberately strict:

* only things the **user** said about themselves - never the assistant's claims
  (a model that says "you live in Delhi" must not teach JARVIS that you do);
* nothing that looks like a secret, a password, a key or a token;
* nothing one-off or speculative ("what is the time", "maybe later");
* keys are normalised, so "my city" and "city" stay one fact instead of two;
* a value that has not changed is not rewritten, so nothing is churned.

Extraction runs off the reply path, so it never adds latency to an answer.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping

from core.logging_setup import get_logger

log = get_logger("learning")

#: Never store more than this many facts from a single turn.
MAX_FACTS_PER_TURN = 3
MAX_KEY_LEN = 40
MAX_VALUE_LEN = 160

#: Anything matching this is refused outright, wherever it appears.
_SECRET = re.compile(
    r"(?:password|passwd|passphrase|secret|api[\s_-]?key|access[\s_-]?token|"
    r"refresh[\s_-]?token|bearer|private[\s_-]?key|-----BEGIN|"
    r"sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})",
    re.IGNORECASE,
)

#: First-person cues.  If none is present the turn is very unlikely to contain a
#: fact about the user, and the model call is skipped entirely.
_SELF_CUES = re.compile(
    r"\b(?:i|i'm|im|me|my|mine|myself|we|our|mera|meri|mujhe|main|mai)\b",
    re.IGNORECASE,
)

#: Keys that describe the turn rather than the person.
_TRANSIENT_KEYS = frozenset(
    {
        "question", "request", "task", "command", "message", "text", "answer",
        "response", "reply", "now", "today", "currently", "current", "this",
        "it", "that", "thing", "user",
    }
)

_EXTRACT_SYSTEM = """You extract durable facts about the user from one exchange.

Return JSON only: {"facts": {"key": "value"}}.

Rules:
- Only facts the USER stated about themselves, in the user's own words.
- Durable things only: name, city, country, job, studies, projects, tools they
  use, languages, preferences, recurring goals, important people or devices.
- Never include tasks, questions, requests, numbers from a calculation, or
  anything the assistant said about the user.
- Never include passwords, keys, tokens or anything that looks like a secret.
- Use a short lower-case key (2-3 words), e.g. "city", "job", "main project".
- If there is nothing durable, return {"facts": {}}.
"""


def looks_like_secret(text: str) -> bool:
    """True when a value must never be written to memory."""
    return bool(_SECRET.search(str(text or "")))


def worth_reading(user_text: str) -> bool:
    """Cheap pre-filter: is this turn even a candidate for a personal fact?

    Saves an API call on every "what's the weather" and every bare command.
    """
    text = str(user_text or "").strip()
    if len(text) < 8:
        return False
    return bool(_SELF_CUES.search(text))


def _clean_key(raw: Any) -> str:
    key = re.sub(r"[^a-z0-9 _-]+", " ", str(raw or "").lower())
    key = re.sub(r"^(?:my|the|a|an)\s+", "", key.strip())
    key = re.sub(r"\s+", " ", key).strip(" _-")[:MAX_KEY_LEN]
    return key


def _clean_value(raw: Any) -> str:
    value = re.sub(r"\s+", " ", str(raw or "")).strip().strip("\"'")
    return value[:MAX_VALUE_LEN]


def parse_facts(payload: Any) -> Dict[str, str]:
    """Validate a model response into a clean ``{key: value}`` map."""
    if not isinstance(payload, Mapping):
        return {}
    raw = payload.get("facts")
    if not isinstance(raw, Mapping):
        return {}
    facts: Dict[str, str] = {}
    for raw_key, raw_value in raw.items():
        key = _clean_key(raw_key)
        value = _clean_value(raw_value)
        if not key or not value or key in _TRANSIENT_KEYS:
            continue
        if len(value) < 2 or looks_like_secret(key) or looks_like_secret(value):
            continue
        facts[key] = value
        if len(facts) >= MAX_FACTS_PER_TURN:
            break
    return facts


def extract(ai: Any, user_text: str, reply_text: str = "") -> Dict[str, str]:
    """Ask the model for durable facts about the user.  Never raises."""
    if ai is None or not worth_reading(user_text):
        return {}
    chat_json = getattr(ai, "chat_json", None)
    if not callable(chat_json):
        return {}
    prompt = (
        "The user said:\n" + str(user_text or "")[:2000] + "\n\n"
        "The assistant replied:\n" + str(reply_text or "")[:1000] + "\n\n"
        'Return only the JSON object, e.g. {"facts": {"city": "Shimla"}}.'
    )
    try:
        payload = chat_json(
            [{"role": "user", "content": prompt}],
            system=_EXTRACT_SYSTEM,
            temperature=0.0,
            max_tokens=300,
        )
    except Exception as exc:  # noqa: BLE001 - learning is always optional
        log.debug("fact extraction skipped: %s", exc)
        return {}
    return parse_facts(payload)


def apply(facts: Mapping[str, str], memory: Any) -> List[str]:
    """Store the facts that are new or changed, and return what was learnt."""
    if memory is None or not facts:
        return []
    try:
        existing = memory.facts()
    except Exception:  # noqa: BLE001 - defensive
        existing = {}
    stored: List[str] = []
    for key, value in facts.items():
        if existing.get(key) == value:
            continue  # unchanged: do not churn the store
        try:
            memory.remember(key, value)
            stored.append(key)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not store fact %r: %s", key, exc)
    return stored


def learn(ai: Any, user_text: str, reply_text: str, memory: Any) -> List[str]:
    """Extract and store in one call - what the background worker runs."""
    return apply(extract(ai, user_text, reply_text), memory)


__all__ = [
    "MAX_FACTS_PER_TURN",
    "apply",
    "extract",
    "learn",
    "looks_like_secret",
    "parse_facts",
    "worth_reading",
]
