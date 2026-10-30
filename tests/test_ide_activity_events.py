from __future__ import annotations

import json

import pytest

from alysis_code.ide import activity_events
from alysis_code.ide.activity_events import (
    activity_capabilities,
    diff_stats,
    patch_activity,
    semantic_tool_name,
    tool_activity,
)
from alysis_code.ide.event_stream import EventContext, ProtocolEventSurface
from alysis_code.surface.types import PatchEvent, ToolEndEvent, ToolStartEvent


def test_activity_capabilities_publish_semantic_contract() -> None:
    capabilities = activity_capabilities()

    assert capabilities["event_type"] == "activity_update"
    assert "edit" in capabilities["kinds"]
    assert "succeeded" in capabilities["statuses"]
    assert capabilities["patch_payload"] is True
    assert capabilities["legacy_tool_events_preserved"] is True


@pytest.mark.parametrize(
    "suffix,title",
    [
        ("list", "List command windows"),
        ("open", "Open command window"),
        ("read", "Read command window"),
        ("write", "Send terminal input"),
        ("close", "Close command window"),
    ],
)
def test_registered_terminal_actions_have_exact_semantics_without_exposing_input(suffix, title):
    event = tool_activity(
        call_id="terminal",
        name=f"desktop_terminal_{suffix}",
        status="blocked",
        arguments={"terminal_id": "owned-terminal", "text": "private command input"},
    ).to_payload()
    assert event["display_title"] == title
    assert event["kind"] == "command"
    assert event["status"] == "blocked"
    assert "private command input" not in json.dumps(event)


def test_internal_tool_names_become_stable_friendly_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOOL_API_KEY", "activity-secret-value")

    activity = tool_activity(
        call_id="call-1",
        name="rs.read",
        arguments={"path": "src/main.py", "api_key": "activity-secret-value"},
        status="running",
        metadata={"source": "runtime", "internal_tool_name": "rs.read"},
    ).to_payload()

    rendered = json.dumps(activity, sort_keys=True)
    assert semantic_tool_name("rs.read") == "fs_read"
    assert activity["kind"] == "read"
    assert activity["operation"] == "read_file"
    assert activity["display_title"] == "Read file"
    assert activity["target"] == "src/main.py"
    assert "rs.read" not in rendered
    assert "activity-secret-value" not in rendered
    assert "internal_tool_name" not in activity["metadata"]


def test_external_and_unknown_tool_names_never_leak_private_names() -> None:
    external = tool_activity(call_id="1", name="mcp__server__private_search").to_payload()
    internal = tool_activity(call_id="2", name="internal.runtime.helper").to_payload()

    assert external["display_title"] == "Use connected tool"
    assert external["operation"] == "use_external_tool"
    assert "mcp__" not in json.dumps(external)
    assert internal["display_title"] == "Use developer tool"
    assert "internal.runtime.helper" not in json.dumps(internal)


def test_document_activity_keeps_each_declared_action_through_out_of_order_completion() -> None:
    emitted: list[dict] = []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="documents"),
        emit=emitted.append,
        semantic_activity_events=True,
    )
    surface.emit_tool_call_started(
        "create",
        "document_operation",
        "{}",
        arguments={
            "action": "create",
            "format": "xlsx",
            "output": "deliveries/Έσοδα.xlsx",
            "spec": "private-working-spec.json",
        },
    )
    surface.emit_tool_call_started(
        "inspect",
        "document_operation",
        "{}",
        arguments={
            "action": "inspect",
            "source": "source.xlsx",
        },
    )
    surface.emit_tool_call_completed("inspect", True, "{}")
    surface.emit_tool_call_completed("create", False, "{}", status="blocked")
    rows = [e["payload"] for e in emitted if e["type"] == "activity_update"]
    assert [(r["operation"], r["kind"], r["target"], r["status"]) for r in rows] == [
        ("create_document", "edit", "deliveries/Έσοδα.xlsx", "running"),
        ("inspect_document", "read", "source.xlsx", "running"),
        ("inspect_document", "read", "source.xlsx", "succeeded"),
        ("create_document", "edit", "deliveries/Έσοδα.xlsx", "blocked"),
    ]
    assert rows[0]["display_title"] == rows[3]["display_title"] == "Create document"
    assert "private-working-spec" not in json.dumps(rows)
    assert not surface._activity_tool_names


def test_patch_activity_preserves_safe_diff_and_exact_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATCH_SECRET", "patch-secret-value")
    event = PatchEvent(
        files=["a.py", "b.py"],
        diff=(
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "-old\n"
            "+new\n"
            "+token=patch-secret-value\n"
        ),
        summary="Updated token=patch-secret-value",
    )

    payload = patch_activity(event).to_payload()

    assert payload["operation"] == "prepare_workspace_patch"
    assert payload["display_title"] == "Prepared workspace changes"
    assert payload["files"] == ["a.py", "b.py"]
    assert payload["diff"] == {"files": 2, "additions": 2, "deletions": 1}
    assert "+new" in payload["patch"]
    assert "patch-secret-value" not in json.dumps(payload)
    assert payload["activity_id"].startswith("patch-")


