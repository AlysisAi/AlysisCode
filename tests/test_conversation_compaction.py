from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import alysis_code.compaction.conversation_compactor as compactor_mod
from alysis_code.agent.turn_contract import (
    TurnEffect,
    TurnOutcome,
    TurnRelation,
    TurnSemantics,
)
from alysis_code.agent_loop import (
    AgentSession,
    create_session,
)
from alysis_code.compaction.conversation_compactor import MEMORY_MARKER, PINS_MARKER
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse
from alysis_code.llm.types import InputTokenCount, UsageConfidence, UsageSource
from alysis_code.session_store import read_session_events


def _summary(*, decisions: list[str]) -> dict[str, Any]:
    return {
        "goal": "keep repository healthy",
        "constraints": [],
        "decisions": decisions,
        "work_done": [],
        "open_threads": [],
        "next_steps": [],
    }


def _workspace_change_semantics(
    instruction: str,
    *,
    relation: TurnRelation,
) -> TurnSemantics:
    return TurnSemantics(
        outcome=TurnOutcome.CHANGE,
        relation=relation,
        requested_effects=(TurnEffect.READ_WORKSPACE, TurnEffect.WRITE_WORKSPACE),
        evidence_quotes=(instruction,),
    )


def _explain_prior_semantics(instruction: str) -> TurnSemantics:
    return TurnSemantics(
        outcome=TurnOutcome.ANSWER,
        relation=TurnRelation.EXPLAIN_PRIOR,
        requested_effects=(TurnEffect.READ_WORKSPACE,),
        evidence_quotes=(instruction,),
    )


class _FakeMainClient:
    def __init__(self) -> None:
        self.model = "gpt-5-nano"
        self.temperature = 1.0
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
        return LLMResponse(
            content="done",
            tool_calls=[],
            raw={},
            response_model=self.model,
        )


class _FakeCompactorClient:
    def __init__(self, summaries: list[dict[str, Any]]) -> None:
        self.model = "gpt-5-nano"
        self.temperature = 0.2
        self._summaries = summaries
        self.calls = 0
        self.captured_payloads: list[dict[str, Any]] = []

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
        if messages:
            payload_raw = str(messages[-1].get("content") or "")
            try:
                payload = json.loads(payload_raw)
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                self.captured_payloads.append(payload)
            else:
                self.captured_payloads.append({})
        idx = min(self.calls - 1, len(self._summaries) - 1)
        return LLMResponse(
            content=json.dumps(self._summaries[idx]),
            tool_calls=[],
            raw={},
            response_model=self.model,
        )


class _FailingCompactorClient:
    def __init__(self, *, max_chars: int) -> None:
        self.model = "gpt-5-nano"
        self.temperature = 0.2
        self.max_chars = max_chars
        self.calls = 0

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
        payload = json.dumps(messages, ensure_ascii=False)
        if len(payload) > self.max_chars:
            from alysis_code.llm.openai_compat import LLMError

            raise LLMError("context length exceeded")
        return LLMResponse(
            content=json.dumps(_summary(decisions=["ok"])),
            tool_calls=[],
            raw={},
            response_model=self.model,
        )


def _append_turns(session: AgentSession, *, count: int, prefix: str) -> None:
    for idx in range(count):
        session.messages.append(
            {
                "role": "user",
                "content": f"{prefix}-u{idx} " + ("x" * 1600),
            }
        )
        session.messages.append(
            {
                "role": "assistant",
                "content": f"{prefix}-a{idx} " + ("y" * 1600),
            }
        )


def _assert_valid_tool_transcript(messages: list[dict[str, Any]]) -> None:
    open_tool_call_ids: set[str] = set()
    for msg in messages:
        role = str(msg.get("role") or "")
        content = str(msg.get("content") or "")
        if role == "user" and (
            content.startswith(MEMORY_MARKER) or content.startswith(PINS_MARKER)
        ):
            continue
        if role == "assistant":
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                assert not open_tool_call_ids
                ids: set[str] = set()
                for item in tool_calls:
                    assert isinstance(item, dict)
                    call_id = str(item.get("id") or "").strip()
                    assert call_id
                    assert call_id not in ids
                    ids.add(call_id)
                open_tool_call_ids = ids
                continue
            assert not open_tool_call_ids
            continue
        if role == "tool":
            tool_call_id = str(msg.get("tool_call_id") or "").strip()
            assert tool_call_id
            assert tool_call_id in open_tool_call_ids
            open_tool_call_ids.remove(tool_call_id)
            continue
        assert not open_tool_call_ids
    assert not open_tool_call_ids


def _make_cfg() -> AppConfig:
    cfg = AppConfig(model="gpt-5-nano", routing_mode="code_only")
    cfg.extra_fields = {
        "compaction": {
            "enabled": True,
            "offload_tool_outputs": False,
            "summarize_conversation": True,
            "recent_user_turns_to_keep": 3,
            "trigger_ratio": 0.25,
            "target_ratio": 0.15,
            "max_chunk_messages": 40,
            "safety_margin_tokens": 512,
        }
    }
    return cfg


def test_hosted_envelope_pressure_compacts_below_the_token_window(tmp_path: Path) -> None:
    from alysis_code.llm.openai_compat import OpenAICompatClient

    cfg = _make_cfg()
    cfg.extra_fields["compaction"].update(trigger_ratio=0.9, target_ratio=0.7)
    cfg.extra_fields["model_metadata_overrides"] = {
        "models": {
            "gpt-5-nano": {
                "context_window_tokens": 1_000_000,
                "max_output_tokens": 8192,
            }
        }
    }
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="x",
        session_log_dir_override=tmp_path / "sessions",
    )
    try:
        compactor = session.conversation_compactor
        assert compactor is not None
        compactor.update_main_client(
            OpenAICompatClient(
                base_url="https://gateway.test/v1",
                api_key="test",
                model="deepseek-flash",
                provider_key="alysis",
            )
        )
        compactor.compactor_client = _FakeCompactorClient(
            [_summary(decisions=["retained"]) for _ in range(20)]
        )
        compactor._input_token_counter = None
        for i in range(10):
            session.messages.extend(
                [
                    {"role": "user", "content": f"old request {i} " + "abc " * 7500},
                    {"role": "assistant", "content": f"old answer {i} " + "def " * 7500},
                ]
            )
        session.messages.append({"role": "user", "content": "Continue the current task."})
        args = {"tool_list": None, "main_model": "gpt-5-nano"}
        assert not compactor.request_fits_input_budget(messages=session.messages, **args)
        result, changed = compactor.maybe_compact(messages=session.messages, **args)
        assert changed
        assert result[-1]["content"] == "Continue the current task."
        assert compactor.request_fits_input_budget(messages=result, **args)
        checks = [
            e["payload"]
            for e in read_session_events(session.store.path)
            if e["type"] == "compaction_check"
        ]
        assert checks[0]["used_tokens"] < checks[0]["target_tokens"]
        assert checks[0]["request_capacity_ratio"] > 1
        assert checks[-1]["request_capacity_ratio"] <= 0.7
        assert compactor.state.history_chunk_index > 0
    finally:
        session.close()


