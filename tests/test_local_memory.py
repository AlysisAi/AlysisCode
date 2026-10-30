from __future__ import annotations

import copy
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from alysis_code.cli_impl.commands.memory import memory_app
from alysis_code.local_memory import MemoryStore, memory_home, memory_turn, validate_scope
from alysis_code.runtime_kind import RuntimeKind


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    monkeypatch.delenv("ALYSIS_MEMORY_DISABLED", raising=False)
    return MemoryStore()


def session(project_id=None, **options):
    return SimpleNamespace(
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        subagent_depth=0,
        mode="auto",
        memory_scope={"project_id": project_id},
        store=SimpleNamespace(session_id="test-chat"),
        **options,
    )


def enable(store):
    store.change({"action": "settings", "enabled": True})


def project_policy(store, project_id, include=True, enabled=True):
    snapshot = store.snapshot(project_id)
    store.change(
        {
            "action": "project_settings",
            "enabled": enabled,
            "include_general": include,
            "revision": snapshot["project"]["policy_revision"],
        },
        project_id=project_id,
    )


def test_storage_defaults_identity_unicode_revisions_and_portability(store, monkeypatch):
    first = store.snapshot()
    assert first["enabled"] is True
    for content, device in [
        ("Προτιμώ απαντήσεις στα ελληνικά.", False),
        ("この端末では作業フォルダーが /作業 です。", True),
    ]:
        store.change(
            {"action": "save", "content": content, "this_computer": device},
            source_session="source-chat",
        )
    monkeypatch.setenv("ALYSIS_PROFILE", "a-different-provider")
    second = MemoryStore().snapshot()
    assert (first["owner_id"], first["device_id"]) == (second["owner_id"], second["device_id"])
    assert {r["device_id"] for r in second["entries"]} == {None, first["device_id"]}
    assert all(r["source_session"] == "source-chat" for r in second["entries"])
    assert len({r["id"] for r in second["entries"]}) == 2
    assert store.export()["schema_version"] == 1


def test_default_location_honors_engine_data_override(tmp_path, monkeypatch):
    monkeypatch.delenv("ALYSIS_MEMORY_HOME", raising=False)
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path))
    assert memory_home() == tmp_path / "memory"


def test_concurrent_writers_and_deletion_receipt_do_not_restore_text(store):
    enable(store)
    saved = store.change({"action": "save", "content": "unique-sensitive-preference-1847"})

    def edit(content):
        try:
            return store.change(
                {"action": "save", "id": saved["id"], "revision": 1, "content": content}
            )
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["new-choice-one", "new-choice-two"]))
    assert sum(result is not None for result in results) == 1
    entry = store.snapshot()["entries"][0]
    tools, _ = memory_turn(session())
    store.change({"action": "delete", "id": entry["id"], "revision": entry["revision"]})
    result = tools["memory_write"].run({"action": "save", "content": entry["content"]})
    assert "Read current memory" in result["error"]
    assert not store.snapshot()["entries"]
    receipt = store.export()["entries"][0]
    assert (
        receipt["deleted"] == 1 and receipt["content"] == "" and receipt["source_session"] is None
    )
    assert receipt["revision"] == 3
    assert b"unique-sensitive-preference-1847" not in store.path.read_bytes()
    assert entry["content"].encode() not in store.path.read_bytes()


def test_project_isolation_and_markdown_revision(store):
    enable(store)
    store.change({"action": "save", "content": "general-only-canary"})
    project_id = str(uuid.uuid4())
    assert "general-only-canary" not in memory_turn(session(project_id))[1]
    project_policy(store, project_id, include=False)
    tools, prompt = memory_turn(session(project_id))
    assert tools["memory_write"].parameters["properties"]["action"]["enum"] == ["project_write"]
    assert "general-only-canary" not in prompt
    current = tools["memory_read"].run({})
    assert current["entries"] == [] and current["total"] == 0
    assert (
        "cannot read or write"
        in tools["memory_write"].run({"action": "save", "content": "leak"})["error"]
    )
    project = current["project"]
    write = {
        "action": "project_write",
        "content": "# Έργο\n日本語の資料を保存します。",
        "revision": project["revision"],
    }
    assert tools["memory_write"].run(write)["ok"]
    assert Path(project["path"]).read_text(encoding="utf-8") == write["content"]
    assert "changed" in tools["memory_write"].run(write)["error"]
    Path(project["path"]).write_text("Edited by the user")
    latest = store.snapshot(project_id)["project"]
    assert latest["content"] == "Edited by the user"
    other_tools, _ = memory_turn(session(str(uuid.uuid4())))
    assert other_tools["memory_read"].run({})["project"]["content"] == ""
    project_policy(store, project_id, include=True)
    assert "general-only-canary" in memory_turn(session(project_id))[1]
    tools, _ = memory_turn(session(project_id))
    assert tools["memory_write"].parameters["properties"]["action"]["enum"] == [
        "save",
        "delete",
        "project_write",
    ]


