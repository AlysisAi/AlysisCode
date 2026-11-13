from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from rich.console import Console

import alysis_code.agent_loop as agent_loop_mod
from alysis_code.agent.tools_assembly import _BUILTIN_MODEL_DESCRIPTIONS
from alysis_code.agent_loop import build_tools
from alysis_code.config import AppConfig
from alysis_code.llm.metadata import endpoint_descriptor
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.session_store import SessionStore, read_session_events
from alysis_code.subagents import built_in_subagents
from alysis_code.tools.availability import (
    ToolSetupError,
    _reset_tool_availability_for_tests,
    get_tool_availability,
    is_tool_unavailable_result,
    mark_available,
    mark_unavailable,
    register_tool_availability,
    unavailable_tool_result,
)
from alysis_code.tools.registry import (
    REPORT_BLOCKER_MAX_MESSAGE_CHARS,
    built_in_subagent_tool_names,
    builtin_tool_names_with_category,
    get_builtin_tool_metadata,
    iter_builtin_tool_metadata,
    summarize_tool_output_chunk,
)
from alysis_code.web_research import build_web_research_artifact_from_events


@pytest.fixture(autouse=True)
def _clear_generic_web_search_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_API_KEY", raising=False)


def _store(root: Path, *, enabled: bool = False) -> SessionStore:
    return SessionStore(
        enabled=enabled,
        sessions_dir=root / "sessions",
        session_id="registry-test",
        cwd=str(root),
        repo_root=str(root),
    )


def _fake_git_repo(root: Path) -> None:
    git_dir = root / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text("0" * 40 + "\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_tool_availability_policy_state() -> Iterator[None]:
    _reset_tool_availability_for_tests()
    yield
    _reset_tool_availability_for_tests()


def test_builtin_tool_registry_metadata_is_complete_and_unique() -> None:
    metadata = iter_builtin_tool_metadata()
    names = [spec.name for spec in metadata]

    assert names
    assert len(names) == len(set(names))
    for spec in metadata:
        assert spec.description.strip()
        assert spec.parameters["type"] == "object"
        assert isinstance(spec.parameters.get("properties"), dict)
        assert spec.categories
        assert spec.rich.display_name.strip()
        assert spec.rich.reasoning_hint.strip()
        assert spec.rich.action_hint.strip()
        assert spec.rich.fallback_hint.strip()


def test_report_blocker_registry_contract_is_free_form_and_hidden_from_subagents() -> None:
    metadata = get_builtin_tool_metadata("report_blocker")
    properties = metadata.parameters["properties"]

    assert set(properties) == {"message"}
    assert metadata.parameters["required"] == ["message"]
    assert properties["message"]["type"] == "string"
    assert properties["message"]["maxLength"] == REPORT_BLOCKER_MAX_MESSAGE_CHARS
    assert "enum" not in properties["message"]
    assert "category" not in json.dumps(metadata.parameters).casefold()
    assert metadata.built_in_subagent_exposure == "hidden"
    assert "report_blocker" not in built_in_subagent_tool_names(exposure="readonly")


def test_report_blocker_accepts_any_language_without_category_keywords(tmp_path: Path) -> None:
    _fake_git_repo(tmp_path)
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        non_interactive=True,
        one_shot_execution=True,
        completion_gate_tools_enabled=True,
        runtime_kind=RuntimeKind.ONE_SHOT,
    )

    for message in (
        "Δεν είναι διαθέσιμη η απαιτούμενη εξωτερική είσοδος.",
        "لا يمكن الوصول إلى الخدمة الخارجية الآن.",
        "必要な外部サービスに接続できません。",
        "¿¡…?!",
    ):
        assert tools["report_blocker"].run({"message": message}) == {
            "ok": True,
            "reported": True,
            "message": message,
        }


def test_report_blocker_is_exposed_only_to_eligible_top_level_runtimes(
    tmp_path: Path,
) -> None:
    cases = (
        (RuntimeKind.ONE_SHOT, 0, True, True),
        (RuntimeKind.FORGE_EXEC, 0, True, True),
        (RuntimeKind.INTERACTIVE_CHAT, 0, True, True),
        (RuntimeKind.ONE_SHOT, 0, False, False),
        (RuntimeKind.SUBAGENT, 1, True, False),
        (RuntimeKind.SWARM_WORKER, 0, True, False),
        (RuntimeKind.CONFLICT_AUTO_RESOLVE, 0, True, False),
    )

    for index, (runtime_kind, subagent_depth, enabled, expected) in enumerate(cases):
        root = tmp_path / f"case-{index}"
        root.mkdir()
        _fake_git_repo(root)
        tools = build_tools(
            root=root,
            console=Console(file=io.StringIO()),
            store=_store(root),
            mode="auto",
            yes=True,
            non_interactive=True,
            one_shot_execution=runtime_kind in {RuntimeKind.ONE_SHOT, RuntimeKind.FORGE_EXEC},
            completion_gate_tools_enabled=enabled,
            subagent_depth=subagent_depth,
            runtime_kind=runtime_kind,
        )

        assert ("report_blocker" in tools) is expected


def test_shell_background_family_metadata_present() -> None:
    metadata_by_name = {spec.name: spec for spec in iter_builtin_tool_metadata()}
    expected_required = {
        "shell_background": ["cmd"],
        "shell_output": ["process_id"],
        "shell_kill": ["process_id"],
        "shell_list": [],
    }

    for tool_name, required in expected_required.items():
        metadata = metadata_by_name[tool_name]
        assert metadata.description.strip()
        assert metadata.parameters["type"] == "object"
        assert metadata.parameters.get("required") == required
        assert isinstance(metadata.parameters.get("properties"), dict)
        assert metadata.rich.display_name.strip()


