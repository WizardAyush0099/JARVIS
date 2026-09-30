"""Spotify - liked songs and real playback control through Spotify's Web API.

``tools/media.py`` already plays, pauses and skips whatever is on the machine,
with no account and no key.  This module adds the two things only Spotify itself
can answer:

* **your liked songs** (list them, like or unlike the track that is playing)
* **starting something specific** - "play Bohemian Rhapsody", "play my Discover
  Weekly playlist" - on whichever Spotify device is currently active

It is strictly optional.  It needs a one-time authorisation on the Pi (see
``scripts/spotify_auth.py``) and it degrades to a plain sentence when Spotify is
not linked, so nothing else in JARVIS depends on it.

Honest limits, stated up front because they are Spotify's, not ours:

* starting playback with the Web API needs **Spotify Premium**
* it needs an **active device** - the Spotify app open somewhere, or the Pi's own
  web player tab.  With no device, JARVIS says exactly that instead of failing
  silently.

Tokens are read from the environment only, never logged, and cached in memory
until they expire.
"""

from __future__ import annotations

import base64
import json as jsonlib
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from ai.http import HttpError, request_json, request_text, with_query
from core.logging_setup import get_logger
from tools.base import ToolResult, tool

log = get_logger("tools.spotify")

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_ROOT = "https://api.spotify.com/v1"

#: Scopes the one-time authorisation asks for.  Kept as a module constant so the
#: script and this module can never drift apart.
SCOPES = (
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "user-library-read",
    "user-library-modify",
)

NOT_LINKED = (
    "Spotify is not linked on this machine. Run `.venv/bin/python scripts/spotify_auth.py` "
    "once, follow the link it prints, and paste the three SPOTIFY_* values into your "
    "environment. Meanwhile I can still play, pause and skip whatever is playing "
    "with my media controls - no account needed."
)

PREMIUM_HINT = (
    "Spotify refused to start playback: starting a track with the API needs a "
    "Premium account. I can still open the track in the browser, or control "
    "whatever is already playing."
)

#: token cache: {access_token, expires_at}
_CACHE: Dict[str, Any] = {"token": "", "expires_at": 0.0}


# --------------------------------------------------------------------------- #
# credentials + token
# --------------------------------------------------------------------------- #
def credentials(settings: Any = None) -> Optional[Tuple[str, str, str]]:
    if settings is None or getattr(settings, "spotify", None) is None:
        return None
    spotify = settings.spotify
    if not spotify.configured:
        return None
    return spotify.client_id, spotify.client_secret, spotify.refresh_token


def access_token(settings: Any = None, force: bool = False) -> str:
    """Exchange the stored refresh token for an access token (cached in memory)."""
    creds = credentials(settings)
    if creds is None:
        raise RuntimeError(NOT_LINKED)
    client_id, client_secret, refresh_token = creds

    now = time.time()
    if not force and _CACHE["token"] and _CACHE["expires_at"] > now + 30:
        return str(_CACHE["token"])

    body = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": refresh_token}
    ).encode("utf-8")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    try:
        text = request_text(
            TOKEN_URL,
            method="POST",
            data=body,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=15.0,
            retries=1,
        )
    except HttpError as exc:
        raise RuntimeError(
            "Spotify rejected the stored authorisation - run "
            "`scripts/spotify_auth.py` again to relink it "
            f"({exc.status or 'network error'})"
        ) from exc

    try:
        payload = jsonlib.loads(text)
    except ValueError as exc:
        raise RuntimeError("Spotify's token endpoint returned something unexpected") from exc

    token = str(payload.get("access_token") or "")
    if not token:
        raise RuntimeError("Spotify did not return an access token")
    _CACHE["token"] = token
    _CACHE["expires_at"] = now + float(payload.get("expires_in") or 3600)
    return token


def forget_token() -> None:
    """Drop the cached access token (used by tests and after a 401)."""
    _CACHE["token"] = ""
    _CACHE["expires_at"] = 0.0


