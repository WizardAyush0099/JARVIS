"""JARVIS hardware layer (Raspberry Pi GPIO, sensors and actuators)."""

from hardware.gpio import HardwareManager  # noqa: F401

__all__ = ["HardwareManager"]
