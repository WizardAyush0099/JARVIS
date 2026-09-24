"""Hardware layer.

The spec asks for a clean seam between thinking and wiring:

    AI layer -> tool layer -> hardware layer -> GPIO

So nothing above this module imports ``gpiozero`` or ``RPi.GPIO``.  Devices are
*declared* in ``config/hardware.json`` (id, kind, pin, options) and addressed by
id, which means adding an LED later is a JSON edit plus a restart, not a code
change - and on a laptop or a desktop the whole thing runs on a mock backend so
development never needs the Pi.
"""

from __future__ import annotations

import json
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import module_present
from core.logging_setup import get_logger
from tools.base import ToolContext, ToolResult, tool

log = get_logger("hardware")

DEFAULT_CONFIG: Dict[str, Any] = {
    "_comment": (
        "Declare your Raspberry Pi hardware here. Every device is addressed by "
        "'id'. Supported kinds: led, button, relay, buzzer, servo, sensor."
    ),
    "backend": "auto",
    "devices": [
        {"id": "status_led", "name": "Status LED", "kind": "led", "pin": 17, "active_high": True},
        {
            "id": "button",
            "name": "Push button",
            "kind": "button",
            "pin": 27,
            "pull_up": True,
            "bounce_time": 0.1,
        },
    ],
}


# --------------------------------------------------------------------------- #
# specs
# --------------------------------------------------------------------------- #
@dataclass
class DeviceSpec:
    id: str
    name: str = ""
    kind: str = "led"
    pin: Optional[int] = None
    options: Dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.name or self.id

    def describe(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.label, "kind": self.kind, "pin": self.pin}


# --------------------------------------------------------------------------- #
# backends
# --------------------------------------------------------------------------- #
class HardwareBackend(ABC):
    name = "abstract"

    @abstractmethod
    def setup(self, spec: DeviceSpec) -> None: ...

    @abstractmethod
    def read(self, spec: DeviceSpec) -> Any: ...

    @abstractmethod
    def write(self, spec: DeviceSpec, value: Any) -> Any: ...

    def close(self) -> None:  # pragma: no cover - optional
        return None


class MockBackend(HardwareBackend):
    """In-memory stand-in: lets the whole assistant be developed off-hardware.

    It deliberately mirrors the real backend's *rules* - read-only kinds refuse a
    write, outputs start off - so development does not quietly pass where a
    Raspberry Pi would fail.  Every value it reports is labelled as simulated by
    the tools above it.
    """

    name = "mock"

    #: kinds that can only be read (a Pi cannot push a button)
    READ_ONLY_KINDS = frozenset({"button", "temperature", "distance", "light", "motion", "sensor"})
    #: what a device starts at when hardware.json declares no "initial"
    DEFAULTS: Dict[str, Any] = {"temperature": 21.5, "distance": 42.0, "light": 0.4, "motion": False, "button": False}

    def __init__(self) -> None:
        self._values: Dict[str, Any] = {}
        self._lock = threading.RLock()

    def setup(self, spec: DeviceSpec) -> None:
        with self._lock:
            if spec.id not in self._values:
                if "initial" in spec.options:
                    self._values[spec.id] = spec.options["initial"]
                else:
                    self._values[spec.id] = self.DEFAULTS.get(spec.kind, False)

    def read(self, spec: DeviceSpec) -> Any:
        if spec.id not in self._values:
            self.setup(spec)
        with self._lock:
            return self._values.get(spec.id)

    def write(self, spec: DeviceSpec, value: Any) -> Any:
        if spec.kind in self.READ_ONLY_KINDS:
            raise ValueError(f"'{spec.id}' is read-only")
        with self._lock:
            self._values[spec.id] = value
        return value


