"""Tool system foundation.

A *tool* is a small, self-describing, independently testable capability.  The AI
never calls Python directly: it emits a plan naming a tool and its arguments, the
router validates those arguments, and only then does anything run.

Key properties this layer guarantees:

* **discoverable** - tools self-register via the :func:`tool` decorator
* **describable** - each one exposes a JSON schema the planner can read
* **safe** - arguments are validated/coerced, unknown keys are dropped, calls
  time out instead of hanging the GUI, and exceptions become data
* **testable** - a tool can be called directly with no AI and no GUI involved

Adding a capability later means writing one function and decorating it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #
@dataclass
class ToolResult:
    """Outcome of a tool call.  ``ok`` is the truth - never assume success."""

    ok: bool
    output: str = ""
    error: str = ""
    data: Any = None
    needs_confirmation: Optional[str] = None

    @classmethod
    def success(cls, output: str = "", data: Any = None) -> "ToolResult":
        return cls(ok=True, output=str(output), data=data)

    @classmethod
    def failure(cls, error: str, data: Any = None) -> "ToolResult":
        return cls(ok=False, output="", error=str(error), data=data)

    @classmethod
    def confirm(cls, question: str, data: Any = None) -> "ToolResult":
        return cls(ok=True, output=question, data=data, needs_confirmation=question)

    def as_dict(self) -> Dict[str, Any]:
        payload = {"ok": self.ok, "output": self.output, "error": self.error}
        if self.needs_confirmation:
            payload["needs_confirmation"] = self.needs_confirmation
        return payload

    def __bool__(self) -> bool:  # `if result:` reads naturally
        return self.ok

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.output if self.ok else f"ERROR: {self.error}"


# --------------------------------------------------------------------------- #
# context
# --------------------------------------------------------------------------- #
class ToolContext:
    """Explicit dependencies handed to tools (no hidden globals)."""

    def __init__(
        self,
        settings: Any = None,
        memory: Any = None,
        events: Any = None,
        ai: Any = None,
        hardware: Any = None,
        reminders: Any = None,
    ) -> None:
        self.settings = settings
        self.memory = memory
        self.events = events
        self.ai = ai  # ProviderManager, used by coding/summarising tools
        self.hardware = hardware
        self.reminders = reminders  # ReminderScheduler instance

    def notify(self, kind: str, message: str, **extra: Any) -> None:
        if self.events is None:
            return
        try:
            self.events.publish(kind, message=message, **extra)
        except Exception:  # pragma: no cover - never let UI plumbing break a tool
            pass


# --------------------------------------------------------------------------- #
# tool object + registry
# --------------------------------------------------------------------------- #
@dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]
    func: Callable[..., Any]
    category: str = "general"
    dangerous: bool = False
    offline_safe: bool = False
    aliases: Sequence[str] = field(default_factory=tuple)

    def schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters or {"type": "object", "properties": {}},
        }

    def __call__(self, **kwargs: Any) -> Any:
        return self.func(**kwargs)


_REGISTRY: Dict[str, Tool] = {}
_LOCK = threading.RLock()


def tool(
    name: str,
    description: str,
    parameters: Optional[Dict[str, Any]] = None,
    category: str = "general",
    dangerous: bool = False,
    offline_safe: bool = False,
    aliases: Sequence[str] = (),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator registering a function as a JARVIS capability."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        spec = Tool(
            name=name,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}},
            func=func,
            category=category,
            dangerous=dangerous,
            offline_safe=offline_safe,
            aliases=tuple(aliases),
        )
        register(spec)
        return func

    return decorator


def register(spec: Tool) -> Tool:
    with _LOCK:
        _REGISTRY[spec.name] = spec
        for alias in spec.aliases:
            _REGISTRY[alias] = spec
    return spec


def get_tool(name: str) -> Optional[Tool]:
    if not name:
        return None
    with _LOCK:
        found = _REGISTRY.get(name.strip())
    if found is not None:
        return found
    # be forgiving about case/separators the model may invent
    key = name.strip().lower().replace("-", "_").replace(" ", "_")
    with _LOCK:
        return _REGISTRY.get(key)


def all_tools(unique: bool = True) -> List[Tool]:
    with _LOCK:
        specs = list(_REGISTRY.values())
    if not unique:
        return specs
    seen: Dict[str, Tool] = {}
    for spec in specs:
        seen.setdefault(spec.name, spec)
    return list(seen.values())


def tool_names() -> List[str]:
    return sorted(spec.name for spec in all_tools())


def tool_schemas(categories: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    wanted = {c.lower() for c in categories} if categories else None
    return [
        spec.schema()
        for spec in all_tools()
        if wanted is None or spec.category.lower() in wanted
    ]


def clear_registry() -> None:
    """Testing helper."""
    with _LOCK:
        _REGISTRY.clear()


# --------------------------------------------------------------------------- #
# argument handling
# --------------------------------------------------------------------------- #
def _coerce(value: Any, kind: Optional[str]) -> Any:
    if kind is None or value is None:
        return value
    try:
        if kind == "string":
            return value if isinstance(value, str) else str(value)
        if kind == "integer":
            return int(value)
        if kind == "number":
            return float(value)
        if kind == "boolean":
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "y", "on"}
            return bool(value)
        if kind == "array":
            if isinstance(value, (list, tuple)):
                return list(value)
            if isinstance(value, str):
                return [part.strip() for part in value.split(",") if part.strip()]
            return [value]
        if kind == "object" and isinstance(value, Mapping):
            return dict(value)
    except (TypeError, ValueError):
        return value
    return value


def validate_args(spec: Tool, args: Optional[Mapping[str, Any]]) -> tuple:
    """Return ``(clean_args, errors)``.  Unknown keys are dropped, not fatal."""
    params = spec.parameters or {}
    properties = params.get("properties") if isinstance(params, Mapping) else None
    properties = properties or {}
    required = list(params.get("required") or [])
    args = dict(args or {})

    errors: List[str] = []
    for key in required:
        if key not in args or args[key] in (None, ""):
            errors.append(f"missing required argument '{key}'")

    clean: Dict[str, Any] = {}
    for key, value in args.items():
        if properties and key not in properties:
            continue  # models invent arguments; ignore rather than crash
        spec_for_key = properties.get(key, {}) if isinstance(properties, Mapping) else {}
        expected = spec_for_key.get("type") if isinstance(spec_for_key, Mapping) else None
        clean[key] = _coerce(value, expected)
    return clean, errors


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
def _call(func: Callable[..., Any], kwargs: Dict[str, Any], timeout: Optional[float]) -> Any:
    """Call ``func``, optionally on a daemon thread so we can enforce a timeout."""
    if not timeout or timeout <= 0:
        return func(**kwargs)

    box: Dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = func(**kwargs)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller thread
            box["error"] = exc

    thread = threading.Thread(target=runner, name="jarvis-tool", daemon=True)
    thread.start()
    thread.join(float(timeout))
    if thread.is_alive():
        raise TimeoutError(f"tool exceeded its {timeout:g}s time limit")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def execute(
    name: str,
    args: Optional[Mapping[str, Any]] = None,
    ctx: Optional[ToolContext] = None,
    timeout: Optional[float] = None,
    force: bool = False,
) -> ToolResult:
    """Run a tool by name.  Never raises; failures come back as a result."""
    spec = get_tool(name)
    if spec is None:
        return ToolResult.failure(f"unknown tool '{name}'")

    clean, errors = validate_args(spec, args)
    if errors:
        return ToolResult.failure(f"{spec.name}: " + "; ".join(errors))

    if spec.dangerous and not force:
        ctx_settings = getattr(ctx, "settings", None)
        confirm = True
        if ctx_settings is not None:
            confirm = bool(getattr(getattr(ctx_settings, "safety", None), "confirm_destructive", True))
        if confirm:
            return ToolResult.confirm(
                f"Confirm: run `{spec.name}` with {clean or 'no arguments'}?",
                data={"tool": spec.name, "args": clean},
            )

    kwargs = dict(clean)
    if ctx is not None:
        # tools opt in to context by declaring a `ctx` parameter
        try:
            import inspect

            if "ctx" in inspect.signature(spec.func).parameters:
                kwargs["ctx"] = ctx
        except (TypeError, ValueError):  # pragma: no cover - builtins
            pass

    if timeout is None and ctx is not None and getattr(ctx, "settings", None) is not None:
        timeout = getattr(ctx.settings.safety, "tool_timeout", None)

    started = time.time()
    try:
        raw = _call(spec.func, kwargs, timeout)
    except TimeoutError as exc:
        return ToolResult.failure(f"{spec.name} timed out: {exc}")
    except PermissionError as exc:
        return ToolResult.failure(f"{spec.name} was blocked: {exc}")
    except FileNotFoundError as exc:
        return ToolResult.failure(f"{spec.name}: {exc}")
    except Exception as exc:  # noqa: BLE001 - tools must never take JARVIS down
        return ToolResult.failure(f"{spec.name} failed: {type(exc).__name__}: {exc}")

    elapsed = round(time.time() - started, 3)
    if isinstance(raw, ToolResult):
        result = raw
    elif raw is None:
        result = ToolResult.success("")
    elif isinstance(raw, str):
        result = ToolResult.success(raw)
    elif isinstance(raw, Mapping):
        result = ToolResult.success(str(raw.get("output", "")), data=raw)
    else:
        result = ToolResult.success(str(raw), data=raw)
    if isinstance(result.data, dict):
        result.data.setdefault("elapsed_s", elapsed)
    return result


__all__ = [
    "Tool",
    "ToolContext",
    "ToolResult",
    "all_tools",
    "clear_registry",
    "execute",
    "get_tool",
    "register",
    "tool",
    "tool_names",
    "tool_schemas",
    "validate_args",
]