def _api(
    path: str,
    method: str = "GET",
    payload: Optional[Any] = None,
    params: Optional[Dict[str, Any]] = None,
    settings: Any = None,
    retry_on_401: bool = True,
) -> Any:
    """One Spotify API call, with the failure modes turned into real sentences."""
    try:
        token = access_token(settings)
    except RuntimeError:
        raise

    url = with_query(API_ROOT + path, params)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        if payload is None and method == "GET":
            return request_json(url, headers=headers, timeout=20.0, retries=1)
        return request_json(url, payload=payload if payload is not None else {}, method=method,
                            headers=headers, timeout=20.0, retries=1)
    except HttpError as exc:
        if exc.status == 401 and retry_on_401:
            # an expired/revoked access token is worth exactly one fresh attempt
            forget_token()
            return _api(path, method, payload, params, settings, retry_on_401=False)
        if exc.status == 403:
            raise RuntimeError(PREMIUM_HINT) from exc
        if exc.status == 404:
            raise RuntimeError(
                "Spotify has no active device. Open Spotify on the Pi or your phone "
                "(or the web player in a browser) and try again - playback control "
                "always talks to the device you select there."
            ) from exc
        if exc.status == 429:
            raise RuntimeError("Spotify is rate limiting right now - try again shortly") from exc
        raise RuntimeError(f"Spotify request failed ({exc.status or 'network error'})") from exc


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _track_line(item: Dict[str, Any]) -> str:
    name = str((item.get("name") or "?")).strip()
    artists = ", ".join(str(a.get("name", "")) for a in (item.get("artists") or []) if a.get("name"))
    extra = f" - {artists}" if artists else ""
    return f"{name}{extra}"


def _best_match(items: List[Dict[str, Any]], kind: str) -> Optional[Dict[str, Any]]:
    for item in items:
        if kind == "track" and item.get("uri", "").startswith("spotify:track:"):
            return item
        if kind != "track" and item.get("uri", "").startswith(f"spotify:{kind}:"):
            return item
    return items[0] if items else None


def current_track(settings: Any = None) -> Optional[Dict[str, Any]]:
    """The track playing right now, or ``None`` when nothing is."""
    data = _api("/me/player/currently-playing", settings=settings)
    if not isinstance(data, dict):
        return None
    item = data.get("item")
    if not isinstance(item, dict):
        return None
    track_id = str(item.get("id") or "")
    if not track_id:
        return None
    return {
        "id": track_id,
        "uri": str(item.get("uri") or ""),
        "name": item.get("name") or "",
        "artist": ", ".join(
            str(a.get("name", "")) for a in (item.get("artists") or []) if a.get("name")
        ),
        "album": (item.get("album") or {}).get("name") or "",
        "playing": bool(data.get("is_playing")),
        "progress_s": round(float(data.get("progress_ms") or 0) / 1000, 1),
        "duration_s": round(float((item.get("duration_ms") or 0)) / 1000, 1),
    }


def search(query: str, kind: str = "track", limit: int = 5, settings: Any = None) -> List[Dict[str, Any]]:
    kinds = {"track", "artist", "album", "playlist"}
    wanted = (kind or "track").strip().lower()
    if wanted not in kinds:
        wanted = "track"
    data = _api(
        "/search",
        params={"q": query, "type": wanted, "limit": max(1, min(int(limit or 5), 10))},
        settings=settings,
    )
    bucket = ((data or {}).get(f"{wanted}s") or {}).get("items") or []
    results: List[Dict[str, Any]] = []
    for item in bucket:
        if not isinstance(item, dict):
            continue
        if wanted == "track":
            results.append(
                {
                    "uri": item.get("uri"),
                    "name": item.get("name"),
                    "artist": ", ".join(
                        str(a.get("name", "")) for a in (item.get("artists") or []) if a.get("name")
                    ),
                    "kind": "track",
                    "url": (item.get("external_urls") or {}).get("spotify", ""),
                }
            )
        else:
            results.append(
                {
                    "uri": item.get("uri"),
                    "name": item.get("name"),
                    "artist": ", ".join(
                        str(a.get("name", "")) for a in (item.get("artists") or []) if a.get("name")
                    ),
                    "kind": wanted,
                    "url": (item.get("external_urls") or {}).get("spotify", ""),
                }
            )
    return results


