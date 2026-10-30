from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent.steering import steer_inbox_for
from alysis_code.agent.tools_assembly import ToolDef, build_tools
from alysis_code.agent_loop import create_session
from alysis_code.cli_impl.tui.surface import TuiSurface
from alysis_code.cli_impl.tui.transcript import TuiTranscript
from alysis_code.config import AppConfig
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.session_store import SessionStore
from alysis_code.user_questions import UserQuestionBroker

QUESTIONS = {
    "questions": [
        {"id": "color", "question": "Which color?", "kind": "single", "options": ["Blue", "Green"]}
    ]
}


def make_session(tmp_path: Path, surface: TuiSurface, client: Any):
    cfg = AppConfig(model="test-model", routing_mode="code_only", stream=False, max_steps=6)
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "default": {"context_window_tokens": 8192, "max_output_tokens": 512}
        }
    }
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="readonly",
        yes=True,
        max_steps=6,
        no_log=False,
        api_key_override="test-key",
        surface=surface,
        session_log_dir_override=tmp_path / "sessions",
        enable_compaction=False,
        enable_tool_output_offload=False,
        enable_conversation_summarization=False,
    )
    session.client = client
    return session


@pytest.mark.parametrize("finalize_early", [False, True])
def test_real_turn_roundtrip_and_independent_work(tmp_path, finalize_early):
    surface = TuiSurface(TuiTranscript())
    calls = []
    independent = threading.Event()
    answered = threading.Event()

    class Client:
        model = "test-model"
        temperature = 0.0

        def chat(self, *, messages, **kwargs):
            calls.append(list(messages))
            if len(calls) == 1:
                return LLMResponse(
                    content="", tool_calls=[ToolCall("q1", "request_user_input", QUESTIONS)], raw={}
                )
            if len(calls) == 2:
                assert surface.user_questions.active is not None
                return LLMResponse(
                    content="", tool_calls=[ToolCall("probe", "independent_probe", {})], raw={}
                )
            if len(calls) == 3:
                if finalize_early:
                    return LLMResponse(content="Premature final", tool_calls=[], raw={})
                request_id = next(
                    json.loads(message["content"])["request_id"]
                    for message in messages
                    if message.get("tool_call_id") == "q1"
                )
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCall("wait1", "wait_user_input", {"request_id": request_id})],
                    raw={},
                )
            assert len(calls) == 4
            assert "My exact detail 42" in json.dumps(messages, ensure_ascii=False)
            return LLMResponse(content="Your preference is Green.", tool_calls=[], raw={})

    session = make_session(tmp_path, surface, Client())
    session.tools["independent_probe"] = ToolDef(
        "independent_probe",
        "Independent read",
        {"type": "object", "properties": {}},
        lambda _: independent.set() or {"ok": True},
    )
    session.tool_list = [tool.as_openai_tool() for tool in session.tools.values()]

    def answer():
        if independent.wait(5):
            request = surface.user_questions.active
            assert request is not None
            surface.user_questions.submit(
                request.id, [{"id": "color", "selected": ["Green"], "text": "My exact detail 42"}]
            )
            answered.set()

    worker = threading.Thread(target=answer, daemon=True)
    worker.start()
    try:
        code = session.run_turn("Ask my color preference and summarize it.")
        assert code == 0
        assert answered.is_set()
        assert len(calls) == 4
        deliveries = [
            message
            for message in session.messages
            if "My exact detail 42" in str(message.get("content", ""))
        ]
        assert len(deliveries) == 1
        assert "Green" in deliveries[0]["content"]
        assert not surface.user_questions.outstanding
        if not finalize_early:
            verified = [
                event["payload"]
                for event in session.store._events
                if event["type"] == "user_question_response"
            ]
            assert len(verified) == 1
            assert verified[0]["answers"][0]["text"] == "My exact detail 42"
            assert verified[0]["question_evidence"][0]["id"] == "color"
        # Tool call/result adjacency remains legal for every provider.
        for index, message in enumerate(session.messages):
            if message.get("role") == "tool":
                assert session.messages[index - 1]["role"] in {"assistant", "tool"}
        assert any(
            "Green" in json.dumps(event, ensure_ascii=False) for event in session.store._events
        )
        assert "My exact detail 42" in session.store.path.read_text(encoding="utf-8")
        from alysis_code import cli as cli_module

        resumed = cli_module._load_chat_resume_messages(session.store.path)
        assert (
            sum("My exact detail 42" in str(message.get("content", "")) for message in resumed) == 1
        )
    finally:
        surface.user_questions.reset()
        worker.join(timeout=2)
        session.close()


@pytest.mark.parametrize(
    "settings",
    [
        {"non_interactive": True},
        {"one_shot_execution": True},
        {"subagent_depth": 1},
        {"runtime_kind": "forge_exec"},
    ],
)
def test_noninteractive_and_child_tools_do_not_request_tui_answers(tmp_path, settings):
    surface = TuiSurface(TuiTranscript())
    store = SessionStore(
        sessions_dir=tmp_path / "session",
        enabled=False,
        session_id="test",
        cwd=str(tmp_path),
        repo_root=None,
    )
    tools = build_tools(
        root=tmp_path,
        console=None,
        surface=surface,
        store=store,
        mode="readonly",
        yes=True,
        **settings,
    )
    assert "request_user_input" not in tools
    assert "wait_user_input" not in tools


