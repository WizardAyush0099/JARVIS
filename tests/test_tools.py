"""Tool-level tests: maths safety, conversions, reminders, notes and files."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from core.memory import Memory
from tools import base
from tools.base import ToolContext
from tools.files import resolve_in_sandbox
from tools.utilities import (
    CalculationError,
    ReminderScheduler,
    convert_units_values,
    parse_when,
    safe_calculate,
)


@pytest.fixture
def ctx(settings):
    return ToolContext(settings=settings, memory=Memory(settings))


# --------------------------------------------------------------------------- #
# calculator
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "expression,expected",
    [
        ("27*43", 1161),
        ("2**10", 1024),
        ("(3+4)*5", 35),
        ("10/4", 2.5),
        ("17%5", 2),
        ("sqrt(16)+2", 6),
        ("round(3.14159, 2)", 3.14),
        ("pi", pytest.approx(3.14159265, abs=1e-6)),
    ],
)
def test_safe_calculate(expression, expected):
    assert safe_calculate(expression) == expected


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo pwned')",
        "open('/etc/passwd')",
        "eval('1+1')",
        "1 if True else 2",
        "x + 1",
        "2**100000",
        "1/0",
        "",
        "a" * 300,
    ],
)
def test_safe_calculate_rejects_dangerous_input(expression):
    with pytest.raises(CalculationError):
        safe_calculate(expression)


def test_calculate_tool_handles_prose(ctx):
    result = base.execute("calculate", {"expression": "12% of 300"}, ctx=ctx)
    assert result.ok
    assert "36" in result.output


def test_calculate_tool_reports_failure_cleanly(ctx):
    result = base.execute("calculate", {"expression": "hello world"}, ctx=ctx)
    assert not result.ok
    assert "can't calculate" in result.error


# --------------------------------------------------------------------------- #
# conversions
# --------------------------------------------------------------------------- #
def test_length_and_temperature_conversions():
    value, _ = convert_units_values(5, "km", "miles")
    assert value == pytest.approx(3.1069, abs=0.001)

    value, _ = convert_units_values(100, "c", "f")
    assert value == pytest.approx(212)

    value, _ = convert_units_values(0, "c", "k")
    assert value == pytest.approx(273.15)


def test_unknown_conversion_is_an_error():
    with pytest.raises(ValueError):
        convert_units_values(1, "bananas", "apples")


def test_convert_tool_message(ctx):
    result = base.execute("convert_units", {"quantity": "1 kg", "target_unit": "g"}, ctx=ctx)
    assert result.ok and "1,000" in result.output


# --------------------------------------------------------------------------- #
# reminders
# --------------------------------------------------------------------------- #
def test_parse_when_relative():
    text, due = parse_when("call mum in 20 minutes", now=datetime(2026, 1, 1, 12, 0))
    assert text == "call mum"
    assert due == datetime(2026, 1, 1, 12, 20)


def test_parse_when_hours_and_named_gaps():
    _, due = parse_when("stretch in 2 hours", now=datetime(2026, 1, 1, 12, 0))
    assert due == datetime(2026, 1, 1, 14, 0)

    _, due = parse_when("tea in half an hour", now=datetime(2026, 1, 1, 12, 0))
    assert due == datetime(2026, 1, 1, 12, 30)


def test_parse_when_clock_rolls_to_tomorrow():
    _, due = parse_when("wake up at 7:30 am", now=datetime(2026, 1, 1, 12, 0))
    assert due == datetime(2026, 1, 2, 7, 30)


def test_parse_when_without_time_returns_none():
    text, due = parse_when("buy milk")
    assert due is None
    assert text == "buy milk"


def test_reminder_scheduler_fires_due_items(tmp_path):
    fired = []
    scheduler = ReminderScheduler(tmp_path / "reminders.json", on_fire=fired.append, interval=0.05)
    entry = scheduler.add("stretch", datetime.now() - timedelta(seconds=1))
    assert scheduler.list()[0]["id"] == entry["id"]

    due = scheduler.due_now()
    assert len(due) == 1 and due[0]["text"] == "stretch"
    assert scheduler.list() == []

    scheduler.start()
    scheduler.add("drink water", datetime.now() - timedelta(seconds=1))
    deadline = time.time() + 3
    while not fired and time.time() < deadline:
        time.sleep(0.05)
    scheduler.stop()
    assert any("drink water" in message for message in fired)


def test_set_reminder_tool(ctx, settings):
    result = base.execute("set_reminder", {"text": "stretch in 5 minutes"}, ctx=ctx)
    assert result.ok
    listed = base.execute("list_reminders", {}, ctx=ctx)
    assert "stretch" in listed.output


def test_reminder_without_a_time_fails_helpfully(ctx):
    result = base.execute("set_reminder", {"text": "buy milk"}, ctx=ctx)
    assert not result.ok
    assert "when" in result.error


# --------------------------------------------------------------------------- #
# notes
# --------------------------------------------------------------------------- #
def test_notes_round_trip(ctx):
    assert base.execute("add_note", {"text": "buy thermal paste"}, ctx=ctx).ok
    listed = base.execute("list_notes", {}, ctx=ctx)
    assert "buy thermal paste" in listed.output


def test_notes_survive_corrupt_file(ctx, settings):
    settings.paths.notes_file.write_text("{broken", encoding="utf-8")
    assert base.execute("add_note", {"text": "still works"}, ctx=ctx).ok
    assert "still works" in base.execute("list_notes", {}, ctx=ctx).output


# --------------------------------------------------------------------------- #
# files: sandbox
# --------------------------------------------------------------------------- #
def test_write_then_read(tmp_path, ctx):
    target = tmp_path / "notes.txt"
    write = base.execute("write_text_file", {"path": str(target), "content": "line one"}, ctx=ctx)
    assert write.ok
    read = base.execute("read_text_file", {"path": str(target)}, ctx=ctx)
    assert read.ok and "line one" in read.output


def test_relative_paths_resolve_inside_the_sandbox(ctx, tmp_path):
    result = base.execute("write_text_file", {"path": "hello.txt", "content": "hi"}, ctx=ctx)
    assert result.ok
    assert (tmp_path / "hello.txt").exists()


@pytest.mark.parametrize("target", ["/etc/passwd", "../../../../etc/shadow", "/root/.ssh/id_rsa"])
def test_paths_outside_the_sandbox_are_refused(target, ctx):
    with pytest.raises(PermissionError):
        resolve_in_sandbox(target, ctx)
    result = base.execute("read_text_file", {"path": target}, ctx=ctx)
    assert not result.ok


def test_listing_and_searching(ctx, tmp_path):
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "app.py").write_text("def hello():\n    return 'needle'\n", encoding="utf-8")

    listing = base.execute("list_directory", {"path": str(tmp_path)}, ctx=ctx)
    assert listing.ok and "src/" in listing.output

    found = base.execute("search_files", {"query": "needle", "path": str(tmp_path)}, ctx=ctx)
    assert found.ok and "app.py" in found.output


def test_delete_moves_to_trash_instead_of_unlinking(ctx, tmp_path, settings):
    victim = tmp_path / "important.txt"
    victim.write_text("data", encoding="utf-8")

    pending = base.execute("delete_file", {"path": str(victim)}, ctx=ctx)
    assert pending.needs_confirmation, "deleting must ask first"

    forced = base.execute("delete_file", {"path": str(victim)}, ctx=ctx, force=True)
    assert forced.ok
    assert not victim.exists()
    assert list(settings.paths.trash_dir.iterdir()), "the file should be recoverable"


def test_missing_required_argument_is_reported(ctx):
    result = base.execute("write_text_file", {"content": "no path given"}, ctx=ctx)
    assert not result.ok and "missing required argument" in result.error


def test_unknown_tool_is_reported(ctx):
    result = base.execute("does_not_exist", {}, ctx=ctx)
    assert not result.ok and "unknown tool" in result.error


def test_tool_timeout_does_not_hang(ctx):
    import time as time_module

    def slow(ctx=None):  # pragma: no cover - the timeout is what matters
        time_module.sleep(5)
        return "never"

    spec = base.Tool(name="slow_tool", description="sleeps", parameters={"type": "object", "properties": {}}, func=slow)
    try:
        base.register(spec)
        result = base.execute("slow_tool", {}, ctx=ctx, timeout=0.3)
        assert not result.ok and "timed out" in result.error
    finally:
        from tools.base import _REGISTRY

        _REGISTRY.pop("slow_tool", None)


def test_dangerous_tool_requires_confirmation(ctx):
    result = base.execute("system_power", {"action": "shutdown"}, ctx=ctx)
    assert result.needs_confirmation
    assert "shutdown" in result.data["args"]["action"]


def test_confirmation_can_be_disabled(ctx, settings):
    settings.safety.confirm_destructive = False
    spec = base.get_tool("forget_fact")
    assert spec is not None and spec.dangerous
    result = base.execute("forget_fact", {"key": "nothing"}, ctx=ctx)
    # no confirmation prompt: it runs and simply reports that nothing was stored
    assert result.needs_confirmation is None


def test_offline_safe_tools_are_read_only():
    for spec in base.all_tools():
        if spec.offline_safe:
            assert not spec.dangerous, f"{spec.name} is offline-safe but destructive"


def test_core_tools_are_registered():
    names = set(base.tool_names())
    for expected in ("calculate", "system_status", "read_text_file", "web_search", "send_email", "generate_image"):
        assert expected in names
