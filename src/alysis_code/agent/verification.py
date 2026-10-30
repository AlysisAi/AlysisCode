from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..diff_paths import iter_patch_paths
from ..failure_category import FailureCategory, is_infra_unavailable_error
from ..language_policy import normalize_language_name
from ..project_folders import allowed_roots
from ..runtime_kind import RuntimeKind
from ..sandbox_runner import HostShellRunner, LazyShellRunner
from ..tools.availability import is_tool_unavailable_result
from ..verification_command_analysis import (
    analyze_verification_command,
    is_benign_non_execution_reason,
)
from ..verification_contract import VerificationCommandProvenance, VerificationCommandRequirement
from ..verify_gate import (
    ResolvedVerifyCommands,
    assess_verification_command_execution,
    extract_actionable_failure_snippet,
    extract_verification_failure_snippet,
    is_authoritative_verify_command_selection,
    is_toolchain_unavailable_verification_output,
    resolve_task_aware_verify_command_selection,
    resolve_verify_sandbox_mode,
    verification_selection_payload,
)
from ..verify_gate import run_task_verification as run_task_verification
from .acceptance_contract import (
    AcceptanceContract,
    AcceptanceCriterionKind,
    acceptance_contract_problem_payload,
    extract_explicit_acceptance_commands,
    invalidate_acceptance_evidence,
    record_acceptance_tool_effect,
    record_consumer_profile_observation,
)
from .blast_radius import (
    MAX_SCOPE_RUNS,
    BlastRadiusAssessment,
    BlastRadiusPolicy,
    BlastRadiusScope,
    BlastRadiusStatus,
    ScopePhase,
    ScopeRun,
    assess_blast_radius,
    blast_radius_blocks_finalization,
    build_blast_radius_nudge_line,
    classify_scope_phase,
    command_path_selectors,
)
from .completion_certificate import (
    CompletionCertificateInput,
    evaluate_completion_certificate,
)
from .completion_gate import CompletionGateControllerState
from .mutation_classification import classify_mutation_paths, material_mutation_paths
from .prompt_context import (
    _extract_workspace_relation_paths_from_text,
    _normalize_repo_relative_hint_path,
    _paths_require_verification,
    _session_repo_scan,
    _session_verify_command_selection,
    _verification_commands_apply_to_paths,
    refresh_session_environment_context_message,
)
from .regression_baseline import (
    EMPTY_REGRESSION_DIFF,
    BaselineRecord,
    PostEditTestRun,
    RegressionDiffResult,
    TestReport,
    aggregate_regression_results,
    baseline_command_key,
    classify_regression_diff,
    command_is_test_runner,
    parse_test_report,
)
from .reproduction_first import (
    MAX_REPRO_ARTIFACTS,
    MAX_REPRO_RUNS,
    ReproAssessment,
    ReproPhase,
    ReproRun,
    TaskShape,
    assess_reproduction,
    build_repro_artifacts_nudge_line,
    build_repro_nudge_line,
    classify_repro_phase,
    match_repro_artifacts,
    repro_blocks_finalization,
)
from .turn_contract import (
    AdvisoryCompletion,
    DispositionRecord,
    Expectation,
    ExpectationAssessment,
    ExpectationEvidence,
    ExpectationKind,
    assess_expectations,
    match_expectation_evidence,
    normalize_locus_path,
)
from .turn_path import greenfield_verification_bootstrap_enabled
from .verification_commands import (
    _matching_effective_verification_commands,
    _normalize_shell_command_for_match,
)
from .verification_evidence import (
    VerificationEvidence,
    VerificationEvidenceCategory,
    classify_verification_evidence,
    command_is_qualifying_execution_evidence,
)

if TYPE_CHECKING:
    from ..config import AppConfig
    from .turn_path import _OneShotRepoTurnIntent


_COMMAND_LIKE_MUTATION_TOOL_NAMES = {"verify_run", "shell_run"}
_MATERIAL_EDIT_TOOL_NAMES = {
    "fs_write",
    "fs_edit",
    "git_apply_patch",
    "fs_move",
    "fs_copy",
    "fs_delete",
    "fs_mkdir",
    "shell_service_start",
    "workspace_preview_start",
}
_VERIFICATION_SHELL_MARKERS = (
    "pytest",
    "py.test",
    "unittest",
    "tox",
    "nox",
    "go test",
    "cargo test",
    "npm test",
    "pnpm test",
    "yarn test",
    "vitest",
    "jest",
    "ruff check",
    "mypy",
    "flake8",
    "pylint",
    "make test",
    "make check",
)
_TEST_EXECUTION_COMMAND_RE = re.compile(
    r"(?:^|\s)(?:pytest|py\.test|tox|nox)(?:\s|$)|"
    r"\b(?:python(?:3)?|py)\s+-m\s+(?:pytest|unittest)\b|"
    r"\b(?:python(?:3)?|py)\b[^\n]*\bmanage\.py\s+test\b|"
    r"\b(?:python(?:3)?|py)\b[^\n]*\b(?:runtests?|test_[^\s/]+|[^\s/]+_test)\.py\b|"
    r"(?:^|\s)(?:\./)?(?:bin/)?(?:runtests?|test)(?:\s|$)|"
    r"\b(?:go|cargo)\s+test\b|"
    r"\b(?:npm|pnpm|yarn)\s+test\b|"
    r"\b(?:vitest|jest|rspec|phpunit|ctest)\b|"
    r"\b(?:mvn|mvnw|maven|gradle|gradlew|dotnet|bazel|mix)\b[^\n]*\btest\b|"
    r"\b(?:make|just)\s+(?:test|check)\b",
    re.IGNORECASE,
)
_TEST_SUCCESS_CLAIM_RE = re.compile(
    r"\b(?:all\s+)?(?:\d+\s+)?tests?(?:\s+suite)?\s+"
    r"(?:(?:is|are|was|were)\s+)?"
    r"(?:pass(?:ed|es|ing)?|succeed(?:ed|s)?|green)\b|"
    r"\btests?\s*:\s*[^\n]{0,120}\b(?:pass(?:ed|es|ing)?|succeed(?:ed|s)?)\b|"
    r"\b(?:pass(?:ed|es|ing)?|green)\s+(?:all\s+)?tests?\b",
    re.IGNORECASE,
)
_GENERIC_VERIFICATION_SUCCESS_CLAIM_RE = re.compile(
    r"\bverified\b|"
    r"\bverification\s+(?:passed|succeeded|completed|was\s+successful)\b|"
    r"\b(?:validation|checks?)\s+(?:passed|succeeded)\b",
    re.IGNORECASE,
)
_NEGATED_CLAIM_PREFIX_RE = re.compile(
    r"(?:\b(?:not|never|no|without)\b[^.!?\n]{0,32}|"
    r"\b(?:cannot|can't|could\s+not|couldn't|did\s+not|didn't|wasn't|isn't|"
    r"unable\s+to|failed\s+to)(?:\s+be)?)\s*$",
    re.IGNORECASE,
)
_SAFE_LEADING_CD_RE = re.compile(
    r"^\s*cd(?:\s+/d)?\s+(?:\"[^\"]*\"|'[^']*'|[^\s]+)\s*&&\s*",
    re.IGNORECASE,
)
_UNSAFE_CLAIM_EVIDENCE_SHELL_RE = re.compile(
    r"\|\||(?<![&])\|(?![&])|;|[\r\n]|&&|(?:^|\s)&(?:\s|$)",
)
_SHELL_REDIRECTION_RE = re.compile(
    r"\s+(?:\d*>&\d+|\d*(?:>>?|<)\s*[^\s]+)(?=\s|$)",
)
SUPPLEMENTAL_VERIFICATION_ADVISORY = (
    "The recorded passing checks are supplemental; no accepted verification evidence "
    "is recorded yet. Confirm the deliverable against the task's exact requirements "
    "and address any remaining verification requirements before finalizing."
)
# Advisory emitted at the first verification-relevant edit when no
# usable pre-edit baseline exists. Advisory only -
# it never blocks the edit; it teaches the baseline-first protocol so failures
# can later be attributed to the change vs pre-existing breakage.
REGRESSION_BASELINE_PRE_EDIT_ADVISORY = (
    "Baseline advisory: this is your first change to a verifiable surface and no "
    "usable pre-edit test baseline is recorded. This edit has already landed; "
    "later test runs cannot retroactively establish the pre-edit state. Verify "
    "the changed code and report any limits on attributing failures to your "
    "change. Advisory only - this does not block your edit."
)
# One-shot advisory emitted the first time a material edit lands inside a
# generated or vendored tree (node_modules, vendor, externals, third_party, ...).
# Advisory only - it never blocks the edit; legitimate vendored fixes exist, but
# in practice edits there are usually a mistargeted change that breaks
# neighboring tests wholesale.
VENDORED_PATH_EDIT_ADVISORY = (
    "Scope advisory: this edit changes files under a generated or vendored tree "
    "({paths}). Vendored/generated code is almost never where the fix belongs - "
    "it is overwritten by upstream syncs and edits there tend to break many "
    "unrelated tests. Prefer the first-party source module; if the vendored copy "
    "truly is the target, keep the change minimal and run the neighboring tests "
    "before finalizing. Advisory only - this does not block your edit."
)
# One-shot finalize-time advisory for execute turns whose completion gate is
# otherwise clear. Small diffs that satisfy the agent's own reproduction are the
# dominant shape of "almost right" outcomes: the issue usually implies more
# behavior (exact message wording, boundary inputs, interactions) than the first
# repro covers. One adversarial pass converts a measurable share of these.
ADVERSARIAL_FINALIZE_REVIEW_ADVISORY = (
    "Adversarial review - one pass before you finish: re-read the original "
    "request end to end and enumerate every behavior it implies, not just the "
    "headline symptom: exact error/message wording, boundary and degenerate "
    "inputs (zero, empty, None, negative), types and units, and every "
    "interaction or API named anywhere in the report. For each implied "
    "behavior, either point at evidence you already ran that covers it, or "
    "extend your reproduction to cover it and run it now. Acceptance checks "
    "usually probe edge semantics beyond the reported case. If everything is "
    "already covered, finalize."
)


def _adversarial_finalize_enabled(cfg: Any | None) -> bool:
    """Kill-switch for the adversarial finalize review (near-miss pass).

    ``ALYSIS_ADVERSARIAL_FINALIZE`` (off/0/false/no/disabled) wins over the
    config value; default is on.
    """
    from ..branding import env_get

    env_value = env_get("ALYSIS_ADVERSARIAL_FINALIZE")
    if env_value is not None:
        normalized = str(env_value).strip().lower()
        if normalized in {"off", "0", "false", "no", "disabled"}:
            return False
        if normalized in {"on", "1", "true", "yes", "enabled"}:
            return True
    return bool(getattr(cfg, "adversarial_finalize_review", True))


# Maximum bounded repair rounds for a post-edit execution-evidence deficit before
# the gate finalizes honestly-unverified rather than accepting prose in its place.
EVIDENCE_REPAIR_ROUND_BOUND = 2
# Visible marker appended to the final summary when the run finalizes without the
# execution evidence the ordering rule requires (fail honest, never silent).
HONEST_UNVERIFIED_FINALIZATION_MARKER = (
    "\n\n---\n"
    "⚠️ Unverified: I could not obtain a passing test execution after my last "
    "change to the code within this run. This result is finalized as UNVERIFIED — "
    "the change has not been confirmed by running the relevant tests."
)
# Visible marker appended when the run finalizes with regressions the change
# introduced that a bounded action-only repair could not resolve (fail honest).
# Distinct wording from the unverified marker; leads with "REGRESSIONS UNRESOLVED".
_REGRESSIONS_UNRESOLVED_FINALIZATION_MARKER_PREFIX = (
    "\n\n---\n"
    "⛔ REGRESSIONS UNRESOLVED: {ids}. These tests passed in the pre-edit baseline "
    "of `{baseline}` and fail after my change; I could not make them pass within "
    "this run. This result is finalized with KNOWN REGRESSIONS my change introduced."
)


def build_regressions_unresolved_marker(
    regressed_ids: list[str] | tuple[str, ...],
    *,
    baseline_command: str = "",
) -> str:
    ids = ", ".join(str(item) for item in regressed_ids if str(item).strip())
    baseline = str(baseline_command or "").strip() or "the baseline command"
    return _REGRESSIONS_UNRESOLVED_FINALIZATION_MARKER_PREFIX.format(ids=ids, baseline=baseline)


# Visible marker appended when the run finalizes with test failures whose
# relationship to the change could not be established (no comparable baseline).
# Distinct wording again; leads with "UNATTRIBUTED FAILURES".
_UNATTRIBUTED_FAILURES_FINALIZATION_MARKER_PREFIX = (
    "\n\n---\n"
    "⚠️ UNATTRIBUTED FAILURES: {ids}. These tests fail after my change, but I have "
    "no comparable pre-edit baseline for the same command to determine whether my "
    "change caused them. This result is finalized with their cause UNATTRIBUTED — "
    "neither confirmed pre-existing nor confirmed a regression."
)


def build_unattributed_failures_marker(unattributed_ids: list[str] | tuple[str, ...]) -> str:
    ids = ", ".join(str(item) for item in unattributed_ids if str(item).strip())
    return _UNATTRIBUTED_FAILURES_FINALIZATION_MARKER_PREFIX.format(ids=ids)


_COMPLETION_GATE_PROBLEM_LABELS = {
    "empty_final_response": "empty final response",
    "no_material_edits": "no material edits",
    "verification_not_attempted": "verification not attempted",
    "verification_incomplete": "verification coverage incomplete",
    "verification_failed": "verification failing",
    "regressions_detected": "regressions introduced",
    "unattributed_failures": "failures not yet attributed",
    "expectations_unaddressed": "task expectations unaddressed",
    "repro_unconfirmed": "reported symptom not reproduced",
    "repro_artifacts_present": "reproduction scaffolding left in the tree",
    "blast_radius_regressions": "neighbouring tests broken by the change",
    "blast_radius_unverified": "blast radius not measured",
    "acceptance_criteria_unverified": "acceptance criteria unverified",
    "acceptance_criteria_failed": "acceptance criteria failed",
    "acceptance_evidence_insufficient": "acceptance evidence insufficient",
    "unexpected_scope_changes": "unexpected scope changes",
}
_ONE_SHOT_COMPLETION_GATE_NUDGE_PREFIX = (
    "Completion gate: this one-shot execution run cannot finalize yet."
)
_RUNTIME_DEFAULT_LANGUAGE = "english"
_RUNTIME_MESSAGE_CATALOG: dict[str, dict[str, str]] = {
    "english": {
        "phase_understanding_request": "Understanding your request.",
        "phase_drafting_response": "Contacting model provider.",
        "phase_compacted_history": "Compacted conversation history.",
        "phase_retrying_step": "Retrying with higher temperature for this step.",
        "phase_running_tool_steps": "Running {count} tool step(s): {names}.",
        "phase_post_explore_bootstrap": (
            "Detected post-explore stagnation; nudging implementation bootstrap."
        ),
        "phase_exploration_stagnation": (
            "Detected exploration stagnation; nudging toward implementation."
        ),
        "phase_failed_edit_loop": "Detected failed edit loop; nudging strategy switch.",
        "phase_continuing_one_shot": (
            "Continuing one-shot execution after non-final progress update."
        ),
        "phase_continuing_execution": "Continuing execution after non-final progress update.",
        "phase_completion_gate_repair": (
            "Completion gate detected missing execution evidence; requesting action-oriented repair."
        ),
        "phase_optional_finalization_review": (
            "Requirements satisfied; running an optional final review."
        ),
        "phase_step_budget_handoff": (
            "Step budget exhausted; preparing a concise handoff so the chat can continue."
        ),
        "phase_writing_final_response": "Writing the final response.",
        "one_shot_continuation_nudge": (
            "Continue execution now. A text-only plan or progress update is incomplete "
            "for this one-shot run. Use the next required tool action to implement or "
            "create the requested deliverable, run an implementation-producing command, "
            "verify only after material work exists or when the implementation already "
            "exists, or explain a concrete evidence-backed blocker."
        ),
        "interactive_continuation_nudge": (
            "Continue execution now. Do not stop at a planning/progress update. "
            "Use tools to make progress, run relevant verification, or explain a concrete blocker."
        ),
        "one_shot_exploration_nudge": (
            "Avoid repeated read-only exploration. Start implementing or creating the "
            "requested deliverable now, delegate once to a suitable available subagent "
            "if more investigation is genuinely needed, or explain a concrete "
            "evidence-backed blocker."
        ),
        "one_shot_post_explore_bootstrap_nudge": (
            "A subagent already returned useful context in this one-shot turn. You now have enough "
            "context to start implementation. Do not call the same research subagent again "
            "in this turn. Do not use more read-only tools unless there is a concrete blocker. "
            "Your next step must be an implementation or deliverable-creation action "
            "(for example fs_edit, fs_write, git_apply_patch, fs_move, fs_copy, or "
            "shell_run only when it actually performs implementation or creates the "
            "requested deliverable) or a concrete evidence-backed blocker report. "
            "Verification comes after material work exists."
        ),
        "one_shot_post_explore_bootstrap_targets": ("Likely repo-root-relative targets: {joined}."),
        "one_shot_edit_strategy_nudge": (
            "Edit strategy is stuck. Switch approach now: re-read the target lines, then use "
            "fs_edit replace_lines/insert_before_line/insert_after_line with expected_old when "
            "possible, or exact ops replace_exact/insert_before_exact/insert_after_exact when "
            "matching known text. If localized fs_edit is a poor fit, use git_apply_patch or "
            "fs_write. Do not repeat the same failing edit call."
        ),
        "one_shot_non_final_progress_stopped": (
            "One-shot run stopped: model returned repeated/non-final progress text "
            "without continuing implementation."
        ),
        "interactive_non_final_progress_stopped": (
            "Execution turn stopped: model returned repeated/non-final progress text "
            "without continuing implementation."
        ),
        "one_shot_post_explore_retry_exhausted": (
            "One-shot run stopped: post-explore stagnation persisted after bounded "
            "implementation-bootstrap nudges. Start implementing or creating the requested "
            "deliverable now or report a concrete blocker."
        ),
        "one_shot_exploration_retry_exhausted": (
            "One-shot run stopped: exploration stagnation persisted after bounded nudges. "
            "Start implementing or creating the requested deliverable, delegate once to a "
            "suitable available subagent if more "
            "investigation is genuinely needed, or report a concrete blocker."
        ),
        "one_shot_edit_retry_exhausted": (
            "One-shot run stopped: failed edit/write loop persisted after bounded strategy "
            "nudges. Switch to exact-match fs_edit ops, or use git_apply_patch/fs_write, "
            "or report a concrete blocker."
        ),
        "one_shot_post_explore_step_budget_exhausted": (
            "One-shot run stopped: post-explore stagnation consumed the step budget. "
            "Start implementing or creating the requested deliverable now or report a concrete blocker."
        ),
        "one_shot_exploration_step_budget_exhausted": (
            "One-shot run stopped: exploration stagnation consumed the step budget. "
            "Start implementing or creating the requested deliverable, delegate once to a "
            "suitable available subagent if more "
            "investigation is genuinely needed, or report a concrete blocker."
        ),
        "one_shot_edit_step_budget_exhausted": (
            "One-shot run stopped: failed edit/write loop consumed the step budget. "
            "Switch to exact-match fs_edit ops, or use git_apply_patch/fs_write, "
            "or report a concrete blocker."
        ),
        "completion_gate_nudge_prefix": _ONE_SHOT_COMPLETION_GATE_NUDGE_PREFIX,
        "interactive_completion_gate_nudge_prefix": (
            "Completion gate: this interactive execution turn cannot finalize yet."
        ),
        "max_steps_exceeded": "max_steps exceeded",
    },
}