def _init_git_repo_with_commit(repo: Path) -> None:
    repo.mkdir()
    subprocess.run(
        ["git", "-C", os.fspath(repo), "init"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", os.fspath(repo), "config", "user.name", "Test User"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", os.fspath(repo), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
        text=True,
    )
    (repo / "README.md").write_text("repo\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", os.fspath(repo), "add", "README.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", os.fspath(repo), "commit", "-m", "init"],
        check=True,
        capture_output=True,
        text=True,
    )


def _compaction_session_dir(session: AgentSession) -> Path:
    return session.store.session_artifact_root


def test_compaction_writes_history_and_inserts_memory_message(tmp_path: Path) -> None:
    cfg = _make_cfg()
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="x",
        session_log_dir_override=tmp_path / "logs",
        session_id_override="compaction-test",
    )
    session.client = _FakeMainClient()  # type: ignore[assignment]
    assert session.conversation_compactor is not None
    session.conversation_compactor.compactor_client = _FakeCompactorClient(  # type: ignore[assignment]
        [_summary(decisions=["d1"])]
    )

    try:
        _append_turns(session, count=7, prefix="old")
        old_count = len(session.messages)
        preserved_last_two_turns = list(session.messages[-4:])
        compacted, changed = session.conversation_compactor.compact_now(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            focus="compaction-test",
        )
        session.messages = compacted
    finally:
        session.close()

    assert changed is True
    assert len(session.messages) < old_count + 2
    for message in preserved_last_two_turns:
        assert message in session.messages
    assert any(
        str(msg.get("role")) == "user" and str(msg.get("content", "")).startswith(MEMORY_MARKER)
        for msg in session.messages
    )

    history_dir = _compaction_session_dir(session) / "history"
    assert history_dir.exists()
    assert list(history_dir.glob("chunk_*.jsonl"))

    summary_path = _compaction_session_dir(session) / "memory" / "summary.json"
    assert summary_path.exists()
    summary_data = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary_data["decisions"] == ["d1"]


def test_maybe_compact_does_not_compact_below_trigger_threshold(tmp_path: Path) -> None:
    cfg = _make_cfg()
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=True,
        api_key_override="x",
        session_log_dir_override=tmp_path / "session-artifacts",
        session_id_override="compaction-below-threshold",
    )
    try:
        assert session.conversation_compactor is not None
        session.conversation_compactor.compactor_client = _FakeCompactorClient(  # type: ignore[assignment]
            [_summary(decisions=["unused"])]
        )
        session.messages.extend(
            [
                {"role": "user", "content": "small request"},
                {"role": "assistant", "content": "small reply"},
            ]
        )

        compacted, changed = session.conversation_compactor.maybe_compact(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            focus="below-threshold",
        )

        assert changed is False
        assert compacted == session.messages
        assert not list((_compaction_session_dir(session) / "history").glob("chunk_*.jsonl"))
    finally:
        session.close()


def test_compactor_refreshes_calibration_route_after_client_reconfiguration(
    tmp_path: Path,
) -> None:
    session = create_session(
        cfg=_make_cfg(),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=True,
        api_key_override="x",
        session_log_dir_override=tmp_path / "session-artifacts",
        session_id_override="compaction-route-refresh",
    )
    try:
        assert session.conversation_compactor is not None
        session.client.provider_key = "provider-b"
        session.client.base_url = "https://api.provider-b.example/v1"

        session.refresh_compactor_calibration_filters()

        assert session.conversation_compactor._calibration_filters["provider_key"] == ("provider-b")
        assert session.conversation_compactor._calibration_filters["base_url_host"] == (
            "api.provider-b.example"
        )
    finally:
        session.close()


def test_maybe_compact_prefers_exact_provider_count_near_trigger(tmp_path: Path) -> None:
    cfg = _make_cfg()
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="x",
        session_log_dir_override=tmp_path / "session-artifacts",
        session_id_override="compaction-exact-count",
    )
    event_path = session.store.path
    exact_count_calls = 0
    try:
        assert session.conversation_compactor is not None
        fake_compactor = _FakeCompactorClient([_summary(decisions=["unused"])])
        session.conversation_compactor.compactor_client = fake_compactor  # type: ignore[assignment]
        _append_turns(session, count=120, prefix="large-estimate")

        def exact_counter(
            _messages: list[dict[str, Any]],
            _tools: list[dict[str, Any]] | None,
        ) -> InputTokenCount:
            nonlocal exact_count_calls
            exact_count_calls += 1
            return InputTokenCount(input_tokens=100)

        session.conversation_compactor._input_token_counter = exact_counter
        compacted, changed = session.conversation_compactor.maybe_compact(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            focus="exact-count",
        )

        assert changed is False
        assert compacted == session.messages
        assert exact_count_calls == 1
        assert fake_compactor.calls == 0
    finally:
        session.close()

    checks = [
        event
        for event in read_session_events(event_path)
        if event.get("type") == "compaction_check"
    ]
    assert checks
    payload = checks[-1]["payload"]
    assert payload["comparison_tokens"] == 100
    assert payload["token_count_source"] == "provider_count"
    assert payload["token_count_confidence"] == "authoritative"


def test_estimated_or_reported_preflight_cannot_lower_calibrated_baseline() -> None:
    for measurement in (
        InputTokenCount(
            input_tokens=100,
            source=UsageSource.LOCAL_ESTIMATE,
            confidence=UsageConfidence.ESTIMATED,
        ),
        InputTokenCount(
            input_tokens=200,
            source=UsageSource.PROVIDER_COUNT,
            confidence=UsageConfidence.REPORTED,
        ),
    ):
        used, source, confidence = compactor_mod._conservative_input_measurement(
            baseline_tokens=1000,
            measurement=measurement,
        )
        assert used == 1000
        assert source == "mixed"
        assert confidence == "estimated"

    authoritative, source, confidence = compactor_mod._conservative_input_measurement(
        baseline_tokens=1000,
        measurement=InputTokenCount(input_tokens=100),
    )
    assert authoritative == 100
    assert source == "provider_count"
    assert confidence == "authoritative"

    calibrated, source, confidence = compactor_mod._conservative_input_measurement(
        baseline_tokens=500,
        measurement=InputTokenCount(
            input_tokens=600,
            source=UsageSource.LOCAL_ESTIMATE,
            confidence=UsageConfidence.ESTIMATED,
        ),
        estimate_multiplier=1.6,
    )
    assert calibrated == 960
    assert source == "mixed"
    assert confidence == "estimated"


