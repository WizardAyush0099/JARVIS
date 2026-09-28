"""Visitor protocol tools.

Ayush says "the Chief Minister of Himachal Pradesh is here" and JARVIS should
not answer like a chatbot.  These three tools give that moment a real,
deterministic behaviour that works with no internet and no API key:

* :func:`announce_visitor` - register the visitor and produce the respectful
  self-introduction (grateful to the creator, creator still first in line)
* :func:`visitor_status` - say who, if anyone, is currently with us
* :func:`visitor_departure` - close the protocol when the visit is over

The visitor is stored as an ordinary fact, which means the planner, the final
answer prompt and the console all see it without any new plumbing - and it
survives a restart.
"""

from __future__ import annotations

from typing import Optional, Tuple

from core import visitors
from tools.base import ToolContext, ToolResult, tool


def _identity(ctx: Optional[ToolContext]) -> Tuple[str, str, str]:
    settings = getattr(ctx, "settings", None)
    assistant = getattr(settings, "assistant_name", None) or "JARVIS"
    owner = getattr(settings, "owner_name", None) or "Ayush"
    creator = getattr(settings, "creator_name", None) or owner
    return assistant, owner, creator


def _remember(ctx: Optional[ToolContext], visitor: Optional[visitors.Visitor]) -> None:
    memory = getattr(ctx, "memory", None)
    if memory is None:
        return
    try:
        if visitor is None:
            memory.forget(visitors.FACT_KEY)
        else:
            memory.remember(visitors.FACT_KEY, visitor.to_fact())
    except Exception:  # pragma: no cover - memory problems never break a greeting
        pass


def _current(ctx: Optional[ToolContext]) -> Optional[visitors.Visitor]:
    memory = getattr(ctx, "memory", None)
    if memory is None:
        return None
    try:
        return visitors.from_facts(memory.facts())
    except Exception:  # pragma: no cover - defensive
        return None


@tool(
    name="announce_visitor",
    description=(
        "A distinguished visitor has arrived: register them and greet them "
        "formally, introducing JARVIS and its creator."
    ),
    parameters={
        "type": "object",
        "properties": {
            "guest": {
                "type": "string",
                "description": "what the user said, e.g. 'the chief minister of himachal pradesh is here'",
            },
            "title": {"type": "string", "description": "office, e.g. 'Chief Minister'"},
            "name": {"type": "string", "description": "the visitor's name, if it was given"},
            "place": {"type": "string", "description": "their region, e.g. 'Himachal Pradesh'"},
        },
    },
    category="identity",
    offline_safe=True,
    aliases=("greet_visitor", "visitor_protocol", "welcome_guest", "honour_guest"),
)
def announce_visitor(
    guest: str = "",
    title: str = "",
    name: str = "",
    place: str = "",
    ctx: Optional[ToolContext] = None,
) -> ToolResult:
    visitor = visitors.build(raw=guest, title=title, name=name, place=place)
    if visitor is None:
        return ToolResult.failure(
            "I couldn't tell who the visitor is - tell me like this: "
            "\"the Chief Minister of Himachal Pradesh is here\"."
        )
    assistant, owner, creator = _identity(ctx)
    _remember(ctx, visitor)
    if ctx is not None:
        ctx.notify("visitor", f"{visitor.label} is here", visitor=visitor.as_dict())
    return ToolResult.success(
        visitors.greeting(visitor, assistant=assistant, owner=owner, creator=creator),
        data=visitor.as_dict(),
    )


@tool(
    name="visitor_status",
    description="Say who, if anyone, is currently visiting and on visitor protocol.",
    parameters={"type": "object", "properties": {}},
    category="identity",
    offline_safe=True,
    aliases=("who_is_visiting", "guest_status"),
)
def visitor_status(ctx: Optional[ToolContext] = None) -> ToolResult:
    _, owner, _ = _identity(ctx)
    visitor = _current(ctx)
    return ToolResult.success(
        visitors.status_line(visitor, owner=owner),
        data=visitor.as_dict() if visitor else {},
    )


@tool(
    name="visitor_departure",
    description="The visit is over: close visitor protocol and return to normal.",
    parameters={"type": "object", "properties": {}},
    category="identity",
    offline_safe=True,
    aliases=("guest_left", "close_visitor_protocol"),
)
def visitor_departure(ctx: Optional[ToolContext] = None) -> ToolResult:
    _, owner, _ = _identity(ctx)
    visitor = _current(ctx)
    _remember(ctx, None)
    if ctx is not None:
        ctx.notify("visitor", "visitor protocol closed", visitor=None)
    return ToolResult.success(visitors.departure_line(visitor, owner=owner))


__all__ = ["announce_visitor", "visitor_departure", "visitor_status"]
