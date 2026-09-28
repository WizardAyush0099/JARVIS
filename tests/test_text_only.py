"""No USB microphone? Everything except voice input still has to work.

This is the case the build has to survive on a bare Raspberry Pi: no microphone
plugged in, no sound card, maybe not even ``speech_recognition`` installed.  Typed
chat, tools, memory, the visitor protocol and JARVIS's own voice are all
independent of microphone hardware, and a missing device must never flood the
console or leave it looking broken.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="FastAPI is not installed")
pytest.importorskip("httpx", reason="httpx is needed for TestClient")

from fastapi.testclient import TestClient  # noqa: E402

from core.brain import Jarvis  # noqa: E402
from server.app import create_app  # noqa: E402
from tests.test_voice_api import make_jarvis  # noqa: E402
from voice.stt import Listener, STTEngine, Transcript  # noqa: E402


# --------------------------------------------------------------------------- #
# engines that stand in for a machine with no device
# --------------------------------------------------------------------------- #
class DeadDeviceEngine(STTEngine):
    """A working recogniser and no microphone at all - the Pi-without-a-USB-mic case."""

    name = "dead-device"

    def available(self) -> bool:
        return False

    def recognizer_available(self) -> bool:
        return True

    def listen_once(self, timeout: float = 6.0, phrase_time_limit: float = 8.0) -> Transcript:
        return Transcript(ok=False, error="no microphone is available (No Default Input Device)")

    def transcribe_audio(self, pcm, sample_rate: int = 16000, sample_width: int = 2) -> Transcript:
        return Transcript(text="what time is it", ok=True, confidence=0.8)


class UnplugLaterEngine(STTEngine):
    """Starts healthy, then the device goes away - unplugged mid-session."""

    name = "unplug-later"

    def __init__(self) -> None:
        self.plugged = True

    def available(self) -> bool:
        return self.plugged

    def recognizer_available(self) -> bool:
        return True

    def listen_once(self, timeout: float = 6.0, phrase_time_limit: float = 8.0) -> Transcript:
        if self.plugged:
            return Transcript(ok=False, error="no speech detected")  # a normal quiet moment
        return Transcript(ok=False, error="no microphone is available (device disappeared)")


def _wire(jarvis, listener) -> None:
    jarvis.listener = listener
    jarvis.ctx.hardware = None


# --------------------------------------------------------------------------- #
# typed chat is the guaranteed path
# --------------------------------------------------------------------------- #
def test_typed_chat_works_with_no_microphone_anywhere(settings, events):
    """No listener, no STT engine, no device: the console is still fully usable."""
    settings.tts.enabled = True
    jarvis = make_jarvis(settings, events)
    jarvis.listener = None

    with TestClient(create_app(settings, jarvis)) as client:
        # the only input path that matters still works
        reply = client.post("/api/chat", json={"text": "what time is it"}).json()["reply"]
        assert reply["error"] is False and reply["text"]

        # and everything that needs a microphone says so plainly
        spoken = client.post("/api/listen").json()["reply"]
        assert spoken["error"] is True
        assert "type your message" in spoken["text"]

        live = client.post("/api/live", json={"enabled": True}).json()
        assert live["enabled"] is False and live["running"] is False
        assert "type your messages" in live["reason"]

        upload = client.post("/api/transcribe?rate=16000", content=b"\x00" * 1600)
        assert upload.status_code == 503
        assert "speech engine" in upload.json()["detail"]

        state = client.get("/api/state").json()["status"]
        assert state["mic"]["engine"] == "none"
        assert state["mic"]["text_only"] is True
        # the brain is untouched by any of it
        assert client.post("/api/chat", json={"text": "system status"}).json()["reply"]["text"]


def test_a_quiet_night_is_not_an_error_and_the_listener_keeps_going(settings, events):
    """\"no speech detected\" is normal: keep listening, publish nothing."""
    settings.stt.enabled = True
    engine = UnplugLaterEngine()
    listener = Listener(settings, events, engine=engine)

    first = listener.listen_once(timeout=0.2)
    assert first.ok is False and first.timed_out is True
    assert [e for e in events.recent(20) if e["kind"] == "stt_error"] == []
    assert listener.status()["available"] is True
    assert listener.status()["can_transcribe"] is True


