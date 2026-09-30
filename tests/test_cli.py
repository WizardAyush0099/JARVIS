"""The command line entry point.

``main.py`` re-runs itself with the project's ``.venv`` interpreter when it was
started with the system Python.  That is what makes "just run main.py" work in
VS Code without picking an interpreter first, and it is guarded four ways.

Nothing here is ever exec'd: :func:`execve` is replaced by a recorder, so the
tests only assert *whether* a hand-over would happen and with what arguments.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main as jarvis_main  # noqa: E402


class _StopParsing(Exception):
    """Raised by the stubbed parser so main() stops right after the hand-over."""


@pytest.fixture
def venv_python(tmp_path):
    """A stand-in for .venv/bin/python that exists and is executable."""
    path = tmp_path / ".venv" / "bin" / "python"
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def system_python(monkeypatch):
    """Pretend this process is the bare system Python, with no debugger."""
    monkeypatch.setattr(jarvis_main.sys, "prefix", "/usr")
    monkeypatch.setattr(jarvis_main.sys, "base_prefix", "/usr")
    monkeypatch.setattr(jarvis_main.sys, "gettrace", lambda: None)
    monkeypatch.delenv("JARVIS_REEXEC", raising=False)


@pytest.fixture
def execve(monkeypatch):
    """Record hand-over attempts instead of performing them."""
    calls = []
    monkeypatch.setattr(jarvis_main.os, "execve", lambda *args: calls.append(args))
    return calls


def test_hands_over_when_started_with_the_system_python(
    system_python, execve, venv_python, monkeypatch
):
    monkeypatch.setattr(jarvis_main.sys, "argv", [str(PROJECT_ROOT / "main.py"), "--check"])
    monkeypatch.setenv("A_SETTING", "kept")

    jarvis_main.reexec_in_project_venv(venv_python)

    assert len(execve) == 1
    interpreter, argv, env = execve[0]
    assert interpreter == str(venv_python)
    assert argv == [str(venv_python), str(PROJECT_ROOT / "main.py"), "--check"]
    # the child must not hand over again, and must inherit the environment
    assert env["JARVIS_REEXEC"] == "1"
    assert env["A_SETTING"] == "kept"


def test_no_hand_over_inside_a_virtual_environment(monkeypatch, execve, venv_python):
    monkeypatch.setattr(jarvis_main.sys, "prefix", "/somewhere/.venv")
    monkeypatch.setattr(jarvis_main.sys, "base_prefix", "/usr")
    monkeypatch.delenv("JARVIS_REEXEC", raising=False)

    jarvis_main.reexec_in_project_venv(venv_python)

    assert execve == []


def test_no_hand_over_twice(monkeypatch, execve, venv_python, system_python):
    monkeypatch.setenv("JARVIS_REEXEC", "1")

    jarvis_main.reexec_in_project_venv(venv_python)

    assert execve == []


def test_no_hand_over_under_a_debugger(monkeypatch, execve, venv_python, system_python):
    # replacing the process would drop the debugger's breakpoints
    monkeypatch.setattr(jarvis_main.sys, "gettrace", lambda: object())

    jarvis_main.reexec_in_project_venv(venv_python)

    assert execve == []


def test_no_hand_over_without_a_venv(system_python, execve, tmp_path):
    jarvis_main.reexec_in_project_venv(tmp_path / "missing" / "bin" / "python")

    assert execve == []


def test_main_hands_over_only_for_the_real_command_line(monkeypatch):
    handed_over = []

    def _build_parser():
        raise _StopParsing

    monkeypatch.setattr(jarvis_main, "reexec_in_project_venv", lambda: handed_over.append(1))
    monkeypatch.setattr(jarvis_main, "build_parser", _build_parser)

    with pytest.raises(_StopParsing):
        jarvis_main.main([])
    assert handed_over == [], "a library call must not replace the process"

    with pytest.raises(_StopParsing):
        jarvis_main.main()
    assert handed_over == [1], "the command line must hand over to the venv"


def test_default_path_is_the_project_venv(system_python, execve, monkeypatch, tmp_path):
    # no explicit path: the function looks for the project's own .venv
    project = tmp_path / "JARVIS"
    (project / ".venv" / "bin").mkdir(parents=True)
    python = project / ".venv" / "bin" / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(jarvis_main, "PROJECT_ROOT", project)
    monkeypatch.setattr(jarvis_main.sys, "argv", ["main.py"])

    jarvis_main.reexec_in_project_venv()

    interpreter, argv, _env = execve[0]
    assert interpreter == str(python)
    # the script is named absolutely so the child finds it from any directory
    assert os.path.isabs(argv[1])
    assert argv[1].endswith("main.py")
