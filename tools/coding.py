"""Coding tools: explain, debug, create, patch and run code.

Two things the old project could not do at all, plus one it did dangerously:

* ``create_script`` syntax-checks what it wrote with ``compile()`` and reports a
  failure instead of claiming success
* ``patch_file`` does an exact, counted replacement and refuses ambiguous edits,
  rather than regenerating whole files and losing the user's work
* ``run_python_file`` is explicitly confirmation-gated and time-limited
"""

from __future__ import annotations

import json as jsonlib
import subprocess
import sys
from typing import Any, Optional

from core.logging_setup import get_logger
from tools.base import ToolContext, ToolResult, tool
from tools.files import resolve_in_sandbox

log = get_logger("tools.coding")

_MAX_CONTEXT_CHARS = 8000


def _manager(ctx: Optional[ToolContext]) -> Any:
    return getattr(ctx, "ai", None) if ctx is not None else None


def _ask(ctx: Optional[ToolContext], prompt: str, system: str, max_tokens: int = 700) -> ToolResult:
    manager = _manager(ctx)
    if manager is None:
        return ToolResult.failure("I need an AI provider for this - add an API key in your .env file")
    try:
        answer = manager.complete(prompt, system=system, max_tokens=max_tokens)
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure(f"the AI provider failed: {exc}")
    return ToolResult.success(answer)


@tool(
    name="explain_code",
    description="Explain what a piece of code or a file does.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "file to explain"},
            "code": {"type": "string", "description": "or paste the code directly"},
            "question": {"type": "string", "description": "optional specific question"},
        },
    },
    category="coding",
)
def explain_code(
    path: str = "", code: str = "", question: str = "", ctx: Optional[ToolContext] = None
) -> ToolResult:
    source = str(code or "").strip()
    origin = "the pasted snippet"
    if not source and path:
        try:
            target = resolve_in_sandbox(path, ctx)
            source = target.read_text(encoding="utf-8", errors="replace")[: _MAX_CONTEXT_CHARS]
            origin = str(target)
        except (ValueError, PermissionError) as exc:
            return ToolResult.failure(str(exc))
        except OSError as exc:
            return ToolResult.failure(f"could not read {path}: {exc}")
    if not source:
        return ToolResult.failure("give me a file to read or paste the code")

    focus = f"\nThe user specifically asks: {question}" if question else ""
    return _ask(
        ctx,
        f"Explain this code from {origin} clearly, then note anything that looks buggy or risky.{focus}\n\n"
        f"```\n{source}\n```",
        system=(
            "You are JARVIS, a precise senior engineer. Explain structure first, then details. "
            "Be concrete, avoid filler, and say when something is a guess."
        ),
    )


@tool(
    name="analyze_error",
    description="Explain a stack trace or error message and how to fix it.",
    parameters={
        "type": "object",
        "properties": {
            "error": {"type": "string", "description": "the traceback or error text"},
            "context": {"type": "string", "description": "optional extra context"},
        },
        "required": ["error"],
    },
    category="coding",
    aliases=("debug_error", "explain_error"),
)
def analyze_error(error: str, context: str = "", ctx: Optional[ToolContext] = None) -> ToolResult:
    text = str(error or "").strip()
    if not text:
        return ToolResult.failure("paste the error and I'll work through it")
    extra = f"\n\nExtra context: {context}" if context else ""
    return _ask(
        ctx,
        f"Diagnose this error and give the fix, ordered by likelihood.\n\n```\n{text[:6000]}\n```{extra}",
        system=(
            "You are JARVIS debugging on a Raspberry Pi 4 running Python 3. "
            "State the root cause, then the exact change that fixes it."
        ),
    )


@tool(
    name="create_script",
    description="Write a code file and verify its syntax.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "where to save it"},
            "code": {"type": "string", "description": "file contents"},
        },
        "required": ["path", "code"],
    },
    category="coding",
)
def create_script(path: str, code: str, ctx: Optional[ToolContext] = None) -> ToolResult:
    try:
        target = resolve_in_sandbox(path, ctx)
    except (ValueError, PermissionError) as exc:
        return ToolResult.failure(str(exc))

    source = str(code or "")
    warning = ""
    if target.suffix == ".py":
        try:
            compile(source, str(target), "exec")
        except SyntaxError as exc:
            line = exc.lineno or 0
            return ToolResult.failure(
                f"I wrote {target.name} but it has a syntax error on line {line}: {exc.msg}. "
                "Nothing was saved as working code - let me fix it first."
            )
    elif target.suffix == ".json":
        try:
            jsonlib.loads(source)
        except ValueError as exc:
            return ToolResult.failure(f"that is not valid JSON: {exc}")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    except OSError as exc:
        return ToolResult.failure(f"could not write {target}: {exc}")

    if target.suffix == ".sh":
        warning = " Remember to run it with `sh <file>`, not `./<file>`."
    return ToolResult.success(
        f"Wrote {len(source.splitlines())} lines to {target} and checked the syntax.{warning}",
        data={"path": str(target)},
    )


