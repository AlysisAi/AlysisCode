from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from alysis_code.agent import tools_assembly
from alysis_code.agent.llm_calls import _request_messages_with_volatile_suffix
from alysis_code.agent.prompt_context import (
    _subagent_context_message,
    prepare_session_prompt_context,
)
from alysis_code.agent.prompt_guidance import render_guidance
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.prompt_guidance_catalog import GUIDANCE_PROFILES, PromptGuidanceProfile
from alysis_code.session_store import read_session_events
from alysis_code.skills import SkillBundle, build_explicit_skill_context_message
from alysis_code.skills.prompting import EXPLICIT_SKILL_CONTEXT_TOTAL_MAX_CHARS
from alysis_code.subagents import SubagentDefinition, built_in_subagents
from alysis_code.tools.web_search import WebSearchRuntimeStatus


@pytest.fixture(autouse=True)
def isolated_skill_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from alysis_code.skills import discovery

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(discovery, "canonical_user_config_dir", lambda: tmp_path / "config")


def _fake_git_repo(root: Path) -> None:
    git_dir = root / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text("0" * 40 + "\n", encoding="utf-8")


def _system_prompt(session: object) -> str:
    messages = getattr(session, "messages", [])
    if not isinstance(messages, list):
        return ""
    for message in messages:
        if str(message.get("role") or "") == "system":
            return str(message.get("content") or "")
    return ""


def _workspace_binding_context(session: object) -> str:
    messages = getattr(session, "messages", [])
    if not isinstance(messages, list):
        return ""
    for message in messages:
        if str(message.get("role") or "") != "user":
            continue
        content = str(message.get("content") or "")
        if content.lstrip().startswith("<workspace_binding_context>"):
            return content
    return ""


def _estimated_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _normalized_bootstrap_json(value: object, root: Path) -> str:
    # Compare prompt growth, independent of the OS or pytest's temporary path length.
    return json.dumps(value, ensure_ascii=True).replace(
        json.dumps(str(root.resolve()), ensure_ascii=True)[1:-1], "/workspace"
    )


def test_subagent_prompt_context_omits_empty_task_placeholder(tmp_path: Path) -> None:
    _fake_git_repo(tmp_path)

    prompt_context = prepare_session_prompt_context(
        cfg=AppConfig(
            model="test-model",
            subagents_enabled=False,
            skills_enabled=False,
            web_search_mode="off",
        ),
        root=tmp_path,
        mode="readonly",
        yes=True,
        non_interactive=True,
        verification_enabled=False,
        subagent_depth=1,
    )

    assert "awaiting_substantive_repo_request" not in str(prompt_context.messages)
    assert "no_retained_task_summary" not in str(prompt_context.messages)


def test_parent_subagent_catalog_states_required_tool_launch_constraints(
    tmp_path: Path,
) -> None:
    _fake_git_repo(tmp_path)
    prompt_context = prepare_session_prompt_context(
        cfg=AppConfig(
            model="test-model",
            subagents_enabled=True,
            skills_enabled=False,
            web_search_mode="off",
        ),
        root=tmp_path,
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=False,
        subagent_depth=0,
    )

    subagent_context = next(
        (
            str(message.get("content") or "")
            for message in prompt_context.messages
            if "<subagent_context>" in str(message.get("content") or "")
        ),
        "",
    )
    assert (
        "requires shell_run; cannot launch readonly; minimum mode review; long "
        "diagnosis: prefer background workspace_view=isolated over synchronous shared."
        in subagent_context
    )
    assert (
        "requires verify_run; cannot launch readonly; minimum mode review; long "
        "diagnosis: prefer background workspace_view=isolated over synchronous shared."
        in subagent_context
    )


def test_parent_subagent_catalog_omits_readonly_satisfiable_constraint() -> None:
    subagent_context = _subagent_context_message(
        subagent_registry={
            "reader": SubagentDefinition(
                name="reader",
                description="Read files.",
                system_prompt="Read the requested files.",
                mode="readonly",
                required_tools=("fs_read",),
            )
        }
    )

    assert subagent_context is not None
    assert "- reader | readonly | Read files." in subagent_context
    assert "requires fs_read" not in subagent_context


