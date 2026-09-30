"""Reading ``.env`` files.

The parser is deliberately tiny, but it has one rule that matters more than it
looks: a line whose value is *only* a comment (``KEY=    # what this is for``)
means "left empty".  Both this parser and python-dotenv used to read it the other
way, which turned the shipped ``env.example`` placeholders into live values -
including a ``JARVIS_WEB_TOKEN``, so a brand-new install refused every request
with a 401 and asked for a token made of comment text.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import _unquote, load_dotenv, parse_env_file  # noqa: E402


@pytest.fixture
def clean_env(monkeypatch):
    """Give the test its own os.environ, restored automatically afterwards."""
    monkeypatch.setattr(os, "environ", dict(os.environ))
    return os.environ


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("value", "value"),
        ("  spaced  ", "spaced"),
        ('"quoted value"', "quoted value"),
        ("'single quoted'", "single quoted"),
        ("value  # trailing comment", "value"),
        # the whole value being a comment means "not set"
        ("               # a comment", ""),
        ("# a comment", ""),
        # a hash inside a value is data, not a comment
        ("abc#def", "abc#def"),
    ],
)
def test_unquote(raw, expected):
    assert _unquote(raw) == expected


def test_parse_env_file_ignores_comments_and_export(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(
            [
                "# a section header",
                "PLAIN=value",
                "export EXPORTED=exported value",
                "QUOTED=\"  keep the spaces  \"",
                "EMPTY=",
                "ANNOTATED=real  # and a note",
                "COMMENTED=    # only a comment",
                "not a pair of lines",
            ]
        ),
        encoding="utf-8",
    )

    parsed = parse_env_file(path)

    assert parsed["PLAIN"] == "value"
    assert parsed["EXPORTED"] == "exported value"
    assert parsed["QUOTED"] == "  keep the spaces  "
    assert parsed["EMPTY"] == ""
    assert parsed["ANNOTATED"] == "real"
    assert parsed["COMMENTED"] == ""
    assert "not a pair of lines" not in parsed


def test_shipped_template_never_turns_a_comment_into_a_value():
    """No line in env.example may parse to something that starts with '#'.

    This is the regression test for the bug that made a fresh install demand
    ``?token=# set to require ?token=...``: the template documents optional
    settings with an inline comment, and a comment is not a value.
    """
    offenders = []
    for line in (PROJECT_ROOT / "env.example").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if "=" not in stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition("=")
        parsed = _unquote(value)
        if parsed.startswith("#"):
            offenders.append(f"{key.strip()} -> {parsed!r}")

    assert not offenders, "these template lines would set a value that is a comment: " + ", ".join(
        offenders
    )


def test_loading_env_treats_a_commented_value_as_unset(clean_env, tmp_path):
    """The bug this file is about, through the real loading path.

    python-dotenv reads this line as a value made of comment text and loads it
    first, so an install that only trusted it ended up with a live
    JARVIS_WEB_TOKEN of ``# set to require ?token=...`` and a console that
    answered every request with 401.
    """
    (tmp_path / ".env").write_text(
        "JARVIS_WEB_TOKEN=               # set to require ?token=... (recommended on LAN)\n"
        "JARVIS_WEB_PORT=8765\n",
        encoding="utf-8",
    )

    assert load_dotenv(tmp_path) == [".env"]

    assert clean_env["JARVIS_WEB_PORT"] == "8765"
    assert clean_env.get("JARVIS_WEB_TOKEN", "") == ""


def test_env_local_wins_and_the_real_environment_wins(clean_env, tmp_path):
    # names that no real environment would already define
    (tmp_path / ".env").write_text("JARVIS_TEST_NAME=from env\nJARVIS_TEST_ONLY=env\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("JARVIS_TEST_NAME=from env.local\n", encoding="utf-8")
    clean_env["JARVIS_TEST_ONLY"] = "already set"
    clean_env["JARVIS_TEST_SHELL"] = "shell wins"

    assert load_dotenv(tmp_path) == [".env", ".env.local"]

    assert clean_env["JARVIS_TEST_NAME"] == "from env.local"  # .env.local overrides .env
    # the real environment is not overwritten by a file
    assert clean_env["JARVIS_TEST_ONLY"] == "already set"
    assert clean_env["JARVIS_TEST_SHELL"] == "shell wins"
