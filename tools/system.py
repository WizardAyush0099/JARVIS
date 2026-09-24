"""Computer control: metrics, processes, applications, URLs and power.

Design notes
------------
* ``psutil`` is used when installed, but every metric has a ``/proc`` or
  ``sysfs`` fallback so JARVIS reports real numbers even on a minimal install.
* Raspberry Pi specifics are handled properly: ``vcgencmd`` / thermal zones for
  temperature, ``/sys/class/power_supply`` for power, and USB power warnings,
  which the old project silently misreported.
* Launching applications is allow-list based and never goes through a shell -
  no ``shell=True``, no string interpolation, no arbitrary command execution.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from typing import Any, Dict, List, Optional, Tuple

from core.logging_setup import get_logger
from tools.base import ToolContext, ToolResult, tool

log = get_logger("tools.system")

# --------------------------------------------------------------------------- #
# optional psutil
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - depends on the host
    import psutil  # type: ignore

    HAVE_PSUTIL = True
except Exception:  # pragma: no cover
    psutil = None  # type: ignore
    HAVE_PSUTIL = False


def is_raspberry_pi() -> bool:
    try:
        model = open("/proc/device-tree/model", "rb").read().decode("utf-8", "replace")
        return "raspberry pi" in model.lower()
    except OSError:
        return "raspberrypi" in platform.uname().release.lower()


# --------------------------------------------------------------------------- #
# metric readers
# --------------------------------------------------------------------------- #
def _cpu_percent() -> Optional[float]:
    if HAVE_PSUTIL:
        try:
            return float(psutil.cpu_percent(interval=0.15))
        except Exception:
            pass
    try:
        def snapshot() -> Tuple[int, int]:
            with open("/proc/stat", "r", encoding="utf-8") as handle:
                fields = handle.readline().split()[1:]
            values = [int(value) for value in fields[:8]]
            idle = values[3] + values[4]
            return sum(values), idle

        total_a, idle_a = snapshot()
        time.sleep(0.15)
        total_b, idle_b = snapshot()
        delta_total = total_b - total_a
        delta_idle = idle_b - idle_a
        if delta_total <= 0:
            return None
        return round(100.0 * (1.0 - delta_idle / delta_total), 1)
    except Exception:
        return None


def _memory() -> Optional[Dict[str, float]]:
    if HAVE_PSUTIL:
        try:
            vm = psutil.virtual_memory()
            swap = psutil.swap_memory()
            return {
                "total_mb": round(vm.total / 1048576, 1),
                "used_mb": round((vm.total - vm.available) / 1048576, 1),
                "percent": round(float(vm.percent), 1),
                "swap_percent": round(float(swap.percent), 1),
            }
        except Exception:
            pass
    try:
        info: Dict[str, float] = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                value = rest.strip().split()[0]
                info[key] = float(value)  # kB
        total = info.get("MemTotal", 0.0) / 1024
        available = info.get("MemAvailable", info.get("MemFree", 0.0)) / 1024
        used = max(0.0, total - available)
        percent = round(100.0 * used / total, 1) if total else 0.0
        return {
            "total_mb": round(total, 1),
            "used_mb": round(used, 1),
            "percent": percent,
            "swap_percent": 0.0,
        }
    except Exception:
        return None


def _disk(path: str = "/") -> Optional[Dict[str, float]]:
    try:
        if HAVE_PSUTIL:
            usage = psutil.disk_usage(path)
        else:
            usage = shutil.disk_usage(path)
        return {
            "total_gb": round(usage.total / 1073741824, 2),
            "used_gb": round(usage.used / 1073741824, 2),
            "free_gb": round(usage.free / 1073741824, 2),
            "percent": round(100.0 * usage.used / usage.total, 1) if usage.total else 0.0,
        }
    except Exception:
        return None


def _temperature() -> Optional[float]:
    if HAVE_PSUTIL:
        try:
            sensors = getattr(psutil, "sensors_temperatures", lambda: {})()
            for readings in sensors.values():
                if readings:
                    return round(float(readings[0].current), 1)
        except Exception:
            pass
    for zone in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            with open(zone, "r", encoding="utf-8") as handle:
                return round(int(handle.read().strip()) / 1000.0, 1)
        except Exception:
            continue
    if shutil.which("vcgencmd"):
        try:
            output = subprocess.run(
                ["vcgencmd", "measure_temp"], capture_output=True, text=True, timeout=5
            ).stdout
            digits = "".join(ch for ch in output if ch.isdigit() or ch == ".")
            if digits:
                return round(float(digits.rstrip(".")), 1)
        except Exception:
            pass
    return None


def _throttled() -> Optional[str]:
    """Raspberry Pi under-voltage / throttling flags - often the real cause of slowness."""
    if not shutil.which("vcgencmd"):
        return None
    try:
        output = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if "=" not in output:
            return None
        flags = int(output.split("=")[1], 16)
        problems: List[str] = []
        if flags & 0x1:
            problems.append("under-voltage detected now")
        if flags & 0x4:
            problems.append("currently throttled")
        if flags & 0x10000:
            problems.append("under-voltage occurred")
        if flags & 0x40000:
            problems.append("throttling occurred")
        return ", ".join(problems) if problems else "power supply healthy"
    except Exception:
        return None


def _battery() -> Optional[Dict[str, Any]]:
    if HAVE_PSUTIL:
        try:
            battery = psutil.sensors_battery()
            if battery is not None:
                return {"percent": round(float(battery.percent), 1), "plugged": bool(battery.power_plugged)}
        except Exception:
            pass
    try:
        base = "/sys/class/power_supply"
        for entry in sorted(os.listdir(base)):
            capacity = os.path.join(base, entry, "capacity")
            if os.path.exists(capacity):
                with open(capacity, "r", encoding="utf-8") as handle:
                    percent = float(handle.read().strip())
                status_path = os.path.join(base, entry, "status")
                status = ""
                if os.path.exists(status_path):
                    with open(status_path, "r", encoding="utf-8") as handle:
                        status = handle.read().strip()
                return {"percent": round(percent, 1), "plugged": status.lower() in {"charging", "full"}}
    except Exception:
        pass
    return None


def _network() -> Dict[str, Any]:
    info: Dict[str, Any] = {"online": False, "hostname": socket.gethostname()}

    # LAN address without sending traffic anywhere
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.5)
        try:
            probe.connect(("8.8.8.8", 80))
            info["lan_ip"] = probe.getsockname()[0]
        finally:
            probe.close()
    except Exception:
        try:
            info["lan_ip"] = socket.gethostbyname(socket.gethostname())
        except Exception:
            info["lan_ip"] = None

    if HAVE_PSUTIL:
        try:
            counters = psutil.net_io_counters()
            info["sent_mb"] = round(counters.bytes_sent / 1048576, 2)
            info["received_mb"] = round(counters.bytes_recv / 1048576, 2)
        except Exception:
            pass
        try:
            stats = psutil.net_if_stats()
            active = [name for name, st in stats.items() if st.isup and name != "lo"]
            info["interfaces_up"] = active
        except Exception:
            pass

    try:
        socket.create_connection(("1.1.1.1", 53), timeout=1.5).close()
        info["online"] = True
    except Exception:
        info["online"] = False
    return info


def _uptime() -> Optional[str]:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            seconds = float(handle.read().split()[0])
        hours, remainder = divmod(int(seconds), 3600)
        minutes = remainder // 60
        return f"{hours}h {minutes}m"
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
@tool(
    name="system_status",
    description="Report system health: CPU, memory, disk, temperature, battery, network or everything.",
    parameters={
        "type": "object",
        "properties": {
            "metric": {
                "type": "string",
                "description": "one of: all, cpu, memory, disk, temperature, battery, network",
            }
        },
    },
    category="system",
    offline_safe=True,
    aliases=("sysinfo", "system_info", "cpu_usage", "memory_usage", "disk_usage", "temperature"),
)
def system_status(metric: str = "all") -> ToolResult:
    metric = (metric or "all").strip().lower()
    if metric in {"cpu_usage", "processor"}:
        metric = "cpu"
    if metric in {"ram", "mem"}:
        metric = "memory"
    if metric in {"temp", "thermal"}:
        metric = "temperature"
    if metric in {"disk_space", "storage", "drive"}:
        metric = "disk"

    lines: List[str] = []
    wanted = {metric} if metric not in {"all", ""} else {"cpu", "memory", "disk", "temperature", "network"}

    if "cpu" in wanted:
        cpu = _cpu_percent()
        load = None
        try:
            load = os.getloadavg()[0]
        except Exception:
            pass
        if cpu is None:
            lines.append("CPU: unavailable on this system")
        else:
            extra = f", load {load:.2f}" if load is not None else ""
            lines.append(f"CPU: {cpu:.0f}% used{extra}")
    if "memory" in wanted:
        memory = _memory()
        lines.append(
            f"Memory: {memory['percent']:.0f}% used ({memory['used_mb']:.0f} of {memory['total_mb']:.0f} MB)"
            if memory
            else "Memory: unavailable on this system"
        )
    if "disk" in wanted:
        disk = _disk()
        lines.append(
            f"Disk: {disk['percent']:.0f}% used, {disk['free_gb']:.1f} GB free of {disk['total_gb']:.1f} GB"
            if disk
            else "Disk: unavailable on this system"
        )
    if "temperature" in wanted:
        temperature = _temperature()
        lines.append(
            f"Temperature: {temperature:.1f} degrees C"
            if temperature is not None
            else "Temperature: not available (no sensor exposed)"
        )
    if "battery" in wanted:
        battery = _battery()
        if battery:
            state = "charging" if battery["plugged"] else "on battery"
            lines.append(f"Battery: {battery['percent']:.0f}% ({state})")
    if "network" in wanted:
        network = _network()
        if metric == "network":
            state = "online" if network["online"] else "offline"
            lines.append(f"Network: {state}, IP {network.get('lan_ip') or 'unknown'}")

    if metric == "all":
        lines.append(f"Uptime: {_uptime() or 'unknown'}")
        if is_raspberry_pi():
            health = _throttled()
            if health:
                lines.append(f"Power: {health}")
    if metric == "network":
        network = _network()
        data: Dict[str, Any] = network
    else:
        data = {
            "cpu_percent": _cpu_percent() if "cpu" in wanted else None,
            "memory": _memory(),
            "disk": _disk(),
            "temperature_c": _temperature(),
            "battery": _battery(),
            "uptime": _uptime(),
        }
    if not lines:
        return ToolResult.failure(f"I don't know how to report '{metric}'")
    return ToolResult.success("\n".join(lines), data=data)


@tool(
    name="top_processes",
    description="List the processes using the most memory or CPU.",
    parameters={
        "type": "object",
        "properties": {
            "by": {"type": "string", "description": "'memory' (default) or 'cpu'"},
            "limit": {"type": "integer", "description": "how many to show (default 5)"},
        },
    },
    category="system",
    offline_safe=True,
)
def top_processes(by: str = "memory", limit: int = 5) -> ToolResult:
    limit = max(1, min(int(limit or 5), 20))
    by = (by or "memory").strip().lower()
    rows: List[Dict[str, Any]] = []

    if HAVE_PSUTIL:
        try:
            for process in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
                try:
                    info = process.info
                    rows.append(
                        {
                            "pid": info.get("pid"),
                            "name": info.get("name") or "?",
                            "rss_mb": round((info.get("memory_info").rss if info.get("memory_info") else 0) / 1048576, 1),
                            "cpu": float(info.get("cpu_percent") or 0.0),
                        }
                    )
                except Exception:
                    continue
        except Exception:
            rows = []

    if not rows and shutil.which("ps"):
        try:
            output = subprocess.run(
                ["ps", "-eo", "pid,comm,rss,%cpu"], capture_output=True, text=True, timeout=10
            ).stdout.splitlines()[1:]
            for line in output:
                parts = line.split(None, 3)
                if len(parts) < 4:
                    continue
                try:
                    rows.append(
                        {
                            "pid": int(parts[0]),
                            "name": parts[1],
                            "rss_mb": round(int(parts[2]) / 1024, 1),
                            "cpu": float(parts[3]),
                        }
                    )
                except ValueError:
                    continue
        except Exception:
            rows = []

    if not rows:
        return ToolResult.failure("I couldn't read the process table on this system")

    key = "cpu" if by.startswith("cpu") else "rss_mb"
    rows.sort(key=lambda row: row.get(key, 0), reverse=True)
    top = rows[:limit]
    label = "CPU" if key == "cpu" else "memory"
    lines = [f"Top {len(top)} processes by {label}:"]
    for row in top:
        lines.append(f"  {row['name']} (pid {row['pid']}) - {row['rss_mb']:.0f} MB, {row['cpu']:.1f}% CPU")
    return ToolResult.success("\n".join(lines), data={"processes": top})


# --------------------------------------------------------------------------- #
# applications
# --------------------------------------------------------------------------- #
_APP_ALIASES: Dict[str, str] = {
    "vs code": "code",
    "vscode": "code",
    "code editor": "code",
    "browser": "chromium-browser",
    "chrome": "chromium-browser",
    "google chrome": "chromium-browser",
    "terminal": "x-terminal-emulator",
    "command prompt": "x-terminal-emulator",
    "files": "thunar",
    "file manager": "thunar",
    "text editor": "gedit",
    "calculator": "galculator",
    "camera": "raspistill",
    "python": "idle3",
    "spotify": "spotify",
    "vlc": "vlc",
    "settings": "gnome-control-center",
}


def _allowed_apps(ctx: Optional[ToolContext]) -> List[str]:
    if ctx is None or getattr(ctx, "settings", None) is None:
        return []
    return [app.lower() for app in getattr(ctx.settings.safety, "allowed_apps", [])]


def _is_allowed(name: str, binary: str, allowed: List[str]) -> bool:
    if not allowed:
        return True
    candidates = {name.lower(), binary.lower()}
    for entry in allowed:
        if entry in candidates:
            return True
        if entry and (entry in name.lower() or entry in binary.lower()):
            return True
    return False


@tool(
    name="open_app",
    description="Launch an installed application (allow-listed), e.g. 'open VS Code'.",
    parameters={
        "type": "object",
        "properties": {"app": {"type": "string", "description": "application name"}},
        "required": ["app"],
    },
    category="system",
    offline_safe=True,
    aliases=("launch_app", "start_app"),
)
def open_app(app: str, ctx: Optional[ToolContext] = None) -> ToolResult:
    name = (app or "").strip()
    if not name:
        return ToolResult.failure("which application should I open?")
    if any(char in name for char in ";&|`$><\n"):
        return ToolResult.failure("that application name contains unsafe characters")

    lowered = name.lower()
    binary = _APP_ALIASES.get(lowered, lowered.replace(" ", ""))
    resolved = shutil.which(binary) or shutil.which(lowered)
    if resolved is None:
        # try the alias table once more with the resolved alias
        for alias, target in _APP_ALIASES.items():
            if alias in lowered:
                resolved = shutil.which(target)
                binary = target
                if resolved:
                    break

    allowed = _allowed_apps(ctx)
    if resolved is None:
        return ToolResult.failure(
            f"I couldn't find an application called '{name}'. Allowed: {', '.join(allowed) or 'any installed app'}"
        )
    if not _is_allowed(lowered, binary, allowed):
        return ToolResult.failure(
            f"'{name}' is not on the allowed list. Add it to ALLOWED_APPS if you want me to launch it."
        )
    try:
        subprocess.Popen(
            [resolved],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure(f"could not start {name}: {exc}")
    log.info("launched application %s", binary)
    return ToolResult.success(f"Opened {name}.", data={"app": binary})


@tool(
    name="close_app",
    description="Close a running application by name.",
    parameters={
        "type": "object",
        "properties": {"app": {"type": "string", "description": "process/application name"}},
        "required": ["app"],
    },
    category="system",
    dangerous=True,
)
def close_app(app: str) -> ToolResult:
    name = (app or "").strip()
    if not name or any(char in name for char in ";&|`$><\n"):
        return ToolResult.failure("that process name is not valid")
    if not shutil.which("pkill"):
        return ToolResult.failure("I can't close applications on this system (pkill is missing)")
    try:
        result = subprocess.run(["pkill", "-f", name], capture_output=True, text=True, timeout=10)
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure(f"could not close {name}: {exc}")
    if result.returncode == 0:
        return ToolResult.success(f"Closed {name}.")
    return ToolResult.failure(f"I couldn't find a running process called '{name}'.")


@tool(
    name="open_url",
    description="Open a web page in the default browser.",
    parameters={
        "type": "object",
        "properties": {"url": {"type": "string", "description": "http(s) URL to open"}},
        "required": ["url"],
    },
    category="system",
    aliases=("open_website", "browse"),
)
def open_url(url: str) -> ToolResult:
    target = (url or "").strip()
    if not target:
        return ToolResult.failure("which page should I open?")
    if not target.startswith(("http://", "https://")):
        target = "https://" + target.lstrip("/")
    if " " in target:
        return ToolResult.failure("that doesn't look like a valid URL")
    opened = False
    try:
        opened = webbrowser.open(target)
    except Exception:
        opened = False
    if not opened and shutil.which("xdg-open"):
        try:
            subprocess.Popen(
                ["xdg-open", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            opened = True
        except Exception:
            opened = False
    if not opened:
        return ToolResult.failure(
            f"I couldn't open a browser here. The link is: {target}"
        )
    return ToolResult.success(f"Opening {target}", data={"url": target})


@tool(
    name="system_power",
    description="Shut down, restart or lock the machine. Always requires confirmation.",
    parameters={
        "type": "object",
        "properties": {"action": {"type": "string", "description": "shutdown, restart, lock or cancel"}},
        "required": ["action"],
    },
    category="power",
    dangerous=True,
    aliases=("shutdown", "restart"),
)
def system_power(action: str) -> ToolResult:
    action = (action or "").strip().lower()
    commands: Dict[str, List[str]] = {}
    if shutil.which("systemctl"):
        commands = {
            "shutdown": ["systemctl", "poweroff"],
            "restart": ["systemctl", "reboot"],
            "lock": ["systemctl", "lock-session"],
            "cancel": ["systemctl", "cancel"],
        }
    elif sys.platform == "darwin":
        commands = {
            "shutdown": ["osascript", "-e", 'tell app "System Events" to shut down'],
            "restart": ["osascript", "-e", 'tell app "System Events" to restart'],
            "lock": ["pmset", "displaysleepnow"],
            "cancel": ["killall", "shutdown"],
        }
    else:
        shutdown_bin = shutil.which("shutdown")
        if shutdown_bin:
            commands = {
                "shutdown": [shutdown_bin, "-h", "now"],
                "restart": [shutdown_bin, "-r", "now"],
                "cancel": [shutdown_bin, "-c"],
            }
    if action not in commands:
        return ToolResult.failure("I can only shut down, restart, lock or cancel a scheduled shutdown")
    try:
        subprocess.Popen(
            commands[action], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure(f"could not {action}: {exc}")
    return ToolResult.success(f"{action.capitalize()} requested.", data={"action": action})


@tool(
    name="set_volume",
    description="Set or change the system output volume.",
    parameters={
        "type": "object",
        "properties": {"level": {"type": "integer", "description": "0-100"}},
        "required": ["level"],
    },
    category="system",
    offline_safe=True,
)
def set_volume(level: int) -> ToolResult:
    try:
        percent = max(0, min(100, int(level)))
    except (TypeError, ValueError):
        return ToolResult.failure("give me a volume between 0 and 100")
    commands = []
    if shutil.which("amixer"):
        commands.append(["amixer", "-q", "sset", "Master", f"{percent}%"])
    if shutil.which("pactl"):
        commands.append(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{percent}%"])
    if not commands:
        return ToolResult.failure("no volume control is available on this system")
    for command in commands:
        try:
            subprocess.run(command, capture_output=True, timeout=5)
            return ToolResult.success(f"Volume set to {percent}%.", data={"level": percent})
        except Exception:
            continue
    return ToolResult.failure("I couldn't change the volume")


@tool(
    name="system_overview",
    description="Describe this machine: OS, CPU model, cores, Python version, Raspberry Pi model.",
    parameters={"type": "object", "properties": {}},
    category="system",
    offline_safe=True,
)
def system_overview() -> ToolResult:
    uname = platform.uname()
    model = None
    try:
        with open("/proc/device-tree/model", "rb") as handle:
            model = handle.read().decode("utf-8", "replace").strip("\x00")
    except OSError:
        model = None
    lines = [
        f"Host: {uname.node}",
        f"OS: {platform.system()} {platform.release()} ({platform.machine()})",
        f"CPU: {platform.processor() or uname.machine} with {os.cpu_count() or '?'} cores",
        f"Python: {platform.python_version()}",
    ]
    if model:
        lines.append(f"Board: {model}")
    lines.append(f"psutil: {'available' if HAVE_PSUTIL else 'not installed (using /proc fallbacks)'}")
    return ToolResult.success("\n".join(lines), data={"uname": list(uname)})


__all__ = ["HAVE_PSUTIL", "is_raspberry_pi"]