def test_parent_subagent_catalog_describes_value_based_delegation() -> None:
    subagent_context = _subagent_context_message(
        subagent_registry=built_in_subagents(include_visual_designer=False)
    )

    assert subagent_context is not None
    assert "work directly by default; delegate autonomously" in subagent_context
    assert (
        "when a bounded contribution warrants extra context, coordination, and latency"
        in subagent_context
    )
    assert "use spawn for useful independent overlap; wait for genuine dependencies" in (
        subagent_context
    )


@pytest.mark.parametrize("cap", [2, 5])
def test_parent_subagent_catalog_exposes_capacity_without_planning_fanout(cap: int) -> None:
    subagent_context = _subagent_context_message(
        subagent_registry=built_in_subagents(include_visual_designer=False),
        max_background_children=cap,
    )

    assert subagent_context is not None
    assert f"background: subagent_spawn max{cap} FIFO" in subagent_context
    assert "Available slots are limits, not a work plan" in subagent_context
    assert "do different work or wait for a real dependency" in subagent_context
    assert "plan independent assignments within" not in subagent_context


def test_builtin_subagent_descriptions_include_when_not_guidance() -> None:
    registry = built_in_subagents()

    assert "Not for a single known-file lookup" in registry["explorer"].description
    assert "bounded research, diagnosis, implementation" in registry["general"].description
    assert "implementer" not in registry
    assert "Not for implementing a known fix" in registry["debugger"].description
    assert "Not for root-cause analysis" in registry["verifier"].description
    assert "Not for initial repository mapping" in registry["code-reviewer"].description


def test_top_level_prompt_context_has_no_objective_placeholder(tmp_path: Path) -> None:
    _fake_git_repo(tmp_path)

    prompt_context = prepare_session_prompt_context(
        cfg=AppConfig(
            model="test-model",
            subagents_enabled=False,
            skills_enabled=False,
            web_search_mode="off",
        ),
        root=tmp_path,
        mode="readonly",
        yes=True,
        non_interactive=True,
        verification_enabled=False,
        subagent_depth=0,
    )

    assert "awaiting_substantive_repo_request" not in str(prompt_context.messages)


def _ready_web_search_status() -> WebSearchRuntimeStatus:
    return WebSearchRuntimeStatus(
        mode="auto",
        provider="fake",
        base_url=None,
        model=None,
        api_key_available=True,
        registration_ready=True,
        notes=(),
    )


def _assert_dependency_scout_visible(session: object) -> None:
    tools = getattr(session, "tool_list", [])
    subagent_tool = next(
        item for item in tools if item.get("function", {}).get("name") == "subagent_run"
    )
    names = subagent_tool["function"]["parameters"]["properties"]["name"]["enum"]
    assert "dependency-scout" in names


def _write_skill(root: Path, rel_root: str, bundle_name: str) -> None:
    bundle = root / rel_root / bundle_name
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "SKILL.md").write_text(
        (
            "---\n"
            "name: pytest\n"
            "description: Debug pytest failures safely.\n"
            "---\n\n"
            "Read failing tests and fix the root cause.\n"
        ),
        encoding="utf-8",
    )


def test_create_session_adds_write_guidance_only_for_writable_modes(tmp_path: Path) -> None:
    cfg = AppConfig(model="test-model", web_search_mode="off")

    auto_session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    readonly_session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="readonly",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    try:
        auto_prompt = _system_prompt(auto_session)
        readonly_prompt = _system_prompt(readonly_session)

        assert "Editing workflow" in auto_prompt
        assert (
            "Tool descriptions are the canonical source for tool strategy and parameters."
            in auto_prompt
        )
        assert "Editing workflow" not in readonly_prompt
    finally:
        auto_session.close()
        readonly_session.close()


