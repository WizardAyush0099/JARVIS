"""Deterministic intent layer - the engineered successor to "FirstLayerDMM".

The old project decided *everything* with keyword matching, which is why it felt
like a chatbot.  Here the same idea survives, but demoted to what it is actually
good at:

1. **instant answers** for unambiguous requests (time, maths, system status) that
   would otherwise burn an API call and a second of latency;
2. **offline capability** so JARVIS still works with no internet and no key;
3. **a safety net** when every AI provider is down.

Anything genuinely needing understanding (explaining, writing, reasoning,
follow-ups) simply does not match, and the planner hands it to the real model.
Every rule is a small pure function, so it is directly unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #
@dataclass
class Intent:
    name: str
    tool: Optional[str] = None
    args: Dict[str, Any] = field(default_factory=dict)
    category: str = "general"
    confidence: float = 0.9
    direct_reply: Optional[str] = None
    reason: str = ""

    @property
    def is_direct(self) -> bool:
        return self.direct_reply is not None


# --------------------------------------------------------------------------- #
# normalisation helpers
# --------------------------------------------------------------------------- #
_TRAILING = "?!.,;: \t\n"


def normalize(text: str, assistant: str = "jarvis") -> str:
    """Lower-case, collapse whitespace and strip a leading wake word."""
    query = (text or "").strip().replace("\u2019", "'")
    assistant = (assistant or "jarvis").strip()
    if assistant:
        name = re.escape(assistant)
        query = re.sub(
            rf"^(?:hey|hi|hello|ok|okay|yo|o)\s+{name}\b[\s,:!.\-]*", "", query, flags=re.I
        )
        query = re.sub(rf"^{name}\b[\s,:!.\-]*", "", query, flags=re.I)
    query = re.sub(r"\s+", " ", query).strip()
    return query.lower().strip(_TRAILING)


_MATH_CHARS = re.compile(r"[^0-9+\-*/().%\s]")


def extract_expression(text: str) -> Optional[str]:
    """Pull a safe arithmetic expression out of a phrase, or ``None``.

    Replaces the old project's ``eval()`` jugaad: this only ever produces
    characters that the sandboxed evaluator in ``tools.utilities`` accepts.
    """
    expr = text or ""
    expr = re.sub(r"(\d[\d.,]*)\s*(?:%|percent)\s*of\s*([\d.,]+)", r"(\1/100)*\2", expr, flags=re.I)
    expr = re.sub(r"(\d[\d.,]*)\s*(?:%|percent)", r"(\1/100)", expr, flags=re.I)
    expr = expr.replace("\u00d7", "*").replace("\u00f7", "/").replace("\u2212", "-")
    expr = re.sub(r"(?<=\d)\s*[x\u2715]\s*(?=\d)", "*", expr)
    expr = expr.replace("^", "**")
    expr = _MATH_CHARS.sub(" ", expr)
    expr = re.sub(r"\s+", " ", expr).strip()
    if not expr or not re.search(r"\d", expr):
        return None
    if not re.search(r"[-+*/%]|\*\*", expr):
        return None
    if expr.count("(") != expr.count(")"):
        return None
    if len(expr) > 200:
        return None
    return expr


def _first_group(pattern: str, text: str, flags: int = re.I) -> Optional[str]:
    match = re.search(pattern, text, flags)
    if not match:
        return None
    for group in match.groups():
        if group:
            return group.strip().strip(_TRAILING)
    return ""


# --------------------------------------------------------------------------- #
# rules
# --------------------------------------------------------------------------- #
def _rule_greeting(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    if re.fullmatch(
        r"(hi|hello|hey|yo|good (morning|afternoon|evening)|namaste|hola|are you there|you up)\b.*",
        text,
    ):
        return Intent(
            "greeting",
            category="identity",
            direct_reply=f"Systems online. What do you need, {ctx['owner']}?",
            reason="greeting",
        )
    return None


def _rule_identity(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    owner = ctx["owner"]
    assistant = ctx["assistant"]
    if re.search(r"\b(who am i|what(?:'s| is) my name|do you know me)\b", text):
        return Intent("identity_user", category="identity", direct_reply=f"You are {owner}.", reason="identity")
    if re.search(r"\b(who (?:is|'s) your (?:owner|user|boss|master)|who (?:do you|d'?you) (?:belong to|work for)|who owns you)\b", text):
        return Intent(
            "identity_owner",
            category="identity",
            direct_reply=f"You're {owner}, my primary user.",
            reason="identity",
        )
    if re.search(r"\b(who are you|what(?:'s| is) your name|introduce yourself|what are you)\b", text):
        return Intent(
            "identity_self",
            category="identity",
            direct_reply=(
                f"I'm {assistant}, your personal assistant. I run locally on your Raspberry Pi, "
                "can use tools, search the web, manage files and talk to you."
            ),
            reason="identity",
        )
    return None


def _rule_capabilities(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    if re.search(r"\b(what can you do|your (?:capabilities|skills|features)|list commands|help me with|what are your commands)\b", text):
        return Intent(
            "capabilities",
            category="identity",
            direct_reply=(
                "I can check and control the system (CPU, RAM, disk, temperature, apps), search the web and "
                "summarise pages, generate images, read and write files, take notes and reminders, switch and "
                "read your GPIO devices, send email with your confirmation, do maths and conversions, "
                "remember things about you, and explain, write or debug code."
            ),
            reason="capabilities",
        )
    if text in {"help", "commands", "features"}:
        return Intent(
            "capabilities",
            category="identity",
            direct_reply="Ask me anything - try 'system status', 'search for Raspberry Pi 5 news', or 'remember my project is called X'.",
            reason="capabilities",
        )
    return None


def _rule_time(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    if re.search(r"\b(what(?:'s| is)? the time|time is it|current time|tell me the time|time now)\b", text) or text in {
        "time",
        "what time",
    }:
        return Intent("time", tool="current_time", category="utilities", reason="time")
    return None


def _rule_date(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    if re.search(r"\b(what(?:'s| is)? (?:the )?date|today'?s date|what day is it|which day is it)\b", text) or text in {
        "date",
        "today",
        "day",
    }:
        return Intent("date", tool="current_datetime", category="utilities", reason="date")
    return None


def _rule_calculator(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    trigger = re.match(
        r"^(?:please\s+)?(?:can you\s+)?(?:calculate|compute|solve|evaluate|work out|what(?:'s| is)|how much is|how many is)\s+(.+)$",
        text,
    )
    candidate = trigger.group(1) if trigger else text
    if not trigger and not re.fullmatch(r"[\d\s+\-*/().%^x\u00d7\u00f7]+", text):
        return None
    expression = extract_expression(candidate)
    if not expression:
        return None
    return Intent(
        "calculate", tool="calculate", args={"expression": expression}, category="utilities", reason="maths"
    )


def _rule_convert(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    match = re.search(
        r"\bconvert\s+(?:me\s+)?(.+?)\s+(?:in ?to|to|into)\s+([a-z\u00b0/ ]+)$", text
    )
    if not match:
        return None
    return Intent(
        "convert_units",
        tool="convert_units",
        args={"quantity": match.group(1).strip(), "target_unit": match.group(2).strip()},
        category="utilities",
        reason="conversion",
    )


def _rule_top_processes(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    if re.search(r"\b(?:top|heaviest|biggest)\b.*\b(?:process|cpu|ram|memory)\b", text) or re.search(
        r"what(?:'s| is)? using (?:the )?most (?:ram|memory|cpu)", text
    ):
        return Intent("top_processes", tool="top_processes", category="system", reason="processes")
    return None


#: ...and words that mean the Pi itself, which ``_rule_system_status`` owns
_SYSTEM_SELF = r"\b(cpu|core|processor|system|gpu|soc|pi|raspberry|thermal|machine|computer)\b"
#: things the OS owns rather than something wired to the GPIO header
_NOT_A_DEVICE = (
    r"\b(wifi|wi-fi|wireless|internet|network|bluetooth|hotspot|airplane|volume|sound|"
    r"audio|music|microphone|speaker|screen|display|brightness|notifications?|do not disturb)\b"
)
#: a locator that only ever appears when a wired sensor is meant
_DEVICE_LOCATORS = r"\b(sensor|room|indoor|ambient|dht|ds18b20|ldr|photo ?resistor|light level|climate)\b"
#: the nouns people use for the kinds ``hardware/gpio.py`` can actually drive
_DEVICE_KINDS = r"(?:led|light|button|relay|buzzer|servo|distance|motion|temperature|temp|sensor)"
_DEVICE_FILLER = frozenset(
    {"the", "my", "our", "a", "an", "is", "are", "of", "on", "off", "value", "reading", "state", "status", "level", "current"}
)


def _looks_like_device(target: str) -> bool:
    """True when a phrase names something wired up, not a random noun.

    Keeps "read room temperature" on the GPIO path while "what's the distance to
    the moon" goes to the model, where it belongs.
    """
    if not target:
        return False
    if re.search(_DEVICE_LOCATORS, target):
        return True
    if "_" in target:  # a declared-style id such as status_led
        return True
    words = [word for word in re.split(r"[^a-z0-9]+", target) if word]
    if not words or len(words) > 3:
        return False
    kinds = sum(1 for word in words if re.fullmatch(_DEVICE_KINDS, word))
    return kinds > 0 and all(
        word in _DEVICE_FILLER or re.fullmatch(_DEVICE_KINDS, word) for word in words
    )


def _is_plain_device(target: str) -> bool:
    return bool(target) and not re.search(_SYSTEM_SELF, target) and not re.search(_NOT_A_DEVICE, target)


#: verbs that can open a request to list the declared devices
_LIST_VERBS = frozenset({"list", "show", "what", "which", "tell", "status", "print", "display"})
#: device words, and the only words allowed between the verb and the device word
_LIST_NOUNS = re.compile(r"(?:devices?|hardware|gpio|sensors?|actuators?|pins?)")
_LIST_FILLER = frozenset(
    {"me", "my", "our", "the", "a", "an", "all", "of", "are", "is", "do", "does", "you", "your", "have", "has", "and", "about", "connected", "available", "declared"}
)


def _asks_for_devices(text: str) -> bool:
    """True for "list my hardware devices", false for "show me the news about sensors".

    A bare keyword check is not enough here: the device word has to come *before*
    anything unrelated, otherwise ordinary sentences that merely mention a sensor
    would be answered with the GPIO listing instead of the model.
    """
    words = [word for word in re.split(r"[^a-z0-9]+", text) if word]
    if not words:
        return False
    if words[0] in {"gpio", "hardware"}:
        return any(_LIST_NOUNS.fullmatch(word) or word in {"status", "list", "info"} for word in words[1:])
    if words[0] not in _LIST_VERBS:
        return False
    for word in words[1:]:
        if _LIST_NOUNS.fullmatch(word):
            return True
        if word not in _LIST_FILLER:
            return False
    return False


def _rule_hardware(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    """GPIO hardware: a small, fixed command set, so it stays deterministic.

    These run before :func:`_rule_system_status` so that "read room temperature"
    reaches the sensor, while "cpu temperature" still reaches the Pi's own
    thermal zone (``_SYSTEM_SELF`` guards the difference).
    """
    if _asks_for_devices(text):
        return Intent("hardware_list", tool="hardware_list", category="hardware", reason="devices")

    flash = re.match(r"^(?:please\s+)?(?:flash|blink|pulse|flicker)\s+(?:the\s+|my\s+)?(.+)$", text)
    if flash:
        target = flash.group(1).strip(_TRAILING)
        if _is_plain_device(target):
            return Intent(
                "hardware_pulse",
                tool="hardware_pulse",
                args={"device": target},
                category="hardware",
                reason="device pulse",
            )

    # "turn the status led on" and "turn on the status led" are both natural
    switch = re.match(r"^(?:please\s+)?(?:turn|switch|put)\s+(.+?)\s+(on|off)$", text)
    target = value = ""
    if switch is not None:
        target, value = switch.group(1), switch.group(2)
    else:
        flipped = re.match(r"^(?:please\s+)?(?:turn|switch)\s+(on|off)\s+(.+)$", text)
        if flipped is not None:
            value, target = flipped.group(1), flipped.group(2)
    if target:
        target = re.sub(r"^(?:the|my|a|an)\s+", "", target.strip(_TRAILING))
        if _is_plain_device(target):
            return Intent(
                "hardware_write",
                tool="hardware_write",
                args={"device": target, "value": value},
                category="hardware",
                confidence=0.8,
                reason="device state",
            )

    # "set the servo to 90" - the value must be a level, so "set a reminder to
    # call mum" is left to the reminder rule that follows.
    level = re.match(
        r"^(?:please\s+)?set\s+(?:the\s+|my\s+)?(.+?)\s+to\s+(\d{1,3}|on|off)$", text
    )
    if level is not None and _is_plain_device(level.group(1).strip(_TRAILING)):
        return Intent(
            "hardware_write",
            tool="hardware_write",
            args={"device": level.group(1).strip(_TRAILING), "value": level.group(2)},
            category="hardware",
            confidence=0.8,
            reason="device level",
        )

    press = re.match(
        r"^(?:is|are|has)\s+(?:the\s+|my\s+)?(.+?)\s+(?:pressed|pressing|triggered|detected|on|high|active)$",
        text,
    )
    if press is not None and _is_plain_device(press.group(1).strip(_TRAILING)):
        return Intent(
            "hardware_read",
            tool="hardware_read",
            args={"device": press.group(1).strip(_TRAILING)},
            category="hardware",
            confidence=0.8,
            reason="device state",
        )

    read = re.match(
        r"^(?:please\s+)?(?:read|check|measure|what(?:'s| is)|tell me|show me|report)\s+"
        r"(?:the\s+|my\s+)?(.+)$",
        text,
    )
    if read:
        target = read.group(1).strip(_TRAILING)
        if _looks_like_device(target) and _is_plain_device(target):
            return Intent(
                "hardware_read",
                tool="hardware_read",
                args={"device": target},
                category="hardware",
                confidence=0.8,
                reason="sensor",
            )
    return None


def _rule_system_status(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    if re.search(r"\b(system (?:status|info|information|health)|how are you doing|diagnostics)\b", text):
        return Intent("system_status", tool="system_status", category="system", reason="system")
    # most specific first: "cpu temperature" must not report CPU load
    checks = {
        "temperature": r"\b(temperature|cpu temp)\b|\bhow hot\b|\bthermal (?:zone|throttl|status)\b",
        "battery": r"\b(battery|power level|charging)\b",
        "network": r"\b(network|wi-?fi|internet connection|ip address|connection status)\b",
        "disk": r"\b(disk|storage|drive space|free space|hard drive)\b",
        "memory": r"\b(ram|memory)\b",
        "cpu": r"\b(cpu|processor)\b",
    }
    for metric, pattern in checks.items():
        if re.search(pattern, text):
            return Intent(
                f"system_{metric}",
                tool="system_status",
                args={"metric": metric},
                category="system",
                reason=f"{metric} status",
            )
    return None


def _rule_open_url(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    match = re.search(
        r"\b(?:open|launch|go to|visit|browse)\s+(?:the\s+)?(?:website\s+|site\s+|page\s+|link\s+)?"
        r"((?:https?://)?[\w.-]+\.[a-z]{2,}(?:/\S*)?)",
        text,
    )
    if not match:
        return None
    url = match.group(1)
    if "." not in url:
        return None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return Intent("open_url", tool="open_url", args={"url": url}, category="system", reason="url")


def _rule_open_app(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    match = re.match(r"^(?:please\s+)?(?:open|launch|start)\s+(?:the\s+)?(?:app\s+|application\s+)?(.{2,40})$", text)
    if not match:
        return None
    target = match.group(1).strip(_TRAILING)
    if any(word in target for word in ("http", "www.", ".com", "youtube", "search", "browser tab")):
        return None
    if target in {"it", "that", "this", "them"}:
        return None
    return Intent(
        "open_app", tool="open_app", args={"app": target}, category="system", confidence=0.75, reason="app"
    )


def _rule_youtube(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    if "youtube" not in text:
        return None
    query = _first_group(r"(?:for|about|of)\s+(.+)$", text) or ""
    if not query:
        query = re.sub(r"\b(?:open|launch|go to|search|find|on|youtube|play|watch)\b", " ", text)
        query = re.sub(r"\s+", " ", query).strip(_TRAILING)
    return Intent("youtube", tool="youtube_search", args={"query": query}, category="web", reason="youtube")


def _rule_web_search(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    match = re.match(
        r"^(?:please\s+)?(?:can you\s+)?(?:search(?: the web)?(?: for)?|google|look up|look for|find out about|"
        r"tell me about|what(?:'s| is) the latest (?:on|about)|latest news (?:on|about))\s+(.+)$",
        text,
    )
    if not match:
        return None
    query = match.group(1).strip(_TRAILING)
    if len(query) < 2 or query in {"it", "that", "this"}:
        return None
    # web_research searches *and* summarises the pages it reads, falling back to
    # a plain result list when no model is available.
    return Intent("web_search", tool="web_research", args={"query": query}, category="web", reason="search")


def _rule_news(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    match = re.search(r"\b(?:today'?s|latest|current)\s+news(?:\s+(?:on|about)\s+(.+))?", text)
    if not match:
        return None
    return Intent(
        "news",
        tool="web_research",
        args={"query": f"{match.group(1).strip()} news".strip() if match.group(1) else "today's top news"},
        category="web",
        reason="news",
    )


def _rule_image(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    match = re.match(
        r"^(?:please\s+)?(?:can you\s+)?(?:generate|create|make|draw|design|paint|render)\s+"
        r"(?:me\s+)?(?:an?\s+)?(?:image|picture|illustration|wallpaper|artwork|art|photo|drawing|logo)"
        r"(?:\s+(?:of|showing|with|for))?\s*(.*)$",
        text,
    )
    if not match:
        return None
    prompt = match.group(1).strip(_TRAILING)
    if not prompt:
        return None
    return Intent(
        "generate_image", tool="generate_image", args={"prompt": prompt}, category="image", reason="image"
    )


def _rule_email(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    if not re.search(r"\b(e-?mail)\b", text):
        return None
    if not re.search(r"\b(send|write|draft|compose|email)\b", text):
        return None
    to = _first_group(r"\bto\s+([\w.+-]+@[\w.-]+\.\w+|\+?\d{10,})", text) or ""
    body = _first_group(r"\b(?:saying|that says|with the message|body)\b[:\s]+(.+)$", text) or ""
    if not body:
        body = _first_group(r"\babout\b\s+(.+)$", text) or ""
    subject = _first_group(r"\b(?:subject|titled|about)\b[:\s]+(.+?)(?:\s+saying\b|$)", text) or ""
    return Intent(
        "email",
        tool="compose_email",
        args={"to": to, "subject": subject, "body": body},
        category="email",
        confidence=0.8,
        reason="email",
    )


def _rule_create_file(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    match = re.match(
        r"^(?:please\s+)?create\s+(?:a\s+)?(?:new\s+)?(?:text\s+|json\s+|python\s+)?file\s+"
        r"(?:called\s+|named\s+)?([\w./~-]+)(?:\s+(?:with|containing|that says|saying)\s+(.+))?$",
        text,
    )
    if not match:
        return None
    return Intent(
        "create_file",
        tool="write_text_file",
        args={"path": match.group(1), "content": (match.group(2) or "").strip()},
        category="files",
        reason="file",
    )


def _rule_read_file(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    match = re.match(
        r"^(?:please\s+)?(?:read|show|display|cat|open)\s+(?:me\s+)?(?:the\s+)?(?:file\s+|contents of\s+)?([\w./~-]+\.\w{1,5})$",
        text,
    )
    if not match:
        return None
    return Intent(
        "read_file", tool="read_text_file", args={"path": match.group(1)}, category="files", reason="file"
    )


def _rule_find_file(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    match = re.match(
        r"^(?:please\s+)?(?:find|locate|search for)\s+(?:my\s+|the\s+)?(.+?)(?:\s+files?|\s+project)?$",
        text,
    )
    if not match:
        return None
    needle = match.group(1).strip(_TRAILING)
    if not needle or needle in {"it", "that"}:
        return None
    return Intent(
        "find_files", tool="search_files", args={"query": needle}, category="files", reason="file"
    )


def _rule_notes(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    if re.search(r"\b(show|list|read|what are)\b.*\bnotes?\b", text):
        return Intent("list_notes", tool="list_notes", category="utilities", reason="notes")
    match = re.match(
        r"^(?:please\s+)?(?:add|take|make|write|save)\s+(?:a\s+)?note\s*:?\s*(?:that|saying|says)?\s*(.*)$",
        text,
    )
    if match and match.group(1).strip():
        return Intent(
            "add_note", tool="add_note", args={"text": match.group(1).strip()}, category="utilities", reason="notes"
        )
    return None


def _rule_weather(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    match = re.match(r"^(?:what(?:'s| is)? the )?weather(?:\s+(?:like\s+)?(?:in|for|at)\s+(.+))?$", text)
    if not match and "weather" not in text:
        return None
    if not match:
        match = re.search(r"weather\s+(?:in|for|at)\s+(.+)$", text)
    place = (match.group(1).strip(_TRAILING) if match and match.groups() else "") or ""
    return Intent(
        "weather",
        tool="weather",
        args={"location": place},
        category="web",
        reason="weather",
    )


def _rule_reminders(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    if re.search(r"\b(show|list|what are)\b.*\b(reminders?|timers?|alarms?)\b", text):
        return Intent("list_reminders", tool="list_reminders", category="utilities", reason="reminders")
    match = re.match(
        r"^(?:please\s+)?(?:remind me to|remind me|set a reminder to|set a timer for|timer for)\s+(.+)$", text
    )
    if match:
        return Intent(
            "set_reminder",
            tool="set_reminder",
            args={"text": match.group(1).strip()},
            category="utilities",
            reason="reminders",
        )
    return None


def _rule_memory_write(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    match = re.match(
        r"^(?:please\s+)?remember\s+(?:that\s+)?(?:my\s+)?(.+?)\s+(?:is|are|=|called|named)\s+(.+)$", text
    )
    if match:
        value = re.sub(r"^(?:called|named|:)\s*", "", match.group(2).strip(_TRAILING))
        return Intent(
            "remember",
            tool="remember_fact",
            args={"key": match.group(1).strip(), "value": value},
            category="memory",
            reason="memory",
        )
    match = re.match(r"^(?:please\s+)?remember\s+(?:that\s+)?(.+)$", text)
    if match:
        statement = match.group(1).strip(_TRAILING)
        return Intent(
            "remember",
            tool="remember_fact",
            args={"key": "note", "value": statement},
            category="memory",
            reason="memory",
        )
    return None


def _rule_memory_read(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    match = re.match(
        r"^(?:do you remember|what(?:'s| is| was) my|what did i (?:say|tell you) (?:my|about)|remind me what my)\s+(.+)$",
        text,
    )
    if not match:
        return None
    return Intent(
        "recall",
        tool="recall_fact",
        args={"query": match.group(1).strip(_TRAILING)},
        category="memory",
        reason="memory",
    )


def _rule_clear_chat(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    if re.search(r"\b(clear|reset|wipe|forget|delete)\b.*\b(chat|conversation|history|context)\b", text):
        return Intent(
            "clear_conversation",
            tool="clear_conversation",
            category="memory",
            reason="conversation reset",
        )
    return None


def _rule_power(text: str, ctx: Dict[str, Any]) -> Optional[Intent]:
    match = re.search(r"\b(?:shut ?down|power ?off|turn off the (?:pi|system)|reboot|restart the (?:pi|system|computer))\b", text)
    if not match:
        return None
    action = "restart" if re.search(r"re ?boot|restart", match.group(0)) else "shutdown"
    return Intent(
        "power", tool="system_power", args={"action": action}, category="power", confidence=0.95, reason="power"
    )


RULE_FUNCTIONS: Sequence[Callable[[str, Dict[str, Any]], Optional[Intent]]] = (
    _rule_greeting,
    _rule_identity,
    _rule_capabilities,
    _rule_time,
    _rule_date,
    _rule_convert,
    _rule_calculator,
    _rule_top_processes,
    _rule_hardware,
    _rule_system_status,
    _rule_open_url,
    _rule_youtube,
    _rule_open_app,
    _rule_web_search,
    _rule_news,
    _rule_weather,
    _rule_image,
    _rule_email,
    _rule_create_file,
    _rule_read_file,
    _rule_find_file,
    _rule_notes,
    _rule_reminders,
    _rule_memory_write,
    _rule_memory_read,
    _rule_clear_chat,
    _rule_power,
)


def match(
    text: str,
    owner: str = "Ayush",
    assistant: str = "JARVIS",
    min_confidence: float = 0.7,
) -> Optional[Intent]:
    """Return the deterministic intent for ``text``, or ``None``.

    ``None`` means "this needs real language understanding" - the planner will
    ask the model instead.
    """
    query = normalize(text, assistant)
    if not query:
        return None
    ctx = {"owner": owner, "assistant": assistant}
    for rule in RULE_FUNCTIONS:
        try:
            intent = rule(query, ctx)
        except Exception:  # a broken rule must not break the assistant
            continue
        if intent is not None and intent.confidence >= min_confidence:
            return intent
    return None


def rule_names() -> List[str]:
    return [rule.__name__.replace("_rule_", "") for rule in RULE_FUNCTIONS]


__all__ = ["Intent", "RULE_FUNCTIONS", "extract_expression", "match", "normalize", "rule_names"]