def test_overflow_fit_check_keeps_calibration_above_estimated_preflight(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session = create_session(
        cfg=_make_cfg(),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="x",
        session_log_dir_override=tmp_path / "session-artifacts",
        session_id_override="compaction-calibrated-fit-check",
    )
    event_path = session.store.path
    try:
        assert session.conversation_compactor is not None
        monkeypatch.setattr(compactor_mod, "estimate_request_tokens", lambda *_args: 300)
        monkeypatch.setattr(compactor_mod, "compute_input_budget", lambda *_args, **_kwargs: 500)
        monkeypatch.setattr(
            session.usage_summary,
            "recent_calibration_snapshot",
            lambda **_kwargs: {"prompt_estimate_error_ratio_p90": 2.0},
        )
        session.conversation_compactor._input_token_counter = lambda _messages, _tools: (
            InputTokenCount(
                input_tokens=400,
                source=UsageSource.LOCAL_ESTIMATE,
                confidence=UsageConfidence.ESTIMATED,
            )
        )

        fits = session.conversation_compactor.request_fits_input_budget(
            messages=[{"role": "user", "content": "small"}],
            tool_list=None,
            main_model=session.client.model,
        )

        assert fits is False
    finally:
        session.close()

    verification = [
        event
        for event in read_session_events(event_path)
        if event.get("type") == "compaction_budget_verification"
    ][-1]["payload"]
    assert verification["local_used_tokens"] == 300
    assert verification["calibrated_used_tokens"] == 600
    assert verification["used_tokens"] == 800
    assert verification["token_count_source"] == "mixed"
    assert verification["token_count_confidence"] == "estimated"


def test_compaction_counts_provider_bound_ephemeral_messages(tmp_path: Path) -> None:
    cfg = _make_cfg()
    cfg.extra_fields["model_metadata_overrides"] = {
        "models": {
            "gpt-5-nano": {
                "context_window_tokens": 8192,
                "max_output_tokens": 1024,
            }
        }
    }
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="x",
        session_log_dir_override=tmp_path / "logs",
        session_id_override="compaction-provider-bound-count",
    )
    captured: list[list[dict[str, Any]]] = []
    try:
        assert session.conversation_compactor is not None
        session.messages.append({"role": "user", "content": "small persistent request"})

        def request_builder(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                *messages,
                {"role": "system", "content": "EPHEMERAL " + ("x" * 30000)},
            ]

        def exact_counter(
            messages: list[dict[str, Any]],
            _tools: list[dict[str, Any]] | None,
        ) -> InputTokenCount:
            captured.append(messages)
            return InputTokenCount(input_tokens=100)

        session.conversation_compactor._input_token_counter = exact_counter
        compacted, changed = session.conversation_compactor.maybe_compact(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            request_messages_builder=request_builder,
        )

        assert changed is False
        assert compacted == session.messages
        assert captured
        assert any("EPHEMERAL" in str(message.get("content") or "") for message in captured[-1])
    finally:
        session.close()


def test_multimodal_request_uses_provider_count_below_estimate_threshold(tmp_path: Path) -> None:
    cfg = _make_cfg()
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="x",
        session_log_dir_override=tmp_path / "logs",
        session_id_override="compaction-media-count",
    )
    exact_calls = 0
    try:
        assert session.conversation_compactor is not None
        session.messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "inspect"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            }
        )

        def exact_counter(
            _messages: list[dict[str, Any]],
            _tools: list[dict[str, Any]] | None,
        ) -> InputTokenCount:
            nonlocal exact_calls
            exact_calls += 1
            return InputTokenCount(input_tokens=100)

        session.conversation_compactor._input_token_counter = exact_counter
        compacted, changed = session.conversation_compactor.maybe_compact(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
        )

        assert changed is False
        assert compacted == session.messages
        assert exact_calls == 1
    finally:
        session.close()


def test_multimodal_compat_estimate_reserves_target_ratio(tmp_path: Path) -> None:
    cfg = _make_cfg()
    cfg.extra_fields["compaction"].update(  # type: ignore[index]
        {
            "trigger_ratio": 0.90,
            "target_ratio": 0.70,
            "recent_user_turns_to_keep": 1,
        }
    )
    cfg.extra_fields["model_metadata_overrides"] = {
        "models": {
            "gpt-5-nano": {
                "context_window_tokens": 12000,
                "max_output_tokens": 1000,
            }
        }
    }
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="x",
        session_log_dir_override=tmp_path / "logs",
        session_id_override="compaction-media-uncertainty",
    )
    event_path = session.store.path
    try:
        assert session.conversation_compactor is not None
        compactor = session.conversation_compactor
        compactor.compactor_client = _FakeCompactorClient(  # type: ignore[assignment]
            [_summary(decisions=["preserve media request"])]
        )
        media_message = {
            "role": "user",
            "content": [
                {"type": "text", "text": "inspect"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                },
            ],
        }
        session.messages.append(media_message)
        budget = compactor_mod.compute_input_budget(
            session.model_registry.get(session.client.model),
            safety_margin=compactor._settings.safety_margin_tokens,
        )
        target_tokens = int(budget * compactor._settings.target_ratio)
        trigger_tokens = int(budget * compactor._settings.trigger_ratio)
        turn = 0
        while compactor_mod.estimate_request_tokens(session.messages, None) <= (
            target_tokens + 100
        ):
            text = " ".join(f"segment_{turn}_{idx}" for idx in range(100))
            session.messages[-1:-1] = [
                {"role": "user", "content": text},
                {"role": "assistant", "content": text},
            ]
            turn += 1
            assert turn < 60
        estimated = compactor_mod.estimate_request_tokens(
            session.messages,
            None,
        )
        assert target_tokens < estimated < trigger_tokens

        compactor._input_token_counter = lambda messages, tools: InputTokenCount(
            input_tokens=compactor_mod.estimate_request_tokens(messages, tools),
            source=UsageSource.LOCAL_ESTIMATE,
            confidence=UsageConfidence.ESTIMATED,
        )
        _messages, changed = compactor.maybe_compact(
            messages=session.messages,
            tool_list=None,
            main_model=session.client.model,
        )

        assert changed is True
    finally:
        session.close()

    checks = [
        event["payload"]
        for event in read_session_events(event_path)
        if event.get("type") == "compaction_check"
    ]
    uncertain = next(payload for payload in checks if payload["media_input_uncertain"] is True)
    assert uncertain["comparison_tokens"] < uncertain["trigger_tokens"]
    assert uncertain["effective_trigger_tokens"] == uncertain["target_tokens"]


def test_overflow_compaction_relaxes_recent_window_but_preserves_latest_turn(
    tmp_path: Path,
) -> None:
    cfg = _make_cfg()
    cfg.extra_fields["compaction"]["recent_user_turns_to_keep"] = 8  # type: ignore[index]
    cfg.extra_fields["compaction"]["importance_enabled"] = False  # type: ignore[index]
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="x",
        session_log_dir_override=tmp_path / "logs",
        session_id_override="compaction-hard-pressure",
    )
    counted_requests: list[str] = []
    try:
        assert session.conversation_compactor is not None

        def exact_counter(
            messages: list[dict[str, Any]],
            _tools: list[dict[str, Any]] | None,
        ) -> InputTokenCount:
            counted_requests.append(json.dumps(messages, ensure_ascii=False))
            return InputTokenCount(input_tokens=100)

        session.conversation_compactor._input_token_counter = exact_counter
        session.conversation_compactor.compactor_client = _FakeCompactorClient(  # type: ignore[assignment]
            [_summary(decisions=["preserved"])]
        )
        oldest = "oldest overflow turn " + ("o" * 3000)
        latest = "latest active turn " + ("l" * 1000)
        session.messages.extend(
            [
                {"role": "user", "content": oldest},
                {"role": "assistant", "content": "old response"},
                {"role": "user", "content": latest},
                {"role": "assistant", "content": "latest response"},
            ]
        )

        compacted, changed = session.conversation_compactor.compact_for_overflow(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
        )

        serialized = json.dumps(compacted, ensure_ascii=False)
        assert changed is True
        assert oldest not in serialized
        assert latest in serialized
        assert len(counted_requests) >= 2
        assert oldest in counted_requests[0]
        assert oldest not in counted_requests[-1]
    finally:
        session.close()


