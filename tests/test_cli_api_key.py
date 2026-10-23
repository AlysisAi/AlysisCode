from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from alysis_code import agent_loop as agent_loop_mod
from alysis_code import cli as cli_mod
from alysis_code.agent.turn_contract import (
    TurnEffect,
    TurnOutcome,
    TurnRelation,
    TurnSemantics,
)
from alysis_code.agent_loop import SYSTEM_PROMPT, create_session
from alysis_code.cancellation import InteractiveCancellationToken
from alysis_code.cli import app as alysis_app
from alysis_code.config import AppConfig, ConfigError
from alysis_code.interactive_input_guard import interactive_prompt_guard
from alysis_code.llm.openai_compat import LLMError
from alysis_code.model_registry import ModelMeta
from alysis_code.request_estimation import estimate_request_token_breakdown
from alysis_code.run_outcome import (
    AGENT_FAILURE_EXIT_CODE,
    INFRASTRUCTURE_FAILURE_EXIT_CODE,
)
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.session_store import read_session_events
from alysis_code.token_budget import compute_input_budget
from alysis_code.verify_gate import VerifyError


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", os.fspath(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _normalize_terminal_output(text: str) -> str:
    no_ansi = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    return " ".join(no_ansi.split())


def _init_git_repo_with_commit(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("repo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _workspace_turn_semantics(
    *,
    outcome: TurnOutcome = TurnOutcome.CHANGE,
    relation: TurnRelation = TurnRelation.NEW,
) -> TurnSemantics:
    effects = (
        (TurnEffect.READ_WORKSPACE, TurnEffect.WRITE_WORKSPACE)
        if outcome is TurnOutcome.CHANGE
        else (TurnEffect.READ_WORKSPACE,)
        if outcome in {TurnOutcome.INSPECT, TurnOutcome.REVIEW, TurnOutcome.PLAN}
        else ()
    )
    return TurnSemantics(outcome=outcome, relation=relation, requested_effects=effects)


def test_create_session_uses_api_key_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    cfg = AppConfig(model="test-model")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="readonly",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    try:
        assert session.client.api_key == "override-key"
    finally:
        session.close()


def test_system_prompt_reinforces_narrow_repo_execution_changes() -> None:
    # The old wording ("unmatched or unknown cases") was orphaned jargon: the sibling
    # bullets that glossed it were dropped in a compression pass, leaving the term
    # undefined in the shipped prompt. It now says what it means in place.
    assert (
        "Preserve existing output/API/file shape, and leave input cases the request did not "
        "name behaving exactly as they do today, unless a broader change is clearly required."
        in SYSTEM_PROMPT
    )
    assert "Keep diffs minimal and reviewable." in SYSTEM_PROMPT


def test_create_session_prefers_repo_inferred_verify_commands_for_normal_chat_js_repo(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        '{"name":"demo","scripts":{"test":"vitest run"}}\n',
        encoding="utf-8",
    )

    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=repo,
        mode="review",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    try:
        assert session.effective_verification_commands == ["npm test"]
        assert session.verification_selection_source == "repo_scan.likely_test_commands"
        assert session.verification_contract_type == "selected"
        assert session.verification_authoritative is False
    finally:
        session.close()


def test_create_session_keeps_generic_verify_fallback_when_repo_scan_has_no_inference(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("demo\n", encoding="utf-8")

    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=repo,
        mode="review",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    try:
        assert session.effective_verification_commands == ["pytest -q"]
        assert session.verification_selection_source == "config.verify_commands_fallback"
        assert session.verification_contract_type == "generic_fallback"
        assert session.verification_authoritative is False
    finally:
        session.close()


@pytest.mark.parametrize(
    ("verify_commands", "authoritative_commands", "expected"),
    [
        (["make verify"], None, ["make verify"]),
        (None, ["./scripts/verify.sh"], ["./scripts/verify.sh"]),
    ],
)
def test_create_session_keeps_higher_priority_verify_commands_than_repo_inference(
    tmp_path: Path,
    monkeypatch,
    verify_commands: list[str] | None,
    authoritative_commands: list[str] | None,
    expected: list[str],
) -> None:
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        '{"name":"demo","scripts":{"test":"vitest run"}}\n',
        encoding="utf-8",
    )

    cfg = AppConfig(model="test-model")
    if verify_commands is not None:
        cfg.verify_commands = list(verify_commands)

    session = create_session(
        cfg=cfg,
        root=repo,
        mode="review",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        authoritative_verification_commands=authoritative_commands,
    )
    try:
        assert session.effective_verification_commands == expected
    finally:
        session.close()


def test_create_session_skips_repo_scan_when_normal_chat_does_not_need_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        agent_loop_mod,
        "scan_workspace",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("scan_workspace should not run")),
    )

    session = create_session(
        cfg=AppConfig(model="test-model", verify_commands=["make verify"]),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    try:
        assert session.effective_verification_commands == ["make verify"]
        assert session.planner_workspace_context is None
    finally:
        session.close()


@pytest.mark.parametrize("resolved_timeout_s", [44.0, 7.0])
def test_create_session_applies_timeout_to_main_and_compactor_only(
    tmp_path: Path,
    monkeypatch,
    resolved_timeout_s: float,
) -> None:
    # Automatic skill choice adds no separate client or timeout policy.
    captured: list[dict[str, Any]] = []
    monkeypatch.setenv("ALYSIS_LLM_TIMEOUT_S", str(resolved_timeout_s))

    class FakeClient:
        supports_tool_calling = False

        def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            self.api_key = kwargs["api_key"]
            self.model = kwargs["model"]
            self.temperature = kwargs["temperature"]
            self.timeout_s = kwargs["timeout_s"]
            self.stream_no_progress_timeout_s = 240.0
            captured.append(dict(kwargs))

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("chat should not run during create_session timeout wiring test")

    monkeypatch.setattr(agent_loop_mod, "OpenAICompatClient", FakeClient)

    session = create_session(
        cfg=AppConfig(model="test-model", llm_timeout_s=17.5),
        root=tmp_path,
        mode="readonly",
        yes=False,
        max_steps=1,
        no_log=False,
        api_key_override="override-key",
        enable_conversation_summarization=True,
        session_log_dir_override=tmp_path / "sessions",
    )
    try:
        assert len(captured) == 2
        assert {item["timeout_s"] for item in captured} == {resolved_timeout_s}
        assert session.router_client is None
    finally:
        session.close()


def test_create_session_skips_startup_git_status_when_surface_hides_status_line(
    tmp_path: Path, monkeypatch
) -> None:
    class _HiddenStatusSurface:
        _show_status_line = False

        def __init__(self) -> None:
            self.status = None

        def on_status_update(self, status) -> None:  # type: ignore[no-untyped-def]
            self.status = status

    monkeypatch.setattr(
        agent_loop_mod,
        "_git_branch",
        lambda _root: (_ for _ in ()).throw(AssertionError("_git_branch should not run")),
    )
    monkeypatch.setattr(
        agent_loop_mod,
        "_git_is_dirty",
        lambda _root: (_ for _ in ()).throw(AssertionError("_git_is_dirty should not run")),
    )

    surface = _HiddenStatusSurface()
    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=tmp_path,
        mode="readonly",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        surface=surface,  # type: ignore[arg-type]
    )
    try:
        assert surface.status is not None
        assert surface.status.branch == "-"
        assert surface.status.dirty is False
    finally:
        session.close()


def test_generate_session_summary_uses_resolved_llm_timeout(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            captured["timeout_s"] = kwargs["timeout_s"]
            captured["model"] = kwargs["model"]

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            return SimpleNamespace(content="Parser retry planning summary")

    monkeypatch.setattr("alysis_code.llm.openai_compat.OpenAICompatClient", FakeClient)
    session = SimpleNamespace(
        cfg=AppConfig(model="test-model", llm_timeout_s=13.0),
        client=SimpleNamespace(api_key="k", model="test-model"),
    )

    title = cli_mod._generate_session_summary_with_model(
        session=session,
        transcript_messages=[
            {"role": "user", "content": "Please fix the retry bug"},
            {"role": "assistant", "content": "I will inspect the parser flow."},
        ],
    )

    assert title == "Parser retry planning summary"
    assert captured["timeout_s"] == 13.0
    assert captured["model"] == "test-model"


def test_generate_session_summary_strict_model_metadata_policy_skips_llm_call(
    monkeypatch,
) -> None:
    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            raise AssertionError("OpenAICompatClient should not be constructed in strict mode")

    monkeypatch.setattr("alysis_code.llm.openai_compat.OpenAICompatClient", FakeClient)
    session = SimpleNamespace(
        cfg=AppConfig(model="unknown-model-xyz", model_metadata_policy="strict"),
        client=SimpleNamespace(api_key="k", model="unknown-model-xyz"),
    )

    title = cli_mod._generate_session_summary_with_model(
        session=session,
        transcript_messages=[
            {"role": "user", "content": "Please fix the retry bug"},
            {"role": "assistant", "content": "I will inspect the parser flow."},
            {"role": "user", "content": "Any updates?"},
            {"role": "assistant", "content": "Still checking."},
        ],
    )

    assert title is None


def test_create_session_records_model_metadata_diagnostics_and_dedupes_same_model_warnings(
    tmp_path: Path,
) -> None:
    class _Surface(agent_loop_mod.NoopSurface):
        def __init__(self) -> None:
            self.errors: list[str] = []
            self.warnings: list[str] = []

        def on_warning(self, warning: str) -> None:
            self.warnings.append(warning)

        def on_error(self, err: str) -> None:
            self.errors.append(err)

    surface = _Surface()
    session_id = "metadata-warn"
    sessions_dir = tmp_path / "sessions"
    # The compactor shares the main model, exercising warning deduplication
    # across the two active roles.
    session = create_session(
        cfg=AppConfig(model="unknown-model-xyz"),
        root=tmp_path,
        mode="readonly",
        yes=False,
        max_steps=1,
        no_log=False,
        api_key_override="override-key",
        surface=surface,
        enable_conversation_summarization=True,
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
    )
    session.close()

    assert surface.errors == []
    assert len(surface.warnings) == 1
    assert "unknown-model-xyz" in surface.warnings[0]
    events = list(read_session_events(sessions_dir / f"{session_id}.jsonl"))
    session_start = next(event for event in events if event.get("type") == "session_start")
    payload = dict(session_start.get("payload") or {})
    diagnostics = list(payload.get("model_metadata_diagnostics") or [])
    assert payload["model_metadata_policy"] == "warn"
    assert {item["role"] for item in diagnostics} == {"coding", "compactor"}
    assert all(item["fallback_capacity_active"] is True for item in diagnostics)
    assert all("context_window_tokens" in item["fallback_capacity_fields"] for item in diagnostics)
    assert all("max_output_tokens" in item["fallback_capacity_fields"] for item in diagnostics)


def test_create_session_warns_for_distinct_compactor_model_separately(tmp_path: Path) -> None:
    class _Surface(agent_loop_mod.NoopSurface):
        def __init__(self) -> None:
            self.errors: list[str] = []
            self.warnings: list[str] = []

        def on_warning(self, warning: str) -> None:
            self.warnings.append(warning)

        def on_error(self, err: str) -> None:
            self.errors.append(err)

    surface = _Surface()
    cfg = AppConfig(model="unknown-main-model")
    cfg.extra_fields = {"role_models": {"compactor": "unknown-compactor-model"}}
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="readonly",
        yes=False,
        max_steps=1,
        no_log=False,
        api_key_override="override-key",
        surface=surface,
        enable_conversation_summarization=True,
        session_log_dir_override=tmp_path / "sessions",
    )
    try:
        assert surface.errors == []
        assert len(surface.warnings) == 2
        joined = " | ".join(surface.warnings)
        assert "unknown-main-model" in joined
        assert "unknown-compactor-model" in joined
    finally:
        session.close()


def test_create_session_warn_mode_metadata_uses_python_warning_when_surface_lacks_on_warning(
    tmp_path: Path,
) -> None:
    class _Surface:
        def __init__(self) -> None:
            self.errors: list[str] = []

        def on_status_update(self, status: object) -> None:
            _ = status

        def on_user_message(self, text: str) -> None:
            _ = text

        def on_progress_update(self, message: str) -> None:
            _ = message

        def on_assistant_token(self, delta: str) -> None:
            _ = delta

        def on_assistant_message_done(self, text: str) -> None:
            _ = text

        def on_subagent_start(self, event: object) -> None:
            _ = event

        def on_subagent_end(self, event: object) -> None:
            _ = event

        def on_tool_start(self, event: object) -> None:
            _ = event

        def on_tool_output(self, event: object) -> None:
            _ = event

        def on_tool_end(self, event: object) -> None:
            _ = event

        def on_patch_generated(self, event: object) -> None:
            _ = event

        def on_error(self, err: str) -> None:
            self.errors.append(err)

        def request_approval(self, request: object) -> object:
            _ = request
            return SimpleNamespace(allow=False)

    surface = _Surface()
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        session = create_session(
            cfg=AppConfig(model="unknown-model-xyz"),
            root=tmp_path,
            mode="readonly",
            yes=False,
            max_steps=1,
            no_log=False,
            api_key_override="override-key",
            surface=surface,
            enable_conversation_summarization=False,
            session_log_dir_override=tmp_path / "sessions",
        )
        session.close()

    assert surface.errors == []
    messages = [str(item.message) for item in seen]
    assert any("Unknown model unknown-model-xyz" in message for message in messages)
    assert not any(
        "Model metadata warning for unknown-model-xyz" in message for message in messages
    )