def test_background_subagent_tool_metadata_and_spawn_schema() -> None:
    metadata_by_name = {spec.name: spec for spec in iter_builtin_tool_metadata()}
    background_names = {
        "subagent_spawn",
        "subagent_send",
        "subagent_resume",
        "subagent_status",
        "subagent_wait",
        "subagent_cancel",
    }

    assert background_names.issubset(metadata_by_name)
    spawn_description = metadata_by_name["subagent_spawn"].description
    assert "bounded child assignment while you do distinct work" in spawn_description
    assert "Completed reports arrive at parent model boundaries" in spawn_description
    assert "depends_on only for real dependencies" in spawn_description
    wait_description = metadata_by_name["subagent_wait"].description
    assert "when a needed result is still outstanding" in wait_description
    assert "Automatically delivered completed reports need no recollection" in wait_description
    spawn_properties = metadata_by_name["subagent_spawn"].parameters["properties"]
    run_properties = metadata_by_name["subagent_run"].parameters["properties"]
    assert set(run_properties).issubset(spawn_properties)
    expected_modes = ["readonly", "review", "auto", "fullaccess"]
    assert run_properties["mode"]["enum"] == expected_modes
    assert spawn_properties["mode"]["enum"] == expected_modes
    assert spawn_properties["run_id"]["maxLength"] == 64
    assert spawn_properties["depends_on"]["uniqueItems"] is True
    assert "workspace_from_run" in metadata_by_name["subagent_run"].parameters["properties"]
    assert metadata_by_name["subagent_wait"].parameters["properties"]["run_id"]["default"] == "all"
    assert (
        metadata_by_name["subagent_send"].parameters["properties"]["message"]["maxLength"] == 4000
    )
    assert metadata_by_name["subagent_resume"].parameters["required"] == ["run_id"]
    assert (
        metadata_by_name["subagent_resume"].parameters["properties"]["reattach_workspace"][
            "default"
        ]
        is True
    )
    assert (
        metadata_by_name["subagent_cancel"].parameters["properties"]["run_id"]["default"] == "all"
    )


def test_shell_background_family_in_shell_category() -> None:
    shell_tools = set(builtin_tool_names_with_category("shell"))

    assert {
        "shell_run",
        "shell_background",
        "shell_output",
        "shell_kill",
        "shell_list",
    } <= shell_tools


def test_process_lifetime_is_visible_in_model_and_registry_descriptions() -> None:
    background = _BUILTIN_MODEL_DESCRIPTIONS["shell_background"]
    service = _BUILTIN_MODEL_DESCRIPTIONS["shell_service_start"]
    preview = _BUILTIN_MODEL_DESCRIPTIONS["workspace_preview_start"]

    assert "session lifetime" in background[:140]
    assert "killed when this session ends" in background[:140]
    assert "durable lifetime" in service[:140]
    assert "durable service" in service[:140]
    assert "keeps running after this session ends" in service[:140]
    assert "keeps running after this session ends" not in background
    assert "killed when this session ends" not in service
    assert "semantic access" in preview
    assert "free port" in preview
    assert "without Docker" in preview

    status = _BUILTIN_MODEL_DESCRIPTIONS["shell_service_status"]
    stop = _BUILTIN_MODEL_DESCRIPTIONS["shell_service_stop"]
    assert "outlives the session" in status[:140]
    assert "keep running after the session ends" in stop[:140]

    registry_background = get_builtin_tool_metadata("shell_background").description
    registry_service = get_builtin_tool_metadata("shell_service_start").description
    registry_preview = get_builtin_tool_metadata("workspace_preview_start").description
    assert "terminated when the session closes" in registry_background
    assert "dev servers you only need while this session is running" in registry_background
    assert "keeps running after the session ends" in registry_service
    assert "AgentSession.close" not in registry_service
    assert "auto, local, or lan" in registry_preview
    assert "allocates a free port" in registry_preview
    assert "does not require Docker" in registry_preview


def test_build_tools_registers_all_non_optional_catalogued_builtin_tools(tmp_path: Path) -> None:
    _fake_git_repo(tmp_path)
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path, enabled=True),
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    catalog_names = {spec.name for spec in iter_builtin_tool_metadata() if not spec.optional}
    assert set(tools) == catalog_names - {"web_search", "skill_read"}


def test_skill_read_schema_states_semantic_selection_boundary() -> None:
    description = get_builtin_tool_metadata("skill_read").description

    assert "requested actions" in description
    assert "concept mentions" in description
    assert "explicit exclusions" in description
    assert "most specific" in description


def test_optional_unavailable_tool_returns_structured_non_error_result_and_logs_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="alysis_code.tools.availability")
    reason = "module not importable: fake_optional_dependency"

    register_tool_availability("fake_optional_tool", optional=True)
    mark_unavailable("fake_optional_tool", reason)
    mark_unavailable("fake_optional_tool", reason)

    result = unavailable_tool_result("fake_optional_tool")
    assert result == {
        "status": "tool_unavailable",
        "tool": "fake_optional_tool",
        "reason": reason,
    }
    assert result is not None
    assert "error" not in result
    assert is_tool_unavailable_result(result)
    assert reason and reason != "unavailable"

    records = [
        record
        for record in caplog.records
        if record.name == "alysis_code.tools.availability"
        and record.getMessage().startswith("optional_tool_unavailable")
        and "fake_optional_tool" in record.getMessage()
    ]
    assert len(records) == 1
    assert reason in records[0].getMessage()


def test_optional_available_tool_has_no_unavailable_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="alysis_code.tools.availability")

    register_tool_availability("fake_optional_tool", optional=True)
    mark_available("fake_optional_tool")

    assert unavailable_tool_result("fake_optional_tool") is None
    assert get_tool_availability("fake_optional_tool") is not None
    assert [
        record for record in caplog.records if "fake_optional_tool" in record.getMessage()
    ] == []


def test_required_unavailable_tool_fails_startup_with_concrete_reason() -> None:
    reason = "module not importable: fake_required_dependency"

    register_tool_availability("fake_required_tool", optional=False)

    with pytest.raises(
        ToolSetupError,
        match="tool fake_required_tool is required but unavailable: module not importable",
    ):
        mark_unavailable("fake_required_tool", reason)


def test_required_available_tool_has_no_unavailable_result() -> None:
    state = register_tool_availability("fake_required_tool", optional=False)
    assert state.optional is False

    available = mark_available("fake_required_tool")

    assert available.optional is False
    assert unavailable_tool_result("fake_required_tool") is None


def test_optional_to_required_collision_fails_if_tool_is_unavailable() -> None:
    register_tool_availability("fake_optional_tool", optional=True)
    mark_unavailable("fake_optional_tool", "module not importable: fake_optional_dependency")

    with pytest.raises(ToolSetupError, match="tool fake_optional_tool is required but unavailable"):
        register_tool_availability("fake_optional_tool", optional=False)


