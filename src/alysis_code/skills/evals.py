from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Protocol, cast

from ..command_text import command_text
from .discovery import project_skill_root_relative_paths
from .eval_models import (
    SkillsEvalArtifacts,
    SkillsEvalCase,
    SkillsEvalExecutionRequest,
    SkillsEvalExecutionResult,
    SkillsEvalMode,
    SkillsEvalRecord,
    SkillsEvalSessionMode,
    SkillsEvalVerificationResult,
)

_VERIFICATION_PLATFORM_NAME = os.name
_WINDOWS_CREATE_NEW_PROCESS_GROUP = getattr(
    subprocess,
    "CREATE_NEW_PROCESS_GROUP",
    0x00000200,
)
_PROCESS_TREE_TERMINATION_TIMEOUT_SECONDS = 1.0

_CONVENTION_FILENAMES = {"AGENTS.md", "CLAUDE.md", "CONVENTIONS.md"}
_WORKSPACE_COPY_IGNORE_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".venv",
    ".alysis",
    "artifacts",
    "build",
    "dist",
    "node_modules",
    "reports",
    "worktrees",
}
DEFAULT_SKILLS_EVAL_MODES: tuple[SkillsEvalMode, ...] = (
    SkillsEvalMode(
        name="baseline",
        conventions_enabled=False,
        skills_enabled=False,
        skills_auto_invoke=False,
    ),
    SkillsEvalMode(
        name="conventions_only",
        conventions_enabled=True,
        skills_enabled=False,
        skills_auto_invoke=False,
    ),
    SkillsEvalMode(
        name="skills_manual_only",
        conventions_enabled=False,
        skills_enabled=True,
        skills_auto_invoke=False,
    ),
    SkillsEvalMode(
        name="skills_auto_only",
        conventions_enabled=False,
        skills_enabled=True,
        skills_auto_invoke=True,
    ),
    SkillsEvalMode(
        name="combined_manual",
        conventions_enabled=True,
        skills_enabled=True,
        skills_auto_invoke=False,
    ),
    SkillsEvalMode(
        name="combined_auto",
        conventions_enabled=True,
        skills_enabled=True,
        skills_auto_invoke=True,
    ),
)
_SKILL_LIFECYCLE_COMMAND_PATTERN = re.compile(
    r"\balysis\s+skill\s+"
    r"(init|create|validate|install|enable|disable|remove|uninstall)\b"
)
_COMPLETION_GATE_FAILURE_EVENTS = {
    "execution_evidence_finalization_blocked",
    "one_shot_completion_gate_failed",
    "interactive_completion_gate_failed",
}
_COMPLETION_GATE_INCOMPLETE_EVENTS = {
    "one_shot_completion_gate_incomplete_after_retries",
    "interactive_completion_gate_incomplete_after_retries",
}
_VERIFICATION_CREDIT_MISS_STAGES = {
    "verification_not_attempted",
    "verification_incomplete",
}
_FORCED_FINAL_SUMMARY_EVENT = "forced_final_summary_requested"
DEFAULT_SKILLS_LAUNCH_GATES: dict[str, float] = {
    "pass_rate_min": 0.80,
    "false_positive_rate_max": 0.0,
    "completion_gate_failure_rate_max": 0.05,
    "completion_gate_incomplete_after_retries_rate_max": 0.01,
    "forced_final_summary_rate_max": 0.01,
    "verification_credit_miss_rate_max": 0.05,
    "relevant_skill_usage_rate_min": 0.90,
    "explicit_skill_success_rate_min": 1.0,
    "automatic_selection_exact_match_rate_min": 1.0,
}
_SKILLS_EVAL_MODE_BY_NAME = {mode.name: mode for mode in DEFAULT_SKILLS_EVAL_MODES}
_MANUAL_LAUNCH_MODE_NAMES = ("skills_manual_only", "combined_manual")
_AUTO_LAUNCH_MODE_NAMES = ("skills_auto_only", "combined_auto")


class SkillsEvalExecutor(Protocol):
    def execute(self, request: SkillsEvalExecutionRequest) -> SkillsEvalExecutionResult: ...


class SkillsEvalVerificationRunner(Protocol):
    def __call__(
        self,
        *,
        workspace: Path,
        command: str,
        timeout_seconds: float | None,
    ) -> SkillsEvalVerificationResult: ...


def load_skills_eval_cases(manifest_path: Path) -> tuple[SkillsEvalCase, ...]:
    resolved_manifest = manifest_path.expanduser().resolve()
    raw = json.loads(resolved_manifest.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw_cases = raw.get("cases")
    else:
        raw_cases = raw
    if not isinstance(raw_cases, list):
        raise ValueError(
            "skills eval manifest must be a JSON array or an object with a 'cases' list"
        )

    cases: list[SkillsEvalCase] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(raw_cases):
        if not isinstance(item, dict):
            raise ValueError(f"skills eval case #{idx + 1} must be a JSON object")
        case_id = str(item.get("id") or "").strip()
        if not case_id:
            raise ValueError(f"skills eval case #{idx + 1} is missing a non-empty 'id'")
        lowered = case_id.casefold()
        if lowered in seen_ids:
            raise ValueError(f"duplicate skills eval case id: {case_id}")
        seen_ids.add(lowered)

        workspace_raw = str(item.get("workspace") or "").strip()
        if not workspace_raw:
            raise ValueError(f"skills eval case '{case_id}' is missing 'workspace'")
        workspace = (resolved_manifest.parent / workspace_raw).resolve()
        if not workspace.is_relative_to(resolved_manifest.parent):
            raise ValueError(
                f"skills eval case '{case_id}' workspace resolves outside the manifest directory"
            )

        task = str(item.get("task") or "").strip()
        if not task:
            raise ValueError(f"skills eval case '{case_id}' is missing 'task'")

        invocation_mode = str(item.get("invocation_mode") or "normal").strip()
        if invocation_mode not in {"normal", "explicit_skill"}:
            raise ValueError(
                f"skills eval case '{case_id}' has unsupported invocation_mode: {invocation_mode}"
            )
        explicit_skill_name = str(item.get("explicit_skill_name") or "").strip() or None
        if invocation_mode == "explicit_skill" and not explicit_skill_name:
            raise ValueError(
                f"skills eval case '{case_id}' requires 'explicit_skill_name' for explicit_skill mode"
            )
        if invocation_mode == "normal":
            explicit_skill_name = None

        session_mode = str(item.get("session_mode") or "").strip().casefold() or None
        if session_mode not in {None, "readonly"}:
            raise ValueError(
                f"skills eval case '{case_id}' has unsupported session_mode: {session_mode}"
            )

        cases.append(
            SkillsEvalCase(
                id=case_id,
                workspace=workspace,
                task=task,
                invocation_mode=invocation_mode,
                explicit_skill_name=explicit_skill_name,
                expected_skills=_normalized_string_tuple(item.get("expected_skills")),
                verification_command=_normalized_optional_string(item.get("verification_command")),
                session_mode=cast(SkillsEvalSessionMode | None, session_mode),
                tags=_normalized_string_tuple(item.get("tags")),
                notes=str(item.get("notes") or "").strip(),
            )
        )

    return tuple(cases)


def resolve_skills_eval_modes(
    selected_names: Sequence[str] | None = None,
) -> tuple[SkillsEvalMode, ...]:
    if not selected_names:
        return DEFAULT_SKILLS_EVAL_MODES
    requested = [str(name or "").strip() for name in selected_names if str(name or "").strip()]
    if not requested:
        return DEFAULT_SKILLS_EVAL_MODES
    available = {mode.name: mode for mode in DEFAULT_SKILLS_EVAL_MODES}
    resolved: list[SkillsEvalMode] = []
    seen: set[str] = set()
    for name in requested:
        if name not in available:
            valid = ", ".join(mode.name for mode in DEFAULT_SKILLS_EVAL_MODES)
            raise ValueError(f"unknown skills eval mode '{name}' (expected one of: {valid})")
        if name in seen:
            continue
        seen.add(name)
        resolved.append(available[name])
    return tuple(resolved)


@contextmanager
def prepared_skills_eval_workspace(
    *,
    source_workspace: Path,
    mode: SkillsEvalMode,
    temp_base_dir: Path | None = None,
) -> Iterator[Path]:
    source = source_workspace.expanduser().resolve()
    if not source.exists() or not source.is_dir():
        raise FileNotFoundError(f"skills eval workspace does not exist: {source}")
    temp_root_base = temp_base_dir.resolve() if temp_base_dir is not None else None
    if temp_root_base is not None:
        temp_root_base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=_skills_eval_tempdir_prefix(mode.name),
        dir=temp_root_base,
    ) as temp_dir:
        temp_root = Path(temp_dir)
        run_workspace = temp_root / "workspace"
        shutil.copytree(
            source,
            run_workspace,
            ignore=shutil.ignore_patterns(*sorted(_WORKSPACE_COPY_IGNORE_NAMES)),
        )
        if not mode.conventions_enabled:
            _mask_conventions_in_workspace(run_workspace)
        if not mode.skills_enabled:
            _mask_project_skill_roots_in_workspace(run_workspace)
        yield run_workspace