class GpioZeroBackend(HardwareBackend):
    """Real hardware via gpiozero (present on Raspberry Pi OS by default)."""

    name = "gpiozero"

    def __init__(self) -> None:
        import gpiozero  # imported here on purpose: only touched when used

        self._gpiozero = gpiozero
        self._devices: Dict[str, Any] = {}

    def setup(self, spec: DeviceSpec) -> None:
        if spec.id in self._devices:
            return
        gz = self._gpiozero
        options = dict(spec.options)
        pin = spec.pin
        kind = spec.kind
        options.pop("initial", None)

        if kind in {"led", "buzzer", "relay"}:
            device = gz.LED(pin, active_high=bool(options.get("active_high", True)))
        elif kind == "button":
            device = gz.Button(
                pin,
                pull_up=bool(options.get("pull_up", True)),
                bounce_time=float(options.get("bounce_time", 0.05)),
            )
        elif kind == "servo":
            device = gz.Servo(pin, min_pulse_width=0.0005, max_pulse_width=0.0025)
        elif kind == "motion":
            device = gz.MotionSensor(pin)
        elif kind == "distance":
            device = gz.DistanceSensor(
                echo=pin, trigger=int(options.get("trigger_pin", (pin or 0) + 1))
            )
        elif kind == "light":
            device = gz.LightSensor(pin)
        elif kind == "temperature":
            from w1thermsensor import W1ThermSensor  # type: ignore

            device = W1ThermSensor()
        else:
            raise ValueError(f"unsupported device kind '{kind}'")
        self._devices[spec.id] = device

    def read(self, spec: DeviceSpec) -> Any:
        device = self._devices.get(spec.id)
        if device is None:
            self.setup(spec)
            device = self._devices.get(spec.id)
        if spec.kind == "button":
            return bool(device.is_pressed)
        if spec.kind == "temperature":
            return round(float(device.get_temperature()), 2)
        if spec.kind == "distance":
            return round(float(device.distance * 100), 1)  # cm
        if spec.kind == "light":
            return round(float(device.value), 3)
        if spec.kind == "motion":
            return bool(device.motion_detected)
        if hasattr(device, "value"):
            return device.value
        return None

    def write(self, spec: DeviceSpec, value: Any) -> Any:
        device = self._devices.get(spec.id)
        if device is None:
            self.setup(spec)
            device = self._devices.get(spec.id)
        if spec.kind in {"led", "buzzer", "relay"}:
            if value in (True, 1, "on", "1"):
                device.on()
            elif value in (False, 0, "off", "0"):
                device.off()
            else:
                device.value = max(0.0, min(1.0, float(value)))
            return value
        if spec.kind == "servo":
            angle = max(0.0, min(180.0, float(value)))
            device.angle = angle
            return angle
        raise ValueError(f"'{spec.id}' is read-only")

    def close(self) -> None:
        for device in self._devices.values():
            try:
                device.close()
            except Exception:
                pass
        self._devices.clear()


