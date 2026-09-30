"""Media control must really drive the player - and say so when it cannot.

No D-Bus and no network here: a tiny stand-in for ``playerctl`` is put on the
PATH, so these tests assert the exact command line JARVIS builds and the exact
wording it reports back.  That is the only way to test a transport control
honestly without a speaker in CI.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai.offline import OfflineEngine
from server.app import create_app
from tests.test_voice_api import make_jarvis
from tools import media
from tools.base import ToolContext

#: Same argv shape, same output shape as the real playerctl, with no D-Bus.
FAKE_PLAYERCTL = """#!/bin/sh
if [ -n "$FAKE_PLAYERCTL_LOG" ]; then
  printf '%s\\n' "$*" >> "$FAKE_PLAYERCTL_LOG"
fi

if [ "$1" = "-l" ]; then
  if [ "$FAKE_NO_PLAYERS" = "1" ]; then
    exit 1
  fi
  printf '%s\\n' spotify chromium
  exit 0
fi

player="$2"
sub="$3"
arg="$4"

case "$sub" in
  metadata)
    if [ "$player" = "chromium" ]; then
      printf '%s\\n' "xesam:title              Lofi Beats"
      exit 0
    fi
    printf '%s\\n' "xesam:title              A Moment Apart"
    printf '%s\\n' "xesam:artist             ODESZA"
    printf '%s\\n' "xesam:album              A Moment Apart"
    printf '%s\\n' "mpris:length             219000000"
    exit 0
    ;;
  status)
    printf '%s\\n' "${FAKE_STATUS:-Playing}"
    exit 0
    ;;
  position)
    printf '%s\\n' 42.5
    exit 0
    ;;
  volume)
    if [ -z "$arg" ]; then
      printf '%s\\n' 0.75
    fi
    exit 0
    ;;
esac

if [ "$sub" = "shuffle" ] && [ "$FAKE_SHUFFLE_BROKEN" = "1" ]; then
  printf '%s\\n' "No player could handle this command" >&2
  exit 1
fi

case "$sub" in
  play|pause|play-pause|next|previous|stop|shuffle|loop) exit 0 ;;
esac

