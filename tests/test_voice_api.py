"""Voice plumbing over HTTP: browser audio, mic control, live talk, /docs.

These use small stubs for the speech engine and the recogniser, so the suite
still runs on a machine with no microphone, no speakers and no network - while
still exercising the real routes, the real `Speaker`, and the real brain.
"""

from __future__ import annotations

import hashlib

import pytest

pytest.importorskip("fastapi", reason="FastAPI is not installed")
pytest.importorskip("httpx", reason="httpx is needed for TestClient")

from fastapi.testclient import TestClient  # noqa: E402

from core.brain import Jarvis  # noqa: E402
from server.app import create_app  # noqa: E402
from voice.stt import Transcript  # noqa: E402


class StubTTSEngine:
    """Stands in for Edge/Piper: writes a tiny file instead of calling anything."""

    name = "stub"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def available(self) -> bool:
        return True

    def synthesize(self, text, language, cache_dir, settings):
        self.calls += 1
        if self.fail:
            return None
        target = cache_dir / (hashlib.sha1(("stub" + text).encode()).hexdigest() + ".mp3")
        if not target.exists():
            target.write_bytes(b"ID3\x03\x00\x00\x00stub-audio")
        return target


class StubRecogniser:
    """Stands in for Google/Vosk: returns whatever the test wants to see."""

    name = "stub-recogniser"

    def __init__(self, text: str = "what time is it", ok: bool = True) -> None:
        self.text = text
        self.ok = ok
        self.received = []

    def available(self) -> bool:
        return True

    def listen_once(self, timeout=6.0, phrase_time_limit=8.0):
        return Transcript(text=self.text, ok=self.ok, error="" if self.ok else "no speech detected")

    def transcribe_audio(self, pcm, sample_rate=16000, sample_width=2):
        self.received.append((len(pcm), sample_rate))
        if not self.ok:
            return Transcript(ok=False, error="I couldn't make out what you said")
        return Transcript(text=self.text, ok=True, confidence=0.9)


class StubListener:
    """The bits of :class:`voice.stt.Listener` the API touches."""

    def __init__(self, engine=None, enabled=True) -> None:
        self.engine = engine or StubRecogniser()
        self._enabled = enabled
        self.started = False
        self.paused = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> bool:
        self.started = True
        return True

    def stop(self, wait: float = 1.5) -> None:
        self.started = False

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def transcribe_audio(self, pcm, sample_rate=16000, sample_width=2):
        return self.engine.transcribe_audio(pcm, sample_rate, sample_width)

    def status(self):
        return {
            "enabled": self.enabled,
            "engine": self.engine.name,
            "running": self.started,
            "paused": self.paused,
            "wake_word": None,
            "heard": 0,
            "available": True,
            "last_error": "",
        }


def make_jarvis(settings, events, *, engine=None, listener=None):
    instance = Jarvis(
        settings, events, with_tts=False, with_stt=False, hardware=None, enable_reminders=False
    )
    from voice.tts import Speaker

    instance.speaker = Speaker(settings, events, engine=engine or StubTTSEngine(), sink=_NullSink())
    instance.listener = listener if listener is not None else StubListener()
    instance.set_voice_output("off")
    instance._apply_voice_output()
    return instance


class _NullSink:
    name = "null"

    def play_file(self, path) -> bool:
        return False

    def stop(self) -> bool:
        return False


@pytest.fixture
def engine():
    return StubTTSEngine()


@pytest.fixture
def listener():
    return StubListener()


@pytest.fixture
def client(settings, events, engine, listener):
    settings.tts.enabled = True
    jarvis = make_jarvis(settings, events, engine=engine, listener=listener)
    app = create_app(settings, jarvis)
    with TestClient(app) as test_client:
        test_client.jarvis = jarvis  # type: ignore[attr-defined]
        yield test_client


# --------------------------------------------------------------------------- #
# the console is the app; /docs is the manual
# --------------------------------------------------------------------------- #
def test_root_serves_the_console_with_every_control(client):
    page = client.get("/").text
    for control in (
        'id="thread"', 'id="composer"', 'id="input"', 'id="btn-send"',
        'id="btn-mic"', 'id="btn-live"', 'id="btn-mic-mute"', 'id="btn-voice-mute"',
        'id="btn-stop"', 'id="btn-clear"', 'id="orb"', 'id="pill-link"',
        'id="state-label"', 'id="player"',
    ):
        assert control in page, control
    assert "/static/app.js" in page


def test_client_script_uses_the_backend_for_voice(client):
    """Voice must go through /api/transcribe and /api/speak, not the browser."""
    script = client.get("/static/app.js").text
    assert "/api/transcribe" in script
    assert "/api/speak" in script
    assert "/api/listen" in script
    # the browser's own speech synthesis is not used to impersonate JARVIS
    assert "SpeechSynthesisUtterance" not in script
    assert "webkitSpeechRecognition" not in script


def test_docs_page_is_separate_from_the_assistant(client):
    response = client.get("/docs")
    assert response.status_code == 200
    assert "JARVIS setup" in response.text
    assert "back to JARVIS" in response.text
    assert 'href="/"' in response.text


# --------------------------------------------------------------------------- #
# voice output
# --------------------------------------------------------------------------- #
def test_voice_mode_switches_between_browser_and_device(client):
    moved = client.post("/api/speech", json={"mode": "browser"})
    assert moved.status_code == 200
    assert moved.json()["mode"] == "browser"

    assert client.post("/api/speech", json={"mode": "device"}).json()["mode"] == "device"

    off = client.post("/api/speech", json={"mode": "off"})
    assert off.json() == {**off.json(), "mode": "off", "muted": True}
    # unmuting restores where the voice was pointed
    toggled = client.post("/api/speech", json={})
    assert toggled.json()["mode"] == "device"