def extract_skills_eval_metrics(
    events: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    *,
    workspace_root: Path | None = None,
) -> dict[str, object]:
    event_list = list(events)
    matched_skill_names: list[str] = []
    skill_read_names: list[str] = []
    successful_skill_read_names: list[str] = []
    lifecycle_cli_commands: list[str] = []
    manual_skill_bundle_names: list[str] = []
    manual_skill_bundle_access_count = 0
    tool_call_count = 0
    successful_skill_read_count = 0
    skill_selection_status: str | None = None
    skill_selection_selected_names: list[str] = []
    skill_selection_call_count = 0
    skill_selection_failure_kinds: list[str] = []
    skill_selection_blocked_count = 0
    skill_selection_blocked_reasons: list[str] = []
    skill_selection_unhonored_count = 0
    completion_gate_failure_count = 0
    completion_gate_incomplete_after_retries_count = 0
    forced_final_summary_count = 0
    verification_credit_miss_count = 0

    blocked_tool_call_ids: set[str] = set()
    successful_skill_read_call_ids: set[str] = set()
    satisfied_skill_names_by_call_id: dict[str, str] = {}
    successful_result_names_by_call_id: dict[str, str] = {}

    for event in event_list:
        event_type = str(event.get("type") or "")
        payload_obj = event.get("payload")
        payload = payload_obj if isinstance(payload_obj, Mapping) else {}
        if event_type == "skill_selection":
            skill_selection_call_count += 1
            raw_status = str(payload.get("status") or "").strip()
            skill_selection_status = (
                raw_status if raw_status in {"selected", "no_match", "unavailable"} else None
            )
            selected_obj = payload.get("selected_names")
            skill_selection_selected_names = (
                [str(item or "").strip() for item in selected_obj if str(item or "").strip()]
                if isinstance(selected_obj, list)
                else []
            )
            failure_kind = str(payload.get("failure_kind") or "").strip()
            if failure_kind:
                skill_selection_failure_kinds.append(failure_kind)
        elif event_type == "skill_selection_mismatch_blocked":
            skill_selection_blocked_count += 1
            tool_call_id = str(payload.get("tool_call_id") or "").strip()
            if tool_call_id:
                blocked_tool_call_ids.add(tool_call_id)
            reason = str(payload.get("reason") or "").strip()
            if reason:
                skill_selection_blocked_reasons.append(reason)
        elif event_type == "skill_selection_read_satisfied":
            tool_call_id = str(payload.get("tool_call_id") or "").strip()
            name = str(payload.get("name") or "").strip()
            if tool_call_id:
                successful_skill_read_call_ids.add(tool_call_id)
                if name:
                    satisfied_skill_names_by_call_id[tool_call_id] = name
            elif name:
                successful_skill_read_names.append(name)
                successful_skill_read_count += 1
        elif event_type == "skill_selection_unhonored":
            skill_selection_unhonored_count += 1
        elif event_type == "tool_result":
            tool_name = str(payload.get("name") or "").strip()
            tool_call_id = str(payload.get("tool_call_id") or "").strip()
            result_obj = payload.get("result")
            result = result_obj if isinstance(result_obj, Mapping) else {}
            if (
                tool_name == "skill_read"
                and tool_call_id
                and isinstance(result_obj, Mapping)
                and "error" not in result
            ):
                successful_skill_read_call_ids.add(tool_call_id)
                name = str(result.get("name") or "").strip()
                if name:
                    successful_result_names_by_call_id[tool_call_id] = name

    processed_successful_call_ids: set[str] = set()
    for event in event_list:
        event_type = str(event.get("type") or "")
        payload_obj = event.get("payload")
        payload = payload_obj if isinstance(payload_obj, Mapping) else {}
        if event_type in _COMPLETION_GATE_FAILURE_EVENTS:
            completion_gate_failure_count += 1
        elif event_type in _COMPLETION_GATE_INCOMPLETE_EVENTS:
            completion_gate_incomplete_after_retries_count += 1
        elif event_type == _FORCED_FINAL_SUMMARY_EVENT:
            forced_final_summary_count += 1
        if _event_is_verification_credit_miss(event_type=event_type, payload=payload):
            verification_credit_miss_count += 1
        if event_type == "skill_matches":
            matches = payload.get("matches")
            if isinstance(matches, list):
                for item in matches:
                    if not isinstance(item, Mapping):
                        continue
                    name = str(item.get("name") or "").strip()
                    if name:
                        matched_skill_names.append(name)
        if event_type == "tool_call":
            tool_call_count += 1
            tool_name = str(payload.get("name") or "").strip()
            tool_call_id = str(payload.get("tool_call_id") or "").strip()
            arguments_obj = payload.get("arguments")
            arguments = arguments_obj if isinstance(arguments_obj, Mapping) else {}
            if tool_name == "skill_read":
                name = str(arguments.get("name") or "").strip()
                if name:
                    skill_read_names.append(name)
                if (
                    tool_call_id
                    and tool_call_id not in blocked_tool_call_ids
                    and tool_call_id in successful_skill_read_call_ids
                ):
                    successful_name = (
                        satisfied_skill_names_by_call_id.get(tool_call_id)
                        or successful_result_names_by_call_id.get(tool_call_id)
                        or name
                    )
                    if successful_name:
                        successful_skill_read_names.append(successful_name)
                    successful_skill_read_count += 1
                    processed_successful_call_ids.add(tool_call_id)
                continue
            if tool_call_id and tool_call_id in blocked_tool_call_ids:
                continue
            lifecycle_cli_commands.extend(
                _extract_skill_lifecycle_cli_commands_from_tool_call(
                    tool_name=tool_name,
                    arguments=arguments,
                )
            )
            manual_accessed, bundle_name = _manual_skill_bundle_access_from_tool_call(
                tool_name=tool_name,
                arguments=arguments,
                workspace_root=workspace_root,
            )
            if manual_accessed:
                manual_skill_bundle_access_count += 1
            if bundle_name:
                manual_skill_bundle_names.append(bundle_name)

    for tool_call_id in successful_skill_read_call_ids - processed_successful_call_ids:
        if tool_call_id in blocked_tool_call_ids:
            continue
        successful_name = satisfied_skill_names_by_call_id.get(
            tool_call_id
        ) or successful_result_names_by_call_id.get(tool_call_id)
        if successful_name:
            successful_skill_read_names.append(successful_name)
            successful_skill_read_count += 1

    matched_unique = _ordered_unique_strings(matched_skill_names)
    skill_read_unique = _ordered_unique_strings(skill_read_names)
    successful_skill_read_unique = _ordered_unique_strings(successful_skill_read_names)
    lifecycle_cli_unique = _ordered_unique_strings(lifecycle_cli_commands)
    manual_unique = _ordered_unique_strings(manual_skill_bundle_names)
    return {
        "matched_skill_context_attached": bool(matched_unique),
        "matched_skill_names": tuple(matched_unique),
        "skill_read_called": bool(skill_read_names),
        "skill_read_names": tuple(skill_read_unique),
        "skill_read_call_count": len(skill_read_names),
        "successful_skill_read_names": tuple(successful_skill_read_unique),
        "successful_skill_read_count": successful_skill_read_count,
        "skill_selection_status": skill_selection_status,
        "skill_selection_selected_names": tuple(
            _ordered_unique_strings(skill_selection_selected_names)
        ),
        "skill_selection_call_count": skill_selection_call_count,
        "skill_selection_failure_kinds": tuple(
            _ordered_unique_strings(skill_selection_failure_kinds)
        ),
        "skill_selection_blocked_count": skill_selection_blocked_count,
        "skill_selection_blocked_reasons": tuple(
            _ordered_unique_strings(skill_selection_blocked_reasons)
        ),
        "skill_selection_unhonored_count": skill_selection_unhonored_count,
        "skill_lifecycle_cli_used": bool(lifecycle_cli_commands),
        "skill_lifecycle_cli_commands": tuple(lifecycle_cli_unique),
        "skill_lifecycle_cli_call_count": len(lifecycle_cli_commands),
        "manual_skill_bundle_accessed": manual_skill_bundle_access_count > 0,
        "manual_skill_bundle_names": tuple(manual_unique),
        "manual_skill_bundle_access_count": manual_skill_bundle_access_count,
        "tool_call_count": tool_call_count,
        "completion_gate_failure_count": completion_gate_failure_count,
        "completion_gate_incomplete_after_retries_count": (
            completion_gate_incomplete_after_retries_count
        ),
        "forced_final_summary_count": forced_final_summary_count,
        "verification_credit_miss_count": verification_credit_miss_count,
    }


def _mode_metadata(mode_name: str) -> SkillsEvalMode | None:
    return _SKILLS_EVAL_MODE_BY_NAME.get(str(mode_name or "").strip())


def _mode_skills_enabled(mode_name: str) -> bool:
    mode = _mode_metadata(mode_name)
    return bool(mode.skills_enabled) if mode is not None else False


