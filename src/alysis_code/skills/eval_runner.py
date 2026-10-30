from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from ..agent_loop import create_session
from ..config import AppConfig, clone_cfg, load_config, resolve_run_deadline
from ..execution_deadline import (
    ExecutionDeadline,
    resolve_deadline_degradation_policy,
    validate_deadline_seconds,
)
from ..session_store import read_session_events
from ..surface.noop_surface import NoopSurface
from .discovery import resolve_skill_by_name
from .eval_models import (
    SkillsEvalAuthPreflightResult,
    SkillsEvalCase,
    SkillsEvalExecutionRequest,
    SkillsEvalExecutionResult,
    SkillsEvalMode,
)
from .evals import (
    default_skills_eval_output_dir,
    evaluate_skills_launch_readiness,
    extract_skills_eval_metrics,
    load_skills_eval_cases,
    resolve_skills_eval_modes,
    run_skills_eval_suite,
    summarize_skills_launch_candidate_metrics,
    write_skills_eval_artifacts,
)
from .paths import bundled_skill_root
from .prompting import build_explicit_skill_context_message

DEFAULT_SKILLS_EVAL_RUN_DEADLINE_SECONDS = 300.0


def _deadline_seconds_arg(value: str) -> float:
    try:
        return validate_deadline_seconds(value, key="--deadline-seconds")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