@pytest.mark.parametrize("finalize_early", [False, True])
def test_timeout_continues_real_turn_and_persists_skipped_questions(tmp_path, finalize_early):
    now = [0.0]
    surface = TuiSurface(TuiTranscript())
    surface.user_questions = UserQuestionBroker(clock=lambda: now[0])
    calls = []

    def question_results(messages):
        results = []
        for message in messages:
            content = str(message.get("content", ""))
            if message.get("tool_call_id") == "wait1" or content.startswith(
                "[User question response] "
            ):
                results.append(json.loads(content.removeprefix("[User question response] ")))
        return results

    class Client:
        model = "test-model"
        temperature = 0.0

        def chat(self, *, messages, **kwargs):
            calls.append(list(messages))
            if len(calls) == 1:
                return LLMResponse(
                    content="", tool_calls=[ToolCall("q1", "request_user_input", QUESTIONS)], raw={}
                )
            if len(calls) == 2:
                assert surface.user_questions.active is not None
                return LLMResponse(
                    content="", tool_calls=[ToolCall("probe", "independent_probe", {})], raw={}
                )
            if len(calls) == 3:
                assert now[0] == 61.0
                if finalize_early:
                    return LLMResponse(content="Premature final", tool_calls=[], raw={})
                identifier = next(
                    json.loads(m["content"])["request_id"]
                    for m in messages
                    if m.get("tool_call_id") == "q1"
                )
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCall("wait1", "wait_user_input", {"request_id": identifier})],
                    raw={},
                )
            assert len(calls) == 4
            delivered = question_results(messages)
            assert len(delivered) == 1
            assert delivered[0]["status"] == "timed_out"
            assert delivered[0]["answers"] == []
            assert delivered[0]["skipped_question_ids"] == ["color"]
            return LLMResponse(
                content="I will assume Blue and continue with the summary.", tool_calls=[], raw={}
            )

    session = make_session(tmp_path, surface, Client())
    session.tools["independent_probe"] = ToolDef(
        "independent_probe",
        "Independent read",
        {"type": "object", "properties": {}},
        lambda _: now.__setitem__(0, 61.0) or {"ok": True},
    )
    session.tool_list = [tool.as_openai_tool() for tool in session.tools.values()]
    try:
        assert session.run_turn("Ask my preference and summarize; continue if I do not reply.") == 0
        assert len(calls) == 4
        assert not surface.user_questions.outstanding
        from alysis_code import cli as cli_module

        resumed = cli_module._load_chat_resume_messages(session.store.path)
        assert len(question_results(resumed)) == 1
        assert question_results(resumed)[0]["status"] == "timed_out"
        assert not any("Premature final" in str(m.get("content")) for m in resumed)
    finally:
        session.close()


@pytest.mark.parametrize("revisit", [False, True])
def test_repeated_question_is_suppressed_until_new_user_steering(tmp_path, revisit):
    now = [0.0]
    surface = TuiSurface(TuiTranscript())
    surface.user_questions = UserQuestionBroker(clock=lambda: now[0])
    calls = []

    def result_for(messages, identifier):
        return next(
            json.loads(m["content"]) for m in messages if m.get("tool_call_id") == identifier
        )

    class Client:
        model = "test-model"
        temperature = 0.0

        def chat(self, *, messages, **kwargs):
            calls.append(list(messages))
            step = len(calls)
            if step == 1:
                assert "Questions and assumptions" in messages[0]["content"]
                return LLMResponse(
                    content="", tool_calls=[ToolCall("q1", "request_user_input", QUESTIONS)], raw={}
                )
            if step == 2:
                now[0] = 60.0
                if revisit:
                    steer_inbox_for(session, create=True).send(
                        "Please ask the color question again."
                    )
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            "w1",
                            "wait_user_input",
                            {"request_id": result_for(messages, "q1")["request_id"]},
                        )
                    ],
                    raw={},
                )
            if step == 3:
                assert result_for(messages, "w1")["status"] == "timed_out"
                return LLMResponse(
                    content="", tool_calls=[ToolCall("q2", "request_user_input", QUESTIONS)], raw={}
                )
            if step == 4:
                repeated = result_for(messages, "q2")
                if revisit:
                    assert repeated["status"] == "pending"
                    assert surface.user_questions.active is not None
                    now[0] = 120.0
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCall(
                                "w2", "wait_user_input", {"request_id": repeated["request_id"]}
                            )
                        ],
                        raw={},
                    )
                assert repeated["status"] == "duplicate_questions"
                assert surface.user_questions.active is None
            else:
                assert step == 5 and revisit
                assert result_for(messages, "w2")["status"] == "timed_out"
            return LLMResponse(
                content="I will proceed with the stated assumption.", tool_calls=[], raw={}
            )

    session = make_session(tmp_path, surface, Client())
    try:
        assert session.run_turn("Ask my preference and continue if I do not reply.") == 0
        assert len(calls) == (5 if revisit else 4)
        assert not surface.user_questions.outstanding
    finally:
        session.close()


def test_desktop_opt_in_registers_live_question_tools(tmp_path):
    surface = TuiSurface(TuiTranscript())
    surface.host_managed_user_questions = True
    store = SessionStore(
        sessions_dir=tmp_path / "session",
        enabled=False,
        session_id="desktop",
        cwd=str(tmp_path),
        repo_root=None,
    )
    tools = build_tools(
        root=tmp_path,
        console=None,
        surface=surface,
        store=store,
        mode="readonly",
        yes=True,
        non_interactive=True,
        runtime_kind="interactive_chat",
    )
    assert "request_user_input" in tools
    assert "wait_user_input" in tools