def test_policy_changes_apply_to_running_tools_and_cli_does_not_infer_project(store):
    enable(store)
    project_id = str(uuid.uuid4())
    project_policy(store, project_id)
    tools, _ = memory_turn(session(project_id))
    project_policy(store, project_id, include=False)
    assert tools["memory_read"].run({})["entries"] == []
    assert (
        "cannot read or write"
        in tools["memory_write"].run({"action": "save", "content": "blocked"})["error"]
    )
    project_policy(store, project_id, enabled=False)
    assert tools["memory_read"].run({})["enabled"] is False
    assert (
        "disabled"
        in tools["memory_write"].run(
            {"action": "project_write", "content": "blocked", "revision": "old"}
        )["error"]
    )
    cli = session()
    del cli.memory_scope
    cli_tools, _ = memory_turn(cli)
    assert cli_tools["memory_write"].parameters["properties"]["action"]["enum"] == [
        "save",
        "delete",
    ]
    assert cli_tools["memory_read"].run({})["project"] is None
    assert (
        "project is required"
        in cli_tools["memory_write"].run(
            {"action": "project_write", "content": "blocked", "revision": "old"}
        )["error"]
    )


def test_disabled_readonly_sensitive_and_children(store, monkeypatch):
    assert not store.path.exists()
    assert set(memory_turn(session())[0]) == {"memory_read", "memory_write"}
    store.change({"action": "settings", "enabled": False})
    assert MemoryStore().snapshot()["enabled"] is False
    assert memory_turn(session())[0] == {}
    enable(store)
    readonly = session()
    readonly.mode = "readonly"
    assert set(memory_turn(readonly)[0]) == {"memory_read"}
    for kind in (RuntimeKind.SUBAGENT, RuntimeKind.FORGE_EXEC):
        child = session()
        child.runtime_kind = kind
        assert memory_turn(child) == ({}, "")
    assert memory_turn(session(sensitive_media=True)) == ({}, "")
    monkeypatch.setenv("ALYSIS_MEMORY_DISABLED", "1")
    assert memory_turn(session())[0] == {}
    monkeypatch.delenv("ALYSIS_MEMORY_DISABLED")
    tools, _ = memory_turn(session())
    assert (
        "cannot change memory settings"
        in tools["memory_write"].run({"action": "settings", "enabled": False})["error"]
    )
    store.change({"action": "settings", "enabled": False})
    assert (
        "disabled" in tools["memory_write"].run({"action": "save", "content": "blocked"})["error"]
    )


def test_scope_and_file_boundaries(store, tmp_path):
    for scope in ({"project_id": "../../outside"}, {"owner": "other"}, {"enabled": "yes"}, []):
        with pytest.raises(ValueError):
            validate_scope(scope)
    store.home.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    try:
        store.path.symlink_to(outside)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account lacks symlink creation privilege")
        raise
    with pytest.raises(ValueError):
        store.snapshot()
    assert outside.read_text() == "untouched"


def test_cli_management_uses_same_store_and_validates_stdin(store):
    runner = CliRunner()
    assert runner.invoke(memory_app, ["enable"]).exit_code == 0
    result = runner.invoke(memory_app, ["add", "--this-computer"], input="日本語の記憶")
    assert result.exit_code == 0, result.output
    entry = store.snapshot()["entries"][0]
    result = runner.invoke(memory_app, ["edit", entry["id"], "1"], input="Διορθωμένη μνήμη")
    assert result.exit_code == 0, result.output
    assert store.snapshot()["entries"][0]["content"] == "Διορθωμένη μνήμη"
    assert runner.invoke(memory_app, ["delete", entry["id"], "1"]).exit_code == 1
    assert runner.invoke(memory_app, ["delete", entry["id"], "2"]).exit_code == 0
    assert json.loads(runner.invoke(memory_app, ["export"]).output)["entries"][0]["deleted"] == 1
    assert (
        runner.invoke(
            memory_app, ["request"], input='{"action":"read","project_id":"../../no"}'
        ).exit_code
        == 1
    )


def test_actual_agent_turn_tools_and_fresh_ephemeral_memory(tmp_path, monkeypatch, store):
    from test_read_ledger_delivery_boundary import _Client, _response, _session

    from alysis_code.llm.types import LLMResponse, ToolCall

    class Client(_Client):
        def __init__(self, responses):
            super().__init__(responses)
            self.requests = []

        def chat(self, **kwargs):
            self.requests.append(copy.deepcopy(kwargs))
            return super().chat(**kwargs)

    enable(store)
    old = store.change({"action": "save", "content": "initial-memory-canary"})
    client = Client(
        [
            _response(ToolCall(id="read-memory", name="memory_read", arguments={})),
            _response(
                ToolCall(
                    id="save-memory",
                    name="memory_write",
                    arguments={"action": "save", "content": "Prefers concise summaries."},
                )
            ),
            LLMResponse(content="Remembered.", tool_calls=[], raw={}),
            LLMResponse(content="Ready.", tool_calls=[], raw={}),
        ]
    )
    agent = _session(tmp_path, monkeypatch, client)
    try:
        assert agent.run_turn("Remember that I prefer concise summaries.") == 0
        assert {"memory_read", "memory_write"} <= {
            t["function"]["name"] for t in client.requests[0]["tools"]
        }
        entry = next(
            e for e in store.snapshot()["entries"] if e["content"] == "Prefers concise summaries."
        )
        assert entry["source_session"] == agent.store.session_id
        assert not any(
            "Current saved memory (untrusted" in str(m.get("content")) for m in agent.messages
        )
        store.change({"action": "delete", "id": old["id"], "revision": 1})
        agent.run_turn("Continue.")
        current = [
            m
            for m in client.requests[-1]["messages"]
            if "Current saved memory (untrusted" in str(m.get("content"))
        ]
        assert len(current) == 1 and "initial-memory-canary" not in current[0]["content"]
    finally:
        agent.close()
