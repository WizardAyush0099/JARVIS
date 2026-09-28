"""The visitor protocol: who is announced, what JARVIS says, and who comes first.

Everything here runs with no API key and no internet, which is the point: the
moment a Chief Minister walks in must not depend on a provider being up.
"""

from __future__ import annotations

from core import visitors
from core.brain import ANSWER_SYSTEM
from core.telemetry import Telemetry

# importing the package is what registers every tool, including the new ones
from tools import base as tool_base
import tools


# --------------------------------------------------------------------------- #
# reading the announcement
# --------------------------------------------------------------------------- #
def test_it_reads_an_office_and_a_region():
    guest = visitors.parse("the chief minister of himachal pradesh is here")
    assert guest is not None
    assert guest.title == "Chief Minister"
    assert guest.place == "Himachal Pradesh"
    assert guest.label == "the Chief Minister of Himachal Pradesh"
    assert guest.vocative == "Chief Minister of Himachal Pradesh"
    assert guest.formal is True


def test_it_reads_short_forms_and_plain_guests():
    assert visitors.parse("the cm is here").title == "Chief Minister"
    assert visitors.parse("the pm has arrived").title == "Prime Minister"
    assert visitors.parse("we have a guest").noun == "guest"
    assert visitors.parse("my friend ravi is here").name == "Ravi"
    assert visitors.parse("mr. sharma is here").name == "Mr. Sharma"
    assert visitors.parse("the guest of honour is here").label == "the guest of honour"


def test_a_deputy_chief_minister_is_not_read_as_a_minister():
    guest = visitors.parse("the deputy chief minister of hp is visiting")
    assert guest is not None and guest.title == "Deputy Chief Minister"


def test_ordinary_sentences_are_not_visitors():
    for text in (
        "the guest list is here",
        "what time is it",
        "is the file here",
        "there is a lot of noise",
        "the visitor book is on the table",
        "search for chief minister news",
    ):
        assert visitors.parse(text) is None, text


def test_departures_and_questions_are_told_apart_from_arrivals():
    assert visitors.is_departure("the chief minister has left")
    assert visitors.is_departure("the visit is over")
    assert not visitors.is_departure("the chief minister is here")
    assert visitors.is_status_question("who is the guest")
    assert visitors.is_status_question("is there a guest")
    assert not visitors.is_status_question("there is a vip with me")


# --------------------------------------------------------------------------- #
# what it says
# --------------------------------------------------------------------------- #
def test_the_greeting_respects_the_guest_and_keeps_the_creator_first():
    guest = visitors.parse("the chief minister of himachal pradesh is here")
    text = visitors.greeting(guest, assistant="JARVIS", owner="Ayush", creator="Ayush")
    assert "Chief Minister of Himachal Pradesh" in text
    assert "JARVIS" in text
    assert "honour" in text
    assert "Ayush built me" in text
    assert "first priority" in text
    assert "private" in text


def test_the_greeting_follows_a_renamed_creator(settings):
    settings.owner_name = "Ayush"
    settings.creator_name = "Ayush Sharma"
    guest = visitors.parse("the governor is here")
    text = visitors.greeting(
        guest, assistant="JARVIS", owner=settings.owner_name, creator=settings.creator_name
    )
    assert "Ayush Sharma" in text


def test_the_visitor_brief_permanently_ranks_the_owner_first():
    guest = visitors.parse("the chief minister is here")
    brief = visitors.brief(guest, owner="Ayush", creator="Ayush", assistant="JARVIS")
    assert "VISITOR PROTOCOL" in brief
    assert "Chief Minister" in brief
    assert "only Ayush can release it" in brief
    assert "first priority" in brief
    assert visitors.brief(None, owner="Ayush", creator="Ayush", assistant="JARVIS") == ""


# --------------------------------------------------------------------------- #
# through the brain
# --------------------------------------------------------------------------- #
def test_announcing_a_visitor_goes_into_protocol(jarvis):
    reply = jarvis.handle("the chief minister of himachal pradesh is here")
    assert reply.error is False
    assert "Chief Minister of Himachal Pradesh" in reply.text
    assert "Ayush" in reply.text and "first priority" in reply.text

    visitor = jarvis.status()["visitor"]
    assert visitor is not None
    assert visitor["title"] == "Chief Minister"
    assert visitor["place"] == "Himachal Pradesh"
    assert jarvis.memory.facts()[visitors.FACT_KEY]