def _play(uris: Optional[List[str]] = None, context: str = "", settings: Any = None) -> None:
    payload: Dict[str, Any] = {}
    if uris:
        payload["uris"] = uris
    elif context:
        payload["context_uri"] = context
    market = getattr(getattr(settings, "spotify", None), "market", "") if settings else ""
    _api("/me/player/play", method="PUT", payload=payload,
         params={"market": market} if market else None, settings=settings)


def _require_link(settings: Any) -> Optional[str]:
    return None if credentials(settings) is not None else NOT_LINKED


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
@tool(
    name="spotify_status",
    description="Whether Spotify is linked, and what it is playing right now (with liked state).",
    parameters={"type": "object", "properties": {}},
    category="media",
)
def spotify_status(ctx: Any = None) -> ToolResult:
    settings = getattr(ctx, "settings", None)
    if _require_link(settings):
        return ToolResult.failure(NOT_LINKED)
    try:
        track = current_track(settings)
        player = _api("/me/player", settings=settings)
    except RuntimeError as exc:
        return ToolResult.failure(str(exc))
    if not track:
        device = (player or {}).get("device") if isinstance(player, dict) else None
        where = f" Active device: {device.get('name')}." if isinstance(device, dict) else ""
        return ToolResult.success(f"Nothing is playing on Spotify right now.{where}", data={})
    lines = [f"{track['name']} - {track['artist']}"]
    if track.get("album"):
        lines.append(f"Album: {track['album']}")
    lines.append("Playing" if track.get("playing") else "Paused")
    return ToolResult.success("\n".join(lines), data=track)


@tool(
    name="spotify_search",
    description="Search Spotify for tracks, artists, albums or playlists and list the matches.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "kind": {"type": "string", "description": "track (default), artist, album or playlist"},
            "limit": {"type": "integer", "description": "how many matches (default 5)"},
        },
        "required": ["query"],
    },
    category="media",
)
def spotify_search(query: str, kind: str = "track", limit: int = 5, ctx: Any = None) -> ToolResult:
    settings = getattr(ctx, "settings", None)
    text = (query or "").strip()
    if not text:
        return ToolResult.failure("what should I search Spotify for?")
    if _require_link(settings):
        return ToolResult.failure(NOT_LINKED)
    try:
        results = search(text, kind=kind, limit=limit, settings=settings)
    except RuntimeError as exc:
        return ToolResult.failure(str(exc))
    if not results:
        return ToolResult.failure(f"Spotify had no {kind} matching '{text}'")
    lines = [f"Spotify {kind} matches for '{text}':"]
    for index, item in enumerate(results, 1):
        lines.append(f"  {index}. {item['name']}" + (f" - {item['artist']}" if item["artist"] else ""))
    return ToolResult.success("\n".join(lines), data={"query": text, "results": results})


