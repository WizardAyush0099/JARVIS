"""Web API tests.

Skipped automatically when FastAPI/httpx are not installed, so the rest of the
suite still runs on a minimal install.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="FastAPI is not installed")
pytest.importorskip("httpx", reason="httpx is needed for TestClient")

from fastapi.testclient import TestClient  # noqa: E402

from core.brain import Jarvis  # noqa: E402
from server.app import create_app  # noqa: E402


@pytest.fixture
def client(settings, events):
    jarvis = Jarvis(settings, events, with_tts=False, with_stt=False, hardware=None, enable_reminders=False)
    app = create_app(settings, jarvis)
    with TestClient(app) as test_client:
        yield test_client


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_index_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "JARVIS" in response.text
    assert "/static/app.js" in response.text


def test_state_snapshot_shape(client):
    payload = client.get("/api/state").json()
    for key in ("identity", "settings", "status", "messages"):
        assert key in payload
    assert payload["identity"]["owner"] == "Ayush"
    assert "providers" in payload["status"]


def test_chat_round_trip(client):
    response = client.post("/api/chat", json={"text": "who am I"})
    assert response.status_code == 200
    body = response.json()
    assert "Ayush" in body["reply"]["text"]


def test_chat_requires_text(client):
    assert client.post("/api/chat", json={}).status_code == 400


def test_tools_endpoint_lists_tools(client):
    names = {item["name"] for item in client.get("/api/tools").json()["tools"]}
    assert {"calculate", "system_status", "generate_image"} <= names


def test_clear_endpoint(client):
    client.post("/api/chat", json={"text": "hello"})
    response = client.post("/api/clear")
    assert response.json()["ok"] is True


def test_settings_never_expose_secrets(client):
    payload = client.get("/api/state").json()
    blob = str(payload).lower()
    for forbidden in ("api_key", "smtp_password", '"password"', "secret"):
        assert forbidden not in blob


def test_logs_endpoint(client):
    response = client.get("/api/logs?limit=5")
    assert response.status_code == 200
    assert "logs" in response.json()


def test_token_is_enforced_when_configured(settings, events):
    settings.web.token = "s3cret"
    jarvis = Jarvis(settings, events, with_tts=False, with_stt=False, hardware=None, enable_reminders=False)
    app = create_app(settings, jarvis)
    with TestClient(app) as guarded:
        assert guarded.get("/api/state").status_code == 401
        assert guarded.get("/api/state", headers={"X-Jarvis-Token": "s3cret"}).status_code == 200
        assert guarded.get("/api/state", params={"token": "s3cret"}).status_code == 200
        # the page itself must stay loadable so the UI can ask for the token
        assert guarded.get("/").status_code == 200


def test_websocket_streams_state(settings, events):
    jarvis = Jarvis(settings, events, with_tts=False, with_stt=False, hardware=None, enable_reminders=False)
    app = create_app(settings, jarvis)
    with TestClient(app) as visitor:
        with visitor.websocket_connect("/ws") as socket:
            hello = socket.receive_json()
            assert hello["type"] == "hello"
            assert hello["state"]["identity"]["owner"] == "Ayush"