printf '%s\\n' "Unknown command" >&2
exit 1
"""


class FakePlayerctl:
    """A playerctl on the PATH, plus the log of everything it was asked to do."""

    def __init__(self, log: Path) -> None:
        self._log = log

    def calls(self) -> list:
        if not self._log.exists():
            return []
        return [line for line in self._log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def did(self, command: str) -> bool:
        return any(line.endswith(command) or line == command for line in self.calls())


@pytest.fixture
def fake_playerctl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakePlayerctl:
    if sys.platform == "win32":  # pragma: no cover - the Pi is the target
        pytest.skip("the fake playerctl is a shell script")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "playerctl"
    script.write_text(FAKE_PLAYERCTL, encoding="utf-8")
    script.chmod(0o755)

    log = tmp_path / "playerctl.log"
    monkeypatch.setenv("FAKE_PLAYERCTL_LOG", str(log))
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    for name in ("FAKE_NO_PLAYERS", "FAKE_STATUS", "FAKE_SHUFFLE_BROKEN"):
        monkeypatch.delenv(name, raising=False)
    return FakePlayerctl(log)


@pytest.fixture
def no_playerctl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


# --------------------------------------------------------------------------- #
# reading the player
# --------------------------------------------------------------------------- #
def test_now_playing_comes_from_the_player_itself(fake_playerctl: FakePlayerctl):
    state = media.snapshot()
    assert state["available"] is True
    assert state["player"] == "spotify"
    assert state["title"] == "A Moment Apart"
    assert state["artist"] == "ODESZA"
    assert state["album"] == "A Moment Apart"
    assert state["playing"] is True
    assert state["status"] == "Playing"
    assert state["length_s"] == 219.0  # mpris:length is microseconds
    assert state["position_s"] == 42.5
    assert state["volume"] == 0.75


def test_a_named_player_can_be_targeted(fake_playerctl: FakePlayerctl):
    state = media.snapshot("chromium")
    assert state["player"] == "chromium"
    assert state["title"] == "Lofi Beats"


def test_a_polling_reader_reuses_a_recent_snapshot(fake_playerctl: FakePlayerctl):
    """A cached answer must not cost another fork of playerctl.

    The console polls /api/media; without this the Pi spawns three processes
    every poll for a title that changes far more slowly.
    """
    media._SNAPSHOT_CACHE.clear()

    first = media.snapshot("", media.SNAPSHOT_TTL)
    calls_after_first = len(fake_playerctl.calls())

    second = media.snapshot("", media.SNAPSHOT_TTL)

    assert second is first  # the very same answer, not a fresh read
    assert len(fake_playerctl.calls()) == calls_after_first
    # a different player is a different question, so it is never served from
    # another player's cache entry
    media.snapshot("chromium", media.SNAPSHOT_TTL)
    assert len(fake_playerctl.calls()) > calls_after_first


def test_the_uncached_default_always_reads_the_player(fake_playerctl: FakePlayerctl):
    media._SNAPSHOT_CACHE.clear()
    first = media.snapshot()
    calls_after_first = len(fake_playerctl.calls())
    second = media.snapshot()

    assert second is not first
    assert len(fake_playerctl.calls()) > calls_after_first


def test_a_transport_action_never_shows_a_stale_track(fake_playerctl: FakePlayerctl, settings):
    media._SNAPSHOT_CACHE.clear()
    media.snapshot("", media.SNAPSHOT_TTL)  # prime the cache
    media.apply_action("pause")

    result = media.media_now_playing()

    assert result.ok is True
    assert result.output is not None
    assert fake_playerctl.did("--player spotify pause")


def test_nothing_playing_is_reported_not_invented(fake_playerctl: FakePlayerctl, monkeypatch):
    monkeypatch.setenv("FAKE_NO_PLAYERS", "1")
    state = media.snapshot()
    assert state["available"] is False
    assert "nothing is playing" in state["reason"]
    result = media.media_now_playing()
    assert result.ok is False
    assert "nothing is playing" in result.error


def test_now_playing_tool_formats_the_track(fake_playerctl: FakePlayerctl):
    result = media.media_now_playing()
    assert result.ok is True
    assert "A Moment Apart" in result.output
    assert "ODESZA" in result.output
    assert result.data["playing"] is True


# --------------------------------------------------------------------------- #
# driving the player
# --------------------------------------------------------------------------- #
def test_pause_builds_the_right_command(fake_playerctl: FakePlayerctl, monkeypatch):
    monkeypatch.setenv("FAKE_STATUS", "Paused")  # what a real player reports afterwards
    ok, message = media.apply_action("pause")
    assert ok is True
    assert "Paused" in message and "A Moment Apart" in message
    assert "--player spotify pause" in fake_playerctl.calls()


def test_a_player_that_ignores_the_command_is_not_claimed_as_paused(
    fake_playerctl: FakePlayerctl
):
    ok, message = media.apply_action("pause")  # the fake keeps reporting "Playing"
    assert ok is True
    assert "still reports playing" in message
    assert "Paused" not in message


@pytest.mark.parametrize(
    "action,expected",
    [
        ("play", "--player spotify play"),
        ("resume", "--player spotify play-pause"),
        ("toggle", "--player spotify play-pause"),
        ("next", "--player spotify next"),
        ("skip", "--player spotify next"),
        ("previous", "--player spotify previous"),
        ("stop", "--player spotify stop"),
        ("mute", "--player spotify volume 0"),
        ("shuffle", "--player spotify shuffle toggle"),
    ],
)
def test_every_transport_word_maps_to_a_real_command(
    fake_playerctl: FakePlayerctl, action: str, expected: str
):
    ok, _ = media.apply_action(action)
    assert ok is True
    assert expected in fake_playerctl.calls()


def test_volume_up_is_a_relative_step(fake_playerctl: FakePlayerctl):
    ok, _ = media.apply_action("louder")
    assert ok is True
    assert "--player spotify volume 0.10+" in fake_playerctl.calls()


def test_volume_can_be_set_as_a_percentage(fake_playerctl: FakePlayerctl):
    ok, _ = media.apply_action("volume", level=35)
    assert ok is True
    assert "--player spotify volume 0.35" in fake_playerctl.calls()


def test_a_refused_action_is_reported_not_hidden(fake_playerctl: FakePlayerctl, monkeypatch):
    monkeypatch.setenv("FAKE_SHUFFLE_BROKEN", "1")
    ok, message = media.apply_action("shuffle")
    assert ok is False
    assert "refused" in message


def test_media_control_tool_surfaces_the_refusal(fake_playerctl: FakePlayerctl, monkeypatch):
    monkeypatch.setenv("FAKE_SHUFFLE_BROKEN", "1")
    result = media.media_control("shuffle")
    assert result.ok is False
    assert "refused" in result.error


def test_unknown_actions_list_what_is_supported(fake_playerctl: FakePlayerctl):
    ok, message = media.apply_action("teleport")
    assert ok is False
    assert "pause" in message and "next" in message


def test_missing_playerctl_explains_how_to_install(no_playerctl):
    state = media.snapshot()
    assert state["available"] is False
    assert "playerctl" in state["reason"]
    ok, message = media.apply_action("pause")
    assert ok is False
    assert "sudo apt install playerctl" in message
    assert media.media_players().ok is False


def test_controlling_an_unknown_player_says_which_ones_exist(fake_playerctl: FakePlayerctl):
    ok, message = media.apply_action("play", player="winamp")
    assert ok is False
    assert "spotify" in message


# --------------------------------------------------------------------------- #
# play_music - the one answer to "play some music"
# --------------------------------------------------------------------------- #
def test_play_music_resumes_what_is_paused(fake_playerctl: FakePlayerctl, monkeypatch, settings):
    monkeypatch.setenv("FAKE_STATUS", "Paused")
    result = media.play_music(ctx=ToolContext(settings=settings))
    assert result.ok is True
    assert "--player spotify play" in fake_playerctl.calls()


def test_play_music_says_when_music_is_already_playing(fake_playerctl: FakePlayerctl, settings):
    result = media.play_music(ctx=ToolContext(settings=settings))
    assert result.ok is True
    assert "Already playing" in result.output
    # it reports instead of touching playback that was already fine
    assert "--player spotify play" not in fake_playerctl.calls()


def test_play_music_without_a_spotify_link_says_how_to_link(
    fake_playerctl: FakePlayerctl, monkeypatch, settings
):
    from tools import spotify as spotify_tools
    from tools.base import ToolResult

    monkeypatch.setattr(
        spotify_tools,
        "spotify_open",
        lambda query="", ctx=None: ToolResult.success("Opened Spotify in your browser."),
    )
    result = media.play_music("lofi beats", ctx=ToolContext(settings=settings))
    assert result.ok is True
    assert "not linked" in result.output
    assert "scripts/spotify_auth.py" in result.output


# --------------------------------------------------------------------------- #
# offline + API + HUD
# --------------------------------------------------------------------------- #
def test_the_offline_engine_can_pause_the_music(fake_playerctl: FakePlayerctl, monkeypatch, settings):
    """The whole point: music control needs no key, no model and no internet."""
    monkeypatch.setenv("FAKE_STATUS", "Paused")
    engine = OfflineEngine(settings)
    answer = engine.answer("pause the music")
    assert answer is not None
    assert "Paused" in answer
    assert "--player spotify pause" in fake_playerctl.calls()


def test_the_offline_engine_only_gets_local_media_tools(settings):
    """Pause/skip/volume are local; anything reaching Spotify is not."""
    from tools.base import get_tool

    assert get_tool("media_control").offline_safe is True
    assert get_tool("media_now_playing").offline_safe is True
    assert get_tool("play_music").offline_safe is False  # may reach Spotify's API
    for name in ("spotify_liked_songs", "spotify_search_and_play", "spotify_control"):
        assert get_tool(name).offline_safe is False


def test_the_offline_engine_can_start_music_that_is_paused(fake_playerctl, settings):
    from core import intent as intent_layer

    found = intent_layer.match("play some music", owner="Ayush", assistant="JARVIS")
    assert found is not None and found.tool == "media_control" and found.category == "media"
    assert OfflineEngine(settings).answer("play some music") is not None


def test_the_console_transport_uses_the_same_control(fake_playerctl: FakePlayerctl, settings, events):
    jarvis = make_jarvis(settings, events)
    with TestClient(create_app(settings, jarvis)) as client:
        state = client.get("/api/media").json()
        assert state["available"] is True
        assert state["title"] == "A Moment Apart"

        result = client.post("/api/media", json={"action": "next"}).json()
        assert result["ok"] is True
        assert "--player spotify next" in fake_playerctl.calls()

        refused = client.post("/api/media", json={"action": "nope"}).json()
        assert refused["ok"] is False
        assert "pause" in refused["message"]


def test_the_hud_ships_the_media_panel(settings, events):
    jarvis = make_jarvis(settings, events)
    with TestClient(create_app(settings, jarvis)) as client:
        html = client.get("/").text
        for marker in ('id="block-media"', 'id="media-title"', 'id="transport"', 'data-media="toggle"'):
            assert marker in html, marker
