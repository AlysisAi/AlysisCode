from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from alysis_code.agent_loop import AgentSession, create_session
from alysis_code.compaction.conversation_compactor import MEMORY_MARKER, PINS_MARKER
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMError, LLMResponse, ToolCall
from alysis_code.token_budget import estimate_tokens


class ScriptedClient:
    def __init__(self, *, model: str, responses: list[LLMResponse]) -> None:
        self.model = model
        self.temperature = 1.0
        self._responses = responses
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
    ) -> LLMResponse:
        _ = messages, tools, stream, on_text_delta
        if self.calls >= len(self._responses):
            raise AssertionError("ScriptedClient exhausted responses")
        response = self._responses[self.calls]
        self.calls += 1
        return response


class RetrySizingCompactor:
    def __init__(self) -> None:
        self.model = "compactor-model"
        self.temperature = 0.2
        self.calls = 0
        self.first_tokens: int | None = None
        self.last_tokens: int | None = None
        self.retried_with_smaller_payload = False

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
    ) -> LLMResponse:
        _ = tools, stream, on_text_delta
        payload = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        tokens = estimate_tokens(payload)
        self.calls += 1
        if self.calls == 1:
            self.first_tokens = tokens
            raise LLMError("context length exceeded")
        self.last_tokens = tokens
        if self.first_tokens is None:
            raise AssertionError("Expected first_tokens to be set before retry")
        if tokens >= self.first_tokens:
            raise AssertionError("Retry payload was not reduced")
        self.retried_with_smaller_payload = True
        summary = {
            "goal": "integration",
            "constraints": ["do not break public API"],
            "decisions": [],
            "work_done": [],
            "open_threads": [],
            "next_steps": [],
        }
        return LLMResponse(content=json.dumps(summary), tool_calls=[], raw={})


class AlwaysFailCompactor:
    def __init__(self) -> None:
        self.model = "compactor-model"
        self.temperature = 0.2
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
    ) -> LLMResponse:
        _ = messages, tools, stream, on_text_delta
        self.calls += 1
        raise LLMError("context length exceeded")


class OverflowThenSuccessClient:
    def __init__(self, *, model: str) -> None:
        self.model = model
        self.temperature = 1.0
        self.calls = 0
        self.request_messages: list[list[dict[str, Any]]] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
    ) -> LLMResponse:
        _ = tools, stream, on_text_delta
        self.calls += 1
        self.request_messages.append(list(messages))
        if self.calls == 1:
            raise LLMError("LLM error 400: maximum context length exceeded")
        return LLMResponse(content="recovered", tool_calls=[], raw={})


def _build_cfg() -> AppConfig:
    cfg = AppConfig(
        model="test-model",
        stream=False,
        max_steps=6,
        temperature=1.0,
        routing_mode="code_only",
    )
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "test-model": {"context_window_tokens": 4096, "max_output_tokens": 512},
                "compactor-model": {"context_window_tokens": 2048, "max_output_tokens": 256},
            },
            "default": {"context_window_tokens": 4096, "max_output_tokens": 512},
        },
        "compaction": {
            "enabled": True,
            "summarize_conversation": True,
            "offload_tool_outputs": True,
            "tool_output_offload_threshold_chars": 500,
            "tool_output_preview_chars": 120,
            "recent_user_turns_to_keep": 1,
            "trigger_ratio": 0.45,
            "target_ratio": 0.30,
            "max_chunk_messages": 60,
            "safety_margin_tokens": 256,
            "max_pins": 20,
            "pin_snippet_chars": 200,
        },
    }
    return cfg


def _append_turn(session: AgentSession, *, user_text: str, assistant_text: str) -> None:
    session.messages.append({"role": "user", "content": user_text})
    session.messages.append({"role": "assistant", "content": assistant_text})


def _compaction_session_dir(session: AgentSession) -> Path:
    return session.store.session_artifact_root