@tool(
    name="patch_file",
    description="Replace an exact block of text inside an existing file.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "find": {"type": "string", "description": "exact text to replace"},
            "replace": {"type": "string", "description": "replacement text (empty deletes it)"},
            "all_matches": {"type": "boolean", "description": "replace every occurrence (default false)"},
        },
        "required": ["path", "find", "replace"],
    },
    category="coding",
)
def patch_file(
    path: str,
    find: str,
    replace: str = "",
    all_matches: bool = False,
    ctx: Optional[ToolContext] = None,
) -> ToolResult:
    try:
        target = resolve_in_sandbox(path, ctx)
    except (ValueError, PermissionError) as exc:
        return ToolResult.failure(str(exc))
    if not target.is_file():
        return ToolResult.failure(f"there is no file at {target}")
    try:
        original = target.read_text(encoding="utf-8")
    except OSError as exc:
        return ToolResult.failure(f"could not read {target}: {exc}")

    needle = str(find)
    if not needle:
        return ToolResult.failure("give me the exact text to replace")
    occurrences = original.count(needle)
    if occurrences == 0:
        return ToolResult.failure(
            f"that exact text was not found in {target.name} - I won't guess at a fuzzy match"
        )
    if occurrences > 1 and not all_matches:
        return ToolResult.failure(
            f"that text appears {occurrences} times in {target.name}. "
            "Include more surrounding context, or set all_matches to true."
        )

    updated = original.replace(needle, str(replace), -1 if all_matches else 1)
    if target.suffix == ".py":
        try:
            compile(updated, str(target), "exec")
        except SyntaxError as exc:
            return ToolResult.failure(
                f"that change would break the syntax on line {exc.lineno}: {exc.msg}. Nothing was written."
            )
    try:
        backup = target.with_suffix(target.suffix + ".bak")
        backup.write_text(original, encoding="utf-8")
        target.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return ToolResult.failure(f"could not write {target}: {exc}")
    changed = occurrences if all_matches else 1
    return ToolResult.success(
        f"Patched {target.name} ({changed} replacement{'s' if changed != 1 else ''}); "
        f"original saved as {backup.name}.",
        data={"path": str(target), "backup": str(backup)},
    )


@tool(
    name="run_python_file",
    description="Run a Python file and return its output (asks for confirmation first).",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "args": {"type": "string", "description": "optional space separated arguments"},
        },
        "required": ["path"],
    },
    category="coding",
    dangerous=True,
    aliases=("run_file", "execute_python"),
)
def run_python_file(path: str, args: str = "", ctx: Optional[ToolContext] = None) -> ToolResult:
    try:
        target = resolve_in_sandbox(path, ctx)
    except (ValueError, PermissionError) as exc:
        return ToolResult.failure(str(exc))
    if not target.is_file():
        return ToolResult.failure(f"there is no file at {target}")

    extra_args = [part for part in str(args or "").split() if part]
    if any(char in "".join(extra_args) for char in ";&|`$><\n"):
        return ToolResult.failure("those arguments contain unsafe characters")

    timeout = 60.0
    if ctx is not None and getattr(ctx, "settings", None) is not None:
        timeout = max(10.0, float(ctx.settings.safety.tool_timeout) * 3)

    try:
        completed = subprocess.run(
            [sys.executable, str(target), *extra_args],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(target.parent),
        )
    except subprocess.TimeoutExpired:
        return ToolResult.failure(f"{target.name} did not finish within {timeout:.0f} seconds")
    except OSError as exc:
        return ToolResult.failure(f"could not run {target.name}: {exc}")

    stdout = (completed.stdout or "").strip()[:4000]
    stderr = (completed.stderr or "").strip()[:2000]
    if completed.returncode == 0:
        body = stdout or "(no output)"
        return ToolResult.success(f"{target.name} exited successfully:\n{body}", data={"stdout": stdout})
    detail = stderr or stdout or "(no output)"
    return ToolResult.failure(
        f"{target.name} exited with code {completed.returncode}:\n{detail}",
        data={"returncode": completed.returncode, "stderr": stderr},
    )


__all__ = ["analyze_error", "create_script", "explain_code", "patch_file", "run_python_file"]
