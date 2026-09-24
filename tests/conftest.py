"""Shared fixtures.

Every test runs against temporary paths and *without* API keys, so the suite is
deterministic, makes no network calls and never touches the user's real data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import Settings  # noqa: E402
from core.events import EventBus  # noqa: E402

#: every environment variable that could leak a key into the tests
_SECRET_KEYS = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "TOGETHER_API_KEY",
    "CEREBRAS_API_KEY",
    "MISTRAL_API_KEY",
    "DEEPSEEK_API_KEY",
    "TAVILY_API_KEY",
    "BRAVE_API_KEY",
    "SMTP_PASSWORD",
    "SMTP_USER",
)


@pytest.fixture(autouse=True)
def _no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _SECRET_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AI_PROVIDERS", "gemini,groq,openrouter")
    monkeypatch.setenv("OLLAMA_ENABLED", "false")
    monkeypatch.setenv("EMAIL_ENABLED", "false")
    monkeypatch.setenv("TTS_ENABLED", "false")
    monkeypatch.setenv("STT_ENABLED", "false")
    monkeypatch.setenv("JARVIS_OPEN_BROWSER", "false")
    monkeypatch.delenv("JARVIS_WEB_TOKEN", raising=False)
    monkeypatch.setenv("JARVIS_OWNER", "Ayush")
    monkeypatch.setenv("JARVIS_NAME", "JARVIS")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    loaded = Settings.load(root=tmp_path)
    loaded.safety.allowed_paths = [tmp_path]
    loaded.paths.ensure()
    return loaded


@pytest.fixture
def events() -> EventBus:
    return EventBus(history=50)


@pytest.fixture
def jarvis(settings: Settings, events: EventBus):
    from core.brain import Jarvis

    instance = Jarvis(
        settings,
        events,
        with_tts=False,
        with_stt=False,
        hardware=None,
        enable_reminders=False,
    )
    # no hardware probing inside tests
    instance.hardware = None
    instance.ctx.hardware = None
    return instance
