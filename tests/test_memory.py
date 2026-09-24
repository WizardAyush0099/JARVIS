"""Memory, legacy migration and the local-state tools."""

from __future__ import annotations

import json

from core.memory import MAX_MESSAGES, Memory
from core.storage import JsonFile


# --------------------------------------------------------------------------- #
# conversation
# --------------------------------------------------------------------------- #
def test_conversation_window_and_context(settings):
    memory = Memory(settings)
    for index in range(20):
        memory.add_user(f"question {index}")
        memory.add_assistant(f"answer {index}")

    assert len(memory.conversation(4)) == 4
    context = memory.context(turns=3)
    assert len(context) == 6
    assert context[-1]["content"] == "answer 19"
    assert all(item["role"] in {"user", "assistant"} for item in context)


def test_context_respects_char_budget(settings):
    memory = Memory(settings)
    memory.add_user("x" * 5000)
    memory.add_assistant("y" * 5000)
    memory.add_user("small")
    context = memory.context(turns=10, char_budget=600)
    assert len(context) == 1
    assert context[0]["content"] == "small"


def test_conversation_is_bounded(settings):
    memory = Memory(settings)
    for index in range(MAX_MESSAGES + 40):
        memory.add_user(f"m{index}")
    assert len(memory.conversation()) == MAX_MESSAGES


# --------------------------------------------------------------------------- #
# facts
# --------------------------------------------------------------------------- #
def test_facts_round_trip(settings):
    memory = Memory(settings)
    memory.remember("project", "Athena")
    assert memory.facts() == {"project": "Athena"}
    assert memory.search_facts("project")[0]["value"] == "Athena"
    assert memory.forget("project") is True
    assert memory.facts() == {}


def test_facts_survive_clear_conversation(settings):
    memory = Memory(settings)
    memory.remember("project", "Athena")
    memory.add_user("hello")
    memory.clear_conversation()
    assert memory.conversation() == []
    assert memory.facts()["project"] == "Athena"


def test_fact_keys_are_normalised(settings):
    memory = Memory(settings)
    memory.remember("  My Project  ", "Athena")
    assert "my project" in memory.facts()


def test_facts_persist_across_instances(settings):
    first = Memory(settings)
    first.remember("pet", "a dog called Bolt")
    second = Memory(settings)
    assert second.facts()["pet"] == "a dog called Bolt"


# --------------------------------------------------------------------------- #
# legacy ChatLog migration
# --------------------------------------------------------------------------- #
def test_migrates_old_pair_list_chatlog(settings):
    settings.paths.chat_log_file.write_text(
        json.dumps({"messages": [["user", "hi"], ["assistant", "hello"], ["bot", "beep"]]}),
        encoding="utf-8",
    )
    memory = Memory(settings)
    contents = [message.content for message in memory.conversation()]
    assert contents[:2] == ["hi", "hello"]
    assert "beep" in contents  # "bot" is mapped to assistant


def test_migrates_old_object_chatlog(settings):
    settings.paths.chat_log_file.write_text(
        json.dumps([{"role": "user", "content": "one"}, {"role": "model", "content": "two"}]),
        encoding="utf-8",
    )
    memory = Memory(settings)
    assert [m.content for m in memory.conversation()] == ["one", "two"]


def test_chatlog_is_written_back_in_the_old_shape(settings):
    memory = Memory(settings)
    memory.add_user("hi")
    memory.add_assistant("hello")
    payload = json.loads(settings.paths.chat_log_file.read_text(encoding="utf-8"))
    assert payload["messages"][0][0] == "user"
    assert payload["messages"][1][0] == "assistant"


def test_corrupt_store_is_quarantined_not_fatal(settings):
    settings.paths.memory_file.write_text("{not json at all", encoding="utf-8")
    memory = Memory(settings)  # must not raise
    assert memory.conversation() == []
    quarantined = list(settings.paths.data_dir.glob("memory.corrupt-*"))
    assert quarantined, "the unreadable file should be moved aside"


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
def test_json_file_is_atomic(tmp_path):
    store = JsonFile(tmp_path / "thing.json", dict)
    assert store.load() == {}
    store.save({"a": 1})
    assert store.load() == {"a": 1}
    assert not list(tmp_path.glob("*.tmp"))


def test_json_file_update(tmp_path):
    store = JsonFile(tmp_path / "counter.json", dict)
    store.update(lambda data: data.update({"n": data.get("n", 0) + 1}))
    store.update(lambda data: data.update({"n": data.get("n", 0) + 1}))
    assert store.load()["n"] == 2
