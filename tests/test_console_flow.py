"""The console's own request sequence, end to end.

This walks the exact order the browser client uses - load state, claim audio,
chat, get audio, play it, mute, speak, stop - plus the websocket path that
carries a voice turn.  It is the closest thing to clicking through the interface
that can run without a browser.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="FastAPI is not installed")
pytest.importorskip("httpx", reason="httpx is needed for TestClient")

from fastapi.testclient import TestClient  # noqa: E402

from server.app import create_app  # noqa: E402
from tests.test_voice_api import (  # noqa: E402
    StubListener,
    StubRecogniser,
    StubTTSEngine,
    make_jarvis,
)


def test_a_whole_browser_session(settings, events):
    settings.tts.enabled = True
    engine = StubTTSEngine()
    recogniser = StubRecogniser(text="what is the time")
    jarvis = make_jarvis(settings, events, engine=engine, listener=StubListener(recogniser))

    with TestClient(create_app(settings, jarvis)) as client:
        # 1. the page is the console, and it loads its state
        assert client.get("/").status_code == 200
        state = client.get("/api/state").json()
        assert state["voice"]["configured"] is True

        # 2. the browser claims the voice from the device's speaker
        assert client.post("/api/speech", json={"mode": "browser"}).json()["mode"] == "browser"

        # 3. a typed turn goes to the real brain
        reply = client.post("/api/chat", json={"text": "what time is it"}).json()["reply"]
        assert reply["text"] and reply["error"] is False

        # 4. the reply is spoken by the backend engine, and the audio is fetchable
        speech = client.post("/api/speak", json={"text": reply["text"]}).json()
        audio = client.get(speech["url"])
        assert audio.status_code == 200 and len(audio.content) > 4

        # 5. mute the microphone: both input paths refuse
        assert client.post("/api/mic", json={"muted": True}).json()["muted"] is True
        assert client.post("/api/listen", json={}).json()["reply"]["error"] is True
        assert client.post("/api/transcribe?rate=16000", content=b"\x00" * 1600).status_code == 409

        # 6. unmute, then a spoken turn: record -> transcribe -> answer
        client.post("/api/mic", json={"muted": False})
        heard = client.post("/api/transcribe?rate=16000", content=b"\x01\x02" * 800).json()
        assert heard["ok"] and heard["text"] == "what is the time"
        spoken_reply = client.post("/api/chat", json={"text": heard["text"]}).json()["reply"]
        assert spoken_reply["text"]

        # 7. the voice mute button, then stop
        assert client.post("/api/speech", json={}).json()["mode"] == "off"
        assert client.post("/api/speak", json={"text": "hello"}).status_code == 409
        assert client.post("/api/stop").json()["ok"] is True


def test_a_voice_turn_reaches_the_browser_over_the_socket(settings, events):
    """Live Talk from the device microphone must show up in the open page."""
    settings.tts.enabled = True
    jarvis = make_jarvis(settings, events, engine=StubTTSEngine(), listener=StubListener())

    with TestClient(create_app(settings, jarvis)) as client:
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "hello"

            # exactly what the microphone loop does when it hears a phrase
            jarvis.handle("what time is it", "voice")

            seen = []
            for _ in range(60):
                payload = socket.receive_json()
                if payload.get("type") != "event":
                    continue
                event = payload["event"]
                seen.append((event.get("kind"), event.get("role"), event.get("text")))
                if event.get("kind") == "message" and event.get("role") == "assistant":
                    break

            kinds = [item[0] for item in seen]
            assert "message" in kinds
            assert any(role == "user" for _, role, _ in seen)
            assert any(role == "assistant" and text for _, role, text in seen)
