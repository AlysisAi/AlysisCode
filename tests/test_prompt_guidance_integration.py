from __future__ import annotations

import copy
import socket
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_subagent_helpers import _child_tools
from test_subagents import _build_main_tools

from alysis_code import agent_loop
from alysis_code import cli as cli_mod
from alysis_code.agent import session as session_mod
from alysis_code.agent.prompt_context import refresh_session_prompt_guidance
from alysis_code.agent.prompt_families import FAMILY_WORKFLOWS
from alysis_code.agent.prompt_guidance import render_guidance
from alysis_code.cli_impl import chat as chat_facade
from alysis_code.cli_impl.chat import loop as chat_loop
from alysis_code.config import AppConfig, set_config_value
from alysis_code.prompt_guidance_catalog import (
    ALL_GUIDANCE_PROFILES,
    PROMPT_FAMILIES,
    get_model_prompt_catalog,
)
from alysis_code.subagents import SubagentDefinition, built_in_subagents


@pytest.fixture(autouse=True)
def _bind_chat_runtime_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))

    def deny_network(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Prompt-guidance integration tests must not use the network")

    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    chat_facade._sync_cli_globals(cli_mod)


def _session(tmp_path: Path, cfg: AppConfig, **overrides: Any) -> session_mod.AgentSession:
    options: dict[str, Any] = {
        "cfg": cfg,
        "root": tmp_path,
        "mode": "readonly",
        "yes": True,
        "max_steps": 8,
        "no_log": False,
        "api_key_override": "test-key",
        "session_log_dir_override": tmp_path / "sessions",
        "enable_compaction": False,
        "verification_enabled": False,
    }
    options.update(overrides)
    return session_mod.create_session(**options)


def _history(session: session_mod.AgentSession) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = [
        {"role": "user", "content": "Keep the existing EUR setting."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "retained-read",
                    "type": "function",
                    "function": {"name": "fs_read", "arguments": '{"path":"price.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "retained-read", "content": "currency = 'EUR'"},
        {"role": "assistant", "content": "The existing setting is EUR."},
    ]
    session.messages.extend(copy.deepcopy(history))
    return history


def _assert_delivered_guidance(
    messages: list[dict[str, Any]], profile: Any, *, can_delegate: bool = True
) -> None:
    bootstrap = messages[0]["content"]
    assert bootstrap.count(render_guidance("workflow", profile)) == 1
    assert bootstrap.count('<alysis_guidance section="workflow">') == 1
    assert (render_guidance("delegation", profile) in bootstrap) is can_delegate


def _profile_representatives() -> list[tuple[str, str]]:
    # The configuration tests check every model mapping. Real sessions exercise
    # delivery and event recording once per distinct catalog profile.
    by_profile: dict[str, str] = {}
    for model, profile in sorted(get_model_prompt_catalog().items()):
        by_profile.setdefault(profile, model)
    return [(model, profile) for profile, model in sorted(by_profile.items())]


@pytest.mark.parametrize(
    ("model", "profile"),
    [*_profile_representatives(), ("unknown-model", "expanded")],
)
def test_real_session_delivers_and_records_applied_profile(
    tmp_path: Path, model: str, profile: str
) -> None:
    session = _session(tmp_path, AppConfig(model=model, skills_enabled=False))
    try:
        assert session.client.model == model
        assert session.prompt_guidance_profile == profile
        _assert_delivered_guidance(session.messages, profile)
        starts = [
            event for event in session.store.events_snapshot() if event["type"] == "session_start"
        ]
        assert starts[-1]["payload"]["prompt_guidance_profile"] == profile
    finally:
        session.close()


def test_gpt_family_shares_normal_and_expanded_workflows_with_common_policy(
    tmp_path: Path,
) -> None:
    assignments = {
        "gpt-5.6-luna": ("gpt-expanded", "expanded"),
        "gpt-5.6-terra": ("gpt-normal", "balanced"),
        "gpt-5.6-sol": ("gpt-normal", "balanced"),
        "gpt-5.5": ("gpt-expanded", "expanded"),
    }
    common_obligations = (
        "Fix the faulty definition and inspect its direct callers",
        "Add or update behavior tests when requested",
        "update documentation for user-facing changes",
        "Distinguish your task's changes from the starting state",
        "return the complete revised answer",
    )
    workflows: dict[str, str] = {}
    shared_bootstraps: list[str] = []
    for model, (profile, delegation_profile) in assignments.items():
        workspace = tmp_path / model
        workspace.mkdir()
        session = _session(workspace, AppConfig(model=model, skills_enabled=False))
        try:
            workflow = render_guidance("workflow", profile)
            delegation = render_guidance("delegation", profile)
            bootstrap = session.messages[0]["content"]
            _assert_delivered_guidance(session.messages, profile)
            assert session.prompt_guidance_profile == profile
            assert bootstrap.count(workflow) == 1
            assert delegation == render_guidance("delegation", delegation_profile)
            for obligation in common_obligations:
                assert obligation in workflow
            workflows[model] = workflow
            shared_bootstraps.append(bootstrap.replace(workflow, "").replace(delegation, ""))
        finally:
            session.close()

    assert workflows["gpt-5.6-luna"] == workflows["gpt-5.5"]
    assert workflows["gpt-5.6-terra"] == workflows["gpt-5.6-sol"]
    assert len(set(workflows.values())) == 2
    assert len(workflows["gpt-5.6-luna"].split()) > len(workflows["gpt-5.6-sol"].split())
    assert len(set(shared_bootstraps)) == 1
    for obligation in (
        "Security and trust boundaries",
        "Never discard uncommitted work",
        "run authoritative_verification_commands exactly as provided",
    ):
        assert obligation in shared_bootstraps[0]


@pytest.mark.parametrize(
    "override",
    [
        "Trusted complete replacement prompt.",
        "Trusted override quoting a profile:\n" + render_guidance("workflow", "gpt-sol"),
    ],
)
@pytest.mark.parametrize("depth", [0, 1, 2], ids=["parent", "child", "nested-helper"])
def test_trusted_prompt_override_does_not_claim_an_applied_profile(
    tmp_path: Path, override: str, depth: int
) -> None:
    session = _session(
        tmp_path,
        AppConfig(model="gpt-5.6-luna", skills_enabled=False),
        trusted_system_prompt_override=override,
        subagent_depth=depth,
    )
    try:
        assert session.prompt_guidance_profile is None
        assert session.messages[0]["content"] == override
        before = copy.deepcopy(session.messages[0])
        cfg = session.cfg.model_copy(deep=True)
        cfg.model = "gpt-6-astra"
        chat_loop._apply_config_menu_changes_to_session(session=session, cfg=cfg)
        assert session.prompt_guidance_profile is None
        assert session.messages[0] == before
    finally:
        session.close()


@pytest.mark.parametrize("one_shot", [False, True], ids=["chat", "one-shot"])
def test_parent_without_subagent_tools_keeps_parent_prompt_scope(
    tmp_path: Path, one_shot: bool
) -> None:
    session = _session(
        tmp_path,
        AppConfig(model="gpt-5.6-luna", skills_enabled=False),
        subagents_enabled=False,
        one_shot_execution=one_shot,
    )
    try:
        assert session.subagent_depth == 0
        assert session.prompt_guidance_profile == "gpt-expanded"
        _assert_delivered_guidance(session.messages, "gpt-expanded", can_delegate=False)
        assert "subagent_spawn" not in session.tools
    finally:
        session.close()


@pytest.mark.parametrize("depth", [1, 2], ids=["child", "nested-helper"])
def test_child_prompt_refresh_uses_active_model_family_not_parent_density(
    tmp_path: Path, depth: int
) -> None:
    session = _session(
        tmp_path,
        AppConfig(model="gpt-5.6-luna", skills_enabled=False),
        subagent_depth=depth,
        trusted_system_prompt_append="Retain this child's narrowed review assignment.",
    )
    try:
        original = copy.deepcopy(session.messages[0])
        history = _history(session)
        prefix_length = session.pinned_prefix_len
        cache_key = session.prompt_cache_stream_key
        assert session.prompt_guidance_profile == "gpt-subagent"
        # The active client is authoritative after a role/model transition;
        # cfg.model intentionally remains the original Luna model.
        session.client.model = "gpt-5.6-sol"
        assert not refresh_session_prompt_guidance(session)
        assert session.messages[0] == original
        session.client.model = "claude-sonnet-5"
        assert refresh_session_prompt_guidance(session)
        assert session.prompt_guidance_profile == "claude-subagent"
        assert session.cfg.model == "gpt-5.6-luna"
        _assert_delivered_guidance(session.messages, "claude-subagent", can_delegate=False)
        assert "Retain this child's narrowed review assignment." in session.messages[0]["content"]
        assert session.messages[-len(history) :] == history
        assert session.startup_messages[0] == session.messages[0]
        assert session.pinned_prefix_len == prefix_length
        assert session.prompt_cache_stream_key == cache_key
        assert not refresh_session_prompt_guidance(session)
    finally:
        session.close()


def test_model_reload_preserves_history_cache_key_and_refreshes_clear_prefix(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path, AppConfig(model="gpt-5.6-luna", skills_enabled=False))
    try:
        original_bootstrap = copy.deepcopy(session.messages[0])
        history = _history(session)
        prefix_length = session.pinned_prefix_len
        cache_key = session.prompt_cache_stream_key
        cfg = session.cfg.model_copy(deep=True)
        cfg.model = "gpt-6-astra"
        chat_loop._apply_config_menu_changes_to_session(session=session, cfg=cfg)
        assert session.client.model == "gpt-6-astra"
        assert session.prompt_guidance_profile == "gpt-normal"
        _assert_delivered_guidance(session.messages, "gpt-normal")
        assert session.messages[-len(history) :] == history
        assert session.pinned_prefix_len == prefix_length
        assert session.prompt_cache_stream_key == cache_key
        assert session.messages[0] != original_bootstrap
        assert session.startup_messages[0] == session.messages[0]
        astra_bootstrap = copy.deepcopy(session.messages[0])
        chat_loop._reset_chat_conversation(session=session, trigger="user_command")
        assert session.messages[0] == astra_bootstrap
        cfg.model = "gpt-5.6-luna"
        chat_loop._apply_config_menu_changes_to_session(session=session, cfg=cfg)
        assert session.messages[0] == original_bootstrap
        assert session.prompt_guidance_profile == "gpt-expanded"
        profile_events = [
            event["payload"]
            for event in session.store.events_snapshot()
            if event["type"] == "prompt_guidance_changed"
        ]
        assert [event["prompt_guidance_profile"] for event in profile_events] == [
            "gpt-normal",
            "gpt-expanded",
        ]
    finally:
        session.close()


def test_documented_sol_alias_reload_preserves_bootstrap_bytes_and_history(tmp_path: Path) -> None:
    session = _session(tmp_path, AppConfig(model="gpt-5.6", skills_enabled=False))
    try:
        original = copy.deepcopy(session.messages[0])
        history = _history(session)
        cfg = session.cfg.model_copy(deep=True)
        cfg.model = "gpt-5.6-sol"
        chat_loop._apply_config_menu_changes_to_session(session=session, cfg=cfg)
        assert session.client.model == "gpt-5.6-sol"
        assert session.prompt_guidance_profile == "gpt-normal"
        assert session.messages[0] == original
        assert session.messages[-len(history) :] == history
        _assert_delivered_guidance(session.messages, "gpt-normal")
    finally:
        session.close()


@pytest.mark.parametrize(
    ("model", "profile", "changes_prompt"),
    [
        ("gpt-5.6-terra", "gpt-normal", True),
        ("gpt-5.6-sol", "gpt-normal", True),
        ("gpt-5.5", "gpt-expanded", False),
    ],
)
def test_gpt_model_reload_only_replaces_guidance_when_family_variant_changes(
    tmp_path: Path, model: str, profile: str, changes_prompt: bool
) -> None:
    session = _session(tmp_path, AppConfig(model="gpt-5.6-luna", skills_enabled=False))
    try:
        original = copy.deepcopy(session.messages[0])
        history = _history(session)
        cache_key = session.prompt_cache_stream_key
        prefix_length = session.pinned_prefix_len
        cfg = session.cfg.model_copy(deep=True)
        cfg.model = model
        chat_loop._apply_config_menu_changes_to_session(session=session, cfg=cfg)
        assert session.client.model == model
        assert session.prompt_guidance_profile == profile
        assert (session.messages[0] != original) is changes_prompt
        if changes_prompt:
            assert render_guidance("workflow", "gpt-expanded") not in session.messages[0]["content"]
        assert session.messages[-len(history) :] == history
        assert session.pinned_prefix_len == prefix_length
        assert session.prompt_cache_stream_key == cache_key
        assert session.startup_messages[0] == session.messages[0]
        _assert_delivered_guidance(session.messages, profile)
    finally:
        session.close()


def test_profile_configuration_reload_changes_guidance_without_changing_model(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path, AppConfig(model="gpt-5.6-luna", skills_enabled=False))
    try:
        original_client = session.client
        original_prompt = copy.deepcopy(session.messages[0])
        history = _history(session)
        cfg = session.cfg.model_copy(deep=True)
        set_config_value(cfg, "prompt_guidance.model_profiles.gpt-5.6-luna", "compact")
        chat_loop._apply_config_menu_changes_to_session(session=session, cfg=cfg)
        assert session.client is original_client
        assert session.client.model == "gpt-5.6-luna"
        assert session.prompt_guidance_profile == "compact"
        _assert_delivered_guidance(session.messages, "compact")
        assert session.messages[0] != original_prompt
        assert session.messages[-len(history) :] == history
        assert session.startup_messages[0] == session.messages[0]
    finally:
        session.close()


@pytest.mark.parametrize("quoted_profile", ["gpt-luna", "gpt-sol", "gpt-astra", "expanded"])
def test_model_reload_preserves_profile_text_quoted_in_trusted_append_and_user_history(
    tmp_path: Path,
    quoted_profile: Any,
) -> None:
    quoted = "\n\n".join(
        (render_guidance("workflow", quoted_profile), render_guidance("delegation", quoted_profile))
    )
    appendix = (
        f"Custom role reference; retain this quotation verbatim:\n\n{quoted}\n\nEnd quotation."
    )
    session = _session(
        tmp_path,
        AppConfig(model="gpt-5.6-luna", skills_enabled=False),
        trusted_system_prompt_append=appendix,
    )
    try:
        user_message = {"role": "user", "content": f"Review this literal text:\n{quoted}"}
        session.messages.append(copy.deepcopy(user_message))
        original_tail = session.messages[0]["content"].partition(appendix)[2]
        cfg = session.cfg.model_copy(deep=True)
        cfg.model = "gpt-6-astra"
        chat_loop._apply_config_menu_changes_to_session(session=session, cfg=cfg)
        assert session.prompt_guidance_profile == "gpt-normal"
        assert session.messages[-1] == user_message
        assert session.messages[0]["content"].count(appendix) == 1
        assert session.messages[0]["content"].partition(appendix)[2] == original_tail
        assert session.startup_messages[0] == session.messages[0]
        # Only the host-owned prefix must have one guidance block. The trusted
        # appendix deliberately quotes another full block and stays verbatim.
        host_prefix = session.messages[0]["content"].partition(appendix)[0]
        _assert_delivered_guidance([{"role": "system", "content": host_prefix}], "gpt-normal")
    finally:
        session.close()


def test_failed_reload_restores_profile_and_both_message_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path, AppConfig(model="gpt-5.6-luna", skills_enabled=False))
    try:
        _history(session)
        before = copy.deepcopy(session.messages)
        startup_before = copy.deepcopy(session.startup_messages)
        append = session.store.append

        def fail_profile_event(kind: str, payload: dict[str, Any]) -> None:
            if kind == "prompt_guidance_changed":
                raise OSError("durable store unavailable")
            append(kind, payload)

        monkeypatch.setattr(session.store, "append", fail_profile_event)
        cfg = session.cfg.model_copy(deep=True)
        cfg.model = "gpt-6-astra"
        with pytest.raises(OSError, match="durable store unavailable"):
            chat_loop._apply_config_menu_changes_to_session(session=session, cfg=cfg)
        assert session.client.model == session.cfg.model == "gpt-5.6-luna"
        assert session.prompt_guidance_profile == "gpt-expanded"
        assert session.messages == before
        assert session.startup_messages == startup_before
    finally:
        session.close()


def test_persona_switch_follows_active_client_while_preserving_base_model(tmp_path: Path) -> None:
    chat_facade._sync_cli_globals(cli_mod)
    cfg = AppConfig(model="gpt-5.6-luna", skills_enabled=False)
    set_config_value(cfg, "role_models.planner", "gpt-6-astra")
    session = _session(tmp_path, cfg, mode="review")
    try:
        original = copy.deepcopy(session.messages[0])
        original_client = session.client
        history = _history(session)
        chat_facade._apply_chat_persona(session=session, persona="architect")
        assert session.cfg.model == "gpt-5.6-luna"
        assert session.client.model == "gpt-6-astra"
        assert session.prompt_guidance_profile == "gpt-normal"
        _assert_delivered_guidance(session.messages, "gpt-normal")
        assert session.messages[-len(history) :] == history
        chat_facade._apply_chat_persona(session=session, persona="code")
        assert session.client is original_client
        assert session.prompt_guidance_profile == "gpt-expanded"
        assert session.messages[0] == original
        assert session.messages[-len(history) :] == history
    finally:
        session.close()


def _record_children(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []

    def report_without_provider(self: session_mod.AgentSession, task: str, **_kwargs: Any) -> int:
        observations.append(
            {
                "model": self.client.model,
                "profile": self.prompt_guidance_profile,
                "messages": copy.deepcopy(self.messages),
                "depth": self.subagent_depth,
            }
        )
        self.messages.append({"role": "user", "content": task})
        self.store.append("user_message", {"content": task})
        report = "Source inspection complete: the existing currency setting remains EUR."
        self.messages.append({"role": "assistant", "content": report})
        self.store.append("assistant_message", {"content": report})
        self.store.append("final", {"content": report})
        return 0

    monkeypatch.setattr(session_mod.AgentSession, "run_turn", report_without_provider)
    monkeypatch.setattr(agent_loop, "create_session", session_mod.create_session)
    return observations


@pytest.mark.parametrize(
    ("model_source", "child_model", "child_profile"),
    [
        ("inherited", "gpt-5.6-luna", "gpt-subagent"),
        ("definition", "gpt-5.6-terra", "gpt-subagent"),
        ("definition", "gpt-5.6-sol", "gpt-subagent"),
        ("definition", "gpt-5.5", "gpt-subagent"),
        ("role", "gpt-5.6-terra", "gpt-subagent"),
        ("role", "gpt-5.6-sol", "gpt-subagent"),
        ("role", "gpt-5.5", "gpt-subagent"),
        ("role", "gpt-6-astra", "gpt-subagent"),
    ],
)
def test_child_profile_uses_its_resolved_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_source: str,
    child_model: str,
    child_profile: str,
) -> None:
    observations = _record_children(monkeypatch)
    cfg = AppConfig(model="gpt-5.6-luna", skills_enabled=False)
    definition = SubagentDefinition(
        name="reader",
        description="Read source",
        system_prompt="Read the assigned source.",
        mode="readonly",
        allow_tools=("fs_read",),
        allow_workspace_writes=False,
        model=child_model if model_source == "definition" else None,
        model_role="review" if model_source == "role" else None,
    )
    if model_source == "role":
        set_config_value(cfg, "role_models.review", child_model)
    tools = _build_main_tools(
        tmp_path=tmp_path,
        cfg=cfg,
        subagents_enabled=True,
        subagent_registry={"reader": definition},
    )
    scheduler = tools["subagent_run"].run.__self__.child_scheduler
    try:
        result = tools["subagent_run"].run(
            {"name": "reader", "task": "Inspect the existing setting."}
        )
        assert "error" not in result
        assert len(observations) == 1
        assert (observations[0]["model"], observations[0]["profile"]) == (
            child_model,
            child_profile,
        )
        _assert_delivered_guidance(observations[0]["messages"], child_profile, can_delegate=False)
        assert cfg.model == "gpt-5.6-luna"
    finally:
        scheduler.shutdown()


def test_helper_profile_uses_helper_model_instead_of_writer_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observations = _record_children(monkeypatch)
    registry = built_in_subagents()
    registry["explorer"] = replace(registry["explorer"], model="gpt-6-astra")
    tools, store = _child_tools(
        tmp_path=tmp_path,
        cfg=AppConfig(model="gpt-5.6-luna", skills_enabled=False),
        registry=registry,
        create_session_factory=session_mod.create_session,
    )
    try:
        result = tools["subagent_run"].run(
            {"name": "explorer", "task": "Inspect the existing setting."}
        )
        assert "error" not in result
        assert [(o["model"], o["profile"], o["depth"]) for o in observations] == [
            ("gpt-6-astra", "gpt-subagent", 2)
        ]
        _assert_delivered_guidance(observations[0]["messages"], "gpt-subagent", can_delegate=False)
    finally:
        store.close()


@pytest.mark.parametrize(
    ("override", "child_profile", "changes_prompt"),
    [
        ("gpt-normal", "gpt-subagent", False),
        ("gpt-expanded", "gpt-subagent", False),
        ("compact", "general-subagent", True),
    ],
)
def test_successful_child_resume_keeps_history_and_resolves_current_profile_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: str,
    child_profile: str,
    changes_prompt: bool,
) -> None:
    observations = _record_children(monkeypatch)
    cfg = AppConfig(model="gpt-5.6-luna", skills_enabled=False)
    definition = SubagentDefinition(
        name="reader",
        description="Read source",
        system_prompt="Read the assigned source.",
        mode="readonly",
        allow_tools=("fs_read",),
        allow_workspace_writes=False,
        model="gpt-5.6-terra",
    )
    tools = _build_main_tools(
        tmp_path=tmp_path,
        cfg=cfg,
        subagents_enabled=True,
        subagent_registry={"reader": definition},
    )
    scheduler = tools["subagent_run"].run.__self__.child_scheduler
    try:
        first = tools["subagent_run"].run(
            {"name": "reader", "task": "Inspect the existing setting."}
        )
        assert "error" not in first
        set_config_value(cfg, "prompt_guidance.model_profiles.gpt-5.6-terra", override)
        resumed = tools["subagent_resume"].run(
            {"run_id": first["run_id"], "task": "Check the earlier finding."}
        )
        assert "error" not in resumed
        joined = tools["subagent_wait"].run({"run_id": resumed["run_id"], "timeout_s": 10})
        assert not joined["pending_run_ids"]
        assert [o["profile"] for o in observations] == ["gpt-subagent", child_profile]
        _assert_delivered_guidance(observations[0]["messages"], "gpt-subagent", can_delegate=False)
        _assert_delivered_guidance(observations[1]["messages"], child_profile, can_delegate=False)
        assert observations[1]["model"] == "gpt-5.6-terra"
        restored = observations[1]["messages"]
        assert {"role": "user", "content": "Inspect the existing setting."} in restored
        assert any(
            m.get("role") == "assistant" and "currency setting remains EUR" in str(m.get("content"))
            for m in restored
        )
        assert (observations[0]["messages"][0] != restored[0]) is changes_prompt
    finally:
        scheduler.shutdown()


@pytest.mark.parametrize(
    "child_profile", [profile for profile in ALL_GUIDANCE_PROFILES if profile.endswith("-subagent")]
)
@pytest.mark.parametrize("variant", ["normal", "expanded"])
def test_every_family_parent_variant_delivers_its_single_child_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child_profile: str, variant: str
) -> None:
    observations = _record_children(monkeypatch)
    family = child_profile.removesuffix("-subagent")
    cfg = AppConfig(
        model="private-family-model",
        skills_enabled=False,
        prompt_guidance={
            "default": "expanded",
            "model_profiles": {"private-family-model": f"{family}-{variant}"},
        },
    )
    role = SubagentDefinition(
        name="reader",
        description="Inspect the assigned source.",
        system_prompt="Keep this source review read-only.",
        mode="readonly",
        allow_tools=("fs_read",),
        allow_workspace_writes=False,
    )
    tools = _build_main_tools(
        tmp_path=tmp_path, cfg=cfg, subagents_enabled=True, subagent_registry={"reader": role}
    )
    scheduler = tools["subagent_run"].run.__self__.child_scheduler
    try:
        result = tools["subagent_run"].run(
            {"name": "reader", "task": "Inspect the existing setting."}
        )
        assert "error" not in result
        assert len(observations) == 1
        child = observations[0]
        assert child["profile"] == child_profile
        assert child["depth"] == 1
        assert child["model"] == "private-family-model"
        _assert_delivered_guidance(child["messages"], child_profile, can_delegate=False)
        assert cfg.prompt_guidance.model_profiles["private-family-model"] == f"{family}-{variant}"
    finally:
        scheduler.shutdown()