def test_compaction_is_incremental_merges_existing_summary(tmp_path: Path) -> None:
    cfg = _make_cfg()
    cfg.extra_fields["compaction"]["recent_user_turns_to_keep"] = 1  # type: ignore[index]

    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=True,
        api_key_override="x",
        session_log_dir_override=tmp_path / "session-artifacts",
        session_id_override="compaction-incremental",
    )
    try:
        assert session.conversation_compactor is not None
        fake_compactor = _FakeCompactorClient(
            [
                _summary(decisions=["d1"]),
                _summary(decisions=["d1", "d2"]),
            ]
        )
        session.conversation_compactor.compactor_client = fake_compactor  # type: ignore[assignment]

        _append_turns(session, count=5, prefix="phase1")
        compacted_messages, changed = session.conversation_compactor.compact_now(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            focus="phase1",
        )
        assert changed is True
        session.messages = compacted_messages

        _append_turns(session, count=4, prefix="phase2")
        compacted_messages_2, changed_2 = session.conversation_compactor.compact_now(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            focus="phase2",
        )
        assert changed_2 is True
        session.messages = compacted_messages_2

        assert len(fake_compactor.captured_payloads) >= 2
        second_payload = fake_compactor.captured_payloads[1]
        existing_summary = second_payload.get("existing_summary")
        assert isinstance(existing_summary, dict)
        assert "d1" in existing_summary.get("decisions", [])
        assert session.conversation_compactor.state.summary.get("decisions") == ["d1", "d2"]
    finally:
        session.close()


def test_execution_profile_compacts_single_instruction_run_and_preserves_instruction(
    tmp_path: Path,
) -> None:
    cfg = _make_cfg()
    cfg.extra_fields["compaction"]["importance_enabled"] = False  # type: ignore[index]
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=True,
        api_key_override="x",
        session_log_dir_override=tmp_path / "session-artifacts",
        session_id_override="compaction-execution",
        compaction_profile="execution",
    )
    try:
        assert session.conversation_compactor is not None
        assert session.conversation_compactor.profile_name == "execution"
        session.conversation_compactor.compactor_client = _FakeCompactorClient(  # type: ignore[assignment]
            [_summary(decisions=["keep execution brief pinned"])]
        )

        execution_instruction = (
            "Implement the task. Keep write scope under src/. Run pytest -q before finishing. "
            + ("I" * 4000)
        )
        session.messages.append({"role": "user", "content": execution_instruction})
        for idx in range(8):
            session.messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"id": f"tc-{idx}", "name": "fs_read", "arguments": {}}],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": f"tc-{idx}",
                        "content": "tool output " + ("x" * 1200),
                    },
                    {"role": "assistant", "content": f"step {idx} complete " + ("y" * 1200)},
                ]
            )

        compacted, changed = session.conversation_compactor.compact_now(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            focus="execution-run",
        )

        assert changed is True
        assert any(msg.get("content") == execution_instruction for msg in compacted)
        history_files = sorted((_compaction_session_dir(session) / "history").glob("chunk_*.jsonl"))
        assert history_files
        archived = "\n".join(path.read_text(encoding="utf-8") for path in history_files)
        assert "step 0 complete" in archived
        assert execution_instruction not in archived
    finally:
        session.close()


def test_execution_compaction_skips_partial_multi_tool_exchange(tmp_path: Path) -> None:
    cfg = _make_cfg()
    cfg.extra_fields["compaction"]["importance_enabled"] = False  # type: ignore[index]
    cfg.extra_fields["compaction"]["max_chunk_messages"] = 8  # type: ignore[index]
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=True,
        api_key_override="x",
        session_log_dir_override=tmp_path / "session-artifacts",
        session_id_override="compaction-execution-multi-tool-skip",
        compaction_profile="execution",
    )
    try:
        assert session.conversation_compactor is not None
        session.conversation_compactor.compactor_client = _FakeCompactorClient(  # type: ignore[assignment]
            [_summary(decisions=["keep tool exchange atomic"])]
        )

        session.messages.append({"role": "user", "content": "Implement the task safely."})
        session.messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "running reads",
                    "tool_calls": [
                        {"id": "tc-a", "name": "fs_read", "arguments": {}},
                        {"id": "tc-b", "name": "search_rg", "arguments": {}},
                    ],
                },
                {"role": "tool", "tool_call_id": "tc-a", "content": "read result"},
                {"role": "tool", "tool_call_id": "tc-b", "content": "search result"},
                {"role": "assistant", "content": "combined the tool results"},
            ]
        )
        for idx in range(6):
            session.messages.append({"role": "assistant", "content": f"recent step {idx}"})

        compacted, changed = session.conversation_compactor.compact_now(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            focus="execution-tail-skip",
        )

        assert changed is False
        assert compacted == session.messages
        _assert_valid_tool_transcript(compacted)
        history_dir = _compaction_session_dir(session) / "history"
        assert not list(history_dir.glob("chunk_*.jsonl"))
    finally:
        session.close()


def test_execution_compaction_tail_boundary_keeps_multi_tool_bundle_intact(tmp_path: Path) -> None:
    cfg = _make_cfg()
    cfg.extra_fields["compaction"]["importance_enabled"] = False  # type: ignore[index]
    cfg.extra_fields["compaction"]["max_chunk_messages"] = 8  # type: ignore[index]
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=True,
        api_key_override="x",
        session_log_dir_override=tmp_path / "session-artifacts",
        session_id_override="compaction-execution-tail-boundary",
        compaction_profile="execution",
    )
    try:
        assert session.conversation_compactor is not None
        session.conversation_compactor.compactor_client = _FakeCompactorClient(  # type: ignore[assignment]
            [_summary(decisions=["keep tool exchange intact"])]
        )

        session.messages.append({"role": "user", "content": "Implement the task safely."})
        older_note_a = "older removable note a " + ("o" * 2600)
        older_note_b = "older removable note b " + ("p" * 2600)
        session.messages.append({"role": "assistant", "content": older_note_a})
        session.messages.append({"role": "assistant", "content": older_note_b})
        session.messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "running reads",
                    "tool_calls": [
                        {"id": "tc-a", "name": "fs_read", "arguments": {}},
                        {"id": "tc-b", "name": "search_rg", "arguments": {}},
                    ],
                },
                {"role": "tool", "tool_call_id": "tc-a", "content": "read result"},
                {"role": "tool", "tool_call_id": "tc-b", "content": "search result"},
                {"role": "assistant", "content": "combined the tool results"},
            ]
        )
        for idx in range(6):
            session.messages.append({"role": "assistant", "content": f"recent step {idx}"})

        compacted, changed = session.conversation_compactor.compact_now(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            focus="execution-tail-boundary",
        )

        assert changed is True
        _assert_valid_tool_transcript(compacted)
        serialized = json.dumps(compacted, ensure_ascii=False)
        assert older_note_a not in serialized
        assert older_note_b not in serialized
        assert "combined the tool results" in serialized
        assert '"tool_call_id": "tc-a"' in serialized
        assert '"tool_call_id": "tc-b"' in serialized

        history_dir = _compaction_session_dir(session) / "history"
        history_files = sorted(history_dir.glob("chunk_*.jsonl"))
        assert history_files
        archived = "\n".join(path.read_text(encoding="utf-8") for path in history_files)
        assert older_note_a in archived
        assert older_note_b in archived
        assert "combined the tool results" not in archived
        assert '"tool_call_id": "tc-a"' not in archived
        assert '"tool_call_id": "tc-b"' not in archived
    finally:
        session.close()