# --------------------------------------------------------------------------- #
# manager
# --------------------------------------------------------------------------- #
class HardwareManager:
    """Loads device declarations and dispatches to whichever backend works."""

    def __init__(
        self,
        config_path: Optional[Path] = None,
        events: Any = None,
        force_mock: bool = False,
    ) -> None:
        self.config_path = Path(config_path) if config_path else None
        self.events = events
        self.force_mock = force_mock
        self.devices: Dict[str, DeviceSpec] = {}
        self._lock = threading.RLock()
        self.backend: HardwareBackend = MockBackend()
        self.backend_reason = "mock backend (development mode)"
        self._load_config()
        self._select_backend()

    # -- config ------------------------------------------------------------
    def _load_config(self) -> None:
        payload: Dict[str, Any] = {}
        if self.config_path and self.config_path.exists():
            try:
                payload = json.loads(self.config_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.warning("could not read %s: %s", self.config_path, exc)
                payload = {}
        elif self.config_path:
            try:
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                self.config_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
                payload = dict(DEFAULT_CONFIG)
                log.info("wrote a starter hardware config to %s", self.config_path)
            except OSError:
                payload = dict(DEFAULT_CONFIG)
        if not payload:
            payload = dict(DEFAULT_CONFIG)

        for entry in payload.get("devices", []) or []:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            spec = DeviceSpec(
                id=str(entry["id"]),
                name=str(entry.get("name", "")),
                kind=str(entry.get("kind", "led")).lower(),
                pin=entry.get("pin"),
                options={
                    key: value
                    for key, value in entry.items()
                    if key not in {"id", "name", "kind", "pin"}
                },
            )
            self.devices[spec.id] = spec
        self.configured_backend = str(payload.get("backend", "auto")).lower()

    def _select_backend(self) -> None:
        if self.force_mock or self.configured_backend == "mock":
            self.backend = MockBackend()
            self.backend_reason = "mock backend (requested)"
            return
        try:
            if not module_present("gpiozero"):
                raise ImportError("gpiozero is not installed")
            backend = GpioZeroBackend()
            self.backend = backend
            self.backend_reason = "gpiozero"
            log.info("hardware backend: gpiozero")
        except Exception as exc:  # not a Pi, or gpiozero missing
            self.backend = MockBackend()
            self.backend_reason = f"mock backend ({exc})"
            log.info("hardware backend: mock (%s)", exc)

    # -- api ---------------------------------------------------------------
    @property
    def using_real_hardware(self) -> bool:
        return not isinstance(self.backend, MockBackend)

    def list_devices(self) -> List[Dict[str, Any]]:
        return [spec.describe() for spec in self.devices.values()]

    def known_devices(self) -> str:
        """Human-readable list of device ids for error messages."""
        return ", ".join(f"{spec.id} ({spec.label})" for spec in self.devices.values()) or "none"

    def get(self, device_id: str) -> Optional[DeviceSpec]:
        """Exact lookup by id (also accepts a different case)."""
        if not device_id:
            return None
        return self.devices.get(device_id) or self.devices.get(str(device_id).strip().lower())

    #: filler words a spoken device name arrives wrapped in
    _DEVICE_STOPWORDS = frozenset(
        {
            "the", "my", "our", "a", "an", "is", "are", "be", "please", "now",
            "current", "currently", "value", "reading", "level", "state", "status",
            "of", "to", "at", "it", "device",
        }
    )

    def resolve(self, query: str) -> Optional[DeviceSpec]:
        """Find a device from a spoken name: an id, a name fragment, or a kind.

        "status_led", "Status LED", "the status led" and (with no name match) the
        kind "led" all reach the same declaration, so a voice command never
        depends on the exact id chosen in ``config/hardware.json``.
        """
        if query is None:
            return None
        raw = str(query).strip().lower()
        if not raw:
            return None
        exact = self.get(raw)
        if exact is not None:
            return exact

        words = [
            word for word in re.split(r"[^a-z0-9]+", raw) if word and word not in self._DEVICE_STOPWORDS
        ]
        cleaned = " ".join(words)
        if not cleaned:
            return None
        compact = cleaned.replace(" ", "")
        specs = list(self.devices.values())

        for spec in specs:
            if cleaned in {spec.id.lower(), spec.label.lower()}:
                return spec
        for spec in specs:
            name = spec.label.lower()
            if cleaned in name or name in cleaned or compact == spec.id.lower().replace("_", ""):
                return spec
        for spec in specs:
            if spec.kind == cleaned or spec.kind in words:
                return spec
        return None

    def read(self, device_id: str) -> Any:
        spec = self.resolve(device_id)
        if spec is None:
            raise KeyError(f"unknown device '{device_id}'")
        with self._lock:
            return self.backend.read(spec)

    def write(self, device_id: str, value: Any) -> Any:
        spec = self.resolve(device_id)
        if spec is None:
            raise KeyError(f"unknown device '{device_id}'")
        with self._lock:
            return self.backend.write(spec, value)

    def pulse(self, device_id: str, seconds: float = 0.5) -> None:
        self.write(device_id, True)
        time.sleep(max(0.01, min(float(seconds), 10.0)))
        self.write(device_id, False)

    def status(self) -> Dict[str, Any]:
        return {
            "backend": self.backend.name,
            "reason": self.backend_reason,
            "real_hardware": self.using_real_hardware,
            "devices": self.list_devices(),
        }

    def close(self) -> None:
        try:
            self.backend.close()
        except Exception:  # pragma: no cover
            pass


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
def _manager(ctx: Optional[ToolContext]) -> Optional[HardwareManager]:
    manager = getattr(ctx, "hardware", None) if ctx is not None else None
    return manager if isinstance(manager, HardwareManager) else None


def _display(value: Any) -> str:
    """Render a reading the way a person (or a voice) would say it."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, dict):
        if "value" in value:
            return _display(value["value"])
        return ", ".join(f"{key}={_display(item)}" for key, item in value.items())
    return str(value)


@tool(
    name="hardware_list",
    description="List the declared GPIO devices and whether real hardware is attached.",
    parameters={"type": "object", "properties": {}},
    category="hardware",
    offline_safe=True,
    aliases=("list_devices", "gpio_list"),
)
def hardware_list(ctx: Optional[ToolContext] = None) -> ToolResult:
    manager = _manager(ctx)
    if manager is None:
        return ToolResult.failure("the hardware layer is not loaded")
    status = manager.status()
    if not status["devices"]:
        return ToolResult.success(
            f"No devices are declared. Backend: {status['backend']} ({status['reason']}). "
            "Add devices in config/hardware.json."
        )
    lines = [f"Hardware backend: {status['backend']} ({status['reason']})"]
    for device in status["devices"]:
        pin = f"pin {device['pin']}" if device["pin"] is not None else "no pin"
        lines.append(f"  {device['id']} - {device['name']} ({device['kind']}, {pin})")
    return ToolResult.success("\n".join(lines), data=status)


@tool(
    name="hardware_read",
    description="Read a sensor, button or output state by device id.",
    parameters={
        "type": "object",
        "properties": {"device": {"type": "string", "description": "device id from hardware.json"}},
        "required": ["device"],
    },
    category="hardware",
    offline_safe=True,
    aliases=("read_sensor", "gpio_read"),
)
def hardware_read(device: str, ctx: Optional[ToolContext] = None) -> ToolResult:
    manager = _manager(ctx)
    if manager is None:
        return ToolResult.failure("the hardware layer is not loaded")
    spec = manager.resolve(device)
    if spec is None:
        return ToolResult.failure(
            f"I don't have a device called '{device}'. Declared devices: {manager.known_devices()}"
        )
    try:
        value = manager.read(spec.id)
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure(f"could not read {spec.id}: {exc}")
    suffix = " (simulated)" if not manager.using_real_hardware else ""
    return ToolResult.success(
        f"{spec.label} = {_display(value)}{suffix}",
        data={"device": spec.id, "value": value, "kind": spec.kind},
    )


@tool(
    name="hardware_write",
    description="Turn an output on/off or set a level (LED, relay, buzzer, servo) by device id.",
    parameters={
        "type": "object",
        "properties": {
            "device": {"type": "string"},
            "value": {"type": "string", "description": "on/off, 0-1, or 0-180 for a servo"},
        },
        "required": ["device", "value"],
    },
    category="hardware",
    offline_safe=True,
    aliases=("gpio_write", "set_device"),
)
def hardware_write(device: str, value: str, ctx: Optional[ToolContext] = None) -> ToolResult:
    manager = _manager(ctx)
    if manager is None:
        return ToolResult.failure("the hardware layer is not loaded")
    spec = manager.resolve(device)
    if spec is None:
        return ToolResult.failure(
            f"I don't have a device called '{device}'. Declared devices: {manager.known_devices()}"
        )

    converted: Any = value
    lowered = str(value).strip().lower()
    if lowered in {"on", "true", "high", "yes"}:
        converted = True
    elif lowered in {"off", "false", "low", "no"}:
        converted = False
    else:
        try:
            converted = float(lowered)
        except ValueError:
            return ToolResult.failure("use 'on', 'off', or a number (0-1, or 0-180 for a servo)")

    try:
        manager.write(spec.id, converted)
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure(f"could not set {spec.id}: {exc}")
    suffix = " (simulated)" if not manager.using_real_hardware else ""
    return ToolResult.success(
        f"Set {spec.label} to {_display(converted)}{suffix}.",
        data={"device": spec.id, "value": converted},
    )


@tool(
    name="hardware_pulse",
    description="Turn an output on briefly, then off again (e.g. flash an LED).",
    parameters={
        "type": "object",
        "properties": {
            "device": {"type": "string"},
            "seconds": {"type": "number", "description": "how long to stay on (default 0.5)"},
        },
        "required": ["device"],
    },
    category="hardware",
    offline_safe=True,
    aliases=("pulse_device", "flash_led"),
)
def hardware_pulse(device: str, seconds: float = 0.5, ctx: Optional[ToolContext] = None) -> ToolResult:
    manager = _manager(ctx)
    if manager is None:
        return ToolResult.failure("the hardware layer is not loaded")
    spec = manager.resolve(device)
    if spec is None:
        return ToolResult.failure(
            f"I don't have a device called '{device}'. Declared devices: {manager.known_devices()}"
        )
    try:
        duration = max(0.05, min(float(seconds or 0.5), 10.0))
        manager.pulse(spec.id, duration)
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure(f"could not flash {spec.id}: {exc}")
    return ToolResult.success(
        f"Flashed {spec.label} for {duration:g}s.", data={"device": spec.id, "seconds": duration}
    )


__all__ = ["DeviceSpec", "GpioZeroBackend", "HardwareManager", "MockBackend"]