def test_create_session_splits_skill_lifecycle_and_discovery_guidance(
    tmp_path: Path,
) -> None:
    cfg = AppConfig(
        model="test-model",
        web_search_mode="off",
        bundled_skills_enabled=False,
    )
    session_without_skills = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    _write_skill(tmp_path, ".alysis_skills", "pytest")
    session_with_skills = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    try:
        no_skills_prompt = _system_prompt(session_without_skills)
        skills_prompt = _system_prompt(session_with_skills)
        no_skills_tool_names = {
            str(item.get("function", {}).get("name") or "")
            for item in getattr(session_without_skills, "tool_list", [])
            if isinstance(item, dict)
        }
        skills_tool_names = {
            str(item.get("function", {}).get("name") or "")
            for item in getattr(session_with_skills, "tool_list", [])
            if isinstance(item, dict)
        }
        skill_context = "\n".join(
            str(message.get("content") or "")
            for message in session_with_skills.messages
            if str(message.get("role") or "") == "user"
            and "<skill_context>" in str(message.get("content") or "")
        )

        assert "Skills lifecycle" in no_skills_prompt
        assert "alysis skill init" in no_skills_prompt
        assert "alysis skill create" in no_skills_prompt
        assert "alysis skill validate" in no_skills_prompt
        assert "default to the managed project-local scaffold" in no_skills_prompt
        assert "Do not hand-build skill bundles with `fs_mkdir` or `fs_write`" in no_skills_prompt
        assert "Skills and skill_read" not in no_skills_prompt
        assert "read the relevant available skill" not in no_skills_prompt
        assert "skill_read(name)" not in no_skills_prompt
        assert "skill_read" not in no_skills_tool_names

        assert "Skills lifecycle" in skills_prompt
        assert "Skills and skill_read" in skills_prompt
        assert "read the relevant available skill" in skills_prompt
        assert "Select only a skill whose action the user requests" in skills_prompt
        assert "concept mention is not a match" in skills_prompt
        assert "Honor explicit exclusions" in skills_prompt
        assert "choose the most specific fit" in skills_prompt
        assert "Do not invent skill names" in skills_prompt
        assert "Project-local explicit-turn skill context" in skills_prompt
        assert "alysis skill init" in skills_prompt
        assert "alysis skill validate" in skills_prompt
        assert "alysis skill install" in skills_prompt
        assert "skill_read" in skills_tool_names
        assert "Compare all descriptions" in skill_context
        assert "requested outcome and workflow" in skill_context
        assert "not shared steps or concept mentions" in skill_context
        assert "honor exclusions" in skill_context
        assert "narrowest fit" in skill_context
        assert "broad skills are fallbacks" in skill_context
        assert "Read a chosen workflow with skill_read(name)" in skill_context
        assert "optional attachable context" not in skill_context
    finally:
        session_without_skills.close()
        session_with_skills.close()