class OneShotSkillsEvalExecutor:
    def __init__(
        self,
        *,
        cfg: AppConfig,
        api_key_override: str | None = None,
        session_mode: str = "auto",
        one_shot_execution: bool = True,
        verification_enabled: bool = True,
        deadline_seconds: float | None = None,
        deadline_source: str = "skills_eval",
    ) -> None:
        self.cfg = clone_cfg(cfg)
        self.api_key_override = api_key_override
        self.session_mode = session_mode
        self.one_shot_execution = one_shot_execution
        self.verification_enabled = verification_enabled
        self.deadline_seconds = deadline_seconds
        self.deadline_source = deadline_source

    def execute(self, request: SkillsEvalExecutionRequest) -> SkillsEvalExecutionResult:
        session = None
        try:
            session_cfg = clone_cfg(self.cfg)
            session_cfg.skills_enabled = request.mode.skills_enabled
            session_cfg.skills_auto_invoke = request.mode.skills_auto_invoke
            if request.case.session_mode not in {None, "readonly"}:
                return SkillsEvalExecutionResult(
                    agent_exit_code=1,
                    error="eval cases may only override session mode to readonly",
                )
            session_mode = (
                "readonly" if request.case.session_mode == "readonly" else self.session_mode
            )
            readonly_case = session_mode == "readonly"
            one_shot_execution = self.one_shot_execution and not readonly_case
            verification_enabled = self.verification_enabled and not readonly_case
            session = create_session(
                cfg=session_cfg,
                root=request.workspace,
                mode=session_mode,
                yes=True,
                max_steps=request.max_steps,
                no_log=False,
                api_key_override=self.api_key_override,
                non_interactive=True,
                one_shot_execution=one_shot_execution,
                verification_enabled=verification_enabled,
                authoritative_verification_commands=(
                    [request.case.verification_command]
                    if verification_enabled and request.case.verification_command
                    else None
                ),
                session_log_dir_override=request.sessions_dir,
                session_id_override=request.session_id,
                surface=NoopSurface(),
                execution_deadline=(
                    ExecutionDeadline.from_duration(
                        self.deadline_seconds,
                        source=self.deadline_source,
                        degradation_policy=resolve_deadline_degradation_policy(self.cfg),
                    )
                    if self.deadline_seconds is not None
                    else None
                ),
            )
            skills_advertised_present = _messages_contain_marker(
                session.messages,
                "<skill_context>",
            )
            repo_conventions_present = _messages_contain_marker(
                session.messages,
                "<repo_conventions>",
            )
            explicit_skill_context_used = False
            ephemeral_user_messages: list[str] | None = None
            if request.case.invocation_mode == "explicit_skill":
                explicit_skill_name = str(request.case.explicit_skill_name or "").strip()
                skill = resolve_skill_by_name(session.skill_registry, explicit_skill_name)
                if skill is None:
                    return SkillsEvalExecutionResult(
                        agent_exit_code=1,
                        skills_advertised_present=skills_advertised_present,
                        repo_conventions_present=repo_conventions_present,
                        error=f"Explicit skill not found: {explicit_skill_name}",
                    )
                explicit_skill_context_used = True
                ephemeral_user_messages = [
                    build_explicit_skill_context_message(
                        skill=skill,
                        task_text=request.case.task,
                    )
                ]

            agent_exit_code = session.run_turn(
                request.case.task,
                ephemeral_user_messages=ephemeral_user_messages,
            )
            session_log_path = session.store.path if session.store.enabled else None
            events = (
                list(read_session_events(session_log_path))
                if session_log_path is not None and session_log_path.exists()
                else []
            )
            deadline_exhausted = any(
                str(event.get("type") or "") == "deadline_exhausted" for event in events
            )
            execution_error = None
            if deadline_exhausted:
                execution_error = "run deadline exhausted"
            elif agent_exit_code != 0:
                execution_error = "agent exited non-zero"
            metrics = extract_skills_eval_metrics(events, workspace_root=request.workspace)
            return SkillsEvalExecutionResult(
                agent_exit_code=agent_exit_code,
                skills_advertised_present=skills_advertised_present,
                repo_conventions_present=repo_conventions_present,
                matched_skill_context_attached=bool(metrics.get("matched_skill_context_attached")),
                matched_skill_names=tuple(metrics.get("matched_skill_names") or ()),
                explicit_skill_context_used=explicit_skill_context_used,
                skill_read_called=bool(metrics.get("skill_read_called")),
                skill_read_names=tuple(metrics.get("skill_read_names") or ()),
                skill_read_call_count=int(metrics.get("skill_read_call_count") or 0),
                successful_skill_read_names=tuple(metrics.get("successful_skill_read_names") or ()),
                successful_skill_read_count=int(metrics.get("successful_skill_read_count") or 0),
                skill_selection_status=metrics.get("skill_selection_status"),
                skill_selection_selected_names=tuple(
                    metrics.get("skill_selection_selected_names") or ()
                ),
                skill_selection_call_count=int(metrics.get("skill_selection_call_count") or 0),
                skill_selection_failure_kinds=tuple(
                    metrics.get("skill_selection_failure_kinds") or ()
                ),
                skill_selection_blocked_count=int(
                    metrics.get("skill_selection_blocked_count") or 0
                ),
                skill_selection_blocked_reasons=tuple(
                    metrics.get("skill_selection_blocked_reasons") or ()
                ),
                skill_selection_unhonored_count=int(
                    metrics.get("skill_selection_unhonored_count") or 0
                ),
                skill_lifecycle_cli_used=bool(metrics.get("skill_lifecycle_cli_used")),
                skill_lifecycle_cli_commands=tuple(
                    metrics.get("skill_lifecycle_cli_commands") or ()
                ),
                skill_lifecycle_cli_call_count=int(
                    metrics.get("skill_lifecycle_cli_call_count") or 0
                ),
                manual_skill_bundle_accessed=bool(metrics.get("manual_skill_bundle_accessed")),
                manual_skill_bundle_names=tuple(metrics.get("manual_skill_bundle_names") or ()),
                manual_skill_bundle_access_count=int(
                    metrics.get("manual_skill_bundle_access_count") or 0
                ),
                tool_call_count=int(metrics.get("tool_call_count") or 0),
                completion_gate_failure_count=int(
                    metrics.get("completion_gate_failure_count") or 0
                ),
                completion_gate_incomplete_after_retries_count=int(
                    metrics.get("completion_gate_incomplete_after_retries_count") or 0
                ),
                forced_final_summary_count=int(metrics.get("forced_final_summary_count") or 0),
                verification_credit_miss_count=int(
                    metrics.get("verification_credit_miss_count") or 0
                ),
                session_log_path=session_log_path,
                session_artifact_root=(
                    session.store.session_artifact_root if session.store.enabled else None
                ),
                error=execution_error,
            )
        except Exception as exc:  # noqa: BLE001
            return SkillsEvalExecutionResult(
                agent_exit_code=1,
                error=str(exc),
            )
        finally:
            if session is not None:
                session.close()