def test_voice_mode_rejects_nonsense(client):
    assert client.post("/api/speech", json={"mode": "loudspeaker"}).status_code == 400


def test_speak_returns_real_audio_from_the_configured_engine(client, engine):
    client.post("/api/speech", json={"mode": "browser"})
    response = client.post("/api/speak", json={"text": "Systems online."})
    assert response.status_code == 200
    url = response.json()["url"]
    assert url.startswith("/media/voice/")
    assert engine.calls == 1

    audio = client.get(url)
    assert audio.status_code == 200
    assert audio.content.startswith(b"ID3")

    # the same sentence is served from cache, not synthesized twice
    client.post("/api/speak", json={"text": "Systems online."})
    assert engine.calls == 1


def test_speak_reports_a_failing_engine(settings, events, listener):
    broken = StubTTSEngine(fail=True)
    settings.tts.enabled = True
    jarvis = make_jarvis(settings, events, engine=broken, listener=listener)
    jarvis.set_voice_output("browser")
    with TestClient(create_app(settings, jarvis)) as client:
        response = client.post("/api/speak", json={"text": "hello"})
    assert response.status_code == 502
    assert "could not produce audio" in response.json()["detail"]


def test_speak_refuses_when_muted(client):
    client.post("/api/speech", json={"mode": "off"})
    response = client.post("/api/speak", json={"text": "hello"})
    assert response.status_code == 409
    assert "muted" in response.json()["detail"]


def test_speak_requires_text(client, engine):
    client.post("/api/speech", json={"mode": "browser"})
    assert client.post("/api/speak", json={}).status_code == 400
    assert engine.calls == 0


# --------------------------------------------------------------------------- #
# microphone
# --------------------------------------------------------------------------- #
def test_mic_mute_blocks_every_input_path(client):
    assert client.post("/api/mic", json={"muted": True}).json()["muted"] is True

    chat = client.post("/api/listen").json()
    assert chat["reply"]["error"] is True
    assert "muted" in chat["reply"]["text"]

    upload = client.post("/api/transcribe?rate=16000", content=b"\x00" * 1600)
    assert upload.status_code == 409


def test_transcribe_uses_the_installed_engine(client, listener):
    response = client.post(
        "/api/transcribe?rate=16000", content=b"\x01\x02" * 800
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["text"] == "what time is it"
    assert body["engine"] == "stub-recogniser"
    assert listener.engine.received == [(1600, 16000)]


def test_transcribe_reports_unintelligible_audio(settings, events):
    listener = StubListener(StubRecogniser(ok=False))
    settings.tts.enabled = True
    jarvis = make_jarvis(settings, events, listener=listener)
    with TestClient(create_app(settings, jarvis)) as client:
        body = client.post("/api/transcribe?rate=16000", content=b"\x00" * 1600).json()
    assert body["ok"] is False
    assert "make out" in body["error"]


def test_transcribe_needs_audio(client):
    assert client.post("/api/transcribe?rate=16000", content=b"").status_code == 400


def test_transcribe_without_an_engine_is_honest(settings, events):
    settings.tts.enabled = True
    jarvis = make_jarvis(settings, events)
    jarvis.listener = None
    with TestClient(create_app(settings, jarvis)) as client:
        response = client.post("/api/transcribe?rate=16000", content=b"\x00" * 1600)
    assert response.status_code == 503
    assert "speech engine" in response.json()["detail"]


def test_live_talk_says_why_it_cannot_listen(settings, events):
    settings.tts.enabled = True
    jarvis = make_jarvis(settings, events, listener=StubListener(enabled=False))
    with TestClient(create_app(settings, jarvis)) as client:
        body = client.post("/api/live", json={"enabled": True}).json()
    assert body["enabled"] is False
    assert "STT_ENABLED" in body["reason"]


def test_live_talk_starts_the_backend_loop(client, listener):
    body = client.post("/api/live", json={"enabled": True}).json()
    assert body["running"] is True
    assert listener.started is True
    assert client.post("/api/live", json={"enabled": False}).json()["running"] is False


def test_stop_reports_success(client):
    response = client.post("/api/stop")
    assert response.status_code == 200 and response.json()["ok"] is True


# --------------------------------------------------------------------------- #
# state + routing
# --------------------------------------------------------------------------- #
def test_state_exposes_voice_routing_and_mic(client):
    client.post("/api/speech", json={"mode": "browser"})
    client.post("/api/mic", json={"muted": True})
    payload = client.get("/api/state").json()
    assert payload["voice"]["mode"] == "browser"
    assert payload["voice"]["mic_muted"] is True
    assert payload["voice"]["configured"] is True
    assert payload["status"]["mic"]["muted"] is True


def test_voice_reverts_to_the_device_when_the_last_client_leaves(settings, events):
    """Closing the browser must not leave JARVIS silent at the keyboard."""
    settings.tts.enabled = True
    settings.tts.voice_output = "device"
    jarvis = make_jarvis(settings, events)
    app = create_app(settings, jarvis)
    with TestClient(app) as visitor:
        with visitor.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "hello"
            visitor.post("/api/speech", json={"mode": "browser"})
            assert jarvis.voice_output == "browser"
        # the socket is closed now: the routing falls back to the configured output
        assert jarvis.voice_output == "device"


def test_tts_disabled_in_env_stays_silent(settings, events):
    settings.tts.enabled = False
    jarvis = make_jarvis(settings, events)
    assert jarvis.voice_output == "off"
    assert jarvis.speaker.muted is True