def test_patch_size_is_bounded_without_losing_diff_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(activity_events, "MAX_PATCH_BYTES", 128)
    payload = patch_activity(PatchEvent(files=["a.py"], diff="+x\n" * 200)).to_payload()

    assert len(payload["patch"].encode("utf-8")) <= 128
    assert payload["patch_truncated"] is True
    assert payload["metadata"]["truncated"] is True
    assert payload["diff"]["files"] == 1


def test_diff_stats_do_not_count_file_headers() -> None:
    stats = diff_stats("--- a/file\n+++ b/file\n-old\n+new\n", file_count=1)
    assert stats.to_dict() == {"files": 1, "additions": 1, "deletions": 1}


def test_protocol_surface_emits_patch_as_structured_activity() -> None:
    emitted: list[dict] = []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="session-1"),
        emit=emitted.append,
    )

    surface.on_patch_generated(
        PatchEvent(files=["a.py"], diff="--- a/a.py\n+++ b/a.py\n+new\n", summary="Add line")
    )

    assert [event["type"] for event in emitted] == ["activity_update"]
    payload = emitted[0]["payload"]
    assert payload["display_title"] == "Prepared workspace changes"
    assert payload["patch"].endswith("+new\n")
    assert payload["diff"] == {"files": 1, "additions": 1, "deletions": 0}


def test_protocol_surface_semantic_tool_events_are_opt_in_and_legacy_names_are_safe() -> None:
    emitted: list[dict] = []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="session-1"),
        emit=emitted.append,
        semantic_activity_events=True,
    )

    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="call-1",
            name="rs.read",
            args={"path": "README.md"},
            step=1,
        )
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="call-1",
            name="rs.read",
            status="done",
            elapsed_ms=9,
            meta={"output_bytes": 42, "raw_internal_id": "rs.read"},
        )
    )

    assert [event["type"] for event in emitted] == [
        "tool_call_started",
        "activity_update",
        "tool_call_completed",
        "activity_update",
    ]
    assert emitted[0]["payload"]["name"] == "fs_read"
    started = emitted[1]["payload"]
    completed = emitted[3]["payload"]
    assert started["display_title"] == "Read file"
    assert started["target"] == "README.md"
    assert completed["status"] == "succeeded"
    assert completed["duration_ms"] == 9
    assert completed["metadata"] == {"output_bytes": 42, "worker_id": None}
    assert "rs.read" not in json.dumps(emitted)


def test_protocol_surface_emits_tool_activity_on_the_canonical_emit_path() -> None:
    # The agent core drives this surface through emit_tool_call_started/completed only
    # (canonical_message_tool_events) and never calls on_tool_start/on_tool_end. The semantic
    # activity therefore has to come from the canonical path, or the IDE advertises
    # activity_update while verify_run / shell_run reach it as legacy events alone.
    emitted: list[dict] = []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="session-1"),
        emit=emitted.append,
        semantic_activity_events=True,
    )

    surface.emit_tool_call_started(
        "call-verify",
        "verify_run",
        json.dumps({"commands": ["python -m pytest -q"]}, sort_keys=True),
    )
    surface.emit_tool_call_completed("call-verify", False, json.dumps({"exit_code": 1}))

    assert [event["type"] for event in emitted] == [
        "tool_call_started",
        "activity_update",
        "tool_call_completed",
        "activity_update",
    ]
    started = emitted[1]["payload"]
    completed = emitted[3]["payload"]
    assert started["activity_id"] == "call-verify"
    assert started["status"] == "running"
    assert started["kind"] == completed["kind"]
    assert completed["activity_id"] == "call-verify"
    assert completed["status"] == "failed"
    assert completed["metadata"] == {"worker_id": None}


def test_protocol_surface_canonical_completion_without_a_start_adds_no_activity() -> None:
    emitted: list[dict] = []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="session-1"),
        emit=emitted.append,
        semantic_activity_events=True,
    )

    surface.emit_tool_call_completed("call-orphan", True, "{}")

    assert [event["type"] for event in emitted] == ["tool_call_completed"]


def test_protocol_surface_legacy_delivery_does_not_double_emit_activity() -> None:
    emitted: list[dict] = []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="session-1"),
        emit=emitted.append,
        semantic_activity_events=True,
    )

    surface.on_tool_start(
        ToolStartEvent(tool_call_id="call-2", name="shell_run", args={"cmd": "pytest -q"}, step=2)
    )
    surface.on_tool_end(
        ToolEndEvent(tool_call_id="call-2", name="shell_run", status="done", elapsed_ms=5, meta={})
    )

    assert [event["type"] for event in emitted].count("activity_update") == 2
    assert emitted[1]["payload"]["metadata"] == {"step": 2, "worker_id": None}
