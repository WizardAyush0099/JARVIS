"""Language: English, Hindi and Hinglish.

Ayush talks in three registers, and JARVIS must follow: answer in Hindi when he
writes Hindi, in Hinglish when he writes Hinglish, and switch for good when he
says "speak in hindi".  These tests pin the detection, the explicit command, and
the two ways the preference reaches the model.
"""

from __future__ import annotations

from core import intent, language
from core.brain import ANSWER_SYSTEM
from core.planner import Planner
from core.planner import PlanStep
from ai.offline import OfflineEngine
from core.memory import Memory


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
def test_devanagari_is_read_as_hindi():
    assert language.detect("नमस्ते, कैसे हो?") == language.HINDI
    assert language.detect("आज मौसम कैसा है") == language.HINDI


def test_romanised_hindi_is_read_as_hinglish():
    for text in ("kya haal hai bhai", "mujhe batao abhi", "theek hai yaar", "kal ka weather bolo"):
        assert language.detect(text) == language.HINGLISH, text


def test_plain_english_stays_english():
    for text in ("what time is it", "search for raspberry pi 5 news", "check the cpu usage"):
        assert language.detect(text) == language.ENGLISH, text


def test_preference_values_are_normalised():
    assert language.normalise("hindi") == language.HINDI
    assert language.normalise("HINGLISH") == language.HINGLISH
    assert language.normalise("English") == language.ENGLISH
    assert language.normalise("auto") == ""
    assert language.normalise("") == ""


# --------------------------------------------------------------------------- #
# the explicit command
# --------------------------------------------------------------------------- #
def test_an_explicit_language_request_is_recognised():
    assert language.command("speak in hindi") == language.HINDI
    assert language.command("please answer in English") == language.ENGLISH
    assert language.command("hinglish me bolo") == language.HINGLISH
    assert language.command("hindi mein baat karo") == language.HINDI
    assert language.command("can you talk in hinglish") == language.HINGLISH
    assert language.command("हिंदी में बोलो") == language.HINDI


def test_a_passing_mention_is_not_a_command():
    assert language.command("what does hindi mean") is None
    assert language.command("translate this to hindi later maybe") is None
    assert language.command("i don't speak hindi") is None
    assert language.command("check the cpu") is None


# --------------------------------------------------------------------------- #
# the prompt block
# --------------------------------------------------------------------------- #
def test_instruction_follows_the_message_then_the_preference():
    auto = language.instruction("kya kar rahe ho")
    assert "Hinglish" in auto
    assert "Devanagari" in language.instruction("नमस्ते")
    # an explicit preference beats whatever the message is written in
    forced = language.instruction("what time is it", language.HINDI)
    assert "Hindi" in forced and "Devanagari" in forced


def test_confirmation_is_written_in_the_new_language():
    assert "हिंदी" in language.confirmation("hi")
    assert "Hinglish" in language.confirmation("hinglish")
    assert "English" in language.confirmation("en")


# --------------------------------------------------------------------------- #
# through the brain
# --------------------------------------------------------------------------- #
def test_speaking_in_hindi_sticks_and_confirms_in_hindi(jarvis):
    reply = jarvis.handle("speak in hindi")
    assert reply.text and "हिंदी" in reply.text
    assert jarvis.settings.language == "hi"
    assert jarvis.memory.facts()["language"] == "hi"
    assert "Devanagari" in jarvis.language_instruction("hello")


def test_switching_to_hinglish_then_english(jarvis):
    jarvis.handle("hinglish me bolo")
    assert jarvis.settings.language == "hinglish"
    assert "Hinglish" in jarvis.language_instruction("hello")

    jarvis.handle("speak in english")
    assert jarvis.settings.language == "en"


def test_the_language_survives_a_restart(settings, events):
    from config.settings import Settings
    from core.brain import Jarvis

    first = Jarvis(settings, events, with_tts=False, with_stt=False, hardware=None, enable_reminders=False)
    first.handle("speak in hinglish")

    # a genuinely fresh load, with the .env back at "auto"
    reloaded = Settings.load(root=settings.paths.root)
    assert reloaded.language == "auto"
    fresh = Jarvis(reloaded, events, with_tts=False, with_stt=False, hardware=None, enable_reminders=False)
    assert fresh.language == "hinglish"


def test_an_env_language_wins_over_the_stored_one(settings, events):
    from core.brain import Jarvis

    settings.language = "hi"
    instance = Jarvis(settings, events, with_tts=False, with_stt=False, hardware=None, enable_reminders=False)
    assert instance.language == "hi"


def test_both_prompts_carry_the_language_instruction(jarvis):
    assert "LANGUAGE:" in ANSWER_SYSTEM.format(
        assistant="JARVIS",
        owner="Ayush",
        creator="Ayush",
        visitor="",
        language=jarvis.language_instruction("hello"),
    )
    assert "LANGUAGE:" in jarvis.planner.system_prompt()


# --------------------------------------------------------------------------- #
# deterministic replies
# --------------------------------------------------------------------------- #
def test_the_greeting_follows_the_preference():
    assert "सिस्टम" in intent.match("hello", language="hi").direct_reply
    assert "Bolo" in intent.match("hello", language="hinglish").direct_reply
    assert "Systems online" in intent.match("hello").direct_reply


def test_capabilities_follow_the_preference():
    assert "सकता" in intent.match("what can you do", language="hi").direct_reply
    assert "sakta hoon" in intent.match("what can you do", language="hinglish").direct_reply


# --------------------------------------------------------------------------- #
# the offline notice must not bury real results
# --------------------------------------------------------------------------- #
class _FallingManager:
    """Pretends every online provider failed and the chain fell to offline."""

    last_provider = "offline"

    def has_online_provider(self) -> bool:
        return True

    def chat(self, messages, system=None, **kwargs):
        return (
            "I'm running offline right now, so I can only handle time, dates, "
            "calculations, unit conversions and system status."
        )


def test_real_tool_results_win_over_the_offline_notice(jarvis):
    step = PlanStep(tool="system_status", args={"metric": "cpu"})
    result = jarvis.router.run("system_status", step.args)
    assert result.ok

    jarvis.ai = _FallingManager()
    answer = jarvis._answer([(step, result)], "check the cpu", None)
    assert "%" in answer
    assert "running offline" not in answer
    assert "API key" not in answer


def test_the_offline_provider_never_runs_tools_from_a_prompt(settings, events):
    """The model fallback must be side-effect free.

    It used to be handed the composed final-answer prompt (which embeds the tool
    results), run intents over it and execute the tool again - so announcing a
    visitor overwrote the stored visitor with the prompt text itself.
    """
    from ai.providers import OfflineProvider

    memory = Memory(settings, events)
    engine = OfflineEngine(settings, memory, events)
    offline_config = next(p for p in settings.ai.providers if p.kind == "offline")
    provider = OfflineProvider(offline_config, engine)

    prompt = (
        "Ayush asked: the chief minister is here\n\n"
        "Tool results (these are real, use them):\n"
        "- announce_visitor: SUCCEEDED\n  Welcome, Chief Minister.\n\n"
        "Write the reply now."
    )
    text = provider.chat([{"role": "user", "content": prompt}])
    assert memory.facts() == {}
    assert "announce_visitor" not in text


def test_the_no_provider_notice_is_said_once(settings):
    memory = Memory(settings)
    planner = Planner(settings, None, memory, None, OfflineEngine(settings, memory))

    first = planner.plan("explain quantum entanglement briefly")
    assert "API key" in first.reply

    second = planner.plan("and explain it again differently")
    assert "API key" not in second.reply
    assert second.reply