def test_generic_unavailable_reason_rejects_placeholder() -> None:
    register_tool_availability("fake_optional_tool", optional=True)

    with pytest.raises(ValueError, match="concrete and non-empty"):
        mark_unavailable("fake_optional_tool", "unavailable")


def test_availability_policy_module_has_no_knowledge_capture_literal() -> None:
    source = (
        Path(__file__).parents[1] / "src" / "alysis_code" / "tools" / "availability.py"
    ).read_text(encoding="utf-8")

    assert "knowledge_capture_json" not in source


def test_unavailable_tool_summary_renders_concrete_reason() -> None:
    result = {
        "status": "tool_unavailable",
        "tool": "fake_optional_tool",
        "reason": "module not importable: fake_optional_dependency",
    }

    summary = summarize_tool_output_chunk("fake_optional_tool", json.dumps(result))

    assert summary == (
        "Tool unavailable: fake_optional_tool: module not importable: fake_optional_dependency"
    )


def test_knowledge_capture_json_is_optional_and_marked_unavailable_by_build_tools(
    tmp_path: Path,
) -> None:
    _fake_git_repo(tmp_path)
    reason = "not registered in active tool registry; knowledge capture is a final assistant fenced block parsed by host"

    metadata = get_builtin_tool_metadata("knowledge_capture_json")
    assert metadata is not None
    assert metadata.optional is True

    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path, enabled=True),
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=False,
        subagent_registry={},
    )

    assert "knowledge_capture_json" not in tools
    assert unavailable_tool_result("knowledge_capture_json") == {
        "status": "tool_unavailable",
        "tool": "knowledge_capture_json",
        "reason": reason,
    }


def test_build_tools_omits_history_search_without_artifact_persistence(
    tmp_path: Path,
) -> None:
    _fake_git_repo(tmp_path)
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path, enabled=False),
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=False,
        subagent_registry={},
    )

    assert "history_search" not in tools
    assert "session_artifact_read" not in tools


def test_build_tools_keeps_history_search_with_explicit_artifact_root_even_without_logging(
    tmp_path: Path,
) -> None:
    _fake_git_repo(tmp_path)
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path, enabled=False),
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=False,
        subagent_registry={},
        session_log_dir_override=tmp_path / "external-sessions",
    )

    assert "history_search" in tools
    assert "session_artifact_read" in tools


def test_build_tools_omits_git_history_in_plain_directory(tmp_path: Path) -> None:
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path, enabled=True),
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=False,
        subagent_registry={},
    )

    assert "git_history" not in tools


def test_build_tools_registers_skill_read_when_skills_are_available(tmp_path: Path) -> None:
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path, enabled=True),
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        skills_enabled=True,
        skill_registry={
            "python": type(
                "Skill",
                (),
                {
                    "name": "python",
                    "bundle_name": "python",
                    "source_path": tmp_path / ".alysis_skills" / "python",
                    "bundle_path": tmp_path / ".alysis_skills" / "python",
                    "entry_path": tmp_path / ".alysis_skills" / "python" / "SKILL.md",
                    "source_scope": "project",
                    "source_kind": "native",
                    "source_family": ".alysis_skills",
                    "trust_level": "untrusted",
                },
            )()
        },
        subagents_enabled=False,
        subagent_registry={},
    )

    assert "skill_read" in tools


def test_build_tools_omits_skill_read_when_skills_are_disabled(tmp_path: Path) -> None:
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path, enabled=True),
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        skills_enabled=False,
        skill_registry={"python": object()},
        subagents_enabled=False,
        subagent_registry={},
    )

    assert "skill_read" not in tools


def test_history_search_is_classified_as_read_tool() -> None:
    assert "history_search" in builtin_tool_names_with_category("read")
    assert "history_search" not in builtin_tool_names_with_category("write")


def test_web_fetch_is_catalogued_as_read_web_tool() -> None:
    names = {spec.name for spec in iter_builtin_tool_metadata()}
    assert "web_fetch" in names
    assert "web_fetch" in builtin_tool_names_with_category("read")
    assert "web_fetch" in builtin_tool_names_with_category("web")
    assert "web_fetch" not in builtin_tool_names_with_category("write")


def test_web_search_is_catalogued_as_read_web_search_tool() -> None:
    names = {spec.name for spec in iter_builtin_tool_metadata()}
    assert "web_search" in names
    assert "web_search" in builtin_tool_names_with_category("read")
    assert "web_search" in builtin_tool_names_with_category("web")
    assert "web_search" in builtin_tool_names_with_category("search")
    assert "web_search" not in builtin_tool_names_with_category("write")


def test_web_tool_metadata_discourages_invented_urls() -> None:
    web_fetch_spec = next(spec for spec in iter_builtin_tool_metadata() if spec.name == "web_fetch")
    web_search_spec = next(
        spec for spec in iter_builtin_tool_metadata() if spec.name == "web_search"
    )

    assert "do not guess or invent URLs" in web_fetch_spec.description
    assert "user-provided URL, one returned by web_search" in web_fetch_spec.description
    assert "configured trusted domain" in web_fetch_spec.description
    assert "user provided, one web_search returned" in web_search_spec.description
    assert "user-provided direct public URL" in web_search_spec.rich.fallback_hint


def test_fs_mkdir_is_catalogued_as_write_fs_tool() -> None:
    names = {spec.name for spec in iter_builtin_tool_metadata()}
    assert "fs_mkdir" in names
    assert "fs_mkdir" in builtin_tool_names_with_category("write")
    assert "fs_mkdir" in builtin_tool_names_with_category("fs")
    assert "fs_mkdir" not in builtin_tool_names_with_category("read")


def test_built_in_subagent_exposure_matches_catalog_policy() -> None:
    expected = built_in_subagent_tool_names(exposure="readonly")
    registry = built_in_subagents()

    assert expected
    assert "shell_run" not in expected
    assert "verify_run" not in expected
    assert "git_apply_patch" not in expected
    assert "image_generate" not in expected
    assert "subagent_run" not in expected
    assert "history_search" in expected
    assert "session_artifact_read" in expected
    assert "web_fetch" not in expected
    assert "web_search" not in expected
    assert registry["explorer"].allow_tools == expected
    assert registry["code-reviewer"].allow_tools == tuple(
        name for name in expected if name != "git_history"
    )
    assert registry["debugger"].allow_tools == (*expected, "shell_run", "verify_run")
    assert registry["general"].allow_tools == ()
    assert registry["general"].deny_tools == ("image_generate",)
    assert registry["frontend-engineer"].allow_tools == ()
    assert registry["frontend-engineer"].deny_tools == ("image_generate",)
    assert registry["visual-designer"].allow_tools == (*expected, "image_generate")