@tool(
    name="spotify_search_and_play",
    description=(
        "Play something specific on Spotify: a track, artist, album or playlist by name. "
        "Needs Spotify linked and an active Spotify device."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "what to play; empty resumes playback"},
            "kind": {"type": "string", "description": "track (default), artist, album or playlist"},
        },
        "required": ["query"],
    },
    category="media",
    aliases=("spotify_play",),
)
def spotify_search_and_play(query: str, kind: str = "track", ctx: Any = None) -> ToolResult:
    settings = getattr(ctx, "settings", None)
    text = (query or "").strip()
    if _require_link(settings):
        return ToolResult.failure(NOT_LINKED)
    try:
        if not text:
            _play(settings=settings)
            track = current_track(settings)
            return ToolResult.success(
                f"Resumed {_track_line(track) if track else 'playback'}." if track else "Playback resumed.",
                data=track or {},
            )
        results = search(text, kind=kind, limit=5, settings=settings)
        if not results:
            return ToolResult.failure(f"Spotify had no {kind} matching '{text}'")
        chosen = _best_match(results, (kind or "track").lower())
        if chosen is None or not chosen.get("uri"):
            return ToolResult.failure(f"I couldn't pick a playable result for '{text}'")
        if chosen["kind"] == "track":
            _play(uris=[str(chosen["uri"])], settings=settings)
        else:
            _play(context=str(chosen["uri"]), settings=settings)
    except RuntimeError as exc:
        message = str(exc)
        # A missing device is the one failure the user can fix in five seconds,
        # so say what to do rather than just what broke.
        if "active device" in message.lower():
            message += (
                f" I can open '{text}' in the browser instead if you like - just ask me to open it."
            )
        return ToolResult.failure(message)
    label = f"{chosen['name']}" + (f" - {chosen['artist']}" if chosen.get("artist") else "")
    return ToolResult.success(f"Playing {label} on Spotify.", data=chosen)


@tool(
    name="spotify_control",
    description="Control Spotify playback through the API: play, pause, next, previous, volume, shuffle, repeat.",
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "one of: play, pause, toggle, next, previous, volume, shuffle, repeat",
            },
            "level": {"type": "integer", "description": "0-100, for action=volume"},
            "state": {"type": "string", "description": "for shuffle/repeat: on, off, track, context"},
        },
        "required": ["action"],
    },
    category="media",
)
def spotify_control(action: str, level: int = 0, state: str = "", ctx: Any = None) -> ToolResult:
    settings = getattr(ctx, "settings", None)
    wanted = (action or "").strip().lower().replace("_", "-")
    if _require_link(settings):
        return ToolResult.failure(NOT_LINKED)

    toggle = (state or "").strip().lower()
    try:
        if wanted in {"play", "resume"}:
            _play(settings=settings)
            return ToolResult.success("Spotify playback resumed.", data={})
        if wanted in {"pause"}:
            _api("/me/player/pause", method="PUT", payload={}, settings=settings)
            return ToolResult.success("Spotify paused.", data={})
        if wanted in {"toggle", "play-pause"}:
            track = current_track(settings)
            if track and track.get("playing"):
                _api("/me/player/pause", method="PUT", payload={}, settings=settings)
                return ToolResult.success("Spotify paused.", data={})
            _play(settings=settings)
            return ToolResult.success("Spotify playing.", data={})
        if wanted in {"next", "skip"}:
            _api("/me/player/next", method="POST", payload={}, settings=settings)
            return ToolResult.success("Skipped to the next track.", data={})
        if wanted in {"previous", "back"}:
            _api("/me/player/previous", method="POST", payload={}, settings=settings)
            return ToolResult.success("Went back a track.", data={})
        if wanted == "volume":
            try:
                percent = max(0, min(100, int(level)))
            except (TypeError, ValueError):
                return ToolResult.failure("give me a volume between 0 and 100")
            _api("/me/player/volume", method="PUT", params={"volume_percent": percent}, settings=settings)
            return ToolResult.success(f"Spotify volume set to {percent}%.", data={})
        if wanted == "shuffle":
            on = toggle != "off"
            _api("/me/player/shuffle", method="PUT", params={"state": str(on).lower()}, settings=settings)
            return ToolResult.success(f"Shuffle {'on' if on else 'off'}.", data={})
        if wanted in {"repeat", "loop"}:
            mode = toggle if toggle in {"track", "context", "off"} else "context"
            _api("/me/player/repeat", method="PUT", params={"state": mode}, settings=settings)
            return ToolResult.success(f"Repeat set to {mode}.", data={})
    except RuntimeError as exc:
        return ToolResult.failure(str(exc))
    return ToolResult.failure(
        "I can play, pause, toggle, skip, go back, change the volume, shuffle or repeat on Spotify."
    )