def test_execution_compaction_leaves_active_tool_messages_valid(tmp_path: Path) -> None:
    cfg = _make_cfg()
    cfg.extra_fields["compaction"]["importance_enabled"] = False  # type: ignore[index]
    cfg.extra_fields["compaction"]["max_chunk_messages"] = 8  # type: ignore[index]
    cfg.extra_fields["compaction"]["execution_min_removable_tokens"] = 1  # type: ignore[index]
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=True,
        api_key_override="x",
        session_log_dir_override=tmp_path / "session-artifacts",
        session_id_override="compaction-execution-validity",
        compaction_profile="execution",
    )
    try:
        assert session.conversation_compactor is not None
        session.conversation_compactor.compactor_client = _FakeCompactorClient(  # type: ignore[assignment]
            [_summary(decisions=["preserve transcript validity"])]
        )

        session.messages.append({"role": "user", "content": "Implement the task safely."})
        session.messages.append({"role": "assistant", "content": "older removable note"})
        session.messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "run first tools",
                    "tool_calls": [
                        {"id": "tc-a1", "name": "fs_read", "arguments": {}},
                        {"id": "tc-a2", "name": "search_rg", "arguments": {}},
                    ],
                },
                {"role": "tool", "tool_call_id": "tc-a1", "content": "read result"},
                {"role": "tool", "tool_call_id": "tc-a2", "content": "search result"},
                {"role": "assistant", "content": "handled first tool bundle"},
                {
                    "role": "assistant",
                    "content": "run second tools",
                    "tool_calls": [{"id": "tc-b1", "name": "fs_read", "arguments": {}}],
                },
                {"role": "tool", "tool_call_id": "tc-b1", "content": "second read result"},
                {"role": "assistant", "content": "handled second tool bundle"},
            ]
        )
        for idx in range(6):
            session.messages.append({"role": "assistant", "content": f"recent step {idx}"})

        compacted, changed = session.conversation_compactor.compact_now(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            focus="execution-validity",
        )

        assert changed is True
        _assert_valid_tool_transcript(compacted)
        serialized = json.dumps(compacted, ensure_ascii=False)
        assert "handled second tool bundle" in serialized
        assert '"tool_call_id": "tc-b1"' in serialized

        history_dir = _compaction_session_dir(session) / "history"
        history_files = sorted(history_dir.glob("chunk_*.jsonl"))
        assert history_files
        archived = "\n".join(path.read_text(encoding="utf-8") for path in history_files)
        assert "handled first tool bundle" in archived
        assert '"tool_call_id": "tc-a1"' in archived
        assert '"tool_call_id": "tc-a2"' in archived
    finally:
        session.close()


def test_execution_compaction_tiny_candidate_no_progress_is_skipped(tmp_path: Path) -> None:
    cfg = _make_cfg()
    cfg.extra_fields["compaction"]["importance_enabled"] = True  # type: ignore[index]
    cfg.extra_fields["compaction"]["importance_strategy"] = "lowest_density"  # type: ignore[index]
    cfg.extra_fields["compaction"]["max_chunk_messages"] = 8  # type: ignore[index]
    cfg.extra_fields["compaction"]["execution_min_removable_messages"] = 1  # type: ignore[index]
    cfg.extra_fields["compaction"]["execution_min_removable_tokens"] = 1  # type: ignore[index]
    huge_summary = _summary(decisions=["S" * 12000])
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=True,
        api_key_override="x",
        session_log_dir_override=tmp_path / "session-artifacts",
        session_id_override="compaction-execution-no-progress",
        compaction_profile="execution",
    )
    try:
        assert session.conversation_compactor is not None
        session.conversation_compactor.compactor_client = _FakeCompactorClient(  # type: ignore[assignment]
            [huge_summary]
        )

        session.messages.append({"role": "user", "content": "Implement the task safely."})
        session.messages.append({"role": "assistant", "content": "tiny old note"})
        for idx in range(8):
            session.messages.append({"role": "assistant", "content": f"recent step {idx}"})
        before_messages = json.loads(json.dumps(session.messages))

        compacted, changed = session.conversation_compactor.compact_now(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            focus="execution-no-progress",
        )

        assert changed is False
        assert compacted == before_messages
        history_dir = _compaction_session_dir(session) / "history"
        assert not list(history_dir.glob("chunk_*.jsonl"))
    finally:
        session.close()