def test_image_generate_metadata_is_billable_optional_write_tool() -> None:
    metadata = get_builtin_tool_metadata("image_generate")

    assert metadata is not None
    assert metadata.optional is True
    assert {"write", "image", "generation", "network"} <= set(metadata.categories)
    assert metadata.parameters["required"] == ["prompt", "output_path"]
    assert metadata.parameters["properties"]["count"]["maximum"] == 4
    assert "never overwritten" in metadata.description


def test_image_generate_tool_is_opt_in(
    tmp_path: Path,
) -> None:
    disabled = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        cfg=AppConfig(model="test-model"),
    )
    assert "image_generate" not in disabled

    cfg = AppConfig(
        model="test-model",
        image_generation={
            "enabled": True,
            "model": "gpt-image-test",
            "base_url": "https://images.example.test/v1",
        },
    )
    enabled = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        cfg=cfg,
        api_key="test-key",
    )
    assert "image_generate" in enabled
    with pytest.raises(RuntimeError, match="protected path"):
        enabled["image_generate"].run(
            {
                "prompt": "An asset that must never be written into Git metadata.",
                "output_path": ".git/generated.png",
            }
        )


def test_web_fetch_schema_sets_max_chars_bounds() -> None:
    web_fetch_spec = next(spec for spec in iter_builtin_tool_metadata() if spec.name == "web_fetch")
    max_chars = web_fetch_spec.parameters["properties"]["max_chars"]

    assert max_chars["minimum"] == 1
    assert max_chars["maximum"] == 50000


def test_fs_read_schema_sets_bounded_default_max_bytes() -> None:
    fs_read_spec = next(spec for spec in iter_builtin_tool_metadata() if spec.name == "fs_read")
    max_bytes = fs_read_spec.parameters["properties"]["max_bytes"]

    assert max_bytes["default"] == 12000


def test_fs_edit_schema_requires_operation_specific_fields() -> None:
    fs_edit_spec = next(spec for spec in iter_builtin_tool_metadata() if spec.name == "fs_edit")
    edit_variants = fs_edit_spec.parameters["properties"]["edits"]["items"]["anyOf"]

    variants_by_ops = {
        tuple(variant["properties"]["op"]["enum"]): variant for variant in edit_variants
    }

    replace_variant = variants_by_ops[("replace", "replace_exact")]
    assert replace_variant["required"] == ["op", "target", "replacement"]
    assert "content" not in replace_variant["properties"]

    insert_variant = variants_by_ops[("insert_before_exact", "insert_after_exact")]
    assert insert_variant["required"] == ["op", "target", "content"]
    assert "replacement" not in insert_variant["properties"]

    append_variant = variants_by_ops[("append", "prepend")]
    assert append_variant["required"] == ["op", "content"]
    assert "target" not in append_variant["properties"]
    assert "replacement" not in append_variant["properties"]

    assert fs_edit_spec.parameters["properties"]["edits"]["minItems"] == 1
    assert all(variant["additionalProperties"] is False for variant in edit_variants)


def test_chat_path_tools_default_to_active_workdir_with_workspace_root_escape_hatch() -> None:
    fs_read_spec = next(spec for spec in iter_builtin_tool_metadata() if spec.name == "fs_read")
    fs_write_spec = next(spec for spec in iter_builtin_tool_metadata() if spec.name == "fs_write")
    fs_move_spec = next(spec for spec in iter_builtin_tool_metadata() if spec.name == "fs_move")
    fs_list_spec = next(spec for spec in iter_builtin_tool_metadata() if spec.name == "fs_list")
    search_rg_spec = next(spec for spec in iter_builtin_tool_metadata() if spec.name == "search_rg")
    symbol_search_spec = next(
        spec for spec in iter_builtin_tool_metadata() if spec.name == "symbol_search"
    )
    shell_run_spec = next(spec for spec in iter_builtin_tool_metadata() if spec.name == "shell_run")

    assert fs_read_spec.parameters["properties"]["path_base"]["default"] == "active_workdir"
    assert fs_write_spec.parameters["properties"]["path_base"]["default"] == "active_workdir"
    assert fs_move_spec.parameters["properties"]["source_path_base"]["default"] == "active_workdir"
    assert (
        fs_move_spec.parameters["properties"]["destination_path_base"]["default"]
        == "active_workdir"
    )
    assert fs_list_spec.parameters["properties"]["path_base"]["default"] == "active_workdir"
    assert search_rg_spec.parameters["properties"]["path_base"]["default"] == "active_workdir"
    assert symbol_search_spec.parameters["properties"]["path_base"]["default"] == "active_workdir"
    assert shell_run_spec.parameters["properties"]["cwd_base"]["default"] == "active_workdir"
    assert "default" not in fs_list_spec.parameters["properties"]["root_path"]
    assert "default" not in search_rg_spec.parameters["properties"]["root_path"]
    assert "default" not in symbol_search_spec.parameters["properties"]["root_path"]


def test_session_set_workdir_metadata_mentions_natural_language_navigation_examples() -> None:
    spec = next(spec for spec in iter_builtin_tool_metadata() if spec.name == "session_set_workdir")

    assert "go to packages/app" in spec.description
    assert "work in apps/web" in spec.rich.action_hint


def test_build_tools_web_fetch_preserves_explicit_max_chars_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_web_fetch(
        *, url: str, max_chars: object, transport: object | None = None
    ) -> dict[str, object]:
        observed["url"] = url
        observed["max_chars"] = max_chars
        observed["transport"] = transport
        return {"ok": True}

    monkeypatch.setattr(agent_loop_mod, "web_fetch", fake_web_fetch)

    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=(store := _store(tmp_path)),
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )
    store.append(
        "user_message",
        {"content": "Please inspect https://example.com/spec before making the change."},
    )

    result = tools["web_fetch"].run({"url": "https://example.com/spec", "max_chars": 0})

    assert result == {"ok": True}
    assert observed["url"] == "https://example.com/spec"
    assert observed["max_chars"] == 0


