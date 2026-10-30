from __future__ import annotations

import json

import pytest
from test_agent_loop_event_emission import _make_session, _RecordingEventSurface

from alysis_code.agent.tool_repetition import (
    EXTERNAL_TOOL_PROGRESS_CHECKPOINT,
    EXTERNAL_TOOL_REASSESSMENT,
    ExternalToolRepetitionTracker,
)
from alysis_code.agent_loop import ToolDef
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.session_store import SessionStore, read_session_events
from alysis_code.surface.events import ToolCallCompleted


def test_repetition_is_bounded_and_retains_no_arguments():
    tracker = ExternalToolRepetitionTracker()
    notices = [
        tracker.observe(
            tool_name="arbitrary_external_tool",
            arguments={"target": "PRIVATE document", "page": 1},
            metadata={"tool_type": "mcp"},
            succeeded=True,
        )
        for _ in range(12)
    ]
    assert [i + 1 for i, notice in enumerate(notices) if notice] == [3, 6]
    assert all(n["state_unchanged"] == "not_established" for n in notices if n)
    assert "PRIVATE" not in repr(tracker.__dict__)
    assert "PRIVATE" not in json.dumps(notices)


@pytest.mark.parametrize("reset", ["page", "action", "failed", "builtin"])
def test_new_work_and_failures_break_the_repetition_episode(reset):
    tracker = ExternalToolRepetitionTracker()
    common = dict(
        tool_name="read", arguments={"page": 1}, metadata={"tool_type": "mcp"}, succeeded=True
    )
    assert tracker.observe(**common) is None
    assert tracker.observe(**common) is None
    changed = dict(common)
    if reset == "page":
        changed["arguments"] = {"page": 2}
    elif reset == "action":
        changed["tool_name"] = "different_operation"
    elif reset == "failed":
        changed["succeeded"] = False
    else:
        changed["metadata"] = {"tool_type": "builtin"}
    assert tracker.observe(**changed) is None
    assert tracker.observe(**common) is None


def test_varied_requests_reach_a_checkpoint_without_claiming_no_progress():
    tracker = ExternalToolRepetitionTracker()
    notices = [
        tracker.observe(
            tool_name=f"arbitrary_tool_{i % 2}",
            arguments={"page": i, "private": "PRIVATE observation argument"},
            metadata={"tool_type": "mcp"},
            succeeded=i % 4 != 0,
        )
        for i in range(30)
    ]
    assert [i + 1 for i, notice in enumerate(notices) if notice] == [8]
    for notice in filter(None, notices):
        assert notice["trigger"] == "activity_checkpoint"
        assert notice["requests_since_notice"] == 8
        assert notice["failed_requests_since_notice"] == 2
        assert notice["state_unchanged"] == "not_established"
    assert "PRIVATE" not in repr(tracker.__dict__)


def test_local_calls_do_not_trigger_external_activity_checkpoints():
    tracker = ExternalToolRepetitionTracker()
    for i in range(24):
        assert (
            tracker.observe(
                tool_name="local_read",
                arguments={"page": i},
                metadata={"tool_type": "builtin"},
                succeeded=True,
            )
            is None
        )


def test_repetition_and_activity_share_one_notice_budget():
    tracker = ExternalToolRepetitionTracker()
    notices = []
    for i in range(30):
        notice = tracker.observe(
            tool_name="external_operation",
            arguments={"page": max(0, i - 2)},
            metadata={"tool_type": "mcp"},
            succeeded=True,
        )
        if notice:
            notices.append((i + 1, notice["trigger"]))
    assert notices == [(3, "request_repetition"), (11, "activity_checkpoint")]


def test_activity_reserves_a_notice_for_repetition_late_in_a_long_workflow():
    tracker = ExternalToolRepetitionTracker()
    notices = []
    for i in range(40):
        notice = tracker.observe(
            tool_name="arbitrary_scan",
            arguments={"page": min(i, 24)},
            metadata={"tool_type": "mcp"},
            succeeded=True,
        )
        if notice:
            notices.append((i + 1, notice["trigger"]))
    assert notices == [(8, "activity_checkpoint"), (27, "request_repetition")]


def test_real_turn_reassesses_late_repetition_after_useful_external_work(tmp_path):
    invoked = []

    class Client:
        model = "test-model"
        temperature = 0.2
        count = 0

        def chat(self, *, messages, tools, tool_choice=None, **kwargs):
            self.count += 1
            assert tool_choice is None
            if self.count == 20:
                assert any(m.get("content") == EXTERNAL_TOOL_REASSESSMENT for m in messages)
                assert any("new-evidence-19" in str(m.get("content")) for m in messages)
                return LLMResponse(
                    content="The repeated query still supplied new evidence; no change is needed.",
                    tool_calls=[],
                    raw={},
                )
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id=str(self.count),
                        name="arbitrary_scan",
                        arguments={"page": min(self.count, 17)},
                    )
                ],
                raw={},
            )

    def observe(args):
        invoked.append(args["page"])
        return {"is_error": False, "content": f"new-evidence-{len(invoked)}"}

    tool = ToolDef(
        name="arbitrary_scan",
        description="Read evidence",
        parameters={"type": "object"},
        run=observe,
        metadata={"tool_type": "mcp"},
    )
    session = _make_session(
        root=tmp_path, client=Client(), surface=_RecordingEventSurface(), tool=tool
    )
    session.max_steps = 20
    try:
        assert (
            session.run_turn("Read the evidence and report its state. Do not change anything.") == 0
        )
        assert invoked == [*range(1, 17), 17, 17, 17]
    finally:
        session.close()


