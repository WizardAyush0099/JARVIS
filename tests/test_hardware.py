"""Hardware layer: device resolution, the toolkit, and the offline GPIO path.

Everything here runs on the mock backend, so the suite still works on a laptop.
The point of these tests is that wiring stays *behind* the tool layer and that a
spoken device name ("the status led", "room temperature") reaches the right
declaration without the exact id.
"""

from __future__ import annotations

import json

import pytest

from hardware.gpio import HardwareManager
from tools.base import ToolContext
from tools import base

DEVICES = {
    "backend": "auto",
    "devices": [
        {"id": "status_led", "name": "Status LED", "kind": "led", "pin": 17},
        {"id": "button", "name": "Push button", "kind": "button", "pin": 27},
        {"id": "room_temp", "name": "Room temperature", "kind": "temperature", "pin": 4},
    ],
}


@pytest.fixture
def hardware(settings, events, tmp_path):
    config = tmp_path / "hardware.json"
    config.write_text(json.dumps(DEVICES), encoding="utf-8")
    manager = HardwareManager(config, events, force_mock=True)
    yield manager
    manager.close()


@pytest.fixture
def ctx(settings, hardware):
    return ToolContext(settings=settings, events=None, hardware=hardware)


def run(name, args, ctx):
    return base.execute(name, args, ctx=ctx)


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_hardware_tools_are_registered():
    names = set(base.tool_names())
    for expected in ("hardware_list", "hardware_read", "hardware_write", "hardware_pulse"):
        assert expected in names

    for name in ("hardware_list", "hardware_read", "hardware_write", "hardware_pulse"):
        spec = base.get_tool(name)
        assert spec is not None
        assert spec.category == "hardware"
        assert spec.offline_safe, f"{name} touches local GPIO only and must work offline"


def test_hardware_tools_reach_the_planner_catalogue(settings, events):
    from core.planner import Planner

    catalogue = Planner(settings, None, None, events, None).tool_catalogue()
    assert "hardware_list(" in catalogue
    assert "hardware_write(" in catalogue


# --------------------------------------------------------------------------- #
# spoken names
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "spoken,expected",
    [
        ("status_led", "status_led"),
        ("status led", "status_led"),
        ("the Status LED", "status_led"),
        ("led", "status_led"),
        ("button", "button"),
        ("the push button", "button"),
        ("room temperature", "room_temp"),
        ("my room temp", "room_temp"),
        ("temperature", "room_temp"),
    ],
)
def test_spoken_names_resolve_to_declared_devices(hardware, spoken, expected):
    found = hardware.resolve(spoken)
    assert found is not None, f"{spoken!r} should reach a device"
    assert found.id == expected


@pytest.mark.parametrize(
    "spoken", ["", "   ", "wifi", "distance to the moon", "the flux capacitor", "nonexistent"]
)
def test_unrelated_phrases_do_not_resolve(hardware, spoken):
    assert hardware.resolve(spoken) is None


# --------------------------------------------------------------------------- #
# the toolkit
# --------------------------------------------------------------------------- #
def test_list_devices(ctx):
    result = run("hardware_list", {}, ctx)
    assert result.ok
    assert "status_led" in result.output
    assert "room_temp" in result.output
    assert result.data["devices"]


def test_read_a_sensor_by_spoken_name(ctx):
    result = run("hardware_read", {"device": "room temperature"}, ctx)
    assert result.ok
    assert "Room temperature" in result.output
    assert result.data["device"] == "room_temp"
    # mock values are always labelled, never presented as real hardware
    assert "simulated" in result.output


def test_write_and_read_back_an_output(ctx):
    assert run("hardware_write", {"device": "the status led", "value": "on"}, ctx).ok
    assert run("hardware_read", {"device": "status led"}, ctx).output.endswith("= on (simulated)")

    assert run("hardware_write", {"device": "status led", "value": "off"}, ctx).ok
    assert run("hardware_read", {"device": "status led"}, ctx).output.endswith("= off (simulated)")


def test_pulse_reports_the_duration(ctx):
    result = run("hardware_pulse", {"device": "status led", "seconds": 0.05}, ctx)
    assert result.ok
    assert "0.05s" in result.output


def test_unknown_device_fails_with_the_known_list(ctx):
    result = run("hardware_read", {"device": "flux capacitor"}, ctx)
    assert not result.ok
    assert "flux capacitor" in result.error
    assert "status_led" in result.error


def test_reading_a_write_only_target_is_refused_clearly(ctx):
    result = run("hardware_write", {"device": "button", "value": "on"}, ctx)
    assert not result.ok
    assert "button" in result.error


def test_bad_value_is_explained(ctx):
    result = run("hardware_write", {"device": "status led", "value": "sideways"}, ctx)
    assert not result.ok
    assert "'on', 'off'" in result.error


def test_hardware_tools_survive_a_missing_layer(settings):
    # no hardware manager in the context: the tool must answer, not raise
    result = run("hardware_read", {"device": "led"}, ToolContext(settings=settings))
    assert not result.ok
    assert "hardware layer is not loaded" in result.error


# --------------------------------------------------------------------------- #
# offline path
# --------------------------------------------------------------------------- #
def test_offline_engine_runs_hardware_commands(settings, events, hardware):
    from ai.offline import OfflineEngine
    from core.memory import Memory

    engine = OfflineEngine(settings, Memory(settings), events)
    context = ToolContext(settings=settings, hardware=hardware)

    listed = engine.answer("list my hardware devices", context)
    assert listed and "room_temp" in listed

    turned_on = engine.answer("turn on the status led", context)
    assert turned_on and "simulated" in turned_on

    temp = engine.answer("read room temperature", context)
    assert temp and "Room temperature" in temp