@pytest.mark.parametrize("family", PROMPT_FAMILIES)
def test_family_composition_preserves_shared_policy_tools_and_separate_child_role(
    tmp_path: Path, family: str
) -> None:
    parents = []
    child = None
    role = "Review only the assigned public API; preserve existing work."
    try:
        for variant in ("normal", "expanded"):
            parent = _session(
                tmp_path,
                AppConfig(
                    model="private-family-model",
                    skills_enabled=False,
                    prompt_guidance={"default": f"{family}-{variant}", "model_profiles": {}},
                ),
            )
            parents.append(parent)
        child = _session(
            tmp_path,
            parents[1].cfg,
            subagent_depth=1,
            subagents_enabled=False,
            trusted_system_prompt_append=role,
        )
        for session, variant in zip(
            (*parents, child), ("normal", "expanded", "subagent"), strict=True
        ):
            profile = f"{family}-{variant}"
            assert session.prompt_guidance_profile == profile
            _assert_delivered_guidance(
                session.messages, profile, can_delegate=variant != "subagent"
            )
            system = session.messages[0]["content"]
            assert system.count(FAMILY_WORKFLOWS[family]) == 1
            assert system.count("A runtime completion checklist continues this request") == 1
        assert parents[0].tool_list == parents[1].tool_list
        shared_policy = [
            parent.messages[0]["content"]
            .replace(render_guidance("workflow", parent.prompt_guidance_profile), "")
            .replace(render_guidance("delegation", parent.prompt_guidance_profile), "")
            for parent in parents
        ]
        assert shared_policy[0] == shared_policy[1]
        assert child.messages[0]["content"].count(role) == 1
        assert "Scoped assignment" in child.messages[0]["content"]
        assert "Keeping the work on track" not in child.messages[0]["content"]
        assert not any(name.startswith("subagent_") for name in child.tools)
    finally:
        for session in (*parents, child):
            if session is not None:
                session.close()