def test_create_session_warn_mode_metadata_falls_back_for_noop_surface_subclass_without_warning_override(
    tmp_path: Path,
) -> None:
    class _Surface(agent_loop_mod.NoopSurface):
        def __init__(self) -> None:
            self.errors: list[str] = []

        def on_error(self, err: str) -> None:
            self.errors.append(err)

    surface = _Surface()
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        session = create_session(
            cfg=AppConfig(model="unknown-model-xyz"),
            root=tmp_path,
            mode="readonly",
            yes=False,
            max_steps=1,
            no_log=False,
            api_key_override="override-key",
            surface=surface,
            enable_conversation_summarization=False,
            session_log_dir_override=tmp_path / "sessions",
        )
        session.close()

    assert surface.errors == []
    messages = [str(item.message) for item in seen]
    assert any("Unknown model unknown-model-xyz" in message for message in messages)
    assert not any(
        "Model metadata warning for unknown-model-xyz" in message for message in messages
    )


def test_create_session_strict_model_metadata_policy_fails_before_client_construction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            raise AssertionError("OpenAICompatClient should not be constructed")

    monkeypatch.setattr(agent_loop_mod, "OpenAICompatClient", FakeClient)

    with pytest.raises(ConfigError, match="model_metadata_policy=strict"):
        create_session(
            cfg=AppConfig(model="unknown-model-xyz", model_metadata_policy="strict"),
            root=tmp_path,
            mode="readonly",
            yes=False,
            max_steps=1,
            no_log=True,
            api_key_override="override-key",
        )


def test_create_session_strict_model_metadata_policy_allows_override_backed_capacity(
    tmp_path: Path,
) -> None:
    cfg = AppConfig(model="custom-model", model_metadata_policy="strict")
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "custom-model": {
                    "context_window_tokens": 65536,
                    "max_output_tokens": 4096,
                }
            }
        }
    }

    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="readonly",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    session.close()


def test_agent_session_context_left_uses_model_window_and_effective_budget(
    tmp_path: Path,
) -> None:
    from alysis_code.cli_impl.tui.footer import footer_fragments
    from alysis_code.cli_impl.tui.state import TuiState

    cfg = AppConfig(model="custom-model", model_metadata_policy="strict")
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "custom-model": {
                    "context_window_tokens": 20000,
                    "max_output_tokens": 2000,
                }
            }
        },
        "compaction": {"safety_margin_tokens": 600},
    }

    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="readonly",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    try:
        ctx = session.context_left()
        model_meta = session.model_registry.get("custom-model")
        expected_used = estimate_request_token_breakdown(
            messages=session.messages,
            tool_list=session.tool_list,
            pinned_prefix_len=session.pinned_prefix_len,
        ).total_tokens

        assert ctx.used_input_tokens == expected_used
        assert ctx.max_input_tokens == model_meta.context_window_tokens
        assert ctx.remaining_tokens == max(0, model_meta.context_window_tokens - expected_used)
        assert ctx.context_window_tokens == model_meta.context_window_tokens
        assert ctx.context_window_remaining_tokens == ctx.remaining_tokens
        assert ctx.context_window_percent_left == ctx.percent_left
        assert ctx.effective_input_budget == compute_input_budget(
            model_meta,
            safety_margin=600,
        )
        assert ctx.effective_remaining_tokens == max(
            0,
            ctx.effective_input_budget - expected_used,
        )
        assert ctx.startup_baseline_tokens == expected_used
        assert ctx.dynamic_context_used_tokens == 0
        assert ctx.dynamic_context_percent_left == 100.0

        session._hud_context_cache = ctx
        fresh_footer = footer_fragments(
            TuiState(context_pct=cli_mod._chat_context_percent_value(session)), width=120
        )
        assert "context: 100% left" in "".join(text for _, text in fresh_footer)

        session.messages.append({"role": "user", "content": "additional context " * 100})
        grown_ctx = session.context_left()
        assert grown_ctx.startup_baseline_tokens == expected_used
        assert grown_ctx.used_input_tokens > expected_used
        assert grown_ctx.dynamic_context_used_tokens == (
            grown_ctx.used_input_tokens - expected_used
        )
        assert grown_ctx.dynamic_context_percent_left is not None
        assert grown_ctx.dynamic_context_percent_left < 100.0
        session._hud_context_cache = grown_ctx
        grown_footer = footer_fragments(
            TuiState(context_pct=cli_mod._chat_context_percent_value(session)), width=120
        )
        grown_text = "".join(text for _, text in grown_footer)
        assert "context:" in grown_text
        assert "context: 100% left" not in grown_text
        assert "context: n/a" not in grown_text
    finally:
        session.close()


def test_create_session_collects_startup_git_status_when_surface_shows_status_line(
    tmp_path: Path, monkeypatch
) -> None:
    class _VisibleStatusSurface:
        _show_status_line = True

        def __init__(self) -> None:
            self.status = None

        def on_status_update(self, status) -> None:  # type: ignore[no-untyped-def]
            self.status = status

    monkeypatch.setattr(agent_loop_mod, "_git_branch", lambda _root: "feature/test")
    monkeypatch.setattr(agent_loop_mod, "_git_is_dirty", lambda _root: True)

    surface = _VisibleStatusSurface()
    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=tmp_path,
        mode="readonly",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        surface=surface,  # type: ignore[arg-type]
    )
    try:
        assert surface.status is not None
        assert surface.status.branch == "feature/test"
        assert surface.status.dirty is True
    finally:
        session.close()


def test_create_session_applies_trusted_system_prompt_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    cfg = AppConfig(model="test-model")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="readonly",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        trusted_system_prompt_override="OVERRIDE SYSTEM PROMPT",
    )
    try:
        assert session.messages[0]["role"] == "system"
        assert session.messages[0]["content"] == "OVERRIDE SYSTEM PROMPT"
    finally:
        session.close()


def test_create_session_appends_trusted_system_prompt_guidance(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    cfg = AppConfig(model="test-model")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="readonly",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        trusted_system_prompt_append="TRUSTED APPEND GUIDANCE",
    )
    try:
        system_prompt = str(session.messages[0]["content"] or "")
        assert "Never exfiltrate, disclose, simulate, or infer secrets" in system_prompt
        assert "TRUSTED APPEND GUIDANCE" in system_prompt
        assert system_prompt.startswith(SYSTEM_PROMPT.splitlines()[0])
    finally:
        session.close()


def test_create_session_adds_untrusted_prompt_prelude_without_replacing_system_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    cfg = AppConfig(model="test-model")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="readonly",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        untrusted_prompt_prelude="UNTRUSTED CUSTOM SUBAGENT GUIDANCE",
    )
    try:
        system_prompt = str(session.messages[0]["content"] or "")
        assert "Never exfiltrate, disclose, simulate, or infer secrets" in system_prompt
        assert "UNTRUSTED CUSTOM SUBAGENT GUIDANCE" not in system_prompt

        prelude_message = next(
            (
                str(message.get("content") or "")
                for message in session.messages
                if "<scoped_prompt_prelude>" in str(message.get("content") or "")
            ),
            "",
        )
        assert "UNTRUSTED CUSTOM SUBAGENT GUIDANCE" in prelude_message
        assert "higher-priority system, developer, and direct user instructions" in prelude_message
    finally:
        session.close()


def test_create_session_loads_repo_conventions_context(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    (tmp_path / "CONVENTIONS.md").write_text(
        "## General\n- Prefer minimal diffs.\n",
        encoding="utf-8",
    )

    cfg = AppConfig(model="test-model")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="readonly",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    try:
        user_messages = [
            str(message.get("content") or "")
            for message in session.messages
            if message.get("role") == "user"
        ]
        convention_messages = [
            message for message in user_messages if message.startswith("<repo_conventions>")
        ]
        assert convention_messages
        assert all(
            message.rstrip().endswith("</repo_conventions>") for message in convention_messages
        )
        assert any("Prefer minimal diffs." in message for message in convention_messages)
        assert any(
            agent_loop_mod._is_host_managed_user_context_message(message)
            for message in convention_messages
        )
    finally:
        session.close()


def test_create_session_injects_environment_context_message(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    cfg = AppConfig(model="test-model")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        deny_write_prefixes=["custom/"],
        allow_write_globs=["src/**", "tests/**", "README.md"],
        non_interactive=False,
    )
    try:
        assert session.verification_selection_source == "config.verify_commands_fallback"
        assert session.verification_contract_type == "generic_fallback"
        assert session.verification_authoritative is False
        env_message = next(
            (
                str(message.get("content") or "")
                for message in session.messages
                if message.get("role") == "user"
                and "<environment_context>" in str(message.get("content") or "")
            ),
            "",
        )
        assert env_message
        assert "mode: auto" in env_message
        assert "yes: false" in env_message
        assert "non_interactive: false" in env_message
        assert "one_shot_execution: false" in env_message
        expected_prefixes = [
            *agent_loop_mod.ALWAYS_PROTECTED_WRITE_PREFIXES,
            "custom",
        ]
        assert f"deny_write_prefixes: {json.dumps(expected_prefixes)}" in env_message
        assert 'allow_write_globs: ["src/**", "tests/**", "README.md"]' in env_message
        assert "verification_enabled: true" in env_message
        assert 'recommended_verification_commands: ["pytest -q"]' in env_message
    finally:
        session.close()


def test_create_session_one_shot_uses_repo_scan_bootstrap_and_repo_aware_verify_commands(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    repo = tmp_path / "repo"
    _init_git_repo_with_commit(repo)
    (repo / "package.json").write_text('{"scripts":{"test":"vitest run"}}\n', encoding="utf-8")

    cfg = AppConfig(model="test-model")
    session = create_session(
        cfg=cfg,
        root=repo,
        mode="auto",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        one_shot_execution=True,
    )
    try:
        user_messages = [
            str(message.get("content") or "")
            for message in session.messages
            if message.get("role") == "user"
        ]
        repo_message = next(
            (message for message in user_messages if message.startswith("Repo summary")),
            "",
        )
        env_message = next(
            (message for message in user_messages if "<environment_context>" in message),
            "",
        )

        assert repo_message.startswith("Repo summary (repo scan):")
        assert "Important files:" in repo_message
        assert "README.md" in repo_message
        assert "package.json" in repo_message
        assert "Likely verify: npm test" in repo_message
        assert "Repo summary (top-level):" not in repo_message
        assert 'recommended_verification_commands: ["npm test"]' in env_message
    finally:
        session.close()


def test_create_session_one_shot_falls_back_to_config_verify_commands_when_scan_has_no_inference(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    cfg = AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check ."])
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        one_shot_execution=True,
    )
    try:
        env_message = next(
            (
                str(message.get("content") or "")
                for message in session.messages
                if message.get("role") == "user"
                and "<environment_context>" in str(message.get("content") or "")
            ),
            "",
        )
        assert 'recommended_verification_commands: ["pytest -q", "ruff check ."]' in env_message
    finally:
        session.close()


def test_create_session_omits_verify_tool_and_verify_hints_when_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    cfg = AppConfig(model="test-model")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        verification_enabled=False,
    )
    try:
        env_message = next(
            (
                str(message.get("content") or "")
                for message in session.messages
                if message.get("role") == "user"
                and "<environment_context>" in str(message.get("content") or "")
            ),
            "",
        )
        assert env_message
        assert "verification_enabled: false" in env_message
        assert "recommended_verification_commands:" not in env_message
        assert "verify_run" not in session.tools
    finally:
        session.close()


def test_create_session_marks_authoritative_verify_commands_in_managed_sessions(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    cfg = AppConfig(model="test-model", verify_commands=["pytest -q"])
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        authoritative_verification_commands=["PYTHONPATH=src pytest -q", "ruff check ."],
    )
    try:
        env_message = next(
            (
                str(message.get("content") or "")
                for message in session.messages
                if message.get("role") == "user"
                and "<environment_context>" in str(message.get("content") or "")
            ),
            "",
        )
        assert env_message
        assert "verification_enabled: true" in env_message
        assert "verification_commands_authoritative: true" in env_message
        assert (
            'authoritative_verification_commands: ["PYTHONPATH=src pytest -q", "ruff check ."]'
            in env_message
        )
        assert "recommended_verification_commands:" not in env_message
        assert session.cfg.verify_commands == ["PYTHONPATH=src pytest -q", "ruff check ."]
    finally:
        session.close()


def test_create_session_rejects_vacuous_explicit_verify_command_before_startup(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(VerifyError, match="vacuous_verifier"):
        create_session(
            cfg=AppConfig(model="test-model", verify_commands=["pytest -q"]),
            root=tmp_path,
            mode="auto",
            yes=False,
            max_steps=1,
            no_log=True,
            api_key_override="override-key",
            verify_cmd=["true"],
        )


def test_create_session_logs_prompt_hash_and_environment_payload(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    cfg = AppConfig(
        model="test-model",
        verify_commands=["pytest -q", "python -m unittest -v"],
    )
    session_id = "test-session-id"
    sessions_dir = tmp_path / "sessions"
    override_prompt = "OVERRIDE SYSTEM PROMPT"

    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="override-key",
        deny_write_prefixes=["./docs/tmp/"],
        allow_write_globs=["src/**", "./README.md"],
        non_interactive=True,
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
        trusted_system_prompt_override=override_prompt,
    )
    session.close()

    events = list(read_session_events(sessions_dir / f"{session_id}.jsonl"))
    session_start = next(event for event in events if event.get("type") == "session_start")
    payload = dict(session_start.get("payload") or {})

    assert (
        payload["system_prompt_sha256"]
        == hashlib.sha256(override_prompt.encode("utf-8")).hexdigest()
    )
    assert payload["yes"] is True
    assert payload["non_interactive"] is True
    assert payload["verification_enabled"] is True
    assert payload["effective_verification_commands"] == [
        "pytest -q",
        "python -m unittest -v",
    ]
    assert payload["workspace_root"] == str(tmp_path)
    assert payload["focus_dir"] == str(tmp_path)
    assert payload["focus_relpath"] == "."
    assert payload["workspace_kind"] == "plain_dir"
    assert payload["git_root"] is None
    assert payload["has_head_commit"] is False
    assert payload["current_branch"] is None
    assert payload["deny_write_prefixes"] == [
        *agent_loop_mod.ALWAYS_PROTECTED_WRITE_PREFIXES,
        "docs/tmp",
    ]
    assert payload["allow_write_globs"] == ["src/**", "README.md"]
    assert payload["recommended_verification_commands"] == [
        "pytest -q",
        "python -m unittest -v",
    ]
    assert payload["authoritative_verification_commands"] is None


def test_sessions_score_outputs_json_metrics(tmp_path: Path) -> None:
    runner = CliRunner()
    sessions_dir = tmp_path / "data" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    events = [
        {
            "type": "session_start",
            "session_id": "sample",
            "payload": {"system_prompt_sha256": "hash-1"},
        },
        {
            "type": "tool_call",
            "payload": {"name": "fs_read", "step": 1, "arguments": {"path": "a.py"}},
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "shell_run",
                "step": 2,
                "arguments": {"cmd": "python3 -m unittest -v"},
            },
        },
        {
            "type": "llm_usage",
            "payload": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        },
    ]
    (sessions_dir / "sample.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "config"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
    }
    result = runner.invoke(
        alysis_app,
        ["sessions", "score", "sample", "--json"],
        env=env,
    )

    assert result.exit_code == 0
    assert '"session_id": "sample"' in result.output
    assert '"tool_calls": 2' in result.output
    assert '"test_shell_runs": 1' in result.output
    assert '"total_tokens": 8' in result.output


def test_run_rejects_multiple_api_key_sources(tmp_path: Path) -> None:
    runner = CliRunner()
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["run", "--model", "test-model", "--api-key", "k", "--api-key-env", "X", "hi"],
        env=env,
    )
    assert result.exit_code == 2


