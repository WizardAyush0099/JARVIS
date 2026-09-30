"""Media control - play, pause, skip and volume for whatever is playing.

Everything here goes through **MPRIS over D-Bus**, via ``playerctl``.  That one
interface is spoken by every media app on Linux, so a single set of tools drives
all of them:

* the Spotify desktop app, or the Spotify web player open in Chromium
* a YouTube (or any other) video playing in a Chromium tab
* VLC, mpv, Rhythmbox, Lollypop, ``cvlc`` - anything that registers a media bus

Why MPRIS instead of a vendor API: it controls *whatever is playing right now*,
needs no account, no key and no internet, and it keeps working when a vendor
changes their API.  It also means "pause the music" works on a Pi with nothing
configured at all.

Nothing here is simulated.  Every call is a real ``playerctl`` invocation and
every answer is read back from the player, so if a command is refused JARVIS says
so rather than pretending it worked.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from core.logging_setup import get_logger
from tools.base import ToolContext, ToolResult, tool

log = get_logger("tools.media")

#: How long any single playerctl call may take.  D-Bus replies are instant; a
#: hang here means the session bus is wedged, not that the song is long.
CALL_TIMEOUT = 6.0

INSTALL_HINT = (
    "media control needs playerctl, which talks to your music player over D-Bus. "
    "Install it with: sudo apt install playerctl"
)


# --------------------------------------------------------------------------- #
# playerctl plumbing
# --------------------------------------------------------------------------- #
def playerctl_path() -> Optional[str]:
    return shutil.which("playerctl")


def _run(args: List[str], timeout: float = CALL_TIMEOUT) -> Tuple[int, str, str]:
    """Run one playerctl command.  Returns (code, stdout, stderr)."""
    binary = playerctl_path()
    if binary is None:
        raise FileNotFoundError(INSTALL_HINT)
    completed = subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _lines(output: str) -> List[str]:
    return [line.strip() for line in output.splitlines() if line.strip()]


def players() -> List[str]:
    """Every media player currently on the bus, in the order playerctl lists them."""
    try:
        code, out, _ = _run(["-l"])
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.debug("playerctl --list failed: %s", exc)
        return []
    if code != 0:
        return []
    return _lines(out)


def _metadata(player: str) -> Dict[str, str]:
    """Read one player's metadata as a plain dict of MPRIS keys."""
    try:
        code, out, _ = _run(["--player", player, "metadata"])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if code != 0 or not out.strip():
        return {}

    values: Dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        # playerctl prints "key<pad>value"; keys never contain whitespace
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        key, value = parts[0].strip(), parts[1].strip()
        if key in values:  # multi-valued (e.g. several artists): keep them all
            values[key] = f"{values[key]}, {value}"
        else:
            values[key] = value
    return values


def _status(player: str) -> str:
    try:
        code, out, _ = _run(["--player", player, "status"])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "Unknown"
    if code != 0:
        return "Unknown"
    return (_lines(out) or ["Unknown"])[0]


def _number(command: List[str]) -> Optional[float]:
    try:
        code, out, _ = _run(command)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if code != 0:
        return None
    try:
        return float((_lines(out) or [""])[0])
    except ValueError:
        return None


def _seconds(value: Any) -> Optional[float]:
    """MPRIS lengths are microseconds."""
    try:
        return round(float(value) / 1_000_000, 1)
    except (TypeError, ValueError):
        return None


def _choose(found: List[str], preferred: str = "") -> Optional[str]:
    """Pick a player from an already-listed set: named, playing, else the first.

    Split out so a status read lists the players exactly once - every playerctl
    call is a process spawn, and this runs on a Pi every few seconds.
    """
    if not found:
        return None
    if preferred:
        wanted = preferred.strip().lower()
        for name in found:
            if name.lower() == wanted:
                return name
        for name in found:
            if wanted in name.lower():
                return name
        return None
    for name in found:
        if _status(name).lower() == "playing":
            return name
    return found[0]