def classify_skills_eval_failure(
    error: str | None,
    *,
    agent_exit_code: int | None = None,
) -> str:
    text = str(error or "").strip().casefold()
    if not text and agent_exit_code == 0:
        return "ok"
    if any(
        marker in text
        for marker in (
            "invalid_api_key",
            "api key",
            "incorrect api key",
            "authentication",
            "unauthorized",
            "expired token",
            "invalid token",
            "permission denied",
            "permission_error",
            "401",
            "403",
        )
    ):
        return "auth"
    if any(
        marker in text
        for marker in (
            "rate limit",
            "rate_limit",
            "too many requests",
            "429",
            "tpm",
        )
    ):
        return "rate_limit"
    if any(
        marker in text
        for marker in (
            "model not found",
            "model is not set",
            "unknown model",
            "unsupported model",
            "config set model",
            "base_url",
            "connection",
            "connect",
            "timeout",
            "timed out",
            "dns",
            "proxy",
            "ssl",
            "tls",
            "refused",
            "provider",
            "bad gateway",
            "service unavailable",
            "502",
            "503",
            "504",
        )
    ):
        return "provider"
    return "runtime"


def run_skills_eval_auth_preflight(
    *,
    cfg: AppConfig,
    workspace: Path,
    output_dir: Path,
    api_key_override: str | None = None,
    session_mode: str = "readonly",
    max_steps: int = 3,
    mode: SkillsEvalMode | None = None,
    deadline_seconds: float | None = None,
    deadline_source: str = "skills_eval",
) -> SkillsEvalAuthPreflightResult:
    preflight_mode = mode or SkillsEvalMode(
        name="baseline",
        conventions_enabled=False,
        skills_enabled=False,
        skills_auto_invoke=False,
    )
    preflight_case = SkillsEvalCase(
        id="auth_preflight",
        workspace=workspace,
        task="Reply with exactly OK.",
        invocation_mode="normal",
    )
    run_output_dir = output_dir.resolve()
    run_output_dir.mkdir(parents=True, exist_ok=True)
    sessions_dir = run_output_dir / "sessions"
    executor = OneShotSkillsEvalExecutor(
        cfg=cfg,
        api_key_override=api_key_override,
        session_mode=session_mode,
        one_shot_execution=False,
        verification_enabled=False,
        deadline_seconds=deadline_seconds,
        deadline_source=deadline_source,
    )
    result = executor.execute(
        SkillsEvalExecutionRequest(
            case=preflight_case,
            mode=preflight_mode,
            workspace=workspace.resolve(),
            output_dir=run_output_dir,
            sessions_dir=sessions_dir,
            session_id="skills_eval_auth_preflight",
            max_steps=max(1, int(max_steps)),
        )
    )
    classification = classify_skills_eval_failure(
        result.error,
        agent_exit_code=result.agent_exit_code,
    )
    ok = classification == "ok"
    message = "Auth preflight passed." if ok else str(result.error or "auth preflight failed")
    return SkillsEvalAuthPreflightResult(
        ok=ok,
        classification=classification,  # type: ignore[arg-type]
        message=message,
        agent_exit_code=result.agent_exit_code,
        session_log_path=result.session_log_path,
        session_artifact_root=result.session_artifact_root,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Skills evaluation harness for Alysis Code.",
    )
    parser.add_argument(
        "--manifest", required=True, help="Path to the skills eval cases JSON manifest."
    )
    parser.add_argument(
        "--output-dir",
        help="Output directory for eval artifacts. Defaults to ./.alysis/evals/skills/<timestamp>/",
    )
    parser.add_argument(
        "--mode",
        action="append",
        dest="modes",
        help="Restrict execution to one or more eval modes.",
    )
    parser.add_argument(
        "--case",
        action="append",
        dest="case_ids",
        help="Restrict execution to one or more case ids.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=25,
        help="Per-run max_steps passed into the existing one-shot runtime.",
    )
    parser.add_argument(
        "--deadline-seconds",
        type=_deadline_seconds_arg,
        help=(
            "Per-run wall-clock deadline. Launch gates default to 300 seconds; "
            "diagnostic runs remain unbounded unless this option is supplied."
        ),
    )
    parser.add_argument("--model", help="Optional model override for this eval run.")
    parser.add_argument("--api-key", help="Optional API key override for this eval run.")
    parser.add_argument(
        "--launch-gate",
        action="store_true",
        help="Evaluate and enforce the skills launch-readiness thresholds.",
    )
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest).expanduser().resolve()
    cases = load_skills_eval_cases(manifest_path)
    if not cases:
        parser.error("skills eval manifest contains no cases")
    if args.launch_gate and args.case_ids:
        parser.error("--launch-gate cannot be combined with --case; run the complete suite")
    if args.launch_gate:
        requested_launch_modes = tuple(str(mode or "").strip() for mode in (args.modes or ()))
        if requested_launch_modes != ("combined_auto",):
            parser.error("--launch-gate requires exactly one --mode combined_auto")
    if args.case_ids:
        wanted = {str(item).strip().casefold() for item in args.case_ids if str(item).strip()}
        if not wanted:
            parser.error("--case requires a non-empty case id")
        available = {case.id.casefold() for case in cases}
        unknown = sorted(wanted - available)
        if unknown:
            parser.error("unknown skills eval case id(s): " + ", ".join(unknown))
        cases = tuple(case for case in cases if case.id.casefold() in wanted)
    if args.launch_gate:
        coverage_errors = _launch_suite_coverage_errors(cases)
        if coverage_errors:
            parser.error("incomplete launch suite: " + "; ".join(coverage_errors))
    modes = resolve_skills_eval_modes(args.modes)
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else default_skills_eval_output_dir(root=Path.cwd())
    )

    cfg = load_config()
    if args.model:
        cfg.model = str(args.model).strip()

    deadline_seconds: float | None = None
    deadline_source = "skills_eval"
    if args.launch_gate:
        resolved_deadline = resolve_run_deadline(
            cfg,
            cli_deadline_seconds=args.deadline_seconds,
            default_seconds=DEFAULT_SKILLS_EVAL_RUN_DEADLINE_SECONDS,
        )
        if resolved_deadline.seconds is None:
            parser.error(
                "--launch-gate requires a finite per-run deadline; "
                "pass --deadline-seconds or configure run_deadline_seconds"
            )
        deadline_seconds = resolved_deadline.seconds
        deadline_source = str(resolved_deadline.source)
    elif args.deadline_seconds is not None:
        deadline_seconds = args.deadline_seconds
        deadline_source = "explicit_cli"

    if args.launch_gate:
        preflight = run_skills_eval_auth_preflight(
            cfg=cfg,
            workspace=cases[0].workspace,
            output_dir=output_dir / "preflight",
            api_key_override=args.api_key,
            deadline_seconds=deadline_seconds,
            deadline_source=deadline_source,
        )
        if not preflight.ok:
            print(f"Skills eval auth preflight failed ({preflight.classification}).")
            return 1

    executor = OneShotSkillsEvalExecutor(
        cfg=cfg,
        api_key_override=args.api_key,
        deadline_seconds=deadline_seconds,
        deadline_source=deadline_source,
    )
    artifacts = run_skills_eval_suite(
        cases=cases,
        modes=modes,
        output_dir=output_dir,
        executor=executor,
        manifest_path=manifest_path,
        max_steps=max(1, int(args.max_steps)),
        deadline_seconds=deadline_seconds,
    )
    failed_cases = sum(1 for record in artifacts.records if record.status == "failed")
    executed_cases = sum(1 for record in artifacts.records if record.status != "skipped")

    release_gates: dict[str, object] | None = None
    if args.launch_gate:
        config_snapshot = {
            "skills_auto_invoke": any(
                mode.name == "combined_auto" and mode.skills_auto_invoke for mode in modes
            )
        }
        launch_metrics = summarize_skills_launch_candidate_metrics(
            artifacts.summary,
            config_snapshot=config_snapshot,
        )
        gated_summary = {
            **artifacts.summary,
            **launch_metrics,
            "failed_selected_record_count": failed_cases,
        }
        release_gates = evaluate_skills_launch_readiness(
            summary=gated_summary,
            config_snapshot=config_snapshot,
        )
        gated_summary["release_gates"] = release_gates
        artifacts = write_skills_eval_artifacts(
            output_dir=artifacts.output_dir,
            records=artifacts.records,
            summary=gated_summary,
            manifest_path=manifest_path,
            cases=cases,
        )

    print(f"Skills eval output: {artifacts.output_dir}")
    print(f"Raw results: {artifacts.results_path}")
    print(f"Summary JSON: {artifacts.summary_json_path}")
    print(f"Summary Markdown: {artifacts.summary_md_path}")
    modes_obj = artifacts.summary.get("modes")
    if isinstance(modes_obj, dict):
        for mode_name in sorted(modes_obj):
            mode_payload = modes_obj.get(mode_name)
            if not isinstance(mode_payload, dict):
                continue
            executed = mode_payload.get("executed_runs", 0)
            pass_rate = mode_payload.get("pass_rate")
            print(
                f"- {mode_name}: executed={executed}, pass_rate={_format_rate_for_cli(pass_rate)}"
            )
    if failed_cases:
        print(f"Failed cases: {failed_cases}")
    if executed_cases == 0:
        print("No cases executed.")
    if release_gates is not None:
        production_ready = bool(release_gates.get("production_ready"))
        readiness_label = "passed" if production_ready else "failed"
        print(f"Launch readiness: {readiness_label}")
        if not production_ready:
            failing_gates = release_gates.get("failing_gates")
            if isinstance(failing_gates, list) and failing_gates:
                print("Failing launch gates: " + ", ".join(str(item) for item in failing_gates))
    else:
        production_ready = True
    return 0 if executed_cases > 0 and failed_cases == 0 and production_ready else 1