def aggregate_skills_eval_records(
    records: Sequence[SkillsEvalRecord],
) -> dict[str, object]:
    grouped: dict[str, list[SkillsEvalRecord]] = defaultdict(list)
    for record in records:
        grouped[record.mode].append(record)

    executed_records = [record for record in records if record.status != "skipped"]
    overall_expected = [record for record in executed_records if record.expected_skills]
    overall_negative_controls = [
        record for record in executed_records if not record.expected_skills
    ]
    overall_explicit_cases = [
        record for record in executed_records if record.invocation_mode == "explicit_skill"
    ]
    overall_relevant_skill_usage_count = sum(
        1 for record in overall_expected if record.relevant_skill_used()
    )
    overall_false_negative_count = sum(
        1 for record in overall_expected if not record.relevant_skill_used()
    )
    overall_false_positive_count = sum(
        1 for record in overall_negative_controls if record.any_skill_activity()
    )
    overall_skill_read_count = sum(1 for record in executed_records if record.skill_read_called)
    overall_lifecycle_cli_count = sum(
        1 for record in executed_records if record.skill_lifecycle_cli_used
    )
    overall_manual_skill_access_count = sum(
        1 for record in executed_records if record.manual_skill_bundle_accessed
    )
    overall_lifecycle_cli_call_count = sum(
        int(record.skill_lifecycle_cli_call_count or 0) for record in executed_records
    )
    overall_explicit_success_count = sum(
        1 for record in overall_explicit_cases if record.passed is True
    )
    overall_passed_count = sum(1 for record in executed_records if record.passed is True)
    skill_enabled_records = [
        record for record in executed_records if _mode_skills_enabled(record.mode)
    ]
    skill_enabled_expected = [record for record in skill_enabled_records if record.expected_skills]
    skill_enabled_relevant_skill_usage_count = sum(
        1 for record in skill_enabled_expected if record.relevant_skill_used()
    )
    skill_enabled_passed_count = sum(1 for record in skill_enabled_records if record.passed is True)

    per_mode: dict[str, dict[str, object]] = {}
    for mode_name, mode_records in grouped.items():
        mode_cfg = _mode_metadata(mode_name)
        executed = [record for record in mode_records if record.status != "skipped"]
        expected = [record for record in executed if record.expected_skills]
        negative_controls = [record for record in executed if not record.expected_skills]
        explicit_cases = [
            record for record in executed if record.invocation_mode == "explicit_skill"
        ]
        passed_count = sum(1 for record in executed if record.passed is True)
        matched_trigger_count = sum(1 for record in expected if record.relevant_skill_used())
        false_negative_count = sum(1 for record in expected if not record.relevant_skill_used())
        false_positive_count = sum(1 for record in negative_controls if record.any_skill_activity())
        skill_read_count = sum(1 for record in executed if record.skill_read_called)
        lifecycle_cli_count = sum(1 for record in executed if record.skill_lifecycle_cli_used)
        manual_skill_access_count = sum(
            1 for record in executed if record.manual_skill_bundle_accessed
        )
        lifecycle_cli_call_count = sum(
            int(record.skill_lifecycle_cli_call_count or 0) for record in executed
        )
        explicit_success_count = sum(1 for record in explicit_cases if record.passed is True)
        bundled_expected_records = [
            record
            for record in expected
            if any(tag.casefold() == "bundled-pack" for tag in record.tags)
        ]
        bundled_expected_skill_names = sorted(
            {skill for record in bundled_expected_records for skill in record.expected_skills},
            key=str.casefold,
        )
        bundled_normal_records = [
            record for record in bundled_expected_records if record.invocation_mode == "normal"
        ]
        bundled_normal_skill_activation: dict[str, dict[str, int]] = {}
        for skill_name in bundled_expected_skill_names:
            skill_key = skill_name.casefold()
            skill_records = [
                record
                for record in bundled_normal_records
                if any(expected.casefold() == skill_key for expected in record.expected_skills)
            ]
            bundled_normal_skill_activation[skill_name] = {
                "runs": len(skill_records),
                "successful_runs": sum(
                    1
                    for record in skill_records
                    if record.passed is True
                    and any(
                        observed.casefold() == skill_key
                        for observed in record.observed_skill_names()
                    )
                ),
            }
        launch_runtime = _launch_runtime_summary(executed)
        selection_summary = _skill_selection_summary(executed)
        per_mode[mode_name] = {
            "total_runs": len(mode_records),
            "executed_runs": len(executed),
            "skipped_runs": sum(1 for record in mode_records if record.status == "skipped"),
            "passed_runs": passed_count,
            "failed_runs": sum(1 for record in executed if record.passed is False),
            "skills_enabled": bool(mode_cfg.skills_enabled) if mode_cfg is not None else None,
            "conventions_enabled": (
                bool(mode_cfg.conventions_enabled) if mode_cfg is not None else None
            ),
            "skills_auto_invoke": (
                bool(mode_cfg.skills_auto_invoke) if mode_cfg is not None else None
            ),
            "pass_rate": _rate(passed_count, len(executed)),
            "expected_skill_runs": len(expected),
            "relevant_skill_usage_count": matched_trigger_count,
            "negative_control_runs": len(negative_controls),
            "false_positive_count": false_positive_count,
            "skill_trigger_rate": _rate(matched_trigger_count, len(expected)),
            "skill_read_rate": _rate(skill_read_count, len(executed)),
            "skill_lifecycle_cli_rate": _rate(lifecycle_cli_count, len(executed)),
            "skill_lifecycle_cli_call_count": lifecycle_cli_call_count,
            "manual_skill_access_rate": _rate(manual_skill_access_count, len(executed)),
            "false_negative_rate": _rate(false_negative_count, len(expected)),
            "false_positive_rate": _rate(false_positive_count, len(negative_controls)),
            "explicit_skill_runs": len(explicit_cases),
            "explicit_skill_success_count": explicit_success_count,
            "explicit_invocation_success_rate": _rate(
                explicit_success_count,
                len(explicit_cases),
            ),
            "bundled_expected_skills": bundled_expected_skill_names,
            "bundled_normal_skill_activation": bundled_normal_skill_activation,
            **selection_summary,
            **launch_runtime,
        }

    per_skill: dict[str, dict[str, object]] = {}
    expected_skill_records = [record for record in records if record.expected_skills]
    skill_names = sorted(
        {
            expected_skill
            for record in expected_skill_records
            for expected_skill in record.expected_skills
        }
    )
    for skill_name in skill_names:
        relevant_records = [
            record
            for record in expected_skill_records
            if any(
                expected.casefold() == skill_name.casefold() for expected in record.expected_skills
            )
            and record.status != "skipped"
        ]
        per_skill[skill_name] = {
            "runs": len(relevant_records),
            "triggered": sum(
                1
                for record in relevant_records
                if any(
                    name.casefold() == skill_name.casefold()
                    for name in record.observed_skill_names()
                )
            ),
            "passed": sum(1 for record in relevant_records if record.passed is True),
        }

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "result_count": len(records),
        "executed_runs": len(executed_records),
        "skipped_runs": sum(1 for record in records if record.status == "skipped"),
        "passed_runs": overall_passed_count,
        "failed_runs": sum(1 for record in executed_records if record.passed is False),
        "pass_rate": _rate(overall_passed_count, len(executed_records)),
        "pass_rate_all_modes": _rate(overall_passed_count, len(executed_records)),
        "skill_enabled_executed_runs": len(skill_enabled_records),
        "pass_rate_skill_enabled": _rate(skill_enabled_passed_count, len(skill_enabled_records)),
        "expected_skill_runs": len(overall_expected),
        "negative_control_runs": len(overall_negative_controls),
        "false_positive_count": overall_false_positive_count,
        "skill_eligible_runs": len(skill_enabled_expected),
        "relevant_skill_usage_count": overall_relevant_skill_usage_count,
        "relevant_skill_usage_count_all_modes": overall_relevant_skill_usage_count,
        "relevant_skill_usage_rate": _rate(
            overall_relevant_skill_usage_count,
            len(overall_expected),
        ),
        "relevant_skill_usage_rate_all_modes": _rate(
            overall_relevant_skill_usage_count,
            len(overall_expected),
        ),
        "relevant_skill_usage_count_skill_enabled": skill_enabled_relevant_skill_usage_count,
        "relevant_skill_usage_rate_skill_enabled": _rate(
            skill_enabled_relevant_skill_usage_count,
            len(skill_enabled_expected),
        ),
        "skill_read_rate": _rate(overall_skill_read_count, len(executed_records)),
        "skill_lifecycle_cli_rate": _rate(overall_lifecycle_cli_count, len(executed_records)),
        "skill_lifecycle_cli_call_count": overall_lifecycle_cli_call_count,
        "manual_skill_access_rate": _rate(
            overall_manual_skill_access_count,
            len(executed_records),
        ),
        "false_negative_rate": _rate(overall_false_negative_count, len(overall_expected)),
        "false_positive_rate": _rate(
            overall_false_positive_count,
            len(overall_negative_controls),
        ),
        "explicit_skill_runs": len(overall_explicit_cases),
        "explicit_skill_success_count": overall_explicit_success_count,
        "explicit_invocation_success_rate": _rate(
            overall_explicit_success_count,
            len(overall_explicit_cases),
        ),
        **_skill_selection_summary(executed_records),
        **_launch_runtime_summary(executed_records),
        "modes": per_mode,
        "per_skill": per_skill,
    }