@dataclass(frozen=True)
class _ObservedVerificationFailure:
    snippet: str
    category: str
    was_test_run: bool
    command: str
    generation: int
    reported_category: str = ""
    provenance: str = ""
    requirement: str = ""
    host_launcher_unavailable: bool = False


@dataclass
class TurnExecutionState:
    execution_requested: bool
    # Applicability used by the latest host completion certificate, never
    # restored across turns. None keeps early/legacy outcome accounting strict.
    verification_expected: bool | None = field(default=None, kw_only=True)
    expected_verification_commands: set[str] = field(default_factory=set)
    covered_verification_commands: set[str] = field(default_factory=set)
    covered_verification_command_generations: dict[str, int] = field(default_factory=dict)
    material_edit_count: int = 0
    material_edit_generation: int = 0
    material_edit_tools: set[str] = field(default_factory=set)
    touched_repo_paths: set[str] = field(default_factory=set)
    # Current live turn only. Task continuation seeding deliberately does not
    # restore this set: earlier edits cannot enroll this reply's attachments or
    # make inferred repository verification apply to unrelated current effects.
    turn_touched_repo_paths: set[str] = field(default_factory=set, repr=False)
    # Exact scoped file-tool effects can confirm a named external output, but
    # must not masquerade as project files or broaden repo test/diff scopes.
    touched_external_paths: set[str] = field(default_factory=set)
    last_diff_review_generation: int | None = None
    verification_attempt_count: int = 0
    consumer_profile_observations: list[dict[str, Any]] = field(default_factory=list)
    artifact_check_observations: list[dict[str, Any]] = field(default_factory=list)
    verification_tools: set[str] = field(default_factory=set)
    last_verification_passed: bool | None = None
    last_verification_failure_snippet: str = ""
    last_verification_failure_category: str = ""
    failed_verification_command_snippets: dict[str, str] = field(default_factory=dict)
    # Execution outcomes are separate from coverage of required commands. Keys
    # fingerprint exact command/context; unknown contexts cannot be superseded.
    observed_verification_failures: dict[str, _ObservedVerificationFailure] = field(
        default_factory=dict
    )
    verification_relevant_edit_generation: int = 0
    last_successful_verification_generation: int | None = None
    verification_evidence_counts: dict[str, int] = field(default_factory=dict)
    latest_verification_evidence_category: str = ""
    latest_verification_evidence_reason: str = ""
    accepted_verification_evidence: list[dict[str, Any]] = field(default_factory=list)
    supplemental_verification_evidence: list[dict[str, Any]] = field(default_factory=list)
    rejected_verification_evidence: list[dict[str, Any]] = field(default_factory=list)
    executed_verification_evidence: list[dict[str, Any]] = field(default_factory=list)
    verification_evidence_generation: int = 0
    # Ordering rule: the verification-relevant edit generation at the time of the
    # most recent qualifying execution-evidence event (a real test/execution run,
    # not a syntax-only or static check). Post-edit execution evidence exists when
    # this equals the current verification_relevant_edit_generation. Keying on the
    # verification-relevant generation (not every material edit) means a docs-only
    # edit after a passing run does not re-open the requirement.
    last_post_edit_execution_generation: int | None = None
    completion_gate_repair_attempts: int = 0
    completion_gate_no_material_edits_repair_attempts: int = 0
    completion_gate_missing_verify_repair_attempts: int = 0
    completion_gate_failed_verify_repair_attempts: int = 0
    completion_gate_regression_repair_attempts: int = 0
    completion_gate_unattributed_repair_attempts: int = 0
    completion_gate_expectations_repair_attempts: int = 0
    completion_gate_repro_repair_attempts: int = 0
    # Baseline-first regression protocol (step 3). Baselines are parsed per-test
    # outcomes of runs recorded before the first verification-relevant edit
    # (generation 0), keyed by the normalized executed command. Post-edit runs
    # are compared against the same-command baseline at the completion gate.
    test_baselines: dict[str, BaselineRecord] = field(default_factory=dict)
    post_edit_test_runs: list[PostEditTestRun] = field(default_factory=list)
    agent_created_paths: set[str] = field(default_factory=set)
    regression_baseline_pre_edit_nudge_sent: bool = False
    # True when the most recent verification attempt executed a test-runner
    # command (pytest/unittest). Combined with an all-pre-existing/agent-authored
    # diff, this lets the gate clear a non-contract test failure (the sympy-12489
    # model) without masking a failing non-test command.
    last_verification_attempt_was_test_run: bool = False
    latest_regression_diff: dict[str, Any] = field(default_factory=dict)
    pending_regression_capture_events: list[dict[str, Any]] = field(default_factory=list)
    # Turn-contract v2 (step 4). ``post_edit_run_outputs`` are bounded observed
    # outputs of post-edit runs, the fact surface the expected-output evidence
    # linker substring-matches contract literals against. ``recorded_*`` hold any
    # agent-declared dispositions / advisory-completion reason (unpopulated in this
    # release; the gate synthesizes mechanically — see turn_contract.py).
    post_edit_run_outputs: list[dict[str, Any]] = field(default_factory=list)
    recorded_expectation_dispositions: dict[str, DispositionRecord] = field(default_factory=dict)
    recorded_advisory_completion: AdvisoryCompletion | None = None
    latest_expectation_assessment: dict[str, Any] = field(default_factory=dict)
    latest_expectation_evidence: list[dict[str, Any]] = field(default_factory=list)
    # Reproduction-first (step 5). ``repro_task_shape`` is set once at turn start;
    # ``repro_runs`` are the observed executions of agent-created artifacts, each
    # already phase-classified against the artifact set known at the time. The
    # remaining fields carry the guardrail signals the summary must surface.
    repro_task_shape: TaskShape = TaskShape.OTHER
    repro_runs: list[ReproRun] = field(default_factory=list)
    repro_artifact_paths: set[str] = field(default_factory=set)
    repro_revision_rounds: int = 0
    repro_artifacts_edited_after_fix: set[str] = field(default_factory=set)
    repro_surviving_artifacts: tuple[str, ...] = ()
    repro_pre_edit_nudge_sent: bool = False
    repro_not_reproducing_nudge_sent: bool = False
    repro_edited_after_fix_nudge_sent: bool = False
    latest_repro_assessment: dict[str, Any] = field(default_factory=dict)
    pending_repro_run_events: list[dict[str, Any]] = field(default_factory=list)
    # Blast radius (step 6). ``blast_radius_scope`` is recomputed by the turn loop
    # as the touched-path set grows; ``blast_radius_runs`` are every parsed test run
    # observed this turn, each already tagged with what it selected and whether it
    # ran on the clean tree. Capture is command-agnostic on purpose: the scope is
    # matched by coverage at assessment time, so the agent may run it any way.
    blast_radius_scope: BlastRadiusScope = field(default_factory=BlastRadiusScope)
    blast_radius_runs: list[ScopeRun] = field(default_factory=list)
    blast_radius_policy: BlastRadiusPolicy = field(default_factory=BlastRadiusPolicy)
    blast_radius_scope_advisory_sent: bool = False
    blast_radius_shrink_rounds: int = 0
    completion_gate_blast_radius_repair_attempts: int = 0
    latest_blast_radius_assessment: dict[str, Any] = field(default_factory=dict)
    pending_blast_radius_events: list[dict[str, Any]] = field(default_factory=list)
    completion_gate_controller_state: CompletionGateControllerState = field(
        default_factory=CompletionGateControllerState
    )
    acceptance_contract: AcceptanceContract | None = None
    latest_completion_certificate: dict[str, Any] = field(default_factory=dict)
    connector_link_evidence: dict[str, Any] = field(default_factory=dict)

    def refresh_verification_coverage(self) -> None:
        self.covered_verification_commands = {
            command
            for command, generation in self.covered_verification_command_generations.items()
            if generation == self.verification_relevant_edit_generation
        }

    def note_verification_relevant_edit(self) -> None:
        self.verification_relevant_edit_generation += 1
        self.refresh_verification_coverage()
        invalidate_acceptance_evidence(
            self.acceptance_contract, generation=self.verification_relevant_edit_generation
        )

    def note_material_edit(self) -> None:
        self.material_edit_count += 1
        self.material_edit_generation += 1

    def record_diff_review(self) -> None:
        self.last_diff_review_generation = self.material_edit_generation

    def diff_review_is_stale(self) -> bool:
        return self.material_edit_count > 0 and (
            self.last_diff_review_generation is None
            or self.last_diff_review_generation < self.material_edit_generation
        )

    def record_verification_coverage(self, commands: set[str]) -> None:
        if not commands:
            return
        for command in commands:
            self.covered_verification_command_generations[command] = (
                self.verification_relevant_edit_generation
            )
            self.failed_verification_command_snippets.pop(command, None)
        self.last_successful_verification_generation = self.verification_relevant_edit_generation
        self.refresh_verification_coverage()

    def record_verification_failures(self, failures: dict[str, str]) -> None:
        for command, snippet in failures.items():
            clean_command = str(command or "").strip()
            if not clean_command:
                continue
            clean_snippet = str(snippet or "").strip()
            self.failed_verification_command_snippets[clean_command] = clean_snippet

    def verification_failure_diagnostic(self) -> str:
        """Describe recorded outcomes without changing coverage or failure resolution."""
        lines: list[str] = []
        failed_commands = set(self.failed_verification_commands())
        for command in sorted(failed_commands):
            snippet = self.failed_verification_command_snippets[command]
            lines.append(f"- Unresolved required check `{command}`: {snippet or 'failed'}.")
        for failure in self.observed_verification_failures.values():
            failed_commands.add(failure.command)
            metadata = []
            if failure.requirement:
                metadata.append(f"selected requirement: {failure.requirement}")
            if failure.provenance:
                metadata.append(f"selection provenance: {failure.provenance}")
            diagnosis = failure.reported_category or failure.category
            if diagnosis:
                metadata.append(f"reported diagnosis: {diagnosis}")
            if failure.generation != self.verification_relevant_edit_generation:
                metadata.append("observed before the latest relevant edit")
            detail = f" ({'; '.join(metadata)})" if metadata else ""
            command = f"`{failure.command}`" if failure.command else "command identity unavailable"
            lines.append(f"- Unresolved check {command}{detail}: {failure.snippet or 'failed'}.")
        if not lines:
            return ""
        passed_commands = sorted(
            {
                str(item.get("normalized_command") or "")
                for item in self.executed_verification_evidence
                if item.get("generation") == self.verification_relevant_edit_generation
                and item.get("real_execution") is True
                and item.get("observed_exit_code") == 0
                and item.get("observed_output") is True
                and item.get("normalized_command")
                and item.get("normalized_command") not in failed_commands
            }
        )
        if passed_commands:
            lines.append(
                "- Separately recorded successful executions after the latest relevant edit: "
                + ", ".join(f"`{command}`" for command in passed_commands)
                + ". These results remain recorded; they do not resolve the checks above."
            )
        lines.append(
            "- Address each unresolved check on its own evidence, or report its remaining "
            "limitation honestly. Repeating a different unchanged passing check cannot resolve "
            "it. A reported diagnosis alone does not establish whether tests executed, and "
            "does not waive a real failure or a required check."
        )
        return "\n".join(lines)

    def record_verification_evidence(
        self,
        evidence: VerificationEvidence,
        *,
        accepted: bool,
        observed_exit_code: int | None = None,
        observed_output: bool = False,
    ) -> None:
        category = evidence.category.value
        self.verification_evidence_counts[category] = (
            self.verification_evidence_counts.get(category, 0) + 1
        )
        self.latest_verification_evidence_category = category
        self.latest_verification_evidence_reason = evidence.reason
        payload = evidence.as_payload()
        payload["accepted"] = bool(accepted)
        payload["generation"] = self.verification_relevant_edit_generation
        payload["observed_exit_code"] = observed_exit_code
        payload["observed_output"] = bool(observed_output)
        if evidence.real_execution is True:
            self.executed_verification_evidence.append(payload)
            self.executed_verification_evidence[:] = self.executed_verification_evidence[-20:]
        if accepted:
            self.verification_evidence_generation += 1
            self.accepted_verification_evidence.append(payload)
            self.accepted_verification_evidence[:] = self.accepted_verification_evidence[-10:]
        elif evidence.supplemental_only:
            self.supplemental_verification_evidence.append(payload)
            self.supplemental_verification_evidence[:] = self.supplemental_verification_evidence[
                -10:
            ]
        else:
            self.rejected_verification_evidence.append(payload)
            self.rejected_verification_evidence[:] = self.rejected_verification_evidence[-10:]

    def record_executed_command_evidence(
        self,
        *,
        normalized_command: str,
        observed_exit_code: int,
        observed_output: bool,
    ) -> None:
        payload: dict[str, Any] = {
            "evidence_category": "COMMAND_EXECUTION",
            "normalized_command": normalized_command,
            "matched_command": None,
            "real_execution": True,
            "allowed_to_satisfy_contract": False,
            "reason": "observed_shell_verification_execution",
            "covered_verification_commands": [],
            "supplemental_only": False,
            "accepted": False,
            "generation": self.verification_relevant_edit_generation,
            "observed_exit_code": observed_exit_code,
            "observed_output": bool(observed_output),
        }
        self.executed_verification_evidence.append(payload)
        self.executed_verification_evidence[:] = self.executed_verification_evidence[-20:]

    def note_qualifying_execution_evidence(self) -> None:
        self.last_post_edit_execution_generation = self.verification_relevant_edit_generation

    def has_post_edit_execution_evidence(self) -> bool:
        return (
            self.material_edit_count > 0
            and self.last_post_edit_execution_generation is not None
            and self.last_post_edit_execution_generation
            == self.verification_relevant_edit_generation
        )

    def note_agent_created_path(self, path: str) -> None:
        cleaned = str(path or "").strip()
        if cleaned:
            self.agent_created_paths.add(cleaned)

    def has_usable_pre_edit_baseline(self) -> bool:
        """Whether a usable test baseline predates verification-relevant edits.

        This only controls the missing-baseline advisory. A failing run can
        supply baseline facts without satisfying verification, and existence
        does not establish coverage or comparability for another test selection.
        """
        return any(
            record.edit_generation == 0 and record.usable for record in self.test_baselines.values()
        )

    def note_test_execution(
        self,
        *,
        command: str,
        report: Any,
        timestamp: str = "",
    ) -> None:
        """Record a parsed test run as a baseline (gen 0) or a post-edit run.

        A run recorded before any verification-relevant edit (generation 0) with
        a usable parse is a baseline for its normalized command; a later run is a
        post-edit run. Unparseable pre-edit output is noted for telemetry but can
        never serve as a baseline.
        """
        command_key = baseline_command_key(command)
        if not command_key:
            return
        generation = self.verification_relevant_edit_generation
        if generation == 0:
            if report.usable_as_baseline:
                record = BaselineRecord(
                    command=str(command),
                    command_key=command_key,
                    report=report,
                    edit_generation=0,
                    timestamp=timestamp,
                )
                self.test_baselines[command_key] = record
                self.pending_regression_capture_events.append(
                    {
                        "kind": "baseline",
                        "command": str(command),
                        "command_key": command_key,
                        "edit_generation": 0,
                        "report": report.as_payload(),
                    }
                )
            else:
                self.pending_regression_capture_events.append(
                    {
                        "kind": "baseline_unusable",
                        "command": str(command),
                        "command_key": command_key,
                        "edit_generation": 0,
                        "report": report.as_payload(),
                    }
                )
            return
        run = PostEditTestRun(
            command=str(command),
            command_key=command_key,
            report=report,
            generation=generation,
        )
        self.post_edit_test_runs.append(run)
        self.post_edit_test_runs[:] = self.post_edit_test_runs[-40:]
        self.pending_regression_capture_events.append(
            {
                "kind": "post_edit",
                "command": str(command),
                "command_key": command_key,
                "generation": generation,
                "report": report.as_payload(),
            }
        )

    def current_post_edit_test_runs(self) -> list[PostEditTestRun]:
        """Post-edit runs recorded after the last verification-relevant edit."""
        return [
            run
            for run in self.post_edit_test_runs
            if run.generation == self.verification_relevant_edit_generation
        ]

    def compute_regression_diff(self, *, enabled: bool) -> RegressionDiffResult:
        """Aggregate the same-command diff over current post-edit runs.

        Pure attribution: each current-generation post-edit run is compared only
        against a baseline of the same normalized command. With ``enabled`` off,
        returns the empty diff (legacy gate policy).
        """
        if not enabled:
            self.latest_regression_diff = {}
            return EMPTY_REGRESSION_DIFF
        results = [
            classify_regression_diff(
                post_report=run.report,
                baseline=self.test_baselines.get(run.command_key),
                agent_created_paths=self.agent_created_paths,
            )
            for run in self.current_post_edit_test_runs()
        ]
        aggregate = aggregate_regression_results(results)
        self.latest_regression_diff = aggregate.as_payload()
        return aggregate

    def note_post_edit_run_output(self, *, command: str, output: str, generation: int) -> None:
        """Record a bounded post-edit run output for expectation evidence linking.

        Only runs after a verification-relevant edit (generation > 0) are captured;
        each output is bounded and the buffer is capped, so evidence matching stays
        cheap and never balloons a long turn's state.
        """
        text = str(output or "")
        if not text:
            return
        self.post_edit_run_outputs.append(
            {
                "normalized_command": _normalize_shell_command_for_match(str(command or "")),
                "output": text[:8000],
                "generation": int(generation),
            }
        )
        self.post_edit_run_outputs[:] = self.post_edit_run_outputs[-30:]

    def current_expectation_evidence(
        self, expectations: list[Expectation]
    ) -> list[ExpectationEvidence]:
        """Link expected-output literals to post-edit runs at the current generation."""
        generation = self.verification_relevant_edit_generation
        runs = [
            run
            for run in self.post_edit_run_outputs
            if int(run.get("generation") or 0) >= generation
        ]
        evidence = match_expectation_evidence(expectations, runs)
        contract = self.acceptance_contract
        observer = contract.artifact_output_observer if contract is not None else None
        if observer is not None and observer.current_for(contract):
            changed = {normalize_locus_path(path) for path in observer.changed_loci}
            evidence.extend(
                ExpectationEvidence(
                    expectation_id=item.expectation_id,
                    normalized_command="",
                    generation=contract.generation,
                    source="host_file_metadata",
                )
                for item in expectations
                if item.kind == ExpectationKind.NAMED_LOCUS
                and normalize_locus_path(item.text) in changed
            )
        return evidence

    def note_repro_run(
        self,
        *,
        command: str,
        artifact_paths: tuple[str, ...],
        exit_code: int | None,
        passed: bool,
    ) -> None:
        """Record one observed execution of an agent-created artifact.

        The phase is resolved *at record time* against the paths the agent created
        this turn, so a run is pre-fix exactly when no pre-existing repo path has
        been modified yet. Writing the repro is itself a material edit, which is
        why the edit generation cannot decide this.
        """
        if not artifact_paths:
            return
        phase, product_paths = classify_repro_phase(
            touched_repo_paths=self.touched_repo_paths,
            created_paths=self.agent_created_paths,
        )
        self.repro_artifact_paths.update(artifact_paths)
        if len(self.repro_artifact_paths) > MAX_REPRO_ARTIFACTS:
            self.repro_artifact_paths = set(sorted(self.repro_artifact_paths)[:MAX_REPRO_ARTIFACTS])
        run = ReproRun(
            command=str(command or ""),
            artifact_paths=tuple(artifact_paths),
            phase=phase,
            passed=bool(passed),
            exit_code=exit_code,
            product_paths=product_paths,
        )
        self.repro_runs.append(run)
        self.repro_runs[:] = self.repro_runs[-MAX_REPRO_RUNS:]
        self.pending_repro_run_events.append(run.as_payload())

    def note_blast_radius_run(
        self,
        *,
        command: str,
        report: Any,
        duration_seconds: float | None = None,
        workspace_root: Path | None = None,
        working_directory: str | None = None,
        environment_known: bool = True,
    ) -> None:
        """Record one observed test run for the blast-radius diff (step 6).

        The phase is resolved *at record time* from the edits recorded so far, so a
        run counts as a clean-tree baseline exactly when no pre-existing repo path
        had been modified when it ran. A run made afterwards is never graced into a
        baseline: by then the agent may already have finished its fix, and crediting
        it would mask the very breakage this step exists to catch.
        """
        cleaned = str(command or "").strip()
        if not cleaned:
            return
        phase = classify_scope_phase(
            touched_repo_paths=self.touched_repo_paths,
            created_paths=self.agent_created_paths,
        )
        run = ScopeRun(
            command=cleaned,
            selectors=command_path_selectors(
                cleaned,
                workspace_root=workspace_root,
                working_directory=working_directory,
                environment_known=environment_known,
            ),
            phase=phase,
            report=report,
            duration_seconds=duration_seconds,
            agent_created_paths=tuple(sorted(self.agent_created_paths)),
            generation=self.verification_relevant_edit_generation,
        )
        self.blast_radius_runs.append(run)
        self.blast_radius_runs[:] = self.blast_radius_runs[-MAX_SCOPE_RUNS:]
        self.pending_blast_radius_events.append(run.as_payload())

    def has_blast_radius_baseline(self) -> bool:
        """True when a usable clean-tree run already covers the selected scope."""
        paths = self.blast_radius_scope.paths
        return bool(paths) and set(paths) <= set(self.blast_radius_baseline_covered_paths())

    def blast_radius_baseline_covered_paths(self) -> tuple[str, ...]:
        """Do not project an earlier selection onto tests created after that run."""
        return tuple(
            path
            for path in self.blast_radius_scope.paths
            if any(
                run.phase == ScopePhase.BASELINE
                and run.usable
                and run.covers((path,))
                and (path not in self.agent_created_paths or path in run.agent_created_paths)
                for run in self.blast_radius_runs
            )
        )

    def blast_radius_baseline_command(self) -> str:
        """Reuse an observed covering command without guessing the test framework."""
        return next(
            (
                run.command
                for run in reversed(self.blast_radius_runs)
                if run.phase == ScopePhase.BASELINE
                and run.usable
                and run.covers(self.blast_radius_scope.paths)
            ),
            "",
        )

    def compute_blast_radius_assessment(
        self, *, enabled: bool, turn_intent: str
    ) -> BlastRadiusAssessment:
        """Assess the blast-radius gate mechanically (step 6).

        With the feature disabled, on a non-execute turn, or with no scope selected
        (nothing edited yet, or no test surface near the change), returns the empty
        (non-applicable) assessment — the gate then behaves exactly as it did before
        this step.
        """
        applicable = bool(enabled and str(turn_intent or "") == "execute")
        assessment = assess_blast_radius(
            scope=self.blast_radius_scope,
            runs=self.blast_radius_runs,
            applicable=applicable,
            policy=self.blast_radius_policy,
            agent_created_paths=self.agent_created_paths,
            current_generation=self.verification_relevant_edit_generation,
        )
        self.latest_blast_radius_assessment = (
            assessment.as_payload() if assessment.applicable else {}
        )
        return assessment

    def note_repro_revision_round(self) -> None:
        self.repro_revision_rounds += 1

    def note_repro_artifact_edited_after_fix(self, paths: set[str] | tuple[str, ...]) -> None:
        for path in paths:
            cleaned = str(path or "").strip()
            if cleaned:
                self.repro_artifacts_edited_after_fix.add(cleaned)

    def repro_protocol_applicable(
        self, *, enabled: bool, turn_intent: str, engagement_based: bool = False
    ) -> bool:
        if not enabled or str(turn_intent or "") != "execute":
            return False
        if self.repro_task_shape == TaskShape.BUG_FIX:
            return True
        if not engagement_based:
            return False
        # Router-free path: no pre-turn task-shape prediction exists. The
        # protocol binds exactly when the agent demonstrably reproduced a
        # failure on the unpatched tree — from then on "the same repro must
        # pass after the fix" is enforceable without interpreting language.
        # Helper scripts that only ever passed never engage the gate.
        return any(run.phase is ReproPhase.PRE_FIX and not run.passed for run in self.repro_runs)

    def compute_repro_assessment(
        self, *, enabled: bool, turn_intent: str, engagement_based: bool = False
    ) -> ReproAssessment:
        """Assess the reproduction protocol mechanically (step 5).

        With the feature disabled, on a non-execute turn, or on a task that reports
        no symptom, returns the empty (non-applicable) assessment — the gate then
        behaves exactly as it did before this step.
        """
        applicable = self.repro_protocol_applicable(
            enabled=enabled,
            turn_intent=turn_intent,
            engagement_based=engagement_based,
        )
        if not applicable:
            self.latest_repro_assessment = {}
            return ReproAssessment()
        assessment = assess_reproduction(
            runs=self.repro_runs,
            applicable=True,
            artifact_paths=self.repro_artifact_paths,
            revision_rounds=self.repro_revision_rounds,
            edited_after_fix=sorted(self.repro_artifacts_edited_after_fix),
            surviving_artifacts=self.repro_surviving_artifacts,
        )
        self.latest_repro_assessment = assessment.as_payload()
        return assessment

    def compute_expectation_assessment(
        self, *, enabled: bool, turn_intent: str
    ) -> ExpectationAssessment:
        """Assess task expectations mechanically at the gate (turn-contract v2).

        Confirmed = an expected-output literal observed in a post-edit run, or a
        named locus that was edited, or an explicit recorded disposition; the rest
        are unaddressed. With the feature disabled, on a non-execute turn, or when
        the contract names no expectations, returns the empty assessment.
        """
        contract = self.acceptance_contract
        expectations = list(contract.expectations) if contract is not None else []
        if not enabled or str(turn_intent or "") != "execute" or not expectations:
            self.latest_expectation_assessment = {}
            self.latest_expectation_evidence = []
            return ExpectationAssessment()
        evidence = self.current_expectation_evidence(expectations)
        assessment = assess_expectations(
            expectations=expectations,
            evidence=evidence,
            edited_loci=(
                self.touched_repo_paths
                | self.touched_external_paths
                | set(contract.external_path_observations)
            ),
            dispositions=self.recorded_expectation_dispositions,
        )
        self.latest_expectation_assessment = assessment.as_payload()
        self.latest_expectation_evidence = [item.as_payload() for item in evidence]
        return assessment

    def missing_verification_commands(self) -> set[str]:
        return self.expected_verification_commands - self.covered_verification_commands

    def failed_verification_commands(self) -> set[str]:
        return set(self.failed_verification_command_snippets) & self.expected_verification_commands

    def first_failed_verification_snippet(self) -> str:
        for command in sorted(self.failed_verification_commands()):
            snippet = self.failed_verification_command_snippets.get(command, "")
            if snippet:
                return snippet
        return ""

    def verification_coverage_is_stale(self) -> bool:
        return (
            bool(self.expected_verification_commands)
            and self.last_successful_verification_generation is not None
            and self.last_successful_verification_generation
            < self.verification_relevant_edit_generation
        )

    def repair_attempts_for_stage(self, stage: str) -> int:
        if stage == "no_material_edits":
            return self.completion_gate_no_material_edits_repair_attempts
        if stage == "verification_not_attempted":
            return self.completion_gate_missing_verify_repair_attempts
        if stage == "verification_incomplete":
            return self.completion_gate_missing_verify_repair_attempts
        if stage == "verification_failed":
            return self.completion_gate_failed_verify_repair_attempts
        if stage == "regressions_detected":
            return self.completion_gate_regression_repair_attempts
        if stage == "unattributed_failures":
            return self.completion_gate_unattributed_repair_attempts
        if stage == "expectations_unaddressed":
            return self.completion_gate_expectations_repair_attempts
        if stage == "repro_unconfirmed":
            return self.completion_gate_repro_repair_attempts
        # Both blast-radius stages share one repair budget: they are two faces of the
        # same protocol, and a run that alternates between them must not get double
        # the rounds.
        if stage in {"blast_radius_regressions", "blast_radius_unverified"}:
            return self.completion_gate_blast_radius_repair_attempts
        return self.completion_gate_repair_attempts

    def increment_repair_attempts_for_stage(self, stage: str) -> None:
        self.completion_gate_repair_attempts += 1
        if stage == "no_material_edits":
            self.completion_gate_no_material_edits_repair_attempts += 1
        elif stage == "verification_not_attempted":
            self.completion_gate_missing_verify_repair_attempts += 1
        elif stage == "verification_incomplete":
            self.completion_gate_missing_verify_repair_attempts += 1
        elif stage == "verification_failed":
            self.completion_gate_failed_verify_repair_attempts += 1
        elif stage == "regressions_detected":
            self.completion_gate_regression_repair_attempts += 1
        elif stage == "unattributed_failures":
            self.completion_gate_unattributed_repair_attempts += 1
        elif stage == "expectations_unaddressed":
            self.completion_gate_expectations_repair_attempts += 1
        elif stage == "repro_unconfirmed":
            self.completion_gate_repro_repair_attempts += 1
        elif stage in {"blast_radius_regressions", "blast_radius_unverified"}:
            self.completion_gate_blast_radius_repair_attempts += 1

    def as_payload(self) -> dict[str, Any]:
        return {
            "execution_requested": self.execution_requested,
            "verification_expected": self.verification_expected,
            "connector_link_evidence": dict(self.connector_link_evidence),
            "expected_verification_commands": sorted(self.expected_verification_commands),
            "covered_verification_commands": sorted(self.covered_verification_commands),
            "missing_verification_commands": sorted(self.missing_verification_commands()),
            "material_edit_count": self.material_edit_count,
            "material_edit_generation": self.material_edit_generation,
            "material_edit_tools": sorted(self.material_edit_tools),
            "touched_repo_paths": sorted(self.touched_repo_paths),
            "touched_external_paths": sorted(self.touched_external_paths),
            "last_diff_review_generation": self.last_diff_review_generation,
            "diff_review_stale": self.diff_review_is_stale(),
            "verification_attempt_count": self.verification_attempt_count,
            "artifact_checks": list(self.artifact_check_observations),
            "verification_tools": sorted(self.verification_tools),
            "last_verification_passed": self.last_verification_passed,
            "last_verification_failure_snippet": self.last_verification_failure_snippet,
            "last_verification_failure_category": self.last_verification_failure_category,
            "unresolved_observed_verification_failures": len(self.observed_verification_failures),
            "failed_verification_commands": sorted(self.failed_verification_commands()),
            "verification_relevant_edit_generation": self.verification_relevant_edit_generation,
            "last_successful_verification_generation": self.last_successful_verification_generation,
            "verification_coverage_stale": self.verification_coverage_is_stale(),
            "verification_evidence_counts": dict(sorted(self.verification_evidence_counts.items())),
            "latest_verification_evidence_category": (self.latest_verification_evidence_category),
            "latest_verification_evidence_reason": self.latest_verification_evidence_reason,
            "accepted_verification_evidence": list(self.accepted_verification_evidence),
            "supplemental_verification_evidence": list(self.supplemental_verification_evidence),
            "rejected_verification_evidence": list(self.rejected_verification_evidence),
            "executed_verification_evidence": list(self.executed_verification_evidence),
            "verification_evidence_generation": self.verification_evidence_generation,
            "last_post_edit_execution_generation": self.last_post_edit_execution_generation,
            "post_edit_execution_evidence_present": self.has_post_edit_execution_evidence(),
            "completion_gate_repair_attempts": self.completion_gate_repair_attempts,
            "completion_gate_no_material_edits_repair_attempts": self.completion_gate_no_material_edits_repair_attempts,
            "completion_gate_missing_verify_repair_attempts": self.completion_gate_missing_verify_repair_attempts,
            "completion_gate_failed_verify_repair_attempts": self.completion_gate_failed_verify_repair_attempts,
            "completion_gate_regression_repair_attempts": self.completion_gate_regression_repair_attempts,
            "completion_gate_unattributed_repair_attempts": self.completion_gate_unattributed_repair_attempts,
            "completion_gate_expectations_repair_attempts": self.completion_gate_expectations_repair_attempts,
            "completion_gate_repro_repair_attempts": self.completion_gate_repro_repair_attempts,
            "test_baselines": {
                key: record.as_payload() for key, record in sorted(self.test_baselines.items())
            },
            "post_edit_test_runs": [run.as_payload() for run in self.post_edit_test_runs],
            "agent_created_paths": sorted(self.agent_created_paths),
            "last_verification_attempt_was_test_run": (self.last_verification_attempt_was_test_run),
            "regression_diff": dict(self.latest_regression_diff),
            "expectation_assessment": dict(self.latest_expectation_assessment),
            "expectation_evidence": list(self.latest_expectation_evidence),
            "repro_task_shape": self.repro_task_shape.value,
            "repro_runs": [run.as_payload() for run in self.repro_runs],
            "repro_artifact_paths": sorted(self.repro_artifact_paths),
            "repro_revision_rounds": self.repro_revision_rounds,
            "repro_artifacts_edited_after_fix": sorted(self.repro_artifacts_edited_after_fix),
            "repro_surviving_artifacts": list(self.repro_surviving_artifacts),
            "repro_assessment": dict(self.latest_repro_assessment),
            "completion_gate_blast_radius_repair_attempts": (
                self.completion_gate_blast_radius_repair_attempts
            ),
            "blast_radius_scope": self.blast_radius_scope.as_payload(),
            "blast_radius_runs": [run.as_payload() for run in self.blast_radius_runs],
            "blast_radius_assessment": dict(self.latest_blast_radius_assessment),
            "advisory_completion": (
                self.recorded_advisory_completion.as_payload()
                if self.recorded_advisory_completion is not None
                else None
            ),
            "completion_gate_controller": self.completion_gate_controller_state.as_payload(),
            "completion_gate_last_decision_kind": self.completion_gate_controller_state.last_decision_kind,
            "completion_certificate": dict(self.latest_completion_certificate),
            **acceptance_contract_problem_payload(self.acceptance_contract),
        }

    def acceptance_problem_names(self) -> list[str]:
        if self.acceptance_contract is None:
            return []
        return self.acceptance_contract.problem_names()

    def acceptance_requires_execution(self) -> bool:
        if self.acceptance_contract is None:
            return False
        return any(
            criterion.required_for_finalization
            and criterion.required
            and (
                criterion.commands
                or criterion.thresholds
                or criterion.ports
                or criterion.kind.value
                in {
                    "explicit_command_io",
                    "functional_api_protocol",
                    "persistent_service",
                    "explicit_host_user_verification_command",
                }
            )
            for criterion in self.acceptance_contract.criteria
        )


