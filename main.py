#!/usr/bin/env python3
"""JARVIS entry point.

    python main.py                # web interface (open it from your phone too)
    python main.py --check        # diagnose your install and configuration
    python main.py --cli          # text-only terminal session
    python main.py --gui          # Tkinter window (needs a display)
    python main.py --web --port 9000 --token mysecret

Nothing here is required on the Pi's desktop - the web interface is the primary
front-end because it works from a phone, a laptop and the Pi itself.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path
from typing import Any, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

VERSION = "2.0.0"

BANNER = r"""
   _   _   _  _   _ ___ ___
  | | /_\ | \| | | | __/ __|
  | |/ _ \| .` |_| | _|\__ \
  |_/_/ \_\_|\_|\___/|___/__/
"""


# --------------------------------------------------------------------------- #
# arguments
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description="JARVIS - a personal AI assistant for Raspberry Pi 4.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python main.py --check\n"
            "  python main.py --web --port 8765\n"
            "  python main.py --cli\n"
            "  python main.py --gui\n"
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--web", action="store_true", help="serve the browser interface (default)")
    mode.add_argument("--gui", action="store_true", help="open the Tkinter desktop window")
    mode.add_argument("--cli", action="store_true", help="text-only terminal session")
    mode.add_argument("--check", "--doctor", action="store_true", dest="check", help="diagnose the install and exit")

    parser.add_argument("--host", default=None, help="bind address (default: from .env, 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="port (default: from .env, 8765)")
    parser.add_argument("--token", default=None, help="shared token required by the API")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    parser.add_argument("--no-voice", action="store_true", help="disable speech output")
    parser.add_argument("--no-stt", action="store_true", help="disable microphone input")
    parser.add_argument("--with-stt", action="store_true", help="force microphone input on")
    parser.add_argument("--config", default=None, help="path to a .env file to load instead of the defaults")
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING or ERROR")
    parser.add_argument("--version", action="version", version=f"JARVIS {VERSION}")
    return parser


def load_settings(args: argparse.Namespace):
    from config.settings import Settings

    settings = Settings.load(root=PROJECT_ROOT)
    if args.host:
        settings.web.host = args.host
    if args.port:
        settings.web.port = int(args.port)
    if args.token is not None:
        settings.web.token = args.token
    if args.no_browser:
        settings.web.open_browser = False
    if args.no_voice:
        settings.tts.enabled = False
    if args.no_stt:
        settings.stt.enabled = False
    if args.with_stt:
        settings.stt.enabled = True
    return settings


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
def run_doctor(settings: Any) -> int:
    """Print a plain-language health report.  Returns a process exit code."""
    ok = True
    print(BANNER)
    print(f"JARVIS {VERSION} - diagnostics\n" + "=" * 58)

    # --- python ---------------------------------------------------------
    print(f"\nPython            : {sys.version.split()[0]} ({sys.executable})")
    print(f"Platform          : {sys.platform} / {os.uname().machine if hasattr(os, 'uname') else 'unknown'}")
    if sys.version_info < (3, 9):
        print("  ! Python 3.9 or newer is required")
        ok = False

    # --- project layout ------------------------------------------------
    print(f"Project root      : {settings.paths.root}")
    print(f"Environment files : {', '.join(settings.env_files) or 'none found (' + 'env.example' + ' exists as a template)'}")
    for label, path in (
        ("data dir", settings.paths.data_dir),
        ("logs dir", settings.paths.logs_dir),
        ("generated images", settings.paths.generated_dir),
        ("voice cache", settings.paths.voice_cache_dir),
    ):
        writable = False
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            writable = True
        except OSError:
            writable = False
        print(f"{label:<18}: {path} {'[writable]' if writable else '[NOT WRITABLE]'}")
        ok = ok and writable

    # --- dependencies ---------------------------------------------------
    print("\nOptional dependencies")
    packages = [
        ("fastapi", "fastapi", "web interface", True),
        ("uvicorn", "uvicorn", "web server", True),
        ("websockets", "websockets", "live console updates (WebSocket)", False),
        ("dotenv", "python-dotenv", ".env support", False),
        ("psutil", "psutil", "accurate system metrics", False),
        ("speech_recognition", "speech_recognition", "microphone input", False),
        ("pyaudio", "pyaudio", "microphone device access", False),
        ("edge_tts", "edge_tts", "natural neural voice", False),
        ("pygame", "pygame", "audio playback", False),
        ("pyttsx3", "pyttsx3", "offline voice", False),
        ("pypdf", "pypdf", "PDF reading", False),
        ("gpiozero", "gpiozero", "real GPIO hardware", False),
        ("vosk", "vosk", "offline speech recognition", False),
    ]
    from config.settings import module_present

    for module, package, purpose, required in packages:
        present = module_present(module, package)
        mark = "yes" if present else ("MISSING" if required else "optional")
        print(f"  {package:<18} {mark:<8} {purpose}")
        if required and not present:
            ok = False

    # --- providers ------------------------------------------------------
    print("\nAI providers (in fallback order)")
    missing = []
    for provider in settings.ai.providers:
        if provider.keyless:
            state = "ready (no key needed)"
        elif provider.usable:
            state = "ready"
        else:
            state = f"needs {provider.key_env or 'a key'}"
            missing.append(provider.key_env or provider.slug)
        print(f"  {provider.slug:<13} {provider.model:<42} {state}")
    if missing:
        print(f"  one key is enough for the whole chain - any of: {', '.join(missing)}")
    if not any(p.usable and p.slug != "offline" for p in settings.ai.providers):
        print("  ! no online provider is configured - JARVIS will answer offline only")
    print("  offline      always available (rules engine, no network needed)")

    # --- features -------------------------------------------------------
    print("\nFeatures")
    print(f"  web search    : {settings.search.active}")
    print(f"  image gen     : {settings.image.provider}")
    print(f"  email         : {'configured' if settings.email.configured else 'not configured'}")
    print(f"  voice output  : {'enabled' if settings.tts.enabled else 'disabled'} (engine: {settings.tts.engine})")
    print(f"  voice input   : {'enabled' if settings.stt.enabled else 'disabled'} (engine: {settings.stt.engine})")
    from tools.media import playerctl_path

    media_ready = playerctl_path() is not None
    print(
        "  media control : "
        + ("playerctl available (play/pause/skip/volume)" if media_ready
           else "playerctl MISSING - sudo apt install playerctl")
    )
    print(
        "  spotify link  : "
        + ("linked (liked songs + playback control)" if settings.spotify.configured
           else "not linked (optional - scripts/spotify_auth.py)")
    )
    print('  visitors      : ready (say "the chief minister is here")')
    print("  text chat     : always available (a microphone is never required)")
    if not settings.stt.enabled:
        print(
            "                  no microphone needed - type every message; voice input is a bonus"
        )
    print(f"  web interface : http://{settings.web.host}:{settings.web.port}"
          f"{' (token required)' if settings.web.token else ''}")

    # --- live checks ----------------------------------------------------
    print("\nLive checks")
    from tools.base import all_tools

    print(f"  tools registered : {len(all_tools())}")

    online = False
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=2.0).close()
        online = True
    except Exception:
        online = False
    print(f"  internet         : {'reachable' if online else 'NOT reachable (offline mode)'}")

    try:
        from voice.tts import build_engine

        engine = build_engine(settings.tts.engine, settings)
        print(f"  speech output    : {engine.name}")
    except Exception as exc:  # noqa: BLE001
        print(f"  speech output    : unavailable ({exc})")

    try:
        from voice.stt import build_engine as build_stt

        stt = build_stt(settings)
        print(f"  speech input     : {stt.name}")
    except Exception as exc:  # noqa: BLE001
        print(f"  speech input     : unavailable ({exc})")

    try:
        from hardware.gpio import HardwareManager

        hardware = HardwareManager(settings.paths.hardware_config)
        status = hardware.status()
        print(f"  gpio backend     : {status['backend']} ({status['reason']})")
        print(f"  declared devices : {len(status['devices'])}")
    except Exception as exc:  # noqa: BLE001
        print(f"  gpio backend     : unavailable ({exc})")

    # --- summary --------------------------------------------------------
    print("\n" + "=" * 58)
    if ok:
        print("Result: ready. Start it with:  python main.py")
    else:
        print("Result: problems found above. Fix them, then run --check again.")
        print(virtualenv_hint(), end="")
    print("Next steps:")
    print("  1. copy env.example to .env and add at least one AI provider key")
    print(f"  2. python main.py            (web interface on port {settings.web.port})")
    print("  3. open the printed LAN address from your phone")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# modes
# --------------------------------------------------------------------------- #
def virtualenv_hint() -> str:
    """Explain the system-Python mistake, when that is what happened.

    "FastAPI is not installed" on a machine where it clearly *is* installed means
    ``main.py`` was started with ``/usr/bin/python`` instead of the project's
    virtual environment - VS Code's Run button does exactly that until an
    interpreter has been selected, and so does a bare ``python main.py`` in a
    fresh terminal.  Naming both interpreters turns a dead end into a one-line
    fix, so every message about a missing package carries it.
    """
    # ``sys.prefix != sys.base_prefix`` is the canonical "am I in a virtual
    # environment" test, and it is the one that works here: .venv/bin/python is a
    # symlink to the system interpreter, so comparing the two paths *resolved*
    # would find them identical and stay silent about the very mistake it exists
    # to name.  Any virtual environment counts as fine - only the bare system
    # Python is worth blaming.
    if sys.prefix != sys.base_prefix:
        return ""
    venv = PROJECT_ROOT / ".venv" / "bin" / "python"
    try:
        if not venv.exists():
            return ""
    except OSError:  # pragma: no cover - defensive
        return ""
    return (
        f"Looks like the wrong Python: this is {sys.executable}, but the packages\n"
        f"are installed in {venv}.\n"
        "Start JARVIS with the virtual environment:  .venv/bin/python main.py\n"
    )


def port_is_busy(host: str, port: int) -> bool:
    """True when something is already listening on this port.

    uvicorn answers a taken port with ``sys.exit(1)``, which reaches a debugger
    as a bare "SystemExit: 1" and tells you nothing about the cause.  Asking
    before we hand over lets us say what is actually wrong - almost always a
    JARVIS instance that is already running.
    """
    probe_host = host if host and host not in ("0.0.0.0", "::", "*") else "127.0.0.1"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.4)
            return probe.connect_ex((probe_host, int(port))) == 0
    except OSError:  # pragma: no cover - defensive
        return False


def run_web(settings: Any, args: argparse.Namespace) -> int:
    try:
        from server.app import HAVE_FASTAPI, lan_address, serve
    except Exception as exc:  # noqa: BLE001
        print(f"Could not load the web server: {exc}")
        print(virtualenv_hint(), end="")
        return 1
    if not HAVE_FASTAPI:
        print(
            "FastAPI is not installed, so the web interface cannot start.\n"
            "Install the required packages:  pip install -r requirements.txt\n"
            "Or use one of the other front-ends:  python main.py --gui | --cli"
        )
        print(virtualenv_hint(), end="")
        return 1

    print(BANNER)
    print(f"JARVIS {VERSION} - web interface")
    print(f"  local   : http://localhost:{settings.web.port}/")
    print(f"  network : {lan_address(settings)}")
    if settings.web.token:
        print("  a token is required (JARVIS_WEB_TOKEN is set)")
    else:
        print("  tip: set JARVIS_WEB_TOKEN in .env before exposing this on a network")

    port = int(settings.web.port)
    if port_is_busy(settings.web.host, port):
        print(
            f"\nPort {port} is already in use, so this instance cannot start.\n"
            f"If JARVIS is already running there is nothing to do - open it:\n"
            f"  http://localhost:{port}/\n"
            f"Otherwise, find and stop whatever holds the port:\n"
            f"  sh scripts/cloud.sh --stop   # a JARVIS started in the background\n"
            f"  ss -ltnp | grep {port}       # any other process"
        )
        return 1

    print("\nPress Ctrl+C to stop.\n")
    try:
        serve(settings, open_browser=settings.web.open_browser)
    except KeyboardInterrupt:
        print("\nshutting down")
    return 0


def run_cli(settings: Any, args: argparse.Namespace) -> int:
    from core.brain import Jarvis
    from core.logging_setup import setup_logging

    setup_logging(settings.paths.logs_dir, args.log_level)
    jarvis = Jarvis(settings, with_tts=not args.no_voice, with_stt=settings.stt.enabled)
    jarvis.start()
    print(BANNER)
    print(f"JARVIS {VERSION} - terminal session ({jarvis.chain_description()})")
    print("Type 'exit' or press Ctrl+C to quit.\n")
    try:
        while True:
            try:
                text = input("you > ").strip()
            except EOFError:
                break
            if text.lower() in {"exit", "quit", ":q"}:
                break
            if not text:
                continue
            reply = jarvis.handle(text, "cli")
            label = "jarvis" if not reply.error else "jarvis!"
            print(f"{label} > {reply.text}\n")
            if reply.pending:
                print("      (waiting for a yes/no confirmation)")
    except KeyboardInterrupt:
        print()
    finally:
        jarvis.stop()
    return 0


def run_gui(settings: Any, args: argparse.Namespace) -> int:
    from gui.desktop import launch

    launch(settings)
    return 0


def reexec_in_project_venv(venv_python: Optional[Path] = None) -> None:
    """Hand over to the project's virtual environment when started outside it.

    ``python main.py`` on the system Python - and VS Code's Run button before an
    interpreter has been picked - both stop at "FastAPI is not installed" while
    the packages sit unused in ``.venv``.  Re-running this same script with the
    venv's interpreter makes every way of starting JARVIS work, not only the ones
    that remember to name the venv.  ``os.execve`` keeps the process, the
    terminal and the signals, so the exit code and ``Ctrl+C`` behave as before.

    Three guards, because this replaces the running process:

    * already inside a virtual environment - nothing to hand over to;
    * ``JARVIS_REEXEC=1`` in the environment - the hand-over already happened,
      so an empty venv can never loop (set it to keep the interpreter you chose);
    * a debugger is attached - replacing the process would drop its breakpoints,
      so ``main.py --help``-style debugging stays the interpreter's business.
    """
    if os.environ.get("JARVIS_REEXEC") == "1" or sys.prefix != sys.base_prefix:
        return
    if sys.gettrace() is not None:
        return
    if venv_python is None:
        venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    venv_python = Path(venv_python)
    try:
        if not venv_python.exists():
            return
    except OSError:  # pragma: no cover - defensive
        return

    argv = [str(venv_python), os.path.abspath(sys.argv[0] or "main.py")]
    argv.extend(sys.argv[1:])
    env = dict(os.environ)
    env["JARVIS_REEXEC"] = "1"
    print(f"-> switching to the project environment: {venv_python}")
    sys.stdout.flush()
    try:
        os.execve(str(venv_python), argv, env)
    except OSError as exc:  # pragma: no cover - defensive
        print(f"(could not switch to {venv_python}: {exc})")


# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:  # the real command line, not a library call
        reexec_in_project_venv()
    parser = build_parser()
    args = parser.parse_args(argv)

    from config.settings import load_dotenv

    if args.config:
        load_dotenv(Path(args.config).parent, override=True)
    settings = load_settings(args)

    from core.logging_setup import get_logger, setup_logging

    setup_logging(settings.paths.logs_dir, args.log_level)
    get_logger("main").info(
        "JARVIS %s starting (mode=%s)", VERSION, "check" if args.check else "run"
    )

    if args.check:
        return run_doctor(settings)
    if args.gui:
        return run_gui(settings, args)
    if args.cli:
        return run_cli(settings, args)
    return run_web(settings, args)


if __name__ == "__main__":
    sys.exit(main())