def test_execution_compaction_retries_larger_safe_window_after_no_progress_preview(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _make_cfg()
    cfg.extra_fields["compaction"]["importance_enabled"] = True  # type: ignore[index]
    cfg.extra_fields["compaction"]["importance_strategy"] = "lowest_density"  # type: ignore[index]
    cfg.extra_fields["compaction"]["max_chunk_messages"] = 8  # type: ignore[index]
    cfg.extra_fields["compaction"]["execution_min_removable_messages"] = 1  # type: ignore[index]
    cfg.extra_fields["compaction"]["execution_min_removable_tokens"] = 1  # type: ignore[index]
    retry_summary = _summary(decisions=["R" * 3200])
    fake_compactor = _FakeCompactorClient([retry_summary, retry_summary])

    def _deterministic_request_tokens(
        messages: list[dict[str, Any]],
        tool_list: list[dict[str, Any]] | None,
    ) -> int:
        _ = tool_list
        total = 0
        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                text = json.dumps(content, ensure_ascii=False, sort_keys=True)
            elif content is None:
                text = ""
            else:
                text = str(content)
            total += len(text)
            if isinstance(content, str) and content.startswith(MEMORY_MARKER):
                total += 2500
            elif isinstance(content, str) and content.startswith(PINS_MARKER):
                total += 250
        return total

    monkeypatch.setattr(compactor_mod, "estimate_request_tokens", _deterministic_request_tokens)

    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=False,
        api_key_override="x",
        session_log_dir_override=tmp_path / "session-artifacts",
        session_id_override="compaction-execution-retry-larger-window",
        compaction_profile="execution",
    )
    try:
        assert session.conversation_compactor is not None
        session.conversation_compactor.compactor_client = fake_compactor  # type: ignore[assignment]

        session.messages.append({"role": "user", "content": "Implement the task safely."})
        older_note_a = "older removable note a " + ("a" * 5000)
        older_note_b = "older removable note b " + ("b" * 5000)
        session.messages.append({"role": "assistant", "content": older_note_a})
        session.messages.append({"role": "assistant", "content": older_note_b})
        for idx in range(8):
            session.messages.append({"role": "assistant", "content": f"recent step {idx}"})

        compacted, changed = session.conversation_compactor.compact_now(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            focus="execution-retry-larger-window",
        )

        assert changed is True
        assert fake_compactor.calls == 2
        serialized = json.dumps(compacted, ensure_ascii=False)
        assert older_note_a not in serialized
        assert older_note_b not in serialized

        history_dir = _compaction_session_dir(session) / "history"
        history_files = sorted(history_dir.glob("chunk_*.jsonl"))
        assert history_files
        archived = "\n".join(path.read_text(encoding="utf-8") for path in history_files)
        assert older_note_a in archived
        assert older_note_b in archived

        events = [
            json.loads(line)
            for line in session.store.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        no_progress_events = [
            event
            for event in events
            if event.get("type") == "compaction_warning"
            and event.get("payload", {}).get("warning") == "compaction_no_progress"
        ]
        assert no_progress_events
        assert any(
            event.get("payload", {}).get("chunk_strategy") == "execution_lowest_density"
            for event in no_progress_events
        )
    finally:
        session.close()


def test_execution_compaction_no_progress_rolls_back_state_and_artifacts(tmp_path: Path) -> None:
    cfg = _make_cfg()
    cfg.extra_fields["compaction"]["importance_enabled"] = True  # type: ignore[index]
    cfg.extra_fields["compaction"]["importance_strategy"] = "lowest_density"  # type: ignore[index]
    cfg.extra_fields["compaction"]["max_chunk_messages"] = 8  # type: ignore[index]
    cfg.extra_fields["compaction"]["execution_min_removable_messages"] = 1  # type: ignore[index]
    cfg.extra_fields["compaction"]["execution_min_removable_tokens"] = 1  # type: ignore[index]
    huge_summary = _summary(decisions=["R" * 12000])
    existing_summary = _summary(decisions=["keep-existing-summary"])
    existing_pin = {
        "kind": "context",
        "text": "keep existing pin",
        "reasons": ["seed"],
        "source": {},
    }
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=True,
        api_key_override="x",
        session_log_dir_override=tmp_path / "session-artifacts",
        session_id_override="compaction-execution-rollback",
        compaction_profile="execution",
    )
    try:
        assert session.conversation_compactor is not None
        session.conversation_compactor.compactor_client = _FakeCompactorClient(  # type: ignore[assignment]
            [huge_summary]
        )
        session.conversation_compactor.state.summary = existing_summary
        session.conversation_compactor.state.pins = [existing_pin]

        session.messages.append({"role": "user", "content": "Implement the task safely."})
        session.messages.append({"role": "assistant", "content": "tiny old note"})
        for idx in range(8):
            session.messages.append({"role": "assistant", "content": f"recent step {idx}"})
        before_messages = json.loads(json.dumps(session.messages))

        compacted, changed = session.conversation_compactor.compact_now(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            focus="execution-rollback",
        )

        assert changed is False
        assert compacted == before_messages
        assert session.conversation_compactor.state.summary == existing_summary
        assert session.conversation_compactor.state.pins == [existing_pin]
        assert session.conversation_compactor.state.history_chunk_index == 0
        assert session.conversation_compactor.state.memory_message_index is None
        assert session.conversation_compactor.state.pins_message_index is None
        history_dir = _compaction_session_dir(session) / "history"
        assert not list(history_dir.glob("chunk_*.jsonl"))
        summary_path = _compaction_session_dir(session) / "memory" / "summary.json"
        pins_path = _compaction_session_dir(session) / "memory" / "pins.json"
        assert not summary_path.exists()
        assert not pins_path.exists()
    finally:
        session.close()


