"""JARVIS tools.

Importing this package registers every capability in the registry.  That is the
whole extension story: drop a new module in here, decorate a function with
``@tool``, import it below - and both the planner and the offline engine can use
it immediately.
"""

from tools import base  # noqa: F401
from tools import coding  # noqa: F401
from tools import email_tool  # noqa: F401
from tools import files  # noqa: F401
from tools import image  # noqa: F401
from tools import system  # noqa: F401
from tools import utilities  # noqa: F401
from tools import web  # noqa: F401

# The GPIO tools live next to their backend (``hardware/gpio.py``) so wiring stays
# in one place.  Importing it here registers them in the same registry as
# everything else - no extra code, and `--check` / the UI see the real tool count.
from hardware import gpio as hardware_gpio  # noqa: F401

from tools.base import (  # noqa: F401
    Tool,
    ToolContext,
    ToolResult,
    all_tools,
    execute,
    get_tool,
    tool,
    tool_names,
    tool_schemas,
)

#: The modules that populate the registry.  Kept as a named tuple so the imports
#: above read as intentional side effects rather than forgotten ones, and so
#: there is exactly one list to extend when a capability is added.
MODULES = (base, coding, email_tool, files, image, system, utilities, web, hardware_gpio)

#: Tools the GPIO layer contributes (handy for diagnostics and the docs).
HARDWARE_TOOL_NAMES = ("hardware_list", "hardware_read", "hardware_write", "hardware_pulse")

__all__ = [
    "HARDWARE_TOOL_NAMES",
    "MODULES",
    "Tool",
    "ToolContext",
    "ToolResult",
    "all_tools",
    "execute",
    "get_tool",
    "tool",
    "tool_names",
    "tool_schemas",
]
