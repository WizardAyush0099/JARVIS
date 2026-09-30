"""What JARVIS actually says out loud.

The console shows the whole answer and speaks a version of it.  Reading a wall
of text aloud is slow to synthesize, slow to listen to, and impossible to follow
while typing the next question - so a long reply is cut at a sentence boundary
and closed with a line that points at the screen.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from voice.tts import (  # noqa: E402
    DEFAULT_SPEAK_LIMIT,
    SPOKEN_TAIL,
    clean_for_speech,
)


def test_markdown_and_links_are_not_read_aloud():
    spoken = clean_for_speech(
        "**Done.** I opened `chromium` and went to "
        "[the docs](https://example.com/docs) — see https://example.com/x"
    )
    assert "**" not in spoken
    assert "`" not in spoken
    assert "]" not in spoken
    assert "https://" not in spoken
    assert "Done." in spoken
    assert "chromium" in spoken


def test_code_blocks_are_replaced_not_spelled_out():
    spoken = clean_for_speech(
        "Here it is:\n```python\nsecret_token_xyz = 42\n```\nThat writes the token."
    )
    assert "secret_token_xyz" not in spoken
    assert "code omitted" in spoken
    assert "That writes the token." in spoken


def test_a_short_answer_is_spoken_whole():
    text = "It is 22 degrees and the CPU is at 4 percent."
    assert clean_for_speech(text, DEFAULT_SPEAK_LIMIT) == text
    assert SPOKEN_TAIL not in clean_for_speech(text, DEFAULT_SPEAK_LIMIT)


def test_a_long_answer_stops_at_a_sentence_and_points_at_the_screen():
    text = (
        "Your Pi is running fine. " + "The temperature is 48 degrees and the memory "
        "is at 31 percent. " * 12
    ) + "That is the whole picture."

    spoken = clean_for_speech(text, 120)

    assert len(spoken) <= 120 + len(SPOKEN_TAIL) + 1
    assert spoken.endswith(SPOKEN_TAIL)
    assert spoken.startswith("Your Pi is running fine.")
    # the cut landed on a sentence, so nothing is left dangling mid-word
    assert not spoken.replace(SPOKEN_TAIL, "").rstrip().endswith("memor")


def test_a_long_answer_without_sentences_still_says_something_readable():
    spoken = clean_for_speech("word " * 200, 60)

    assert spoken.endswith(SPOKEN_TAIL)
    assert spoken.count(" ") > 5  # a real phrase, not one chopped token
    assert not spoken.replace(SPOKEN_TAIL, "").strip().endswith("wor")


def test_zero_means_read_everything():
    text = "This sentence keeps going. " * 40
    assert clean_for_speech(text, 0) == clean_for_speech(text)


@pytest.mark.parametrize("limit", [-5, None, "not a number"])
def test_an_unusable_limit_falls_back_to_speaking_everything(limit):
    text = "A short answer."
    assert clean_for_speech(text, limit) == text


def test_the_speaker_reads_the_configured_limit_from_settings(settings):
    """The setting is what the three speaking paths use - not a magic number."""
    from voice.tts import Speaker

    settings.tts.speak_limit = 80
    speaking = Speaker(settings, events=None, engine=_SilentEngine())

    assert speaking.speak_limit == 80
    assert speaking.status()["speak_limit"] == 80


def test_a_long_reply_is_shortened_on_the_way_to_the_engine(settings):
    """End to end through the Speaker: the engine is handed the short version."""
    from voice.tts import Speaker

    settings.tts.speak_limit = 60
    settings.tts.enabled = True  # the shared fixture starts with the voice off
    engine = _RecordingEngine()
    speaking = Speaker(settings, events=None, engine=engine)

    speaking.say_now(
        "The Pi is healthy. " + "The temperature is 48 degrees Celsius. " * 10
    )

    assert engine.spoken, "the engine was never asked to speak"
    assert len(engine.spoken[0]) < 60 + len(SPOKEN_TAIL) + 1
    assert engine.spoken[0].endswith(SPOKEN_TAIL)


class _SilentEngine:
    """An engine that produces no audio, so the speaker never touches a sound card."""

    name = "silent"

    def available(self) -> bool:
        return True

    def synthesize(self, text, language, cache_dir, settings):  # noqa: ANN001, D401
        return None


class _RecordingEngine(_SilentEngine):
    """Remembers every phrase it was asked for."""

    def __init__(self) -> None:
        self.spoken: list = []

    def synthesize(self, text, language, cache_dir, settings):  # noqa: ANN001, D401
        self.spoken.append(text)
        return None
