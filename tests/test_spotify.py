"""Spotify is an optional extra, so it must degrade politely - and never leak.

The HTTP layer is replaced with canned responses, so these tests assert the exact
requests JARVIS builds and the exact words it uses when Spotify says no.  No
network, no account, no keys.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
from typing import Any, Dict, List, Optional

import pytest

from ai.http import HttpError
from tools import spotify as spotify_tools
from tools.base import ToolContext


@pytest.fixture
def linked(settings):
    settings.spotify.client_id = "client-id"
    settings.spotify.client_secret = "client-secret"
    settings.spotify.refresh_token = "refresh-token"
    return settings


@pytest.fixture
def ctx(linked):
    return ToolContext(settings=linked)


class FakeSpotify:
    """Records every request and answers with fixed JSON."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.status_for: Dict[str, int] = {}
        self.token_calls = 0
        self.playing = True
        self.has_track = True
        self.body: Dict[str, Any] = {"Authorization": ""}

    # -- installation ------------------------------------------------------
    def install(self, monkeypatch: pytest.MonkeyPatch) -> "FakeSpotify":
        spotify_tools.forget_token()
        monkeypatch.setattr(spotify_tools, "request_text", self._token)
        monkeypatch.setattr(spotify_tools, "request_json", self._api)
        return self

    # -- the two HTTP entry points ----------------------------------------
    def _token(self, url: str, **kwargs: Any) -> str:
        self.token_calls += 1
        self.requests.append({"url": url, "method": "POST", "body": kwargs.get("data")})
        assert url == spotify_tools.TOKEN_URL
        headers = kwargs.get("headers") or {}
        blob = base64.b64decode(headers["Authorization"].split(" ", 1)[1]).decode()
        assert blob == "client-id:client-secret"
        assert b"grant_type=refresh_token" in kwargs["data"]
        return json.dumps({"access_token": "AT", "expires_in": 3600})

    def _api(self, url: str, payload: Any = None, method: Optional[str] = None,
             headers: Any = None, **kwargs: Any) -> Any:
        headers = headers or {}
        assert headers.get("Authorization") == "Bearer AT"
        path = urllib.parse.urlparse(url).path
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.requests.append({"path": path, "method": method or "GET", "payload": payload, "query": query})
        # one-shot: a stale token fails once, then the fresh one works
        status = self.status_for.pop(path, None)
        if status:
            raise HttpError("request failed", status=status, body="{}", url=url)
        return self._respond(path)

    # -- canned answers ----------------------------------------------------
    def _respond(self, path: str) -> Any:
        if path == "/v1/me/player/currently-playing":
            if not self.has_track:
                return None
            return {
                "is_playing": self.playing,
                "progress_ms": 30_000,
                "item": {
                    "id": "track-1",
                    "uri": "spotify:track:track-1",
                    "name": "A Moment Apart",
                    "duration_ms": 219_000,
                    "artists": [{"name": "ODESZA"}],
                    "album": {"name": "A Moment Apart"},
                },
            }
        if path == "/v1/me/player":
            return {"device": {"name": "Living Room speaker"}}
        if path == "/v1/me/tracks":
            return {
                "total": 7,
                "items": [
                    {"track": {"name": "Kusanagi", "artists": [{"name": "ODESZA"}]}},
                    {"track": {"name": "Loyal", "artists": [{"name": "ODESZA"}]}},
                ],
            }
        if path == "/v1/search":
            return {
                "tracks": {
                    "items": [
                        {
                            "uri": "spotify:track:bohemian",
                            "name": "Bohemian Rhapsody",
                            "artists": [{"name": "Queen"}],
                            "external_urls": {"spotify": "https://open.spotify.com/track/bohemian"},
                        }
                    ]
                }
            }
        return None

    # -- assertions helpers ------------------------------------------------
    def find(self, path: str, method: str = "GET") -> Optional[Dict[str, Any]]:
        for entry in self.requests:
            if entry.get("path") == path and entry.get("method") == method:
                return entry
        return None


@pytest.fixture
def api(linked, monkeypatch) -> FakeSpotify:
    return FakeSpotify().install(monkeypatch)


# --------------------------------------------------------------------------- #
# not linked at all
# --------------------------------------------------------------------------- #
def test_unlinked_spotify_explains_how_to_link(settings):
    """No credentials: every tool says what to do instead of failing obscurely."""
    context = ToolContext(settings=settings)
    assert settings.spotify.configured is False
    for tool in (
        spotify_tools.spotify_status,
        spotify_tools.spotify_search,
        spotify_tools.spotify_search_and_play,
        spotify_tools.spotify_liked_songs,
        spotify_tools.spotify_like_current,
    ):
        result = tool(ctx=context) if tool is spotify_tools.spotify_status else tool("x", ctx=context)
        assert result.ok is False
        assert "scripts/spotify_auth.py" in result.error
        assert "Player." not in result.error  # nothing pretends to have happened


def test_partial_credentials_are_reported_not_used(settings):
    settings.spotify.client_id = "client-id"
    assert settings.spotify.configured is False
    assert settings.spotify.partial is True


# --------------------------------------------------------------------------- #
# token handling
# --------------------------------------------------------------------------- #
def test_the_refresh_token_is_exchanged_and_cached(api: FakeSpotify, ctx):
    first = spotify_tools.access_token(ctx.settings)
    second = spotify_tools.access_token(ctx.settings)
    assert first == second == "AT"
    assert api.token_calls == 1  # cached until it expires


