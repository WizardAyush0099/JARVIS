"""Productivity + local-state tools: maths, time, units, notes, reminders, memory.

Two things here are direct answers to problems in the old code:

* the calculator uses a restricted AST walker instead of ``eval()`` - the old
  project evaluated user text, which is a remote-code-execution hole once voice
  input is involved;
* reminders live in a scheduler thread that only touches the filesystem, so a
  reminder still fires while the GUI is idle or the network is down.
"""

from __future__ import annotations

import ast
import math
import operator
import random
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from core.logging_setup import get_logger
from core.storage import JsonFile
from tools.base import ToolContext, ToolResult, tool

log = get_logger("tools.utilities")

# --------------------------------------------------------------------------- #
# safe maths
# --------------------------------------------------------------------------- #
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf}
_FUNCTIONS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "log": math.log,
    "log2": math.log2,
    "log10": math.log10,
    "exp": math.exp,
    "floor": math.floor,
    "ceil": math.ceil,
    "degrees": math.degrees,
    "radians": math.radians,
}
_MAX_EXPONENT = 512
_MAX_ABS_RESULT = 1e100


class CalculationError(ValueError):
    pass


def _evaluate(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalculationError("only numbers are allowed")
        return node.value
    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise CalculationError(f"unknown name '{node.id}'")
    if isinstance(node, ast.BinOp):
        handler = _BIN_OPS.get(type(node.op))
        if handler is None:
            raise CalculationError("that operator is not allowed")
        left, right = _evaluate(node.left), _evaluate(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_EXPONENT:
            raise CalculationError("exponent is too large")
        if type(node.op) in (ast.Div, ast.FloorDiv, ast.Mod) and right == 0:
            raise CalculationError("division by zero")
        result = handler(left, right)
        if isinstance(result, complex) or abs(result) > _MAX_ABS_RESULT:
            raise CalculationError("result is out of range")
        return result
    if isinstance(node, ast.UnaryOp):
        handler = _UNARY_OPS.get(type(node.op))
        if handler is None:
            raise CalculationError("that operator is not allowed")
        return handler(_evaluate(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            raise CalculationError("that function is not allowed")
        if node.keywords:
            raise CalculationError("keyword arguments are not allowed")
        args = [_evaluate(arg) for arg in node.args]
        if len(args) > 4:
            raise CalculationError("too many arguments")
        result = _FUNCTIONS[node.func.id](*args)
        if isinstance(result, complex):
            raise CalculationError("result is not a real number")
        return result
    raise CalculationError("that expression is not allowed")


def safe_calculate(expression: str) -> float:
    """Evaluate an arithmetic expression without ``eval``."""
    if not expression or not str(expression).strip():
        raise CalculationError("nothing to calculate")
    text = str(expression).strip()
    if len(text) > 200:
        raise CalculationError("expression is too long")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise CalculationError(f"could not understand '{text}'") from exc
    return _evaluate(tree)


def format_number(value: float) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return f"{int(value):,}"
    return f"{value:,.10g}"


@tool(
    name="calculate",
    description="Evaluate an arithmetic expression (supports + - * / // % **, sqrt, sin, cos, log, round, pi, e).",
    parameters={
        "type": "object",
        "properties": {"expression": {"type": "string", "description": "e.g. '27*43' or 'sqrt(16)+2'"}},
        "required": ["expression"],
    },
    category="utilities",
    offline_safe=True,
    aliases=("calc", "maths", "math"),
)
def calculate(expression: str) -> ToolResult:
    original = str(expression or "")
    candidates = [original]
    if re.search(r"[A-Za-z]", original):
        # the model (or the user) may pass prose like "12% of 300"
        from core.intent import extract_expression

        normalised = extract_expression(original)
        if normalised and normalised not in candidates:
            candidates.append(normalised)

    for candidate in candidates:
        try:
            value = safe_calculate(candidate)
        except Exception:  # noqa: BLE001 - try the next interpretation
            continue
        return ToolResult.success(
            f"{original.strip()} = {format_number(value)}", data={"value": value}
        )
    return ToolResult.failure(f"I can't calculate '{original.strip()}' - try something like 27*43")


# --------------------------------------------------------------------------- #
# unit conversion
# --------------------------------------------------------------------------- #
_LENGTH = {  # metres
    "mm": 0.001, "millimetre": 0.001, "millimeter": 0.001, "cm": 0.01, "centimetre": 0.01,
    "centimeter": 0.01, "m": 1.0, "metre": 1.0, "meter": 1.0, "km": 1000.0, "kilometre": 1000.0,
    "kilometer": 1000.0, "in": 0.0254, "inch": 0.0254, "inches": 0.0254, "ft": 0.3048, "foot": 0.3048,
    "feet": 0.3048, "yd": 0.9144, "yard": 0.9144, "mile": 1609.344, "miles": 1609.344, "mi": 1609.344,
}
_MASS = {  # kilograms
    "mg": 1e-6, "g": 0.001, "gram": 0.001, "grams": 0.001, "kg": 1.0, "kilo": 1.0, "kilogram": 1.0,
    "kilograms": 1.0, "lb": 0.45359237, "lbs": 0.45359237, "pound": 0.45359237, "pounds": 0.45359237,
    "oz": 0.028349523125, "ounce": 0.028349523125, "tonne": 1000.0, "ton": 907.18474,
}
_VOLUME = {  # litres
    "ml": 0.001, "millilitre": 0.001, "l": 1.0, "litre": 1.0, "liter": 1.0, "litres": 1.0,
    "liters": 1.0, "cup": 0.2365882365, "pint": 0.473176473, "gallon": 3.785411784,
    "gallons": 3.785411784,
}
_DATA = {  # bytes
    "b": 1.0, "byte": 1.0, "bytes": 1.0, "kb": 1024.0, "mb": 1024.0**2, "gb": 1024.0**3,
    "tb": 1024.0**4,
}
_TIME = {"s": 1.0, "sec": 1.0, "second": 1.0, "seconds": 1.0, "min": 60.0, "minute": 60.0,
         "minutes": 60.0, "h": 3600.0, "hr": 3600.0, "hour": 3600.0, "hours": 3600.0,
         "day": 86400.0, "days": 86400.0, "week": 604800.0}
_SPEED = {"m/s": 1.0, "km/h": 1 / 3.6, "kph": 1 / 3.6, "mph": 0.44704, "knot": 0.514444}
_TABLES = {"length": _LENGTH, "mass": _MASS, "volume": _VOLUME, "data": _DATA, "time": _TIME, "speed": _SPEED}


def _parse_quantity(quantity: str) -> Tuple[float, str]:
    text = str(quantity).strip().lower().replace(",", "")
    parts = text.split()
    if len(parts) < 2:
        raise ValueError(f"expected something like '5 km', got '{quantity}'")
    try:
        value = float(parts[0])
    except ValueError as exc:
        raise ValueError(f"'{parts[0]}' is not a number") from exc
    return value, " ".join(parts[1:])


def convert_units_values(value: float, from_unit: str, to_unit: str) -> Tuple[float, str]:
    source = from_unit.strip().lower()
    target = to_unit.strip().lower()

    if source in {"c", "celsius", "\u00b0c"} or target in {"c", "celsius", "\u00b0c"}:
        if source in {"c", "celsius", "\u00b0c"}:
            celsius = value
        elif source in {"f", "fahrenheit", "\u00b0f"}:
            celsius = (value - 32) * 5 / 9
        elif source in {"k", "kelvin"}:
            celsius = value - 273.15
        else:
            raise ValueError(f"can't convert from '{from_unit}'")
        if target in {"c", "celsius", "\u00b0c"}:
            return celsius, "C"
        if target in {"f", "fahrenheit", "\u00b0f"}:
            return celsius * 9 / 5 + 32, "F"
        if target in {"k", "kelvin"}:
            return celsius + 273.15, "K"
        raise ValueError(f"can't convert to '{to_unit}'")

    for name, table in _TABLES.items():
        if source in table and target in table:
            converted = value * table[source] / table[target]
            return converted, f"{target} ({name})"
    raise ValueError(
        f"I don't know how to convert '{from_unit}' to '{to_unit}'. "
        "Supported: length, mass, volume, data, time, speed and temperatures."
    )


@tool(
    name="convert_units",
    description="Convert between units (length, mass, volume, data, time, speed, temperature).",
    parameters={
        "type": "object",
        "properties": {
            "quantity": {"type": "string", "description": "value with unit, e.g. '5 km'"},
            "target_unit": {"type": "string", "description": "unit to convert to, e.g. 'miles'"},
        },
        "required": ["quantity", "target_unit"],
    },
    category="utilities",
    offline_safe=True,
)
def convert_units(quantity: str, target_unit: str) -> ToolResult:
    try:
        value, unit = _parse_quantity(quantity)
    except ValueError as exc:
        return ToolResult.failure(str(exc))
    try:
        result, label = convert_units_values(value, unit, target_unit)
    except ValueError as exc:
        return ToolResult.failure(str(exc))
    return ToolResult.success(
        f"{format_number(value)} {unit} is {format_number(result)} {target_unit}.",
        data={"value": result, "unit": target_unit},
    )


# --------------------------------------------------------------------------- #
# time / date
# --------------------------------------------------------------------------- #
@tool(
    name="current_time",
    description="Get the current local time.",
    parameters={"type": "object", "properties": {}},
    category="utilities",
    offline_safe=True,
)
def current_time() -> ToolResult:
    now = datetime.now()
    return ToolResult.success(f"It's {now.strftime('%I:%M %p').lstrip('0')}.", data={"time": now.isoformat()})


@tool(
    name="current_datetime",
    description="Get the current date, weekday and time.",
    parameters={"type": "object", "properties": {}},
    category="utilities",
    offline_safe=True,
    aliases=("current_date", "date"),
)
def current_datetime() -> ToolResult:
    now = datetime.now()
    return ToolResult.success(
        f"Today is {now.strftime('%A, %d %B %Y')} and it's {now.strftime('%I:%M %p').lstrip('0')}.",
        data={"datetime": now.isoformat()},
    )


# --------------------------------------------------------------------------- #
# notes
# --------------------------------------------------------------------------- #
def _json_file(ctx: Optional[ToolContext], attribute: str, factory: Any = list) -> JsonFile:
    if ctx is None or getattr(ctx, "settings", None) is None:
        raise RuntimeError("this tool needs a configured context (notes storage)")
    path = getattr(ctx.settings.paths, attribute)
    return JsonFile(path, default_factory=factory)


@tool(
    name="add_note",
    description="Save a note locally.",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string", "description": "note contents"}},
        "required": ["text"],
    },
    category="utilities",
    offline_safe=True,
)
def add_note(text: str, ctx: Optional[ToolContext] = None) -> ToolResult:
    text = (text or "").strip()
    if not text:
        return ToolResult.failure("that note was empty")
    store = _json_file(ctx, "notes_file", list)
    entry = {"id": uuid.uuid4().hex[:8], "text": text, "created": time.time()}

    def mutate(data: Any) -> None:
        items = data.get("notes") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise TypeError("notes store is not a list")
        items.append(entry)

    try:
        store.update(mutate)
    except TypeError:
        if not store.save([entry]):
            return ToolResult.failure("could not save the note")
    except OSError as exc:
        return ToolResult.failure(f"could not save the note: {exc}")
    return ToolResult.success(f"Saved note #{entry['id']}: {text}", data={"id": entry["id"]})


def _notes_list(store: JsonFile) -> List[Dict[str, Any]]:
    data = store.load()
    if isinstance(data, dict):
        data = data.get("notes", [])
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


@tool(
    name="list_notes",
    description="List saved notes.",
    parameters={"type": "object", "properties": {}},
    category="utilities",
    offline_safe=True,
)
def list_notes(ctx: Optional[ToolContext] = None) -> ToolResult:
    notes = _notes_list(_json_file(ctx, "notes_file", list))
    if not notes:
        return ToolResult.success("You have no notes yet.")
    lines = []
    for note in notes[-20:]:
        when = datetime.fromtimestamp(note.get("created", time.time())).strftime("%d %b %H:%M")
        lines.append(f"[{note.get('id', '?')}] {when} - {note.get('text', '')}")
    return ToolResult.success("Your notes:\n" + "\n".join(lines), data={"notes": notes})


# --------------------------------------------------------------------------- #
# reminders
# --------------------------------------------------------------------------- #
_RELATIVE = re.compile(
    r"in\s+(?:about\s+|roughly\s+)?(\d+(?:\.\d+)?)\s*"
    r"(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b",
    re.I,
)
_CLOCK = re.compile(r"at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)
_NAMED_GAPS = (
    (re.compile(r"in\s+half\s+an?\s+hour", re.I), timedelta(minutes=30)),
    (re.compile(r"in\s+an?\s+hour", re.I), timedelta(hours=1)),
    (re.compile(r"in\s+a\s+(?:minute|min)\b", re.I), timedelta(minutes=1)),
)
_SECONDS = {"s", "sec", "secs", "second", "seconds"}
_HOURS = {"h", "hr", "hrs", "hour", "hours"}


def parse_when(text: str, now: Optional[datetime] = None) -> Tuple[str, Optional[datetime]]:
    """Split 'call mum in 20 minutes' into ``('call mum', due_datetime)``."""
    now = now or datetime.now()
    source = text or ""

    match = _RELATIVE.search(source)
    if match:
        amount = float(match.group(1))
        unit = match.group(2).lower()
        if unit in _SECONDS:
            delta = timedelta(seconds=amount)
        elif unit in _HOURS:
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(minutes=amount)
        return _strip_when(source, match.start(), match.end()), now + delta

    for pattern, delta in _NAMED_GAPS:
        match = pattern.search(source)
        if match:
            return _strip_when(source, match.start(), match.end()), now + delta

    match = _CLOCK.search(source)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = (match.group(3) or "").lower()
        if not 0 <= hour <= 23:
            return _strip_when(source, match.start(), match.end()), None
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        try:
            due = now.replace(hour=hour, minute=min(minute, 59), second=0, microsecond=0)
        except ValueError:
            return _strip_when(source, match.start(), match.end()), None
        if due <= now:
            due += timedelta(days=1)
        return _strip_when(source, match.start(), match.end()), due

    return source.strip(), None


def _strip_when(source: str, start: int, end: int) -> str:
    cleaned = (source[:start] + source[end:]).strip(" ,.-")
    cleaned = re.sub(r"\b(?:to|that|about|me)\s*$", "", cleaned, flags=re.I).strip()
    cleaned = re.sub(r"^(?:please\s+)?(?:remind me|remind)\s+", "", cleaned, flags=re.I).strip()
    return cleaned or "your reminder"


class ReminderScheduler:
    """Minimal background scheduler: filesystem only, no network, no GUI."""

    def __init__(self, path: Any = None, events: Any = None, on_fire: Any = None, interval: float = 1.0) -> None:
        self.path = path
        self.events = events
        self.on_fire = on_fire
        self.interval = interval
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._store = JsonFile(path, default_factory=list) if path is not None else None

    # -- storage -----------------------------------------------------------
    def _load(self) -> List[Dict[str, Any]]:
        if self._store is None:
            return []
        data = self._store.load()
        return data if isinstance(data, list) else []

    def _save(self, items: List[Dict[str, Any]]) -> None:
        if self._store is not None:
            self._store.save(items)

    def add(self, text: str, due: datetime) -> Dict[str, Any]:
        entry = {
            "id": uuid.uuid4().hex[:8],
            "text": text,
            "due": due.timestamp(),
            "due_text": due.strftime("%d %b %I:%M %p").replace(" 0", " "),
            "created": time.time(),
        }
        with self._lock:
            items = self._load()
            items.append(entry)
            self._save(items)
        return entry

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            items = [item for item in self._load() if isinstance(item, dict)]
        return sorted(items, key=lambda item: item.get("due", 0))

    def cancel(self, reminder_id: str) -> bool:
        with self._lock:
            items = self._load()
            kept = [item for item in items if item.get("id") != reminder_id]
            self._save(kept)
            return len(kept) != len(items)

    def due_now(self) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            items = self._load()
            due = [item for item in items if float(item.get("due", 0)) <= now]
            if due:
                remaining = [item for item in items if item not in due]
                self._save(remaining)
        return due

    # -- thread ------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="jarvis-reminders", daemon=True)
        self._thread.start()
        log.info("reminder scheduler started")

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                for reminder in self.due_now():
                    message = f"Reminder: {reminder.get('text', '')}"
                    if self.events is not None:
                        self.events.publish("reminder", message=message, reminder=reminder)
                    if callable(self.on_fire):
                        try:
                            self.on_fire(message)
                        except Exception:  # pragma: no cover - never kill the thread
                            log.exception("reminder callback failed")
            except Exception:  # pragma: no cover - defensive
                log.exception("reminder scheduler iteration failed")


@tool(
    name="set_reminder",
    description="Set a reminder or timer, e.g. 'call mum in 20 minutes' or 'at 7:30am'.",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string", "description": "what to be reminded about, with a time"}},
        "required": ["text"],
    },
    category="utilities",
    offline_safe=True,
    aliases=("set_timer", "remind"),
)
def set_reminder(text: str, ctx: Optional[ToolContext] = None) -> ToolResult:
    cleaned, due = parse_when(text)
    if due is None:
        return ToolResult.failure(
            "tell me when - for example 'remind me to stretch in 10 minutes'"
        )
    scheduler = getattr(ctx, "reminders", None) if ctx is not None else None
    if scheduler is None:
        if ctx is None or getattr(ctx, "settings", None) is None:
            return ToolResult.failure("reminders need a configured context")
        scheduler = ReminderScheduler(ctx.settings.paths.reminders_file, events=ctx.events)
    entry = scheduler.add(cleaned, due)
    return ToolResult.success(
        f"Okay, I'll remind you to {cleaned} at {entry['due_text']}.", data=entry
    )


@tool(
    name="list_reminders",
    description="List pending reminders and timers.",
    parameters={"type": "object", "properties": {}},
    category="utilities",
    offline_safe=True,
)
def list_reminders(ctx: Optional[ToolContext] = None) -> ToolResult:
    if ctx is None or getattr(ctx, "settings", None) is None:
        return ToolResult.failure("reminders need a configured context")
    scheduler = getattr(ctx, "reminders", None) or ReminderScheduler(
        ctx.settings.paths.reminders_file, events=ctx.events
    )
    items = scheduler.list()
    if not items:
        return ToolResult.success("You have no reminders set.")
    lines = [f"[{i.get('id')}] {i.get('due_text')} - {i.get('text')}" for i in items]
    return ToolResult.success("Your reminders:\n" + "\n".join(lines), data={"reminders": items})


@tool(
    name="cancel_reminder",
    description="Cancel a reminder by its id.",
    parameters={
        "type": "object",
        "properties": {"reminder_id": {"type": "string"}},
        "required": ["reminder_id"],
    },
    category="utilities",
    offline_safe=True,
)
def cancel_reminder(reminder_id: str, ctx: Optional[ToolContext] = None) -> ToolResult:
    if ctx is None or getattr(ctx, "settings", None) is None:
        return ToolResult.failure("reminders need a configured context")
    scheduler = getattr(ctx, "reminders", None) or ReminderScheduler(
        ctx.settings.paths.reminders_file, events=ctx.events
    )
    if scheduler.cancel(reminder_id):
        return ToolResult.success(f"Cancelled reminder {reminder_id}.")
    return ToolResult.failure(f"no reminder with id {reminder_id}")


# --------------------------------------------------------------------------- #
# memory tools
# --------------------------------------------------------------------------- #
@tool(
    name="remember_fact",
    description="Persist a durable fact or preference about the user.",
    parameters={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "short label, e.g. 'project'"},
            "value": {"type": "string", "description": "what to remember"},
        },
        "required": ["value"],
    },
    category="memory",
    offline_safe=True,
)
def remember_fact(value: str, key: str = "note", ctx: Optional[ToolContext] = None) -> ToolResult:
    if ctx is None or getattr(ctx, "memory", None) is None:
        return ToolResult.failure("memory is not available")
    record = ctx.memory.remember(key or "note", value)
    return ToolResult.success(f"Noted - I'll remember {record['key']} is {record['value']}.", data=record)


@tool(
    name="recall_fact",
    description="Look up something previously stored in memory.",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
    category="memory",
    offline_safe=True,
    aliases=("remember", "recall"),
)
def recall_fact(query: str, ctx: Optional[ToolContext] = None) -> ToolResult:
    if ctx is None or getattr(ctx, "memory", None) is None:
        return ToolResult.failure("memory is not available")
    matches = ctx.memory.search_facts(query)
    if not matches:
        return ToolResult.success(f"I don't have anything stored about '{query}' yet.")
    lines = [f"{item['key']}: {item['value']}" for item in matches]
    return ToolResult.success("Here's what I remember:\n" + "\n".join(lines), data={"matches": matches})


@tool(
    name="forget_fact",
    description="Delete a stored fact from memory.",
    parameters={
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
    },
    category="memory",
    # deleting memory is destructive, so it is never reachable from the offline
    # rule engine - only through an explicit, confirmed request
    offline_safe=False,
    dangerous=True,
)
def forget_fact(key: str, ctx: Optional[ToolContext] = None) -> ToolResult:
    if ctx is None or getattr(ctx, "memory", None) is None:
        return ToolResult.failure("memory is not available")
    if ctx.memory.forget(key):
        return ToolResult.success(f"Forgotten: {key}.")
    return ToolResult.failure(f"nothing stored under '{key}'")


@tool(
    name="clear_conversation",
    description="Clear the current conversation history (keeps long-term facts).",
    parameters={"type": "object", "properties": {}},
    category="memory",
    offline_safe=True,
)
def clear_conversation(ctx: Optional[ToolContext] = None) -> ToolResult:
    if ctx is None or getattr(ctx, "memory", None) is None:
        return ToolResult.failure("memory is not available")
    ctx.memory.clear_conversation()
    return ToolResult.success("Conversation cleared. Long-term memory is untouched.")


# --------------------------------------------------------------------------- #
# misc
# --------------------------------------------------------------------------- #
@tool(
    name="random_number",
    description="Pick a random integer between two bounds.",
    parameters={
        "type": "object",
        "properties": {
            "low": {"type": "integer", "description": "lower bound (default 1)"},
            "high": {"type": "integer", "description": "upper bound (default 100)"},
        },
    },
    category="utilities",
    offline_safe=True,
)
def random_number(low: int = 1, high: int = 100) -> ToolResult:
    low, high = int(low), int(high)
    if low > high:
        low, high = high, low
    value = random.randint(low, high)
    return ToolResult.success(f"Random number between {low} and {high}: {value}", data={"value": value})


@tool(
    name="flip_coin",
    description="Flip a coin.",
    parameters={"type": "object", "properties": {}},
    category="utilities",
    offline_safe=True,
)
def flip_coin() -> ToolResult:
    return ToolResult.success(f"It's {random.choice(['heads', 'tails'])}.")


__all__ = [
    "CalculationError",
    "ReminderScheduler",
    "convert_units_values",
    "format_number",
    "parse_when",
    "safe_calculate",
]