def test_build_tools_web_fetch_allows_user_provided_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_web_fetch(
        *, url: str, max_chars: object, transport: object | None = None
    ) -> dict[str, object]:
        observed["url"] = url
        observed["max_chars"] = max_chars
        return {"url": url, "final_url": url, "status_code": 200}

    monkeypatch.setattr(agent_loop_mod, "web_fetch", fake_web_fetch)
    store = _store(tmp_path)
    store.append(
        "user_message",
        {"content": "Please read https://docs.example.com/start for the bug report."},
    )
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=store,
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    result = tools["web_fetch"].run({"url": "https://docs.example.com/start"})

    assert result["status_code"] == 200
    assert observed["url"] == "https://docs.example.com/start"


@pytest.mark.parametrize(
    ("message", "tool_url", "expected_url"),
    [
        (
            "Please read https://docs.example.com/spec. before the bugfix.",
            "https://docs.example.com/spec.",
            "https://docs.example.com/spec",
        ),
        (
            "Please read (https://docs.example.com/spec) before the bugfix.",
            "(https://docs.example.com/spec)",
            "https://docs.example.com/spec",
        ),
        (
            'Please read "https://docs.example.com/spec" before the bugfix.',
            'https://docs.example.com/spec"',
            "https://docs.example.com/spec",
        ),
    ],
)
def test_build_tools_web_fetch_canonicalizes_user_provided_punctuation_variants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    tool_url: str,
    expected_url: str,
) -> None:
    observed: dict[str, object] = {}

    def fake_web_fetch(
        *, url: str, max_chars: object, transport: object | None = None
    ) -> dict[str, object]:
        observed["url"] = url
        return {"url": url, "final_url": url, "status_code": 200}

    monkeypatch.setattr(agent_loop_mod, "web_fetch", fake_web_fetch)
    store = _store(tmp_path)
    store.append("user_message", {"content": message})
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=store,
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    result = tools["web_fetch"].run({"url": tool_url})

    assert result["status_code"] == 200
    assert observed["url"] == expected_url
    assert result["url"] == expected_url


def test_build_tools_web_fetch_allows_url_returned_by_web_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_web_fetch(
        *, url: str, max_chars: object, transport: object | None = None
    ) -> dict[str, object]:
        observed["url"] = url
        return {"url": url, "final_url": url, "status_code": 200}

    monkeypatch.setattr(agent_loop_mod, "web_fetch", fake_web_fetch)
    store = _store(tmp_path)
    store.append(
        "tool_result",
        {
            "name": "web_search",
            "step": 1,
            "result": {
                "query": "docs example",
                "backend": "openai_responses",
                "sources": [
                    {
                        "title": "Spec",
                        "url": "https://docs.example.com/spec",
                        "snippet": "Official docs",
                    }
                ],
            },
        },
    )
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=store,
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    result = tools["web_fetch"].run({"url": "https://docs.example.com/spec"})

    assert result["status_code"] == 200
    assert observed["url"] == "https://docs.example.com/spec"


def test_build_tools_web_fetch_preserves_structured_search_result_url_with_colon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_web_fetch(
        *, url: str, max_chars: object, transport: object | None = None
    ) -> dict[str, object]:
        observed["url"] = url
        return {"url": url, "final_url": url, "status_code": 200}

    monkeypatch.setattr(agent_loop_mod, "web_fetch", fake_web_fetch)
    store = _store(tmp_path)
    clean_url = "https://docs.example.com/path:"
    store.append(
        "tool_result",
        {
            "name": "web_search",
            "step": 1,
            "result": {
                "query": "docs example",
                "backend": "openai_responses",
                "sources": [
                    {
                        "title": "Spec",
                        "url": clean_url,
                        "snippet": "Official docs",
                    }
                ],
            },
        },
    )
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=store,
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    result = tools["web_fetch"].run({"url": clean_url})

    assert result["status_code"] == 200
    assert observed["url"] == clean_url
    assert result["url"] == clean_url


def test_build_tools_web_fetch_canonicalizes_url_returned_by_web_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_web_fetch(
        *, url: str, max_chars: object, transport: object | None = None
    ) -> dict[str, object]:
        observed["url"] = url
        return {"url": url, "final_url": url, "status_code": 200}

    monkeypatch.setattr(agent_loop_mod, "web_fetch", fake_web_fetch)
    store = _store(tmp_path)
    store.append(
        "tool_result",
        {
            "name": "web_search",
            "step": 1,
            "result": {
                "query": "docs example",
                "backend": "openai_responses",
                "sources": [
                    {
                        "title": "Spec",
                        "url": "https://docs.example.com/spec",
                        "snippet": "Official docs",
                    }
                ],
            },
        },
    )
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=store,
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    result = tools["web_fetch"].run({"url": "https://docs.example.com/spec."})

    assert result["status_code"] == 200
    assert observed["url"] == "https://docs.example.com/spec"
    assert result["url"] == "https://docs.example.com/spec"


def test_build_tools_web_fetch_allows_parenthesized_markdown_link_target_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_web_fetch(
        *, url: str, max_chars: object, transport: object | None = None
    ) -> dict[str, object]:
        observed["url"] = url
        return {"url": url, "final_url": url, "status_code": 200}

    monkeypatch.setattr(agent_loop_mod, "web_fetch", fake_web_fetch)
    store = _store(tmp_path)
    clean_url = "https://docs.example.com/spec"
    store.append(
        "user_message",
        {"content": "Please read ([docs](https://docs.example.com/spec)) before the bugfix."},
    )
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=store,
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    result = tools["web_fetch"].run({"url": clean_url})

    assert result["status_code"] == 200
    assert observed["url"] == clean_url
    assert result["url"] == clean_url

    corrupted_result = tools["web_fetch"].run({"url": "https://docs.example.com/spec)"})
    assert corrupted_result["error_code"] == "web_fetch_provenance_required"
    assert observed["url"] == clean_url


def test_build_tools_web_fetch_allows_clean_url_after_markdown_wrapped_user_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_web_fetch(
        *, url: str, max_chars: object, transport: object | None = None
    ) -> dict[str, object]:
        observed["url"] = url
        return {"url": url, "final_url": url, "status_code": 200}

    monkeypatch.setattr(agent_loop_mod, "web_fetch", fake_web_fetch)
    store = _store(tmp_path)
    store.append(
        "user_message",
        {"content": "Please read `https://docs.example.com/spec` and use it."},
    )
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=store,
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    result = tools["web_fetch"].run({"url": "https://docs.example.com/spec"})

    assert result["status_code"] == 200
    assert observed["url"] == "https://docs.example.com/spec"
    assert result["url"] == "https://docs.example.com/spec"