def test_run_passes_non_interactive_flag_to_run_agent(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, bool] = {}

    def fake_run_agent(*, non_interactive: bool = False, **_kwargs) -> int:  # type: ignore[override]
        captured["non_interactive"] = non_interactive
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(cli_mod.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli_mod.sys.stdout, "isatty", lambda: False)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["run", "--model", "test-model", "--api-key", "k", "hi"],
        env=env,
    )
    assert result.exit_code == 0
    assert captured["non_interactive"] is True


def test_run_uses_infrastructure_exit_code_after_transient_llm_retries_exhaust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_agent(**_kwargs: object) -> int:
        raise LLMError("LLM request failed: [Errno -2] Name or service not known")

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = CliRunner().invoke(
        alysis_app,
        [
            "run",
            "--path",
            os.fspath(tmp_path),
            "--allow-broad-workspace",
            "--model",
            "test-model",
            "--api-key",
            "k",
            "hi",
        ],
        env=env,
    )

    assert result.exit_code == INFRASTRUCTURE_FAILURE_EXIT_CODE
    # Raised straight from run_agent, so no retry loop recorded any counts;
    # the label must not claim that retries happened.
    assert "Infrastructure error:" in result.output
    assert "after retries" not in result.output


def test_run_keeps_permanent_provider_rejections_as_real_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_agent(**_kwargs: object) -> int:
        raise LLMError("LLM error 401: invalid api key")

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = CliRunner().invoke(
        alysis_app,
        [
            "run",
            "--path",
            os.fspath(tmp_path),
            "--allow-broad-workspace",
            "--model",
            "test-model",
            "--api-key",
            "k",
            "hi",
        ],
        env=env,
    )

    assert result.exit_code == AGENT_FAILURE_EXIT_CODE
    assert "Infrastructure error" not in result.output


def test_run_passes_one_shot_execution_flag_to_run_agent(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, object] = {}

    def fake_run_agent(
        *,
        one_shot_execution: bool = False,
        compaction_profile: str = "chat",
        **_kwargs,
    ) -> int:  # type: ignore[override]
        captured["one_shot_execution"] = one_shot_execution
        captured["compaction_profile"] = compaction_profile
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["run", "--model", "test-model", "--api-key", "k", "hi"],
        env=env,
    )
    assert result.exit_code == 0
    assert captured["one_shot_execution"] is True
    assert captured["compaction_profile"] == "execution"


def test_run_passes_runtime_kind_to_run_agent(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, object] = {}

    def fake_run_agent(*, runtime_kind: object = None, **_kwargs) -> int:  # type: ignore[override]
        captured["runtime_kind"] = runtime_kind
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["run", "--model", "test-model", "--api-key", "k", "hi"],
        env=env,
    )
    assert result.exit_code == 0
    assert captured["runtime_kind"] == RuntimeKind.ONE_SHOT


def test_run_passes_simple_agent_turn_budget_flags_to_run_agent(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, object] = {}

    def fake_run_agent(
        *,
        enable_chat_turn_step_budget: bool = False,
        chat_turn_fixed_override: int | None = None,
        **_kwargs,
    ) -> int:
        captured["enable_chat_turn_step_budget"] = enable_chat_turn_step_budget
        captured["chat_turn_fixed_override"] = chat_turn_fixed_override
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["run", "--model", "test-model", "--api-key", "k", "--max-steps", "7", "hi"],
        env=env,
    )
    assert result.exit_code == 0
    assert captured["enable_chat_turn_step_budget"] is True
    assert captured["chat_turn_fixed_override"] == 7


def test_run_benchmark_profile_applies_raw_agent_defaults(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {}

    def fake_run_agent(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
    }

    result = runner.invoke(
        alysis_app,
        [
            "run",
            "--path",
            os.fspath(tmp_path),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--benchmark",
            "hi",
        ],
        env=env,
    )

    assert result.exit_code == 0
    cfg = captured["cfg"]
    assert isinstance(cfg, AppConfig)
    assert captured["mode"] == "auto"
    assert captured["yes"] is True
    assert captured["max_steps"] >= 80
    assert captured["chat_turn_fixed_override"] == captured["max_steps"]
    assert captured["enable_tool_output_offload"] is True
    assert captured["compaction_profile"] == "execution"
    assert captured["subagents_enabled"] is False
    assert cfg.routing_mode == "code_only"
    assert cfg.subagents_enabled is False
    assert cfg.skills_enabled is False
    assert cfg.skills_auto_invoke is False
    assert cfg.custom_tools_enabled is False
    assert cfg.web_search_mode == "off"


def test_benchmark_flag_keeps_one_shot_system_prompt_identical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_cfg = AppConfig(
        model="test-model",
        routing_mode="code_only",
        subagents_enabled=False,
        skills_enabled=False,
        skills_auto_invoke=False,
        custom_tools_enabled=False,
        web_search_mode="off",
        max_steps=80,
    )
    prompts: list[bytes] = []

    def fake_load_config() -> AppConfig:
        return profile_cfg.model_copy(deep=True)

    def fake_run_agent(**kwargs: Any) -> int:
        prompt_context = agent_loop_mod.prepare_session_prompt_context(
            cfg=kwargs["cfg"],
            root=kwargs["root"],
            mode=kwargs["mode"],
            yes=kwargs["yes"],
            non_interactive=kwargs["non_interactive"],
            one_shot_execution=kwargs["one_shot_execution"],
            verification_enabled=False,
            subagents_enabled=kwargs["subagents_enabled"],
            workspace_binding=kwargs["workspace_binding"],
        )
        prompts.append(prompt_context.system_prompt.encode("utf-8"))
        return 0

    monkeypatch.setattr(cli_mod, "load_config", fake_load_config)
    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
    }
    common_args = [
        "run",
        "--path",
        os.fspath(tmp_path),
        "--allow-broad-workspace",
        "--yes",
        "--mode",
        "auto",
        "--max-steps",
        "80",
        "--no-subagents",
        "--model",
        "test-model",
        "--api-key",
        "k",
    ]

    normal = CliRunner().invoke(alysis_app, [*common_args, "hi"], env=env)
    profiled = CliRunner().invoke(
        alysis_app,
        [*common_args, "--benchmark", "hi"],
        env=env,
    )

    assert normal.exit_code == 0
    assert profiled.exit_code == 0
    assert len(prompts) == 2
    assert prompts[0] == prompts[1]


def test_run_benchmark_profile_respects_explicit_overrides(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {}

    def fake_run_agent(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
    }

    result = runner.invoke(
        alysis_app,
        [
            "run",
            "--path",
            os.fspath(tmp_path),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--benchmark",
            "--mode",
            "review",
            "--max-steps",
            "12",
            "--subagents",
            "hi",
        ],
        env=env,
    )

    assert result.exit_code == 0
    cfg = captured["cfg"]
    assert isinstance(cfg, AppConfig)
    assert captured["mode"] == "review"
    assert captured["max_steps"] == 12
    assert captured["chat_turn_fixed_override"] == 12
    assert captured["subagents_enabled"] is True
    assert cfg.subagents_enabled is True


def test_run_profile_env_enables_raw_benchmark_profile(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {}

    def fake_run_agent(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
        "ALYSIS_RUN_PROFILE": "raw-benchmark",
    }

    result = runner.invoke(
        alysis_app,
        [
            "run",
            "--path",
            os.fspath(tmp_path),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "hi",
        ],
        env=env,
    )

    assert result.exit_code == 0
    assert captured["mode"] == "auto"
    assert captured["yes"] is True
    assert captured["max_steps"] >= 80
    assert captured["compaction_profile"] == "execution"


def test_run_passes_api_key_env_to_run_agent(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, str | None] = {}

    def fake_run_agent(*, api_key_override: str | None = None, **_kwargs) -> int:
        captured["api_key_override"] = api_key_override
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
        "MY_KEY": "env-key",
    }

    result = runner.invoke(
        alysis_app,
        ["run", "--model", "test-model", "--api-key-env", "MY_KEY", "hi"],
        env=env,
    )
    assert result.exit_code == 0
    assert captured["api_key_override"] == "env-key"


def test_run_passes_api_key_stdin_to_run_agent(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, str | None] = {}

    def fake_run_agent(*, api_key_override: str | None = None, **_kwargs) -> int:
        captured["api_key_override"] = api_key_override
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["run", "--model", "test-model", "--api-key-stdin", "hi"],
        input="stdin-key\n",
        env=env,
    )
    assert result.exit_code == 0
    assert captured["api_key_override"] == "stdin-key"


def test_run_passes_temperature_to_run_agent(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, float] = {}

    def fake_run_agent(*, cfg: AppConfig, **_kwargs) -> int:  # type: ignore[override]
        captured["temperature"] = cfg.temperature
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["run", "--model", "test-model", "--temperature", "0.2", "--api-key", "k", "hi"],
        env=env,
    )
    assert result.exit_code == 0
    assert captured["temperature"] == 0.2


def test_run_passes_stream_to_run_agent(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, bool] = {}

    def fake_run_agent(*, cfg: AppConfig, **_kwargs) -> int:  # type: ignore[override]
        captured["stream"] = cfg.stream
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["run", "--model", "test-model", "--stream", "--api-key", "k", "hi"],
        env=env,
    )
    assert result.exit_code == 0
    assert captured["stream"] is True


def test_run_passes_image_paths_to_run_agent(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, list[str] | None] = {}

    def fake_run_agent(*, image_paths: list[str] | None = None, **_kwargs) -> int:
        captured["image_paths"] = image_paths
        return 0

    img1 = tmp_path / "a.png"
    img2 = tmp_path / "b.jpg"
    img1.write_bytes(b"fake")
    img2.write_bytes(b"fake")

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        [
            "run",
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--image",
            os.fspath(img1),
            "--image",
            os.fspath(img2),
            "hi",
        ],
        env=env,
    )
    assert result.exit_code == 0
    assert captured["image_paths"] == [os.fspath(img1), os.fspath(img2)]


def test_run_passes_bound_workspace_root_and_focus_metadata(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    _init_git_repo_with_commit(repo)
    subdir = repo / "pkg" / "api"
    subdir.mkdir(parents=True)
    captured: dict[str, object] = {}

    def fake_run_agent(*, root: Path, workspace_binding: Any = None, **_kwargs) -> int:
        captured["root"] = root
        captured["workspace_binding"] = workspace_binding
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["run", "--path", os.fspath(subdir), "--model", "test-model", "--api-key", "k", "hi"],
        env=env,
    )

    assert result.exit_code == 0
    assert captured["root"] == repo.resolve()
    binding = captured["workspace_binding"]
    assert binding.workspace_context.workspace_root == repo.resolve()
    assert binding.workspace_context.focus_path == subdir.resolve()
    assert binding.workspace_context.focus_relpath == "pkg/api"


