"""The console script and the page it drives must not drift apart.

A stale cached ``app.js`` meeting a newer ``index.html`` is what produced
"Cannot set properties of null (setting 'textContent')": the script looked an
element up by id, the page had moved it to a class, and the whole render
aborted.  These tests keep the two files honest so that cannot come back.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "gui" / "web"

#: Elements the script creates itself, so they are legitimately absent from the
#: static page.
DYNAMIC_IDS = {"typing-indicator"}


def _read(path: Path) -> str:
    if not path.exists():  # pragma: no cover - only if the UI is deleted
        pytest.skip(f"{path.name} is missing")
    return path.read_text(encoding="utf-8")


def test_every_element_the_script_looks_up_exists_on_the_page():
    ids_in_page = set(re.findall(r'id="([^"]+)"', _read(WEB_DIR / "index.html")))
    referenced = set(re.findall(r'\$\("([^"]+)"\)', _read(WEB_DIR / "app.js")))
    missing = sorted(referenced - ids_in_page - DYNAMIC_IDS)
    assert not missing, f"app.js looks up ids that index.html does not define: {missing}"


def test_the_page_never_writes_text_without_a_guard():
    """An unguarded ``$("x").textContent`` is the exact shape of the old bug."""
    unguarded = re.findall(r'^\s*\$\("([^"]+)"\)\.textContent', _read(WEB_DIR / "app.js"), re.M)
    assert not unguarded, f"guard these with setText() instead: {sorted(set(unguarded))}"


def test_console_assets_are_never_served_from_a_stale_cache():
    """Without this the browser keeps a pre-pull app.js and breaks the page."""
    assert "no-cache" in _read(ROOT / "server" / "app.py")