def test_build_tools_web_fetch_allows_bracket_query_user_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_web_fetch(
        *, url: str, max_chars: object, transport: object | None = None
    ) -> dict[str, object]:
        observed["url"] = url
        return {"url": url, "final_url": url, "status_code": 200}

    monkeypatch.setattr(agent_loop_mod, "web_fetch", fake_web_fetch)
    store = _store(tmp_path)
    clean_url = "https://docs.example.com/path?foo[bar]=1"
    store.append(
        "user_message",
        {"content": f"Please read {clean_url} before the bugfix."},
    )
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=store,
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    result = tools["web_fetch"].run({"url": clean_url})

    assert result["status_code"] == 200
    assert observed["url"] == clean_url
    assert result["url"] == clean_url

    truncated_result = tools["web_fetch"].run({"url": "https://docs.example.com/path?foo"})
    assert truncated_result["error_code"] == "web_fetch_provenance_required"
    assert observed["url"] == clean_url


def test_build_tools_web_fetch_allows_exclamation_user_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_web_fetch(
        *, url: str, max_chars: object, transport: object | None = None
    ) -> dict[str, object]:
        observed["url"] = url
        return {"url": url, "final_url": url, "status_code": 200}

    monkeypatch.setattr(agent_loop_mod, "web_fetch", fake_web_fetch)
    store = _store(tmp_path)
    clean_url = "https://docs.example.com/Yahoo!"
    store.append(
        "user_message",
        {"content": f"Please read {clean_url} before the bugfix."},
    )
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=store,
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    result = tools["web_fetch"].run({"url": clean_url})

    assert result["status_code"] == 200
    assert observed["url"] == clean_url
    assert result["url"] == clean_url

    truncated_result = tools["web_fetch"].run({"url": "https://docs.example.com/Yahoo"})
    assert truncated_result["error_code"] == "web_fetch_provenance_required"
    assert observed["url"] == clean_url


def test_build_tools_web_fetch_allows_markdown_link_url_with_legal_trailing_parenthesis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_web_fetch(
        *, url: str, max_chars: object, transport: object | None = None
    ) -> dict[str, object]:
        observed["url"] = url
        return {"url": url, "final_url": url, "status_code": 200}

    monkeypatch.setattr(agent_loop_mod, "web_fetch", fake_web_fetch)
    store = _store(tmp_path)
    clean_url = "https://docs.example.com/path?x=a)"
    store.append(
        "user_message",
        {"content": f"Please read [docs]({clean_url}) before the bugfix."},
    )
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=store,
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    result = tools["web_fetch"].run({"url": clean_url})

    assert result["status_code"] == 200
    assert observed["url"] == clean_url
    assert result["url"] == clean_url


def test_build_tools_web_fetch_allows_markdown_link_title_parenthesized_target_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_web_fetch(
        *, url: str, max_chars: object, transport: object | None = None
    ) -> dict[str, object]:
        observed["url"] = url
        return {"url": url, "final_url": url, "status_code": 200}

    monkeypatch.setattr(agent_loop_mod, "web_fetch", fake_web_fetch)
    store = _store(tmp_path)
    clean_url = "https://docs.example.com/Function_(mathematics)"
    truncated_url = "https://docs.example.com/Function_(mathematics"
    store.append(
        "user_message",
        {
            "content": (
                "Please read [Function](https://docs.example.com/Function_(mathematics) "
                '"Function docs") before changes.'
            )
        },
    )
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=store,
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    result = tools["web_fetch"].run({"url": clean_url})

    assert result["status_code"] == 200
    assert observed["url"] == clean_url
    assert result["url"] == clean_url

    truncated_result = tools["web_fetch"].run({"url": truncated_url})
    assert truncated_result["error_code"] == "web_fetch_provenance_required"
    assert observed["url"] == clean_url


def test_build_tools_web_fetch_preserves_parenthesized_search_result_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_web_fetch(
        *, url: str, max_chars: object, transport: object | None = None
    ) -> dict[str, object]:
        observed["url"] = url
        return {"url": url, "final_url": url, "status_code": 200}

    monkeypatch.setattr(agent_loop_mod, "web_fetch", fake_web_fetch)
    store = _store(tmp_path)
    store.append(
        "tool_result",
        {
            "name": "web_search",
            "step": 1,
            "result": {
                "query": "function mathematics docs",
                "backend": "dashscope_chat",
                "sources": [
                    {
                        "title": "Function (mathematics)",
                        "url": "https://docs.example.com/Function_(mathematics)",
                        "snippet": "Balanced parenthesized path",
                    }
                ],
            },
        },
    )
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=store,
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    result = tools["web_fetch"].run({"url": "https://docs.example.com/Function_(mathematics)"})

    assert result["status_code"] == 200
    assert observed["url"] == "https://docs.example.com/Function_(mathematics)"
    assert result["url"] == "https://docs.example.com/Function_(mathematics)"


def test_build_tools_web_fetch_blocks_guessed_public_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_web_fetch(
        *, url: str, max_chars: object, transport: object | None = None
    ) -> dict[str, object]:
        nonlocal called
        called = True
        return {"url": url, "final_url": url, "status_code": 200}

    monkeypatch.setattr(agent_loop_mod, "web_fetch", fake_web_fetch)
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    result = tools["web_fetch"].run({"url": "https://guessed.example.com/spec"})

    assert called is False
    assert result["error_code"] == "web_fetch_provenance_required"
    assert "user or one returned by web_search" in result["error"]
    # Nothing has been searched/provided, so there is nothing to offer.
    assert "fetchable_urls" not in result
    assert "Run web_search first" in result["guidance"]


def test_build_tools_web_fetch_allows_trusted_registry_domain_without_prior_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reproduces the dependency-scout incident: a fresh (sub)session with an
    # empty provenance store fetches package-registry JSON endpoints. Search
    # engines never index these, so search-mediated recovery cannot authorize
    # them; the default trusted-domain allowlist must.
    observed: dict[str, object] = {}

    def fake_web_fetch(
        *, url: str, max_chars: object, transport: object | None = None
    ) -> dict[str, object]:
        observed["url"] = url
        return {"url": url, "final_url": url, "status_code": 200}

    monkeypatch.setattr(agent_loop_mod, "web_fetch", fake_web_fetch)
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    result = tools["web_fetch"].run({"url": "https://registry.npmjs.org/vite/latest"})

    assert observed["url"] == "https://registry.npmjs.org/vite/latest"
    assert result["status_code"] == 200
    assert "error_code" not in result