def test_run_create_path_bootstraps_missing_directory(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    missing = tmp_path / "new" / "repo"
    captured: dict[str, object] = {}

    def fake_run_agent(*, root: Path, workspace_binding: Any = None, **_kwargs) -> int:
        captured["root"] = root
        captured["workspace_binding"] = workspace_binding
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        [
            "run",
            "--path",
            os.fspath(missing),
            "--create-path",
            "--model",
            "test-model",
            "--api-key",
            "k",
            "hi",
        ],
        env=env,
    )

    assert result.exit_code == 0
    assert missing.exists()
    assert captured["root"] == missing.resolve()
    binding = captured["workspace_binding"]
    assert binding.created_path is True
    assert binding.requested_path == missing.resolve()


def test_chat_passes_temperature_to_create_session(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, float] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def close(self) -> None:
            return None

    def fake_create_session(*, cfg: AppConfig, **_kwargs) -> _DummySession:  # type: ignore[override]
        captured["temperature"] = cfg.temperature
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--temperature", "0.2", "--api-key", "k", "--no-log"],
        input="exit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert captured["temperature"] == 0.2


def test_chat_passes_runtime_kind_to_create_session(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, object] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def close(self) -> None:
            return None

    def fake_create_session(*, runtime_kind: object = None, **_kwargs) -> _DummySession:
        captured["runtime_kind"] = runtime_kind
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="exit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert captured["runtime_kind"] == RuntimeKind.INTERACTIVE_CHAT


def test_chat_passes_stream_to_create_session(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, bool] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def close(self) -> None:
            return None

    def fake_create_session(*, cfg: AppConfig, **_kwargs) -> _DummySession:  # type: ignore[override]
        captured["stream"] = cfg.stream
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--stream", "--api-key", "k", "--no-log"],
        input="exit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert captured["stream"] is True


def test_chat_defaults_stream_on_when_flag_omitted(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, bool] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def close(self) -> None:
            return None

    def fake_create_session(*, cfg: AppConfig, **_kwargs) -> _DummySession:  # type: ignore[override]
        captured["stream"] = cfg.stream
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="exit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert captured["stream"] is True