@pytest.mark.parametrize("same_connection", [True, False])
def test_chat_resume_uses_effective_restored_model_and_current_profile_configuration(
    tmp_path: Path, same_connection: bool
) -> None:
    previous_cfg = AppConfig(model="gpt-6-astra", skills_enabled=False)
    set_config_value(previous_cfg, "prompt_guidance.model_profiles.gpt-6-astra", "compact")
    previous = _session(tmp_path, previous_cfg)
    target_id = previous.store.session_id
    previous.store.append("user_message", {"content": "Preserve the existing EUR setting."})
    previous.store.append("assistant_message", {"content": "The setting remains EUR."})
    previous.close()

    cfg = AppConfig(
        model="gpt-5.6-luna",
        skills_enabled=False,
        base_url="https://api.openai.com/v1" if same_connection else "https://gateway.example/v1",
    )
    current = _session(tmp_path, cfg)
    try:
        ok, message, _ = cli_mod._resume_chat_session(session=current, target_session_id=target_id)
        assert ok, message
        expected_model = "gpt-6-astra" if same_connection else "gpt-5.6-luna"
        expected_profile = "gpt-normal" if same_connection else "gpt-expanded"
        assert current.client.model == expected_model
        assert current.prompt_guidance_profile == expected_profile
        _assert_delivered_guidance(current.messages, expected_profile)
        assert {"role": "user", "content": "Preserve the existing EUR setting."} in current.messages
        assert {"role": "assistant", "content": "The setting remains EUR."} in current.messages
        assert current.messages[0] == current.startup_messages[0]
        starts = [
            event for event in current.store.events_snapshot() if event["type"] == "session_start"
        ]
        assert starts[-1]["payload"]["session_source"] == "resume"
        assert starts[-1]["payload"]["prompt_guidance_profile"] == expected_profile
    finally:
        current.close()