def test_build_tools_web_fetch_trusted_domains_config_replaces_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called_urls: list[str] = []

    def fake_web_fetch(
        *, url: str, max_chars: object, transport: object | None = None
    ) -> dict[str, object]:
        called_urls.append(url)
        return {"url": url, "final_url": url, "status_code": 200}

    monkeypatch.setattr(agent_loop_mod, "web_fetch", fake_web_fetch)
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        cfg=AppConfig(
            model="test-model",
            web_fetch_trusted_domains=["internal-registry.example.com"],
        ),
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    # Subdomain of the configured host is authorized.
    ok = tools["web_fetch"].run({"url": "https://api.internal-registry.example.com/pkg"})
    assert ok["status_code"] == 200
    assert called_urls == ["https://api.internal-registry.example.com/pkg"]
    # The curated defaults are replaced, not merged: npm registry now blocks.
    blocked = tools["web_fetch"].run({"url": "https://registry.npmjs.org/vite/latest"})
    assert blocked["error_code"] == "web_fetch_provenance_required"
    assert "trusted_domain_allowlist" in blocked["allowed_provenance"]
    assert called_urls == ["https://api.internal-registry.example.com/pkg"]


def test_build_tools_web_fetch_trusted_domains_env_disable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_web_fetch(
        *, url: str, max_chars: object, transport: object | None = None
    ) -> dict[str, object]:
        nonlocal called
        called = True
        return {"url": url, "final_url": url, "status_code": 200}

    monkeypatch.setattr(agent_loop_mod, "web_fetch", fake_web_fetch)
    monkeypatch.setenv("ALYSIS_WEB_FETCH_TRUSTED_DOMAINS", "none")
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    result = tools["web_fetch"].run({"url": "https://registry.npmjs.org/vite/latest"})

    assert called is False
    assert result["error_code"] == "web_fetch_provenance_required"


def test_build_tools_web_fetch_rejection_lists_fetchable_search_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reproduces the reported incident: web_search returned real sources, but the
    # model then tried to fetch a URL it invented from memory. The gate must still
    # reject the invented URL, yet now hand back the genuinely fetchable sources so
    # the model can retry against one instead of dead-ending.
    called = False

    def fake_web_fetch(
        *, url: str, max_chars: object, transport: object | None = None
    ) -> dict[str, object]:
        nonlocal called
        called = True
        return {"url": url, "final_url": url, "status_code": 200}

    monkeypatch.setattr(agent_loop_mod, "web_fetch", fake_web_fetch)
    store = _store(tmp_path)
    store.append(
        "tool_result",
        {
            "name": "web_search",
            "step": 1,
            "result": {
                "query": "world cup news",
                "backend": "openrouter_web",
                "sources": [
                    {"title": "FIFA", "url": "https://www.fifa.com/worldcup/news"},
                    {"title": "UEFA", "url": "https://www.uefa.com/news"},
                ],
            },
        },
    )
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=store,
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    invented_url = "https://www.fifa.com/fifaplus/en/tournaments/mens/worldcup/canadamexicousa2026"
    result = tools["web_fetch"].run({"url": invented_url})

    assert called is False
    assert result["error_code"] == "web_fetch_provenance_required"
    assert result["fetchable_urls"], "rejection should list the real search sources"
    assert result["fetchable_urls"][0] == "https://www.fifa.com/worldcup/news"
    assert "fetchable_urls" in result["guidance"]
    # Retrying with a listed URL (verbatim) is authorized and actually fetches.
    ok = tools["web_fetch"].run({"url": result["fetchable_urls"][0]})
    assert ok["status_code"] == 200
    assert called is True


def test_build_tools_web_fetch_allows_previously_valid_url_after_store_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_web_fetch(
        *, url: str, max_chars: object, transport: object | None = None
    ) -> dict[str, object]:
        observed["url"] = url
        return {"url": url, "final_url": url, "status_code": 200}

    monkeypatch.setattr(agent_loop_mod, "web_fetch", fake_web_fetch)
    original_store = _store(tmp_path, enabled=True)
    original_store.append(
        "tool_result",
        {
            "name": "web_search",
            "step": 1,
            "result": {
                "query": "docs example",
                "backend": "openai_responses",
                "sources": [
                    {
                        "title": "Guide",
                        "url": "https://docs.example.com/guide",
                        "snippet": "Official guide",
                    }
                ],
            },
        },
    )
    original_store.close()

    reopened_store = _store(tmp_path, enabled=False)
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=reopened_store,
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    result = tools["web_fetch"].run({"url": "https://docs.example.com/guide"})

    assert result["status_code"] == 200
    assert observed["url"] == "https://docs.example.com/guide"


def test_build_tools_web_fetch_allows_artifact_only_url_after_store_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_web_fetch(
        *, url: str, max_chars: object, transport: object | None = None
    ) -> dict[str, object]:
        observed["url"] = url
        return {"url": url, "final_url": url, "status_code": 200}

    monkeypatch.setattr(agent_loop_mod, "web_fetch", fake_web_fetch)
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_id = "registry-artifact-ahead"
    logged_events = [
        {
            "type": "tool_result",
            "session_id": session_id,
            "payload": {
                "name": "web_search",
                "step": 1,
                "result": {
                    "query": "docs example start",
                    "backend": "openai_responses",
                    "sources": [
                        {
                            "title": "Start",
                            "url": "https://docs.example.com/start",
                            "snippet": "Initial source",
                        }
                    ],
                },
            },
        }
    ]
    artifact_only_events = [
        {
            "type": "tool_result",
            "session_id": session_id,
            "payload": {
                "name": "web_search",
                "step": 2,
                "result": {
                    "query": "docs example guide",
                    "backend": "openai_responses",
                    "sources": [
                        {
                            "title": "Guide",
                            "url": "https://docs.example.com/guide",
                            "snippet": "Artifact-only source",
                        }
                    ],
                },
            },
        }
    ]
    (sessions_dir / f"{session_id}.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=True) + "\n" for event in logged_events),
        encoding="utf-8",
    )
    artifact_root = sessions_dir / session_id
    artifact_root.mkdir(parents=True, exist_ok=True)
    (artifact_root / "web_research_sources.json").write_text(
        json.dumps(
            build_web_research_artifact_from_events(logged_events + artifact_only_events),
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )

    reopened_store = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=sessions_dir,
        session_id=session_id,
        cwd=str(tmp_path),
        repo_root=str(tmp_path),
    )
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=reopened_store,
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    result = tools["web_fetch"].run({"url": "https://docs.example.com/guide"})

    assert result["status_code"] == 200
    assert observed["url"] == "https://docs.example.com/guide"