def test_run_turn_compaction_and_offload_creates_artifacts_and_pins(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_MODEL_COMPACTOR", "compactor-model")
    big_file = tmp_path / "big.txt"
    big_file.write_text("A" * 24000, encoding="utf-8")

    session = create_session(
        cfg=_build_cfg(),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=6,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / ".alysis" / "sessions",
        session_id_override="integration-sid",
        enable_compaction=True,
    )
    try:
        assert session.conversation_compactor is not None
        session.client = ScriptedClient(  # type: ignore[assignment]
            model="test-model",
            responses=[
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="tc-read",
                            name="fs_read",
                            arguments={"path": "big.txt", "max_bytes": 20000},
                        )
                    ],
                    raw={},
                ),
                LLMResponse(content="Final answer.", tool_calls=[], raw={}),
            ],
        )

        compactor_summary = {
            "goal": "integration",
            "constraints": ["do not change public API"],
            "decisions": ["keep review mode defaults"],
            "work_done": [],
            "open_threads": [],
            "next_steps": [],
        }
        session.conversation_compactor.compactor_client = ScriptedClient(  # type: ignore[assignment]
            model="compactor-model",
            responses=[
                LLMResponse(content=json.dumps(compactor_summary), tool_calls=[], raw={})
                for _ in range(12)
            ],
        )

        _append_turn(
            session,
            user_text="ok " + ("l" * 9000),
            assistant_text="ack " + ("m" * 9000),
        )
        _append_turn(
            session,
            user_text=(
                "MUST keep API stable. DO NOT rename commands. "
                "Acceptance criteria: tests pass. Constraint: preserve behavior. " + ("h" * 15000)
            ),
            assistant_text="noted " + ("n" * 4000),
        )
        _append_turn(
            session,
            user_text="recent context " + ("r" * 5000),
            assistant_text="recent ack",
        )

        exit_code = session.run_turn("Please read big.txt and summarize key points.")
        assert exit_code == 0

        compaction_session_dir = _compaction_session_dir(session)
        offload_session_dir = session.store.session_artifact_root
        history_files = sorted((compaction_session_dir / "history").glob("chunk_*.jsonl"))
        assert history_files, "Expected at least one compacted history chunk"
        history_text = "\n".join(path.read_text(encoding="utf-8") for path in history_files)
        assert "MUST keep API stable" in history_text
        summary_path = compaction_session_dir / "memory" / "summary.json"
        assert summary_path.exists()
        summary_data = json.loads(summary_path.read_text(encoding="utf-8"))
        assert summary_data.get("goal") == "integration"
        assert "keep review mode defaults" in (summary_data.get("decisions") or [])
        pins_path = compaction_session_dir / "memory" / "pins.json"
        assert pins_path.exists()
        offload_files = sorted((offload_session_dir / "tool_outputs").glob("*.json"))
        assert offload_files, "Expected offloaded tool output artifact(s)"
        offload_data = json.loads(offload_files[0].read_text(encoding="utf-8"))
        assert offload_data.get("tool_name") == "fs_read"
        assert "result" not in offload_data
        canonical_result = json.loads(str(offload_data.get("content_json") or "{}"))
        full_content = str(canonical_result.get("content") or "")
        assert len(full_content) > 10000
        assert "A" * 1000 in full_content
        session_log_text = session.store.path.read_text(encoding="utf-8")
        assert full_content not in session_log_text

        tool_messages = [m for m in session.messages if str(m.get("role")) == "tool"]
        assert tool_messages, "Expected tool message in conversation"
        tool_stub = json.loads(str(tool_messages[-1].get("content") or "{}"))
        assert tool_stub.get("offloaded") is True
        assert tool_stub.get("artifact_locator") == (
            "session_artifacts/tool_outputs/step1_fs_read_tc-read.json"
        )
        assert tool_stub.get("artifact_readable_via_fs") is True
        assert tool_stub.get("raw_saved_in_session_log") is False
        assert "use session_artifact_read with that locator" in str(
            tool_stub.get("full_output") or ""
        )

        pins_data = json.loads(pins_path.read_text(encoding="utf-8"))
        assert any(
            (
                "MUST keep API stable" in str(pin.get("text") or "")
                or "Acceptance criteria" in str(pin.get("text") or "")
            )
            for pin in pins_data.get("pins", [])
        )
        assert any(
            str(m.get("content") or "").startswith(MEMORY_MARKER)
            for m in session.messages
            if str(m.get("role")) == "user"
        )
        assert any(
            str(m.get("content") or "").startswith(PINS_MARKER)
            for m in session.messages
            if str(m.get("role")) == "user"
        )
    finally:
        session.close()