def test_execution_compaction_artifact_publish_failure_rolls_back(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _make_cfg()
    cfg.extra_fields["compaction"]["importance_enabled"] = False  # type: ignore[index]
    cfg.extra_fields["compaction"]["max_chunk_messages"] = 8  # type: ignore[index]
    existing_summary = _summary(decisions=["keep-existing-summary"])
    existing_pin = {
        "kind": "context",
        "text": "keep existing pin",
        "reasons": ["seed"],
        "source": {},
    }
    old_summary_text = json.dumps(existing_summary, ensure_ascii=False, indent=2) + "\n"
    old_pins_text = (
        json.dumps(
            {
                "pins": [
                    {
                        "kind": existing_pin["kind"],
                        "text": existing_pin["text"],
                        "reasons": existing_pin["reasons"],
                        "source": existing_pin["source"],
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=True,
        api_key_override="x",
        session_log_dir_override=tmp_path / "session-artifacts",
        session_id_override="compaction-execution-artifact-rollback",
        compaction_profile="execution",
    )
    try:
        assert session.conversation_compactor is not None
        session.conversation_compactor.compactor_client = _FakeCompactorClient(  # type: ignore[assignment]
            [_summary(decisions=["updated-summary"])]
        )
        session.conversation_compactor.state.summary = existing_summary
        session.conversation_compactor.state.pins = [existing_pin]

        summary_path = _compaction_session_dir(session) / "memory" / "summary.json"
        pins_path = _compaction_session_dir(session) / "memory" / "pins.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(old_summary_text, encoding="utf-8")
        pins_path.write_text(old_pins_text, encoding="utf-8")

        original_publish = session.conversation_compactor._publish_staged_artifact

        def fail_summary_publish(*, staged_path: Path, artifact_path: Path, warning: str) -> bool:
            if artifact_path == summary_path:
                return False
            return original_publish(
                staged_path=staged_path,
                artifact_path=artifact_path,
                warning=warning,
            )

        monkeypatch.setattr(
            session.conversation_compactor,
            "_publish_staged_artifact",
            fail_summary_publish,
        )

        session.messages.append({"role": "user", "content": "Implement the task safely."})
        session.messages.append({"role": "assistant", "content": "old note a " + ("a" * 2600)})
        session.messages.append({"role": "assistant", "content": "old note b " + ("b" * 2600)})
        for idx in range(8):
            session.messages.append({"role": "assistant", "content": f"recent step {idx}"})
        before_messages = json.loads(json.dumps(session.messages))

        compacted, changed = session.conversation_compactor.compact_now(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            focus="execution-artifact-rollback",
        )

        assert changed is False
        assert compacted == before_messages
        assert session.conversation_compactor.state.history_chunk_index == 0
        assert session.conversation_compactor.state.summary == existing_summary
        assert session.conversation_compactor.state.pins == [existing_pin]
        history_dir = _compaction_session_dir(session) / "history"
        assert not list(history_dir.glob("chunk_*.jsonl"))
        assert summary_path.read_text(encoding="utf-8") == old_summary_text
        assert pins_path.read_text(encoding="utf-8") == old_pins_text
    finally:
        session.close()


def test_execution_compaction_enforces_minimum_removal_thresholds(tmp_path: Path) -> None:
    cfg = _make_cfg()
    cfg.extra_fields["compaction"]["importance_enabled"] = True  # type: ignore[index]
    cfg.extra_fields["compaction"]["importance_strategy"] = "lowest_density"  # type: ignore[index]
    cfg.extra_fields["compaction"]["max_chunk_messages"] = 8  # type: ignore[index]
    cfg.extra_fields["compaction"]["execution_min_removable_messages"] = 3  # type: ignore[index]
    cfg.extra_fields["compaction"]["execution_min_removable_tokens"] = 1500  # type: ignore[index]
    fake_compactor = _FakeCompactorClient([_summary(decisions=["unused"])])
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=True,
        api_key_override="x",
        session_log_dir_override=tmp_path / "session-artifacts",
        session_id_override="compaction-execution-thresholds",
        compaction_profile="execution",
    )
    try:
        assert session.conversation_compactor is not None
        session.conversation_compactor.compactor_client = fake_compactor  # type: ignore[assignment]

        session.messages.append({"role": "user", "content": "Implement the task safely."})
        session.messages.append({"role": "assistant", "content": "old tiny note a"})
        session.messages.append({"role": "assistant", "content": "old tiny note b"})
        for idx in range(8):
            session.messages.append({"role": "assistant", "content": f"recent step {idx}"})
        before_messages = json.loads(json.dumps(session.messages))

        compacted, changed = session.conversation_compactor.compact_now(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            focus="execution-thresholds",
        )

        assert changed is False
        assert compacted == before_messages
        assert fake_compactor.calls == 0
        history_dir = _compaction_session_dir(session) / "history"
        assert not list(history_dir.glob("chunk_*.jsonl"))
    finally:
        session.close()


def test_compaction_prefers_low_importance_turn_first(tmp_path: Path) -> None:
    cfg = _make_cfg()
    cfg.extra_fields["compaction"]["recent_user_turns_to_keep"] = 1  # type: ignore[index]
    cfg.extra_fields["compaction"]["max_chunk_messages"] = 2  # type: ignore[index]
    cfg.extra_fields["compaction"]["importance_enabled"] = True  # type: ignore[index]
    cfg.extra_fields["compaction"]["importance_strategy"] = "lowest_density"  # type: ignore[index]

    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=True,
        api_key_override="x",
        session_log_dir_override=tmp_path / "session-artifacts",
        session_id_override="compaction-importance",
    )
    try:
        assert session.conversation_compactor is not None
        session.conversation_compactor.compactor_client = _FakeCompactorClient(  # type: ignore[assignment]
            [_summary(decisions=["d1"])]
        )

        high_signal = "MUST keep API stable. Acceptance criteria: pytest -q passes. " + ("H" * 1400)
        low_signal = "ok thanks " + ("L" * 1400)
        recent_signal = "recent turn " + ("R" * 1400)
        session.messages.extend(
            [
                {"role": "user", "content": high_signal},
                {"role": "assistant", "content": "Will do"},
                {"role": "user", "content": low_signal},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": recent_signal},
                {"role": "assistant", "content": "recent response"},
            ]
        )

        compacted, changed = session.conversation_compactor.compact_now(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            focus="importance-test",
        )
        assert changed is True
        first_chunk = _compaction_session_dir(session) / "history" / "chunk_0001.jsonl"
        assert first_chunk.exists()
        chunk_payload = first_chunk.read_text(encoding="utf-8")
        assert low_signal in chunk_payload
        assert high_signal not in chunk_payload

        serialized = json.dumps(compacted, ensure_ascii=False)
        assert recent_signal in serialized
    finally:
        session.close()


def test_pins_preserved_when_high_signal_turn_compacted(tmp_path: Path) -> None:
    cfg = _make_cfg()
    cfg.extra_fields["compaction"]["recent_user_turns_to_keep"] = 1  # type: ignore[index]
    cfg.extra_fields["compaction"]["importance_enabled"] = False  # type: ignore[index]
    cfg.extra_fields["compaction"]["importance_strategy"] = "oldest"  # type: ignore[index]
    cfg.extra_fields["compaction"]["pin_score_threshold"] = 5.0  # type: ignore[index]

    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="x",
        session_log_dir_override=tmp_path / "logs",
        session_id_override="compaction-pins",
    )
    try:
        assert session.conversation_compactor is not None
        session.conversation_compactor.compactor_client = _FakeCompactorClient(  # type: ignore[assignment]
            [_summary(decisions=["d1"])]
        )

        high_signal = (
            "MUST and DO NOT break public API. Acceptance criteria: pytest -q passes. "
            "Constraint: keep existing command names."
        ) + ("Z" * 1800)
        recent_signal = "recent turn " + ("Q" * 1400)
        session.messages.extend(
            [
                {"role": "user", "content": high_signal},
                {"role": "assistant", "content": "ack"},
                {"role": "user", "content": recent_signal},
                {"role": "assistant", "content": "recent ack"},
            ]
        )

        compacted, changed = session.conversation_compactor.compact_now(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            focus="pins-test",
        )
        assert changed is True

        pins_messages = [
            msg
            for msg in compacted
            if str(msg.get("role") or "") == "user"
            and str(msg.get("content") or "").startswith(PINS_MARKER)
        ]
        assert pins_messages
        pins_payload = str(pins_messages[0]["content"]).split("\n", 1)[1]
        pins_json = json.loads(pins_payload)
        assert any(
            "MUST and DO NOT break public API" in pin.get("text", "") for pin in pins_json["pins"]
        )

        history_dir = _compaction_session_dir(session) / "history"
        assert list(history_dir.glob("chunk_*.jsonl"))
        pins_file = _compaction_session_dir(session) / "memory" / "pins.json"
        assert pins_file.exists()
        summary_path = _compaction_session_dir(session) / "memory" / "summary.json"
        assert summary_path.exists()
    finally:
        session.close()


def test_summary_normalization_unions_previous_items(tmp_path: Path) -> None:
    cfg = _make_cfg()
    cfg.extra_fields["compaction"]["recent_user_turns_to_keep"] = 1  # type: ignore[index]

    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=True,
        api_key_override="x",
        session_log_dir_override=tmp_path / "session-artifacts",
        session_id_override="compaction-summary-union",
    )
    try:
        assert session.conversation_compactor is not None
        fake_compactor = _FakeCompactorClient(
            [
                _summary(decisions=["d1", "d2"]),
                _summary(decisions=["d1"]),
            ]
        )
        session.conversation_compactor.compactor_client = fake_compactor  # type: ignore[assignment]
        _append_turns(session, count=4, prefix="union-a")
        compacted_a, changed_a = session.conversation_compactor.compact_now(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            focus="union-1",
        )
        assert changed_a is True
        session.messages = compacted_a

        _append_turns(session, count=4, prefix="union-b")
        compacted_b, changed_b = session.conversation_compactor.compact_now(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            focus="union-2",
        )
        assert changed_b is True
        session.messages = compacted_b

        decisions = session.conversation_compactor.state.summary.get("decisions", [])
        assert "d1" in decisions
        assert "d2" in decisions
    finally:
        session.close()


def test_compactor_failure_preserves_chunk_and_writes_no_partial_artifacts(tmp_path: Path) -> None:
    cfg = _make_cfg()
    cfg.extra_fields["compaction"]["recent_user_turns_to_keep"] = 1  # type: ignore[index]
    cfg.extra_fields["compaction"]["importance_enabled"] = False  # type: ignore[index]
    cfg.extra_fields["compaction"]["importance_strategy"] = "oldest"  # type: ignore[index]
    cfg.extra_fields["compaction"]["pin_score_threshold"] = 1.0  # type: ignore[index]
    cfg.extra_fields["compaction"]["trigger_ratio"] = 0.2  # type: ignore[index]
    cfg.extra_fields["compaction"]["target_ratio"] = 0.1  # type: ignore[index]

    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="x",
        session_log_dir_override=tmp_path / "logs",
        session_id_override="compaction-faildrop",
    )
    try:
        assert session.conversation_compactor is not None
        failing = _FailingCompactorClient(max_chars=200)
        session.conversation_compactor.compactor_client = failing  # type: ignore[assignment]

        high_signal = (
            "MUST preserve this requirement while handling failures. "
            "Acceptance criteria: no stalls."
        ) + ("X" * 2200)
        recent_signal = "recent turn " + ("R" * 1200)
        session.messages.extend(
            [
                {"role": "user", "content": high_signal},
                {"role": "assistant", "content": "ack"},
                {"role": "user", "content": recent_signal},
                {"role": "assistant", "content": "recent"},
            ]
        )

        compacted, changed = session.conversation_compactor.compact_now(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            focus="faildrop",
        )
        assert changed is False
        assert failing.calls >= 2

        history_dir = _compaction_session_dir(session) / "history"
        history_files = list(history_dir.glob("chunk_*.jsonl"))
        assert history_files == []

        serialized = json.dumps(compacted, ensure_ascii=False)
        assert high_signal in serialized
        assert not any(
            str(msg.get("content") or "").startswith((MEMORY_MARKER, PINS_MARKER))
            for msg in compacted
        )
    finally:
        session.close()