def test_build_tools_does_not_register_web_search_when_mode_is_off(
    tmp_path: Path,
) -> None:
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        cfg=AppConfig(model="test-model", web_search_mode="off"),
        api_key="main-key",
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )
    assert "web_search" not in tools


def test_build_tools_does_not_register_web_search_when_policy_is_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        cfg=AppConfig(
            model="test-model",
            web_search_mode="auto",
            web_search_policy="off",
        ),
        api_key="main-key",
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    assert "web_search" not in tools


def test_build_tools_does_not_register_web_search_in_auto_mode_when_runtime_is_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_KEYLESS", "0")
    unconfigured_tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        cfg=AppConfig(
            model="test-model",
            base_url="https://example-proxy.invalid/v1",
            web_search_mode="auto",
        ),
        api_key="main-key",
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )
    assert "web_search" not in unconfigured_tools


def test_build_tools_registers_web_search_via_keyless_ddgs_without_any_search_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_KEYLESS", raising=False)
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        cfg=AppConfig(
            model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
            web_search_mode="auto",
        ),
        api_key="main-key",
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    assert "web_search" in tools


def test_build_tools_registers_web_search_in_auto_mode_when_openai_runtime_is_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        cfg=AppConfig(
            model="test-model",
            base_url="https://api.openai.com/v1",
            web_search_mode="auto",
        ),
        api_key="main-key",
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    assert "web_search" in tools


def test_build_tools_registers_web_search_in_auto_mode_when_dashscope_runtime_is_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        cfg=AppConfig(
            model="qwen3.5-plus",
            base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
            web_search_mode="auto",
        ),
        api_key="main-key",
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    assert "web_search" in tools


def test_build_tools_registers_web_search_in_auto_mode_when_tavily_runtime_is_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        cfg=AppConfig(
            model="test-model",
            base_url="https://example-proxy.invalid/v1",
            web_search_mode="auto",
        ),
        api_key="main-key",
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    assert "web_search" in tools


def test_build_tools_registers_external_web_search_for_deepseek_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        cfg=AppConfig(
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
            web_search_mode="auto",
            web_search_policy="auto",
        ),
        api_key="deepseek-key",
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )

    assert "web_search" in tools


def test_build_tools_registers_web_tools_in_top_level_readonly_when_search_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="readonly",
        yes=True,
        cfg=AppConfig(
            model="test-model",
            base_url="https://example-proxy.invalid/v1",
            web_search_mode="auto",
        ),
        api_key="main-key",
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=False,
        subagent_registry={},
        subagent_depth=0,
    )

    assert "web_fetch" in tools
    assert "web_search" in tools
    assert "shell_run" not in tools
    assert "fs_write" not in tools


def test_build_tools_keeps_web_tools_hidden_in_nested_readonly_subagents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="readonly",
        yes=True,
        cfg=AppConfig(
            model="test-model",
            base_url="https://example-proxy.invalid/v1",
            web_search_mode="auto",
        ),
        api_key="main-key",
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=False,
        subagent_registry={},
        subagent_depth=1,
    )

    assert "web_fetch" not in tools
    assert "web_search" not in tools
    assert "shell_run" not in tools
    assert "fs_write" not in tools


def test_build_tools_emits_web_search_runtime_unavailable_event_for_auto_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_KEYLESS", "0")
    store = _store(tmp_path, enabled=True)
    secret_base_url = (
        "https://search-user:search-password@example-proxy.invalid/private/search-token"
    )

    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=store,
        mode="auto",
        yes=True,
        cfg=AppConfig(
            model="test-model",
            base_url=secret_base_url,
            web_search_mode="auto",
        ),
        api_key="main-key",
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
        emit_web_search_runtime_diagnostics=True,
    )
    store.close()

    assert "web_search" not in tools
    events = list(read_session_events(store.path))
    diagnostic_events = [
        event for event in events if event.get("type") == "web_search_runtime_unavailable"
    ]
    assert len(diagnostic_events) == 1
    payload = diagnostic_events[0]["payload"]
    assert payload["mode"] == "auto"
    assert payload["provider"] is None
    assert payload["registration_ready"] is False
    assert payload["api_key_available"] is True
    assert payload["base_url_descriptor"] == endpoint_descriptor(secret_base_url)
    assert "base_url" not in payload
    serialized_payload = json.dumps(payload, sort_keys=True)
    assert "search-user" not in serialized_payload
    assert "search-password" not in serialized_payload
    assert "private/search-token" not in serialized_payload
    assert any(
        "OpenAI auto readiness requires explicit web_search_base_url" in note
        for note in payload["notes"]
    )
    assert any("missing TAVILY_API_KEY" in note for note in payload["notes"])


def test_build_tools_does_not_emit_web_search_runtime_unavailable_event_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_KEYLESS", "0")
    store = _store(tmp_path, enabled=True)

    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=store,
        mode="auto",
        yes=True,
        cfg=AppConfig(
            model="test-model",
            base_url="https://example-proxy.invalid/v1",
            web_search_mode="auto",
        ),
        api_key="main-key",
        non_interactive=True,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
    )
    store.close()

    assert "web_search" not in tools
    events = list(read_session_events(store.path))
    assert [
        event for event in events if event.get("type") == "web_search_runtime_unavailable"
    ] == []


def test_command_tools_offer_an_optional_step_description() -> None:
    from alysis_code.tools.registry import iter_builtin_tool_metadata

    specs = {spec.name: spec for spec in iter_builtin_tool_metadata()}
    for name in ("shell_run", "shell_background"):
        parameters = specs[name].parameters
        assert parameters["properties"]["description"]["type"] == "string"
        assert "description" not in parameters["required"]