def summarize_skills_launch_candidate_metrics(
    summary: Mapping[str, Any],
    *,
    config_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    modes_obj = summary.get("modes")
    modes = modes_obj if isinstance(modes_obj, Mapping) else {}
    default_skills_auto_invoke = None
    if isinstance(config_snapshot, Mapping):
        default_skills_auto_invoke = config_snapshot.get("skills_auto_invoke")

    preferred_launch_modes = (
        _AUTO_LAUNCH_MODE_NAMES if default_skills_auto_invoke is True else _MANUAL_LAUNCH_MODE_NAMES
    )
    fallback_launch_modes = (
        _MANUAL_LAUNCH_MODE_NAMES if default_skills_auto_invoke is True else _AUTO_LAUNCH_MODE_NAMES
    )
    launch_mode_names = tuple(name for name in preferred_launch_modes if name in modes)
    if not launch_mode_names:
        launch_mode_names = tuple(name for name in fallback_launch_modes if name in modes)
    if not launch_mode_names:
        launch_mode_names = tuple(
            sorted(
                name
                for name, payload_obj in modes.items()
                if isinstance(payload_obj, Mapping) and bool(payload_obj.get("skills_enabled"))
            )
        )

    skill_enabled_mode_names = tuple(
        sorted(
            name
            for name, payload_obj in modes.items()
            if isinstance(payload_obj, Mapping) and bool(payload_obj.get("skills_enabled"))
        )
    )

    def _sum_mode_counts(mode_names: Sequence[str], key: str) -> int:
        total = 0
        for mode_name in mode_names:
            payload_obj = modes.get(mode_name)
            payload = payload_obj if isinstance(payload_obj, Mapping) else {}
            total += int(payload.get(key) or 0)
        return total

    launch_executed_runs = _sum_mode_counts(launch_mode_names, "executed_runs")
    launch_passed_runs = _sum_mode_counts(launch_mode_names, "passed_runs")
    launch_failed_runs = _sum_mode_counts(launch_mode_names, "failed_runs")
    launch_skill_eligible_runs = _sum_mode_counts(launch_mode_names, "expected_skill_runs")
    launch_relevant_skill_usage_count = _sum_mode_counts(
        launch_mode_names, "relevant_skill_usage_count"
    )
    launch_negative_control_runs = _sum_mode_counts(launch_mode_names, "negative_control_runs")
    launch_false_positive_count = _sum_mode_counts(launch_mode_names, "false_positive_count")
    launch_explicit_skill_runs = _sum_mode_counts(launch_mode_names, "explicit_skill_runs")
    launch_explicit_skill_success_count = _sum_mode_counts(
        launch_mode_names, "explicit_skill_success_count"
    )
    launch_automatic_selection_runs = _sum_mode_counts(
        launch_mode_names, "automatic_selection_runs"
    )
    launch_automatic_selection_exact_match_count = _sum_mode_counts(
        launch_mode_names, "automatic_selection_exact_match_count"
    )
    launch_selector_available_run_count = _sum_mode_counts(
        launch_mode_names, "selector_available_run_count"
    )
    launch_selector_unavailable_run_count = _sum_mode_counts(
        launch_mode_names, "selector_unavailable_run_count"
    )
    launch_skill_selection_blocked_count = _sum_mode_counts(
        launch_mode_names, "skill_selection_blocked_count"
    )
    launch_skill_selection_blocked_run_count = _sum_mode_counts(
        launch_mode_names, "skill_selection_blocked_run_count"
    )
    launch_skill_selection_unhonored_count = _sum_mode_counts(
        launch_mode_names, "skill_selection_unhonored_count"
    )
    launch_skill_selection_unhonored_run_count = _sum_mode_counts(
        launch_mode_names, "skill_selection_unhonored_run_count"
    )
    launch_runtime_rates: dict[str, float | None] = {}
    for metric_name in (
        "completion_gate_failure",
        "completion_gate_incomplete_after_retries",
        "forced_final_summary",
        "verification_credit_miss",
    ):
        run_count = _sum_mode_counts(launch_mode_names, f"{metric_name}_run_count")
        launch_runtime_rates[f"{metric_name}_rate_launch_modes"] = _rate(
            run_count,
            launch_executed_runs,
        )

    bundled_expected_skill_names: set[str] = set()
    bundled_normal_skill_activation: dict[str, dict[str, int]] = {}
    for mode_name in launch_mode_names:
        payload_obj = modes.get(mode_name)
        payload = payload_obj if isinstance(payload_obj, Mapping) else {}
        expected_skills_obj = payload.get("bundled_expected_skills")
        if isinstance(expected_skills_obj, Sequence) and not isinstance(
            expected_skills_obj, (str, bytes)
        ):
            bundled_expected_skill_names.update(
                skill_name
                for raw_skill_name in expected_skills_obj
                if (skill_name := str(raw_skill_name or "").strip())
            )
        per_skill_obj = payload.get("bundled_normal_skill_activation")
        per_skill = per_skill_obj if isinstance(per_skill_obj, Mapping) else {}
        for raw_skill_name, stats_obj in per_skill.items():
            skill_name = str(raw_skill_name or "").strip()
            stats = stats_obj if isinstance(stats_obj, Mapping) else {}
            if not skill_name:
                continue
            bundled_expected_skill_names.add(skill_name)
            combined = bundled_normal_skill_activation.setdefault(
                skill_name,
                {"runs": 0, "successful_runs": 0},
            )
            combined["runs"] += int(stats.get("runs") or 0)
            combined["successful_runs"] += int(stats.get("successful_runs") or 0)
    missing_bundled_normal_skill_activations = sorted(
        (
            skill_name
            for skill_name in bundled_expected_skill_names
            if int(bundled_normal_skill_activation.get(skill_name, {}).get("successful_runs") or 0)
            <= 0
        ),
        key=str.casefold,
    )

    return {
        "launch_mode_basis": (
            "skills_auto_invoke=true"
            if default_skills_auto_invoke is True
            else "skills_auto_invoke=false"
        ),
        "launch_mode_names": list(launch_mode_names),
        "skill_enabled_mode_names": list(skill_enabled_mode_names),
        "launch_mode_executed_runs": launch_executed_runs,
        "launch_mode_passed_runs": launch_passed_runs,
        "launch_mode_failed_runs": launch_failed_runs,
        "pass_rate_launch_modes": _rate(launch_passed_runs, launch_executed_runs),
        "launch_skill_eligible_runs": launch_skill_eligible_runs,
        "relevant_skill_usage_count_launch_modes": launch_relevant_skill_usage_count,
        "relevant_skill_usage_rate_launch_modes": _rate(
            launch_relevant_skill_usage_count,
            launch_skill_eligible_runs,
        ),
        "negative_control_runs_launch_modes": launch_negative_control_runs,
        "false_positive_count_launch_modes": launch_false_positive_count,
        "false_positive_rate_launch_modes": _rate(
            launch_false_positive_count,
            launch_negative_control_runs,
        ),
        "explicit_skill_runs_launch_modes": launch_explicit_skill_runs,
        "explicit_skill_success_count_launch_modes": launch_explicit_skill_success_count,
        "explicit_invocation_success_rate_launch_modes": _rate(
            launch_explicit_skill_success_count,
            launch_explicit_skill_runs,
        ),
        "automatic_selection_runs_launch_modes": launch_automatic_selection_runs,
        "automatic_selection_exact_match_count_launch_modes": (
            launch_automatic_selection_exact_match_count
        ),
        "automatic_selection_exact_match_rate_launch_modes": _rate(
            launch_automatic_selection_exact_match_count,
            launch_automatic_selection_runs,
        ),
        "selector_available_run_count_launch_modes": launch_selector_available_run_count,
        "selector_unavailable_run_count_launch_modes": launch_selector_unavailable_run_count,
        "selector_unavailable_rate_launch_modes": _rate(
            launch_selector_unavailable_run_count,
            launch_automatic_selection_runs,
        ),
        "skill_selection_blocked_count_launch_modes": launch_skill_selection_blocked_count,
        "skill_selection_blocked_run_count_launch_modes": (
            launch_skill_selection_blocked_run_count
        ),
        "skill_selection_blocked_rate_launch_modes": _rate(
            launch_skill_selection_blocked_run_count,
            launch_automatic_selection_runs,
        ),
        "skill_selection_unhonored_count_launch_modes": (launch_skill_selection_unhonored_count),
        "skill_selection_unhonored_run_count_launch_modes": (
            launch_skill_selection_unhonored_run_count
        ),
        "skill_selection_unhonored_rate_launch_modes": _rate(
            launch_skill_selection_unhonored_run_count,
            launch_automatic_selection_runs,
        ),
        "bundled_normal_skill_activation_launch_modes": bundled_normal_skill_activation,
        "missing_bundled_normal_skill_activations": (missing_bundled_normal_skill_activations),
        **launch_runtime_rates,
    }


def evaluate_skills_launch_readiness(
    *,
    summary: Mapping[str, Any],
    config_snapshot: Mapping[str, Any] | None = None,
    thresholds: Mapping[str, float] | None = None,
) -> dict[str, object]:
    effective_thresholds = dict(DEFAULT_SKILLS_LAUNCH_GATES)
    if thresholds is not None:
        for key, value in thresholds.items():
            try:
                effective_thresholds[str(key)] = float(value)
            except (TypeError, ValueError):
                continue

    launch_metrics = summarize_skills_launch_candidate_metrics(
        summary,
        config_snapshot=config_snapshot,
    )
    launch_mode_names = tuple(launch_metrics.get("launch_mode_names") or ())
    launch_label = ", ".join(launch_mode_names) if launch_mode_names else "launch-candidate modes"
    pass_rate_actual = launch_metrics.get("pass_rate_launch_modes")
    if pass_rate_actual is None:
        pass_rate_actual = summary.get("pass_rate")
    relevant_skill_usage_actual = launch_metrics.get("relevant_skill_usage_rate_launch_modes")
    if relevant_skill_usage_actual is None:
        relevant_skill_usage_actual = summary.get("relevant_skill_usage_rate_skill_enabled")
    if relevant_skill_usage_actual is None:
        relevant_skill_usage_actual = summary.get("relevant_skill_usage_rate")
    false_positive_actual = launch_metrics.get("false_positive_rate_launch_modes")
    if false_positive_actual is None:
        false_positive_actual = summary.get("false_positive_rate")
    explicit_skill_success_actual = launch_metrics.get(
        "explicit_invocation_success_rate_launch_modes"
    )
    if explicit_skill_success_actual is None:
        explicit_skill_success_actual = summary.get("explicit_invocation_success_rate")
    failed_selected_records_actual = summary.get("failed_selected_record_count")
    if failed_selected_records_actual is None:
        failed_selected_records_actual = launch_metrics.get("launch_mode_failed_runs")
    missing_bundled_normal_skills = list(
        launch_metrics.get("missing_bundled_normal_skill_activations") or ()
    )
    default_skills_auto_invoke = None
    if isinstance(config_snapshot, Mapping):
        default_skills_auto_invoke = config_snapshot.get("skills_auto_invoke")
    automatic_selection_gates_enabled = default_skills_auto_invoke is True
    automatic_selection_runs = int(launch_metrics.get("automatic_selection_runs_launch_modes") or 0)
    automatic_selection_exact_actual = launch_metrics.get(
        "automatic_selection_exact_match_rate_launch_modes"
    )
    selector_unavailable_actual = int(
        launch_metrics.get("selector_unavailable_run_count_launch_modes") or 0
    )

    def _launch_runtime_rate(metric_name: str) -> object:
        launch_value = launch_metrics.get(f"{metric_name}_launch_modes")
        return summary.get(metric_name) if launch_value is None else launch_value

    gates = {
        "failed_selected_records": _max_count_gate(
            actual=failed_selected_records_actual,
            threshold=0,
            description="Every selected deterministic launch record must pass.",
        ),
        "pass_rate": _min_rate_gate(
            actual=pass_rate_actual,
            threshold=effective_thresholds["pass_rate_min"],
            description=(
                f"Pass rate across the current launch-candidate modes ({launch_label}) must stay strong on the launch suite."
            ),
        ),
        "false_positive_rate": _max_rate_gate(
            actual=false_positive_actual,
            threshold=effective_thresholds["false_positive_rate_max"],
            description="Launch-candidate negative controls must not activate skills.",
        ),
        "completion_gate_failure_rate": _max_rate_gate(
            actual=_launch_runtime_rate("completion_gate_failure_rate"),
            threshold=effective_thresholds["completion_gate_failure_rate_max"],
            description="Completion-gate failure rate must stay below the public-launch bar.",
        ),
        "completion_gate_incomplete_after_retries_rate": _max_rate_gate(
            actual=_launch_runtime_rate("completion_gate_incomplete_after_retries_rate"),
            threshold=effective_thresholds["completion_gate_incomplete_after_retries_rate_max"],
            description=(
                "Completion-gate incomplete-after-retries should be essentially absent in a "
                "production candidate."
            ),
        ),
        "forced_final_summary_rate": _max_rate_gate(
            actual=_launch_runtime_rate("forced_final_summary_rate"),
            threshold=effective_thresholds["forced_final_summary_rate_max"],
            description="Forced-final-summary fallback must stay rare on the launch suite.",
        ),
        "verification_credit_miss_rate": _max_rate_gate(
            actual=_launch_runtime_rate("verification_credit_miss_rate"),
            threshold=effective_thresholds["verification_credit_miss_rate_max"],
            description="Equivalent real verification commands must be credited consistently.",
        ),
        "relevant_skill_usage_rate": _min_rate_gate(
            actual=relevant_skill_usage_actual,
            threshold=effective_thresholds["relevant_skill_usage_rate_min"],
            description=(
                "Skill-expected launch-candidate runs should show relevant observed skill usage "
                "without counting control modes where skills are disabled."
            ),
        ),
        "explicit_skill_success_rate": _min_rate_gate(
            actual=explicit_skill_success_actual,
            threshold=effective_thresholds["explicit_skill_success_rate_min"],
            description="Explicit /skill flows must complete successfully on the launch suite.",
        ),
        "automatic_selection_exact_match_rate": _min_rate_gate(
            actual=(
                automatic_selection_exact_actual
                if automatic_selection_gates_enabled and automatic_selection_runs > 0
                else (0.0 if automatic_selection_gates_enabled else 1.0)
            ),
            threshold=effective_thresholds["automatic_selection_exact_match_rate_min"],
            description=(
                "Normal automatic cases must select exactly the expected set, while negative "
                "controls must return NONE."
            ),
        ),
        "selector_unavailable_runs": _max_count_gate(
            actual=(selector_unavailable_actual if automatic_selection_gates_enabled else 0),
            threshold=0,
            description="The automatic selector must be available for every eligible launch run.",
        ),
        "bundled_normal_skill_activation": {
            **_max_count_gate(
                actual=len(missing_bundled_normal_skills),
                threshold=0,
                description=(
                    "Every expected bundled skill must have a passing normal case with relevant "
                    "skill activation."
                ),
            ),
            "missing_skills": missing_bundled_normal_skills,
        },
    }

    # On-demand runs have no preliminary selector. Do not invent a missing
    # selector failure or a perfect selection score; retain historical gates
    # only for reports that actually recorded selector events.
    if automatic_selection_gates_enabled and automatic_selection_runs == 0:
        for name in ("automatic_selection_exact_match_rate", "selector_unavailable_runs"):
            gates[name] = {
                **gates[name],
                "actual": None,
                "status": "not_applicable",
                "description": "No preliminary selector events were recorded; activation is evaluated from observed skill usage.",
            }

    failing_gates = [
        gate_name
        for gate_name, payload in gates.items()
        if str(payload.get("status") or "") not in {"pass", "not_applicable"}
    ]
    passing_gates = sorted(
        gate_name
        for gate_name, payload in gates.items()
        if str(payload.get("status") or "") == "pass"
    )
    failing_gates = sorted(failing_gates)
    production_ready = not failing_gates
    return {
        "evaluated_at": datetime.now(UTC).isoformat(),
        "production_ready": production_ready,
        "launch_metrics": launch_metrics,
        "passing_gates": passing_gates,
        "failing_gates": failing_gates,
        "gates": gates,
        "thresholds": effective_thresholds,
        "default_skills_auto_invoke": default_skills_auto_invoke,
    }


def render_skills_eval_summary_markdown(
    *,
    summary: Mapping[str, Any],
    manifest_path: Path | None = None,
) -> str:
    lines = ["# Skills Eval Summary", ""]
    if manifest_path is not None:
        lines.append(f"- Manifest: `{manifest_path.as_posix()}`")
    generated_at = str(summary.get("generated_at") or "").strip()
    if generated_at:
        lines.append(f"- Generated at: `{generated_at}`")
    lines.extend(
        [
            "",
            "| mode | executed | skipped | pass rate | trigger rate | skill_read rate | lifecycle CLI rate | manual access rate | false-negative | false-positive | explicit success |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    modes_obj = summary.get("modes")
    modes = modes_obj if isinstance(modes_obj, Mapping) else {}
    for mode_name in sorted(modes):
        item_obj = modes.get(mode_name)
        item = item_obj if isinstance(item_obj, Mapping) else {}
        lines.append(
            "| {mode} | {executed} | {skipped} | {pass_rate} | {trigger} | {skill_read} | {lifecycle_cli} | {manual} | {fn} | {fp} | {explicit} |".format(
                mode=mode_name,
                executed=item.get("executed_runs", 0),
                skipped=item.get("skipped_runs", 0),
                pass_rate=_format_rate(item.get("pass_rate")),
                trigger=_format_rate(item.get("skill_trigger_rate")),
                skill_read=_format_rate(item.get("skill_read_rate")),
                lifecycle_cli=_format_rate(item.get("skill_lifecycle_cli_rate")),
                manual=_format_rate(item.get("manual_skill_access_rate")),
                fn=_format_rate(item.get("false_negative_rate")),
                fp=_format_rate(item.get("false_positive_rate")),
                explicit=_format_rate(item.get("explicit_invocation_success_rate")),
            )
        )
    lines.extend(
        [
            "",
            "## Launch Runtime Metrics",
            "",
            f"- completion-gate failure rate: {_format_rate(summary.get('completion_gate_failure_rate'))} ({summary.get('completion_gate_failure_run_count', 0)} run(s), {summary.get('completion_gate_failure_count', 0)} event(s))",
            f"- completion-gate incomplete-after-retries rate: {_format_rate(summary.get('completion_gate_incomplete_after_retries_rate'))} ({summary.get('completion_gate_incomplete_after_retries_run_count', 0)} run(s), {summary.get('completion_gate_incomplete_after_retries_count', 0)} event(s))",
            f"- forced-final-summary rate: {_format_rate(summary.get('forced_final_summary_rate'))} ({summary.get('forced_final_summary_run_count', 0)} run(s), {summary.get('forced_final_summary_count', 0)} event(s))",
            f"- verification-credit miss rate: {_format_rate(summary.get('verification_credit_miss_rate'))} ({summary.get('verification_credit_miss_run_count', 0)} run(s), {summary.get('verification_credit_miss_count', 0)} event(s))",
            f"- overall pass rate (all completed modes): {_format_rate(summary.get('pass_rate_all_modes', summary.get('pass_rate')))}",
            f"- launch-candidate pass rate: {_format_rate(summary.get('pass_rate_launch_modes', summary.get('pass_rate')))}",
            f"- relevant skill usage rate (all modes): {_format_rate(summary.get('relevant_skill_usage_rate_all_modes', summary.get('relevant_skill_usage_rate')))}",
            f"- relevant skill usage rate (skill-enabled modes): {_format_rate(summary.get('relevant_skill_usage_rate_skill_enabled', summary.get('relevant_skill_usage_rate')))}",
            f"- relevant skill usage rate (launch-candidate modes): {_format_rate(summary.get('relevant_skill_usage_rate_launch_modes', summary.get('relevant_skill_usage_rate')))}",
            f"- explicit /skill success rate: {_format_rate(summary.get('explicit_invocation_success_rate'))}",
        ]
    )
    launch_mode_names = summary.get("launch_mode_names")
    if isinstance(launch_mode_names, Sequence) and not isinstance(launch_mode_names, str):
        names = [str(item).strip() for item in launch_mode_names if str(item).strip()]
        if names:
            lines.append(f"- launch-candidate modes: {', '.join(names)}")
    release_gates_obj = summary.get("release_gates")
    release_gates = release_gates_obj if isinstance(release_gates_obj, Mapping) else {}
    gates_obj = release_gates.get("gates")
    gates = gates_obj if isinstance(gates_obj, Mapping) else {}
    if gates:
        lines.extend(["", "## Release Gates", ""])
        for gate_name in sorted(gates):
            gate_obj = gates.get(gate_name)
            gate = gate_obj if isinstance(gate_obj, Mapping) else {}
            actual = gate.get("actual")
            actual_text = _format_rate(actual) if isinstance(actual, float) else str(actual)
            threshold_text = _gate_threshold_text(gate)
            lines.append(
                f"- `{gate_name}`: {str(gate.get('status') or 'unknown')} (actual={actual_text}, required={threshold_text})"
            )
    per_skill_obj = summary.get("per_skill")
    per_skill = per_skill_obj if isinstance(per_skill_obj, Mapping) else {}
    if per_skill:
        lines.extend(["", "## Per-skill", ""])
        for skill_name in sorted(per_skill):
            item_obj = per_skill.get(skill_name)
            item = item_obj if isinstance(item_obj, Mapping) else {}
            lines.append(
                f"- `{skill_name}`: runs={item.get('runs', 0)}, triggered={item.get('triggered', 0)}, passed={item.get('passed', 0)}"
            )
    lines.append("")
    return "\n".join(lines)


def write_skills_eval_artifacts(
    *,
    output_dir: Path,
    records: Sequence[SkillsEvalRecord],
    summary: Mapping[str, Any],
    manifest_path: Path | None = None,
    cases: Sequence[SkillsEvalCase] | None = None,
) -> SkillsEvalArtifacts:
    output_dir.mkdir(parents=True, exist_ok=True)
    results_path = output_dir / "results.jsonl"
    summary_json_path = output_dir / "summary.json"
    summary_md_path = output_dir / "summary.md"
    normalized_cases_path = output_dir / "cases.json"

    with results_path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record.to_payload(output_dir=output_dir), ensure_ascii=True) + "\n")

    summary_payload = dict(summary)
    if manifest_path is not None:
        summary_payload["manifest_path"] = manifest_path.resolve().as_posix()
    summary_json_path.write_text(
        json.dumps(summary_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary_md_path.write_text(
        render_skills_eval_summary_markdown(summary=summary_payload, manifest_path=manifest_path),
        encoding="utf-8",
    )
    if cases is not None:
        normalized_cases_path.write_text(
            json.dumps(
                [_case_to_payload(case) for case in cases],
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    return SkillsEvalArtifacts(
        output_dir=output_dir,
        results_path=results_path,
        summary_json_path=summary_json_path,
        summary_md_path=summary_md_path,
        records=tuple(records),
        summary=dict(summary_payload),
    )


def run_skills_eval_suite(
    *,
    cases: Sequence[SkillsEvalCase],
    modes: Sequence[SkillsEvalMode],
    output_dir: Path,
    executor: SkillsEvalExecutor,
    verification_runner: SkillsEvalVerificationRunner | None = None,
    manifest_path: Path | None = None,
    max_steps: int = 25,
    temp_base_dir: Path | None = None,
    deadline_seconds: float | None = None,
) -> SkillsEvalArtifacts:
    results: list[SkillsEvalRecord] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    sessions_dir = output_dir / "sessions"
    verification_runner = verification_runner or run_shell_verification_command

    for case_idx, case in enumerate(cases, start=1):
        for mode in modes:
            automatic_selection_required = bool(
                mode.skills_enabled and mode.skills_auto_invoke and case.invocation_mode == "normal"
            )
            if case.invocation_mode == "explicit_skill" and not mode.skills_enabled:
                results.append(
                    SkillsEvalRecord(
                        case_id=case.id,
                        mode=mode.name,
                        workspace=case.workspace,
                        task=case.task,
                        invocation_mode=case.invocation_mode,
                        explicit_skill_name=case.explicit_skill_name,
                        expected_skills=case.expected_skills,
                        tags=case.tags,
                        notes=case.notes,
                        status="skipped",
                        passed=None,
                        skip_reason="explicit skill invocation requires a skills-enabled mode",
                        agent_exit_code=None,
                        verification_command=case.verification_command,
                        verification_exit_code=None,
                        verification_output_preview=None,
                        skills_advertised_present=False,
                        repo_conventions_present=False,
                        matched_skill_context_attached=False,
                        matched_skill_names=(),
                        explicit_skill_context_used=False,
                        skill_read_called=False,
                        skill_read_names=(),
                        skill_read_call_count=0,
                        skill_lifecycle_cli_used=False,
                        skill_lifecycle_cli_commands=(),
                        skill_lifecycle_cli_call_count=0,
                        manual_skill_bundle_accessed=False,
                        manual_skill_bundle_names=(),
                        manual_skill_bundle_access_count=0,
                        tool_call_count=0,
                        automatic_selection_required=automatic_selection_required,
                        session_log_path=None,
                        session_artifact_root=None,
                    )
                )
                continue

            try:
                with prepared_skills_eval_workspace(
                    source_workspace=case.workspace,
                    mode=mode,
                    temp_base_dir=temp_base_dir,
                ) as run_workspace:
                    request = SkillsEvalExecutionRequest(
                        case=case,
                        mode=mode,
                        workspace=run_workspace,
                        output_dir=output_dir,
                        sessions_dir=sessions_dir,
                        session_id=_build_session_id(
                            case_id=case.id, mode_name=mode.name, ordinal=case_idx
                        ),
                        max_steps=max_steps,
                    )
                    run_started_at = monotonic() if deadline_seconds is not None else None
                    execution = executor.execute(request)
                    verification = None
                    if case.verification_command:
                        verification_timeout_seconds = _remaining_eval_run_seconds(
                            deadline_seconds=deadline_seconds,
                            run_started_at=run_started_at,
                        )
                        if (
                            verification_timeout_seconds is not None
                            and verification_timeout_seconds <= 0
                        ):
                            verification = SkillsEvalVerificationResult(
                                exit_code=124,
                                output_preview=(
                                    "Verification not started: the per-run deadline was exhausted "
                                    "before the manifest verification command."
                                ),
                            )
                        else:
                            verification = verification_runner(
                                workspace=run_workspace,
                                command=case.verification_command,
                                timeout_seconds=verification_timeout_seconds,
                            )
            except Exception as exc:  # noqa: BLE001
                results.append(
                    SkillsEvalRecord(
                        case_id=case.id,
                        mode=mode.name,
                        workspace=case.workspace,
                        task=case.task,
                        invocation_mode=case.invocation_mode,
                        explicit_skill_name=case.explicit_skill_name,
                        expected_skills=case.expected_skills,
                        tags=case.tags,
                        notes=case.notes,
                        status="failed",
                        passed=False,
                        skip_reason=None,
                        agent_exit_code=None,
                        verification_command=case.verification_command,
                        verification_exit_code=None,
                        verification_output_preview=None,
                        skills_advertised_present=False,
                        repo_conventions_present=False,
                        matched_skill_context_attached=False,
                        matched_skill_names=(),
                        explicit_skill_context_used=False,
                        skill_read_called=False,
                        skill_read_names=(),
                        skill_read_call_count=0,
                        skill_lifecycle_cli_used=False,
                        skill_lifecycle_cli_commands=(),
                        skill_lifecycle_cli_call_count=0,
                        manual_skill_bundle_accessed=False,
                        manual_skill_bundle_names=(),
                        manual_skill_bundle_access_count=0,
                        tool_call_count=0,
                        automatic_selection_required=automatic_selection_required,
                        session_log_path=None,
                        session_artifact_root=None,
                        error=str(exc),
                    )
                )
                continue

            verification_exit_code = verification.exit_code if verification is not None else None
            status = (
                "passed"
                if execution.agent_exit_code == 0
                and execution.error is None
                and (verification_exit_code is None or verification_exit_code == 0)
                else "failed"
            )
            results.append(
                SkillsEvalRecord(
                    case_id=case.id,
                    mode=mode.name,
                    workspace=case.workspace,
                    task=case.task,
                    invocation_mode=case.invocation_mode,
                    explicit_skill_name=case.explicit_skill_name,
                    expected_skills=case.expected_skills,
                    tags=case.tags,
                    notes=case.notes,
                    status=status,
                    passed=status == "passed",
                    skip_reason=None,
                    agent_exit_code=execution.agent_exit_code,
                    verification_command=case.verification_command,
                    verification_exit_code=verification_exit_code,
                    verification_output_preview=(
                        verification.output_preview if verification is not None else None
                    ),
                    skills_advertised_present=execution.skills_advertised_present,
                    repo_conventions_present=execution.repo_conventions_present,
                    matched_skill_context_attached=execution.matched_skill_context_attached,
                    matched_skill_names=execution.matched_skill_names,
                    explicit_skill_context_used=execution.explicit_skill_context_used,
                    skill_read_called=execution.skill_read_called,
                    skill_read_names=execution.skill_read_names,
                    skill_read_call_count=execution.skill_read_call_count,
                    skill_lifecycle_cli_used=execution.skill_lifecycle_cli_used,
                    skill_lifecycle_cli_commands=execution.skill_lifecycle_cli_commands,
                    skill_lifecycle_cli_call_count=execution.skill_lifecycle_cli_call_count,
                    manual_skill_bundle_accessed=execution.manual_skill_bundle_accessed,
                    manual_skill_bundle_names=execution.manual_skill_bundle_names,
                    manual_skill_bundle_access_count=execution.manual_skill_bundle_access_count,
                    tool_call_count=execution.tool_call_count,
                    completion_gate_failure_count=execution.completion_gate_failure_count,
                    completion_gate_incomplete_after_retries_count=(
                        execution.completion_gate_incomplete_after_retries_count
                    ),
                    forced_final_summary_count=execution.forced_final_summary_count,
                    verification_credit_miss_count=execution.verification_credit_miss_count,
                    automatic_selection_required=automatic_selection_required,
                    successful_skill_read_names=execution.successful_skill_read_names,
                    successful_skill_read_count=execution.successful_skill_read_count,
                    skill_selection_status=execution.skill_selection_status,
                    skill_selection_selected_names=execution.skill_selection_selected_names,
                    skill_selection_call_count=execution.skill_selection_call_count,
                    skill_selection_failure_kinds=execution.skill_selection_failure_kinds,
                    skill_selection_blocked_count=execution.skill_selection_blocked_count,
                    skill_selection_blocked_reasons=execution.skill_selection_blocked_reasons,
                    skill_selection_unhonored_count=execution.skill_selection_unhonored_count,
                    session_log_path=execution.session_log_path,
                    session_artifact_root=execution.session_artifact_root,
                    error=execution.error,
                )
            )

    summary = aggregate_skills_eval_records(results)
    return write_skills_eval_artifacts(
        output_dir=output_dir,
        records=results,
        summary=summary,
        manifest_path=manifest_path,
        cases=cases,
    )


def run_shell_verification_command(
    *,
    workspace: Path,
    command: str,
    timeout_seconds: float | None = None,
) -> SkillsEvalVerificationResult:
    if timeout_seconds is not None and timeout_seconds <= 0:
        return SkillsEvalVerificationResult(
            exit_code=124,
            output_preview="Verification command timed out before execution.",
        )
    env = os.environ.copy()
    env.pop("PYTHONOPTIMIZE", None)
    interpreter_dir = str(Path(sys.executable).parent)
    current_path = str(env.get("PATH") or "")
    env["PATH"] = os.pathsep.join(part for part in (interpreter_dir, current_path) if part)
    process_group_kwargs: dict[str, Any] = {}
    if _VERIFICATION_PLATFORM_NAME == "posix":
        process_group_kwargs["start_new_session"] = True
    elif _VERIFICATION_PLATFORM_NAME == "nt":
        process_group_kwargs["creationflags"] = _WINDOWS_CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=workspace,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **command_text(),
        **process_group_kwargs,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _terminate_verification_process_tree(proc)
        stdout, stderr = _reap_verification_process(
            proc,
            fallback_stdout=exc.stdout,
            fallback_stderr=exc.stderr,
        )
        timeout_label = f"{float(timeout_seconds):g}s"
        partial_output = (
            _verification_process_output(stdout) + _verification_process_output(stderr)
        ).strip()
        output = f"Verification command timed out after {timeout_label}."
        if partial_output:
            output += f"\n{partial_output}"
        return SkillsEvalVerificationResult(exit_code=124, output_preview=output[:500])
    except BaseException:
        _terminate_verification_process_tree(proc)
        _reap_verification_process(proc)
        raise
    output = (_verification_process_output(stdout) + _verification_process_output(stderr)).strip()
    preview = output[:500]
    return SkillsEvalVerificationResult(exit_code=proc.returncode, output_preview=preview)


def _terminate_verification_process_tree(proc: subprocess.Popen[str]) -> None:
    if _VERIFICATION_PLATFORM_NAME == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return
    if _VERIFICATION_PLATFORM_NAME == "nt":
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=_PROCESS_TREE_TERMINATION_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            _kill_verification_process(proc)
        else:
            if completed.returncode != 0:
                _kill_verification_process(proc)
        return
    _kill_verification_process(proc)


def _kill_verification_process(proc: subprocess.Popen[str]) -> None:
    try:
        proc.kill()
    except OSError:
        pass


def _reap_verification_process(
    proc: subprocess.Popen[str],
    *,
    fallback_stdout: str | bytes | None = None,
    fallback_stderr: str | bytes | None = None,
) -> tuple[str | bytes | None, str | bytes | None]:
    try:
        return proc.communicate(timeout=_PROCESS_TREE_TERMINATION_TIMEOUT_SECONDS)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        _kill_verification_process(proc)
        try:
            proc.wait(timeout=_PROCESS_TREE_TERMINATION_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError):
            pass
        return fallback_stdout, fallback_stderr


def _remaining_eval_run_seconds(
    *,
    deadline_seconds: float | None,
    run_started_at: float | None,
) -> float | None:
    if deadline_seconds is None or run_started_at is None:
        return None
    elapsed = max(0.0, monotonic() - run_started_at)
    return max(0.0, float(deadline_seconds) - elapsed)


def _verification_process_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def default_skills_eval_output_dir(*, root: Path | None = None) -> Path:
    base_root = (root or Path.cwd()).resolve()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return base_root / ".alysis" / "evals" / "skills" / timestamp


def _build_session_id(*, case_id: str, mode_name: str, ordinal: int) -> str:
    safe_case = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in case_id)
    safe_mode = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in mode_name)
    return f"skills_eval_{ordinal:03d}_{safe_case}_{safe_mode}"


def _mask_conventions_in_workspace(workspace: Path) -> None:
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        if path.name not in _CONVENTION_FILENAMES:
            continue
        path.unlink()


def _mask_project_skill_roots_in_workspace(workspace: Path) -> None:
    for root_parts in project_skill_root_relative_paths():
        target = workspace.joinpath(*root_parts)
        if not target.exists():
            continue
        if target.is_symlink() or target.is_file():
            target.unlink()
            continue
        shutil.rmtree(target)


def _manual_skill_bundle_access_from_tool_call(
    *,
    tool_name: str,
    arguments: Mapping[str, Any],
    workspace_root: Path | None,
) -> tuple[bool, str | None]:
    path_argument_name = _manual_skill_tool_path_argument_name(tool_name)
    if path_argument_name is None:
        return False, None
    path_value = arguments.get(path_argument_name)
    if not isinstance(path_value, str):
        return False, None
    if tool_name in {"fs_list", "search_rg"} and path_argument_name == "root_path":
        if str(path_value).strip() in {"", "."}:
            return False, None
    return _classify_manual_skill_bundle_path(path_value, workspace_root=workspace_root)


def _manual_skill_tool_path_argument_name(tool_name: str) -> str | None:
    if tool_name in {"fs_read", "fs_read_lines"}:
        return "path"
    if tool_name in {"fs_list", "search_rg"}:
        return "root_path"
    return None


def _extract_skill_lifecycle_cli_commands_from_tool_call(
    *,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> tuple[str, ...]:
    normalized_tool = str(tool_name or "").strip()
    if normalized_tool == "shell_run":
        command = str(arguments.get("cmd") or "").strip()
        return _extract_skill_lifecycle_cli_commands_from_text(command)
    if normalized_tool == "verify_run":
        commands_obj = arguments.get("commands")
        if not isinstance(commands_obj, list):
            return ()
        commands: list[str] = []
        for item in commands_obj:
            value = str(item or "").strip()
            if not value:
                continue
            commands.extend(_extract_skill_lifecycle_cli_commands_from_text(value))
        return tuple(commands)
    return ()


def _extract_skill_lifecycle_cli_commands_from_text(command: str) -> tuple[str, ...]:
    normalized = " ".join(str(command or "").casefold().split())
    if not normalized:
        return ()
    matches = [
        match.group(1).strip() for match in _SKILL_LIFECYCLE_COMMAND_PATTERN.finditer(normalized)
    ]
    return tuple(matches)


def _classify_manual_skill_bundle_path(
    raw_path: str,
    *,
    workspace_root: Path | None,
) -> tuple[bool, str | None]:
    for candidate_parts in _candidate_relative_path_parts(raw_path, workspace_root=workspace_root):
        for root_parts in project_skill_root_relative_paths():
            if len(candidate_parts) < len(root_parts):
                continue
            if tuple(candidate_parts[: len(root_parts)]) != tuple(root_parts):
                continue
            if len(candidate_parts) == len(root_parts):
                return True, None
            return True, candidate_parts[len(root_parts)]
    return False, None


def _candidate_relative_path_parts(
    raw_path: str,
    *,
    workspace_root: Path | None,
) -> tuple[tuple[str, ...], ...]:
    candidates: list[tuple[str, ...]] = []
    text = str(raw_path or "").strip()
    if not text:
        return ()

    if workspace_root is not None:
        try:
            relative = Path(text).expanduser().resolve().relative_to(workspace_root.resolve())
        except (OSError, RuntimeError, ValueError):
            relative = None
        if relative is not None:
            candidates.append(relative.parts)

    normalized = text.replace("\\", "/")
    parts = tuple(part for part in normalized.split("/") if part and part != ".")
    if parts and not (len(parts) > 1 and parts[0].endswith(":")):
        candidates.append(parts)

    ordered: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        ordered.append(candidate)
    return tuple(ordered)


def _normalized_optional_string(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _skills_eval_tempdir_prefix(mode_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(mode_name or "").strip().lower()).strip("-")
    if not slug:
        slug = "mode"
    return f"se-{slug[:12]}-"


def _normalized_string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("expected a JSON array of strings")
    return tuple(_ordered_unique_strings(str(item or "").strip() for item in value))


def _ordered_unique_strings(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        lowered = value.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(value)
    return out


def _case_to_payload(case: SkillsEvalCase) -> dict[str, object]:
    return {
        "id": case.id,
        "workspace": case.workspace.as_posix(),
        "task": case.task,
        "invocation_mode": case.invocation_mode,
        "explicit_skill_name": case.explicit_skill_name,
        "expected_skills": list(case.expected_skills),
        "verification_command": case.verification_command,
        "session_mode": case.session_mode,
        "tags": list(case.tags),
        "notes": case.notes,
    }


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _format_rate(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value * 100:.1f}%"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{numeric * 100:.1f}%"


def _launch_runtime_summary(records: Sequence[SkillsEvalRecord]) -> dict[str, object]:
    executed = [record for record in records if record.status != "skipped"]
    return _counter_rate_summary(
        executed,
        metric_fields=(
            "completion_gate_failure_count",
            "completion_gate_incomplete_after_retries_count",
            "forced_final_summary_count",
            "verification_credit_miss_count",
        ),
    )


def _skill_selection_summary(records: Sequence[SkillsEvalRecord]) -> dict[str, object]:
    executed = [record for record in records if record.status != "skipped"]
    automatic = [record for record in executed if record.selector_available() is not None]
    exact_count = sum(1 for record in automatic if record.automatic_selection_exact_match() is True)
    available_count = sum(1 for record in automatic if record.selector_available() is True)
    unavailable_count = len(automatic) - available_count
    blocked_count = sum(int(record.skill_selection_blocked_count or 0) for record in automatic)
    blocked_run_count = sum(
        1 for record in automatic if int(record.skill_selection_blocked_count or 0) > 0
    )
    unhonored_count = sum(int(record.skill_selection_unhonored_count or 0) for record in automatic)
    unhonored_run_count = sum(
        1 for record in automatic if int(record.skill_selection_unhonored_count or 0) > 0
    )
    successful_read_count = sum(int(record.successful_skill_read_count or 0) for record in executed)
    successful_read_run_count = sum(
        1 for record in executed if int(record.successful_skill_read_count or 0) > 0
    )
    return {
        "automatic_selection_runs": len(automatic),
        "automatic_selection_exact_match_count": exact_count,
        "automatic_selection_exact_match_rate": _rate(exact_count, len(automatic)),
        "selector_available_run_count": available_count,
        "selector_unavailable_run_count": unavailable_count,
        "selector_unavailable_rate": _rate(unavailable_count, len(automatic)),
        "skill_selection_blocked_count": blocked_count,
        "skill_selection_blocked_run_count": blocked_run_count,
        "skill_selection_blocked_rate": _rate(blocked_run_count, len(automatic)),
        "skill_selection_unhonored_count": unhonored_count,
        "skill_selection_unhonored_run_count": unhonored_run_count,
        "skill_selection_unhonored_rate": _rate(unhonored_run_count, len(automatic)),
        "successful_skill_read_count": successful_read_count,
        "successful_skill_read_run_count": successful_read_run_count,
        "successful_skill_read_rate": _rate(successful_read_run_count, len(executed)),
    }


def _counter_rate_summary(
    records: Sequence[SkillsEvalRecord],
    *,
    metric_fields: Sequence[str],
) -> dict[str, object]:
    summary: dict[str, object] = {}
    executed_count = len(records)
    for field_name in metric_fields:
        total_count = sum(int(getattr(record, field_name, 0) or 0) for record in records)
        run_count = sum(1 for record in records if int(getattr(record, field_name, 0) or 0) > 0)
        metric_prefix = field_name.removesuffix("_count")
        summary[field_name] = total_count
        summary[f"{metric_prefix}_run_count"] = run_count
        summary[f"{metric_prefix}_rate"] = _rate(run_count, executed_count)
    return summary


def _event_is_verification_credit_miss(*, event_type: str, payload: Mapping[str, Any]) -> bool:
    if event_type not in (_COMPLETION_GATE_FAILURE_EVENTS | _COMPLETION_GATE_INCOMPLETE_EVENTS):
        return False
    stage = str(payload.get("stage") or "").strip()
    if stage not in _VERIFICATION_CREDIT_MISS_STAGES:
        return False
    state_obj = payload.get("state")
    state = state_obj if isinstance(state_obj, Mapping) else {}
    try:
        verification_attempt_count = int(state.get("verification_attempt_count") or 0)
    except (TypeError, ValueError):
        verification_attempt_count = 0
    return verification_attempt_count > 0


def _max_rate_gate(*, actual: object, threshold: float, description: str) -> dict[str, object]:
    actual_rate = _coerce_rate(actual)
    status = "fail" if actual_rate is None or actual_rate > threshold else "pass"
    return {
        "status": status,
        "actual": actual_rate,
        "max_allowed": threshold,
        "description": description,
    }


def _max_count_gate(*, actual: object, threshold: int, description: str) -> dict[str, object]:
    try:
        actual_count = int(actual)
    except (TypeError, ValueError):
        actual_count = None
    status = "fail" if actual_count is None or actual_count > threshold else "pass"
    return {
        "status": status,
        "actual": actual_count,
        "max_allowed": threshold,
        "description": description,
    }


def _min_rate_gate(*, actual: object, threshold: float, description: str) -> dict[str, object]:
    actual_rate = _coerce_rate(actual)
    status = "fail" if actual_rate is None or actual_rate < threshold else "pass"
    return {
        "status": status,
        "actual": actual_rate,
        "min_required": threshold,
        "description": description,
    }


def _coerce_rate(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _gate_threshold_text(gate: Mapping[str, Any]) -> str:
    if "max_allowed" in gate:
        return f"<= {_format_rate(gate.get('max_allowed'))}"
    if "min_required" in gate:
        return f">= {_format_rate(gate.get('min_required'))}"
    if "required" in gate:
        return repr(gate.get("required"))
    return "-"