def _successful_verification_claim_kind(final_text: str) -> str | None:
    text = str(final_text or "")
    for kind, pattern in (
        ("tests", _TEST_SUCCESS_CLAIM_RE),
        ("verification", _GENERIC_VERIFICATION_SUCCESS_CLAIM_RE),
    ):
        for match in pattern.finditer(text):
            prefix = text[max(0, match.start() - 48) : match.start()]
            if _NEGATED_CLAIM_PREFIX_RE.search(prefix):
                continue
            return kind
    return None


def _fresh_executed_evidence_for_claim(
    state: TurnExecutionState,
    *,
    claim_kind: str,
) -> list[dict[str, Any]]:
    required_generation = state.verification_relevant_edit_generation
    evidence: list[dict[str, Any]] = []
    for raw_item in state.executed_verification_evidence:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        try:
            generation = int(item.get("generation"))
        except (TypeError, ValueError):
            continue
        if generation < required_generation:
            continue
        if item.get("real_execution") is not True:
            continue
        if item.get("reason") == "mutated_material_paths":
            continue
        if item.get("observed_exit_code") != 0 or item.get("observed_output") is not True:
            continue
        command = str(item.get("normalized_command") or "").strip()
        if not command:
            continue
        if claim_kind == "tests":
            analysis = analyze_verification_command(command, trusted=True)
            family = str(analysis.command_family or "").casefold()
            if "test" not in family and _TEST_EXECUTION_COMMAND_RE.search(command) is None:
                continue
        evidence.append(item)
    return evidence