def active_player(preferred: str = "") -> Optional[str]:
    """Pick a player: the named one, else whatever is playing, else the first."""
    return _choose(players(), preferred)


def snapshot(player: str = "") -> Dict[str, Any]:
    """What is playing right now, read from the player itself.

    Always returns a dict.  ``available`` is False when there is no player at all
    (or playerctl is missing) - the console shows that honestly instead of an
    empty track title.
    """
    if playerctl_path() is None:
        return {"available": False, "reason": INSTALL_HINT, "players": [], "count": 0}

    names = players()
    chosen = _choose(names, player)
    if chosen is None:
        return {
            "available": False,
            "reason": "nothing is playing on this machine right now",
            "players": names,
            "count": 0,
        }

    meta = _metadata(chosen)
    status = _status(chosen)
    position = _number(["--player", chosen, "position"]) if status.lower() == "playing" else None
    volume = _number(["--player", chosen, "volume"])

    return {
        "available": True,
        "player": chosen,
        "players": names,
        "count": len(names),
        "status": status,
        "playing": status.lower() == "playing",
        "title": meta.get("xesam:title", ""),
        "artist": meta.get("xesam:artist", ""),
        "album": meta.get("xesam:album", ""),
        "track": meta.get("mpris:trackid", ""),
        "art_url": meta.get("mpris:artUrl", ""),
        "length_s": _seconds(meta.get("mpris:length")),
        "position_s": position,
        "volume": volume,
    }


def _describe(state: Dict[str, Any]) -> str:
    title = state.get("title") or ""
    artist = state.get("artist") or ""
    if title and artist:
        return f"{title} - {artist}"
    return title or artist or "unknown track"


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #
#: What the user says -> the playerctl command (and the tool argument accepted).
ACTIONS: Dict[str, List[str]] = {
    "play": ["play"],
    "pause": ["pause"],
    "toggle": ["play-pause"],
    "play-pause": ["play-pause"],
    "resume": ["play-pause"],
    "next": ["next"],
    "skip": ["next"],
    "previous": ["previous"],
    "back": ["previous"],
    "stop": ["stop"],
    "shuffle": ["shuffle", "toggle"],
    "shuffle-on": ["shuffle", "on"],
    "shuffle-off": ["shuffle", "off"],
    "loop": ["loop", "toggle"],
    "loop-track": ["loop", "Track"],
    "loop-playlist": ["loop", "Playlist"],
    "loop-off": ["loop", "None"],
}

#: Actions that need a volume argument instead of a fixed command.
VOLUME_UP = ("volume-up", "louder", "turn it up", "volume up")
VOLUME_DOWN = ("volume-down", "quieter", "turn it down", "volume down")
MUTE = ("mute", "silence")

#: The whole vocabulary a caller may use, so a model cannot invent an action.
ACTION_NAMES = sorted(set(ACTIONS) | {"volume-up", "volume-down", "mute", "volume"})