def test_create_session_respects_explicit_skills_auto_invoke_false_for_discovery_directive(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path, ".alysis_skills", "pytest")
    session = create_session(
        cfg=AppConfig(
            model="test-model",
            web_search_mode="off",
            skills_auto_invoke=False,
        ),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    try:
        joined_messages = "\n".join(
            str(message.get("content") or "") for message in session.messages
        )
        system_prompt = _system_prompt(session)
        tool_names = {
            str(item.get("function", {}).get("name") or "")
            for item in getattr(session, "tool_list", [])
            if isinstance(item, dict)
        }

        assert session.skills_auto_invoke is False
        assert "<skill_context>" in joined_messages
        assert "Skills are optional attachable context" in joined_messages
        assert "requested actions, not concept mentions" not in joined_messages
        assert "call skill_read(name) before any other task action" not in joined_messages
        assert "skill_read" in tool_names
        assert "Select only a skill whose action the user requests" not in system_prompt
        assert "read the relevant available skill" not in system_prompt
        assert "<matched_skill_context>" not in joined_messages
    finally:
        session.close()


# Serialized-size regression budgets, not provider token measurements. The
# Work increment adds ~1,324 char/4 units: native document/check schemas,
# five skill catalog entries, notes and deliverable guidance. Skill bodies
# remain deferred. Observed compact/balanced/expanded totals are approximately
# 14,252/14,827/15,833. Keep a small fixed allowance, not a dynamic self-budget.
# One-shot adds a separate fixed 450 below.
# Windows native sandbox permissions, shell schemas and boundary guidance add
# about 3,064 units. Seatbelt folder, network and Git contracts add about
# 4,847 units on macOS. Keep fixed allowances for these required contracts.
_NATIVE_BOOTSTRAP_ALLOWANCE = {"win32": 3_100, "darwin": 4_900}
_BOOTSTRAP_PROFILE_LIMITS = [
    pytest.param(None, 15_850, id="unknown-expanded-default"),
    pytest.param("compact", 14_275, id="compact"),
    pytest.param("balanced", 14_850, id="balanced"),
    pytest.param("expanded", 15_850, id="expanded"),
    *[
        pytest.param(profile, 14_500, id=profile)
        for profile in ("gpt-astra", "gpt-sol", "gpt-terra")
    ],
    *[
        pytest.param(profile, 15_350, id=profile)
        for profile in (
            "gpt-luna",
            "gpt-5.5",
            "claude-fable",
            "claude-opus",
            "claude-sonnet",
            "gemini",
            "qwen",
            "glm",
        )
    ],
    *[
        pytest.param(profile, 14_500, id=profile)
        for profile in GUIDANCE_PROFILES
        if profile.endswith("-normal")
    ],
    *[
        pytest.param(profile, 15_350, id=profile)
        for profile in GUIDANCE_PROFILES
        if profile.endswith("-expanded")
    ],
]


def test_bootstrap_budget_contract_covers_every_supported_profile() -> None:
    profiles = [case.values[0] for case in _BOOTSTRAP_PROFILE_LIMITS]
    assert len(profiles) == len(set(profiles))
    assert set(profiles) == {None, *GUIDANCE_PROFILES}


@pytest.mark.parametrize(("profile", "token_limit"), _BOOTSTRAP_PROFILE_LIMITS)
def test_interactive_bootstrap_payload_stays_bounded(
    tmp_path: Path,
    monkeypatch,
    record_property,
    profile: PromptGuidanceProfile | None,
    token_limit: int,
) -> None:
    _fake_git_repo(tmp_path)
    monkeypatch.setattr(
        tools_assembly,
        "resolve_web_search_runtime_status",
        lambda **_kwargs: _ready_web_search_status(),
    )
    cfg = AppConfig(model="test-model", web_search_mode="auto")
    if profile is not None:
        cfg.prompt_guidance.default = profile
        cfg.prompt_guidance.model_profiles = {}
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
    )
    try:
        messages_json = _normalized_bootstrap_json(
            _request_messages_with_volatile_suffix(messages=session.messages), tmp_path
        )
        tools_json = _normalized_bootstrap_json(session.tool_list, tmp_path)
        # Measure provider-facing context, excluding host metadata, with all
        # existing tool contracts and the selected workflow actually delivered.
        estimated_tokens = _estimated_tokens(messages_json) + _estimated_tokens(tools_json)
        record_property("estimated_bootstrap_tokens", estimated_tokens)
        expected_profile = profile or "expanded"
        assert session.prompt_guidance_profile == expected_profile
        assert render_guidance("workflow", expected_profile) in _system_prompt(session)
        _assert_dependency_scout_visible(session)
        platform_allowance = _NATIVE_BOOTSTRAP_ALLOWANCE.get(sys.platform, 0)
        assert estimated_tokens <= token_limit + platform_allowance
    finally:
        session.close()