def _runtime_message_locale(
    *,
    language: str = "",
    explicit_language_override: bool = False,
) -> str:
    if not explicit_language_override:
        return _RUNTIME_DEFAULT_LANGUAGE
    normalized = normalize_language_name(language).casefold()
    if normalized in _RUNTIME_MESSAGE_CATALOG:
        return normalized
    return _RUNTIME_DEFAULT_LANGUAGE


def _runtime_message(
    key: str,
    *,
    language: str = "",
    explicit_language_override: bool = False,
    **kwargs: Any,
) -> str:
    locale = _runtime_message_locale(
        language=language,
        explicit_language_override=explicit_language_override,
    )
    template = _RUNTIME_MESSAGE_CATALOG.get(locale, {}).get(key)
    if template is None:
        template = _RUNTIME_MESSAGE_CATALOG[_RUNTIME_DEFAULT_LANGUAGE].get(key, key)
    try:
        return template.format(**kwargs)
    except Exception:  # noqa: BLE001
        return template


def _extract_scoped_external_edits(
    *, root: Path, tool_name: str, arguments: dict[str, Any], result: dict[str, Any]
) -> dict[str, str]:
    fields = {
        "fs_write": ("path",),
        "fs_edit": ("path",),
        "fs_delete": ("path",),
        "fs_mkdir": ("path",),
        "fs_move": ("source_path", "destination_path"),
        "fs_copy": ("destination_path",),
    }.get(tool_name, ())
    scoped = result.get("scoped_workspace")
    if not fields:
        return {}
    project = root.resolve()
    scopes = list(allowed_roots(project)[1:])
    if isinstance(scoped, str) and Path(scoped).is_absolute():
        scopes.append(Path(scoped).resolve())
    scopes = [scope for scope in scopes if not scope.is_relative_to(project)]
    if not scopes:
        return {}
    edited: dict[str, str] = {}
    for key in fields:
        value, proposed = result.get(key), arguments.get(key)
        if not isinstance(value, str) or not isinstance(proposed, str):
            continue
        path = Path(value)
        if (
            path.is_absolute()
            and any(path.resolve().is_relative_to(scope) for scope in scopes)
            and path.resolve() == (project / proposed).resolve()
        ):
            # Preserve the exact requested alias as well as its observed target.
            # This is identity evidence, not another filesystem permission.
            edited[str(project / proposed)] = str(path.resolve())
    return edited