def test_chat_passes_bound_workspace_root_to_create_session(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    _init_git_repo_with_commit(repo)
    subdir = repo / "pkg"
    subdir.mkdir()
    captured: dict[str, object] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def close(self) -> None:
            return None

    def fake_create_session(
        *, root: Path, workspace_binding: Any = None, **_kwargs
    ) -> _DummySession:
        captured["root"] = root
        captured["workspace_binding"] = workspace_binding
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        [
            "chat",
            "--path",
            os.fspath(subdir),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        input="exit\n",
        env=env,
    )

    assert result.exit_code == 0
    assert captured["root"] == repo.resolve()
    binding = captured["workspace_binding"]
    assert binding.workspace_context.workspace_root == repo.resolve()
    assert binding.workspace_context.focus_path == subdir.resolve()
    assert binding.workspace_context.focus_relpath == "pkg"


def test_chat_create_path_bootstraps_missing_directory(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    missing = tmp_path / "missing" / "repo"
    captured: dict[str, object] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def close(self) -> None:
            return None

    def fake_create_session(
        *, root: Path, workspace_binding: Any = None, **_kwargs
    ) -> _DummySession:
        captured["root"] = root
        captured["workspace_binding"] = workspace_binding
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        [
            "chat",
            "--path",
            os.fspath(missing),
            "--create-path",
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        input="exit\n",
        env=env,
    )

    assert result.exit_code == 0
    assert missing.exists()
    assert captured["root"] == missing.resolve()
    binding = captured["workspace_binding"]
    assert binding.created_path is True
    assert binding.requested_path == missing.resolve()


def test_chat_image_command_queues_for_next_turn(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, list[str] | None] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def run_turn(
            self, _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            captured["image_paths"] = image_paths
            return 0

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }
    image_path = tmp_path / "sample.png"

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input=f"/image {image_path}\nhello\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert captured["image_paths"] == [os.fspath(image_path)]


def test_chat_image_command_without_path_pastes_clipboard(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, list[str] | None] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def run_turn(
            self, _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            captured["image_paths"] = image_paths
            return 0

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    pasted = tmp_path / "from_image_clipboard.png"
    expected_root = Path(".").resolve()

    def fake_paste_clipboard_image(*, root: Path, output_path: str | None = None) -> Path:
        assert root == expected_root
        assert output_path is None
        return pasted

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(cli_mod, "paste_clipboard_image", fake_paste_clipboard_image)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/image\nhello\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert captured["image_paths"] == [os.fspath(pasted)]


def test_chat_paste_image_command_queues_for_next_turn(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, list[str] | None] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def run_turn(
            self, _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            captured["image_paths"] = image_paths
            return 0

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    pasted = tmp_path / "from_clipboard.png"
    expected_root = Path(".").resolve()

    def fake_paste_clipboard_image(*, root: Path, output_path: str | None = None) -> Path:
        assert root == expected_root
        assert output_path is None
        return pasted

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(cli_mod, "paste_clipboard_image", fake_paste_clipboard_image)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/paste-image\nhello\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert captured["image_paths"] == [os.fspath(pasted)]


def test_chat_trace_command_updates_reasoning_level_for_following_turn(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, str] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummyClient:
        model = "test-model"
        temperature = 1.0

    class _DummySurface:
        trace_level = "compact"

        def set_trace_level(self, level: str) -> str:
            self.trace_level = level
            return self.trace_level

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        surface = _DummySurface()
        stream = True
        mode = "review"

        def run_turn(
            self, _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            captured["trace"] = self.surface.trace_level
            return 0

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/trace full\nhello\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Reasoning trace set for this session: full" in result.output
    assert captured["trace"] == "full"


def test_chat_trace_command_rejects_invalid_level(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/trace detailed\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Invalid trace level." in result.output


def test_chat_trace_command_without_arg_uses_picker_selection(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, str] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummySurface:
        trace_level = "compact"

        def set_trace_level(self, level: str) -> str:
            self.trace_level = level
            return self.trace_level

    class _DummySession:
        store = _DummyStore()
        surface = _DummySurface()
        stream = False
        mode = "review"

        def run_turn(
            self, _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            captured["trace"] = self.surface.trace_level
            return 0

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(
        cli_mod,
        "_select_chat_trace_interactive",
        lambda **_kwargs: ("full", True),
    )
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/trace\nhello\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Reasoning trace set for this session: full" in result.output
    assert captured["trace"] == "full"


def test_chat_model_command_updates_model_for_following_turn(tmp_path: Path, monkeypatch) -> None:
    from alysis_code.cli_impl.chat import loop as chat_loop_mod

    runner = CliRunner()
    captured: dict[str, str] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummyClient:
        model = "test-model"
        temperature = 1.0

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        cfg = AppConfig(model="test-model")
        stream = False
        mode = "review"

        def run_turn(
            self, _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            captured["model"] = self.client.model
            return 0

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    def fake_apply_config(*, session: _DummySession, cfg: AppConfig) -> None:
        session.cfg = cfg
        session.client.model = cfg.model

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(chat_loop_mod, "_apply_config_menu_changes_to_session", fake_apply_config)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/model gpt-4.1-mini\nhello\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert captured["model"] == "gpt-4.1-mini"


def test_chat_mode_command_updates_mode_for_following_turn(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, str] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummyClient:
        model = "test-model"
        temperature = 1.0

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"

        def run_turn(
            self, _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            captured["mode"] = self.mode
            return 0

        def close(self) -> None:
            return None

    rebuilt: dict[str, str] = {}

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    def fake_rebuild_session_tools_for_mode(*, session: _DummySession, mode: str) -> None:
        rebuilt["mode"] = mode

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(
        cli_mod,
        "_rebuild_session_tools_for_mode",
        fake_rebuild_session_tools_for_mode,
    )
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/permissions auto\nhello\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert rebuilt["mode"] == "auto"
    assert captured["mode"] == "auto"


def test_chat_mode_command_refreshes_environment_context_message(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    session_holder: dict[str, Any] = {}
    refreshed_messages: list[str] = []

    class _DummyStore:
        session_id = "sid"

    class _DummyClient:
        model = "test-model"
        temperature = 1.0

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="test-model", default_mode="review")
        yes = False
        non_interactive = False
        one_shot_execution = False
        verification_enabled = True
        effective_verification_commands = ["pytest -q"]
        authoritative_verification_commands = None
        verification_selection_source = "config.verify_commands_fallback"
        verification_selection_reason = (
            "using the configured generic fallback because repo scan found no repo-native command"
        )
        verification_contract_type = "generic_fallback"
        verification_authoritative = False
        deny_write_prefixes = [".alysis"]
        allow_write_globs = None
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": agent_loop_mod._environment_context_message(
                    mode="review",
                    yes=False,
                    non_interactive=False,
                    deny_write_prefixes=[".alysis"],
                    allow_write_globs=None,
                    verification_enabled=True,
                    recommended_verification_commands=["pytest -q"],
                    authoritative_verification_commands=None,
                    verification_selection_source="config.verify_commands_fallback",
                    verification_selection_reason=(
                        "using the configured generic fallback because repo scan found no repo-native command"
                    ),
                    verification_contract_type="generic_fallback",
                    verification_authoritative=False,
                    one_shot_execution=False,
                ),
            },
            {"role": "user", "content": "<other_pin>\nkeep me\n</other_pin>\n"},
        ]

        def run_turn(
            self, _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            _ = image_paths
            return 0

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs: Any) -> _DummySession:
        session = _DummySession()
        session_holder["session"] = session
        return session

    real_refresh = cli_mod.refresh_session_environment_context_message

    def spy_refresh(session: Any) -> bool:
        refreshed = bool(real_refresh(session))
        if refreshed:
            refreshed_messages.append(str(session.messages[1]["content"]))
        return refreshed

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(cli_mod, "_rebuild_session_tools_for_mode", lambda **_kwargs: None)
    monkeypatch.setattr(cli_mod, "refresh_session_environment_context_message", spy_refresh)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/permissions auto\nhello\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert refreshed_messages
    assert "mode: auto" in refreshed_messages[-1]
    assert (
        session_holder["session"].messages[2]["content"] == "<other_pin>\nkeep me\n</other_pin>\n"
    )


def test_persona_clamp_and_restore_refresh_environment_context_message(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # The ask persona clamps the session to readonly and code restores the
    # base mode; both transitions must refresh the pinned environment context.
    runner = CliRunner()
    refreshed_messages: list[str] = []

    class _DummyStore:
        session_id = "sid"

    class _DummyClient:
        model = "test-model"
        temperature = 1.0

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="test-model", default_mode="review")
        yes = False
        non_interactive = False
        one_shot_execution = False
        verification_enabled = True
        effective_verification_commands = ["pytest -q"]
        authoritative_verification_commands = None
        verification_selection_source = "config.verify_commands_fallback"
        verification_selection_reason = (
            "using the configured generic fallback because repo scan found no repo-native command"
        )
        verification_contract_type = "generic_fallback"
        verification_authoritative = False
        deny_write_prefixes = [".alysis"]
        allow_write_globs = None
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": agent_loop_mod._environment_context_message(
                    mode="review",
                    yes=False,
                    non_interactive=False,
                    deny_write_prefixes=[".alysis"],
                    allow_write_globs=None,
                    verification_enabled=True,
                    recommended_verification_commands=["pytest -q"],
                    authoritative_verification_commands=None,
                    verification_selection_source="config.verify_commands_fallback",
                    verification_selection_reason=(
                        "using the configured generic fallback because repo scan found no repo-native command"
                    ),
                    verification_contract_type="generic_fallback",
                    verification_authoritative=False,
                    one_shot_execution=False,
                ),
            },
            {"role": "user", "content": "<other_pin>\nkeep me\n</other_pin>\n"},
        ]

        @staticmethod
        def run_turn(
            _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    real_refresh = cli_mod.refresh_session_environment_context_message

    def spy_refresh(session: Any) -> bool:
        refreshed = bool(real_refresh(session))
        if refreshed:
            refreshed_messages.append(str(session.messages[1]["content"]))
        return refreshed

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_rebuild_session_tools_for_mode", lambda **_kwargs: None)
    monkeypatch.setattr(cli_mod, "refresh_session_environment_context_message", spy_refresh)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/persona ask\n/persona code\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Persona set for this session: ask" in result.output
    assert "Persona set for this session: code" in result.output
    assert len(refreshed_messages) == 2
    assert "mode: readonly" in refreshed_messages[0]
    assert "mode: review" in refreshed_messages[1]


def test_chat_mode_command_accepts_friendly_alias(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, str] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummyClient:
        model = "test-model"
        temperature = 1.0

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"

        def run_turn(
            self, _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            captured["mode"] = self.mode
            return 0

        def close(self) -> None:
            return None

    rebuilt: dict[str, str] = {}

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    def fake_rebuild_session_tools_for_mode(*, session: _DummySession, mode: str) -> None:
        rebuilt["mode"] = mode

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(
        cli_mod,
        "_rebuild_session_tools_for_mode",
        fake_rebuild_session_tools_for_mode,
    )
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/permissions fast\nhello\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert rebuilt["mode"] == "auto"
    assert captured["mode"] == "auto"


def test_chat_mode_command_without_arg_uses_picker_selection(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, str] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummyClient:
        model = "test-model"
        temperature = 1.0

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"

        def run_turn(
            self, _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            captured["mode"] = self.mode
            return 0

        def close(self) -> None:
            return None

    rebuilt: dict[str, str] = {}

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    def fake_rebuild_session_tools_for_mode(*, session: _DummySession, mode: str) -> None:
        rebuilt["mode"] = mode

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(
        cli_mod,
        "_rebuild_session_tools_for_mode",
        fake_rebuild_session_tools_for_mode,
    )
    monkeypatch.setattr(
        cli_mod,
        "_select_chat_mode_interactive",
        lambda **_kwargs: ("auto", True),
    )
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/permissions\nhello\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert rebuilt["mode"] == "auto"
    assert captured["mode"] == "auto"


def test_chat_mode_command_without_arg_falls_back_to_panel(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, str] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummyClient:
        model = "test-model"
        temperature = 1.0

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"

        def run_turn(
            self, _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            captured["mode"] = self.mode
            return 0

        def close(self) -> None:
            return None

    rebuilt: dict[str, str] = {}

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    def fake_rebuild_session_tools_for_mode(*, session: _DummySession, mode: str) -> None:
        rebuilt["mode"] = mode

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(
        cli_mod,
        "_rebuild_session_tools_for_mode",
        fake_rebuild_session_tools_for_mode,
    )
    monkeypatch.setattr(
        cli_mod,
        "_select_chat_mode_interactive",
        lambda **_kwargs: (None, False),
    )
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/permissions\nhello\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Permissions (current:" in result.output
    assert "mode" not in rebuilt
    assert captured["mode"] == "review"


def test_removed_onboarding_commands_are_unknown(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, str] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()
        stream = False
        mode = "review"

        def run_turn(
            self, instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            captured["instruction"] = instruction
            return 0

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/examples\n/keys\n/tour start\nhello\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Unknown command: /examples" in result.output
    assert "Unknown command: /keys" in result.output
    assert "Unknown command: /tour" in result.output
    assert captured["instruction"] == "hello"


def test_chat_usage_and_context_commands_are_handled(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, str] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummyUsageSummary:
        def by_model_rows(self) -> list[dict[str, object]]:
            return [
                {
                    "model": "gpt-5-nano",
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "cost_usd": 0.123,
                    "known_cost_calls": 1,
                    "unknown_cost_count": 0,
                    "api_usage_calls": 1,
                    "estimate_usage_calls": 0,
                }
            ]

        def totals(self) -> dict[str, object]:
            return {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "cost_usd": 0.123,
                "known_cost_calls": 1,
                "unknown_cost_calls": 0,
                "api_usage_calls": 1,
                "estimate_usage_calls": 0,
                "cache_efficiency": {
                    "reported_calls": 2,
                    "cached_prompt_tokens": 65,
                    "uncached_prompt_tokens": 35,
                    "hit_ratio": 0.65,
                    "largest_uncached_call": {
                        "call_position": 2,
                        "uncached_prompt_tokens": 30,
                    },
                },
            }

    class _DummyContext:
        model_name = "gpt-5-nano"
        source = "fallback"
        max_input_tokens = 8192
        used_input_tokens = 128
        remaining_tokens = 8064
        percent_left = 98.4

    class _DummySession:
        store = _DummyStore()
        usage_summary = _DummyUsageSummary()
        stream = False
        mode = "review"

        def context_left(self) -> _DummyContext:
            return _DummyContext()

        def run_turn(
            self, instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            captured["instruction"] = instruction
            return 0

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/usage\n/context\n/ctx\nhello\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Usage" in result.output
    assert "Cache efficiency: 65.00% hit" in result.output
    assert "largest uncached 30 at call 2" in result.output
    assert result.output.count("Context Window Left") == 2
    assert "context_window_left_percent" in result.output
    assert "Effective Input Budget" in result.output
    assert "Conversation Context Left" in result.output
    assert "15 tokens   ↓ 10   ↑ 5   context left: 98.4%" in result.output
    assert "Session usage:" not in result.output
    assert captured["instruction"] == "hello"


def test_chat_turn_usage_line_formats_compact_totals_and_context_warning() -> None:
    class _Summary:
        @staticmethod
        def totals() -> dict[str, object]:
            return {
                "prompt_tokens": 150_334,
                "completion_tokens": 3_422,
                "total_tokens": 153_756,
                "cost_usd": None,
                "known_cost_calls": 0,
                "unknown_cost_calls": 16,
                "api_usage_calls": 0,
                "estimate_usage_calls": 16,
                "corrected_usage_calls": 16,
            }

    session = SimpleNamespace(
        _usage_hud_enabled=True,
        _hud_context_cache=SimpleNamespace(percent_left=99.4),
        usage_summary=_Summary(),
    )
    assert cli_mod._chat_turn_usage_line(session) == (
        "153.8k [dim]tokens[/dim]   "
        "[dim]↓[/dim] 150.3k   "
        "[dim]↑[/dim] 3,422   "
        "[dim]context left:[/dim] 99.4%",
        None,
    )
    assert cli_mod._chat_turn_usage_style(session) == "dim"
    assert cli_mod._chat_context_hud_value(session) == "99.4%"

    session._hud_context_cache = SimpleNamespace(
        percent_left=72.2,
        dynamic_context_percent_left=93.3,
        effective_percent_left=8.5,
    )
    assert cli_mod._chat_turn_usage_line(session) == (
        "153.8k [dim]tokens[/dim]   [dim]↓[/dim] 150.3k   "
        "[dim]↑[/dim] 3,422   [dim]context left:[/dim] 93.3%",
        "\u26a0\ufe0e  Critical input budget — run /compact to reduce context",
    )
    assert cli_mod._chat_turn_usage_style(session) == "bold red"
    assert cli_mod._chat_context_hud_value(session) == "93.3% !!"

    session._hud_context_cache = SimpleNamespace(
        percent_left=55.0,
        dynamic_context_percent_left=44.0,
        effective_percent_left=15.0,
    )
    assert cli_mod._chat_turn_usage_line(session) == (
        "153.8k [dim]tokens[/dim]   "
        "[dim]↓[/dim] 150.3k   "
        "[dim]↑[/dim] 3,422   "
        "[dim]context left:[/dim] 44.0%",
        "\u26a0\ufe0e  Low input budget — run /compact to reduce context",
    )
    assert cli_mod._chat_turn_usage_style(session) == "yellow"
    assert cli_mod._chat_context_hud_value(session) == "44.0% !"

    session._hud_context_cache = SimpleNamespace(percent_left=None)
    assert cli_mod._chat_turn_usage_line(session) == (
        "153.8k [dim]tokens[/dim]   [dim]↓[/dim] 150.3k   [dim]↑[/dim] 3,422",
        None,
    )

    session._hud_context_cache = SimpleNamespace(percent_left=99.4)
    session.usage_summary = SimpleNamespace(
        totals=lambda: {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
    )
    assert cli_mod._chat_turn_usage_line(session) == ("[dim]context left:[/dim] 99.4%", None)


def test_chat_compact_command_forces_compaction(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummyClient:
        model = "test-model"
        temperature = 1.0

    class _DummyUsageSummary:
        @staticmethod
        def totals() -> dict[str, object]:
            return {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": None,
                "known_cost_calls": 0,
                "unknown_cost_calls": 0,
                "api_usage_calls": 0,
                "estimate_usage_calls": 0,
            }

    class _DummyCompactorClient:
        model = "compactor-model"

    class _DummyCompactorState:
        history_chunk_index = 0
        pins: list[dict[str, object]] = []
        pinned_prefix_len = 1

    class _DummyCompactor:
        compactor_client = _DummyCompactorClient()
        state = _DummyCompactorState()

        def compact_now(
            self,
            *,
            messages: list[dict[str, Any]],
            tool_list: list[dict[str, Any]] | None,
            main_model: str,
            cache_policy: dict[str, Any] | None = None,
            focus: str | None = None,
            request_messages_builder: Any = None,
        ) -> tuple[list[dict[str, Any]], bool]:
            captured["focus"] = focus
            captured["main_model"] = main_model
            captured["tool_list"] = tool_list
            captured["cache_policy"] = cache_policy
            self.state.history_chunk_index += 1
            self.state.pins = [{"text": "pin"}]
            return messages[:-1], True

    class _DummyContext:
        model_name = "test-model"
        source = "fallback"
        max_input_tokens = 8192
        used_input_tokens = 128
        remaining_tokens = 8064
        percent_left = 98.4

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        usage_summary = _DummyUsageSummary()
        conversation_compactor = _DummyCompactor()
        root = tmp_path
        cfg = AppConfig(model="test-model")
        stream = False
        mode = "review"
        tool_list: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old reply"},
        ]
        request_context_measurement: object | None = object()

        def invalidate_request_context(self, *, reason: str) -> None:
            captured["invalidation_reason"] = reason
            self.request_context_measurement = None

        def context_left(self) -> _DummyContext:
            return _DummyContext()

        def run_turn(
            self, _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            captured["run_turn_called"] = True
            _ = image_paths
            return 0

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/compact focus text\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Compaction" in result.output
    assert "focus text" in result.output
    assert captured["focus"] == "focus text"
    assert captured["main_model"] == "test-model"
    assert captured["invalidation_reason"] == "manual_compaction"
    assert "run_turn_called" not in captured


def test_chat_compact_unavailable_when_compaction_disabled(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()

    class _DummyStore:
        session_id = "sid"

    class _DummyClient:
        model = "test-model"
        temperature = 1.0

    class _DummyUsageSummary:
        @staticmethod
        def totals() -> dict[str, object]:
            return {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": None,
                "known_cost_calls": 0,
                "unknown_cost_calls": 0,
                "api_usage_calls": 0,
                "estimate_usage_calls": 0,
            }

    class _DummyContext:
        model_name = "test-model"
        source = "fallback"
        max_input_tokens = 8192
        used_input_tokens = 10
        remaining_tokens = 8182
        percent_left = 99.8

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        usage_summary = _DummyUsageSummary()
        conversation_compactor = None
        root = tmp_path
        cfg = AppConfig(model="test-model")
        stream = False
        mode = "review"
        tool_list: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = [{"role": "system", "content": "system"}]

        @staticmethod
        def context_left() -> _DummyContext:
            return _DummyContext()

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/compact\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Compaction unavailable for this session (disabled or not supported)." in result.output


def test_chat_compact_blocked_in_forge_mode(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummyClient:
        model = "test-model"
        temperature = 1.0

    class _DummyUsageSummary:
        @staticmethod
        def totals() -> dict[str, object]:
            return {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": None,
                "known_cost_calls": 0,
                "unknown_cost_calls": 0,
                "api_usage_calls": 0,
                "estimate_usage_calls": 0,
            }

    class _DummyCompactorClient:
        model = "compactor-model"

    class _DummyCompactor:
        compactor_client = _DummyCompactorClient()
        state = SimpleNamespace(history_chunk_index=0, pins=[], pinned_prefix_len=1)

        def compact_now(
            self,
            *,
            messages: list[dict[str, Any]],
            tool_list: list[dict[str, Any]] | None,
            main_model: str,
            cache_policy: dict[str, Any] | None = None,
            focus: str | None = None,
        ) -> tuple[list[dict[str, Any]], bool]:
            captured["compact_called"] = True
            _ = messages, tool_list, main_model, cache_policy, focus
            return messages, False

    class _DummyContext:
        model_name = "test-model"
        source = "fallback"
        max_input_tokens = 8192
        used_input_tokens = 10
        remaining_tokens = 8182
        percent_left = 99.8

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        usage_summary = _DummyUsageSummary()
        conversation_compactor = _DummyCompactor()
        root = tmp_path
        cfg = AppConfig(model="test-model")
        stream = False
        mode = "review"
        tool_list: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = [{"role": "system", "content": "system"}]

        @staticmethod
        def context_left() -> _DummyContext:
            return _DummyContext()

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    def fake_enter_forge_mode(
        *, root: Path, console: Console, forge_state: Any, **_kwargs: Any
    ) -> bool:
        _ = root, console
        plan_dir = tmp_path / ".alysis" / "runs" / "wf" / "plan"
        notes_dir = plan_dir / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        forge_state.ui_mode = "forge"
        forge_state.paths = SimpleNamespace(
            plan_json_path=plan_dir / "plan.json",
            plan_md_path=plan_dir / "PLAN.md",
            notes_dir=notes_dir,
            notes_path=notes_dir / "user_notes.md",
        )
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "x",
            "summary": "x",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        return True

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/compact\n/back\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Compaction is disabled in Forge." in result.output
    assert "compact_called" not in captured


def test_chat_compact_nothing_to_compact_prints_message(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()

    class _DummyStore:
        session_id = "sid"

    class _DummyClient:
        model = "test-model"
        temperature = 1.0

    class _DummyUsageSummary:
        @staticmethod
        def totals() -> dict[str, object]:
            return {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": None,
                "known_cost_calls": 0,
                "unknown_cost_calls": 0,
                "api_usage_calls": 0,
                "estimate_usage_calls": 0,
            }

    class _DummyCompactorClient:
        model = "compactor-model"

    class _DummyCompactor:
        compactor_client = _DummyCompactorClient()
        state = SimpleNamespace(history_chunk_index=0, pins=[], pinned_prefix_len=1)

        def compact_now(
            self,
            *,
            messages: list[dict[str, Any]],
            tool_list: list[dict[str, Any]] | None,
            main_model: str,
            cache_policy: dict[str, Any] | None = None,
            focus: str | None = None,
            request_messages_builder: Any = None,
        ) -> tuple[list[dict[str, Any]], bool]:
            _ = tool_list, main_model, cache_policy, focus, request_messages_builder
            return messages, False

    class _DummyContext:
        model_name = "test-model"
        source = "fallback"
        max_input_tokens = 8192
        used_input_tokens = 10
        remaining_tokens = 8182
        percent_left = 99.8

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        usage_summary = _DummyUsageSummary()
        conversation_compactor = _DummyCompactor()
        root = tmp_path
        cfg = AppConfig(model="test-model")
        stream = False
        mode = "review"
        tool_list: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = [{"role": "system", "content": "system"}]

        @staticmethod
        def context_left() -> _DummyContext:
            return _DummyContext()

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/compact\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Compaction" in result.output
    assert "Nothing to compact (no eligible history beyond recent window)." in result.output


def test_chat_usage_hud_off_keeps_usage_tracking(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()

    class _DummyStore:
        session_id = "sid"

    class _DummyUsageSummary:
        def by_model_rows(self) -> list[dict[str, object]]:
            return [
                {
                    "model": "gpt-5-nano",
                    "prompt_tokens": 30,
                    "completion_tokens": 10,
                    "total_tokens": 40,
                    "cost_usd": 0.0123,
                    "known_cost_calls": 1,
                    "unknown_cost_count": 0,
                    "api_usage_calls": 1,
                    "estimate_usage_calls": 0,
                }
            ]

        def totals(self) -> dict[str, object]:
            return {
                "prompt_tokens": 30,
                "completion_tokens": 10,
                "total_tokens": 40,
                "cost_usd": 0.0123,
                "known_cost_usd": 0.0123,
                "known_cost_calls": 1,
                "unknown_cost_calls": 0,
                "api_usage_calls": 1,
                "estimate_usage_calls": 0,
            }

    class _DummyContext:
        model_name = "gpt-5-nano"
        source = "fallback"
        max_input_tokens = 8192
        used_input_tokens = 128
        remaining_tokens = 8064
        percent_left = 98.4

    class _DummySession:
        store = _DummyStore()
        usage_summary = _DummyUsageSummary()
        stream = False
        mode = "review"

        def context_left(self) -> _DummyContext:
            return _DummyContext()

        def run_turn(
            self, _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            _ = image_paths
            return 0

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/usage hud off\nhello\n/usage\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Usage HUD set for this session: off" in result.output
    assert "context left: 98.4%" in result.output
    assert "Session status: ctx_left=" not in result.output
    assert "Session usage:" not in result.output
    assert "Usage" in result.output


def test_chat_usage_hud_subcommand_without_arg_uses_picker_selection(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, bool] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()
        stream = False
        mode = "review"

        def run_turn(
            self, _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            captured["usage_hud_enabled"] = bool(getattr(self, "_usage_hud_enabled", True))
            return 0

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(
        cli_mod,
        "_select_chat_usage_hud_interactive",
        lambda **_kwargs: ("off", True),
    )
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/usage hud\nhello\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Usage HUD set for this session: off" in result.output
    assert captured["usage_hud_enabled"] is False


def test_chat_removed_usage_hud_command_suggests_usage(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()
        stream = False
        mode = "review"

        def run_turn(
            self, _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            _ = image_paths
            return 0

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/usage-hud\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Unknown command: /usage-hud." in result.output
    assert "Did you mean /usage? Try /help." in result.output


def test_chat_toolbar_command_shows_updates_and_resets_session_items(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, list[str]] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()
        cfg = AppConfig(model="test-model")
        stream = False
        mode = "review"

        def run_turn(
            self, _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            captured["toolbar_items"] = list(self.cfg.toolbar_items)
            return 0

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/toolbar\n/toolbar add tokens\n/toolbar remove ctx\n/toolbar reset\nhello\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Toolbar Items" in result.output
    assert "Active: mode, model, ctx, subagents" in result.output
    assert "Toolbar item added for this session: tokens" in result.output
    assert "Toolbar item removed for this session: ctx" in result.output
    assert "Toolbar items reset for this session: mode, model, ctx, subagents" in result.output
    assert captured["toolbar_items"] == ["mode", "model", "ctx", "subagents"]


def test_chat_toolbar_command_save_persists_and_rejects_invalid_item(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()
        cfg = AppConfig(model="test-model")
        stream = False
        mode = "review"

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/toolbar add trace\n/toolbar save\n/toolbar add nope\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Toolbar item added for this session: trace" in result.output
    assert "Saved toolbar settings:" in result.output
    assert "Invalid toolbar item:" in result.output

    payload = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert payload["toolbar_items"] == ["mode", "model", "ctx", "subagents", "trace"]


def test_print_chat_context_shows_zero_remaining_to_budget_not_na(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class _DummyContext:
        model_name = "test-model"
        source = "fallback"
        max_input_tokens = 4096
        used_input_tokens = 4096
        remaining_tokens = 0
        percent_left = 0.0

    class _DummyStore:
        session_id = "sid"

    class _DummyClient:
        model = "test-model"

    class _DummyRegistry:
        def get(self, _model_name: str) -> ModelMeta:
            return ModelMeta(
                model_name="test-model",
                context_window_tokens=4096,
                max_output_tokens=512,
                input_cost_per_token=None,
                output_cost_per_token=None,
                raw_metadata={},
                source="fallback",
            )

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        model_registry = _DummyRegistry()
        cfg = AppConfig(model="test-model")
        root = tmp_path
        messages = [{"role": "user", "content": "x"}]
        tool_list: list[dict[str, Any]] | None = None
        conversation_compactor = None

        @staticmethod
        def context_left() -> _DummyContext:
            return _DummyContext()

    monkeypatch.setattr(cli_mod, "compute_input_budget", lambda _meta, safety_margin=0: 100)
    monkeypatch.setattr(
        cli_mod,
        "estimate_tokens",
        lambda text: 100 if text.strip() else 0,
    )
    console = Console(record=True, width=140)
    cli_mod._print_chat_context(console=console, session=_DummySession())
    rendered = console.export_text()
    assert "remaining_to_budget" in rendered
    remaining_line = next(
        (line for line in rendered.splitlines() if "remaining_to_budget" in line),
        "",
    )
    assert remaining_line
    assert "n/a" not in remaining_line
    assert "0" in remaining_line


def test_print_chat_context_uses_single_request_total_for_budget_table(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class _DummyContext:
        model_name = "test-model"
        source = "fallback"
        max_input_tokens = 4096
        used_input_tokens = 80
        remaining_tokens = 4016
        percent_left = 98.0
        effective_input_budget = 100
        effective_remaining_tokens = 20
        effective_percent_left = 20.0
        startup_baseline_tokens = 60
        dynamic_context_budget_tokens = 40
        dynamic_context_used_tokens = 20
        dynamic_context_remaining_tokens = 20
        dynamic_context_percent_left = 50.0

    class _DummyStore:
        session_id = "sid"

    class _DummyClient:
        model = "test-model"

    class _DummyRegistry:
        def get(self, _model_name: str) -> ModelMeta:
            return ModelMeta(
                model_name="test-model",
                context_window_tokens=4096,
                max_output_tokens=512,
                input_cost_per_token=None,
                output_cost_per_token=None,
                raw_metadata={},
                source="fallback",
            )

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        model_registry = _DummyRegistry()
        cfg = AppConfig(model="test-model")
        root = tmp_path
        messages = [{"role": "user", "content": "x"}]
        tool_list = [{"type": "function", "function": {"name": "demo"}}]
        conversation_compactor = None
        pinned_prefix_len = 0

        @staticmethod
        def context_left() -> _DummyContext:
            return _DummyContext()

    monkeypatch.setattr(
        cli_mod,
        "estimate_tokens",
        lambda text: 150 if text.strip() else 0,
    )
    monkeypatch.setattr(
        cli_mod,
        "estimate_request_token_breakdown",
        lambda **_kwargs: SimpleNamespace(total_tokens=80, tool_schema_tokens=10),
    )

    console = Console(record=True, width=140)
    cli_mod._print_chat_context(console=console, session=_DummySession())
    rendered = console.export_text()

    total_lines = [
        line for line in rendered.splitlines() if "estimated_total_request_tokens" in line
    ]
    assert total_lines
    assert all("80" in line for line in total_lines)
    assert all("150" not in line for line in total_lines)
    assert "estimated_messages_tokens" in rendered
    assert "70" in next(
        line for line in rendered.splitlines() if "estimated_messages_tokens" in line
    )
    assert "estimated_tools_tokens" in rendered
    assert "10" in next(line for line in rendered.splitlines() if "estimated_tools_tokens" in line)
    assert "Conversation Context Left" in rendered
    assert "dynamic_context_left_percent" in rendered
    assert "50.0%" in rendered
    assert "Request estimate exceeds effective input budget" not in rendered


def test_chat_status_and_model_info_show_bundled_catalog_registry_diagnostics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()

    class _DummyStore:
        session_id = "sid"

    class _DummyClient:
        model = "gpt-5-nano"
        temperature = 1.0

    class _DummyRegistry:
        last_error = "bundled model catalog missing"

        def get(self, _model_name: str) -> ModelMeta:
            return ModelMeta(
                model_name="gpt-5-nano",
                context_window_tokens=128000,
                max_output_tokens=4096,
                input_cost_per_token=None,
                output_cost_per_token=None,
                raw_metadata={},
                source="bundled_litellm_snapshot",
                field_sources={"context_window_tokens": "bundled_litellm_snapshot"},
            )

    class _DummyProvenance:
        error = None
        upstream_commit_sha = "10a48f7655225b0dc765d5521839a8bf621805d9"
        fetched_at_utc = "2026-03-25T13:46:29Z"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        model_registry = _DummyRegistry()
        stream = False
        mode = "review"

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(
        cli_mod,
        "get_bundled_model_catalog_provenance",
        lambda: _DummyProvenance(),
    )
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/status\n/model-info\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "model_metadata_source" in result.output
    assert "bundled_litellm_snapshot" in result.output
    assert "model_metadata_error" in result.output
    assert "bundled model catalog missing" in result.output
    assert "model_metadata_warning" in result.output
    assert "context_window_source" in result.output
    assert "supports_vision" in result.output
    assert "bundled_catalog_commit" in result.output
    assert "10a48f7655225b0dc765d5521839a8bf621805d9" in result.output
    assert "bundled_catalog_fetched_at" in result.output
    assert "2026-03-25T13:46:29Z" in result.output


def test_chat_config_lists_tracked_models(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()

    class _DummyStore:
        session_id = "sid"

    class _DummyClient:
        model = "active-model"
        temperature = 1.0

    class _DummyUsageSummary:
        @staticmethod
        def by_model_rows() -> list[dict[str, object]]:
            return [{"model": "used-model"}, {"model": "used-model-2"}]

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        cfg = AppConfig(model="active-model")
        usage_summary = _DummyUsageSummary()

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/config\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Model Config" in result.output
    assert "active-model" in result.output
    assert "used-model" in result.output


def test_chat_config_set_saves_model_metadata_overrides(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()

    class _DummyStore:
        session_id = "sid"

    class _DummyClient:
        model = "acme/super-model"
        temperature = 1.0

    class _DummyUsageSummary:
        @staticmethod
        def by_model_rows() -> list[dict[str, object]]:
            return []

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        cfg = AppConfig(model="acme/super-model")
        usage_summary = _DummyUsageSummary()

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/config set 1 200000 8000 false 0.000001 0.000002\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Saved model metadata override for acme/super-model." in result.output

    cfg_payload = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    model_cfg = cfg_payload["model_metadata_overrides"]["models"]["acme/super-model"]
    assert model_cfg["context_window_tokens"] == 200000
    assert model_cfg["max_output_tokens"] == 8000
    assert model_cfg["supports_vision"] is False
    assert model_cfg["input_cost_per_token"] == 0.000001
    assert model_cfg["output_cost_per_token"] == 0.000002


def test_chat_shows_welcome_panel_without_status_table(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="exit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Alysis Code" in result.output
    assert "Chat Status" not in result.output


def test_chat_unknown_command_suggests_closest_match(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/permissons\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Did you mean /permissions?" in result.output


def test_chat_removed_help_aliases_and_colon_picker_fall_through(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, str] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def run_turn(
            self, instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            captured["instruction"] = instruction
            return 0

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/h\n/?\n:\nhello\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Unknown command: /h. Try /help." in result.output
    assert "Unknown command: /?. Try /help." in result.output
    assert "Unknown command: :. Try /help." in result.output
    assert captured["instruction"] == "hello"


def test_chat_mode_command_accepts_numeric_shortcut(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, str] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummyClient:
        model = "test-model"
        temperature = 1.0

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"

        def run_turn(
            self, _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            captured["mode"] = self.mode
            return 0

        def close(self) -> None:
            return None

    rebuilt: dict[str, str] = {}

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    def fake_rebuild_session_tools_for_mode(*, session: _DummySession, mode: str) -> None:
        rebuilt["mode"] = mode

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(
        cli_mod,
        "_rebuild_session_tools_for_mode",
        fake_rebuild_session_tools_for_mode,
    )
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/permissions 2\nhello\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert "Thinking... Press Esc to interrupt." not in result.output
    assert rebuilt["mode"] == "auto"
    assert captured["mode"] == "auto"


def test_chat_mode_command_accepts_fullaccess_numeric_alias(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, str] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummyClient:
        model = "test-model"
        temperature = 1.0

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"

        def run_turn(
            self, _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            captured["mode"] = self.mode
            return 0

        def close(self) -> None:
            return None

    rebuilt: dict[str, str] = {}

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    def fake_rebuild_session_tools_for_mode(*, session: _DummySession, mode: str) -> None:
        rebuilt["mode"] = mode

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(
        cli_mod,
        "_rebuild_session_tools_for_mode",
        fake_rebuild_session_tools_for_mode,
    )
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/permissions 4\nhello\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert rebuilt["mode"] == "fullaccess"
    assert captured["mode"] == "fullaccess"
    assert "full (fullaccess) disables write/shell safety guards" in result.output


def test_chat_turn_keyboard_interrupt_is_handled(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    calls: dict[str, int] = {"run_turn": 0}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()
        stream = False
        mode = "review"

        def run_turn(
            self, _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            calls["run_turn"] += 1
            raise KeyboardInterrupt

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="hello\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert calls["run_turn"] == 1
    assert "Interrupted current turn." in result.output


def test_chat_turn_keyboard_interrupt_finishes_surface_activity(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"done_calls": []}

    class _DummyStore:
        session_id = "sid"

    class _DummySurface:
        trace_level = "compact"

        @staticmethod
        def on_user_message(_text: str) -> None:
            return None

        @staticmethod
        def on_progress_update(_message: str) -> None:
            return None

        @staticmethod
        def on_assistant_message_done(text: str) -> None:
            captured["done_calls"].append(text)

    class _DummySession:
        store = _DummyStore()
        stream = False
        mode = "review"
        surface = _DummySurface()

        def run_turn(
            self, instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            _ = image_paths
            self.surface.on_user_message(instruction)
            self.surface.on_progress_update("Understanding your request.")
            raise KeyboardInterrupt

        def close(self) -> None:
            return None

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="hello\nexit\n",
        env=env,
    )

    assert result.exit_code == 0
    assert captured["done_calls"] == [""]


def test_chat_llm_error_is_recoverable_without_traceback(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    calls: dict[str, int] = {"run_turn": 0}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()
        stream = True
        mode = "review"

        def __init__(self, *, console: Console | None) -> None:
            safe_console = console or Console()
            self.surface = cli_mod._make_rich_surface(
                console=safe_console,
                show_status_line=False,
            )

        def run_turn(
            self, _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            _ = image_paths
            calls["run_turn"] += 1
            raise LLMError("LLM request failed: connection timed out")

        def close(self) -> None:
            return None

    def fake_create_session(*, console: Console | None = None, **_kwargs) -> _DummySession:
        return _DummySession(console=console)

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="hello\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert calls["run_turn"] == 1
    assert "The model provider did not respond before the request timed out." in result.output
    assert "LLM request failed: connection timed out" not in result.output
    assert "Check base URL, API key, and network connectivity." in result.output
    assert "Traceback" not in result.output


def test_chat_llm_error_reports_tool_transcript_problems_without_network_hint(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    calls: dict[str, int] = {"run_turn": 0}
    broken_tool_error = (
        "LLM error 400: {"
        ' "error": {'
        ' "message": "An assistant message with \'tool_calls\' must be followed by tool messages '
        "responding to each 'tool_call_id'. The following tool_call_ids did not have response "
        'messages: call_123",'
        ' "type": "invalid_request_error"'
        " }"
        "}"
    )

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()
        stream = True
        mode = "review"

        def __init__(self, *, console: Console | None) -> None:
            safe_console = console or Console()
            self.surface = cli_mod._make_rich_surface(
                console=safe_console,
                show_status_line=False,
            )

        def run_turn(
            self, _instruction: str, *, image_paths: list[str] | None = None, **_kwargs: object
        ) -> int:
            _ = image_paths
            calls["run_turn"] += 1
            raise LLMError(broken_tool_error)

        def close(self) -> None:
            return None

    def fake_create_session(*, console: Console | None = None, **_kwargs) -> _DummySession:
        return _DummySession(console=console)

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="hello\nexit\n",
        env=env,
    )
    normalized_output = _normalize_terminal_output(result.output)
    assert result.exit_code == 0
    assert calls["run_turn"] == 1
    assert "did not have response messages: call_123" in normalized_output
    assert "This session's tool transcript is incomplete or malformed." in normalized_output
    assert (
        "Retry in a new session. If it repeats, inspect resume/compaction around missing tool responses."
        in normalized_output
    )
    assert "Check base URL, API key, and network connectivity." not in normalized_output
    assert "Traceback" not in normalized_output


def test_chat_llm_error_panel_classifies_tool_transcript_errors() -> None:
    panel = cli_mod._chat_llm_error_panel(
        message=(
            "LLM error 400: invalid_request_error: "
            "An assistant message with 'tool_calls' must be followed by tool messages "
            "responding to each 'tool_call_id'. The following tool_call_ids did not have "
            "response messages: call_abc"
        )
    )

    assert panel.title == "Tool Transcript Error"
    assert "This session's tool transcript is incomplete or malformed." in str(panel.renderable)
    assert (
        "Retry in a new session. If it repeats, inspect resume/compaction around missing tool responses."
        in str(panel.renderable)
    )
    assert "Check base URL, API key, and network connectivity." not in str(panel.renderable)


def test_chat_llm_error_panel_humanizes_provider_auth_json() -> None:
    panel = cli_mod._chat_llm_error_panel(
        message=(
            'LLM error 401: {"error":{"message":"Incorrect API key provided: sk-test. '
            'You can find your API key at https://platform.openai.com/account/api-keys.",'
            '"type":"invalid_request_error","code":"invalid_api_key"}}'
        )
    )

    renderable = str(panel.renderable)
    assert panel.title == "Network/Model Error"
    assert "Your API key was rejected (401). Update it with /config." in renderable
    assert "https://platform.openai.com/account/api-keys" not in renderable
    assert "LLM error 401" not in renderable
    assert "sk-test" not in renderable
    assert '{"error"' not in renderable


def test_chat_llm_error_panel_humanizes_unsupported_model_json() -> None:
    panel = cli_mod._chat_llm_error_panel(
        message=(
            'LLM error 400: {"error":{"code":"400","message":"Param Incorrect",'
            '"param":"Not supported model MiMo-V2-Pro"}}'
        )
    )

    renderable = str(panel.renderable)
    assert "The provider rejected model 'MiMo-V2-Pro' for this request (400)." in renderable
    assert "Check the model id or provider profile in /config." in renderable
    assert '{"error"' not in renderable


def test_chat_llm_error_panel_humanizes_connection_failures() -> None:
    panel = cli_mod._chat_llm_error_panel(
        message="LLM request failed for http://127.0.0.1:1/v1: Connection refused"
    )

    renderable = str(panel.renderable)
    assert panel.title == "Network/Model Error"
    assert "Can't reach the model provider at 127.0.0.1:1." in renderable
    assert "Check the profile base URL in /config and try again." in renderable


def test_chat_turn_interrupt_monitor_restores_terminal_for_nested_prompt(monkeypatch) -> None:
    read_calls: list[tuple[int, int]] = []
    kill_calls: list[tuple[int, int]] = []
    select_calls: list[float] = []
    cbreak_calls: list[int] = []
    restore_calls: list[tuple[int, int, tuple[str]]] = []
    allow_select_return = threading.Event()
    select_entered = threading.Event()
    select_count = 0

    class _FakeStdin:
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return 9

    fake_termios = SimpleNamespace(
        tcgetattr=lambda _fd: ("orig",),
        tcsetattr=lambda fd, when, attrs: restore_calls.append((fd, when, attrs)),
        TCSADRAIN=1,
    )
    fake_tty = SimpleNamespace(setcbreak=lambda fd: cbreak_calls.append(fd))

    def fake_select(
        _reads: object, _writes: object, _errors: object, timeout: float
    ) -> tuple[list[int], list[int], list[int]]:
        nonlocal select_count
        select_calls.append(timeout)
        if select_count == 0:
            select_count += 1
            select_entered.set()
            allow_select_return.wait(timeout=1.0)
            return ([9], [], [])
        time.sleep(min(timeout, 0.01))
        return ([], [], [])

    monkeypatch.setattr(cli_mod.sys, "stdin", _FakeStdin())
    monkeypatch.setattr(cli_mod.os, "name", "posix", raising=False)
    monkeypatch.setattr(cli_mod.os, "read", lambda fd, size: read_calls.append((fd, size)) or b"y")
    monkeypatch.setattr(cli_mod.os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))
    monkeypatch.setitem(sys.modules, "select", SimpleNamespace(select=fake_select))
    monkeypatch.setitem(sys.modules, "termios", fake_termios)
    monkeypatch.setitem(sys.modules, "tty", fake_tty)

    with cli_mod._chat_turn_interrupt_monitor():
        assert select_entered.wait(timeout=1.0)
        with interactive_prompt_guard():
            allow_select_return.set()
            time.sleep(0.12)

    assert read_calls == []
    assert kill_calls == []
    assert cbreak_calls == [9]
    assert select_calls
    assert restore_calls == [(9, 1, ("orig",))]


def test_chat_turn_interrupt_monitor_does_not_restore_terminal_for_raw_owned_prompt(
    monkeypatch,
) -> None:
    read_calls: list[tuple[int, int]] = []
    kill_calls: list[tuple[int, int]] = []
    select_calls: list[float] = []
    cbreak_calls: list[int] = []
    restore_calls: list[tuple[int, int, tuple[str]]] = []
    allow_select_return = threading.Event()
    select_entered = threading.Event()
    select_count = 0

    class _FakeStdin:
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return 9

    fake_termios = SimpleNamespace(
        tcgetattr=lambda _fd: ("orig",),
        tcsetattr=lambda fd, when, attrs: restore_calls.append((fd, when, attrs)),
        TCSADRAIN=1,
    )
    fake_tty = SimpleNamespace(setcbreak=lambda fd: cbreak_calls.append(fd))

    def fake_select(
        _reads: object, _writes: object, _errors: object, timeout: float
    ) -> tuple[list[int], list[int], list[int]]:
        nonlocal select_count
        select_calls.append(timeout)
        if select_count == 0:
            select_count += 1
            select_entered.set()
            allow_select_return.wait(timeout=1.0)
            return ([9], [], [])
        time.sleep(min(timeout, 0.01))
        return ([], [], [])

    monkeypatch.setattr(cli_mod.sys, "stdin", _FakeStdin())
    monkeypatch.setattr(cli_mod.os, "name", "posix", raising=False)
    monkeypatch.setattr(cli_mod.os, "read", lambda fd, size: read_calls.append((fd, size)) or b"y")
    monkeypatch.setattr(cli_mod.os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))
    monkeypatch.setitem(sys.modules, "select", SimpleNamespace(select=fake_select))
    monkeypatch.setitem(sys.modules, "termios", fake_termios)
    monkeypatch.setitem(sys.modules, "tty", fake_tty)

    with cli_mod._chat_turn_interrupt_monitor():
        assert select_entered.wait(timeout=1.0)
        with interactive_prompt_guard(owns_terminal=True):
            allow_select_return.set()
            time.sleep(0.12)
            assert restore_calls == []

    assert read_calls == []
    assert kill_calls == []
    assert cbreak_calls == [9]
    assert select_calls
    assert restore_calls == [(9, 1, ("orig",))]


def test_chat_turn_interrupt_monitor_keeps_escape_interrupt_behavior(monkeypatch) -> None:
    read_calls: list[tuple[int, int]] = []
    kill_calls: list[tuple[int, int]] = []
    select_calls: list[float] = []

    class _FakeStdin:
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return 9

    fake_termios = SimpleNamespace(
        tcgetattr=lambda _fd: ("orig",),
        tcsetattr=lambda *_args, **_kwargs: None,
        TCSADRAIN=1,
    )
    fake_tty = SimpleNamespace(setcbreak=lambda _fd: None)

    def fake_select(
        _reads: object, _writes: object, _errors: object, timeout: float
    ) -> tuple[list[int], list[int], list[int]]:
        select_calls.append(timeout)
        return ([9], [], [])

    monkeypatch.setattr(cli_mod.sys, "stdin", _FakeStdin())
    monkeypatch.setattr(cli_mod.os, "name", "posix", raising=False)
    monkeypatch.setattr(
        cli_mod.os, "read", lambda fd, size: read_calls.append((fd, size)) or b"\x1b"
    )
    token = InteractiveCancellationToken()

    def _record_kill(pid: int, sig: int) -> None:
        assert token.is_cancelled is True
        kill_calls.append((pid, sig))

    monkeypatch.setattr(cli_mod.os, "kill", _record_kill)
    monkeypatch.setitem(sys.modules, "select", SimpleNamespace(select=fake_select))
    monkeypatch.setitem(sys.modules, "termios", fake_termios)
    monkeypatch.setitem(sys.modules, "tty", fake_tty)

    with cli_mod._chat_turn_interrupt_monitor(cancellation_token=token):
        time.sleep(0.12)

    assert token.is_cancelled is True
    assert read_calls == [(9, 1)]
    assert len(kill_calls) == 1
    assert select_calls


def test_classic_chat_uses_a_fresh_cancellation_token_for_each_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    tokens: list[InteractiveCancellationToken] = []

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()
        stream = False
        mode = "review"

        def run_turn(
            self,
            _instruction: str,
            *,
            image_paths: list[str] | None = None,
            cancellation_token: InteractiveCancellationToken | None = None,
        ) -> int:
            _ = image_paths
            assert cancellation_token is not None
            tokens.append(cancellation_token)
            if len(tokens) == 1:
                raise KeyboardInterrupt
            assert cancellation_token.is_cancelled is False
            return 0

        def close(self) -> None:
            return None

    class _FakePromptSession:
        def __init__(self) -> None:
            self._responses = iter(["first", "second", "exit"])

        def prompt(self, *_args: object, **_kwargs: object) -> str:
            return next(self._responses)

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(
        cli_mod,
        "_maybe_make_chat_prompt_session",
        lambda **_kwargs: _FakePromptSession(),
    )

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="",
        env={
            "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
            "ALYSIS_DATA_DIR": os.fspath(tmp_path),
            "ALYSIS_TUI": "0",
        },
    )

    assert result.exit_code == 0
    assert len(tokens) == 2
    assert tokens[0] is not tokens[1]
    assert tokens[0].is_cancelled is True
    assert tokens[1].is_cancelled is False


def test_chat_prompt_session_does_not_pass_erase_when_done_kwarg(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"prompt_kwargs": [], "run_turn_calls": []}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()
        stream = False
        mode = "review"

        def run_turn(
            self,
            instruction: str,
            *,
            image_paths: list[str] | None = None,
            cancellation_token: InteractiveCancellationToken | None = None,
        ) -> int:
            assert cancellation_token is not None
            captured["run_turn_calls"].append((instruction, image_paths))
            return 0

        def close(self) -> None:
            return None

    class _FakePromptSession:
        def __init__(self) -> None:
            self._responses = iter(["hello", "exit"])

        def prompt(self, *_args: object, **kwargs: object) -> str:
            captured["prompt_kwargs"].append(dict(kwargs))
            return next(self._responses)

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(
        cli_mod,
        "_maybe_make_chat_prompt_session",
        lambda **_kwargs: _FakePromptSession(),
    )

    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="",
        env=env,
    )

    assert result.exit_code == 0
    assert len(captured["run_turn_calls"]) == 1
    assert captured["run_turn_calls"][0][0] == "hello"
    assert len(captured["prompt_kwargs"]) >= 2
    assert all("erase_when_done" not in kwargs for kwargs in captured["prompt_kwargs"])


def test_maybe_make_chat_prompt_session_sets_constructor_erase_when_done(
    tmp_path: Path, monkeypatch
) -> None:
    captured: list[dict[str, Any]] = []

    class _PromptSessionStub:
        def __init__(self, **kwargs: Any) -> None:
            captured.append(dict(kwargs))
            self.app = SimpleNamespace(
                output=SimpleNamespace(responds_to_cpr=lambda: True),
                ttimeoutlen=0.5,
            )

    import prompt_toolkit  # type: ignore[import-not-found]

    monkeypatch.setattr(prompt_toolkit, "PromptSession", _PromptSessionStub)
    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path))

    session = cli_mod._maybe_make_chat_prompt_session(
        console=Console(),
        root=Path("."),
        pending_images=[],
        forge_state=cli_mod._ForgeChatState(),
    )

    assert session is not None
    assert len(captured) == 1
    assert captured[0].get("erase_when_done") is True
    assert session._alysis_erase_when_done is True  # type: ignore[attr-defined]
    assert session.app.ttimeoutlen == cli_mod._CHAT_PROMPT_ESCAPE_SEQUENCE_TIMEOUT_S


def test_maybe_make_chat_prompt_session_uses_session_registries_for_completions(
    tmp_path: Path, monkeypatch
) -> None:
    captured: list[dict[str, Any]] = []

    class _PromptSessionStub:
        def __init__(self, **kwargs: Any) -> None:
            captured.append(dict(kwargs))
            self.app = SimpleNamespace(
                output=SimpleNamespace(responds_to_cpr=lambda: True),
                ttimeoutlen=0.5,
            )

    import prompt_toolkit  # type: ignore[import-not-found]
    from prompt_toolkit.document import Document

    monkeypatch.setattr(prompt_toolkit, "PromptSession", _PromptSessionStub)
    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path))

    prompt_session = cli_mod._maybe_make_chat_prompt_session(
        console=Console(),
        root=Path("."),
        pending_images=[],
        forge_state=cli_mod._ForgeChatState(),
        session=SimpleNamespace(
            subagent_registry={"explorer": object()},
            skill_registry={"python": object()},
        ),
    )

    assert prompt_session is not None
    completer = captured[0]["completer"]
    removed_subagent_completions = [
        completion.text
        for completion in completer.get_completions(Document(text="/subagent "), None)
    ]
    skill_completions = [
        completion.text for completion in completer.get_completions(Document(text="/skill "), None)
    ]
    assert removed_subagent_completions == []
    assert "/skill python" in skill_completions


def test_maybe_make_chat_prompt_session_falls_back_when_constructor_unsupported(
    tmp_path: Path, monkeypatch
) -> None:
    captured: list[dict[str, Any]] = []

    class _PromptSessionStub:
        def __init__(self, **kwargs: Any) -> None:
            if "erase_when_done" in kwargs:
                raise TypeError(
                    "PromptSession.__init__() got an unexpected keyword argument 'erase_when_done'"
                )
            captured.append(dict(kwargs))
            self.app = SimpleNamespace(
                output=SimpleNamespace(responds_to_cpr=lambda: True),
                ttimeoutlen=0.25,
            )

    import prompt_toolkit  # type: ignore[import-not-found]

    monkeypatch.setattr(prompt_toolkit, "PromptSession", _PromptSessionStub)
    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path))

    session = cli_mod._maybe_make_chat_prompt_session(
        console=Console(),
        root=Path("."),
        pending_images=[],
        forge_state=cli_mod._ForgeChatState(),
    )

    assert session is not None
    assert len(captured) == 1
    assert "erase_when_done" not in captured[0]
    assert session._alysis_erase_when_done is False  # type: ignore[attr-defined]
    assert session.app.ttimeoutlen == cli_mod._CHAT_PROMPT_ESCAPE_SEQUENCE_TIMEOUT_S


def test_maybe_make_chat_prompt_session_preserves_larger_escape_sequence_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    class _PromptSessionStub:
        def __init__(self, **_kwargs: Any) -> None:
            self.app = SimpleNamespace(
                output=SimpleNamespace(responds_to_cpr=lambda: True),
                ttimeoutlen=1.75,
            )

    import prompt_toolkit  # type: ignore[import-not-found]

    monkeypatch.setattr(prompt_toolkit, "PromptSession", _PromptSessionStub)
    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path))

    session = cli_mod._maybe_make_chat_prompt_session(
        console=Console(),
        root=Path("."),
        pending_images=[],
        forge_state=cli_mod._ForgeChatState(),
    )

    assert session is not None
    assert session.app.ttimeoutlen == 1.75


def test_chat_prompt_session_never_passes_erase_when_done_kwarg(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"prompt_kwargs": [], "run_turn_calls": []}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()
        stream = False
        mode = "review"

        def run_turn(
            self,
            instruction: str,
            *,
            image_paths: list[str] | None = None,
            cancellation_token: InteractiveCancellationToken | None = None,
        ) -> int:
            assert cancellation_token is not None
            captured["run_turn_calls"].append((instruction, image_paths))
            return 0

        def close(self) -> None:
            return None

    class _FakePromptSession:
        def __init__(self) -> None:
            self._responses = iter(["hello", "exit"])

        def prompt(self, *_args: object, **kwargs: object) -> str:
            captured["prompt_kwargs"].append(dict(kwargs))
            if "erase_when_done" in kwargs:
                raise TypeError(
                    "PromptSession.prompt() got an unexpected keyword argument 'erase_when_done'"
                )
            return next(self._responses)

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(
        cli_mod,
        "_maybe_make_chat_prompt_session",
        lambda **_kwargs: _FakePromptSession(),
    )

    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="",
        env=env,
    )

    assert result.exit_code == 0
    assert len(captured["run_turn_calls"]) == 1
    assert captured["run_turn_calls"][0][0] == "hello"
    assert all("erase_when_done" not in kwargs for kwargs in captured["prompt_kwargs"])


def test_accept_chat_suggestion_or_complete_accepts_history_suggestion() -> None:
    inserted: list[str] = []
    started: list[bool] = []
    completed: list[bool] = []
    event = SimpleNamespace(
        current_buffer=SimpleNamespace(
            complete_state=None,
            suggestion=SimpleNamespace(text=" world"),
            document=SimpleNamespace(is_cursor_at_the_end=True),
            insert_text=lambda text: inserted.append(text),
            start_completion=lambda *, select_first=False: started.append(select_first),
            complete_next=lambda: completed.append(True),
        )
    )

    cli_mod._accept_chat_suggestion_or_complete(event)

    assert inserted == [" world"]
    assert started == []
    assert completed == []


def test_accept_chat_suggestion_or_complete_starts_completion_without_suggestion() -> None:
    inserted: list[str] = []
    started: list[bool] = []
    completed: list[bool] = []
    event = SimpleNamespace(
        current_buffer=SimpleNamespace(
            complete_state=None,
            suggestion=None,
            document=SimpleNamespace(is_cursor_at_the_end=True),
            insert_text=lambda text: inserted.append(text),
            start_completion=lambda *, select_first=False: started.append(select_first),
            complete_next=lambda: completed.append(True),
        )
    )

    cli_mod._accept_chat_suggestion_or_complete(event)

    assert inserted == []
    assert started == [False]
    assert completed == []


def test_accept_chat_suggestion_or_complete_advances_open_completion_menu() -> None:
    inserted: list[str] = []
    started: list[bool] = []
    completed: list[bool] = []
    event = SimpleNamespace(
        current_buffer=SimpleNamespace(
            complete_state=object(),
            suggestion=SimpleNamespace(text=" world"),
            document=SimpleNamespace(is_cursor_at_the_end=True),
            insert_text=lambda text: inserted.append(text),
            start_completion=lambda *, select_first=False: started.append(select_first),
            complete_next=lambda: completed.append(True),
        )
    )

    cli_mod._accept_chat_suggestion_or_complete(event)

    assert inserted == []
    assert started == []
    assert completed == [True]


def test_clear_submitted_prompt_line_writes_ansi_when_interactive(monkeypatch) -> None:
    class _FakeStdout:
        def __init__(self) -> None:
            self.buffer = ""
            self.flushed = False

        def write(self, value: str) -> int:
            self.buffer += value
            return len(value)

        def flush(self) -> None:
            self.flushed = True

        def isatty(self) -> bool:
            return True

    fake_stdout = _FakeStdout()
    monkeypatch.setattr(cli_mod.sys, "stdout", fake_stdout)
    monkeypatch.setattr(cli_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")

    cli_mod._clear_submitted_prompt_line()

    assert fake_stdout.buffer == "\x1b[1A\r\x1b[2K\r"
    assert fake_stdout.flushed is True


def test_submitted_prompt_total_lines_wraps_for_narrow_and_normal_widths() -> None:
    text = "x" * 170
    lines_80 = cli_mod._submitted_prompt_total_lines(
        submitted_text=text,
        prompt_label="> ",
        terminal_columns=80,
    )
    lines_100 = cli_mod._submitted_prompt_total_lines(
        submitted_text=text,
        prompt_label="> ",
        terminal_columns=100,
    )
    assert lines_80 > 1
    assert lines_100 > 1
    assert lines_80 >= lines_100


def test_clear_previous_terminal_lines_ansi_is_deterministic() -> None:
    assert cli_mod._clear_previous_terminal_lines_ansi(1) == "\x1b[1A\r\x1b[2K\r"
    assert cli_mod._clear_previous_terminal_lines_ansi(3) == (
        "\x1b[1A\r\x1b[2K\x1b[1A\r\x1b[2K\x1b[1A\r\x1b[2K\r"
    )


def test_clear_submitted_prompt_line_clears_multiple_wrapped_lines(monkeypatch) -> None:
    class _FakeStdout:
        def __init__(self) -> None:
            self.buffer = ""
            self.flushed = False

        def write(self, value: str) -> int:
            self.buffer += value
            return len(value)

        def flush(self) -> None:
            self.flushed = True

        def isatty(self) -> bool:
            return True

    fake_stdout = _FakeStdout()
    monkeypatch.setattr(cli_mod.sys, "stdout", fake_stdout)
    monkeypatch.setattr(cli_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")

    submitted = "x" * 170
    expected_lines = cli_mod._submitted_prompt_total_lines(
        submitted_text=submitted,
        prompt_label="> ",
        terminal_columns=80,
    )
    assert expected_lines > 1

    cli_mod._clear_submitted_prompt_line(
        submitted_text=submitted,
        prompt_label="> ",
        terminal_columns=80,
    )

    assert fake_stdout.buffer == cli_mod._clear_previous_terminal_lines_ansi(expected_lines)
    assert fake_stdout.flushed is True


def test_chat_shows_escape_hint_in_welcome_banner_when_interactive(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()
        stream = False
        mode = "review"

        def close(self) -> None:
            return None

    class _FakePromptSession:
        def prompt(self, *_args: object, **_kwargs: object) -> str:
            return "exit"

    def fake_create_session(**_kwargs) -> _DummySession:
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(
        cli_mod,
        "_maybe_make_chat_prompt_session",
        lambda **_kwargs: _FakePromptSession(),
    )
    monkeypatch.setattr(cli_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli_mod.sys.stdout, "isatty", lambda: True)

    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="",
        env=env,
    )
    assert result.exit_code == 0
    assert "Alysis Code" in result.output
    assert "/forge" in result.output
    assert "/status" in result.output
    assert "/help" in result.output
    assert "Esc to stop  /help for commands" not in result.output
    assert "alysis config set subagents_enabled true" in result.output