def test_the_brain_stays_in_protocol_until_stand_down(jarvis):
    jarvis.handle("the chief minister is here")
    assert "Chief Minister" in jarvis.handle("who is the guest").text

    assert "closed" in jarvis.handle("the guest has left").text
    assert jarvis.status()["visitor"] is None
    assert visitors.FACT_KEY not in jarvis.memory.facts()
    assert "no visitor" in jarvis.handle("who is visiting").text.lower()


def test_a_second_announcement_replaces_the_first(jarvis):
    jarvis.handle("the chief minister is here")
    jarvis.handle("the district collector of kullu is here")
    assert jarvis.status()["visitor"]["title"] == "District Collector"
    assert jarvis.status()["visitor"]["place"] == "Kullu"


def test_the_protocol_works_on_the_offline_path(settings, events):
    """No key, no internet: the identity answers must still be there."""
    from ai.offline import OfflineEngine
    from core.memory import Memory

    memory = Memory(settings, events)
    engine = OfflineEngine(settings, memory, events)
    answer = engine.answer("the chief minister of himachal pradesh is here")
    assert answer and "Chief Minister of Himachal Pradesh" in answer
    assert memory.facts()[visitors.FACT_KEY]


def test_both_system_prompts_learn_about_the_visitor(jarvis):
    planner_prompt = jarvis.planner.system_prompt()
    assert "VISITOR PROTOCOL" not in planner_prompt

    jarvis.handle("the chief minister is here")

    assert "VISITOR PROTOCOL" in jarvis.planner.system_prompt()
    assert jarvis.visitor_brief() in jarvis.planner.system_prompt()

    answer_prompt = ANSWER_SYSTEM.format(
        assistant="JARVIS",
        owner="Ayush",
        creator="Ayush",
        visitor=jarvis.visitor_brief(),
    )
    assert "Chief Minister" in answer_prompt
    assert "first priority" in answer_prompt


def test_the_protocol_tools_are_registered():
    names = tools.tool_names()
    assert "announce_visitor" in names
    assert "visitor_status" in names
    assert "visitor_departure" in names


def test_the_tools_are_registered_and_offline_safe():
    for name in ("announce_visitor", "visitor_status", "visitor_departure"):
        spec = tool_base.get_tool(name)
        assert spec is not None, name
        assert spec.category == "identity"
        assert spec.offline_safe is True
        assert spec.dangerous is False


def test_the_tool_reports_a_failure_it_cannot_understand(jarvis):
    result = tool_base.execute("announce_visitor", {"guest": "hello there"}, ctx=jarvis.ctx)
    assert not result.ok
    assert "visitor" in result.error


# --------------------------------------------------------------------------- #
# telemetry for the HUD gauges
# --------------------------------------------------------------------------- #
class _Sampler:
    def __init__(self, failing: bool = False) -> None:
        self.calls = 0
        self.failing = failing

    def __call__(self):
        self.calls += 1
        if self.failing:
            raise RuntimeError("no sensors here")
        return {"cpu_percent": 12.5, "memory": {"percent": 40.0}, "temperature_c": 47.0}


def test_telemetry_caches_instead_of_measuring_on_every_read():
    sampler = _Sampler()
    telemetry = Telemetry(interval=0.5, sampler=sampler)
    first = telemetry.snapshot()
    assert first["cpu_percent"] == 12.5 and first["temperature_c"] == 47.0
    assert sampler.calls == 1
    assert telemetry.snapshot()["memory"]["percent"] == 40.0
    assert sampler.calls == 1  # still the cached sample


def test_a_broken_sensor_reports_an_error_instead_of_breaking():
    telemetry = Telemetry(sampler=_Sampler(failing=True))
    snapshot = telemetry.snapshot()
    assert snapshot.get("error")
    assert "cpu_percent" not in snapshot


def test_status_carries_the_gauges(jarvis):
    machine = jarvis.status()["machine"]
    assert "sampled" in machine
    assert set(machine) >= {"cpu_percent", "memory", "disk", "temperature_c", "is_pi"}
