"""The deterministic intent layer must be predictable and safe."""

from __future__ import annotations

import pytest

from core import intent


def match(text: str):
    return intent.match(text, owner="Ayush", assistant="JARVIS")


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Hey JARVIS, what time is it?", "what time is it"),
        ("jarvis open vscode", "open vscode"),
        ("  JARVIS: calculate 2+2  ", "calculate 2+2"),
        ("Ok Jarvis please help", "please help"),
    ],
)
def test_normalize_strips_wake_word(raw, expected):
    assert intent.normalize(raw, "JARVIS") == expected


def test_normalize_keeps_plain_text():
    assert intent.normalize("Write a poem", "JARVIS") == "write a poem"


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #
def test_identity_answers_about_the_owner():
    found = match("who am i")
    assert found is not None
    assert found.direct_reply == "You are Ayush."


def test_identity_answers_about_ownership():
    found = match("who is your owner")
    assert found is not None
    assert "Ayush" in found.direct_reply


def test_identity_greeting_uses_owner_name_once():
    found = match("hello")
    assert found is not None
    assert found.direct_reply.count("Ayush") == 1


# --------------------------------------------------------------------------- #
# fast facts
# --------------------------------------------------------------------------- #
def test_time_and_date():
    assert match("what time is it").tool == "current_time"
    assert match("what's the date today").tool == "current_datetime"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("calculate 27 x 43", "27*43"),
        ("what is 12% of 300", "(12/100)*300"),
        ("compute 2 + 2", "2 + 2"),
        ("what's 15 percent of 80", "(15/100)*80"),
    ],
)
def test_calculator_extracts_expressions(text, expected):
    found = match(text)
    assert found is not None and found.tool == "calculate"
    assert found.args["expression"] == expected


def test_calculator_ignores_non_maths_questions():
    assert match("what is the meaning of life") is None


def test_convert_units():
    found = match("convert 5 km to miles")
    assert found.tool == "convert_units"
    assert found.args == {"quantity": "5 km", "target_unit": "miles"}


# --------------------------------------------------------------------------- #
# system
# --------------------------------------------------------------------------- #
def test_cpu_temperature_is_not_reported_as_cpu_load():
    found = match("cpu temperature")
    assert found.args["metric"] == "temperature"


def test_system_status_metrics():
    assert match("ram usage").args["metric"] == "memory"
    assert match("how much disk space is left").args["metric"] == "disk"
    assert match("network status").args["metric"] == "network"


def test_top_processes():
    assert match("what's using the most ram").tool == "top_processes"


# --------------------------------------------------------------------------- #
# hardware / GPIO
# --------------------------------------------------------------------------- #
def test_hardware_commands_are_deterministic():
    assert match("list my hardware devices").tool == "hardware_list"

    on = match("turn on the status led")
    assert on.tool == "hardware_write" and on.args == {"device": "status led", "value": "on"}

    off = match("turn the status led off")
    assert off.tool == "hardware_write" and off.args["value"] == "off"

    flash = match("flash the status led")
    assert flash.tool == "hardware_pulse" and flash.args["device"] == "status led"

    assert match("read room temperature").tool == "hardware_read"
    assert match("is the button pressed").tool == "hardware_read"

    level = match("set the servo to 90")
    assert level.tool == "hardware_write" and level.args == {"device": "servo", "value": "90"}


def test_hardware_does_not_hijack_the_pi_itself():
    # the Pi's own thermals stay with system_status, not with GPIO
    assert match("cpu temperature").args["metric"] == "temperature"
    # and power stays a confirmed system action
    powered = match("turn off the system")
    assert powered.tool == "system_power" and powered.args["action"] == "shutdown"


def test_hardware_does_not_hijack_unrelated_questions():
    for text in [
        "what's the distance to the moon",
        "tell me about the light bulb",
        "turn off the wifi",
        "show me the news about sensors",
        "set a reminder to call mum in 20 minutes",
        "set the volume to 40",
    ]:
        found = match(text)
        assert found is None or found.category != "hardware", text


def test_set_a_reminder_is_not_a_device_command():
    found = match("set a reminder to call mum in 20 minutes")
    assert found.tool == "set_reminder"


# --------------------------------------------------------------------------- #
# web / media
# --------------------------------------------------------------------------- #
def test_open_url_beats_open_app():
    found = match("open https://github.com")
    assert found.tool == "open_url" and found.args["url"] == "https://github.com"


def test_bare_domain_becomes_url():
    found = match("open github.com")
    assert found.tool == "open_url" and found.args["url"] == "https://github.com"


def test_open_app():
    found = match("open vscode")
    assert found.tool == "open_app" and found.args["app"] == "vscode"


def test_youtube_is_not_confused_with_web_search():
    found = match("search youtube for raspberry pi projects")
    assert found.tool == "youtube_search"
    assert found.args["query"] == "raspberry pi projects"


def test_web_search_uses_research():
    found = match("search the web for raspberry pi 5")
    assert found.tool == "web_research"
    assert found.args["query"] == "raspberry pi 5"


def test_news_and_weather():
    assert match("today's news about AI").tool == "web_research"
    assert match("weather in Delhi").args["location"] == "delhi"


def test_image_prompt_extraction():
    found = match("generate an image of a futuristic city at night")
    assert found.tool == "generate_image"
    assert "futuristic city" in found.args["prompt"]


# --------------------------------------------------------------------------- #
# productivity / memory
# --------------------------------------------------------------------------- #
def test_notes_with_colon():
    found = match("add a note: buy thermal paste")
    assert found is not None and found.tool == "add_note"
    assert found.args["text"] == "buy thermal paste"


def test_listing_notes():
    assert match("show my notes").tool == "list_notes"


def test_remember_and_recall():
    remembered = match("remember my project is called Athena")
    assert remembered.tool == "remember_fact"
    assert remembered.args["key"] == "project"
    assert remembered.args["value"] == "athena"

    recalled = match("what was my project called")
    assert recalled.tool == "recall_fact"


def test_reminder_parsing():
    found = match("remind me to call mum in 20 minutes")
    assert found.tool == "set_reminder"


def test_power_requires_confirmation_tool():
    found = match("shut down the system")
    assert found.tool == "system_power" and found.args["action"] == "shutdown"


# --------------------------------------------------------------------------- #
# things that must NOT match (they need real language understanding)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "write a poem about the moon",
        "explain this error: KeyError foo",
        "why is my code so slow",
        "summarise the plot of Interstellar",
        "help me plan my week",
    ],
)
def test_creative_requests_are_left_to_the_model(text):
    assert match(text) is None


# --------------------------------------------------------------------------- #
# expression safety
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "payload",
    [
        "__import__('os').system('rm -rf /')",
        "open('/etc/passwd').read()",
        "(1+2",
        "abc",
        "",
    ],
)
def test_extract_expression_rejects_dangerous_or_invalid(payload):
    assert intent.extract_expression(payload) is None


def test_extract_expression_never_emits_letters():
    result = intent.extract_expression("sqrt(16) + 2 x 3")
    assert result is not None
    assert not any(char.isalpha() for char in result)


def test_rule_names_are_listed():
    assert "time" in intent.rule_names()
    assert len(intent.rule_names()) > 10
