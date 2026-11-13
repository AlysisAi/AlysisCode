"""Exact-source caching and retrieval policy. Classifier responses are fixtures."""

import json
import threading

import pytest
from test_bots_memory_changes import case as shared_case
from test_bots_runtime import ScriptedClient, response

from alysis_code.bots.memory_applicability import MemoryApplicability, source_hash
from alysis_code.bots.memory_retrieval import selected
from alysis_code.bots.models import BotInput, MemoryInput
from alysis_code.bots.runtime import RuntimeLimits
from alysis_code.bots.store import Conflict, NotFound
from alysis_code.cancellation import EventCancellationToken

case = shared_case


def test_cache_uses_exact_current_approved_source_and_survives_restart(case):
    store, owner, bot, task = case
    memory = store.save_memory(
        owner, bot["id"], MemoryInput(title="応答", content="短い段落で答える")
    )
    assert (
        store.cache_memory_applicability(
            owner, bot["id"], task["id"], [memory], {memory["id"]: "general"}
        )
        == 1
    )
    current = store.list_memories(owner, bot["id"])[0]
    assert current["context_scope"] == "general" and current["content"] == memory["content"]
    store.save_memory(
        owner, bot["id"], MemoryInput(title="New", content="Topic-specific"), memory_id=memory["id"]
    )
    assert store.list_memories(owner, bot["id"])[0]["context_scope"] is None
    assert (
        store.cache_memory_applicability(
            owner, bot["id"], task["id"], [memory], {memory["id"]: "general"}
        )
        == 0
    )
    store.delete_item(owner, bot["id"], memory["id"], kind="memories")
    assert (
        store.cache_memory_applicability(
            owner, bot["id"], task["id"], [memory], {memory["id"]: "general"}
        )
        == 0
    )
    with store.connect() as db:
        assert not db.execute(
            "SELECT 1 FROM memory_applicability WHERE memory_id=?", (memory["id"],)
        ).fetchone()


def test_pending_foreign_and_cancelled_sources_cannot_be_indexed(case):
    store, owner, bot, task = case
    pending = store.save_memory(
        owner, bot["id"], MemoryInput(title="Proposed", content="未承認"), source_task_id=task["id"]
    )
    assert (
        store.cache_memory_applicability(
            owner, bot["id"], task["id"], [pending], {pending["id"]: "general"}
        )
        == 0
    )
    other = store.save_bot(owner, BotInput(name="Peer", purpose="Isolation"))
    foreign = store.save_memory(
        owner, other["id"], MemoryInput(title="Private", content="Other bot")
    )
    assert (
        store.cache_memory_applicability(
            owner, bot["id"], task["id"], [foreign], {foreign["id"]: "general"}
        )
        == 0
    )
    with pytest.raises(NotFound):
        store.cache_memory_applicability("foreign-owner", bot["id"], task["id"], [], {})
    store.finish(owner, task["id"], "cancelled")
    with pytest.raises(Conflict):
        store.cache_memory_applicability(owner, bot["id"], task["id"], [], {})


def test_general_records_survive_unrelated_topical_selection_without_rewriting():
    records = [
        dict(
            id=str(i),
            title="Project",
            content="project reference " * 30,
            updated_at=i,
            context_scope="contextual",
        )
        for i in range(100)
    ]
    records[0].update(title="応答", content="短い段落で答える", context_scope="general")
    before = json.dumps(records)
    result = selected(records, query="project", budget=800)
    assert result["items"][0]["id"] == "0"
    assert result["items"][0]["content"] == records[0]["content"]
    assert not result["index_complete"]
    assert json.dumps(records) == before
    assert source_hash(records[0]) != source_hash({**records[0], "content": "Changed"})


def test_classifier_caches_only_valid_complete_identity_sets_and_accounts_calls(case):
    store, owner, bot, task = case
    records = [
        store.save_memory(
            owner, bot["id"], MemoryInput(title=f"Record {i}", content="Source text " * 100)
        )
        for i in range(8)
    ]

    class Classifier(ScriptedClient):
        def chat(self, **kwargs):
            self.requests.append(kwargs["messages"])
            records = json.loads(kwargs["messages"][-1]["content"])
            return response(
                json.dumps({"items": [{"id": r["id"], "scope": "general"} for r in records]})
            )

    client = Classifier([])
    index = MemoryApplicability(store, owner)
    index.prepare(task, client, RuntimeLimits(), EventCancellationToken(threading.Event()))
    count = len(client.requests)
    assert count >= 2
    assert all(r["context_scope"] == "general" for r in store.list_memories(owner, bot["id"]))
    index.prepare(task, client, RuntimeLimits(), EventCancellationToken(threading.Event()))
    assert len(client.requests) == count
    assert store.usage(owner)["calls"] == count
    assert store.usage(owner)["unconfirmed_calls"] == 0
    edited = records[0]
    store.save_memory(
        owner, bot["id"], MemoryInput(title="Changed", content="New source"), memory_id=edited["id"]
    )
    invalid = ScriptedClient([response('{"items":[]}')])
    index.prepare(task, invalid, RuntimeLimits(), EventCancellationToken(threading.Event()))
    assert store.list_memories(owner, bot["id"])[0]["context_scope"] is None
    assert store.usage(owner)["calls"] == count + 1