def test_create_session_workspace_binding_context_includes_active_workdir(
    tmp_path: Path,
) -> None:
    session = create_session(
        cfg=AppConfig(model="test-model", web_search_mode="off"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    try:
        binding_context = _workspace_binding_context(session)

        assert "<workspace_binding_context>" in binding_context
        assert f"workspace_root: {tmp_path.resolve()}" in binding_context
        assert f"focus_dir: {tmp_path.resolve()}" in binding_context
        assert f"active_workdir: {tmp_path.resolve()}" in binding_context
        assert "focus_relpath: ." in binding_context
        assert "active_workdir_relpath: ." in binding_context
    finally:
        session.close()


def test_system_prompt_instructs_model_to_use_session_set_workdir_for_navigation(
    tmp_path: Path,
) -> None:
    session = create_session(
        cfg=AppConfig(model="test-model", web_search_mode="off"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    try:
        prompt = _system_prompt(session)

        assert "active_workdir" in prompt
        assert "session_set_workdir" in prompt
        assert "path_base`/`cwd_base` to `workspace_root`" in prompt
        assert "workspace_root" in prompt
        assert "new workspace bind/session is needed" in prompt
    finally:
        session.close()


@pytest.mark.parametrize(("profile", "token_limit"), _BOOTSTRAP_PROFILE_LIMITS)
def test_one_shot_bootstrap_payload_stays_bounded(
    tmp_path: Path,
    monkeypatch,
    record_property,
    profile: PromptGuidanceProfile | None,
    token_limit: int,
) -> None:
    _fake_git_repo(tmp_path)
    monkeypatch.setattr(
        tools_assembly,
        "resolve_web_search_runtime_status",
        lambda **_kwargs: _ready_web_search_status(),
    )
    cfg = AppConfig(model="test-model", web_search_mode="auto")
    if profile is not None:
        cfg.prompt_guidance.default = profile
        cfg.prompt_guidance.model_profiles = {}
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        one_shot_execution=True,
        session_log_dir_override=tmp_path / "sessions",
    )
    try:
        messages_json = _normalized_bootstrap_json(session.messages, tmp_path)
        tools_json = _normalized_bootstrap_json(session.tool_list, tmp_path)
        estimated_tokens = _estimated_tokens(messages_json) + _estimated_tokens(tools_json)
        record_property("estimated_bootstrap_tokens", estimated_tokens)
        expected_profile = profile or "expanded"
        assert session.prompt_guidance_profile == expected_profile
        assert render_guidance("workflow", expected_profile) in _system_prompt(session)
        _assert_dependency_scout_visible(session)
        platform_allowance = _NATIVE_BOOTSTRAP_ALLOWANCE.get(sys.platform, 0)
        assert estimated_tokens <= token_limit + platform_allowance + 450
    finally:
        session.close()


def test_create_session_wires_optional_prompt_cache_knobs(tmp_path: Path) -> None:
    _fake_git_repo(tmp_path)
    cfg = AppConfig(
        model="test-model",
        web_search_mode="off",
        prompt_cache_key="repo-main",
        prompt_cache_retention="24h",
    )
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    try:
        assert session.client.prompt_cache_key == "repo-main"
        assert session.client.prompt_cache_retention == "24h"
    finally:
        session.close()


def test_create_session_auto_prompt_cache_key_is_session_scoped(tmp_path: Path) -> None:
    _fake_git_repo(tmp_path)
    cfg = AppConfig(
        model="gpt-test",
        web_search_mode="off",
        prompt_cache_mode="auto",
    )
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        session_id_override="cache-session",
    )
    try:
        assert session.client.prompt_cache_key == "cache-session"
        router_client = getattr(session, "router_client", None)
        if router_client is not None:
            assert router_client.prompt_cache_key == session.client.prompt_cache_key
    finally:
        session.close()


def test_create_session_kimi_auto_cache_key_is_resume_stable_and_session_scoped(
    tmp_path: Path,
) -> None:
    _fake_git_repo(tmp_path)
    cfg = AppConfig(
        model="kimi-k3",
        base_url="https://api.moonshot.ai/v1",
        web_search_mode="off",
        prompt_cache_mode="auto",
    )

    first = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        session_id_override="cache-session-1",
    )
    resumed = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        session_id_override="cache-session-1",
    )
    different = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        session_id_override="cache-session-2",
    )
    try:
        assert first.client.prompt_cache_key is not None
        assert resumed.client.prompt_cache_key is not None
        assert first.client.prompt_cache_key == resumed.client.prompt_cache_key
        assert first.client.prompt_cache_key != different.client.prompt_cache_key
    finally:
        first.close()
        resumed.close()
        different.close()


