"""Keyless reference lookups - answers that need no AI provider at all.

Wikipedia's API needs no key, no account and no quota.  That matters here
because "I have no AI provider" and "I cannot answer anything" are very
different situations: a Pi with internet but no working provider key can still
give a real, cited answer to a factual question.

Two entry points:

* :func:`wikipedia` - the lookup itself, returning structured data.
* :func:`wikipedia_lookup` - the tool JARVIS exposes (and can call itself).
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, Dict, List, Optional

from ai.http import HttpError, request_json, with_query
from config.settings import env_bool
from core.logging_setup import get_logger
from tools.base import ToolContext, ToolResult, tool

log = get_logger("tools.knowledge")

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_REST = "https://en.wikipedia.org/api/rest_v1"

#: Questions that a reference lookup can answer, and the words that are part
#: of the question rather than the thing being asked about.
_QUESTION_WORDS = (
    "who", "what", "when", "where", "which", "how", "why", "define", "definition",
    "meaning", "tell me about", "explain", "describe", "do you know", "wikipedia",
)
_LEADING = re.compile(
    r"^(?:can you |could you |please |hey |ok |okay |jarvis |tell me about |"
    r"explain |describe |define |what is |what are |what's |whats |who is |who was |"
    r"who's |who are |when is |when was |when did |where is |where was |where's |"
    r"which is |how does |how do |how much |how many |"
    r"give me |search for |look up |search |lookup |wikipedia )+",
    re.IGNORECASE,
)
_TRAILING = re.compile(r"[?.!]+\s*$")
_STRIP = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)
#: "25 * 4" is arithmetic, not a lookup - the offline engine owns those.
_MATH = re.compile(r"\d\s*(?:[-+*/x×÷]|\+\+)\s*\d|\d+\s*%", re.IGNORECASE)


def enabled() -> bool:
    """Reference lookups are on by default - they need no key.

    ``WIKIPEDIA_ENABLED=false`` keeps every lookup off the machine, which is
    what the test suite does so a run never touches the network.
    """
    return env_bool("WIKIPEDIA_ENABLED", True)


def looks_factual(question: str) -> bool:
    """Could a reference lookup answer this, rather than needing an opinion?"""
    text = (question or "").strip()
    if len(text) < 3 or len(text.split()) > 18:
        return False
    if _MATH.search(text):
        return False
    lowered = text.lower()
    return any(word in lowered for word in _QUESTION_WORDS)


def topic_from(question: str) -> str:
    """Pull the thing being asked about out of a question."""
    text = _TRAILING.sub("", (question or "").strip())
    previous = None
    while previous != text:  # "please tell me about who is x" -> "x"
        previous = text
        text = _LEADING.sub("", text).strip()
    text = _STRIP.sub("", text)
    return re.sub(r"\s+", " ", text).strip(" ,")


def _summary(title: str, timeout: float) -> Optional[Dict[str, Any]]:
    """The REST summary endpoint: one short, readable introduction."""
    url = f"{WIKI_REST}/page/summary/{urllib.parse.quote(str(title).replace(' ', '_'))}"
    try:
        data = request_json(url, timeout=timeout, retries=0)
    except HttpError as exc:
        if exc.status == 404:
            return None
        raise
    if not isinstance(data, dict) or data.get("type") == "disambiguation":
        return None
    extract = str(data.get("extract") or "").strip()
    if not extract:
        return None
    content_urls = data.get("content_urls") or {}
    return {
        "title": data.get("title") or title,
        "description": str(data.get("description") or "").strip(),
        "extract": extract,
        "url": (content_urls.get("desktop") or {}).get("page")
        or f"https://en.wikipedia.org/wiki/{urllib.parse.quote(str(title).replace(' ', '_'))}",
        "source": "wikipedia",
    }


def _search_title(query: str, timeout: float) -> Optional[str]:
    """Ask Wikipedia which article it would use for this phrasing."""
    url = with_query(WIKI_API, {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": 1,
        "format": "json",
        "origin": "*",
    })
    try:
        data = request_json(url, timeout=timeout, retries=0)
    except HttpError as exc:
        log.info("wikipedia search failed: %s", exc)
        return None
    if not isinstance(data, dict):
        return None
    hits = ((data.get("query") or {}).get("search") or [])
    if not hits:
        return None
    title = str(hits[0].get("title") or "").strip()
    return title or None


def wikipedia(topic: str, timeout: float = 8.0) -> Optional[Dict[str, Any]]:
    """Look ``topic`` up on Wikipedia.  Returns ``None`` when nothing fits.

    Tries the article title directly (fast, exact), then falls back to
    Wikipedia's own search so a whole question still finds its article.
    """
    topic = (topic or "").strip()
    if not topic:
        return None
    found = _summary(topic, timeout)
    if found:
        return found
    title = _search_title(topic, timeout)
    if title and title.lower() != topic.lower():
        found = _summary(title, timeout)
        if found:
            return found
    return None


def first_sentences(text: str, count: int = 2, limit: int = 320) -> str:
    """The opening of an article - the part written to define the subject."""
    parts = re.split(r"(?<=[.!?])\s+", " ".join((text or "").split()))
    out = ""
    for part in parts[:count]:
        if out and len(out) + len(part) + 1 > limit:
            break
        out = f"{out} {part}".strip()
    return out or " ".join((text or "").split())[:limit]


def format_answer(found: Dict[str, Any], sentences: int = 2) -> str:
    """A spoken-friendly answer with its source attached."""
    body = first_sentences(found.get("extract", ""), sentences)
    title = found.get("title") or "that"
    header = f"{title}"
    if found.get("description"):
        header = f"{title} - {found['description']}"
    return f"{header}. {body}\n\nSource: {found.get('url', '')}"


@tool(
    name="wikipedia_lookup",
    description=(
        "Look a topic up on Wikipedia and return a short, cited summary. "
        "Use it for facts, definitions, dates, people, places and science - "
        "anything a reference work can answer. Needs no API key."
    ),
    parameters={
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "the subject to look up"},
        },
        "required": ["topic"],
    },
    category="knowledge",
    aliases=("wiki", "wikipedia_search", "look_up"),
)
def wikipedia_lookup(topic: str, ctx: Optional[ToolContext] = None) -> ToolResult:
    subject = (topic or "").strip()
    if not subject:
        return ToolResult.failure("what should I look up?")
    timeout = 8.0
    settings = getattr(ctx, "settings", None)
    if settings is not None:
        timeout = float(getattr(getattr(settings, "search", None), "timeout", timeout) or timeout)
    try:
        found = wikipedia(subject, timeout=timeout)
    except HttpError as exc:
        log.info("wikipedia lookup failed for %r: %s", subject, exc)
        return ToolResult.failure(f"could not reach Wikipedia: {exc}")
    except Exception as exc:  # noqa: BLE001 - never take the turn down
        log.warning("wikipedia lookup failed for %r: %s", subject, exc)
        return ToolResult.failure(f"wikipedia lookup failed: {exc}")
    if not found:
        return ToolResult.failure(f"Wikipedia has no article matching '{subject}'")
    return ToolResult.success(format_answer(found), data=found)


def answer_without_a_provider(question: str, timeout: float = 8.0) -> Optional[str]:
    """Best keyless answer to ``question``, or ``None`` if it is not factual.

    This is the path JARVIS takes when every AI provider is unavailable but the
    machine still has internet - a real answer beats a refusal.
    """
    if not looks_factual(question):
        return None
    subject = topic_from(question)
    if not subject:
        return None
    found = wikipedia(subject, timeout=timeout)
    if not found:
        return None
    log.info("keyless answer for %r from %s", question, found.get("url"))
    return format_answer(found)


__all__: List[str] = [
    "answer_without_a_provider",
    "enabled",
    "first_sentences",
    "format_answer",
    "looks_factual",
    "topic_from",
    "wikipedia",
    "wikipedia_lookup",
]