def test_an_unplugged_microphone_stands_down_after_one_message(settings, events):
    """The loop must not hammer a dead device (or flood the chat) every second."""
    settings.stt.enabled = True
    engine = UnplugLaterEngine()
    listener = Listener(settings, events, engine=engine)
    assert listener.start() is True
    listener.stop()

    engine.plugged = False
    # what the push-to-talk button does: one real capture attempt
    failed = listener.listen_once(timeout=0.2)
    assert failed.ok is False

    status = listener.status()
    assert status["available"] is False
    assert status["running"] is False  # no loop is left spinning
    assert "type your message" in listener.listen_once(timeout=0.2).error

    before = len([e for e in events.recent(40) if e["kind"] == "stt_error"])
    assert before == 1  # exactly one explanation, not a stream

    listener._run()  # the background loop must return at once, not retry
    after = len([e for e in events.recent(40) if e["kind"] == "stt_error"])
    assert after == before


def test_plugging_the_microphone_back_in_resumes_voice(settings, events):
    settings.stt.enabled = True
    engine = UnplugLaterEngine()
    engine.plugged = False
    listener = Listener(settings, events, engine=engine)
    assert listener.start() is False  # nothing to listen to yet

    engine.plugged = True  # the USB microphone arrives
    assert listener.start() is True
    assert listener.status()["available"] is True


def test_a_phone_can_still_dictate_without_a_pi_microphone(settings, events):
    """The Pi has no device, but the recogniser works - so a phone's mic can be used."""
    settings.tts.enabled = True
    settings.stt.enabled = True
    jarvis = make_jarvis(settings, events)
    _wire(jarvis, Listener(settings, events, engine=DeadDeviceEngine()))

    with TestClient(create_app(settings, jarvis)) as client:
        heard = client.post("/api/transcribe?rate=16000", content=b"\x01\x02" * 800)
        assert heard.status_code == 200
        assert heard.json()["ok"] is True and heard.json()["text"] == "what time is it"

        # ...while the machine's own microphone is honestly reported as missing
        live = client.post("/api/live", json={"enabled": True}).json()
        assert live["enabled"] is False
        assert "microphone" in live["reason"] and "type your message" in live["reason"]

        mic = client.get("/api/state").json()["status"]["mic"]
        assert mic["available"] is False
        assert mic["can_transcribe"] is True

        # and a typed turn never depends on any of it
        assert client.post("/api/chat", json={"text": "who made you"}).json()["reply"]["text"]


def test_the_brain_points_at_typing_when_the_device_is_missing(jarvis):
    jarvis.settings.stt.enabled = True
    listener = Listener(jarvis.settings, jarvis.events, engine=DeadDeviceEngine())
    listener.listen_once(timeout=0.2)  # let it discover the missing device
    jarvis.listener = listener

    reply = jarvis.listen_once(timeout=0.2)
    assert reply.error is True
    assert "type your message" in reply.text

    live = jarvis.listen_live(True)
    assert live["enabled"] is False
    assert "microphone" in live["reason"] and "type your message" in live["reason"]
    # typed turns are unaffected by any of the above
    assert jarvis.handle("hello").error is False


def test_the_console_client_handles_a_missing_microphone(settings, events):
    """The UI must disable the voice controls and say why, not fail on click."""
    jarvis = make_jarvis(settings, events)
    with TestClient(create_app(settings, jarvis)) as client:
        script = client.get("/static/app.js").text
    assert "textOnly" in script
    assert "No microphone is available here" in script
    assert "No microphone needed - type your message" in script
    assert "type your message instead" in script


def test_a_jarvis_without_any_listener_can_still_be_built(settings):
    """No voice extras installed at all: construction and status must not raise."""
    jarvis = Jarvis(settings, with_tts=False, with_stt=False, hardware=None, enable_reminders=False)
    assert jarvis.listener is None
    assert jarvis.listen_live(True)["enabled"] is False
    assert jarvis.status()["mic"]["text_only"] is True