def test_a_rejected_token_is_retried_once(api: FakeSpotify, ctx):
    spotify_tools.access_token(ctx.settings)  # a token is already cached
    api.status_for["/v1/me/tracks"] = 401
    result = spotify_tools.spotify_liked_songs(ctx=ctx, limit=2)
    # the expired token is thrown away and a fresh one fetched, so the user
    # never sees a session-expired error for something JARVIS can fix itself
    assert result.ok is True
    assert api.token_calls == 2


# --------------------------------------------------------------------------- #
# liked songs
# --------------------------------------------------------------------------- #
def test_liked_songs_are_listed(api: FakeSpotify, ctx):
    result = spotify_tools.spotify_liked_songs(ctx=ctx, limit=2)
    assert result.ok is True
    assert "Kusanagi" in result.output and "ODESZA" in result.output
    assert "7 songs saved in total" in result.output
    assert api.find("/v1/me/tracks", "GET")["query"]["limit"] == "2"


def test_liking_saves_the_track_that_is_playing(api: FakeSpotify, ctx):
    result = spotify_tools.spotify_like_current(ctx=ctx)
    assert result.ok is True
    assert "Saved" in result.output
    saved = api.find("/v1/me/tracks", "PUT")
    assert saved is not None
    assert saved["payload"] == {"ids": ["track-1"]}


def test_unliking_removes_it(api: FakeSpotify, ctx):
    result = spotify_tools.spotify_like_current(False, ctx=ctx)
    assert result.ok is True
    assert "Removed" in result.output
    assert api.find("/v1/me/tracks", "DELETE")["payload"] == {"ids": ["track-1"]}


def test_liking_with_nothing_playing_is_explained(api: FakeSpotify, ctx):
    api.has_track = False
    result = spotify_tools.spotify_like_current(ctx=ctx)
    assert result.ok is False
    assert "nothing is playing" in result.error


# --------------------------------------------------------------------------- #
# search and playback
# --------------------------------------------------------------------------- #
def test_search_lists_matches(api: FakeSpotify, ctx):
    result = spotify_tools.spotify_search("bohemian", ctx=ctx)
    assert result.ok is True
    assert "Bohemian Rhapsody" in result.output
    assert "Queen" in result.output


def test_playing_a_named_track_sends_its_uri(api: FakeSpotify, ctx):
    result = spotify_tools.spotify_search_and_play("bohemian rhapsody", ctx=ctx)
    assert result.ok is True
    assert "Playing Bohemian Rhapsody" in result.output
    assert api.find("/v1/me/player/play", "PUT")["payload"] == {"uris": ["spotify:track:bohemian"]}


def test_playing_a_playlist_sends_a_context(api: FakeSpotify, ctx):
    api._respond = lambda path: {  # type: ignore[assignment]
        "playlists": {
            "items": [
                {"uri": "spotify:playlist:discover", "name": "Discover Weekly", "external_urls": {}}
            ]
        }
    } if path == "/v1/search" else None
    result = spotify_tools.spotify_search_and_play("discover weekly", kind="playlist", ctx=ctx)
    assert result.ok is True
    assert api.find("/v1/me/player/play", "PUT")["payload"] == {"context_uri": "spotify:playlist:discover"}


def test_shuffle_sets_the_state(api: FakeSpotify, ctx):
    result = spotify_tools.spotify_control("shuffle", state="off", ctx=ctx)
    assert result.ok is True
    assert api.find("/v1/me/player/shuffle", "PUT")["query"] == {"state": "false"}


def test_toggle_pauses_when_something_is_playing(api: FakeSpotify, ctx):
    result = spotify_tools.spotify_control("toggle", ctx=ctx)
    assert result.ok is True
    assert "paused" in result.output.lower()
    assert api.find("/v1/me/player/pause", "PUT") is not None


# --------------------------------------------------------------------------- #
# Spotify's own refusals, in the user's language
# --------------------------------------------------------------------------- #
def test_premium_refusal_is_explained(api: FakeSpotify, ctx):
    api.status_for["/v1/me/player/play"] = 403
    result = spotify_tools.spotify_search_and_play("anything", ctx=ctx)
    assert result.ok is False
    assert "Premium" in result.error


def test_missing_device_is_explained(api: FakeSpotify, ctx):
    api.status_for["/v1/me/player/play"] = 404
    result = spotify_tools.spotify_search_and_play("anything", ctx=ctx)
    assert result.ok is False
    assert "active device" in result.error
    # ...and it offers the thing that always works
    assert "browser" in result.error


def test_rate_limiting_is_explained(api: FakeSpotify, ctx):
    api.status_for["/v1/search"] = 429
    result = spotify_tools.spotify_search("anything", ctx=ctx)
    assert result.ok is False
    assert "rate limiting" in result.error


# --------------------------------------------------------------------------- #
# opening Spotify needs no account at all
# --------------------------------------------------------------------------- #
def test_opening_spotify_works_without_credentials(settings, monkeypatch):
    opened: List[str] = []

    class Opened:
        ok = True
        output = "Opening Spotify."
        data = {}

    monkeypatch.setattr("tools.system.open_url", lambda url: (opened.append(url), Opened())[1])
    result = spotify_tools.spotify_open("lofi beats", ctx=ToolContext(settings=settings))
    assert result.ok is True
    assert opened and opened[0] == "https://open.spotify.com/search/lofi%20beats"


def test_public_settings_never_carry_spotify_values(linked):
    blob = str(linked.public_dict())
    assert linked.public_dict()["spotify"] == {"linked": True, "partial": False}
    for secret in ("client-secret", "refresh-token"):
        assert secret not in blob
