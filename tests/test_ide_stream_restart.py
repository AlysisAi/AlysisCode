from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.ide.event_stream import EventContext, ProtocolEventSurface
from alysis_code.llm.openai_compat import LLMResponse
from alysis_code.llm.types import ToolCall


def test_retry_resets_only_the_unfinished_message_in_the_owning_job(tmp_path):
    events = []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="session", job_id="job"),
        emit=events.append,
        stream_reset=True,
    )

    class Client:
        model = "test-model"
        temperature = 0.2

        def chat(self, **kwargs):
            delta = kwargs["on_text_delta"]
            delta("Abandoned.")
            delta.stream_restart()
            delta("Kept")
            delta(" text.")
            return LLMResponse(content="Kept text.", tool_calls=[], raw={})

    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", stream=True),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=True,
        api_key_override="fixture",
        enable_chat_turn_step_budget=True,
        verification_enabled=False,
        surface=surface,
    )
    session.client = Client()
    try:
        assert session.run_turn("Answer the question") == 0
    finally:
        session.close()
    messages = [e for e in events if e["type"] in {"message_delta", "message_end"}]
    assert [(e["type"], e["payload"]["text"], e["payload"].get("reset")) for e in messages] == [
        ("message_delta", "Abandoned.", None),
        ("message_delta", "", True),
        ("message_delta", "Kept", None),
        ("message_delta", " text.", None),
        ("message_end", "Kept text.", None),
    ]
    assert all(e["session_id"] == "session" and e["job_id"] == "job" for e in messages)


def test_older_hosts_keep_the_original_stream_payload():
    events = []
    surface = ProtocolEventSurface(context=EventContext(session_id="legacy"), emit=events.append)
    surface.emit_message_delta("original")
    surface.reset_streamed_assistant()
    assert [e["payload"] for e in events] == [{"text": "original", "worker_id": None, "role": None}]


def test_repeated_completed_stream_does_not_run_into_the_next_message(tmp_path):
    events = []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="session", job_id="job"),
        emit=events.append,
    )
    (tmp_path / "nested").mkdir()
    narration = "I will inspect the project."
    final = "The inspection is complete."
    responses = iter(
        (
            LLMResponse(
                content=narration,
                tool_calls=[ToolCall(id="first", name="fs_list", arguments={"path": "."})],
                raw={},
            ),
            LLMResponse(
                content=narration,
                tool_calls=[ToolCall(id="second", name="fs_list", arguments={"path": "nested"})],
                raw={},
            ),
            LLMResponse(content=final, tool_calls=[], raw={}),
        )
    )

    class Client:
        model = "test-model"
        temperature = 0.2

        def chat(self, **kwargs):
            response = next(responses)
            kwargs["on_text_delta"](response.content)
            return response

    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", stream=True),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=True,
        api_key_override="fixture",
        enable_chat_turn_step_budget=True,
        verification_enabled=False,
        surface=surface,
    )
    session.client = Client()
    try:
        assert session.run_turn("Inspect the project and the nested folder.") == 0
    finally:
        session.close()

    messages = [
        (event["type"], event["payload"]["text"])
        for event in events
        if event["type"] in {"message_delta", "message_end"}
    ]
    assert messages == [
        ("message_delta", narration),
        ("message_end", narration),
        ("message_delta", narration),
        ("message_end", narration),
        ("message_delta", final),
        ("message_end", final),
    ]