def _extract_touched_repo_paths(
    *,
    root: Path,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> set[str]:
    normalized_tool = tool_name.strip().lower()
    raw_paths: list[str] = []

    if normalized_tool in {"fs_write", "fs_edit", "fs_delete", "fs_mkdir"}:
        raw_path = result.get("path", arguments.get("path"))
        if isinstance(raw_path, str):
            raw_paths.append(raw_path)
    elif normalized_tool == "document_operation" and result.get("status") in {
        "created",
        "edited",
        "recalculated",
    }:
        raw_path = result.get("output", result.get("pdf"))
        if isinstance(raw_path, str):
            raw_paths.append(raw_path)
    elif normalized_tool in {"fs_move", "fs_copy"}:
        for key in ("source_path", "destination_path"):
            raw_path = result.get(key, arguments.get(key))
            if isinstance(raw_path, str):
                raw_paths.append(raw_path)
    elif normalized_tool == "git_apply_patch":
        patch = str(arguments.get("patch") or "")
        raw_paths.extend(iter_patch_paths(patch))
    elif normalized_tool in {"subagent_run", "subagent_wait"}:
        touched_paths = result.get(
            "material_touched_repo_paths",
            result.get("touched_repo_paths"),
        )
        if isinstance(touched_paths, list):
            raw_paths.extend(str(item) for item in touched_paths if isinstance(item, str))
    elif normalized_tool == "subagent_apply":
        if result.get("ok") is True and result.get("semantic_no_progress") is not True:
            applied_paths = result.get("applied_paths")
            if isinstance(applied_paths, list):
                raw_paths.extend(str(item) for item in applied_paths if isinstance(item, str))
    elif normalized_tool in _COMMAND_LIKE_MUTATION_TOOL_NAMES:
        touched_paths = result.get("touched_repo_paths")
        if isinstance(touched_paths, list):
            raw_paths.extend(str(item) for item in touched_paths if isinstance(item, str))

    touched: set[str] = set()
    for raw_path in raw_paths:
        normalized = _normalize_repo_relative_hint_path(root=root, raw=raw_path)
        if normalized:
            touched.add(normalized)
    if normalized_tool in _COMMAND_LIKE_MUTATION_TOOL_NAMES:
        return set(material_mutation_paths(touched, root=root))
    return touched


def _verification_attempt_passed(
    *,
    tool_name: str,
    status: str,
    result: dict[str, Any],
) -> bool:
    if status == "failed":
        return False
    normalized_tool = tool_name.strip().lower()
    touched_repo_paths = result.get("material_touched_repo_paths", result.get("touched_repo_paths"))
    normalized_touched = (
        {str(item) for item in touched_repo_paths if isinstance(item, str) and str(item).strip()}
        if isinstance(touched_repo_paths, list)
        else set()
    )
    if normalized_tool == "verify_run":
        if normalized_touched and _paths_require_verification(normalized_touched):
            return False
        all_passed = result.get("all_passed")
        if isinstance(all_passed, bool):
            return all_passed
        command_results = result.get("command_results")
        if isinstance(command_results, list):
            checks: list[bool] = []
            for item in command_results:
                if not isinstance(item, dict):
                    checks.append(False)
                    continue
                real_execution = item.get("real_execution")
                if real_execution is not True:
                    checks.append(False)
                    continue
                ok = item.get("ok")
                if isinstance(ok, bool):
                    checks.append(ok)
                    continue
                exit_code = item.get("exit_code")
                checks.append(isinstance(exit_code, int) and exit_code == 0)
            return bool(checks) and all(checks)
        return False
    if normalized_tool == "shell_run":
        exit_code = result.get("exit_code")
        if not (isinstance(exit_code, int) and exit_code == 0):
            return False
        if normalized_touched and _paths_require_verification(normalized_touched):
            return False
        output = "\n".join(
            [
                str(result.get("stdout") or "").strip(),
                str(result.get("stderr") or "").strip(),
            ]
        ).strip()
        assessment = assess_verification_command_execution(
            command=str(result.get("effective_cmd") or result.get("cmd") or ""),
            exit_code=exit_code,
            output=output,
        )
        return assessment.real_execution is True
    return False


def _verification_relevant_material_paths(
    paths: set[str], *, root: Path | None = None, contract: AcceptanceContract | None = None
) -> set[str]:
    if not paths or not (
        _paths_require_verification(paths)
        or (
            root is not None
            and _acceptance_artifact_changed(root=root, contract=contract, touched_paths=paths)
        )
    ):
        return set()
    return set(paths)


def _verification_command_result_is_benign_skip(item: dict[str, Any]) -> bool:
    return (
        item.get("status") == "skipped"
        and item.get("ok") is True
        and is_benign_non_execution_reason(str(item.get("non_execution_reason") or ""))
    )


def _verification_command_result_passed(item: dict[str, Any]) -> bool:
    if _verification_command_result_is_benign_skip(item):
        return True
    real_execution = item.get("real_execution")
    if real_execution is not True:
        return False
    ok = item.get("ok")
    if isinstance(ok, bool):
        return ok
    exit_code = item.get("exit_code")
    return isinstance(exit_code, int) and exit_code == 0


def _verification_command_result_snippet(item: dict[str, Any]) -> str:
    parts = [
        str(item.get("output_preview") or "").strip(),
        str(item.get("output") or "").strip(),
        str(item.get("stderr") or "").strip(),
        str(item.get("stdout") or "").strip(),
    ]
    text = "\n".join(part for part in parts if part)
    snippet = extract_actionable_failure_snippet(text)
    return snippet or (text[:240].rstrip() if text else "")


def _verification_failure_category_for_tool_result(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> str:
    normalized_tool = tool_name.strip().lower()
    if normalized_tool == "verify_run":
        category = str(result.get("failure_category") or "").strip()
        return category or FailureCategory.VERIFICATION_FAILED.value

    if normalized_tool == "shell_run":
        output = "\n".join(
            [
                str(result.get("stdout") or "").strip(),
                str(result.get("stderr") or "").strip(),
            ]
        ).strip()
        command = str(
            result.get("effective_cmd") or result.get("cmd") or arguments.get("cmd") or ""
        )
        exit_code_raw = result.get("exit_code")
        exit_code = exit_code_raw if isinstance(exit_code_raw, int) else 1
        assessment = assess_verification_command_execution(
            command=command,
            exit_code=exit_code,
            output=output,
        )
        if (
            assessment.non_execution_reason == "execution_layer_failure"
            or is_infra_unavailable_error(output)
            or is_toolchain_unavailable_verification_output(output)
        ):
            return FailureCategory.INFRA_UNAVAILABLE.value

    return FailureCategory.VERIFICATION_FAILED.value


def _record_verify_run_command_outcomes(
    *,
    state: TurnExecutionState,
    result: dict[str, Any],
    known_verification_commands: list[str] | None,
    evidence_records: list[VerificationEvidence],
) -> None:
    allowed_coverage = {
        command
        for record in evidence_records
        if record.allowed_to_satisfy_contract
        for command in record.covered_verification_commands
    }
    command_results = result.get("command_results")
    if not isinstance(command_results, list):
        if result.get("all_passed") is True:
            state.record_verification_coverage(allowed_coverage)
        return

    covered: set[str] = set()
    failures: dict[str, str] = {}
    for item in command_results:
        if not isinstance(item, dict):
            continue
        matches: set[str] = set()
        observed_candidates = [
            str(item.get("command") or ""),
            str(item.get("effective_command") or ""),
        ]
        for observed in observed_candidates:
            if not observed:
                continue
            matches.update(
                _matching_effective_verification_commands(
                    observed_command=observed,
                    effective_verification_commands=known_verification_commands,
                )
            )
        if not matches:
            continue
        if _verification_command_result_passed(item):
            covered.update(matches.intersection(allowed_coverage))
            continue
        snippet = _verification_command_result_snippet(item)
        for command in matches:
            failures[command] = f"{command}: {snippet}" if snippet else command

    state.record_verification_coverage(covered)
    state.record_verification_failures(failures)


def _record_shell_verification_command_outcome(
    *,
    root: Path,
    state: TurnExecutionState,
    arguments: dict[str, Any],
    result: dict[str, Any],
    known_verification_commands: list[str] | None,
    passed: bool,
    evidence: VerificationEvidence | None = None,
) -> None:
    # The same command text in another folder is a different check. Its actual
    # outcome is recorded separately, keyed by command and working directory.
    if evidence is not None and evidence.working_directory != str(root.resolve()):
        return
    allowed_matches = (
        set(evidence.covered_verification_commands)
        if evidence is not None and evidence.allowed_to_satisfy_contract
        else set()
    )
    if passed:
        state.record_verification_coverage(allowed_matches)
        return
    matches = allowed_matches or _matching_effective_verification_commands(
        observed_command=str(result.get("effective_cmd") or arguments.get("cmd") or ""),
        effective_verification_commands=known_verification_commands,
    )
    if not matches:
        return
    output = "\n".join(
        [
            str(result.get("stdout") or "").strip(),
            str(result.get("stderr") or "").strip(),
        ]
    ).strip()
    snippet = extract_actionable_failure_snippet(output) or output[:240].rstrip()
    state.record_verification_failures(
        {command: f"{command}: {snippet}" if snippet else command for command in matches}
    )


def _verification_output_text(result: dict[str, Any]) -> str:
    return "\n".join(
        [
            str(result.get("stdout") or "").strip(),
            str(result.get("stderr") or "").strip(),
            str(result.get("output") or "").strip(),
            str(result.get("output_preview") or "").strip(),
        ]
    ).strip()


def _aggregate_verification_evidence(
    records: list[VerificationEvidence],
    *,
    fallback_command: str = "",
) -> VerificationEvidence:
    if not records:
        return VerificationEvidence(
            category=VerificationEvidenceCategory.NOT_VERIFICATION,
            normalized_command=fallback_command,
            reason="no_verification_evidence",
        )
    priority = {
        VerificationEvidenceCategory.AUTHORITATIVE: 0,
        VerificationEvidenceCategory.REPO_NATIVE: 1,
        VerificationEvidenceCategory.TASK_ACCEPTANCE: 2,
        VerificationEvidenceCategory.NOT_VERIFICATION: 3,
    }
    primary = sorted(records, key=lambda item: priority[item.category])[0]
    covered = sorted(
        {command for item in records for command in item.covered_verification_commands if command}
    )
    allowed = bool(records) and all(
        item.allowed_to_satisfy_contract
        for item in records
        if item.category != VerificationEvidenceCategory.NOT_VERIFICATION
    )
    if any(item.category == VerificationEvidenceCategory.NOT_VERIFICATION for item in records):
        allowed = False
    return VerificationEvidence(
        category=primary.category,
        normalized_command=primary.normalized_command,
        matched_command=primary.matched_command,
        real_execution=primary.real_execution,
        allowed_to_satisfy_contract=allowed,
        reason=primary.reason
        if allowed
        else next(
            (item.reason for item in records if not item.allowed_to_satisfy_contract),
            primary.reason,
        ),
        covered_verification_commands=tuple(covered),
        supplemental_only=all(item.supplemental_only for item in records),
    )


def _verification_evidence_note(
    evidence: VerificationEvidence,
    *,
    result: dict[str, Any] | None = None,
) -> str:
    if evidence.category == VerificationEvidenceCategory.NOT_VERIFICATION:
        return ""
    if evidence.supplemental_only:
        if evidence.reason == "supplemental_only_contract_selection_differs":
            return (
                "Additional verification execution; its selection does not establish "
                "coverage of the resolved verification command."
            )
        return (
            "Additional task-check execution; it does not establish coverage "
            "of the resolved verification command."
        )
    result_payload = result if isinstance(result, dict) else {}
    command_specs = result_payload.get("verification_command_specs")
    # Specs describe the resolved selection, which may include checks absent
    # from this result. Use only identities already matched by the classifier;
    # neither contract type nor coverage establishes check authorship.
    matched_commands = set(evidence.covered_verification_commands)
    if evidence.matched_command:
        matched_commands.add(evidence.matched_command)
    provenance_by_command: dict[str, set[str]] = {}
    if isinstance(command_specs, list):
        for item in command_specs:
            if not isinstance(item, dict):
                continue
            command = item.get("original_text")
            if not isinstance(command, str) or command not in matched_commands:
                continue
            try:
                provenance = VerificationCommandProvenance(item.get("provenance"))
            except (ValueError, TypeError):
                continue
            provenance_by_command.setdefault(command, set()).add(provenance.value)
    if matched_commands and matched_commands == provenance_by_command.keys():
        origins = sorted({origin for values in provenance_by_command.values() for origin in values})
        return "Matched command provenance: " + ", ".join(origins) + "."
    return "Verification command provenance is unconfirmed."


@dataclass(frozen=True)
class _ObservedVerificationEvidence:
    """A classified check bound to the result occurrence that produced it."""

    evidence: VerificationEvidence
    observed_exit_code: int | None
    observed_output: bool
    command_result_index: int | None = None


def _verification_outcome_key(
    *,
    root: Path,
    command: str,
    working_directory: object,
    environment_known: bool,
    execution_context: object = None,
    execution_scope: object = None,
) -> str | None:
    """Identify the actual execution, without equating different selections.

    Keep argv spelling/assignments intact and never export environment values.
    A native runner can supply a digest of its admitted environment and policy.
    Unknown/custom environments and opaque shell wrappers remain incomparable.
    """
    native_context = (
        execution_context
        if isinstance(execution_context, str)
        and re.fullmatch(r"windows-mxc-v1:[0-9a-f]{64}", execution_context)
        else None
    )
    scope_known = isinstance(execution_scope, str) and re.fullmatch(
        r"[0-9a-f]{64}", execution_scope
    )
    if (
        (not environment_known and native_context is None and not scope_known)
        or not command.strip()
        or not isinstance(working_directory, str)
    ):
        return None
    analysis = analyze_verification_command(command, trusted=True, workspace_root=root)
    if (
        analysis.rejection_reason
        or analysis.shell_control_flow not in {"none", "safe_cd_and"}
        or analysis.unwrapped_command != analysis.normalized_command
    ):
        return None
    try:
        cwd = (root / working_directory).resolve()
        if analysis.cd_target is not None:
            cwd = (cwd / analysis.cd_target).resolve()
        context = (
            command.strip(),
            str(cwd),
            native_context,
            execution_scope if scope_known else None,
            sorted(os.environ.items())
            if environment_known and native_context is None and not scope_known
            else None,
        )
        return hashlib.sha256(json.dumps(context, ensure_ascii=True).encode()).hexdigest()
    except (OSError, RuntimeError, ValueError):
        return None


def _record_observed_verification_outcomes(
    *,
    root: Path,
    state: TurnExecutionState,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
    observations: list[_ObservedVerificationEvidence],
    attempt_passed: bool,
    environment_known: bool,
) -> bool:
    """Retire only a failed execution that a later comparable pass supersedes."""
    resolved = False
    failed_in_this_attempt: set[str] = set()
    command_results = result.get("command_results")
    for index, observation in enumerate(observations):
        evidence = observation.evidence
        item = result
        bound_to_execution = evidence.category != VerificationEvidenceCategory.NOT_VERIFICATION
        if tool_name == "verify_run":
            result_index = observation.command_result_index
            if (
                result_index is None
                or not isinstance(command_results, list)
                or not isinstance(command_results[result_index], dict)
            ):
                # A legacy aggregate cannot bind a result to one execution.
                bound_to_execution = False
            else:
                item = command_results[result_index]
        command = str(
            item.get("effective_command")
            or item.get("effective_cmd")
            or item.get("command")
            or item.get("cmd")
            or arguments.get("cmd")
            or ""
        )
        cwd = item.get(
            "verification_working_directory",
            item.get(
                "cwd",
                result.get(
                    "verification_working_directory", result.get("cwd", arguments.get("cwd", "."))
                ),
            ),
        )
        key = (
            _verification_outcome_key(
                root=root,
                command=command,
                working_directory=cwd,
                environment_known=environment_known,
                execution_context=item.get("execution_context"),
                execution_scope=item.get("verification_execution_scope"),
            )
            if bound_to_execution
            else None
        )
        code = observation.observed_exit_code
        if (
            attempt_passed
            and code == 0
            and evidence.real_execution is True
            and observation.observed_output
        ):
            if (
                key is not None
                and key not in failed_in_this_attempt
                and key in state.observed_verification_failures
            ):
                del state.observed_verification_failures[key]
                resolved = True
        elif (
            code is not None
            and code != 0
            and not state.expected_verification_commands.intersection(
                evidence.covered_verification_commands
            )
        ):
            # Required-command failures already have their own reconciliation
            # map, including the verifier's sandbox/legacy result contracts.
            # A matched recommendation is not necessarily required, so its
            # observed failure still needs exact execution/context tracking.
            failure_key = key or f"unknown:{state.verification_attempt_count}:{index}"
            failed_in_this_attempt.add(failure_key)
            # Reinsert so the last outstanding failure supplies the diagnostic.
            state.observed_verification_failures.pop(failure_key, None)
            category_result = item
            if (
                tool_name == "verify_run"
                and bound_to_execution
                and isinstance(command_results, list)
                and len(command_results) == 1
                and not item.get("failure_category")
            ):
                # A single execution can inherit its aggregate diagnosis. A
                # batch diagnosis cannot identify which command caused it.
                category_result = {**item, "failure_category": result.get("failure_category")}
            provenance = ""
            requirement = ""
            specs = result.get("verification_command_specs")
            matching_specs = [
                spec
                for spec in (specs if isinstance(specs, list) else [])
                if isinstance(spec, dict)
                and str(spec.get("original_text") or "").strip() == command.strip()
                and spec.get("working_directory", ".") == cwd
            ]
            if len(matching_specs) == 1:
                try:
                    provenance = VerificationCommandProvenance(
                        matching_specs[0]["provenance"]
                    ).value
                    requirement = VerificationCommandRequirement(
                        matching_specs[0]["requirement"]
                    ).value
                except (KeyError, ValueError, TypeError):
                    provenance = ""
                    requirement = ""
            state.observed_verification_failures[failure_key] = _ObservedVerificationFailure(
                snippet=_verification_command_result_snippet(item),
                category=_verification_failure_category_for_tool_result(
                    tool_name=tool_name, arguments=arguments, result=item
                ),
                # Aggregate diagnoses enrich reporting only. The existing
                # per-command category also controls explicit blocker admission.
                reported_category=_verification_failure_category_for_tool_result(
                    tool_name=tool_name, arguments=arguments, result=category_result
                ),
                was_test_run=command_is_test_runner(command),
                command=command,
                generation=state.verification_relevant_edit_generation,
                provenance=provenance,
                requirement=requirement,
                host_launcher_unavailable=(
                    item.get("host_launcher_unavailable") is True
                    and evidence.real_execution is False
                    and code == 127
                ),
            )
    # No execution/exit observation is not a failed execution. It also cannot
    # retire an older failure or establish a passing attempt.
    return resolved


def _verification_evidence_in_workspace(
    evidence: VerificationEvidence,
    *,
    root: Path,
    cwd: Any,
) -> VerificationEvidence:
    """A command's targets refer to its cwd, not merely its command text.

    Configured checks are relative to the workspace root. A check launched in
    a different directory can be useful evidence, but cannot claim that check's
    coverage without an explicit equivalent command rooted there.
    """

    try:
        actual = Path(cwd or root)
        if not actual.is_absolute():
            actual = root / actual
        actual = actual.resolve()
    except (OSError, TypeError, ValueError):
        return replace(
            evidence,
            allowed_to_satisfy_contract=False,
            covered_verification_commands=(),
            reason="verification_cwd_invalid",
        )
    if actual == root.resolve():
        return replace(evidence, working_directory=str(actual))
    return replace(
        evidence,
        allowed_to_satisfy_contract=False,
        covered_verification_commands=(),
        matched_command=None,
        supplemental_only=True,
        working_directory=str(actual),
        reason=(
            "supplemental_working_directory"
            if evidence.category != VerificationEvidenceCategory.NOT_VERIFICATION
            and evidence.real_execution is True
            and evidence.reason
            not in {"mutated_material_paths", "external_mutation_tracking_unavailable"}
            else evidence.reason
        ),
    )


def _classify_acceptance_aware_verification_evidence(
    command: str,
    *,
    acceptance_contract: AcceptanceContract | None,
    known_verification_commands: list[str] | None,
    authoritative: bool,
    **kwargs: Any,
) -> VerificationEvidence:
    """Accepted user checks coexist with configured checks without replacing them."""
    explicit_commands = (
        [
            command
            for criterion in acceptance_contract.criteria
            if criterion.kind == AcceptanceCriterionKind.EXPLICIT_COMMAND_IO
            for command in criterion.commands
        ]
        if acceptance_contract is not None
        else []
    )
    explicit_only = bool(
        _matching_effective_verification_commands(
            observed_command=command,
            effective_verification_commands=explicit_commands,
        )
    ) and not _matching_effective_verification_commands(
        observed_command=command,
        effective_verification_commands=known_verification_commands,
    )
    evidence = classify_verification_evidence(
        command,
        known_verification_commands=(
            [*(known_verification_commands or []), *explicit_commands]
            if explicit_only
            else known_verification_commands
        ),
        authoritative=authoritative and not explicit_only,
        **kwargs,
    )
    if explicit_only and evidence.category != VerificationEvidenceCategory.NOT_VERIFICATION:
        evidence = replace(evidence, category=VerificationEvidenceCategory.TASK_ACCEPTANCE)
    if acceptance_contract is not None and not acceptance_contract.baseline_available:
        evidence = replace(
            evidence,
            allowed_to_satisfy_contract=False,
            covered_verification_commands=(),
            supplemental_only=True,
            reason="acceptance_baseline_unavailable",
        )
    return evidence


def _verify_run_evidence_observations(
    *,
    result: dict[str, Any],
    known_verification_commands: list[str] | None,
    required_verification_commands: set[str],
    verification_authoritative: bool,
    material_touched_paths: set[str],
    root: Path,
    evidence_v2: bool = True,
    acceptance_contract: AcceptanceContract | None = None,
) -> list[_ObservedVerificationEvidence]:
    command_results = result.get("command_results")
    verification_relevant_touched_paths = _verification_relevant_material_paths(
        material_touched_paths, root=root, contract=acceptance_contract
    )
    records: list[_ObservedVerificationEvidence] = []
    if isinstance(command_results, list):
        for index, item in enumerate(command_results):
            if not isinstance(item, dict):
                continue
            command = str(item.get("command") or item.get("effective_command") or "")
            if not command:
                continue
            exit_code_raw = item.get("exit_code")
            exit_code = exit_code_raw if isinstance(exit_code_raw, int) else None
            record = _classify_acceptance_aware_verification_evidence(
                command,
                acceptance_contract=acceptance_contract,
                known_verification_commands=known_verification_commands,
                required_verification_commands=required_verification_commands,
                authoritative=verification_authoritative,
                material_touched_paths=verification_relevant_touched_paths,
                exit_code=exit_code,
                output=_verification_output_text(item),
                real_execution=(
                    item.get("real_execution")
                    if isinstance(item.get("real_execution"), bool)
                    or item.get("real_execution") is None
                    else None
                ),
                root=root,
                evidence_v2=evidence_v2,
            )
            # A benign skip is useful lifecycle information, but cannot prove
            # that a required suite executed. Keep the classifier's rejection
            # for zero tests and other non-execution results.
            record = _verification_evidence_in_workspace(
                record, root=root, cwd=item.get("cwd", result.get("cwd"))
            )
            # The batch aggregate may be rejected while an independent sibling
            # check is valid. Acceptance consumes each command's own decision.
            item["verification_evidence_allowed"] = record.allowed_to_satisfy_contract
            item["verification_evidence_category"] = record.category.value
            item["verification_evidence_reason"] = record.reason
            item["verification_evidence_covered_commands"] = list(
                record.covered_verification_commands
            )
            observed_exit_code, observed_output = _verification_evidence_observation(
                tool_name="verify_run", result={"command_results": [item]}
            )
            records.append(
                _ObservedVerificationEvidence(
                    evidence=record,
                    observed_exit_code=observed_exit_code,
                    observed_output=observed_output,
                    command_result_index=index,
                )
            )
        return records

    commands = result.get("commands")
    if isinstance(commands, list):
        run_status = str(result.get("status") or "")
        if run_status:
            # Tri-state: not_run maps to "no observation", never to failure.
            exit_code = 0 if run_status == "passed" else 1 if run_status == "failed" else None
        else:
            all_passed = result.get("all_passed")
            exit_code = 0 if all_passed is True else 1 if all_passed is False else None
        observed_exit_code, observed_output = _verification_evidence_observation(
            tool_name="verify_run", result=result
        )
        for command in commands:
            records.append(
                _ObservedVerificationEvidence(
                    evidence=_classify_acceptance_aware_verification_evidence(
                        str(command),
                        acceptance_contract=acceptance_contract,
                        known_verification_commands=known_verification_commands,
                        required_verification_commands=required_verification_commands,
                        authoritative=verification_authoritative,
                        material_touched_paths=verification_relevant_touched_paths,
                        exit_code=exit_code,
                        output=_verification_output_text(result),
                        root=root,
                        evidence_v2=evidence_v2,
                    ),
                    observed_exit_code=observed_exit_code,
                    observed_output=observed_output,
                )
            )
    return records


def _shell_verification_evidence(
    *,
    root: Path,
    state: TurnExecutionState,
    arguments: dict[str, Any],
    result: dict[str, Any],
    known_verification_commands: list[str] | None,
    verification_authoritative: bool,
    material_touched_paths: set[str],
    evidence_v2: bool = True,
) -> VerificationEvidence:
    exit_code_raw = result.get("exit_code")
    exit_code = exit_code_raw if isinstance(exit_code_raw, int) else None
    command = str(result.get("effective_cmd") or result.get("cmd") or arguments.get("cmd") or "")
    cwd = result.get("cwd", arguments.get("cwd")) or root
    try:
        execution_root = (root / cwd).resolve()
    except (OSError, TypeError, ValueError):
        execution_root = root
    in_project = execution_root == root.resolve()
    stage_status_raw = result.get("pipeline_stage_status")
    stage_status = (
        [int(item) for item in stage_status_raw]
        if isinstance(stage_status_raw, list)
        and all(isinstance(item, int) for item in stage_status_raw)
        else None
    )
    evidence = _classify_acceptance_aware_verification_evidence(
        command,
        acceptance_contract=state.acceptance_contract if in_project else None,
        known_verification_commands=known_verification_commands if in_project else [],
        required_verification_commands=state.expected_verification_commands
        if in_project
        else set(),
        authoritative=verification_authoritative and in_project,
        changed_paths=state.touched_repo_paths
        if in_project
        else {
            str(Path(path).relative_to(execution_root))
            for path in state.touched_external_paths
            if Path(path).is_relative_to(execution_root)
        },
        material_touched_paths=material_touched_paths
        if not in_project
        else _verification_relevant_material_paths(
            material_touched_paths, root=root, contract=state.acceptance_contract
        ),
        exit_code=exit_code,
        output=_verification_output_text(result),
        root=execution_root,
        stage_status=stage_status,
        evidence_v2=evidence_v2,
        working_directory=".",
    )
    if result.get("host_launcher_unavailable") is True and exit_code == 127:
        evidence = replace(
            evidence,
            real_execution=False,
            allowed_to_satisfy_contract=False,
            reason="host_launcher_unavailable",
        )
    if result.get("external_mutation_tracking_unavailable") is True:
        evidence = replace(
            evidence,
            allowed_to_satisfy_contract=False,
            reason="external_mutation_tracking_unavailable",
        )
    return _verification_evidence_in_workspace(
        evidence, root=root, cwd=result.get("cwd", arguments.get("cwd"))
    )


def _verification_evidence_observation(
    *,
    tool_name: str,
    result: dict[str, Any],
) -> tuple[int | None, bool]:
    """Read one result occurrence; a batch cannot identify a single observation."""

    def _observed_output_capture(payload: dict[str, Any]) -> bool:
        if any(
            key in payload and isinstance(payload.get(key), str)
            for key in ("output", "output_preview", "stdout", "stderr")
        ):
            return True
        output_chars = payload.get("output_chars")
        return isinstance(output_chars, int) and output_chars >= 0

    normalized_tool = tool_name.strip().casefold()
    if normalized_tool == "shell_run":
        exit_code = result.get("exit_code")
        return (
            exit_code if isinstance(exit_code, int) else None,
            _observed_output_capture(result),
        )

    if normalized_tool == "verify_run":
        command_results = result.get("command_results")
        if isinstance(command_results, list):
            if len(command_results) != 1 or not isinstance(command_results[0], dict):
                return None, False
            item = command_results[0]
            exit_code = item.get("exit_code")
            return (
                exit_code if isinstance(exit_code, int) else None,
                _observed_output_capture(item),
            )
        run_status = str(result.get("status") or "")
        if run_status:
            # Tri-state: not_run maps to "no observation", never to failure.
            exit_code = 0 if run_status == "passed" else 1 if run_status == "failed" else None
        else:
            all_passed = result.get("all_passed")
            exit_code = 0 if all_passed is True else 1 if all_passed is False else None
        return (
            exit_code,
            _observed_output_capture(result),
        )

    return None, False


def _unmasked_shell_verification_command(command: str) -> str:
    candidate = str(command or "").strip()
    while match := _SAFE_LEADING_CD_RE.match(candidate):
        candidate = candidate[match.end() :].strip()
    if not candidate or _UNSAFE_CLAIM_EVIDENCE_SHELL_RE.search(candidate):
        return ""
    analysis_candidate = _SHELL_REDIRECTION_RE.sub("", candidate).strip()
    analysis = analyze_verification_command(analysis_candidate, trusted=True)
    if analysis.command_family is None:
        return ""
    # Stripping wrappers is only for recognizing the runner. Retained evidence
    # must still identify the actual directory, redirects and arguments used.
    return _normalize_shell_command_for_match(command)


def _tool_effect_has_qualifying_execution(
    *,
    observations: list[_ObservedVerificationEvidence],
) -> bool:
    """True when a real test/execution run (pass or fail) is observed.

    Qualifying = a recognized test/execution program that actually executed the
    code — a passing run (``real_execution is True``) or a genuine failing run
    (a non-zero exit of a recognized test command). Excludes syntax-only and
    static checks (ast.parse, py_compile, mypy, ruff check) and non-executions
    (no-tests collected, vacuous commands). Used only for the ordering rule.
    """
    for observation in observations:
        record = observation.evidence
        if record.reason in {"mutated_material_paths", "external_mutation_tracking_unavailable"}:
            continue
        if record.category == VerificationEvidenceCategory.NOT_VERIFICATION:
            continue
        if record.real_execution is False:
            continue
        if not command_is_qualifying_execution_evidence(record.normalized_command or ""):
            continue
        if record.real_execution is True:
            return True
        observed_exit_code = observation.observed_exit_code
        if observed_exit_code is not None and observed_exit_code != 0:
            return True
    return False


def _regression_capture_timestamp() -> str:
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:  # noqa: BLE001 - a telemetry timestamp must never crash a turn
        return ""


def _iter_executed_test_command_results(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """Keep each test-runner command paired with its own result occurrence.

    Only commands whose meaningful first stage is pytest or unittest/Django are
    returned — the runners the parsers understand. Other qualifying executions
    (validation scripts, linters) emit no per-test ids and are out of scope.
    """
    pairs: list[tuple[str, dict[str, Any]]] = []
    if tool_name == "shell_run":
        command = str(
            result.get("effective_cmd") or result.get("cmd") or arguments.get("cmd") or ""
        )
        if command and command_is_test_runner(command):
            pairs.append((command, result))
        return pairs
    if tool_name == "verify_run":
        command_results = result.get("command_results")
        if isinstance(command_results, list):
            for item in command_results:
                if not isinstance(item, dict):
                    continue
                command = str(item.get("command") or item.get("effective_command") or "")
                if command and command_is_test_runner(command):
                    pairs.append((command, item))
    return pairs


def _iter_executed_test_commands(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> list[tuple[str, str]]:
    return [
        (command, _verification_output_text(item))
        for command, item in _iter_executed_test_command_results(
            tool_name=tool_name, arguments=arguments, result=result
        )
    ]


def _iter_executed_commands_with_outcome(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    status: str,
    result: dict[str, Any],
) -> list[tuple[str, int | None]]:
    """Yield ``(command, exit_code)`` for every command this tool actually ran.

    ``exit_code`` is ``None`` when the runner reported none; a failed tool status
    with no exit code is reported as a non-zero sentinel so a crashed run is never
    mistaken for a passing one.
    """

    def _exit_code(payload: dict[str, Any]) -> int | None:
        raw = payload.get("exit_code")
        if raw is None:
            raw = payload.get("returncode")
        if raw is None:
            return 1 if status == "failed" else None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 1 if status == "failed" else None

    pairs: list[tuple[str, int | None]] = []
    if tool_name == "shell_run":
        command = str(
            result.get("effective_cmd") or result.get("cmd") or arguments.get("cmd") or ""
        )
        if command:
            pairs.append((command, _exit_code(result)))
        return pairs
    if tool_name == "verify_run":
        command_results = result.get("command_results")
        if isinstance(command_results, list):
            for item in command_results:
                if not isinstance(item, dict):
                    continue
                command = str(item.get("command") or item.get("effective_command") or "")
                if command:
                    pairs.append((command, _exit_code(item)))
    return pairs


def _capture_repro_runs(
    *,
    state: TurnExecutionState,
    tool_name: str,
    arguments: dict[str, Any],
    status: str,
    result: dict[str, Any],
) -> None:
    """Record executions of files the agent created this turn (step 5).

    A command "runs a reproduction" when one of its path tokens is a path the
    agent created this turn — a fact, not an inference about the file's purpose.
    A run with no resolvable exit code is not recorded at all: an unobservable
    outcome can neither confirm nor refute the reproduction.
    """
    if tool_name not in _COMMAND_LIKE_MUTATION_TOOL_NAMES:
        return
    if not state.agent_created_paths:
        return
    for command, exit_code in _iter_executed_commands_with_outcome(
        tool_name=tool_name,
        arguments=arguments,
        status=status,
        result=result,
    ):
        artifacts = match_repro_artifacts(command, state.agent_created_paths)
        if not artifacts or exit_code is None:
            continue
        state.note_repro_run(
            command=command,
            artifact_paths=artifacts,
            exit_code=exit_code,
            passed=exit_code == 0,
        )


def _capture_repro_artifact_edits(
    *,
    state: TurnExecutionState,
    touched_paths: set[str],
) -> None:
    """Record edits to a recorded reproduction artifact made after a product edit.

    Called before ``touched_repo_paths`` absorbs this edit, so the "product code
    already changed" test reads only prior edits.
    """
    if not state.repro_artifact_paths or not touched_paths:
        return
    edited_artifacts = touched_paths & state.repro_artifact_paths
    if not edited_artifacts:
        return
    if not (state.touched_repo_paths - state.agent_created_paths):
        return
    state.note_repro_artifact_edited_after_fix(edited_artifacts)


def scope_environment_is_host(runner: Any, *, verification_config: AppConfig | None = None) -> bool:
    """Only the standard observed host runner has this process's environment.

    Do not instantiate a lazy runner or infer a custom/container environment.
    Inline shell assignments are accounted for by command selection analysis.
    """
    if verification_config is not None:
        # verify_run chooses its own runner, independently of the shell tool.
        try:
            return resolve_verify_sandbox_mode(verification_config) == "off"
        except (TypeError, ValueError, RuntimeError):
            return False
    if type(runner) is LazyShellRunner:
        runner = runner._runner
    return type(runner) is HostShellRunner


def _capture_regression_test_runs(
    *,
    root: Path,
    state: TurnExecutionState,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
    elapsed_ms: int | None = None,
    environment_known: bool = True,
) -> None:
    if tool_name == "shell_run":
        try:
            if (
                root / (result.get("cwd") or arguments.get("cwd") or ".")
            ).resolve() != root.resolve():
                # Project baselines describe that project's files and test IDs.
                # External outcomes have their own cwd-bound observation ledger.
                return
        except (OSError, TypeError, ValueError):
            return
    timestamp = _regression_capture_timestamp()
    pairs = _iter_executed_test_command_results(
        tool_name=tool_name,
        arguments=arguments,
        result=result,
    )
    # A tool call's elapsed time covers everything it ran, so attributing it to a
    # single command is only honest when that call ran exactly one test command.
    duration_seconds: float | None = None
    if elapsed_ms is not None and len(pairs) == 1:
        try:
            duration_seconds = max(0.0, float(elapsed_ms) / 1000.0)
        except (TypeError, ValueError):
            duration_seconds = None
    for command, item in pairs:
        if tool_name == "verify_run" and "host_test_report" in item:
            # This report came from the wrapper's full command output. Explicit
            # unknown/incomplete evidence must not become a pass via its preview.
            report = TestReport.from_payload(item["host_test_report"]) or TestReport()
        else:
            report = parse_test_report(_verification_output_text(item))
            if tool_name == "verify_run":
                report = _structured_verify_test_report(
                    command_result=item,
                    all_passed=result.get("all_passed") is True,
                    parsed_report=report,
                )
        state.note_test_execution(command=command, report=report, timestamp=timestamp)
        # Blast radius (step 6) reads the same parsed reports but keys them by what
        # each run selected rather than by command identity, so a clean whole-suite
        # run can baseline a scope the agent never named.
        state.note_blast_radius_run(
            command=command,
            report=report,
            duration_seconds=duration_seconds,
            workspace_root=root,
            working_directory=(
                str(result.get("cwd") or arguments.get("cwd") or ".")
                if tool_name == "shell_run"
                else None  # verify_run executes from the bound workspace root.
            ),
            environment_known=environment_known,
        )


def _structured_verify_test_report(
    *,
    command_result: dict[str, Any],
    all_passed: bool,
    parsed_report: TestReport,
) -> TestReport:
    """Prefer host-recorded verify success; raw output only adds parsed detail."""
    exit_code = command_result.get("exit_code")
    structured_passed = command_result.get("ok") is True or (all_passed and exit_code == 0)
    if not structured_passed or command_result.get("real_execution") is False:
        return parsed_report
    runner = parsed_report.runner
    if runner == "unknown":
        observed_command = str(
            command_result.get("effective_command") or command_result.get("command") or ""
        )
        lowered = observed_command.casefold()
        runner = "pytest" if "pytest" in lowered else "unittest"
    return TestReport(
        runner=runner,
        passed=parsed_report.passed,
        failed=0,
        skipped=parsed_report.skipped,
        errors=0,
        counts_known=True,
    )


def _capture_expectation_run_outputs(
    *,
    state: TurnExecutionState,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Capture bounded observed outputs of post-edit runs (turn-contract v2).

    The expected-output evidence linker substring-matches contract literals against
    these outputs. Capture is unconditional telemetry (never kill-switched) and
    only records runs after a verification-relevant edit — pre-edit output can never
    confirm a post-edit expectation.
    """
    generation = state.verification_relevant_edit_generation
    if generation <= 0:
        return
    if tool_name == "shell_run":
        command = str(
            result.get("effective_cmd") or result.get("cmd") or arguments.get("cmd") or ""
        )
        output = _verification_output_text(result)
        if command and output:
            state.note_post_edit_run_output(command=command, output=output, generation=generation)
    elif tool_name == "verify_run":
        command_results = result.get("command_results")
        if isinstance(command_results, list):
            for item in command_results:
                if not isinstance(item, dict):
                    continue
                command = str(item.get("command") or item.get("effective_command") or "")
                output = _verification_output_text(item)
                if command and output:
                    state.note_post_edit_run_output(
                        command=command, output=output, generation=generation
                    )
        else:
            output = _verification_output_text(result)
            commands = result.get("commands")
            command = (
                ", ".join(str(item) for item in commands if item)
                if isinstance(commands, list)
                else ""
            )
            if output:
                state.note_post_edit_run_output(
                    command=command or "verify_run", output=output, generation=generation
                )


def _verification_attempt_executed_test_runner(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> bool:
    """True when the recorded verification attempt ran a test-runner command."""
    return bool(
        _iter_executed_test_commands(
            tool_name=tool_name,
            arguments=arguments,
            result=result,
        )
    )


def _acceptance_artifact_changed(
    *, root: Path, contract: AcceptanceContract | None, touched_paths: set[str]
) -> bool:
    """Declared artifacts remain verification-relevant regardless of suffix.

    The general code classifier intentionally ignores documentation/data edits.
    A task that explicitly checks such an artifact still needs fresh evidence
    when it changes; unrelated documentation keeps the existing behavior.
    """

    if contract is None or not touched_paths:
        return False
    declared = {
        ref.workspace_relative_path for ref in contract.path_refs if ref.workspace_relative_path
    } | contract.allowed_output_paths
    for touched in touched_paths:
        changed = (root / touched).resolve()
        for path in declared:
            target = (root / path).resolve()
            if changed == target or target in changed.parents or changed in target.parents:
                return True
    return False


def _record_tool_effect(
    *,
    root: Path,
    state: TurnExecutionState,
    tool_name: str,
    arguments: dict[str, Any],
    status: str,
    result: dict[str, Any],
    known_verification_commands: list[str] | None,
    verification_authoritative: bool = False,
    evidence_v2: bool = True,
    elapsed_ms: int | None = None,
    scope_environment_known: bool = True,
) -> None:
    if is_tool_unavailable_result(result):
        return
    # Required commands are also known identities, including direct callers
    # that supplied the expected set without a separate recommendation list.
    if state.expected_verification_commands:
        known_verification_commands = list(
            dict.fromkeys(
                [
                    *(known_verification_commands or []),
                    *sorted(state.expected_verification_commands),
                ]
            )
        )
    normalized_tool = tool_name.strip().lower()
    external_edits: dict[str, str] = {}
    external_command_paths: set[str] = set()
    if normalized_tool == "shell_run":
        scope = result.get("external_mutation_scope")
        paths = result.get("touched_external_paths")
        if isinstance(scope, str) and Path(scope).is_absolute() and isinstance(paths, list):
            scope_path = Path(scope)
            if not scope_path.is_relative_to(root.resolve()):
                external_command_paths = {
                    item.path
                    for item in classify_mutation_paths(
                        [
                            path
                            for path in paths
                            if isinstance(path, str)
                            and Path(path).is_absolute()
                            and Path(path).is_relative_to(scope_path)
                            and ".." not in Path(path).parts
                        ],
                        root=scope_path,
                        command_was_verification=False,
                    )
                    if item.is_material
                }
                external_command_paths = {str(scope_path / path) for path in external_command_paths}
                state.touched_external_paths.update(external_command_paths)
    if status != "failed" and not result.get("error"):
        external_edits = _extract_scoped_external_edits(
            root=root, tool_name=normalized_tool, arguments=arguments, result=result
        )
        state.touched_external_paths.update(external_edits)
        state.touched_external_paths.update(external_edits.values())
    if (
        normalized_tool == "artifact_check"
        and status != "failed"
        and isinstance(result.get("items"), list)
    ):
        # These are engine observations about exact file bytes, not a certificate
        # that every user requirement or business assertion has been satisfied.
        state.artifact_check_observations.append(
            {key: result[key] for key in ("path", "documentSha256", "items") if key in result}
        )
        state.artifact_check_observations[:] = state.artifact_check_observations[-50:]
        return
    touched_paths: set[str] = set()
    benign_runtime_paths: set[str] = set()
    if normalized_tool in _COMMAND_LIKE_MUTATION_TOOL_NAMES:
        raw_touched_paths = result.get("touched_repo_paths")
        if isinstance(raw_touched_paths, list):
            classifications = classify_mutation_paths(
                [str(item) for item in raw_touched_paths if isinstance(item, str)],
                root=root,
                command_was_verification=normalized_tool == "verify_run",
            )
            touched_paths = {item.path for item in classifications if item.is_material}
            benign_runtime_paths = {item.path for item in classifications if not item.is_material}
            if touched_paths:
                result["material_touched_repo_paths"] = sorted(touched_paths)
            if benign_runtime_paths:
                result["benign_runtime_paths"] = sorted(benign_runtime_paths)
        else:
            touched_paths = _extract_touched_repo_paths(
                root=root,
                tool_name=normalized_tool,
                arguments=arguments,
                result=result,
            )
    elif status != "failed" and (
        normalized_tool in _MATERIAL_EDIT_TOOL_NAMES or normalized_tool == "document_operation"
    ):
        touched_paths = _extract_touched_repo_paths(
            root=root,
            tool_name=normalized_tool,
            arguments=arguments,
            result=result,
        )
    elif normalized_tool in {"subagent_run", "subagent_wait", "subagent_apply"}:
        touched_paths = _extract_touched_repo_paths(
            root=root,
            tool_name=normalized_tool,
            arguments=arguments,
            result=result,
        )

    if status != "failed" and normalized_tool == "fs_write" and result.get("created") is True:
        # A brand-new file the agent authored this turn: a failing test in it is
        # signal (agent_authored), not a regression. touched_paths already holds
        # the normalized repo-relative path for fs_write.
        for created_path in touched_paths:
            # Recreating a previously touched/deleted existing file does not
            # turn it into an agent-authored artifact or erase its obligations.
            if (
                created_path not in state.touched_repo_paths
                or created_path in state.agent_created_paths
            ):
                state.note_agent_created_path(created_path)

    material_edit = normalized_tool in _MATERIAL_EDIT_TOOL_NAMES or (
        normalized_tool == "document_operation" and bool(touched_paths)
    )
    # Reproduction-first guardrail (step 5): the reproduction is only evidence
    # while it stays the one that failed before the fix. Editing a recorded
    # artifact once product code has already changed is recorded and surfaced.
    if status != "failed" and (
        material_edit or normalized_tool in {"subagent_wait", "subagent_apply"}
    ):
        _capture_repro_artifact_edits(state=state, touched_paths=touched_paths)

    if (status != "failed" and material_edit) or (
        normalized_tool in {"subagent_run", "subagent_wait", "subagent_apply"} and touched_paths
    ):
        state.note_material_edit()
        state.material_edit_tools.add(normalized_tool)
        state.touched_repo_paths.update(touched_paths)
        state.turn_touched_repo_paths.update(touched_paths)
        if (
            external_edits
            or _paths_require_verification(touched_paths)
            or _acceptance_artifact_changed(
                root=root, contract=state.acceptance_contract, touched_paths=touched_paths
            )
        ):
            state.note_verification_relevant_edit()
    elif normalized_tool in _COMMAND_LIKE_MUTATION_TOOL_NAMES and (
        touched_paths or external_command_paths
    ):
        state.note_material_edit()
        state.material_edit_tools.add(normalized_tool)
        state.touched_repo_paths.update(touched_paths)
        state.turn_touched_repo_paths.update(touched_paths)
        if (
            external_command_paths
            or _paths_require_verification(touched_paths)
            or _acceptance_artifact_changed(
                root=root, contract=state.acceptance_contract, touched_paths=touched_paths
            )
        ):
            state.note_verification_relevant_edit()
    elif status != "failed" and normalized_tool == "git_diff":
        state.record_diff_review()

    if normalized_tool == "verify_run" and isinstance(result.get("consumer_profile_report"), dict):
        report = result["consumer_profile_report"]
        report["task_id"] = state.acceptance_contract.task_id if state.acceptance_contract else ""
        report["generation"] = state.verification_relevant_edit_generation
        report["authority"] = "supplemental"
        if touched_paths:
            report["passed"] = False
            report["status"] = "failed"
            report["workspace_changed_during_probe"] = True
            result["profile_passed"] = False
        state.consumer_profile_observations.append(dict(report))
        state.consumer_profile_observations[:] = state.consumer_profile_observations[-8:]
        record_consumer_profile_observation(
            contract=state.acceptance_contract,
            report=report,
            generation=state.verification_relevant_edit_generation,
        )
        result["verification_evidence_allowed"] = False
        result["verification_evidence_supplemental_only"] = True
        # Proposed checks do not overwrite the outcome or coverage of the
        # separately executed host/user verification contract, pass or fail.
        return

    verification_attempt = False
    evidence_records: list[VerificationEvidence] = []
    evidence_observations: list[_ObservedVerificationEvidence] = []
    evidence = VerificationEvidence(
        category=VerificationEvidenceCategory.NOT_VERIFICATION,
        normalized_command=str(arguments.get("cmd") or ""),
        reason="not_checked",
    )
    if normalized_tool == "verify_run":
        verification_attempt = True
        evidence_observations = _verify_run_evidence_observations(
            result=result,
            known_verification_commands=known_verification_commands,
            required_verification_commands=state.expected_verification_commands,
            verification_authoritative=verification_authoritative,
            material_touched_paths=touched_paths,
            root=root,
            evidence_v2=evidence_v2,
            acceptance_contract=state.acceptance_contract,
        )
        evidence_records = [observation.evidence for observation in evidence_observations]
        evidence = _aggregate_verification_evidence(evidence_records)
    elif normalized_tool == "shell_run":
        evidence = _shell_verification_evidence(
            root=root,
            state=state,
            arguments=arguments,
            result=result,
            known_verification_commands=known_verification_commands,
            verification_authoritative=verification_authoritative,
            material_touched_paths=touched_paths | external_command_paths,
            evidence_v2=evidence_v2,
        )
        evidence_records = [evidence]
        observed_exit_code, observed_output = _verification_evidence_observation(
            tool_name=normalized_tool, result=result
        )
        evidence_observations = [
            _ObservedVerificationEvidence(
                evidence=evidence,
                observed_exit_code=observed_exit_code,
                observed_output=observed_output,
            )
        ]
        verification_attempt = evidence.category != VerificationEvidenceCategory.NOT_VERIFICATION
    if normalized_tool in _COMMAND_LIKE_MUTATION_TOOL_NAMES:
        result["verification_evidence_category"] = evidence.category.value
        result["verification_evidence_reason"] = evidence.reason
        result["verification_evidence_allowed"] = evidence.allowed_to_satisfy_contract
        result["verification_evidence_supplemental_only"] = evidence.supplemental_only
        result["evidence_verdict"] = evidence.evidence_verdict
        verification_note = _verification_evidence_note(evidence, result=result)
        if verification_note:
            result["verification_note"] = verification_note
    record_acceptance_tool_effect(
        contract=state.acceptance_contract,
        root=root,
        tool_name=normalized_tool,
        arguments=arguments,
        status=status,
        result=result,
        touched_paths=touched_paths,
        external_edits=external_edits,
        known_verification_commands=known_verification_commands,
        verification_authoritative=verification_authoritative,
        evidence_category=evidence.category.value,
        evidence_allowed=evidence.allowed_to_satisfy_contract,
        generation=state.verification_relevant_edit_generation,
        task_id=state.acceptance_contract.task_id if state.acceptance_contract else None,
    )
    # Baseline-first regression protocol (step 3): capture parsed per-test
    # outcomes for baseline/attribution. Runs for every executed test-runner
    # command regardless of the evidence classifier's verdict (so an
    # unobservable-pipeline run still contributes what its output shows), and
    # regardless of the kill-switch (capture is telemetry; only the gate policy
    # is gated).
    _capture_regression_test_runs(
        root=root,
        state=state,
        tool_name=normalized_tool,
        arguments=arguments,
        result=result,
        elapsed_ms=elapsed_ms,
        environment_known=scope_environment_known,
    )
    # Turn-contract v2 (step 4): capture post-edit run output for the expected-output
    # evidence linker. Like the regression capture above, this is unconditional
    # telemetry (only the gate policy is kill-switched).
    _capture_expectation_run_outputs(
        state=state,
        tool_name=normalized_tool,
        arguments=arguments,
        result=result,
    )
    # Reproduction-first (step 5): capture every executed command that runs a file
    # the agent created this turn, phase-classified against the product edits
    # recorded so far. Unconditional telemetry, like the captures above — only the
    # gate policy and the turn directives are kill-switched.
    if normalized_tool != "shell_run" or evidence.working_directory == str(root.resolve()):
        _capture_repro_runs(
            state=state,
            tool_name=normalized_tool,
            arguments=arguments,
            status=status,
            result=result,
        )
    if normalized_tool == "shell_run" and not verification_attempt:
        raw_command = str(
            result.get("effective_cmd") or result.get("cmd") or arguments.get("cmd") or ""
        )
        normalized_command = _unmasked_shell_verification_command(raw_command)
        observed_exit_code, observed_output = _verification_evidence_observation(
            tool_name=normalized_tool,
            result=result,
        )
        if normalized_command and observed_exit_code == 0 and observed_output:
            state.record_executed_command_evidence(
                normalized_command=normalized_command,
                observed_exit_code=observed_exit_code,
                observed_output=observed_output,
            )
    if not verification_attempt:
        return

    state.verification_attempt_count += 1
    state.verification_tools.add(normalized_tool)
    attempt_physically_passed = _verification_attempt_passed(
        tool_name=normalized_tool,
        status=status,
        result=result,
    )
    any_allowed_evidence = any(record.allowed_to_satisfy_contract for record in evidence_records)
    all_evidence_is_supplemental = bool(evidence_records) and all(
        record.supplemental_only for record in evidence_records
    )
    preserve_prior_verification_outcome = bool(
        attempt_physically_passed and not any_allowed_evidence and all_evidence_is_supplemental
    )
    # A successful supplemental check is additional telemetry, not a newer contract
    # verdict. A nonzero result must remain a failure: exit codes and diagnostic
    # text cannot prove that the program never launched. In particular, a program
    # that executed can itself return 126/127. A successful later attempt can
    # establish a newer verdict; retain individual accepted observations for audit.
    if not preserve_prior_verification_outcome:
        state.last_verification_passed = bool(attempt_physically_passed and any_allowed_evidence)
    for observation in evidence_observations:
        record = observation.evidence
        observed_exit_code = observation.observed_exit_code
        observed_output = observation.observed_output
        state.record_verification_evidence(
            record,
            accepted=bool(
                record.allowed_to_satisfy_contract
                and (
                    attempt_physically_passed
                    or (
                        normalized_tool == "verify_run"
                        and record.real_execution is True
                        and observed_exit_code == 0
                        and observed_output
                    )
                )
            ),
            observed_exit_code=observed_exit_code,
            observed_output=observed_output,
        )
    if evidence_v2 and _tool_effect_has_qualifying_execution(
        observations=evidence_observations,
    ):
        # Ordering rule: stamp that a real execution run happened after the most
        # recent material edit, so finalization can require post-edit evidence.
        state.note_qualifying_execution_evidence()
    # Baseline-first regression protocol (step 3): remember whether this
    # verification attempt ran a test-runner command, so the gate can clear a
    # non-contract all-pre-existing test failure without masking a non-test one.
    if not preserve_prior_verification_outcome:
        state.last_verification_attempt_was_test_run = _verification_attempt_executed_test_runner(
            tool_name=normalized_tool,
            arguments=arguments,
            result=result,
        )
    if normalized_tool == "verify_run":
        _record_verify_run_command_outcomes(
            state=state,
            result=result,
            known_verification_commands=known_verification_commands,
            evidence_records=evidence_records,
        )
    elif normalized_tool == "shell_run":
        _record_shell_verification_command_outcome(
            root=root,
            state=state,
            arguments=arguments,
            result=result,
            known_verification_commands=known_verification_commands,
            passed=attempt_physically_passed,
            evidence=evidence,
        )

    resolved_observed_failure = _record_observed_verification_outcomes(
        root=root,
        state=state,
        tool_name=normalized_tool,
        arguments=arguments,
        result=result,
        observations=evidence_observations,
        attempt_passed=attempt_physically_passed,
        environment_known=scope_environment_known,
    )
    complete_execution_observations = bool(evidence_observations) and all(
        observation.evidence.real_execution is True
        and observation.observed_exit_code == 0
        and observation.observed_output
        for observation in evidence_observations
    )
    if normalized_tool == "verify_run":
        commands = result.get("commands")
        command_results = result.get("command_results")
        complete_execution_observations = bool(
            complete_execution_observations
            and isinstance(commands, list)
            and isinstance(command_results, list)
            and len(commands) == len(command_results) == len(evidence_observations)
            and all(item.command_result_index is not None for item in evidence_observations)
        )
    if (
        attempt_physically_passed
        and complete_execution_observations
        and any(item.evidence.allowed_to_satisfy_contract for item in evidence_observations)
    ):
        for key, failure in tuple(state.observed_verification_failures.items()):
            if (
                failure.host_launcher_unavailable
                and failure.requirement != VerificationCommandRequirement.REQUIRED.value
            ):
                # This attempted optional launcher never supplied a test result.
                # Keep its rejected evidence in history, but let a real accepted
                # check recover. Required commands remain in their separate map.
                del state.observed_verification_failures[key]
                resolved_observed_failure = True
    if (
        attempt_physically_passed
        and complete_execution_observations
        and any(
            item.evidence.allowed_to_satisfy_contract
            and (
                item.evidence.covered_verification_commands
                or not any(str(command).strip() for command in (known_verification_commands or []))
            )
            for item in evidence_observations
        )
    ):
        # Preserve the existing accepted-pass policy for contexts we cannot
        # compare (e.g. the strict verifier), including no-contract sessions.
        # This does not prove an unknown supplemental command itself passed.
        # Comparable failures still require their own successful rerun.
        # Newly accepted independent checks beside recommendations must not
        # broaden this legacy recovery policy to different unknown failures.
        for key in tuple(state.observed_verification_failures):
            if key.startswith("unknown:"):
                del state.observed_verification_failures[key]
    if state.observed_verification_failures:
        failure = next(reversed(state.observed_verification_failures.values()))
        state.last_verification_passed = False
        state.last_verification_failure_snippet = failure.snippet
        state.last_verification_failure_category = failure.category
        state.last_verification_attempt_was_test_run = failure.was_test_run
        return
    if (
        (
            resolved_observed_failure
            or (preserve_prior_verification_outcome and state.last_verification_passed is not True)
        )
        and attempt_physically_passed
        and complete_execution_observations
        and all(
            record.allowed_to_satisfy_contract or record.supplemental_only
            for record in evidence_records
        )
        and not state.failed_verification_commands()
    ):
        # A real supplemental success is an execution outcome, not coverage of
        # a wider command. With no outstanding failure it can supersede an
        # inconclusive/non-executing attempt; missing/stale coverage is checked
        # independently.
        state.last_verification_passed = True
        state.last_verification_attempt_was_test_run = _verification_attempt_executed_test_runner(
            tool_name=normalized_tool, arguments=arguments, result=result
        )
        preserve_prior_verification_outcome = False
    if preserve_prior_verification_outcome:
        return
    if state.last_verification_passed is True:
        state.last_verification_failure_category = ""
        if not state.failed_verification_commands():
            state.last_verification_failure_snippet = ""
    else:
        state.last_verification_failure_category = _verification_failure_category_for_tool_result(
            tool_name=normalized_tool,
            arguments=arguments,
            result=result,
        )
        state.last_verification_failure_snippet = (
            extract_verification_failure_snippet(
                tool_name=normalized_tool,
                result=result,
            )
            or state.first_failed_verification_snippet()
        )


def _verification_expected_for_turn(
    *,
    turn_intent: _OneShotRepoTurnIntent,
    blocked: bool,
    touched_repo_paths: set[str],
    verification_contract_requires_execution: bool = False,
    verification_contract_available: bool = True,
    effective_verification_commands: list[str] | tuple[str, ...] | set[str] | None = None,
) -> bool:
    if turn_intent != "execute":
        return False
    if verification_contract_requires_execution:
        return True
    if blocked:
        return False
    if not verification_contract_available:
        return False
    return _verification_commands_apply_to_paths(
        touched_repo_paths,
        effective_verification_commands,
    )


def _completion_gate_blocker_allows_final(
    *,
    state: TurnExecutionState,
    blocked_response: bool,
) -> bool:
    if not blocked_response:
        return False
    if not state.touched_repo_paths or not _paths_require_verification(state.touched_repo_paths):
        return True
    if state.verification_attempt_count <= 0:
        return False
    if state.last_verification_passed is True:
        return True
    return state.last_verification_failure_category == FailureCategory.INFRA_UNAVAILABLE.value


def _execution_evidence_required_for_turn(
    *,
    state: TurnExecutionState,
    turn_intent: str,
    blocked: bool,
    evidence_v2: bool,
    verification_expected: bool,
) -> bool:
    """Ordering rule trigger: an execute turn that mutated a verifiable surface.

    The point of the rule is to catch "edited source, then only ran a syntax
    check (or nothing), then finalized". It requires that verification is
    actually applicable for this turn (``verification_expected``) so greenfield
    workspaces with no test surface are not harassed. Turns with no mutating
    edits (pure Q&A/analysis/advisory) are exempt, as are non-execute turns and
    blocker finalizations.
    """
    return bool(
        evidence_v2
        and verification_expected
        and str(turn_intent or "") == "execute"
        and not blocked
        and state.material_edit_count > 0
        and _paths_require_verification(state.touched_repo_paths)
    )


def _completion_gate_problems(
    *,
    state: TurnExecutionState,
    final_text: str,
    blocked: bool,
    verification_expected: bool,
    require_material_edit_evidence: bool = True,
    evidence_v2: bool = False,
    turn_intent: str = "",
    regression_baseline_enabled: bool = False,
    turn_contract_v2_enabled: bool = False,
    reproduction_first_enabled: bool = False,
    repro_engagement_based: bool = False,
    blast_radius_enabled: bool = False,
) -> list[str]:
    expectation_assessment = state.compute_expectation_assessment(
        enabled=turn_contract_v2_enabled,
        turn_intent=turn_intent,
    )
    repro_assessment = state.compute_repro_assessment(
        enabled=reproduction_first_enabled,
        turn_intent=turn_intent,
        engagement_based=repro_engagement_based,
    )
    blast_radius_assessment = state.compute_blast_radius_assessment(
        enabled=blast_radius_enabled,
        turn_intent=turn_intent,
    )
    execution_evidence_required = _execution_evidence_required_for_turn(
        state=state,
        turn_intent=turn_intent,
        blocked=blocked,
        evidence_v2=evidence_v2,
        verification_expected=verification_expected,
    )
    regression_diff = state.compute_regression_diff(enabled=regression_baseline_enabled)
    # A newer attributed test cannot dispose of a different retained failure.
    # Reuse existing per-run attribution only for the same current observation;
    # old/unknown contexts and multiple contexts for one command stay unresolved.
    retained_failures_attributable = True
    current_runs = {run.command: run for run in state.current_post_edit_test_runs()}
    retained_commands: set[str] = set()
    for key, failure in state.observed_verification_failures.items():
        run = current_runs.get(failure.command)
        if (
            key.startswith("unknown:")
            or not failure.was_test_run
            or failure.command in retained_commands
            or failure.generation != state.verification_relevant_edit_generation
            or run is None
        ):
            retained_failures_attributable = False
            break
        retained_commands.add(failure.command)
        attribution = classify_regression_diff(
            post_report=run.report,
            baseline=state.test_baselines.get(run.command_key),
            agent_created_paths=state.agent_created_paths,
        )
        if not (attribution.regressions or attribution.unattributed or attribution.pre_existing):
            retained_failures_attributable = False
            break
    # Let attribution supersede a non-contract "last attempt failed" block only
    # when that last attempt was itself a test run AND the diff attributes at
    # least one failure as pre-existing/regression/unattributed. The test-run
    # guard stops an all-benign earlier run from masking a failing non-test
    # command; the "not agent-authored-only" guard keeps a failing repro the agent
    # just wrote (agent-authored only) blocking as a generic verification failure.
    regression_attribution_supersedes_last_failure = bool(
        regression_baseline_enabled
        and state.last_verification_attempt_was_test_run
        and retained_failures_attributable
        and (
            regression_diff.regressions
            or regression_diff.unattributed
            or regression_diff.pre_existing
        )
    )
    certificate = evaluate_completion_certificate(
        CompletionCertificateInput(
            # Acceptance criteria are enforcement for execute work. A turn the
            # gate explicitly classifies non-execute produced nothing the
            # contract could bind to, so it is not enforced - mirroring how
            # verification, expectations, repro, and blast-radius checks
            # de-apply above. An unspecified intent keeps enforcement
            # (fail-safe toward the gate).
            contract=(
                None
                if str(turn_intent or "")
                in {"read_only", "advisory_non_execution", "plan_or_analysis_only"}
                else state.acceptance_contract
            ),
            final_text=final_text,
            blocked=blocked,
            blocker_valid=blocked,
            material_edit_count=state.material_edit_count,
            require_material_result=require_material_edit_evidence,
            verification_expected=verification_expected,
            verification_attempt_count=state.verification_attempt_count,
            last_verification_passed=state.last_verification_passed,
            failed_verification_commands=state.failed_verification_commands(),
            expected_verification_commands=set(state.expected_verification_commands),
            missing_verification_commands=state.missing_verification_commands(),
            verification_coverage_stale=state.verification_coverage_is_stale(),
            accepted_verification_evidence=list(state.accepted_verification_evidence),
            execution_evidence_required=execution_evidence_required,
            post_edit_execution_evidence_present=state.has_post_edit_execution_evidence(),
            regression_baseline_enabled=regression_baseline_enabled,
            regressions=regression_diff.regressions,
            unattributed_failures=regression_diff.unattributed,
            pre_existing_failures=regression_diff.pre_existing,
            agent_authored_failures=regression_diff.agent_authored,
            regression_attribution_supersedes_last_failure=(
                regression_attribution_supersedes_last_failure
            ),
            turn_contract_v2_enabled=turn_contract_v2_enabled,
            expectations_unaddressed=expectation_assessment.unaddressed,
            reproduction_first_enabled=reproduction_first_enabled,
            repro_unconfirmed=repro_blocks_finalization(
                repro_assessment,
                material_edit_count=state.material_edit_count,
            ),
            repro_failing_after_fix=repro_assessment.contradicted,
            repro_status=repro_assessment.status.value if repro_assessment.applicable else "",
            repro_artifacts_present=repro_assessment.surviving_artifacts,
            blast_radius_enabled=blast_radius_enabled,
            # Failures step 3 already reports as regressions of the same command are
            # dropped here: one fact, one blocker. What remains is the breakage only
            # the selected scope saw — the tests the agent never chose to run.
            blast_radius_new_failures=tuple(
                test_id
                for test_id in blast_radius_assessment.new_failures
                if test_id not in set(regression_diff.regressions)
            ),
            # Only the "never measured" state feeds the weaker problem; a REGRESSED
            # assessment whose ids step 3 already owns must not resurface here as a
            # coverage complaint about a scope the agent demonstrably ran.
            blast_radius_unverified=(
                blast_radius_assessment.status == BlastRadiusStatus.GATE_MISSING
                and blast_radius_blocks_finalization(
                    blast_radius_assessment,
                    material_edit_count=state.material_edit_count,
                )
            ),
            blast_radius_status=(
                blast_radius_assessment.status.value if blast_radius_assessment.applicable else ""
            ),
        )
    )
    state.latest_completion_certificate = certificate.as_payload()
    state.verification_expected = verification_expected
    return list(certificate.problems)


def _sorted_missing_verification_commands(state: TurnExecutionState) -> list[str]:
    return sorted(state.missing_verification_commands())


def _completion_gate_problem_summary(problems: list[str]) -> str:
    labels = [_COMPLETION_GATE_PROBLEM_LABELS.get(item, item) for item in problems]
    return ", ".join(labels) if labels else "unknown completion gate failure"


def _completion_gate_repair_stage(problems: list[str]) -> str:
    if "no_material_edits" in problems:
        return "no_material_edits"
    # Regressions are the most specific, most actionable verification failure:
    # named tests that passed pre-edit and now fail. Rank them ahead of the
    # generic verification_failed so the repair nudge names them concretely.
    if "regressions_detected" in problems:
        return "regressions_detected"
    # Proven collateral damage ranks with the other regression stages and above the
    # generic verification failure: it names concrete tests, and its repair is a
    # different action (narrow the change) than "make your own check pass".
    if "blast_radius_regressions" in problems:
        return "blast_radius_regressions"
    if "verification_failed" in problems:
        return "verification_failed"
    if "verification_incomplete" in problems:
        return "verification_incomplete"
    if "verification_not_attempted" in problems:
        return "verification_not_attempted"
    if "unattributed_failures" in problems:
        return "unattributed_failures"
    # An unmeasured blast radius ranks *below* the verification stages: when nothing
    # has been run at all, "you ran no tests" is the more fundamental complaint and
    # owns the repair loop. This stage takes over once that is satisfied and only the
    # neighbouring tests are still unrun.
    if "blast_radius_unverified" in problems:
        return "blast_radius_unverified"
    # Reproduction-first: an unvalidated reported symptom ranks above the
    # task-expectation stage — a reproduction is the most direct evidence that the
    # delivered change addresses what was reported, not the agent's reading of it.
    # Scaffolding cleanup shares the stage; the nudge names whichever applies.
    if "repro_unconfirmed" in problems or "repro_artifacts_present" in problems:
        return "repro_unconfirmed"
    # Turn-contract v2: task-named expectations rank below verification/regression
    # deficits (broken behavior is more urgent) but above acceptance-criteria stages.
    if "expectations_unaddressed" in problems:
        return "expectations_unaddressed"
    if "acceptance_criteria_failed" in problems or "unexpected_scope_changes" in problems:
        return "acceptance_failed"
    if (
        "acceptance_criteria_unverified" in problems
        or "acceptance_evidence_insufficient" in problems
    ):
        return "acceptance_unverified"
    if "empty_final_response" in problems:
        return "empty_final_response"
    return "generic"


_LIVE_BACKGROUND_PROCESS_FINALIZATION_LINE = (
    "- You have {n} background process(es) started with shell_background; they are "
    "terminated when this run ends. If the task requires a server/daemon to still "
    "be running after you finish, start it with shell_service_start (durable) instead, "
    "and re-verify."
)


def _live_background_process_finalization_advisory_line(
    *,
    one_shot_execution: bool,
    live_background_processes: int = 0,
) -> str:
    try:
        count = int(live_background_processes)
    except (TypeError, ValueError):
        count = 0
    if not one_shot_execution or count <= 0:
        return ""
    return _LIVE_BACKGROUND_PROCESS_FINALIZATION_LINE.format(n=count)


def _completion_gate_nudge_message(
    problems: list[str],
    *,
    prefix_key: str = "completion_gate_nudge_prefix",
    verification_failure_snippet: str = "",
    verification_outcome_context: str = "",
    missing_verification_commands: list[str] | None = None,
    verification_coverage_stale: bool = False,
    anchor_paths: list[str] | None = None,
    has_material_edits: bool = False,
    has_only_supplemental_verification_evidence: bool = False,
    diff_review_stale: bool = False,
    language: str = "",
    explicit_language_override: bool = False,
    one_shot_execution: bool = False,
    live_background_processes: int = 0,
    execution_evidence_missing_detail: str = "",
    regression_ids: list[str] | None = None,
    regression_baseline_command: str = "",
    unattributed_ids: list[str] | None = None,
    expectation_details: list[str] | None = None,
    repro_assessment: ReproAssessment | None = None,
    blast_radius_assessment: BlastRadiusAssessment | None = None,
) -> str:
    _ = (
        prefix_key,
        verification_coverage_stale,
        anchor_paths,
        language,
        explicit_language_override,
    )
    problem_set = set(problems)
    # A post-edit execution-evidence deficit is action-only: the model must run
    # the tests; a written explanation cannot clear it. Naming the concrete
    # missing fact keeps successive nudges specific rather than repetitive.
    evidence_deficit = bool(execution_evidence_missing_detail) and bool(
        problem_set & {"verification_not_attempted", "verification_incomplete"}
    )
    # Regressions are action-only in the same way: only making the named tests
    # pass again clears the deficit; prose cannot. Unattributed failures need a
    # fact (a rerun of the baseline-known command) to be attributed.
    regression_deficit = "regressions_detected" in problem_set
    unattributed_deficit = "unattributed_failures" in problem_set
    # Turn-contract v2: task-named expectations neither confirmed nor disposed. Each
    # is addressed by editing the named locus or producing (and running) the expected
    # output — prose alone cannot clear it.
    expectation_deficit = "expectations_unaddressed" in problem_set
    # Reproduction-first: an unvalidated reported symptom is action-only too — only
    # a reproduction that failed before the fix and passes after it clears it.
    repro_deficit = "repro_unconfirmed" in problem_set
    repro_artifacts_deficit = "repro_artifacts_present" in problem_set
    # Blast radius: both states are action-only. Only running the scope measures it,
    # and only making the broken tests pass again (by narrowing the change) clears a
    # regression; neither can be talked away.
    blast_radius_deficit = bool(
        problem_set & {"blast_radius_regressions", "blast_radius_unverified"}
    )
    lines = [
        "Finalization check - one pass before you finish:",
        "Continue the current user request. Revise your preceding draft as needed to address "
        "these checks; retain its supported implementation and verification results, and state "
        "any remaining limitation.",
    ]
    if "no_material_edits" in problem_set:
        lines.append(
            "- No file changes are recorded yet. If the task required creating/modifying "
            "something, do it now; if you concluded no change is needed, say so explicitly "
            "with your reasoning."
        )
    snippet = extract_actionable_failure_snippet(verification_failure_snippet)
    if "verification_failed" in problem_set:
        if verification_outcome_context:
            lines.append(verification_outcome_context)
        else:
            failure_detail = snippet or "a verification attempt did not pass"
            lines.append(
                f"- An unresolved verification failure is recorded: {failure_detail}. "
                "Identify the affected check. Fix and re-run it, or explain its remaining limitation."
            )
    if missing_verification_commands and (
        "verification_not_attempted" in problem_set or "verification_incomplete" in problem_set
    ):
        lines.append(
            "- Expected verification not yet run: "
            + ", ".join(missing_verification_commands)
            + ". Run them, or state why they don't apply."
        )
    elif "verification_not_attempted" in problem_set or "verification_incomplete" in problem_set:
        lines.append(
            "- Expected verification has not been completed. Run it, or state why it does not apply."
        )
    if evidence_deficit:
        lines.append(
            f"- No test execution recorded {execution_evidence_missing_detail}. Run the relevant "
            "tests now and observe their output and exit code. A written explanation cannot "
            "clear this - only a new test run can."
        )
    if regression_deficit and regression_ids:
        baseline = str(regression_baseline_command or "").strip() or "the baseline command"
        lines.append(
            "- Regressions your change introduced: "
            + ", ".join(str(item) for item in regression_ids)
            + f". These tests passed in the pre-edit baseline of `{baseline}` and now fail. "
            "Fix them and re-run so they pass again. A written explanation cannot clear this - "
            "only making the tests pass can."
        )
    if unattributed_deficit and unattributed_ids:
        lines.append(
            "- Failures with no comparable pre-edit baseline (cannot tell if your change caused "
            "them): "
            + ", ".join(str(item) for item in unattributed_ids)
            + ". Re-run the exact command you have a baseline for (or run it now to establish "
            "one) so these can be attributed, or state their relationship to your change with "
            "evidence."
        )
    if expectation_deficit and expectation_details:
        lines.append(
            "- Task expectations not yet addressed: "
            + "; ".join(str(item) for item in expectation_details)
            + ". The task named these concretely. Either make your change satisfy each one "
            "(edit the named locus, or produce the expected output and run the command so it "
            "is observed), or explicitly state why the expectation no longer applies."
        )
    if repro_deficit and repro_assessment is not None:
        repro_line = build_repro_nudge_line(repro_assessment)
        if repro_line:
            lines.append(repro_line)
    if repro_artifacts_deficit and repro_assessment is not None:
        artifacts_line = build_repro_artifacts_nudge_line(repro_assessment.surviving_artifacts)
        if artifacts_line:
            lines.append(artifacts_line)
    if blast_radius_deficit and blast_radius_assessment is not None:
        blast_radius_line = build_blast_radius_nudge_line(blast_radius_assessment)
        if blast_radius_line:
            lines.append(blast_radius_line)
    if has_only_supplemental_verification_evidence:
        lines.append(f"- {SUPPLEMENTAL_VERIFICATION_ADVISORY}")
    if has_material_edits and diff_review_stale:
        lines.append(
            "- Consider reviewing the current diff for accidental scope or quality issues before "
            "finalizing."
        )
    live_background_process_line = _live_background_process_finalization_advisory_line(
        one_shot_execution=one_shot_execution,
        live_background_processes=live_background_processes,
    )
    if live_background_process_line:
        lines.append(live_background_process_line)
    lines.append(
        "- Re-read the task statement once and confirm every explicitly named output "
        "(paths, formats, values) exists exactly as requested."
    )
    if (
        evidence_deficit
        or regression_deficit
        or unattributed_deficit
        or expectation_deficit
        or repro_deficit
        or blast_radius_deficit
    ):
        lines.append(
            "Run the relevant tests now, then give your final answer once you have observed "
            "the result."
        )
    else:
        lines.append(
            "Then give your final answer. If you are confident the work is complete as-is, "
            "finalize - this checklist is advisory."
        )
    return "\n".join(lines)


def _build_interactive_turn_verify_task(
    *,
    session: Any,
    instruction: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    task_texts = [instruction] if instruction else []
    task_paths: list[str] = []
    for text in task_texts:
        for path in _extract_workspace_relation_paths_from_text(root=session.root, text=text):
            if path not in task_paths:
                task_paths.append(path)
    if not task_paths and not task_texts:
        return None, []
    task: dict[str, Any] = {}
    if task_paths:
        task["estimated_files"] = list(task_paths)
        task["write_scope"] = list(task_paths)
    if task_texts:
        task["acceptance_criteria"] = list(task_texts)
    return task, task_texts


def _refresh_execute_turn_verification_selection(
    session: Any,
    *,
    instruction: str,
    route_execution_posture: str,
) -> None:
    if not bool(getattr(session, "verification_enabled", True)):
        return
    runtime_kind = getattr(session, "runtime_kind", RuntimeKind.INTERACTIVE_CHAT)
    one_shot_execution = bool(getattr(session, "one_shot_execution", False))
    if runtime_kind != RuntimeKind.INTERACTIVE_CHAT and not one_shot_execution:
        return
    if getattr(session, "authoritative_verification_commands", None) is not None:
        return
    if str(route_execution_posture or "").strip().lower() != "execute":
        return

    repo_scan = _session_repo_scan(session)
    task, plan_requirements = _build_interactive_turn_verify_task(
        session=session,
        instruction=instruction,
    )
    current = _session_verify_command_selection(session)
    # Task-derived selections are projections of the accepted requirements.
    # Recompute them when those requirements change (including a new task),
    # instead of inheriting a previous task's command as permanent authority.
    selection = (
        None if current is not None and current.source.startswith("task_refinement.") else current
    )
    resolved = resolve_task_aware_verify_command_selection(
        cfg=session.cfg,
        verify_cmd=None,
        task=task,
        root=session.root,
        repo_scan=repo_scan,
        plan_requirements=plan_requirements,
        selection=selection,
    )
    explicit_commands = extract_explicit_acceptance_commands(
        *[str(item) for item in plan_requirements],
    )
    if (
        explicit_commands
        and not is_authoritative_verify_command_selection(current)
        and resolved.contract_type in {"generic_fallback", "unavailable", ""}
    ):
        resolved = ResolvedVerifyCommands(
            commands=tuple(explicit_commands),
            source="task_refinement.explicit_user_command",
            reason="explicit user command is the task-native verification contract",
            contract_type="task_acceptance",
        )
    if (
        greenfield_verification_bootstrap_enabled(getattr(session, "cfg", None))
        and str(getattr(session, "verification_selection_source", "")).startswith(
            "agent_authored_bootstrap"
        )
        and resolved.contract_type in {"unavailable", "generic_fallback", ""}
    ):
        # An agent-authored best-effort contract, earned by a passing suite
        # this session, is not re-downgraded by a repo scan that cannot see it.
        # Anything stronger (repo-native, task acceptance, explicit user command)
        # still replaces it above.
        return
    if (
        current is not None
        and current.commands == resolved.commands
        and current.source == resolved.source
        and current.reason == resolved.reason
        and current.contract_type == resolved.contract_type
    ):
        return

    previous_payload = (
        verification_selection_payload(
            current,
            authoritative=is_authoritative_verify_command_selection(current),
        )
        if current is not None
        else None
    )
    session.effective_verification_commands = list(resolved.commands)
    session.verification_selection_source = resolved.source
    session.verification_selection_reason = resolved.reason
    session.verification_contract_type = resolved.contract_type
    session.verification_authoritative = is_authoritative_verify_command_selection(resolved)
    refresh_session_environment_context_message(session)
    payload: dict[str, Any] = {
        "instruction_paths": list(task.get("estimated_files", []))
        if isinstance(task, dict)
        else [],
        "route_execution_posture": route_execution_posture,
        **verification_selection_payload(
            resolved,
            authoritative=is_authoritative_verify_command_selection(resolved),
        ),
    }
    if previous_payload is not None:
        payload["previous"] = previous_payload
    session.store.append("verification_contract_updated", payload)


def _refresh_interactive_turn_verification_selection(
    session: Any,
    *,
    instruction: str,
    route_execution_posture: str,
) -> None:
    _refresh_execute_turn_verification_selection(
        session,
        instruction=instruction,
        route_execution_posture=route_execution_posture,
    )


# ---------------------------------------------------------------------------
# Greenfield governance: agent-authored contract bootstrap and the
# zero-coverage advisory for newly created modules.
# ---------------------------------------------------------------------------


AGENT_BOOTSTRAP_SOURCE = "agent_authored_bootstrap.passing_suite"
AGENT_BOOTSTRAP_REASON = (
    "agent-authored test suite executed and passed in this session; adopted as "
    "the best-effort verification contract for subsequent edits"
)


def maybe_bootstrap_agent_verification_contract(
    session: Any,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Promote an ``unavailable`` contract when an agent-authored suite passes.

    The selection heuristics refuse a generic fallback on projects with no
    pre-existing test surface — which a greenfield project cannot have, so its
    contract stayed ``unavailable`` and the completion gate had nothing to
    demand (the greenfield governance gap). Observed execution breaks that circularity: once
    a test run demonstrably passed in this session, that command is a real,
    runnable check, and later edits are held to re-running it.

    Adopts, in order of preference: the commands of a fully ``passed``
    ``verify_run``; or a passing ``shell_run`` whose command is a recognized
    test runner. Never overrides an authoritative or already-bootstrapped
    selection; kill switch ``ALYSIS_GREENFIELD_VERIFY_BOOTSTRAP``.
    """
    if not greenfield_verification_bootstrap_enabled(getattr(session, "cfg", None)):
        return
    if not bool(getattr(session, "verification_enabled", True)):
        return
    if bool(getattr(session, "one_shot_execution", False)):
        # One-shot/Forge runs resolve their contracts up front and manage their
        # own verification lifecycle; mid-run contract swaps are interactive-only
        # (same scoping as the zero-activity downgrade).
        return
    if (
        getattr(session, "runtime_kind", RuntimeKind.INTERACTIVE_CHAT)
        != RuntimeKind.INTERACTIVE_CHAT
    ):
        return
    if getattr(session, "authoritative_verification_commands", None) is not None:
        return
    contract_type = str(getattr(session, "verification_contract_type", "") or "")
    if contract_type not in {"unavailable", ""}:
        return
    if is_tool_unavailable_result(result):
        return

    normalized_tool = str(tool_name or "").strip().lower()
    commands: list[str] = []
    if normalized_tool == "verify_run":
        if str(result.get("status") or "") != "passed":
            return
        raw_commands = result.get("commands")
        if isinstance(raw_commands, list):
            commands = [str(item).strip() for item in raw_commands if str(item).strip()]
    elif normalized_tool == "shell_run":
        exit_code = result.get("exit_code")
        if not isinstance(exit_code, int) or exit_code != 0:
            return
        commands = [
            command
            for command, _output in _iter_executed_test_commands(
                tool_name=normalized_tool,
                arguments=arguments,
                result=result,
            )
        ]
    if not commands:
        return

    resolved = ResolvedVerifyCommands(
        commands=tuple(dict.fromkeys(commands)),
        source=AGENT_BOOTSTRAP_SOURCE,
        reason=AGENT_BOOTSTRAP_REASON,
        contract_type="best_effort",
        best_effort=True,
    )
    current = _session_verify_command_selection(session)
    previous_payload = (
        verification_selection_payload(
            current,
            authoritative=is_authoritative_verify_command_selection(current),
        )
        if current is not None
        else None
    )
    session.effective_verification_commands = list(resolved.commands)
    session.verification_selection_source = resolved.source
    session.verification_selection_reason = resolved.reason
    session.verification_contract_type = resolved.contract_type
    session.verification_authoritative = False
    session.verification_best_effort = True
    refresh_session_environment_context_message(session)
    payload: dict[str, Any] = {
        "trigger_tool": normalized_tool,
        **verification_selection_payload(resolved, authoritative=False),
    }
    if previous_payload is not None:
        payload["previous"] = previous_payload
    store = getattr(session, "store", None)
    if store is not None:
        store.append("verification_contract_updated", payload)


ZERO_COVERAGE_MARKER_PREFIX = "\n\n---\n⚠️ New modules without detected test references: "

_ZERO_COVERAGE_MAX_TEST_FILES = 40
_ZERO_COVERAGE_MAX_BYTES_PER_FILE = 65536
_ZERO_COVERAGE_EXEMPT_BASENAMES = {"__init__.py", "conftest.py", "setup.py"}


def _zero_coverage_is_testish_path(path: str) -> bool:
    parts = [part.casefold() for part in Path(path).parts]
    basename = parts[-1] if parts else ""
    if any(part in {"tests", "test"} for part in parts[:-1]):
        return True
    return basename.startswith("test_") or basename.endswith("_test.py")


def build_zero_coverage_marker(modules: list[str]) -> str:
    joined = ", ".join(modules)
    return (
        f"{ZERO_COVERAGE_MARKER_PREFIX}{joined} — a bounded text scan found no "
        "references; this does not measure executed coverage."
    )


def uncovered_new_agent_modules(root: Path, state: TurnExecutionState) -> list[str]:
    """Retained agent-created ``.py`` modules that no test file mentions by stem.

    Cheap textual heuristic (would have caught the QA run's greenfield defect: both of that session's tests
    never referenced the parser module). Bounded reads; failures return [] —
    the advisory must never break finalization.
    """
    try:
        # Creation history also includes temporary files later removed. Inspect
        # the final candidate without erasing that history: a recreated file
        # remains newly created and must still be checked.
        modules = sorted(
            path
            for path in state.agent_created_paths
            if path.endswith(".py")
            and Path(path).name not in _ZERO_COVERAGE_EXEMPT_BASENAMES
            and not _zero_coverage_is_testish_path(path)
            and (root / path).is_file()
        )
        if not modules:
            return []
        test_files: list[Path] = []
        for candidate in sorted(root.rglob("*.py")):
            relative = candidate.relative_to(root).as_posix()
            if any(part in {".git", ".alysis", "__pycache__"} for part in candidate.parts):
                continue
            if not _zero_coverage_is_testish_path(relative):
                continue
            test_files.append(candidate)
            if len(test_files) >= _ZERO_COVERAGE_MAX_TEST_FILES:
                break
        haystack_parts: list[str] = []
        for test_file in test_files:
            try:
                haystack_parts.append(
                    test_file.read_text(encoding="utf-8", errors="replace")[
                        :_ZERO_COVERAGE_MAX_BYTES_PER_FILE
                    ]
                )
            except OSError:
                continue
        haystack = "\n".join(haystack_parts)
    except OSError:
        return []
    uncovered: list[str] = []
    for module in modules:
        stem = Path(module).stem
        if stem and stem not in haystack:
            uncovered.append(module)
    return uncovered