def _launch_suite_coverage_errors(cases: Sequence[SkillsEvalCase]) -> tuple[str, ...]:
    errors: list[str] = []
    expected_by_key = {
        skill.casefold(): skill for case in cases for skill in case.expected_skills if skill.strip()
    }
    expected_skills = sorted(expected_by_key.values(), key=str.casefold)
    if not expected_skills:
        return ("no expected skills",)

    if any(any(tag.casefold() == "bundled-pack" for tag in case.tags) for case in cases):
        root = bundled_skill_root()
        bundled_names = sorted(
            child.name
            for child in root.iterdir()
            if child.is_dir()
            and (child / "SKILL.md").is_file()
            # These evaluations create ordinary CLI sessions without a desktop
            # host bridge. Computer Use has separate host protocol/runtime tests.
            and child.name != "computer-use"
        )
        missing_bundled = [name for name in bundled_names if name.casefold() not in expected_by_key]
        if missing_bundled:
            errors.append("missing bundled skill coverage for " + ", ".join(missing_bundled))

    missing_verifiers = sorted(case.id for case in cases if not case.verification_command)
    if missing_verifiers:
        errors.append("missing verification commands for " + ", ".join(missing_verifiers))

    negative_controls = [
        case
        for case in cases
        if not case.expected_skills
        and any(tag.casefold() == "negative-control" for tag in case.tags)
    ]
    non_readonly_controls = sorted(
        case.id for case in negative_controls if case.session_mode != "readonly"
    )
    if non_readonly_controls:
        errors.append("negative controls must be readonly: " + ", ".join(non_readonly_controls))

    for skill in expected_skills:
        skill_key = skill.casefold()
        positives = [
            case
            for case in cases
            if any(expected.casefold() == skill_key for expected in case.expected_skills)
        ]
        if not any(case.invocation_mode == "normal" for case in positives):
            errors.append(f"{skill} has no normal positive case")
        if not any(
            case.invocation_mode == "explicit_skill"
            and str(case.explicit_skill_name or "").casefold() == skill_key
            for case in positives
        ):
            errors.append(f"{skill} has no explicit positive case")
        adjacent_tag = f"{skill_key}-adjacent"
        if not any(
            any(tag.casefold() == adjacent_tag for tag in case.tags) for case in negative_controls
        ):
            errors.append(f"{skill} has no adjacent negative control")
    return tuple(errors)


def _messages_contain_marker(messages: Sequence[dict[str, object]], marker: str) -> bool:
    return any(marker in str(message.get("content") or "") for message in messages)


def _format_rate_for_cli(value: object) -> str:
    if value is None:
        return "-"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{numeric * 100:.1f}%"


if __name__ == "__main__":
    raise SystemExit(main())
