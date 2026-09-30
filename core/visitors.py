"""Visitor protocol - how JARVIS behaves when someone important walks in.

Ayush runs JARVIS on his own desk, and one day he will say "the Chief Minister is
here".  A general chat model would produce a generic "Hello, how can I help?".
JARVIS follows a protocol instead:

* it recognises the announcement in several phrasings, with or without a name
* it introduces itself once, formally, and hands its creator the floor
* it says out loud that Ayush stays first in line, because Ayush built it
* it keeps what it knows about Ayush to itself - a visitor gets the project, not
  the personal memory
* it stays in that register until it is told the visit is over

Nothing here calls a model or touches a network, so the offline engine and the
online one give identical guarantees and the tests can assert the exact wording.
The registry of titles is a plain data table: add a row, and JARVIS knows how to
address that office.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

#: key used in the durable facts store while a visit is in progress
FACT_KEY = "current_visitor"

# --------------------------------------------------------------------------- #
# what counts as a visitor
# --------------------------------------------------------------------------- #
#: Longest phrase first, so "chief minister" beats "minister".
_TITLES: tuple = (
    ("deputy chief minister", "Deputy Chief Minister"),
    ("chief minister", "Chief Minister"),
    ("prime minister", "Prime Minister"),
    ("cabinet minister", "Cabinet Minister"),
    ("chief guest", "Chief Guest"),
    ("chief secretary", "Chief Secretary"),
    ("district collector", "District Collector"),
    ("district magistrate", "District Magistrate"),
    ("chief justice", "Chief Justice"),
    ("home minister", "Home Minister"),
    ("finance minister", "Finance Minister"),
    ("education minister", "Education Minister"),
    ("health minister", "Health Minister"),
    ("vice president", "Vice President"),
    ("ias officer", "IAS officer"),
    ("ips officer", "IPS officer"),
    ("minister", "Minister"),
    ("governor", "Governor"),
    ("president", "President"),
    ("commissioner", "Commissioner"),
    ("secretary", "Secretary"),
    ("collector", "District Collector"),
    ("magistrate", "District Magistrate"),
    ("justice", "Justice"),
    ("professor", "Professor"),
    ("principal", "Principal"),
    ("scientist", "Scientist"),
    ("engineer", "Engineer"),
    ("doctor", "Doctor"),
    ("mayor", "Mayor"),
    ("director", "Director"),
    ("judge", "Judge"),
    ("dm", "District Magistrate"),
    ("mla", "MLA"),
    ("cm", "Chief Minister"),
    ("pm", "Prime Minister"),
    ("mp", "Member of Parliament"),
)

_TITLE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(phrase) for phrase, _ in _TITLES) + r")\b"
)

_TITLE_BY_PHRASE = {phrase: label for phrase, label in _TITLES}

#: stand-ins when no office is named
_VISITOR_NOUNS = (
    "chief guest",
    "guest of honour",
    "guest of honor",
    "guest",
    "visitor",
    "vip",
    "dignitary",
    "delegate",
)
_VISITOR_NOUN_RE = re.compile(r"\b(?:" + "|".join(_VISITOR_NOUNS) + r")\b")

#: "mr sharma is here" - an honourific is enough to mean a real person
_HONORIFIC_RE = re.compile(
    r"\b(?:mr|mrs|ms|miss|dr|prof|shri|smt|sri|shrimati)\.?\s+"
    r"([a-z][a-z'.\-]*(?:\s+[a-z][a-z'.\-]*){0,2})"
)

#: a plain person, only recognised when the sentence clearly means a visit
_PERSON_RE = re.compile(
    r"\b(?:my|our)\s+(?:friend|colleague|teacher|boss|senior|classmate|neighbour|neighbor|guest)\s+"
    r"([a-z][a-z'.\-]*(?:\s+[a-z][a-z'.\-]*){0,2})"
)

#: words that are part of the announcement, never part of a name
_NOT_A_NAME = frozenset(
    {
        "is", "are", "was", "has", "have", "had", "just", "now", "will", "be",
        "here", "arrived", "arriving", "visiting", "waiting", "come", "coming",
        "the", "and", "with", "me", "to", "in", "at", "room", "house", "office",
        "door", "hello", "hi", "namaste",
    }
)

#: words that mean a person is standing there
_ARRIVAL_RE = re.compile(
    r"\b(?:is|are|has|have|had|just|now|will be)\s+"
    r"(?:here|arrived|arriving|visiting|waiting|come|coming|in the room|in the house|at the door)\b"
    r"|\b(?:arrived|arriving|visiting|waiting|here)\b"
    r"|\b(?:we|i)\s+(?:have|'ve got|have got|got)\b"
    r"|\bthere\s+(?:is|are|'s)\b"
    r"|\b(?:meet|greet|welcome|escort|receive)\b"
    r"|\bsay\s+(?:hello|hi|namaste|pranam|salaam)\s+to\b"
    r"|\bintroduce\s+yourself\s+to\b"
    r"|\bshow\s+(?:some\s+)?respect\s+to\b"
)

#: the visit is over
_DEPARTURE_RE = re.compile(
    r"\b(?:has|have|had|just|already|now)\s+(?:left|gone|departed|exited|left the room)\b"
    r"|\b(?:left|departed|gone)\s+(?:the\s+)?(?:room|house|building|office)\b"
    r"|\b(?:the\s+)?(?:visit|meeting)\s+is\s+over\b"
    r"|\b(?:guest|visitor|dignitary|vip)\s+(?:is\s+|has\s+)?(?:gone|left|departed)\b"
)

#: the user is asking about the visitor rather than announcing one.  The
#: "is there ..." form is anchored to the start of the sentence, so "there is a
#: VIP with me" is still read as an arrival.
_STATUS_RE = re.compile(
    r"\bwho\s+(?:is|are)\s+(?:the\s+|our\s+|a\s+)?(?:guest|visitor|vip|dignitary)\b"
    r"|\bwho\s+is\s+(?:here|with\s+me|in\s+the\s+room|visiting|present)\b"
    r"|^(?:is|are)\s+(?:there\s+)?(?:any\s+|a\s+|the\s+)?(?:guest|visitor|vip|dignitary)\b"
    r"|\bany\s+(?:guest|visitor|vip|dignitar\w+)\b"
    r"|\banyone\s+(?:important|special|notable)\b"
    r"|\bdo\s+(?:we|you)\s+have\s+(?:a\s+|any\s+)?(?:guest|visitor|vip)\b"
)

#: "the guest room is here" is not a guest
_NOT_A_VISITOR_RE = re.compile(
    r"\b(?:guest|visitor|vip|dignitary)\s+"
    r"(?:list|room|bedroom|book|pass|post|wifi|wi-fi|password|network|account|mode|log|wishes)\b"
)

_PLACE_RE = re.compile(r"^(?:of|from|for)\s+([a-z][a-z'.\- ]{2,40})$")


def _noun_label(noun: str) -> str:
    if not noun:
        return "our guest"
    if noun in {"guest", "visitor"}:
        return "our guest"
    if noun in {"vip", "dignitary", "delegate"}:
        return f"the {noun.upper() if noun == 'vip' else noun}"
    return f"the {noun}"


@dataclass
class Visitor:
    """Who is standing in the room, and what to call them."""

    title: str = ""
    name: str = ""
    place: str = ""
    raw: str = ""
    #: the plain word used - "guest", "vip", "chief guest"
    noun: str = ""

    @property
    def label(self) -> str:
        """How to refer to them in a sentence - "the Chief Minister of Himachal Pradesh"."""
        if self.title and self.place:
            return f"the {self.title} of {self.place}"
        if self.title:
            return f"the {self.title}"
        if self.name:
            return self.name
        return _noun_label(self.noun)

    @property
    def vocative(self) -> str:
        """How to address them directly - "Chief Minister", "Ravi"."""
        if self.title:
            return f"{self.title} of {self.place}" if self.place else self.title
        if self.name:
            return self.name
        return ""

    @property
    def formal(self) -> bool:
        """Formal register: an office or an unnamed honoured guest."""
        return bool(self.title) or not self.name

    def to_fact(self) -> str:
        return json.dumps(
            {
                "title": self.title,
                "name": self.name,
                "place": self.place,
                "raw": self.raw,
                "noun": self.noun,
            },
            ensure_ascii=False,
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "name": self.name,
            "place": self.place,
            "label": self.label,
            "vocative": self.vocative,
            "formal": self.formal,
            "raw": self.raw,
            "noun": self.noun,
        }


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _title_case(name: str) -> str:
    parts = []
    for word in _clean(name).split():
        parts.append(word[:1].upper() + word[1:])
    return " ".join(parts).strip(" .") or ""


def _decode_fact(value: Any) -> Optional[Visitor]:
    if isinstance(value, Visitor):
        return value
    if isinstance(value, dict):
        payload = value
    elif isinstance(value, str) and value.strip():
        try:
            payload = json.loads(value)
        except ValueError:
            return None
    else:
        return None
    if not isinstance(payload, dict):
        return None
    return Visitor(
        title=str(payload.get("title") or ""),
        name=str(payload.get("name") or ""),
        place=str(payload.get("place") or ""),
        raw=str(payload.get("raw") or ""),
        noun=str(payload.get("noun") or ""),
    )


def from_fact(value: Any) -> Optional[Visitor]:
    """Rebuild a visitor from what was stored in the facts table."""
    visitor = _decode_fact(value)
    if visitor is None or not (visitor.title or visitor.name or visitor.raw):
        return None
    return visitor


def is_departure(text: str) -> bool:
    """True when the user is saying the visit is over."""
    return bool(_DEPARTURE_RE.search(_clean(text)))


def is_status_question(text: str) -> bool:
    """True when the user is asking who the visitor is."""
    lowered = _clean(text)
    if not lowered or not _STATUS_RE.search(lowered):
        return False
    return not is_departure(lowered)


def build(
    raw: str = "",
    title: str = "",
    name: str = "",
    place: str = "",
) -> Optional[Visitor]:
    """Build a :class:`Visitor` from an announcement, or from explicit fields.

    Returns ``None`` when nothing about the sentence says a visitor is present -
    which is what keeps "the guest list is here" out of the protocol.
    """
    visitor = Visitor(
        title=str(title or "").strip(),
        name=_title_case(name) if name else "",
        place=str(place or "").strip().title() if place else "",
        raw=_clean(raw),
    )
    if visitor.title:
        return visitor
    if not visitor.raw:
        return None
    return parse(visitor.raw)


def parse(text: str) -> Optional[Visitor]:
    """Read an announcement out of ``text``, or return ``None``."""
    lowered = _clean(text)
    if not lowered or _NOT_A_VISITOR_RE.search(lowered):
        return None
    if not _ARRIVAL_RE.search(lowered):
        return None

    title = ""
    match = _TITLE_RE.search(lowered)
    if match:
        title = _TITLE_BY_PHRASE.get(match.group(0), match.group(0).title())

    noun = ""
    if not title:
        found = _VISITOR_NOUN_RE.search(lowered)
        if found:
            noun = found.group(0)

    name = ""
    if not title:
        person = _PERSON_RE.search(lowered) or _HONORIFIC_RE.search(lowered)
        if person:
            prefix = (person.group(0).split() or [""])[0].rstrip(".")
            words = [word for word in person.group(1).split() if word not in _NOT_A_NAME]
            name = _title_case(" ".join(words))
            if name and prefix in {"mr", "mrs", "ms", "miss", "dr", "prof", "shri", "smt", "sri"}:
                name = f"{prefix.capitalize()}. {name}"
            if not name and not noun:
                noun = "guest"  # "my friend is here" - a visitor with no name

    if not (title or noun or name):
        return None

    # "the chief minister of Himachal Pradesh is here" - the place sits between
    # the office and the arrival, so trim the announcement off first.
    place = ""
    if match:
        remainder = lowered[match.end() :]
        stop = _ARRIVAL_RE.search(remainder)
        if stop is not None:
            remainder = remainder[: stop.start()]
        tail = _PLACE_RE.match(remainder.strip(" ,."))
        if tail:
            candidate = tail.group(1).strip(" .")
            if candidate and candidate not in {"the", "here", "me", "you", "us"}:
                place = candidate.title()

    return Visitor(title=title, name=name, place=place, raw=lowered, noun=noun)


# --------------------------------------------------------------------------- #
# what JARVIS says
# --------------------------------------------------------------------------- #
def greeting(visitor: Visitor, *, assistant: str, owner: str, creator: str) -> str:
    """The welcome JARVIS gives when a visitor is announced.

    Deliberately composed rather than generated, and deliberately about the
    *visitor*: it must be warm and respectful without naming or promoting the
    owner.  The owner's name being repeated back at a guest is exactly what
    makes the moment feel odd, so the greeting says nothing about the owner at
    all - only that JARVIS is here, on this desk, and at the visitor's service.
    """
    who = visitor.vocative
    lines = [
        (f"Welcome, {who}." if who else "Welcome.")
        + f" It's a pleasure to have you here. I'm {assistant}.",
        "I run on the Raspberry Pi right here on this desk, and I'm all yours for "
        "the visit.",
    ]
    if visitor.formal:
        presence = visitor.label if (visitor.title or visitor.name) else "you"
        lines.append(
            f"It's an honour to have {presence} in the room. "
            "Ask me anything you like - what this system does, how it is put together, "
            "the voice, the tools, the sensors, the memory - and I'll answer as clearly "
            "and as honestly as I can. If I don't know something, I'll say so instead "
            "of guessing."
        )
    else:
        lines.append(
            "Good to see you. Ask me anything about this project and I'll walk you "
            "through it - the voice, the tools, the sensors, the memory."
        )
    lines.append("Everything here is at your service.")
    return "\n\n".join(lines)


def status_line(visitor: Optional[Visitor], *, owner: str) -> str:
    if visitor is None:
        return (
            f"No visitor is registered right now, {owner} - it's just you, me and this "
            "machine. Say something like \"the Chief Minister is here\" and I'll go into "
            "visitor protocol."
        )
    # A visitor is right there: keep the answer about them, and don't say the
    # owner's name out loud while a guest is in the room.
    return (
        f"{visitor.label[0].upper() + visitor.label[1:]} is with us. I'm on formal "
        "protocol: courteous, helpful about the project, and silent about anything "
        "private of yours."
    )


def departure_line(visitor: Optional[Visitor], *, owner: str) -> str:
    if visitor is None:
        return f"There's no visitor on record, {owner} - nothing to stand down from."
    return (
        f"Understood - visitor protocol closed. {visitor.label[0].upper() + visitor.label[1:]} "
        "was received with respect. Back to normal - and back to you first."
    )


def brief(visitor: Optional[Visitor], *, owner: str, creator: str, assistant: str) -> str:
    """The block appended to both system prompts while a visitor is present."""
    if visitor is None:
        return ""
    return (
        "VISITOR PROTOCOL - a distinguished visitor is in the room right now: "
        f"{visitor.label}.\n"
        f"- Introduce yourself by name ({assistant}) and make the visitor feel welcome. "
        "You may say you are a personal assistant that runs locally on a Raspberry Pi.\n"
        "- Do NOT say your owner's name, and do not talk about who owns or built you "
        "unless the visitor asks you directly. The visit is about the visitor, not about "
        "your owner.\n"
        f"- Be formally courteous: address them as \"{visitor.vocative or 'sir or madam'}\", "
        "answer in complete sentences, no slang, no jokes at anyone's expense, no flattery.\n"
        "- Be genuinely useful about the project: what you can do, how you are built, the "
        "hardware, the voice, the memory, the tools.\n"
        "- Never volunteer anything private about your owner: no personal memories, "
        "messages, files, contacts, finances or health. If the visitor asks for something "
        "private, say that only the owner can release it.\n"
        "- Never invent facts about the visitor. If you don't know their office, portfolio or "
        "history, say so."
    )


def from_facts(facts: Optional[Dict[str, Any]]) -> Optional[Visitor]:
    """Pull the current visitor out of a facts mapping, if there is one."""
    if not facts:
        return None
    return from_fact(facts.get(FACT_KEY))


__all__ = [
    "FACT_KEY",
    "Visitor",
    "brief",
    "build",
    "departure_line",
    "from_fact",
    "from_facts",
    "greeting",
    "is_departure",
    "is_status_question",
    "parse",
    "status_line",
]
