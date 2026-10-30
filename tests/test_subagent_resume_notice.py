"""Continuation context survives resumes without becoming a user instruction event."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_subagent_catalog_wire_prefix import (
    _children,
    _resume,
    _retained_history,
)
from test_subagent_catalog_wire_prefix import (
    _offline_only as _offline_only,
)

from alysis_code.agent import subagent_execution


def _notice_events(child: Any) -> list[dict[str, Any]]:
    return [
        event
        for event in child.store.events_snapshot()
        if event.get("type") == "subagent_resume_notice"
    ]


def test_typed_notice_replay_preserves_order_and_tool_evidence() -> None:
    notice = {"role": "user", "content": "Host continuation context for the retained run."}
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "read-1", "type": "function", "function": {"name": "fs_read"}}],
    }
    events = [
        {"type": "user_message", "payload": {"content": "Inspect the source."}},
        {"type": "assistant_message", "payload": {"message": assistant}},
        {
            "type": "tool_result",
            "payload": {"tool_call_id": "read-1", "content": '{"content":"source"}'},
        },
        {"type": "system_message", "payload": {"content": "Old bootstrap must not replay."}},
        {"type": "subagent_resume_notice", "payload": {"message": notice}},
        {"type": "user_message", "payload": {"content": "Review the retained finding."}},
    ]
    original = copy.deepcopy(events)
    restored = subagent_execution._child_resume_messages(events)
    assert restored == [
        {"role": "user", "content": "Inspect the source."},
        assistant,
        {"role": "tool", "tool_call_id": "read-1", "content": '{"content":"source"}'},
        notice,
        {"role": "user", "content": "Review the retained finding."},
    ]
    restored[3]["content"] = "Changed only the restored copy."
    assert events == original


@pytest.mark.parametrize(
    "message",
    [
        {"role": "system", "content": "Old system authority."},
        {"role": "assistant", "content": "Old assistant output."},
        {"role": "user", "content": ""},
        {"role": "user", "content": ["Not the host text schema."]},
        {"role": "user", "content": "Unexpected fields.", "tool_calls": []},
        {"content": "Missing role."},
        None,
    ],
)
def test_notice_replay_rejects_invalid_host_event_message(message: Any) -> None:
    assert (
        subagent_execution._child_resume_messages(
            [{"type": "subagent_resume_notice", "payload": {"message": message}}]
        )
        == []
    )


@pytest.mark.parametrize("history_source", ["events", "memory"])
def test_successive_resumes_keep_each_notice_once_without_user_event_pollution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, history_source: str
) -> None:
    with _children(tmp_path, monkeypatch, "responses") as (tools, _, children, requests):
        first = tools["subagent_run"].run({"name": "reader", "task": "First inspection."})
        second = _resume(tools, first["run_id"], "Second inspection.")
        second_notice = _notice_events(children[-1])[0]["payload"]["message"]
        assert second_notice["role"] == "user"
        if history_source == "memory":
            launcher = tools["subagent_run"].run.__self__
            monkeypatch.setattr(
                launcher.store, "sessions_dir", tmp_path / "sessions", raising=False
            )
            monkeypatch.setattr(children[-1].store, "events_snapshot", lambda: [])
            monkeypatch.setattr(subagent_execution, "read_session_events", lambda _path: [])
        third = _resume(tools, second["run_id"], "Third inspection.")
        third_notice = _notice_events(children[-1])[0]["payload"]["message"]
        if history_source == "memory":
            monkeypatch.setattr(children[-1].store, "events_snapshot", lambda: [])
        fourth = _resume(tools, third["run_id"], "Fourth inspection.")
        assert fourth["status"] == "success"
        last = children[-1]
        fourth_notice = _notice_events(last)[0]["payload"]["message"]
        for notice in (second_notice, third_notice, fourth_notice):
            assert last.messages.count(notice) == 1
        for previous, current in zip(requests, requests[1:], strict=False):
            previous_history = _retained_history(previous, "input")
            assert _retained_history(current, "input")[: len(previous_history)] == previous_history
        assert [
            e["payload"]["content"]
            for e in last.store.events_snapshot()
            if e.get("type") == "user_message"
        ] == ["Fourth inspection."]
        assert all(
            last.messages.count({"role": "user", "content": task}) == 1
            for task in (
                "First inspection.",
                "Second inspection.",
                "Third inspection.",
                "Fourth inspection.",
            )
        )


@pytest.mark.parametrize(
    "failure_stage", ["before_session", "after_attach_before_notice", "after_notice_before_turn"]
)
def test_startup_failure_does_not_invent_a_notice_or_lose_previous_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    with _children(tmp_path, monkeypatch, "responses") as (tools, registry, children, requests):
        first = tools["subagent_run"].run({"name": "reader", "task": "First inspection."})
        second = _resume(tools, first["run_id"], "Second inspection.")
        old_notice = _notice_events(children[-1])[0]["payload"]["message"]
        prior_requests = copy.deepcopy(requests)
        with monkeypatch.context() as patch:
            if failure_stage == "before_session":

                def fail_start(**_kwargs: Any) -> Any:
                    raise RuntimeError("Synthetic startup failure")

                patch.setattr(subagent_execution, "_create_session_for_subagent", fail_start)
            elif failure_stage == "after_attach_before_notice":
                patch.setitem(
                    registry,
                    "reader",
                    replace(registry["reader"], allow_tools=("fs_list", "unavailable_tool")),
                )
            else:

                def fail_turn(_self: Any, _task: str, **_kwargs: Any) -> Any:
                    raise RuntimeError("Synthetic failure after notice installation")

                patch.setattr(type(children[-1]), "run_turn", fail_turn)
            failed = _resume(tools, second["run_id"], "Never delivered failed task.")
        assert failed.get("error")
        assert requests == prior_requests
        scheduler = tools["subagent_run"].run.__self__.child_scheduler
        failed_child = scheduler._children[failed["run_id"]]
        failed_notices = (
            _notice_events(failed_child.sub_session) if failed_child.sub_session is not None else []
        )
        assert len(failed_notices) == int(failure_stage == "after_notice_before_turn")
        recovered = _resume(tools, failed["run_id"], "Continue after startup failure.")
        assert recovered["status"] == "success"
        current = children[-1]
        new_notice = _notice_events(current)[0]["payload"]["message"]
        assert current.messages.count(old_notice) == current.messages.count(new_notice) == 1
        for event in failed_notices:
            assert current.messages.count(event["payload"]["message"]) == 1
        assert "Never delivered failed task." not in json.dumps(current.messages)
        prior_history = _retained_history(prior_requests[-1], "input")
        assert _retained_history(requests[-1], "input")[: len(prior_history)] == prior_history
        assert current.provider_session_id == children[0].provider_session_id