def apply_action(
    action: str, player: str = "", level: int = 0, step: float = 0.1
) -> Tuple[bool, str]:
    """Run one transport action.  Returns ``(ok, message)`` with a real reason."""
    wanted = (action or "").strip().lower().replace("_", "-")
    if not wanted:
        return False, "which control should I use? I know: " + ", ".join(ACTION_NAMES)

    if playerctl_path() is None:
        return False, INSTALL_HINT

    chosen = active_player(player)
    if chosen is None:
        known = players()
        if known:
            return False, f"I can't find a player called '{player}'. Open: {', '.join(known)}"
        return False, (
            "nothing is playing on this machine yet, so there is nothing to control - "
            "start a song or a video first"
        )

    base = ["--player", chosen]

    if wanted.startswith(VOLUME_UP) or wanted in VOLUME_UP:
        command = base + ["volume", f"{abs(step):.2f}+"]
    elif wanted.startswith(VOLUME_DOWN) or wanted in VOLUME_DOWN:
        command = base + ["volume", f"{abs(step):.2f}-"]
    elif wanted in MUTE:
        command = base + ["volume", "0"]
    elif wanted == "volume":
        try:
            percent = max(0, min(100, int(level)))
        except (TypeError, ValueError):
            return False, "give me a volume between 0 and 100"
        command = base + ["volume", f"{percent / 100:.2f}"]
    elif wanted in ACTIONS:
        command = base + ACTIONS[wanted]
    else:
        return False, f"I don't know the control '{action}'. I know: {', '.join(ACTION_NAMES)}"

    try:
        code, _, err = _run(command)
    except FileNotFoundError:
        return False, INSTALL_HINT
    except subprocess.TimeoutExpired:
        return False, f"the player ({chosen}) did not answer in time"

    if code != 0:
        detail = (err or "").strip().splitlines()
        reason = detail[0] if detail else f"playerctl exited with {code}"
        # A missing Shuffle/Loop capability is normal: it is the player, not us.
        return False, f"{chosen} refused '{wanted}': {reason}"

    # Read the player back, and admit it when it disagrees with what we asked
    # for - a transport control that says "paused" over a still-playing song is
    # exactly the kind of fake answer this project exists to avoid.
    state = snapshot(chosen)
    title = _describe(state)
    still_playing = bool(state.get("playing"))
    wants_pause = wanted in {"pause", "stop"}

    if wants_pause and still_playing:
        return True, f"Sent {wanted} to {chosen}, but it still reports playing."
    if wanted == "stop":
        return True, f"Stopped {title} on {chosen}."
    if wanted == "pause":
        return True, f"Paused {title} on {chosen}."
    if wanted in {"play", "resume"} and not still_playing:
        return True, f"Asked {chosen} to play, but it still reports paused."
    if still_playing:
        return True, f"Playing {title} on {chosen}."
    return True, f"Done ({wanted}) on {chosen}."


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
@tool(
    name="media_now_playing",
    description=(
        "What is playing right now on this machine: track, artist, player and whether "
        "it is playing or paused. Works for Spotify, YouTube in the browser, VLC and "
        "anything else that plays media."
    ),
    parameters={
        "type": "object",
        "properties": {
            "player": {"type": "string", "description": "optional player name (spotify, chromium, vlc, ...)"}
        },
    },
    category="media",
    offline_safe=True,
    aliases=("whats_playing", "now_playing", "current_song"),
)
def media_now_playing(player: str = "") -> ToolResult:
    state = snapshot(player)
    if not state.get("available"):
        return ToolResult.failure(state.get("reason", "nothing is playing"), data=state)

    title = state.get("title") or "an unknown track"
    artist = state.get("artist")
    lines = [f"{title}" + (f" by {artist}" if artist else "")]
    if state.get("album"):
        lines.append(f"Album: {state['album']}")
    lines.append(f"{state.get('status', 'unknown')} on {state.get('player')}")
    if state.get("position_s") is not None and state.get("length_s"):
        lines.append(f"{state['position_s']:.0f}s of {state['length_s']:.0f}s")
    if state.get("players") and len(state["players"]) > 1:
        lines.append("Other players: " + ", ".join(state["players"]))
    return ToolResult.success("\n".join(lines), data=state)


@tool(
    name="media_control",
    description=(
        "Control whatever is playing: play, pause, toggle, next, previous, stop, "
        "volume up/down/set, mute, shuffle or loop. Works with Spotify, YouTube in "
        "the browser, VLC and any other media player on this machine."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": (
                    "one of: play, pause, toggle, resume, next, previous, stop, "
                    "volume-up, volume-down, volume, mute, shuffle, shuffle-on, "
                    "shuffle-off, loop, loop-track, loop-playlist, loop-off"
                ),
            },
            "player": {"type": "string", "description": "optional player name"},
            "level": {"type": "integer", "description": "0-100, for action=volume"},
        },
        "required": ["action"],
    },
    category="media",
    offline_safe=True,
    aliases=("media_play", "media_pause", "media_next", "media_previous", "music_control"),
)
def media_control(action: str, player: str = "", level: int = 0) -> ToolResult:
    ok, message = apply_action(action, player=player, level=level)
    if not ok:
        return ToolResult.failure(message)
    return ToolResult.success(message, data=snapshot(player))


