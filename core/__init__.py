"""Core JARVIS internals.

Submodules are imported lazily by callers so that the package stays free of
import cycles (``core.intent`` is used by both the offline engine and the
planner, ``core.brain`` wires everything together).
"""

__all__ = [
    "brain",
    "events",
    "intent",
    "logging_setup",
    "memory",
    "planner",
    "router",
]