@tool(
    name="spotify_liked_songs",
    description="List the songs saved in the user's Spotify Liked Songs.",
    parameters={
        "type": "object",
        "properties": {"limit": {"type": "integer", "description": "how many to list (default 10)"}},
    },
    category="media",
    aliases=("liked_songs", "spotify_liked"),
)
def spotify_liked_songs(limit: int = 10, ctx: Any = None) -> ToolResult:
    settings = getattr(ctx, "settings", None)
    if _require_link(settings):
        return ToolResult.failure(NOT_LINKED)
    count = max(1, min(int(limit or 10), 50))
    try:
        data = _api("/me/tracks", params={"limit": count}, settings=settings)
    except RuntimeError as exc:
        return ToolResult.failure(str(exc))
    items = (data or {}).get("items") or []
    if not items:
        return ToolResult.failure("your Liked Songs are empty")
    lines = [f"Your last {len(items)} liked songs:"]
    tracks: List[Dict[str, Any]] = []
    for entry in items:
        track = entry.get("track") or {}
        lines.append(f"  {_track_line(track)}")
        tracks.append({"name": track.get("name"), "artist": ", ".join(
            str(a.get("name", "")) for a in (track.get("artists") or []) if a.get("name"))})
    total = (data or {}).get("total")
    if isinstance(total, int):
        lines.append(f"({total} songs saved in total)")
    return ToolResult.success("\n".join(lines), data={"tracks": tracks, "total": total})


@tool(
    name="spotify_like_current",
    description="Like or unlike the track that is playing right now on Spotify.",
    parameters={
        "type": "object",
        "properties": {
            "liked": {"type": "boolean", "description": "true to save, false to remove (default true)"}
        },
    },
    category="media",
    aliases=("like_current_song", "save_song"),
)
def spotify_like_current(liked: bool = True, ctx: Any = None) -> ToolResult:
    settings = getattr(ctx, "settings", None)
    if _require_link(settings):
        return ToolResult.failure(NOT_LINKED)
    try:
        track = current_track(settings)
        if not track:
            return ToolResult.failure(
                "nothing is playing on Spotify right now, so there is no track to save. "
                "Play something first (or tell me what to play) and ask me again."
            )
        _api(
            "/me/tracks",
            method="PUT" if liked else "DELETE",
            payload={"ids": [track["id"]]},
            settings=settings,
        )
    except RuntimeError as exc:
        return ToolResult.failure(str(exc))
    verb = "Saved" if liked else "Removed"
    return ToolResult.success(f"{verb} {_track_line(track)} {'to' if liked else 'from'} your Liked Songs.",
                              data=track)


@tool(
    name="spotify_open",
    description="Open Spotify: the installed app if present, otherwise the web player (optionally on a search).",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "optional thing to search for"}},
    },
    category="media",
    aliases=("open_spotify",),
)
def spotify_open(query: str = "", ctx: Any = None) -> ToolResult:
    from tools.system import open_app, open_url

    text = (query or "").strip()
    web = "https://open.spotify.com/" + (
        "search/" + urllib.parse.quote(text) if text else ""
    )
    if not text:
        launched = open_app("spotify", ctx=ctx)
        if launched.ok:
            return ToolResult.success("Opened the Spotify app.", data={"target": "app"})
    opened = open_url(web)
    if opened.ok:
        message = f"Opened Spotify{' for ' + text if text else ''} in your browser."
        return ToolResult.success(message, data={"url": web})
    return ToolResult.success(f"Here's the link: {web}", data={"url": web})


__all__ = [
    "NOT_LINKED",
    "PREMIUM_HINT",
    "SCOPES",
    "access_token",
    "credentials",
    "current_track",
    "forget_token",
    "search",
]