def test_compactor_restores_summary_pins_and_history_index_for_resume(tmp_path: Path) -> None:
    cfg = _make_cfg()
    cfg.extra_fields["compaction"]["recent_user_turns_to_keep"] = 1  # type: ignore[index]
    sessions_dir = tmp_path / "logs"
    session_id = "compaction-state-restore"
    first = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="x",
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
    )
    try:
        assert first.conversation_compactor is not None
        first.conversation_compactor.compactor_client = _FakeCompactorClient(  # type: ignore[assignment]
            [_summary(decisions=["restore this decision"])]
        )
        _append_turns(first, count=4, prefix="restore")
        compacted, changed = first.conversation_compactor.compact_now(
            messages=first.messages,
            tool_list=first.tool_list,
            main_model=first.client.model,
        )
        first.messages = compacted
        assert changed is True
        expected_index = first.conversation_compactor.state.history_chunk_index
        expected_summary = first.conversation_compactor.state.summary
        expected_pins = first.conversation_compactor.state.pins
        assert expected_index > 0
    finally:
        first.close()

    resumed = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="x",
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
        session_source="resume",
    )
    try:
        assert resumed.conversation_compactor is not None
        assert resumed.conversation_compactor.state.history_chunk_index == expected_index
        assert resumed.conversation_compactor.state.summary == expected_summary
        assert resumed.conversation_compactor.state.pins == expected_pins
    finally:
        resumed.close()


def test_reinject_context_messages_restores_memory_and_pins_after_resume(
    tmp_path: Path,
) -> None:
    cfg = _make_cfg()
    sessions_dir = tmp_path / "logs"
    session_id = "compaction-resume-reinject"
    seed = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="x",
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
    )
    try:
        memory_dir = _compaction_session_dir(seed) / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "summary.json").write_text(
            json.dumps(_summary(decisions=["resume-restored-decision"])),
            encoding="utf-8",
        )
        (memory_dir / "pins.json").write_text(
            json.dumps(
                {
                    "pins": [
                        {
                            "kind": "context",
                            "text": "resume-restored pin",
                            "reasons": ["seed"],
                            "source": {},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
    finally:
        seed.close()

    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="x",
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
        session_source="resume",
    )
    try:
        compactor = session.conversation_compactor
        assert compactor is not None
        assert compactor.state.summary["decisions"] == ["resume-restored-decision"]
        assert len(compactor.state.pins) == 1
        assert compactor.state.memory_message_index is None
        assert compactor.state.pins_message_index is None

        session.messages.append({"role": "user", "content": "resumed history turn"})
        session.messages[:] = compactor.reinject_context_messages(session.messages)

        memory_index = compactor.state.memory_message_index
        pins_index = compactor.state.pins_message_index
        assert memory_index is not None
        assert pins_index is not None
        assert pins_index >= compactor.state.pinned_prefix_len
        assert memory_index == pins_index + 1
        memory_content = str(session.messages[memory_index]["content"])
        pins_content = str(session.messages[pins_index]["content"])
        assert memory_content.startswith(MEMORY_MARKER)
        assert "resume-restored-decision" in memory_content
        assert pins_content.startswith(PINS_MARKER)
        assert "resume-restored pin" in pins_content
        assert session.messages[-1] == {"role": "user", "content": "resumed history turn"}

        # Reinjecting again must not duplicate the marker messages.
        session.messages[:] = compactor.reinject_context_messages(session.messages)
        memory_count = sum(
            1 for msg in session.messages if str(msg.get("content") or "").startswith(MEMORY_MARKER)
        )
        pins_count = sum(
            1 for msg in session.messages if str(msg.get("content") or "").startswith(PINS_MARKER)
        )
        assert memory_count == 1
        assert pins_count == 1
    finally:
        session.close()

    events = list(read_session_events(sessions_dir / f"{session_id}.jsonl"))
    reinjected_events = [
        event
        for event in events
        if isinstance(event, dict)
        and str(event.get("type") or "") == "compaction_context_reinjected"
    ]
    assert len(reinjected_events) == 2
    payload = reinjected_events[-1].get("payload")
    assert isinstance(payload, dict)
    assert payload["summary_restored"] is True
    assert payload["pins_count"] == 1
    assert isinstance(payload["memory_message_index"], int)
    assert isinstance(payload["pins_message_index"], int)


def test_reinject_context_messages_without_restored_state_is_a_no_op(
    tmp_path: Path,
) -> None:
    cfg = _make_cfg()
    sessions_dir = tmp_path / "logs"
    session_id = "compaction-resume-reinject-empty"
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="x",
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
        session_source="resume",
    )
    try:
        compactor = session.conversation_compactor
        assert compactor is not None
        assert compactor.state.summary == {}
        assert compactor.state.pins == []

        before = list(session.messages)
        result = compactor.reinject_context_messages(session.messages)

        assert result is session.messages
        assert session.messages == before
        assert compactor.state.memory_message_index is None
        assert compactor.state.pins_message_index is None
        assert not any(
            str(msg.get("content") or "").startswith((MEMORY_MARKER, PINS_MARKER))
            for msg in session.messages
        )
    finally:
        session.close()

    events = list(read_session_events(sessions_dir / f"{session_id}.jsonl"))
    assert not any(
        isinstance(event, dict) and str(event.get("type") or "") == "compaction_context_reinjected"
        for event in events
    )