def test_real_turn_checkpoint_preserves_useful_pagination_and_action_choice(tmp_path):
    invoked = []

    class Client:
        model = "test-model"
        temperature = 0.2
        count = 0

        def chat(self, *, messages, tools, tool_choice=None, **kwargs):
            self.count += 1
            assert tool_choice is None
            if self.count == 9:
                assert any(m.get("content") == EXTERNAL_TOOL_PROGRESS_CHECKPOINT for m in messages)
                assert any("useful-page-8" in str(m.get("content")) for m in messages)
            return LLMResponse(
                content="Each page provided new evidence; all nine pages are now read."
                if self.count == 10
                else "",
                tool_calls=[]
                if self.count == 10
                else [
                    ToolCall(
                        id=str(self.count), name="arbitrary_scan", arguments={"page": self.count}
                    )
                ],
                raw={},
            )

    def observe(args):
        invoked.append(args["page"])
        return {"is_error": False, "content": f"useful-page-{args['page']}"}

    tool = ToolDef(
        name="arbitrary_scan",
        description="Read a page",
        parameters={"type": "object"},
        run=observe,
        metadata={"tool_type": "mcp"},
    )
    session = _make_session(
        root=tmp_path, client=Client(), surface=_RecordingEventSurface(), tool=tool
    )
    session.max_steps = 10
    try:
        assert (
            session.run_turn("Read the nine pages and report what you learned. Do not change them.")
            == 0
        )
        assert invoked == list(range(1, 10))
        assert not list(tmp_path.glob("*.txt"))
    finally:
        session.close()


def test_real_turn_reassesses_mcp_reads_without_forcing_actions_or_repo_writes(tmp_path):
    surface = _RecordingEventSurface()
    invoked = []

    class Client:
        model = "test-model"
        temperature = 0.2

        def __init__(self):
            self.count = 0

        def chat(self, *, messages, tools, tool_choice=None, **kwargs):
            self.count += 1
            assert tool_choice is None
            assert any(t["function"]["name"] == "arbitrary_scan" for t in tools)
            if self.count < 4:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(id=str(self.count), name="arbitrary_scan", arguments={"page": 1})
                    ],
                    raw={},
                )
            assert any(m.get("content") == EXTERNAL_TOOL_REASSESSMENT for m in messages)
            assert any("observation-3" in str(m.get("content")) for m in messages)
            return LLMResponse(
                content="The observations are changing; the operation is still in progress.",
                tool_calls=[],
                raw={},
            )

    def observe(args):
        invoked.append(args)
        # Even fresh IDs/new results must not conceal identical repeated requests.
        return {"is_error": False, "content": f"observation-{len(invoked)}"}

    tool = ToolDef(
        name="arbitrary_scan",
        description="Observe",
        parameters={"type": "object"},
        run=observe,
        metadata={"tool_type": "mcp"},
    )
    session = _make_session(root=tmp_path, client=Client(), surface=surface, tool=tool)
    session.store = SessionStore(
        enabled=True,
        sessions_dir=tmp_path / "logs",
        session_id="repetition",
        cwd=str(tmp_path),
        repo_root=str(tmp_path),
    )
    try:
        assert (
            session.run_turn("Observe the operation and report its state. Do not change it.") == 0
        )
        assert len(invoked) == 3
        notices = [
            e
            for e in read_session_events(session.store.path)
            if e["type"] == "controller_intervention"
            and e["payload"].get("detail") == "external_tool_reassessment"
        ]
        assert len(notices) == 1
    finally:
        session.close()


def test_mcp_error_flag_reaches_surface_as_failure(tmp_path):
    surface = _RecordingEventSurface()

    class Client:
        model = "test-model"
        temperature = 0.2
        count = 0

        def chat(self, **kwargs):
            self.count += 1
            return LLMResponse(
                content="" if self.count == 1 else "The observation failed.",
                tool_calls=[ToolCall(id="failed", name="external_read", arguments={})]
                if self.count == 1
                else [],
                raw={},
            )

    tool = ToolDef(
        name="external_read",
        description="Observe",
        parameters={"type": "object"},
        run=lambda args: {"is_error": True, "content": "not dispatched"},
        metadata={"tool_type": "mcp"},
    )
    session = _make_session(root=tmp_path, client=Client(), surface=surface, tool=tool)
    try:
        assert session.run_turn("Read the external state.") == 0
        completed = [e for e in surface.events if isinstance(e, ToolCallCompleted)]
        assert len(completed) == 1
        assert completed[0].success is False
    finally:
        session.close()