@tool(
    name="media_players",
    description="List the media players running on this machine and what each one is doing.",
    parameters={"type": "object", "properties": {}},
    category="media",
    offline_safe=True,
    aliases=("list_players",),
)
def media_players() -> ToolResult:
    if playerctl_path() is None:
        return ToolResult.failure(INSTALL_HINT)
    names = players()
    if not names:
        return ToolResult.failure(
            "no media player is running right now - start a song or a video and I can control it"
        )
    lines = ["Media players:"]
    listed: List[Dict[str, Any]] = []
    for name in names:
        state = snapshot(name)
        status = state.get("status", "unknown")
        lines.append(f"  {name}: {status} - {_describe(state)}")
        listed.append({"player": name, "status": status, "title": state.get("title", "")})
    return ToolResult.success("\n".join(lines), data={"players": listed})


@tool(
    name="play_music",
    description=(
        "Start music now. Resumes whatever is paused on this machine; with a name "
        "('lofi beats', 'Arijit Singh') it plays that on Spotify when Spotify is linked, "
        "and opens Spotify otherwise."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "song, artist, album or playlist to play; empty means just play something",
            }
        },
    },
    category="media",
    # Not offline_safe: with Spotify linked this reaches its API, and the offline
    # engine's contract is that every tool it runs is purely local.  Bare "play
    # music" therefore routes to media_control (MPRIS only), and this cascade is
    # used when a *name* was given - where Spotify is genuinely needed.
    offline_safe=False,
    aliases=("resume_music", "start_music", "play_some_music"),
)
def play_music(query: str = "", ctx: Optional[ToolContext] = None) -> ToolResult:
    """One sensible answer to "play <something>", in three steps.

    1. no name given and something is on this machine -> resume it
    2. Spotify is linked -> play the named track/artist there
    3. otherwise -> open Spotify so a single click starts it
    """
    text = (query or "").strip()
    state = snapshot()

    if not text and state.get("available"):
        if state.get("playing"):
            return ToolResult.success(
                f"Already playing {_describe(state)} on {state['player']}.", data=state
            )
        ok, message = apply_action("play")
        if ok:
            return ToolResult.success(message, data=snapshot())

    # Imported here so a machine with no Spotify link never touches this module.
    from tools import spotify as spotify_tools

    settings = getattr(ctx, "settings", None)
    if spotify_tools.credentials(settings) is not None:
        played = spotify_tools.spotify_search_and_play(text, ctx=ctx)
        if played.ok:
            return played
        if not text:
            opened = spotify_tools.spotify_open("", ctx=ctx)
            if opened.ok:
                return ToolResult.success(
                    opened.output + " Press play there once and I can control it from here.",
                    data=opened.data,
                )
        return played

    opened = spotify_tools.spotify_open(text, ctx=ctx)
    if opened.ok:
        return ToolResult.success(
            opened.output
            + " Spotify is not linked to me yet, so press play once - or run "
            "`scripts/spotify_auth.py` to give me your liked songs and full control.",
            data=opened.data,
        )
    if state.get("available"):
        ok, message = apply_action("play")
        return ToolResult.success(message, data=snapshot()) if ok else ToolResult.failure(message)
    return ToolResult.failure(
        "I couldn't find anything to play: no media player is running and no Spotify "
        "is linked on this machine."
    )


__all__ = [
    "ACTION_NAMES",
    "ACTIONS",
    "INSTALL_HINT",
    "active_player",
    "apply_action",
    "playerctl_path",
    "players",
    "snapshot",
]