def test_compactor_context_length_retry_then_success_end_to_end(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_MODEL_COMPACTOR", "compactor-model")
    session = create_session(
        cfg=_build_cfg(),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=True,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "session-artifacts",
        session_id_override="integration-retry",
        enable_compaction=True,
    )
    try:
        assert session.conversation_compactor is not None
        session.client = ScriptedClient(  # type: ignore[assignment]
            model="test-model",
            responses=[LLMResponse(content="done", tool_calls=[], raw={})],
        )
        retry_compactor = RetrySizingCompactor()
        session.conversation_compactor.compactor_client = retry_compactor  # type: ignore[assignment]

        _append_turn(
            session,
            user_text="very large low signal " + ("x" * 22000),
            assistant_text="assistant filler " + ("y" * 22000),
        )
        _append_turn(
            session,
            user_text="recent keep " + ("z" * 3000),
            assistant_text="recent assistant",
        )

        exit_code = session.run_turn("continue")
        assert exit_code == 0
        assert retry_compactor.calls >= 2
        assert retry_compactor.first_tokens is not None
        assert retry_compactor.last_tokens is not None
        assert retry_compactor.retried_with_smaller_payload is True

        session_dir = _compaction_session_dir(session)
        summary_path = session_dir / "memory" / "summary.json"
        assert summary_path.exists()
        summary_data = json.loads(summary_path.read_text(encoding="utf-8"))
        assert summary_data.get("goal") == "integration"
        assert list((session_dir / "history").glob("chunk_*.jsonl"))
    finally:
        session.close()


def test_compactor_fails_both_attempts_preserves_chunk_and_still_completes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_MODEL_COMPACTOR", "compactor-model")
    session = create_session(
        cfg=_build_cfg(),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=True,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "session-artifacts",
        session_id_override="integration-dropchunk",
        enable_compaction=True,
    )
    try:
        assert session.conversation_compactor is not None
        session.client = ScriptedClient(  # type: ignore[assignment]
            model="test-model",
            responses=[LLMResponse(content="done", tool_calls=[], raw={})],
        )
        failing_compactor = AlwaysFailCompactor()
        session.conversation_compactor.compactor_client = failing_compactor  # type: ignore[assignment]

        high_signal = (
            "MUST preserve this requirement. DO NOT remove approval UX. "
            "Acceptance criteria: no stalls. " + ("q" * 18000)
        )
        _append_turn(
            session,
            user_text=high_signal,
            assistant_text="ack " + ("w" * 8000),
        )
        _append_turn(
            session,
            user_text="recent keep " + ("e" * 3000),
            assistant_text="recent assistant",
        )

        exit_code = session.run_turn("continue")
        assert exit_code == 0
        assert failing_compactor.calls >= 2

        session_dir = _compaction_session_dir(session)
        assert not list((session_dir / "history").glob("chunk_*.jsonl"))
        assert not (session_dir / "memory" / "summary.json").exists()
        assert not (session_dir / "memory" / "pins.json").exists()
        serialized_messages = json.dumps(session.messages, ensure_ascii=False)
        assert high_signal in serialized_messages
    finally:
        session.close()


def test_main_provider_context_overflow_compacts_exact_request_and_retries_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_MODEL_COMPACTOR", "compactor-model")
    cfg = _build_cfg()
    models = cfg.extra_fields["model_metadata_overrides"]["models"]  # type: ignore[index]
    models["test-model"] = {"context_window_tokens": 100_000, "max_output_tokens": 4096}
    cfg.extra_fields["compaction"].update(  # type: ignore[index]
        {
            "recent_user_turns_to_keep": 8,
            "trigger_ratio": 0.95,
            "target_ratio": 0.70,
        }
    )
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "session-artifacts",
        session_id_override="integration-overflow-recovery",
        enable_compaction=True,
    )
    try:
        assert session.conversation_compactor is not None
        session.conversation_compactor._input_token_counter = None
        summary = {
            "goal": "recover safely",
            "constraints": ["preserve the active turn"],
            "decisions": [],
            "work_done": [],
            "open_threads": [],
            "next_steps": [],
        }
        session.conversation_compactor.compactor_client = ScriptedClient(  # type: ignore[assignment]
            model="compactor-model",
            responses=[LLMResponse(content=json.dumps(summary), tool_calls=[], raw={})],
        )
        main_client = OverflowThenSuccessClient(model="test-model")
        session.client = main_client  # type: ignore[assignment]
        old_turn = "old turn that should be compacted " + ("x" * 4000)
        session.messages.extend(
            [
                {"role": "user", "content": old_turn},
                {"role": "assistant", "content": "old response"},
                {"role": "user", "content": "recent turn to preserve"},
                {"role": "assistant", "content": "recent response"},
            ]
        )

        exit_code = session.run_turn("current active instruction")

        assert exit_code == 0
        assert main_client.calls == 2
        assert old_turn in json.dumps(main_client.request_messages[0], ensure_ascii=False)
        assert old_turn not in json.dumps(main_client.request_messages[1], ensure_ascii=False)
        assert "current active instruction" in json.dumps(
            main_client.request_messages[1], ensure_ascii=False
        )
    finally:
        session.close()