def test_session_start_logs_workspace_grounding_descriptor(tmp_path: Path) -> None:
    _fake_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("notes cli\n", encoding="utf-8")
    session = create_session(
        cfg=AppConfig(model="test-model", web_search_mode="off"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
    )
    event_path = session.store.path
    session.close()

    [session_start] = [
        event for event in read_session_events(event_path) if event["type"] == "session_start"
    ]
    payload = session_start["payload"]
    grounding = payload["workspace_grounding"]
    assert grounding["workspace_kind"] == "git_repo"
    assert grounding["stable_grounding_available"] is True
    assert grounding["workspace_hint"] == "notes cli"
    assert "anchor_paths" in grounding


def test_explicit_skill_context_payload_is_turn_scoped_and_argument_bound() -> None:
    skill = SkillBundle(
        name="pytest",
        description="Debug pytest failures safely.",
        instructions='Investigate "$ARGUMENTS" using $1 then $2.',
        bundle_name="pytest",
        bundle_path=Path("/tmp/pytest"),
        entry_path=Path("/tmp/pytest/SKILL.md"),
        source_scope="project",
        source_kind="native",
        source_family=".alysis_skills",
        source_path=Path("/tmp/pytest"),
        trust_level="untrusted",
    )

    payload = build_explicit_skill_context_message(
        skill=skill,
        task_text='debug parser "retry bug"',
    )

    assert "This skill is attached only for the current turn." in payload
    assert '- $ARGUMENTS = "debug parser \\"retry bug\\""' in payload
    assert '- $1 = "debug"' in payload
    assert '- $2 = "parser"' in payload
    assert 'Investigate "debug parser "retry bug"" using debug then parser.' in payload


def test_explicit_skill_context_payload_stays_within_total_wrapper_budget() -> None:
    skill = SkillBundle(
        name="oversized",
        description="Large explicit wrapper",
        instructions=("Inspect $ARGUMENTS carefully.\n" * 40) + "Start with $1.\n",
        bundle_name="oversized",
        bundle_path=Path("/tmp/oversized"),
        entry_path=Path("/tmp/oversized/SKILL.md"),
        source_scope="project",
        source_kind="native",
        source_family=".alysis_skills",
        source_path=Path("/tmp/oversized"),
        trust_level="untrusted",
    )

    payload = build_explicit_skill_context_message(
        skill=skill,
        task_text="δοκιμή " * 4_000,
    )

    assert len(payload) <= EXPLICIT_SKILL_CONTEXT_TOTAL_MAX_CHARS
    assert "entrypoint_notice:" in payload
    assert "The direct user task remains available in the next user message." in payload
    assert payload.endswith("</explicit_skill_context>\n")


def test_explicit_skill_context_payload_stays_structurally_closed_with_oversized_metadata() -> None:
    skill = SkillBundle(
        name="n" * 2_000,
        description="d" * 3_000,
        instructions='Inspect "$ARGUMENTS" carefully.',
        bundle_name="oversized",
        bundle_path=Path("/tmp/oversized"),
        entry_path=Path("/tmp/oversized/SKILL.md"),
        source_scope="project",
        source_kind="native",
        source_family=".alysis_skills",
        source_path=Path("/tmp/" + ("nested/" * 80) + "oversized"),
        trust_level="untrusted",
    )

    payload = build_explicit_skill_context_message(
        skill=skill,
        task_text="run audit",
    )

    assert len(payload) <= EXPLICIT_SKILL_CONTEXT_TOTAL_MAX_CHARS
    assert payload.startswith("<explicit_skill_context>\n")
    assert payload.endswith("</explicit_skill_context>\n")
    assert "\n</skill_instructions>\n</explicit_skill_context>\n" in payload
