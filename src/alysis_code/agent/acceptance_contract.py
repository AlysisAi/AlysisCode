from __future__ import annotations

import ast
import hashlib
import json
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from ..config import normalize_verify_command_list
from ..repo_scan import RepoScanResult
from ..runtime_artifacts import is_runtime_artifact_path
from ..verification_command_analysis import (
    CheckerEntrypointFingerprint,
    VerificationCommandEvidentiaryCapability,
    analyze_verification_command,
)
from ..verify_gate import assess_verification_command_execution
from .artifact_output_observation import ArtifactOutputObserver
from .turn_contract import (
    MAX_EXPECTATIONS,
    MIN_EXPECTED_OUTPUT_LITERAL_LEN,
    Expectation,
    ExpectationKind,
)
from .verification_commands import _matching_effective_verification_commands


class AcceptanceCriterionKind(StrEnum):
    REQUIRED_ARTIFACT_PATH = "required_artifact_path"
    CONTENT_FORMAT_SCHEMA = "content_format_schema"
    EXPLICIT_COMMAND_IO = "explicit_command_io"
    FUNCTIONAL_API_PROTOCOL = "functional_api_protocol"
    PERSISTENT_SERVICE = "persistent_service"
    DEPENDENCY_VERSION = "dependency_version"
    PRESERVATION_UNCHANGED_PATH = "preservation_unchanged_path"
    THRESHOLD = "threshold"
    EXPLICIT_HOST_USER_VERIFICATION_COMMAND = "explicit_host_user_verification_command"
    PREEXISTING_REPO_CHECK_SURFACE = "preexisting_repo_check_surface"
    REFERENCE_PATH = "reference_path"
    PUBLIC_SYMBOL_INTERFACE = "public_symbol_interface"


class AcceptanceCriterionStatus(StrEnum):
    UNVERIFIED = "UNVERIFIED"
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class AcceptanceCriterionSource(StrEnum):
    USER_INSTRUCTION = "user_instruction"
    PLANNING_CONSTRAINT = "planning_constraint"
    HOST_VERIFICATION = "host_verification"
    REPO_SCAN = "repo_scan"
    DELEGATED_TASK = "delegated_task"


class EvidenceOrigin(StrEnum):
    HOST_AUTHORITATIVE = "HOST_AUTHORITATIVE"
    USER_EXPLICIT = "USER_EXPLICIT"
    PREEXISTING_REPO_NATIVE = "PREEXISTING_REPO_NATIVE"
    PREEXISTING_TASK_CHECKER = "PREEXISTING_TASK_CHECKER"
    DIRECT_BLACK_BOX = "DIRECT_BLACK_BOX"
    SELF_AUTHORED = "SELF_AUTHORED"
    AD_HOC_OBSERVATION = "AD_HOC_OBSERVATION"


class AcceptancePathKind(StrEnum):
    WORKSPACE_RELATIVE = "WORKSPACE_RELATIVE"
    ABSOLUTE_WITHIN_WORKSPACE = "ABSOLUTE_WITHIN_WORKSPACE"
    ABSOLUTE_EXTERNAL = "ABSOLUTE_EXTERNAL"
    UNRESOLVED = "UNRESOLVED"


class AcceptancePathRole(StrEnum):
    REQUIRED_OUTPUT = "required_output"
    EXISTING_INPUT = "existing_input"
    PRESERVATION_TARGET = "preservation_target"
    VERIFICATION_CHECKER = "verification_checker"
    UNKNOWN_REFERENCE = "unknown_reference"
    LOCATION_CONTEXT = "location_context"


class AcceptanceCriterionConfidence(StrEnum):
    AUTHORITATIVE = "AUTHORITATIVE"
    EXPLICIT = "EXPLICIT"
    DERIVED_HIGH_CONFIDENCE = "DERIVED_HIGH_CONFIDENCE"
    HEURISTIC = "HEURISTIC"


class AcceptanceCriterionEnforcement(StrEnum):
    HARD = "HARD"
    ADVISORY = "ADVISORY"


@dataclass(frozen=True)
class AcceptancePathRef:
    raw_text: str
    display_path: str
    path_kind: AcceptancePathKind
    role: AcceptancePathRole
    workspace_relative_path: str = ""
    absolute_path: str = ""
    clause: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "raw_text": self.raw_text,
            "display_path": self.display_path,
            "path_kind": self.path_kind.value,
            "role": self.role.value,
            "workspace_relative_path": self.workspace_relative_path,
            "absolute_path": self.absolute_path,
            "clause": self.clause,
        }


@dataclass(frozen=True)
class AcceptanceThreshold:
    metric: str
    operator: str
    value: float
    unit: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "operator": self.operator,
            "value": self.value,
            "unit": self.unit,
        }


@dataclass
class AcceptanceEvidence:
    evidence_id: str
    origin: EvidenceOrigin
    summary: str
    passed: bool | None = None
    command: str = ""
    paths: tuple[str, ...] = tuple()
    criterion_ids: tuple[str, ...] = tuple()
    category: str = ""
    task_id: str = ""
    generation: int = 0
    tool_name: str = ""
    cwd: str = ""
    exit_code: int | None = None
    discovered_test_count: int | None = None
    executed_test_count: int | None = None
    result_id: str = ""
    evidence_allowed: bool | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "id": self.evidence_id,
            "origin": self.origin.value,
            "summary": self.summary,
            "passed": self.passed,
            "command": self.command,
            "paths": list(self.paths),
            "criterion_ids": list(self.criterion_ids),
            "category": self.category,
            "task_id": self.task_id,
            "generation": self.generation,
            "tool_name": self.tool_name,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "discovered_test_count": self.discovered_test_count,
            "executed_test_count": self.executed_test_count,
            "result_id": self.result_id,
            "evidence_allowed": self.evidence_allowed,
        }


@dataclass
class AcceptanceCriterion:
    criterion_id: str
    kind: AcceptanceCriterionKind
    source: AcceptanceCriterionSource | str
    description: str
    paths: tuple[str, ...] = tuple()
    path_refs: tuple[AcceptancePathRef, ...] = tuple()
    commands: tuple[str, ...] = tuple()
    ports: tuple[int, ...] = tuple()
    thresholds: tuple[AcceptanceThreshold, ...] = tuple()
    required: bool = True
    status: AcceptanceCriterionStatus = AcceptanceCriterionStatus.UNVERIFIED
    evidence_ids: list[str] = field(default_factory=list)
    service_ids: list[str] = field(default_factory=list)
    failure_summary: str = ""
    required_for_finalization: bool = True
    confidence: AcceptanceCriterionConfidence = AcceptanceCriterionConfidence.EXPLICIT
    enforcement: AcceptanceCriterionEnforcement = AcceptanceCriterionEnforcement.HARD

    def add_evidence(
        self,
        evidence_id: str,
        *,
        status: AcceptanceCriterionStatus | None = None,
        summary: str = "",
    ) -> None:
        if evidence_id not in self.evidence_ids:
            self.evidence_ids.append(evidence_id)
        if status is not None:
            self.status = status
        if summary:
            self.failure_summary = summary

    def as_payload(self) -> dict[str, Any]:
        return {
            "id": self.criterion_id,
            "kind": self.kind.value,
            "source": str(getattr(self.source, "value", self.source)),
            "description": self.description,
            "paths": list(self.paths),
            "path_refs": [path_ref.as_payload() for path_ref in self.path_refs],
            "commands": list(self.commands),
            "ports": list(self.ports),
            "thresholds": [threshold.as_payload() for threshold in self.thresholds],
            "required": self.required,
            "status": self.status.value,
            "evidence_ids": list(self.evidence_ids),
            "service_ids": list(self.service_ids),
            "failure_summary": self.failure_summary,
            "required_for_finalization": self.required_for_finalization,
            "confidence": self.confidence.value,
            "enforcement": self.enforcement.value,
        }


@dataclass(frozen=True)
class AcceptanceWorkspaceSnapshot:
    preexisting_paths: frozenset[str] = frozenset()
    preexisting_test_paths: frozenset[str] = frozenset()
    preexisting_checker_paths: frozenset[str] = frozenset()
    preexisting_checker_fingerprints: tuple[CheckerEntrypointFingerprint, ...] = tuple()
    preexisting_verify_commands: tuple[str, ...] = tuple()

    def as_payload(self) -> dict[str, Any]:
        return {
            "preexisting_paths": sorted(self.preexisting_paths)[:200],
            "preexisting_test_paths": sorted(self.preexisting_test_paths)[:200],
            "preexisting_checker_paths": sorted(self.preexisting_checker_paths)[:200],
            "preexisting_checker_fingerprints": [
                item.as_payload() for item in self.preexisting_checker_fingerprints[:200]
            ],
            "preexisting_verify_commands": list(self.preexisting_verify_commands),
        }


@dataclass
class AcceptanceContract:
    criteria: list[AcceptanceCriterion] = field(default_factory=list)
    evidence: list[AcceptanceEvidence] = field(default_factory=list)
    snapshot: AcceptanceWorkspaceSnapshot = field(default_factory=AcceptanceWorkspaceSnapshot)
    allowed_output_paths: set[str] = field(default_factory=set)
    path_refs: list[AcceptancePathRef] = field(default_factory=list)
    # Current-host file-tool observations only; never serialized as authority or
    # restored from transcripts. Values bind a requested spelling to its target.
    external_path_observations: dict[str, str] = field(default_factory=dict, repr=False)
    # Turn-contract v2 (step 4): concrete, checkable expectations extracted from the
    # task text (expected-output literals, named loci, named behaviors). Empty is
    # valid; the completion gate demands a disposition per expectation.
    expectations: list[Expectation] = field(default_factory=list)
    task_id: str = ""
    generation: int = 0
    acceptance_revision: str = ""
    baseline_available: bool = True
    artifact_output_observer: ArtifactOutputObserver | None = field(default=None, repr=False)

    def next_evidence_id(self) -> str:
        return f"ev{len(self.evidence) + 1:03d}"

    def add_evidence(
        self,
        *,
        origin: EvidenceOrigin,
        summary: str,
        passed: bool | None = None,
        command: str = "",
        paths: tuple[str, ...] = tuple(),
        criterion_ids: tuple[str, ...] = tuple(),
        category: str = "",
        task_id: str | None = None,
        generation: int | None = None,
        tool_name: str = "",
        cwd: str = "",
        exit_code: int | None = None,
        discovered_test_count: int | None = None,
        executed_test_count: int | None = None,
        result_id: str = "",
        evidence_allowed: bool | None = None,
    ) -> AcceptanceEvidence:
        evidence = AcceptanceEvidence(
            evidence_id=self.next_evidence_id(),
            origin=origin,
            summary=summary,
            passed=passed,
            command=command,
            paths=tuple(paths),
            criterion_ids=tuple(criterion_ids),
            category=category,
            task_id=self.task_id if task_id is None else task_id,
            generation=self.generation if generation is None else generation,
            tool_name=tool_name,
            cwd=cwd,
            exit_code=exit_code,
            discovered_test_count=discovered_test_count,
            executed_test_count=executed_test_count,
            result_id=result_id,
            evidence_allowed=evidence_allowed,
        )
        self.evidence.append(evidence)
        return evidence

    def required_criteria(self) -> list[AcceptanceCriterion]:
        return [
            criterion
            for criterion in self.criteria
            if criterion.required and criterion.required_for_finalization
        ]

    def status_counts(self) -> dict[str, int]:
        counts = {status.value: 0 for status in AcceptanceCriterionStatus}
        for criterion in self.criteria:
            counts[criterion.status.value] = counts.get(criterion.status.value, 0) + 1
        return counts

    def problem_names(self) -> list[str]:
        problems: list[str] = [] if self.baseline_available else ["acceptance_baseline_unavailable"]
        required = self.required_criteria()
        if any(item.status == AcceptanceCriterionStatus.FAILED for item in required):
            problems.append("acceptance_criteria_failed")
        if any(item.status == AcceptanceCriterionStatus.BLOCKED for item in required):
            problems.append("acceptance_evidence_insufficient")
        if any(
            item.status == AcceptanceCriterionStatus.UNVERIFIED
            and item.kind != AcceptanceCriterionKind.PREEXISTING_REPO_CHECK_SURFACE
            for item in required
        ):
            problems.append("acceptance_criteria_unverified")
        if any(
            item.status == AcceptanceCriterionStatus.FAILED
            and item.kind == AcceptanceCriterionKind.PRESERVATION_UNCHANGED_PATH
            for item in required
        ):
            problems.append("unexpected_scope_changes")
        return list(dict.fromkeys(problems))

    def failure_summaries(self) -> list[str]:
        return [
            f"{criterion.criterion_id}: {criterion.failure_summary or criterion.description}"
            for criterion in self.required_criteria()
            if criterion.status
            in {
                AcceptanceCriterionStatus.FAILED,
                AcceptanceCriterionStatus.BLOCKED,
                AcceptanceCriterionStatus.UNVERIFIED,
            }
        ]

    def as_payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "generation": self.generation,
            "acceptance_revision": self.acceptance_revision,
            "baseline_available": self.baseline_available,
            "criteria": [criterion.as_payload() for criterion in self.criteria],
            "evidence": [evidence.as_payload() for evidence in self.evidence[-20:]],
            "snapshot": self.snapshot.as_payload(),
            "allowed_output_paths": sorted(self.allowed_output_paths),
            "path_refs": [path_ref.as_payload() for path_ref in self.path_refs],
            "expectations": [expectation.as_payload() for expectation in self.expectations],
            "status_counts": self.status_counts(),
            "problems": self.problem_names(),
            "failure_summaries": self.failure_summaries()[:10],
        }


_BACKTICK_COMMAND_RE = re.compile(r"`([^`\n]+)`")
_PATH_EXTENSIONS = (
    r"py|js|ts|tsx|jsx|json|toml|yaml|yml|txt|md|html|css|csv|xml|sql|sh|go|rs|java|rb|php|"
    r"out|expected|actual|bin|docx|xlsx|pptx|pdf|png|jpg|jpeg|webp"
)
_QUOTED_PATH = "|".join(
    rf"(?<={re.escape(quote)})(?:[A-Za-z]:)?"
    rf"[^\x00-\x1f{re.escape(quote)}<>|?:]+?\.(?:{_PATH_EXTENSIONS})(?={re.escape(quote)})"
    for quote in ('"', "'", "`")
)
# Resource-locator syntax is not a local path, regardless of host, scheme,
# language, or filename suffix. Include scp-style user@host:path Git remotes.
_RESOURCE_LOCATOR_RE = re.compile(
    r"(?<![\w./\\])(?:[A-Za-z][A-Za-z0-9+.-]*://|[^\s/@:`\"'<>]+@[^\s/:`\"'<>]+:)"
    r"[^\s`\"'<>]+"
)
_PATH_RE = re.compile(
    r"(?<![\w.-])("
    rf"{_QUOTED_PATH}|"
    # Explicit quoted paths retain spaces and Unicode as literal filename data.
    # A path prefix must occur immediately after the quote. Relative directory
    # prefixes retain U1R filenames with spaces without swallowing quoted commands.
    r'"(?:[A-Za-z]:[\\/]|[\\/]{1,2}|[\w.-]+[\\/])[^"\n]+"|'
    r"'(?:[A-Za-z]:[\\/]|[\\/]{1,2}|[\w.-]+[\\/])[^'\n]+'|"
    r"`(?:[A-Za-z]:[\\/]|[\\/]{1,2}|[\w.-]+[\\/])[^`\n]+`|"
    r"(?:[A-Za-z]:[\\/]|\\\\)"
    r"(?:(?:[\w-]+ )*[\w.-]+[\\/])+[\w.-]+|"
    r"(?:[A-Za-z]:[\\/]|[\\/]{1,2}|\.{1,2}[\\/])?"
    r"(?:[\w.-]+[\\/])+[\w.-]+|"
    r"(?:\.{1,2}[\\/])?[\w.-]+\.[\w-]+"
    r")(?=$|[`\"'\s,;:!?\]\)}]|[.](?:\s|$))"
)
_PORT_RE = re.compile(r"\bport\s+([1-9][0-9]{1,4})\b", re.I)
_THRESHOLD_RE = re.compile(
    r"\b(?P<metric>accuracy|score|coverage|latency|runtime|time|duration|size|memory|throughput|performance)\b"
    r"[^.\n]{0,80}?"
    r"(?P<op>>=|<=|>|<|at least|at most|under|below|above|over|less than|more than)\s*"
    r"(?P<value>[0-9]+(?:\.[0-9]+)?)\s*(?P<unit>%|ms|s|sec|seconds|mb|kb|fps|x)?",
    re.I,
)
_NUMBER_RE = re.compile(r"[-+]?[0-9]+(?:\.[0-9]+)?")
_FORMAT_RE = re.compile(r"\b(json|yaml|csv|xml|html|markdown|schema|format)\b", re.I)
_EXPLICIT_FORMAT_RE = re.compile(
    r"\b(?:valid|well-formed|as|in|format(?:ted)?\s+as|schema(?:\s+of)?|must\s+be|should\s+be)\s+"
    r"(json|yaml|csv|xml|html|markdown)\b|"
    r"\b(json|yaml|csv|xml|html|markdown)\s+(?:format|schema|file|document|output)\b",
    re.I,
)
_SERVICE_RE = re.compile(
    r"\b(?:keep|remain|stay)\s+(?:the\s+)?(?:server|service|process|daemon)?\s*running\b|"
    r"\b(?:persistent|background)\s+(?:server|service|process|daemon)\b|"
    r"\blisten(?:ing)?\s+on\s+port\b",
    re.I,
)
_PRESERVE_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|without|never|leave|keep)\s+"
    r"(?:modify|change|touch|overwrite|alter|remove|delete|unchanged)\b[^\n]*",
    re.I,
)
_ONLY_RE = re.compile(r"\b(?:only|just)\s+(?:write|create|modify|change|touch)\b[^.\n]*", re.I)
_OUTPUT_ROLE_RE = re.compile(
    r"\b(?:save|write|create|produce|output|generate|emit|export|move\s+to|put\s+in|store)\b",
    re.I,
)
_ATTEMPTED_IO_RE = re.compile(
    r"\b(?:attempt|try)\s+to\s+(?:read|write|create|open|delete|remove)\b", re.I
)
_NEGATED_OUTPUT_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never)\s+(?:write|create|produce|generate|emit|export)\b",
    re.I,
)
_INPUT_ROLE_RE = re.compile(
    r"\b(?:read(?:\s+from)?|input|initial|reference|source|existing|given\s+at|provided\s+at|load\s+from|using)\b",
    re.I,
)
# A source connector can switch from an output to its inputs inside one clause.
# Bare nouns such as "source" must not override "create source code at ...".
_INPUT_CONNECTOR_RE = re.compile(r"\b(?:using|from|given\s+at|provided\s+at)\b", re.I)
_PRESERVATION_ROLE_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|without|never|leave|keep|preserve)\b"
    r"[^;\n]{0,80}?"
    r"\b(?:modif(?:y|ying)|chang(?:e|ing)|touch(?:ing)?|overwrit(?:e|ing)|alter(?:ing)?|"
    r"remov(?:e|ing)|delet(?:e|ing)|unchanged|intact)\b|"
    r"\b(?:preserve|keep)\b[^;\n]{0,80}?\b(?:unchanged|intact)\b",
    re.I,
)
_CHECKER_ROLE_RE = re.compile(
    r"\b(?:test|check|verify|validator|validation|compare|diff|cmp)\b", re.I
)
_COMMAND_INTRO_RE = re.compile(
    r"\b(?:run|execute|test\s+with|verify\s+with|validate\s+with|check\s+with|install\s+with|"
    r"using\s+command|command|shell|terminal|bash)\b",
    re.I,
)
_PYTHON_SNIPPET_INTRO_RE = re.compile(
    r"\b(?:python|py)\b.{0,40}\b(?:snippet|code|validation|check|assert|execute|run)\b|"
    r"\b(?:snippet|code|validation|check|assert|execute|run)\b.{0,40}\b(?:python|py)\b",
    re.I,
)
_COMMAND_HEADS = {
    "bash",
    "cargo",
    "cmp",
    "curl",
    "diff",
    "go",
    "just",
    "make",
    "node",
    "npm",
    "pnpm",
    "pytest",
    "python",
    "python3",
    "py",
    "sh",
    "uv",
    "yarn",
}
_CHECK_PATH_MARKERS = {"check", "checks", "test", "tests", "verify", "validation", "validator"}


def build_acceptance_contract(
    *,
    root: Path,
    instruction: str,
    authoritative_verification_commands: list[str] | None = None,
    effective_verification_commands: list[str] | None = None,
    repo_scan: RepoScanResult | None = None,
    planning_constraints: Any | None = None,
    delegated: bool = False,
    generation: int = 0,
    workspace_snapshot: AcceptanceWorkspaceSnapshot | None = None,
    baseline_available: bool = True,
    interpreted_path_refs: list[AcceptancePathRef] | None = None,
) -> AcceptanceContract:
    snapshot = (
        workspace_snapshot
        if workspace_snapshot is not None
        else capture_acceptance_workspace_snapshot(
            root=root,
            repo_scan=repo_scan,
            authoritative_verification_commands=authoritative_verification_commands,
            effective_verification_commands=effective_verification_commands,
        )
    )
    # Only the current host-supplied request creates explicit obligations.
    instruction_source = (
        AcceptanceCriterionSource.DELEGATED_TASK
        if delegated
        else AcceptanceCriterionSource.USER_INSTRUCTION
    )
    texts = [instruction] if instruction else []
    criteria: list[AcceptanceCriterion] = []
    allowed_output_paths: set[str] = set()
    path_refs = (
        list(interpreted_path_refs)
        if interpreted_path_refs is not None
        else _extract_path_refs(root=root, texts=texts)
    )
    ambiguous_external_clauses = {
        ref.clause
        for ref in path_refs
        if ref.path_kind == AcceptancePathKind.ABSOLUTE_EXTERNAL
        and not ref.raw_text.startswith(('"', "'", "`"))
    }
    has_external_context = any(
        ref.path_kind == AcceptancePathKind.ABSOLUTE_EXTERNAL for ref in path_refs
    )
    bound_path_refs: list[AcceptancePathRef] = []

    for path_ref in path_refs:
        # Unquoted external paths can be only a prefix of a filename containing
        # spaces. Keep the prose hint, but never turn that guess into a hard
        # completion requirement. Actual effects are bound to host tool paths.
        # A validated interpretation has already resolved complete components
        # and containing directories. Prefix/cwd ambiguity applies only to the
        # heuristic detector; it must not weaken an exact declared requirement.
        exact_scope = interpreted_path_refs is not None or (
            (
                path_ref.clause not in ambiguous_external_clauses
                or path_ref.raw_text.startswith(('"', "'", "`"))
            )
            and not (
                has_external_context
                and path_ref.path_kind
                in {
                    AcceptancePathKind.WORKSPACE_RELATIVE,
                    AcceptancePathKind.UNRESOLVED,
                }
            )
        )
        if exact_scope:
            bound_path_refs.append(path_ref)
        # A filename mentioned elsewhere in an external-folder task has no
        # explicit cwd. Do not invent a requirement to create it in the project.
        path_confidence = (
            AcceptanceCriterionConfidence.EXPLICIT
            if exact_scope
            else AcceptanceCriterionConfidence.HEURISTIC
        )
        path_enforcement = (
            AcceptanceCriterionEnforcement.HARD
            if exact_scope
            else AcceptanceCriterionEnforcement.ADVISORY
        )
        if path_ref.role == AcceptancePathRole.REQUIRED_OUTPUT:
            if exact_scope and path_ref.workspace_relative_path:
                allowed_output_paths.add(path_ref.workspace_relative_path)
            criteria.append(
                _criterion(
                    criteria,
                    kind=AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH,
                    source=instruction_source,
                    description=f"Required output path: {path_ref.display_path}",
                    paths=_legacy_paths_from_refs((path_ref,)),
                    path_refs=(path_ref,),
                    confidence=path_confidence,
                    enforcement=path_enforcement,
                )
            )
        elif path_ref.role == AcceptancePathRole.PRESERVATION_TARGET:
            criteria.append(
                _criterion(
                    criteria,
                    kind=AcceptanceCriterionKind.PRESERVATION_UNCHANGED_PATH,
                    source=instruction_source,
                    description=f"Preserve unchanged: {path_ref.display_path}",
                    paths=_legacy_paths_from_refs((path_ref,)),
                    path_refs=(path_ref,),
                    confidence=path_confidence,
                    enforcement=path_enforcement,
                )
            )
        elif path_ref.role in {
            AcceptancePathRole.EXISTING_INPUT,
            AcceptancePathRole.VERIFICATION_CHECKER,
            AcceptancePathRole.UNKNOWN_REFERENCE,
            AcceptancePathRole.LOCATION_CONTEXT,
        }:
            criteria.append(
                _criterion(
                    criteria,
                    kind=AcceptanceCriterionKind.REFERENCE_PATH,
                    source=instruction_source,
                    description=f"Path reference: {path_ref.display_path}",
                    paths=_legacy_paths_from_refs((path_ref,)),
                    path_refs=(path_ref,),
                    confidence=AcceptanceCriterionConfidence.HEURISTIC,
                    enforcement=AcceptanceCriterionEnforcement.ADVISORY,
                )
            )

    for path_ref, fmt, explicit in _extract_path_scoped_formats(path_refs, texts=texts):
        confidence = (
            AcceptanceCriterionConfidence.EXPLICIT
            if explicit
            else AcceptanceCriterionConfidence.HEURISTIC
        )
        enforcement = (
            AcceptanceCriterionEnforcement.HARD
            if explicit
            and path_ref.role == AcceptancePathRole.REQUIRED_OUTPUT
            and any(
                path_ref in item.path_refs
                and item.enforcement == AcceptanceCriterionEnforcement.HARD
                for item in criteria
            )
            else AcceptanceCriterionEnforcement.ADVISORY
        )
        criteria.append(
            _criterion(
                criteria,
                kind=AcceptanceCriterionKind.CONTENT_FORMAT_SCHEMA,
                source=instruction_source,
                description=f"{fmt.upper()} format requirement for {path_ref.display_path}",
                paths=_legacy_paths_from_refs((path_ref,)),
                path_refs=(path_ref,),
                confidence=confidence,
                enforcement=enforcement,
            )
        )

    for command in _extract_explicit_commands(texts):
        criteria.append(
            _criterion(
                criteria,
                kind=AcceptanceCriterionKind.EXPLICIT_COMMAND_IO,
                source=instruction_source,
                description=f"Explicit command must pass: {command}",
                commands=(command,),
                confidence=AcceptanceCriterionConfidence.EXPLICIT,
                # Prose supplies no structured cwd for this command. In a task
                # spanning folders it cannot become a selected-project check.
                # Actual runs and failures are still recorded with their cwd.
                enforcement=(
                    AcceptanceCriterionEnforcement.ADVISORY
                    if has_external_context
                    else AcceptanceCriterionEnforcement.HARD
                ),
            )
        )

    for symbol in _extract_public_symbols(texts):
        criteria.append(
            _criterion(
                criteria,
                kind=AcceptanceCriterionKind.PUBLIC_SYMBOL_INTERFACE,
                source=instruction_source,
                description=f"Public symbol/interface reference: {symbol}",
                confidence=AcceptanceCriterionConfidence.HEURISTIC,
                enforcement=AcceptanceCriterionEnforcement.ADVISORY,
            )
        )

    for threshold in _extract_thresholds(texts):
        criteria.append(
            _criterion(
                criteria,
                kind=AcceptanceCriterionKind.THRESHOLD,
                source=instruction_source,
                description=(
                    f"Threshold: {threshold.metric} {threshold.operator} "
                    f"{threshold.value:g}{threshold.unit}"
                ),
                thresholds=(threshold,),
                confidence=AcceptanceCriterionConfidence.EXPLICIT,
                enforcement=AcceptanceCriterionEnforcement.HARD,
            )
        )

    for port in _extract_ports(texts):
        criteria.append(
            _criterion(
                criteria,
                kind=AcceptanceCriterionKind.FUNCTIONAL_API_PROTOCOL,
                source=instruction_source,
                description=f"Protocol/API behavior on port {port}",
                ports=(port,),
                confidence=AcceptanceCriterionConfidence.EXPLICIT,
                enforcement=AcceptanceCriterionEnforcement.HARD,
            )
        )

    if any(_SERVICE_RE.search(text) for text in texts):
        criteria.append(
            _criterion(
                criteria,
                kind=AcceptanceCriterionKind.PERSISTENT_SERVICE,
                source=instruction_source,
                description="Persistent service must survive finalization",
                ports=tuple(_extract_ports(texts)),
                confidence=AcceptanceCriterionConfidence.EXPLICIT,
                enforcement=AcceptanceCriterionEnforcement.HARD,
            )
        )

    if any(_ONLY_RE.search(text) for text in texts) and allowed_output_paths:
        criteria.append(
            _criterion(
                criteria,
                kind=AcceptanceCriterionKind.PRESERVATION_UNCHANGED_PATH,
                source=instruction_source,
                description="No unexpected material paths outside requested outputs",
                paths=tuple(sorted(allowed_output_paths)),
                path_refs=tuple(
                    ref
                    for ref in path_refs
                    if ref.role == AcceptancePathRole.REQUIRED_OUTPUT
                    and ref.workspace_relative_path in allowed_output_paths
                ),
                confidence=AcceptanceCriterionConfidence.EXPLICIT,
                enforcement=AcceptanceCriterionEnforcement.HARD,
            )
        )

    for command in normalize_verify_command_list(authoritative_verification_commands or []):
        criteria.append(
            _criterion(
                criteria,
                kind=AcceptanceCriterionKind.EXPLICIT_HOST_USER_VERIFICATION_COMMAND,
                source=AcceptanceCriterionSource.HOST_VERIFICATION,
                description=f"Host verification command must pass: {command}",
                commands=(command,),
                confidence=AcceptanceCriterionConfidence.AUTHORITATIVE,
                enforcement=AcceptanceCriterionEnforcement.HARD,
            )
        )

    for command in normalize_verify_command_list(effective_verification_commands or []):
        if command in {item for criterion in criteria for item in criterion.commands}:
            continue
        source = (
            AcceptanceCriterionSource.REPO_SCAN
            if command in snapshot.preexisting_verify_commands
            else AcceptanceCriterionSource.HOST_VERIFICATION
        )
        criteria.append(
            _criterion(
                criteria,
                kind=AcceptanceCriterionKind.PREEXISTING_REPO_CHECK_SURFACE,
                source=source,
                description=f"Pre-existing verification surface: {command}",
                commands=(command,),
                confidence=AcceptanceCriterionConfidence.DERIVED_HIGH_CONFIDENCE,
                enforcement=AcceptanceCriterionEnforcement.ADVISORY,
            )
        )

    for item in _planning_constraint_criteria(criteria, planning_constraints):
        criteria.append(item)

    residual = _residual_functional_requirement(texts)
    if residual:
        criteria.append(
            _criterion(
                criteria,
                kind=AcceptanceCriterionKind.FUNCTIONAL_API_PROTOCOL,
                source=instruction_source,
                description=f"Functional requirement context: {residual}",
                confidence=AcceptanceCriterionConfidence.HEURISTIC,
                enforcement=AcceptanceCriterionEnforcement.ADVISORY,
            )
        )

    return AcceptanceContract(
        task_id="",
        generation=generation,
        criteria=_dedupe_criteria(criteria),
        snapshot=snapshot,
        baseline_available=baseline_available,
        allowed_output_paths=allowed_output_paths,
        path_refs=path_refs,
        expectations=extract_task_expectations(texts=texts, path_refs=bound_path_refs),
    )


def capture_acceptance_workspace_snapshot(
    *,
    root: Path,
    repo_scan: RepoScanResult | None = None,
    authoritative_verification_commands: list[str] | None = None,
    effective_verification_commands: list[str] | None = None,
) -> AcceptanceWorkspaceSnapshot:
    preexisting_paths: set[str] = set()
    preexisting_test_paths: set[str] = set()
    preexisting_checker_paths: set[str] = set()
    if repo_scan is not None:
        for raw_path in [
            *(str(item.get("path") or "") for item in repo_scan.manifests),
            *repo_scan.readme_paths,
            *repo_scan.observed_paths,
        ]:
            path = _normalize_rel_path(raw_path)
            if path:
                preexisting_paths.add(path)
                if _is_test_or_checker_path(path):
                    preexisting_test_paths.add(path)
    root = root.resolve()
    visited = 0
    for candidate in _iter_bounded_existing_paths(root):
        visited += 1
        if visited > 600:
            break
        preexisting_paths.add(candidate)
        if _is_test_or_checker_path(candidate):
            preexisting_test_paths.add(candidate)
        if _is_checker_path(candidate):
            preexisting_checker_paths.add(candidate)
    for command in normalize_verify_command_list(
        [
            *normalize_verify_command_list(authoritative_verification_commands or []),
            *normalize_verify_command_list(effective_verification_commands or []),
        ]
    ):
        analysis = analyze_verification_command(command, trusted=True, workspace_root=root)
        for path in analysis.checker_entrypoint_paths:
            normalized_path = _normalize_rel_path(path)
            if normalized_path and (root / normalized_path).exists():
                preexisting_checker_paths.add(normalized_path)
                preexisting_test_paths.add(normalized_path)
                preexisting_paths.add(normalized_path)
    checker_fingerprints = tuple(
        _fingerprint_checker_path(root=root, relpath=path)
        for path in sorted(preexisting_checker_paths)
    )
    return AcceptanceWorkspaceSnapshot(
        preexisting_paths=frozenset(preexisting_paths),
        preexisting_test_paths=frozenset(preexisting_test_paths),
        preexisting_checker_paths=frozenset(preexisting_checker_paths),
        preexisting_checker_fingerprints=checker_fingerprints,
        preexisting_verify_commands=tuple(
            normalize_verify_command_list(
                repo_scan.likely_test_commands if repo_scan is not None else []
            )
        ),
    )


def record_acceptance_tool_effect(
    *,
    contract: AcceptanceContract | None,
    root: Path,
    tool_name: str,
    arguments: dict[str, Any],
    status: str,
    result: dict[str, Any],
    touched_paths: set[str],
    external_edits: dict[str, str] | None = None,
    known_verification_commands: list[str] | None = None,
    verification_authoritative: bool = False,
    evidence_category: str = "",
    evidence_allowed: bool | None = None,
    generation: int | None = None,
    task_id: str | None = None,
) -> None:
    if contract is None:
        return
    normalized_tool = str(tool_name or "").strip().lower()
    command_results = result.get("command_results")
    if normalized_tool == "verify_run" and isinstance(command_results, list) and command_results:
        for item in command_results:
            if not isinstance(item, dict):
                continue
            command = str(item.get("effective_command") or item.get("command") or "").strip()
            if not command:
                continue
            single_result = {
                "cwd": result.get("cwd", arguments.get("cwd", str(root))),
                **item,
                "commands": [command],
            }
            record_acceptance_tool_effect(
                contract=contract,
                root=root,
                tool_name=normalized_tool,
                arguments=arguments,
                status="failed" if item.get("ok") is False else "ok",
                result=single_result,
                touched_paths=touched_paths,
                known_verification_commands=known_verification_commands,
                verification_authoritative=verification_authoritative,
                evidence_category=str(
                    item.get("verification_evidence_category") or evidence_category
                ),
                evidence_allowed=item.get("verification_evidence_allowed", evidence_allowed),
                generation=generation,
                task_id=task_id,
            )
        return
    observed_generation = contract.generation if generation is None else generation
    observed_task = contract.task_id if task_id is None else task_id
    if observed_task == contract.task_id and observed_generation > contract.generation:
        invalidate_acceptance_evidence(contract, generation=observed_generation)
    command = _observed_command(tool_name=normalized_tool, arguments=arguments, result=result)
    command_passed = _command_passed(status=status, result=result)
    output = _tool_output(result)
    cwd = str(result.get("cwd") or arguments.get("cwd") or root)
    try:
        cwd_path = Path(cwd)
        same_workspace = (
            cwd_path if cwd_path.is_absolute() else root / cwd_path
        ).resolve() == root.resolve()
    except (OSError, ValueError):
        same_workspace = False
    current = observed_generation == contract.generation and observed_task == contract.task_id
    exit_code = result.get("exit_code")
    exit_code = exit_code if type(exit_code) is int else None
    discovered, executed = _observed_test_counts(command=command, result=result, output=output)
    allowed = evidence_allowed
    # Two different questions meet here. "Did the command really run, here and
    # now, without side effects?" is about execution. "Can this command prove
    # anything beyond its own exit status?" is about evidentiary capability.
    # Verification evidence needs both. A command the user named is its own
    # acceptance check, so that criterion needs only the first.
    capability_verdicts = _evidentiary_capability_verdicts(command) if command else frozenset()
    execution_observed = bool(command)
    if command:
        if (
            not same_workspace
            or not current
            or exit_code is None
            or executed == 0
            or discovered == 0
            or touched_paths
        ):
            allowed = False
            execution_observed = False
        if result.get("real_execution") is False:
            allowed = False
            if str(result.get("non_execution_reason") or "") not in capability_verdicts:
                execution_observed = False
        if exit_code is not None:
            assessment = assess_verification_command_execution(
                command=command, exit_code=exit_code, output=output
            )
            if assessment.real_execution is False:
                allowed = False
                if str(assessment.non_execution_reason or "") not in capability_verdicts:
                    execution_observed = False
    explicit_command_allowed = allowed
    if (
        allowed is False
        and execution_observed
        and (
            evidence_allowed is not False
            or str(result.get("verification_evidence_reason") or "") in capability_verdicts
        )
    ):
        # Every refusal was about capability, none about the run itself. The
        # evidence record below still carries ``allowed`` (not verification).
        explicit_command_allowed = True
    host_covered_commands: tuple[str, ...] = ()
    covered_payload = result.get("verification_evidence_covered_commands")
    if normalized_tool == "verify_run" and allowed is True and isinstance(covered_payload, list):
        known = set(normalize_verify_command_list(known_verification_commands or []))
        known.update(
            command
            for criterion in contract.criteria
            if criterion.kind == AcceptanceCriterionKind.EXPLICIT_COMMAND_IO
            for command in criterion.commands
        )
        host_covered_commands = tuple(
            item for item in covered_payload if isinstance(item, str) and item in known
        )
    origin = (
        classify_evidence_origin(
            contract=contract,
            root=root,
            command=command,
            touched_paths=touched_paths,
            known_verification_commands=known_verification_commands,
            verification_authoritative=verification_authoritative,
        )
        if same_workspace
        else EvidenceOrigin.AD_HOC_OBSERVATION
    )
    if host_covered_commands and origin != EvidenceOrigin.SELF_AUTHORED:
        # The host may expand a glob before executing a required command. Its
        # classified coverage preserves the obligation's identity; the evidence
        # command below still records the invocation that actually executed.
        origin = classify_evidence_origin(
            contract=contract,
            root=root,
            command=host_covered_commands[0],
            touched_paths=touched_paths,
            known_verification_commands=known_verification_commands,
            verification_authoritative=verification_authoritative,
        )
    criterion_ids: list[str] = []
    if command and current and same_workspace:
        criterion_ids.extend(
            _update_command_and_threshold_criteria(
                contract=contract,
                command=command,
                output=output,
                passed=command_passed,
                origin=origin,
                evidence_allowed=allowed,
                explicit_command_allowed=explicit_command_allowed,
                host_covered_commands=host_covered_commands,
            )
        )
    if current:
        if status != "failed" and not result.get("error"):
            contract.external_path_observations.update(external_edits or {})
            # A user may name an alias while the tool uses its canonical path.
            # Bind that exact reference now; later alias retargeting invalidates it.
            targets = set((external_edits or {}).values())
            for ref in contract.path_refs:
                if (
                    ref.path_kind == AcceptancePathKind.ABSOLUTE_EXTERNAL
                    and Path(ref.absolute_path).is_absolute()
                ):
                    target = str(Path(ref.absolute_path).resolve())
                    if target in targets:
                        contract.external_path_observations[ref.absolute_path] = target
        criterion_ids.extend(
            _update_path_criteria(
                contract=contract,
                root=root,
                touched_paths=touched_paths,
                status=status,
                external_path_observations=contract.external_path_observations,
            )
        )
    if current and normalized_tool in {
        "shell_service_start",
        "shell_service_status",
        "workspace_preview_start",
    }:
        criterion_ids.extend(
            _update_durable_service_criteria(
                contract=contract,
                result=result,
            )
        )
    elif current and normalized_tool == "shell_background":
        # persist=true routes shell_background through the durable-service
        # manager, so its result carries durable ownership and is durable
        # evidence. A plain background start produces no such payload and is
        # still blocked exactly as before.
        durable_criterion_ids = _update_durable_service_criteria(
            contract=contract,
            result=result,
        )
        criterion_ids.extend(
            durable_criterion_ids
            if durable_criterion_ids
            else _block_session_owned_service_criteria(contract=contract)
        )
    if current and same_workspace and normalized_tool in {"verify_run", "shell_run"}:
        criterion_ids.extend(
            _update_repo_surface_criteria(
                contract=contract,
                command=command,
                passed=command_passed,
                evidence_allowed=allowed,
                known_verification_commands=known_verification_commands,
                origin=origin,
                host_covered_commands=host_covered_commands,
            )
        )
    observed_paths = touched_paths | set((external_edits or {}).values())
    if command or observed_paths or criterion_ids:
        evidence = contract.add_evidence(
            origin=origin,
            summary=_evidence_summary(command=command, touched_paths=observed_paths),
            passed=command_passed,
            command=command,
            paths=tuple(sorted(observed_paths)),
            criterion_ids=tuple(sorted(set(criterion_ids))),
            category=evidence_category,
            task_id=observed_task,
            generation=observed_generation,
            tool_name=normalized_tool,
            cwd=cwd,
            exit_code=exit_code,
            discovered_test_count=discovered,
            executed_test_count=executed,
            result_id=str(
                result.get("result_id")
                or result.get("artifact_id")
                or (
                    "sha256:"
                    + hashlib.sha256(
                        json.dumps(result, sort_keys=True, default=str).encode("utf-8")
                    ).hexdigest()
                )
            ),
            evidence_allowed=allowed,
        )
        for criterion in contract.criteria:
            if criterion.criterion_id in criterion_ids and evidence.evidence_id not in (
                criterion.evidence_ids
            ):
                criterion.evidence_ids.append(evidence.evidence_id)


def invalidate_acceptance_evidence(contract: AcceptanceContract | None, *, generation: int) -> None:
    """Expire execution-dependent passes using the runtime's existing edit clock."""
    if contract is None or generation <= contract.generation:
        return
    contract.generation = generation
    for criterion in contract.criteria:
        if criterion.status == AcceptanceCriterionStatus.PASSED and criterion.kind not in {
            AcceptanceCriterionKind.REFERENCE_PATH,
            AcceptanceCriterionKind.PRESERVATION_UNCHANGED_PATH,
        }:
            criterion.status = AcceptanceCriterionStatus.UNVERIFIED
            criterion.failure_summary = "Evidence predates a relevant edit"


def _observed_test_counts(
    *, command: str, result: dict[str, Any], output: str
) -> tuple[int | None, int | None]:
    from .regression_baseline import command_is_test_runner, parse_test_report

    def count(key: str) -> int | None:
        value = result.get(key)
        return value if type(value) is int and value >= 0 else None

    discovered = count("discovered_test_count")
    executed = count("executed_test_count")
    if not command_is_test_runner(command):
        return discovered, executed
    if discovered is None:
        collected = re.search(r"\bcollected\s+(\d+)\s+items?\b", output, re.I)
        if collected:
            discovered = int(collected.group(1))
    if executed is None:
        report = parse_test_report(output)
        ran = re.search(r"\bRan\s+(\d+)\s+tests?\s+in\b", output)
        if ran:
            executed = max(0, int(ran.group(1)) - (report.skipped or 0))
        elif (
            report.counts_known
            and report.passed is not None
            and not re.search(r"\b\d+\s+x(?:failed|passed)\b", output)
        ):
            executed = sum(value or 0 for value in (report.passed, report.failed, report.errors))
        elif re.search(r"\b(?:no tests ran|no tests collected|ran 0 tests)\b", output, re.I):
            executed = 0
    return discovered, executed


def finalize_acceptance_contract(
    *,
    contract: AcceptanceContract | None,
    root: Path,
    touched_paths: set[str],
    durable_service_status: Callable[[str], dict[str, Any]] | None = None,
) -> None:
    if contract is None:
        return
    observer = contract.artifact_output_observer
    if observer is not None:
        observer.refresh(contract)
    for criterion in contract.criteria:
        if criterion.kind == AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH:
            missing = _missing_required_output_paths(
                criterion=criterion,
                root=root,
                external_path_observations=contract.external_path_observations,
                observer=observer,
            )
            if missing:
                criterion.status = AcceptanceCriterionStatus.UNVERIFIED
                criterion.failure_summary = "Required output path is missing: " + ", ".join(missing)
            elif criterion.status == AcceptanceCriterionStatus.UNVERIFIED:
                criterion.status = AcceptanceCriterionStatus.PASSED
                criterion.failure_summary = ""
        elif criterion.kind == AcceptanceCriterionKind.PRESERVATION_UNCHANGED_PATH:
            if not contract.baseline_available:
                criterion.status = AcceptanceCriterionStatus.BLOCKED
                criterion.failure_summary = "The original task preservation baseline is unavailable"
                continue
            if "outside requested outputs" in criterion.description.casefold():
                unexpected = [
                    path
                    for path in sorted(touched_paths)
                    if _path_is_material_for_scope(path, root=root)
                    and not _path_matches_any(path, criterion.paths)
                ]
                if unexpected:
                    criterion.status = AcceptanceCriterionStatus.FAILED
                    criterion.failure_summary = "Unexpected material path changed: " + ", ".join(
                        unexpected[:8]
                    )
                elif criterion.status == AcceptanceCriterionStatus.UNVERIFIED:
                    criterion.status = AcceptanceCriterionStatus.PASSED
            else:
                changed = [
                    path
                    for path in sorted(touched_paths)
                    if _path_matches_any(path, _workspace_paths_for_criterion(criterion))
                ]
                if changed:
                    criterion.status = AcceptanceCriterionStatus.FAILED
                    criterion.failure_summary = "Preservation path changed: " + ", ".join(changed)
                elif criterion.status == AcceptanceCriterionStatus.UNVERIFIED:
                    criterion.status = AcceptanceCriterionStatus.PASSED
        elif criterion.kind == AcceptanceCriterionKind.PERSISTENT_SERVICE:
            if criterion.service_ids:
                _finalize_persistent_service_criterion(
                    criterion=criterion,
                    durable_service_status=durable_service_status,
                )
            elif criterion.status == AcceptanceCriterionStatus.PASSED:
                criterion.status = AcceptanceCriterionStatus.BLOCKED
                criterion.failure_summary = (
                    "Same-session service evidence is not durable-service evidence"
                )
        elif (
            criterion.kind == AcceptanceCriterionKind.FUNCTIONAL_API_PROTOCOL
            and criterion.service_ids
        ):
            _finalize_persistent_service_criterion(
                criterion=criterion,
                durable_service_status=durable_service_status,
            )
        elif criterion.kind == AcceptanceCriterionKind.CONTENT_FORMAT_SCHEMA:
            _finalize_content_format_criterion(criterion=criterion, root=root)


def classify_evidence_origin(
    *,
    contract: AcceptanceContract,
    command: str,
    touched_paths: set[str],
    root: Path | None = None,
    known_verification_commands: list[str] | None = None,
    verification_authoritative: bool = False,
) -> EvidenceOrigin:
    command = _normalize_command(command)
    if not contract.baseline_available:
        return EvidenceOrigin.AD_HOC_OBSERVATION
    if command and _command_references_mutable_preexisting_checker(
        command,
        contract=contract,
        root=root,
    ):
        return EvidenceOrigin.SELF_AUTHORED
    if command and verification_authoritative:
        matches = _matching_effective_verification_commands(
            observed_command=command,
            effective_verification_commands=known_verification_commands,
        )
        if matches:
            return EvidenceOrigin.HOST_AUTHORITATIVE
    if command and any(
        _commands_equivalent(command, candidate)
        for criterion in contract.criteria
        if criterion.kind == AcceptanceCriterionKind.EXPLICIT_COMMAND_IO
        for candidate in criterion.commands
    ):
        return EvidenceOrigin.USER_EXPLICIT
    if touched_paths and any(
        _is_self_authored_check_path(path, contract) for path in touched_paths
    ):
        return EvidenceOrigin.SELF_AUTHORED
    if command and _command_references_self_authored_check(command, contract):
        return EvidenceOrigin.SELF_AUTHORED
    if command and _is_direct_black_box_command(command):
        return EvidenceOrigin.DIRECT_BLACK_BOX
    if command and _command_references_preexisting_checker(command, contract):
        return EvidenceOrigin.PREEXISTING_TASK_CHECKER
    if command and (
        command in contract.snapshot.preexisting_verify_commands
        or bool(
            _matching_effective_verification_commands(
                observed_command=command,
                effective_verification_commands=contract.snapshot.preexisting_verify_commands,
            )
        )
    ):
        return EvidenceOrigin.PREEXISTING_REPO_NATIVE
    return EvidenceOrigin.AD_HOC_OBSERVATION


def acceptance_contract_problem_payload(contract: AcceptanceContract | None) -> dict[str, Any]:
    if contract is None:
        return {
            "acceptance_status_counts": {},
            "acceptance_problems": [],
            "acceptance_failure_summaries": [],
        }
    return {
        "acceptance_status_counts": contract.status_counts(),
        "acceptance_problems": contract.problem_names(),
        "acceptance_failure_summaries": contract.failure_summaries()[:10],
        "acceptance_contract": contract.as_payload(),
    }


def _criterion(
    existing: list[AcceptanceCriterion],
    *,
    kind: AcceptanceCriterionKind,
    source: AcceptanceCriterionSource | str,
    description: str,
    paths: tuple[str, ...] = tuple(),
    path_refs: tuple[AcceptancePathRef, ...] = tuple(),
    commands: tuple[str, ...] = tuple(),
    ports: tuple[int, ...] = tuple(),
    thresholds: tuple[AcceptanceThreshold, ...] = tuple(),
    required: bool | None = None,
    required_for_finalization: bool | None = None,
    confidence: AcceptanceCriterionConfidence = AcceptanceCriterionConfidence.EXPLICIT,
    enforcement: AcceptanceCriterionEnforcement = AcceptanceCriterionEnforcement.HARD,
) -> AcceptanceCriterion:
    is_hard = enforcement == AcceptanceCriterionEnforcement.HARD
    resolved_required = is_hard if required is None else bool(required)
    resolved_required_for_finalization = (
        is_hard if required_for_finalization is None else bool(required_for_finalization)
    )
    return AcceptanceCriterion(
        criterion_id=f"ac{len(existing) + 1:03d}",
        kind=kind,
        source=source,
        description=" ".join(str(description or "").split())[:500],
        paths=tuple(_normalize_rel_path(path) for path in paths if _normalize_rel_path(path)),
        path_refs=tuple(path_refs),
        commands=tuple(_normalize_command(command) for command in commands if command.strip()),
        ports=tuple(ports),
        thresholds=tuple(thresholds),
        required=resolved_required,
        required_for_finalization=resolved_required_for_finalization,
        confidence=confidence,
        enforcement=enforcement,
    )


def _dedupe_criteria(criteria: list[AcceptanceCriterion]) -> list[AcceptanceCriterion]:
    seen: set[tuple[Any, ...]] = set()
    out: list[AcceptanceCriterion] = []
    for criterion in criteria:
        key = (
            criterion.kind.value,
            criterion.description.casefold(),
            criterion.paths,
            criterion.commands,
            criterion.ports,
            tuple(
                (item.metric, item.operator, item.value, item.unit) for item in criterion.thresholds
            ),
        )
        if key in seen:
            continue
        seen.add(key)
        criterion.criterion_id = f"ac{len(out) + 1:03d}"
        out.append(criterion)
    return out


def _mask_path_tokens(text: str) -> str:
    # Preserve offsets while ensuring a filename such as input/write.csv cannot
    # introduce a new action or split a coordinated list of source files.
    # Use non-whitespace so an "and <path>" list cannot become "and <verb>".
    def masked(match: re.Match[str]) -> str:
        token = match.group(0)
        length = len(token.rstrip(".,;:!?)]}"))
        return "\x00" * length + token[length:]

    return _PATH_RE.sub(masked, text)


def _iter_clauses(
    texts: list[str], *, split_actions: bool = True, protected_literals: tuple[str, ...] = ()
) -> list[str]:
    clauses: list[str] = []
    boundary = re.compile(
        r"\n+|;|,(?=\s*(?:(?:and|then)\s+)?(?:save|write|create|produce|output|generate|emit|move|"
        r"read|input|initial|reference|source|given|do\s+not|don't|dont|without|never|"
        r"leave|keep|preserve|run|verify|check)\b)|"
        r"\b(?:and|then)\s+(?=(?:save|write|create|produce|output|generate|emit|move|read|"
        r"input|initial|reference|source|given|do\s+not|don't|dont|without|never|"
        r"leave|keep|preserve|run|verify|check)\b)|"
        r"(?<=[.!?])\s+(?=[A-Z])",
        re.I,
    )
    if not split_actions:
        boundary = re.compile(r"\n+|;|(?<=[.!?])\s+(?=[A-Z])", re.I)
    for text in texts:
        raw = str(text or "")
        # Quoted filenames may contain spaces, semicolons, or words such as
        # "and save". Those bytes belong to the path, not the task grammar.
        protected = list(_mask_path_tokens(raw))
        for match in _RESOURCE_LOCATOR_RE.finditer(raw):
            protected[match.start() : match.end()] = "x" * (match.end() - match.start())
        for literal in protected_literals:
            if literal:
                for match in re.finditer(re.escape(literal), raw):
                    protected[match.start() : match.end()] = "x" * (match.end() - match.start())
        protected_view = "".join(protected)
        start = 0
        for match in boundary.finditer(protected_view):
            # Attempted operations do not create an unconditional output. Only
            # unprotected words may supply that predicate; filenames are data.
            if match.group(0).lower().startswith("and ") and _ATTEMPTED_IO_RE.search(
                protected_view[start : match.start()]
            ):
                continue
            if clause := raw[start : match.start()].strip():
                clauses.append(clause)
            start = match.end()
        if clause := raw[start:].strip():
            clauses.append(clause)
    return clauses


def _prefix_path_role(prefix: str) -> AcceptancePathRole:
    output = list(_OUTPUT_ROLE_RE.finditer(prefix))
    source = list(_INPUT_CONNECTOR_RE.finditer(prefix))
    if output:
        if source and source[-1].start() > output[-1].start():
            return AcceptancePathRole.EXISTING_INPUT
        return AcceptancePathRole.REQUIRED_OUTPUT
    if source or _INPUT_ROLE_RE.search(prefix):
        return AcceptancePathRole.EXISTING_INPUT
    if _CHECKER_ROLE_RE.search(prefix):
        return AcceptancePathRole.VERIFICATION_CHECKER
    return AcceptancePathRole.UNKNOWN_REFERENCE


def _clause_path_role(clause: str) -> AcceptancePathRole:
    # Classify the surrounding instruction, not words inside a path candidate.
    # "input/output example table" and "inspect src/create.py" do not ask us
    # to create files; an explicit "create input/output" still does.
    clause = _PATH_RE.sub(lambda match: " " * len(match.group(0)), clause)
    if _PRESERVATION_ROLE_RE.search(clause):
        return AcceptancePathRole.PRESERVATION_TARGET
    if _OUTPUT_ROLE_RE.search(clause):
        return AcceptancePathRole.REQUIRED_OUTPUT
    if _INPUT_ROLE_RE.search(clause):
        return AcceptancePathRole.EXISTING_INPUT
    if _CHECKER_ROLE_RE.search(clause):
        return AcceptancePathRole.VERIFICATION_CHECKER
    return AcceptancePathRole.UNKNOWN_REFERENCE


def _path_role(
    clause: str,
    path_match: re.Match[str],
) -> AcceptancePathRole:
    path_start = path_match.start(1)
    masked = _mask_path_tokens(clause)
    prefix = masked[:path_start]
    if _ATTEMPTED_IO_RE.search(prefix) or _NEGATED_OUTPUT_RE.search(prefix):
        # An attempt (including an expected denial) does not promise that the
        # target will exist. Keep the reference without inventing an output
        # obligation; execution/check evidence must still establish the result.
        return AcceptancePathRole.UNKNOWN_REFERENCE
    preservation = None
    for candidate in _PRESERVATION_ROLE_RE.finditer(masked):
        if candidate.start() >= path_start:
            break
        preservation = candidate
    if preservation is None:
        return _prefix_path_role(masked[:path_start])
    if path_start < preservation.end():
        return AcceptancePathRole.PRESERVATION_TARGET

    boundary = clause.rfind(",", preservation.end(), path_start)
    if boundary < 0:
        return AcceptancePathRole.PRESERVATION_TARGET
    scope_is_complete = bool(re.search(r"\b(?:unchanged|intact)\s*$", preservation.group(0), re.I))
    prior_path = _PATH_RE.search(clause, preservation.end(), boundary)
    # A completed state phrase ends at the comma. An open direct-object phrase
    # carries across it only after binding a path, which preserves path lists.
    if not scope_is_complete and prior_path is not None:
        return AcceptancePathRole.PRESERVATION_TARGET
    return _prefix_path_role(masked[boundary + 1 : path_start])


def _path_source_literal(path: str) -> str:
    """Remove prose delimiters without rewriting the literal's source spelling."""
    token = str(path or "").strip()
    quoted = len(token) > 1 and token[0] in "`'\"" and token[-1] == token[0]
    return token[1:-1] if quoted else token.rstrip(".,;:!?)]}")


def _clean_path_token(path: str) -> str:
    cleaned = _path_source_literal(path).replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def _extract_path_refs(*, root: Path, texts: list[str]) -> list[AcceptancePathRef]:
    refs: list[AcceptancePathRef] = []
    seen: set[tuple[str, str, str]] = set()
    for clause in _iter_clauses(texts):
        resource_spans = [match.span() for match in _RESOURCE_LOCATOR_RE.finditer(clause)]
        commands = set(_extract_explicit_commands([clause]))
        command_spans = [
            token.span()
            for token in _BACKTICK_COMMAND_RE.finditer(clause)
            if _normalize_command(token.group(1)) in commands
        ]
        for match in _PATH_RE.finditer(clause):
            if any(start <= match.start(1) < end for start, end in resource_spans):
                continue
            # An interpreter's absolute installation path is part of a command,
            # not an instruction to work on files in that installation folder.
            if any(start <= match.start() < end for start, end in command_spans):
                continue
            # A Git ref may contain slashes without naming a filesystem output.
            # Match the immediate noun, so "on branch x create src/a.py" still
            # requires the real file while "create branch test/x" does not.
            before = clause[: match.start(1)].rstrip(" `\"'")
            if re.search(r"\b(?:branch|tag|ref)(?:\s+(?:named|called))?\s*$", before, re.I):
                continue
            role = _path_role(clause, match)
            if _context_is_suggestion(clause):
                role = AcceptancePathRole.UNKNOWN_REFERENCE
            path_ref = _resolve_acceptance_path(
                root=root,
                raw_text=match.group(1),
                role=role,
                clause=clause,
            )
            if path_ref is None:
                continue
            if (
                path_ref.role == AcceptancePathRole.UNKNOWN_REFERENCE
                and not _looks_like_explicit_artifact_path(path_ref.display_path)
                and path_ref.path_kind != AcceptancePathKind.ABSOLUTE_EXTERNAL
            ):
                continue
            key = (
                path_ref.display_path.casefold(),
                path_ref.role.value,
                path_ref.path_kind.value,
            )
            if key in seen:
                continue
            seen.add(key)
            refs.append(path_ref)
    return refs[:40]


def _resolve_acceptance_path(
    *,
    root: Path,
    raw_text: str,
    role: AcceptancePathRole,
    clause: str,
) -> AcceptancePathRef | None:
    cleaned = _clean_path_token(raw_text)
    if not cleaned or cleaned.startswith("-"):
        return None
    pure = PurePosixPath(cleaned)
    if ".." in pure.parts:
        return AcceptancePathRef(
            raw_text=raw_text,
            display_path=cleaned,
            path_kind=AcceptancePathKind.UNRESOLVED,
            role=role,
            clause=clause,
        )
    absolute = _classify_absolute_acceptance_path(cleaned=cleaned, root=root)
    if absolute is not None:
        rel, absolute_path = absolute
        if rel is None:
            return AcceptancePathRef(
                raw_text=raw_text,
                display_path=cleaned,
                path_kind=AcceptancePathKind.ABSOLUTE_EXTERNAL,
                role=role,
                absolute_path=absolute_path,
                clause=clause,
            )
        return AcceptancePathRef(
            raw_text=raw_text,
            display_path=rel or ".",
            path_kind=AcceptancePathKind.ABSOLUTE_WITHIN_WORKSPACE,
            role=role,
            workspace_relative_path=rel,
            absolute_path=absolute_path,
            clause=clause,
        )
    normalized = _normalize_rel_path(cleaned)
    if not normalized:
        return None
    windows_root = PureWindowsPath(str(root))
    candidate = (
        windows_root / normalized
        if windows_root.is_absolute()
        else Path(root).expanduser().resolve(strict=False) / normalized
    )
    return AcceptancePathRef(
        raw_text=raw_text,
        display_path=normalized,
        path_kind=AcceptancePathKind.WORKSPACE_RELATIVE,
        role=role,
        workspace_relative_path=normalized,
        absolute_path=candidate.as_posix(),
        clause=clause,
    )


def _classify_absolute_acceptance_path(
    *,
    cleaned: str,
    root: Path,
) -> tuple[str | None, str] | None:
    """Classify absolute paths without applying host-OS semantics to user text.

    ``Path('/usr/local/bin/tool').resolve()`` on Windows silently prefixes the
    current drive, while ``Path('C:/workspace/file')`` is not absolute on POSIX.
    Acceptance paths can describe either style regardless of the host, so compare
    them with their matching pure-path model and preserve external spelling.
    """

    root_text = _clean_path_token(str(root))
    windows_candidate = PureWindowsPath(cleaned)
    if windows_candidate.is_absolute() and windows_candidate.drive:
        absolute_path = windows_candidate.as_posix()
        windows_root = PureWindowsPath(root_text)
        if windows_root.is_absolute() and windows_root.drive:
            try:
                rel = windows_candidate.relative_to(windows_root).as_posix()
            except ValueError:
                rel = None
            return rel, absolute_path
        return None, absolute_path

    if not cleaned.startswith("/"):
        return None

    posix_candidate = PurePosixPath(cleaned)
    posix_root = PurePosixPath(root_text)
    absolute_path = posix_candidate.as_posix()
    if posix_root.is_absolute():
        try:
            rel = posix_candidate.relative_to(posix_root).as_posix()
        except ValueError:
            rel = None
        return rel, absolute_path
    return None, absolute_path


def _legacy_paths_from_refs(path_refs: tuple[AcceptancePathRef, ...]) -> tuple[str, ...]:
    paths: list[str] = []
    for path_ref in path_refs:
        if path_ref.workspace_relative_path:
            paths.append(path_ref.workspace_relative_path)
        elif path_ref.path_kind == AcceptancePathKind.ABSOLUTE_EXTERNAL:
            paths.append(path_ref.display_path)
    return tuple(dict.fromkeys(paths))


def _extract_path_scoped_formats(
    path_refs: list[AcceptancePathRef],
    *,
    texts: list[str],
) -> list[tuple[AcceptancePathRef, str, bool]]:
    out: list[tuple[AcceptancePathRef, str, bool]] = []
    seen: set[tuple[str, str]] = set()
    output_refs = [
        path_ref for path_ref in path_refs if path_ref.role == AcceptancePathRole.REQUIRED_OUTPUT
    ]
    extension_formats = {
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".csv": "csv",
        ".xml": "xml",
        ".html": "html",
        ".md": "markdown",
    }
    for path_ref in path_refs:
        if path_ref.role != AcceptancePathRole.REQUIRED_OUTPUT:
            continue
        explicit_format = _explicit_format_for_clause(path_ref.clause)
        if explicit_format:
            key = (path_ref.display_path.casefold(), explicit_format)
            if key not in seen:
                seen.add(key)
                out.append((path_ref, explicit_format, True))
            continue
        suffix = PurePosixPath(path_ref.display_path).suffix.casefold()
        inferred = extension_formats.get(suffix)
        if inferred:
            key = (path_ref.display_path.casefold(), inferred)
            if key not in seen:
                seen.add(key)
                out.append((path_ref, inferred, False))
    if len(output_refs) == 1:
        for clause in _iter_clauses(texts):
            if _PATH_RE.search(clause):
                continue
            if not re.search(r"\b(?:output|result|artifact|file)\b", clause, re.I):
                continue
            explicit_format = _explicit_format_for_clause(clause)
            if not explicit_format:
                continue
            path_ref = output_refs[0]
            key = (path_ref.display_path.casefold(), explicit_format)
            if key not in seen:
                seen.add(key)
                out.append((path_ref, explicit_format, True))
    return out


def _explicit_format_for_clause(clause: str) -> str:
    for match in _EXPLICIT_FORMAT_RE.finditer(clause or ""):
        value = (match.group(1) or match.group(2) or "").casefold()
        if value:
            return value
    return ""


def _extract_paths(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for path_ref in _extract_path_refs(root=Path("."), texts=[text]):
        path = path_ref.workspace_relative_path or path_ref.display_path
        if not path or path.startswith("-") or ".." in PurePosixPath(path).parts:
            continue
        if not _looks_like_explicit_artifact_path(path):
            continue
        if path.casefold() in seen:
            continue
        seen.add(path.casefold())
        out.append(path)
    return out[:24]


def _extract_explicit_commands(texts: list[str]) -> list[str]:
    commands: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for match in _BACKTICK_COMMAND_RE.finditer(text):
            candidate = _normalize_command(match.group(1))
            context = str(text or "")[max(0, match.start() - 80) : match.start()]
            if _context_is_suggestion(context) or _context_negates_command(context):
                continue
            suffix_requires_pass = bool(
                re.match(
                    r"\s*(?:must|shall|needs to)\s+(?:pass|succeed|exit\s+with\s+(?:code\s+)?0)\b",
                    text[match.end() : match.end() + 80],
                    re.I,
                )
            )
            python_snippet = _python_interpreter_snippet_command(candidate, context=context)
            if python_snippet:
                if not re.search(r"\b(?:validate|verify|check|run|execute|test)\b", context, re.I):
                    continue
                candidate = python_snippet
            elif (
                not candidate
                or not _looks_like_command(
                    candidate, context="run " if suffix_requires_pass else context
                )
                or not (_has_direct_command_intro(context) or suffix_requires_pass)
            ):
                continue
            key = candidate
            if key in seen:
                continue
            seen.add(key)
            commands.append(candidate)
    return commands[:8]


def _extract_public_symbols(texts: list[str]) -> list[str]:
    symbols: list[str] = []
    for text in texts:
        for match in _BACKTICK_COMMAND_RE.finditer(text):
            candidate = match.group(1).strip()
            context = text[max(0, match.start() - 80) : match.start()]
            if (
                _expectation_span_is_symbol_like(candidate)
                and not _PATH_RE.fullmatch(candidate)
                and not _looks_like_command(candidate, context=context)
                and candidate not in symbols
            ):
                symbols.append(candidate)
    return symbols[:40]


def extract_explicit_acceptance_commands(*texts: str) -> list[str]:
    return _extract_explicit_commands([str(text or "") for text in texts if str(text or "")])


# ---------------------------------------------------------------------------
# Turn-contract v2 (step 4): expectation extraction.
#
# A zero-new-regex projection of the derivation's EXISTING extracted signals into
# the contract-v2 expectations schema. Backtick literals the task shows as desired
# output become ``expected_output`` expectations; files the task points at as the
# fix site become ``named_locus`` expectations. This adds no new NL/keyword/regex
# heuristic over task text — it reuses ``_BACKTICK_COMMAND_RE``, the existing
# command/path classifiers, and the already-computed path refs. Precision over
# recall: capped, deduped, and limited to signals a later mechanical check can
# confirm (an ``expected_output`` literal observed in a run, or an edited locus).
# ---------------------------------------------------------------------------

# A named locus is a file the task tells us to create or modify. UNKNOWN_REFERENCE
# is deliberately excluded: the role classifier cannot tell "fix the bug in X"
# (an edit site) from "count the lines in X" (a read-only input), so treating every
# bare file mention as an edit target would spuriously demand editing inputs.
# Precision over recall — a REQUIRED_OUTPUT path is unambiguously an edit target.
_EXPECTATION_LOCUS_ROLES = {
    AcceptancePathRole.REQUIRED_OUTPUT,
}


def _expectation_span_is_path_like(span: str) -> bool:
    # A forward slash is the path separator used throughout; a bare backslash is
    # NOT treated as a path signal because LaTeX/math literals (``\dagger``) — a
    # common expected_output shape — carry backslashes. Windows-style ``dir\file``
    # paths still resolve via the explicit-artifact-path extension check below.
    token = span.strip().strip("`'\"")
    if "/" in token:
        return True
    return _looks_like_explicit_artifact_path(token)


def _expectation_span_is_symbol_like(span: str) -> bool:
    token = span.strip()
    if not token or any(char.isspace() for char in token):
        return False
    core = token[:-2] if token.endswith("()") else token
    parts = [part for part in core.split(".") if part]
    return bool(parts) and all(part.isidentifier() for part in parts)


def _expectation_source_window(text: str, start: int, end: int) -> str:
    window = str(text or "")[max(0, start - 60) : end + 60]
    return " ".join(window.split())[:280]


def extract_task_expectations(
    *,
    texts: list[str],
    path_refs: list[AcceptancePathRef],
) -> list[Expectation]:
    """Extract concrete, checkable expectations from the task text (contract v2).

    Empty is valid (many tasks name no such literal or locus). Deterministic.
    """
    expectations: list[Expectation] = []
    seen: set[tuple[str, str]] = set()

    def _add(kind: ExpectationKind, raw_text: str, source_quote: str) -> None:
        text = str(raw_text or "").strip()
        if not text:
            return
        key = (kind.value, " ".join(text.split()).casefold())
        if key in seen:
            return
        seen.add(key)
        expectations.append(
            Expectation(
                expectation_id=f"exp{len(expectations) + 1:03d}",
                kind=kind,
                text=text,
                source_quote=" ".join(str(source_quote or "").split())[:280],
            )
        )

    # named_locus: files the task points at as the fix site / subject of the change
    # (a create/modify target or a bare reference — not a read-only input, a
    # preservation target, or a checker path, which editing would not confirm).
    for path_ref in path_refs:
        if path_ref.role not in _EXPECTATION_LOCUS_ROLES:
            continue
        locus = path_ref.workspace_relative_path or path_ref.display_path
        _add(ExpectationKind.NAMED_LOCUS, locus, path_ref.clause)

    # expected_output: inline backtick literals shown as desired output — excluding
    # commands (already command criteria), paths (already loci) and bare identifiers
    # (code references, never runtime output).
    for text in texts:
        for match in _BACKTICK_COMMAND_RE.finditer(str(text or "")):
            span = match.group(1).strip()
            if len(span) < MIN_EXPECTED_OUTPUT_LITERAL_LEN:
                continue
            context = str(text)[max(0, match.start() - 80) : match.start()]
            if _looks_like_command(span, context=context):
                continue
            if _expectation_span_is_path_like(span):
                continue
            if _expectation_span_is_symbol_like(span):
                continue
            _add(
                ExpectationKind.EXPECTED_OUTPUT,
                span,
                _expectation_source_window(text, match.start(), match.end()),
            )

    return expectations[:MAX_EXPECTATIONS]


def _extract_thresholds(texts: list[str]) -> list[AcceptanceThreshold]:
    thresholds: list[AcceptanceThreshold] = []
    for text in texts:
        for match in _THRESHOLD_RE.finditer(text):
            if _context_is_suggestion(text[: match.start()]):
                continue
            operator = match.group("op").casefold()
            operator = {
                "at least": ">=",
                "above": ">",
                "over": ">",
                "more than": ">",
                "at most": "<=",
                "under": "<",
                "below": "<",
                "less than": "<",
            }.get(operator, operator)
            thresholds.append(
                AcceptanceThreshold(
                    metric=match.group("metric").casefold(),
                    operator=operator,
                    value=float(match.group("value")),
                    unit=str(match.group("unit") or ""),
                )
            )
    return thresholds[:8]


def _extract_ports(texts: list[str]) -> list[int]:
    ports: list[int] = []
    seen: set[int] = set()
    for text in texts:
        for match in _PORT_RE.finditer(text):
            if _context_is_suggestion(text[: match.start()]):
                continue
            port = int(match.group(1))
            if 0 < port <= 65535 and port not in seen:
                seen.add(port)
                ports.append(port)
    return ports[:8]


def _extract_formats(texts: list[str]) -> list[str]:
    formats: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for match in _FORMAT_RE.finditer(text):
            value = match.group(1).casefold()
            if value in seen:
                continue
            seen.add(value)
            formats.append(value)
    return formats[:8]


def _residual_functional_requirement(texts: list[str]) -> str:
    combined = " ".join(" ".join(text.split()) for text in texts if text.strip())
    if not combined:
        return ""
    return combined[:280]


def _path_appears_preserved(path: str, texts: list[str]) -> bool:
    for text in texts:
        for match in _PRESERVE_RE.finditer(text):
            if path in _extract_paths(match.group(0)):
                return True
    return False


def _planning_constraint_criteria(
    existing: list[AcceptanceCriterion],
    planning_constraints: Any | None,
) -> list[AcceptanceCriterion]:
    out: list[AcceptanceCriterion] = []
    if planning_constraints is None:
        return out
    for attr in ("forbidden_roots", "decoy_roots", "unrelated_roots"):
        for item in getattr(planning_constraints, attr, ()) or ():
            path = _normalize_rel_path(str(getattr(item, "path", "") or ""))
            if not path:
                continue
            out.append(
                _criterion(
                    [*existing, *out],
                    kind=AcceptanceCriterionKind.PRESERVATION_UNCHANGED_PATH,
                    source=AcceptanceCriterionSource.PLANNING_CONSTRAINT,
                    description=f"Planning constraint preserves blocked scope: {path}",
                    paths=(path,),
                )
            )
    return out


def _iter_bounded_existing_paths(root: Path) -> list[str]:
    out: list[str] = []
    skip = {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "__pycache__",
        "node_modules",
        "target",
        ".pytest_cache",
        ".ruff_cache",
    }
    stack = [(root, 0)]
    while stack and len(out) < 600:
        current, depth = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name in skip:
                continue
            try:
                rel = entry.relative_to(root).as_posix()
            except ValueError:
                continue
            out.append(rel)
            if entry.is_dir() and depth < 3:
                stack.append((entry, depth + 1))
            if len(out) >= 600:
                break
    return out


def _normalize_rel_path(path: str) -> str:
    cleaned = str(path or "").strip().replace("\\", "/")
    cleaned = cleaned.rstrip(".,;:!?)]}")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def _looks_like_explicit_artifact_path(path: str) -> bool:
    pure = PurePosixPath(path)
    name = pure.name
    if not name:
        return False
    if "." in name:
        return True
    return name in {
        "Dockerfile",
        "Gemfile",
        "Makefile",
        "Procfile",
        "Rakefile",
    }


def _normalize_command(command: str) -> str:
    # Whitespace inside quoted arguments or inline code is significant.
    return str(command or "").strip()


def _python_interpreter_snippet_command(command: str, *, context: str) -> str:
    if not _PYTHON_SNIPPET_INTRO_RE.search(context or ""):
        return ""
    if "\n" in command or "\r" in command:
        return ""
    lowered = command.casefold()
    if not (
        lowered.startswith(("from ", "import ", "assert "))
        or "; assert " in lowered
        or lowered.startswith(("print(", "raise "))
    ):
        return ""
    try:
        ast.parse(command, mode="exec")
    except SyntaxError:
        return ""
    return "python -c " + shlex.quote(command)


def _looks_like_command(command: str, *, context: str = "") -> bool:
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts:
        return False
    direct_intro = _has_direct_command_intro(context)
    # A path or API name in a sentence is data, regardless of an earlier 'run'.
    if len(parts) == 1:
        if command.endswith("()"):
            return False
        return direct_intro
    head = Path(parts[0]).name.casefold()
    if head in _COMMAND_HEADS:
        return True
    return direct_intro


def _has_direct_command_intro(context: str) -> bool:
    clause = re.split(r"[.!?](?=\s|$)|[;\n]", context or "")[-1]
    coordinated = re.fullmatch(
        r"(?P<prefix>.*)`[^`\r\n]+`\s*(?:,\s*(?:and\s+)?|and\s+)\s*",
        clause,
        re.I,
    )
    if coordinated:
        # Only a bare quoted list inherits its introduction. Intervening prose
        # such as 'inspect' or 'write' ends the command relationship.
        return _has_direct_command_intro(coordinated.group("prefix"))
    if _context_negates_command(clause):
        return False
    if re.search(r"\b(?:old|previous|formerly|historical|document|describe)\b", clause, re.I):
        return False
    return bool(
        re.search(
            r"\b(?:run|execute|(?:test|verify|validate|check) (?:with|using)|"
            r"install with|using command|acceptance check|verification command|command)"
            r"\s*(?:(?:the|this)\s+)?(?:command\s*)?[:=]?\s*$",
            clause,
            re.I,
        )
    )


def _context_negates_command(context: str) -> bool:
    # Inspect only the immediate introduction, never the quoted command body
    # or a previous clause's prohibition of a different command.
    return bool(
        re.search(
            r"\b(?:do\s+not|don['’]t|never|without)\s+(?:ever\s+)?"
            r"(?:run(?:ning)?|execut(?:e|ing)|(?:test|verify|validate|check)(?:ing)?"
            r"(?:\s+(?:with|using))?)\s*"
            r"(?:(?:the|this)\s+)?(?:command\s*)?[:=]?\s*$",
            context,
            re.I,
        )
    )


def _context_is_suggestion(context: str) -> bool:
    clause = re.split(r"[.!?](?=\s|$)|[;\n]", context or "")[-1]
    return bool(
        re.search(r"\b(?:could|might|optionally|consider|suggest|for example)\b", clause, re.I)
    )


def _is_test_or_checker_path(path: str) -> bool:
    pure = PurePosixPath(path)
    parts = {part.casefold() for part in pure.parts}
    name = pure.name.casefold()
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith(".test.js")
        or name.endswith(".spec.js")
        or bool(parts & _CHECK_PATH_MARKERS)
    )


def _is_checker_path(path: str) -> bool:
    pure = PurePosixPath(path)
    parts = {part.casefold() for part in pure.parts}
    stem = pure.stem.casefold()
    return bool(parts & {"checks", "verify", "validation"}) or any(
        marker in stem for marker in ("check", "verify", "validate")
    )


def _is_self_authored_check_path(path: str, contract: AcceptanceContract) -> bool:
    normalized = _normalize_rel_path(path)
    return _is_test_or_checker_path(normalized) and (
        normalized not in contract.snapshot.preexisting_test_paths
        and normalized not in contract.snapshot.preexisting_checker_paths
    )


def _command_references_self_authored_check(command: str, contract: AcceptanceContract) -> bool:
    return any(
        _is_self_authored_check_path(path, contract) for path in _command_path_tokens(command)
    )


def _command_references_preexisting_checker(command: str, contract: AcceptanceContract) -> bool:
    return any(
        path in contract.snapshot.preexisting_test_paths
        or path in contract.snapshot.preexisting_checker_paths
        for path in _command_path_tokens(command)
    )


def _command_references_mutable_preexisting_checker(
    command: str,
    *,
    contract: AcceptanceContract,
    root: Path | None,
) -> bool:
    if root is None:
        return False
    fingerprints = {
        _normalize_rel_path(item.display_path): item
        for item in contract.snapshot.preexisting_checker_fingerprints
    }
    for path in _command_path_tokens(command):
        if path not in contract.snapshot.preexisting_checker_paths:
            continue
        before = fingerprints.get(path)
        if before is None:
            return True
        after = _fingerprint_checker_path(root=root, relpath=path)
        if (
            before.resolved_path != after.resolved_path
            or before.is_regular_file != after.is_regular_file
            or before.size != after.size
            or before.sha256 != after.sha256
        ):
            return True
    return False


def _fingerprint_checker_path(
    *,
    root: Path,
    relpath: str,
    max_bytes: int = 2_000_000,
) -> CheckerEntrypointFingerprint:
    display_path = _normalize_rel_path(relpath)
    candidate = (root / display_path).resolve(strict=False)
    try:
        resolved = candidate.resolve(strict=True)
        stat = resolved.stat()
    except OSError:
        return CheckerEntrypointFingerprint(
            display_path=display_path,
            resolved_path=str(candidate),
            is_regular_file=False,
            size=None,
            sha256=None,
        )
    if not resolved.is_file() or stat.st_size > max_bytes:
        return CheckerEntrypointFingerprint(
            display_path=display_path,
            resolved_path=str(resolved),
            is_regular_file=resolved.is_file(),
            size=int(stat.st_size),
            sha256=None,
        )
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return CheckerEntrypointFingerprint(
        display_path=display_path,
        resolved_path=str(resolved),
        is_regular_file=True,
        size=int(stat.st_size),
        sha256=digest.hexdigest(),
    )


def _command_path_tokens(command: str) -> list[str]:
    try:
        parts = shlex.split(command)
    except ValueError:
        return []
    return [
        _normalize_rel_path(part)
        for part in parts
        if "/" in part or "." in PurePosixPath(part).name
    ]


def _is_direct_black_box_command(command: str) -> bool:
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts:
        return False
    return Path(parts[0]).name.casefold() in {"diff", "cmp", "curl"}


def _observed_command(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> str:
    if tool_name == "verify_run":
        commands = result.get("commands")
        if isinstance(commands, list) and len(commands) == 1:
            return _normalize_command(str(commands[0]))
        return ""
    if tool_name == "shell_run":
        return _normalize_command(str(result.get("effective_cmd") or arguments.get("cmd") or ""))
    return ""


def _evidentiary_capability_verdicts(command: str) -> frozenset[str]:
    """Reasons that only say what ``command`` can prove, never how it ran.

    These are the verdicts the command analysis gives for this exact command
    when it is not an assertive check (unknown, non-assertive, or a shape that
    cannot assert by itself). An assertive command has none, so every refusal
    of it is about the run. Reasons such as a mutation, another working
    directory, zero tests, an execution-layer failure, a missing pipeline
    status or an unmatched command are never capability verdicts.
    """
    analysis = analyze_verification_command(command, trusted=True)
    if (
        analysis.evidentiary_capability == VerificationCommandEvidentiaryCapability.ASSERTIVE
        and not analysis.rejection_reason
    ):
        return frozenset()
    return frozenset(
        {analysis.rejection_reason, analysis.inconclusive_reason, analysis.capability_reason} - {""}
    )


def _command_passed(*, status: str, result: dict[str, Any]) -> bool | None:
    if status == "failed":
        return False
    if str(result.get("status") or "") == "not_run":
        # Tri-state: a verify run where nothing executed proves nothing —
        # neither pass nor fail.
        return None
    if "all_passed" in result:
        return bool(result.get("all_passed"))
    exit_code = result.get("exit_code")
    if isinstance(exit_code, int):
        return exit_code == 0
    return None


def _tool_output(result: dict[str, Any]) -> str:
    parts = [
        str(result.get("stdout") or ""),
        str(result.get("stderr") or ""),
        str(result.get("output") or ""),
        str(result.get("output_preview") or ""),
    ]
    command_results = result.get("command_results")
    if isinstance(command_results, list):
        for item in command_results:
            if isinstance(item, dict):
                parts.append(str(item.get("output_preview") or item.get("output") or ""))
    return "\n".join(part for part in parts if part)


def _update_command_and_threshold_criteria(
    *,
    contract: AcceptanceContract,
    command: str,
    output: str,
    passed: bool | None,
    origin: EvidenceOrigin,
    evidence_allowed: bool | None = None,
    explicit_command_allowed: bool | None = None,
    host_covered_commands: tuple[str, ...] = (),
) -> list[str]:
    matched: list[str] = []
    if explicit_command_allowed is None:
        explicit_command_allowed = evidence_allowed
    for criterion in contract.criteria:
        if criterion.kind == AcceptanceCriterionKind.EXPLICIT_COMMAND_IO and any(
            _commands_equivalent(observed, candidate)
            for observed in (command, *host_covered_commands)
            for candidate in criterion.commands
        ):
            status = (
                AcceptanceCriterionStatus.BLOCKED
                if passed is True and explicit_command_allowed is False
                else AcceptanceCriterionStatus.PASSED
                if passed is True
                else AcceptanceCriterionStatus.FAILED
                if passed is False
                else AcceptanceCriterionStatus.UNVERIFIED
            )
            criterion.status = status
            criterion.failure_summary = ""
            if status == AcceptanceCriterionStatus.FAILED:
                criterion.failure_summary = f"Explicit command failed: {command}"
            elif status == AcceptanceCriterionStatus.BLOCKED:
                criterion.failure_summary = "Verification evidence was supplemental or unsafe"
            matched.append(criterion.criterion_id)
        elif criterion.kind == AcceptanceCriterionKind.THRESHOLD:
            if passed is True and evidence_allowed is False:
                continue
            status, summary = _evaluate_thresholds(
                thresholds=criterion.thresholds,
                output=output,
                passed=passed,
                origin=origin,
            )
            if status is not None:
                _apply_status_from_evidence(
                    criterion=criterion,
                    status=status,
                    summary=summary,
                    origin=origin,
                )
                matched.append(criterion.criterion_id)
        elif criterion.kind == AcceptanceCriterionKind.PERSISTENT_SERVICE and passed is True:
            criterion.status = AcceptanceCriterionStatus.BLOCKED
            criterion.failure_summary = (
                "Same-session command succeeded but does not prove durable service lifetime"
            )
            matched.append(criterion.criterion_id)
        elif (
            criterion.kind == AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH
            and passed is True
            and evidence_allowed is not False
            and origin
            in {
                EvidenceOrigin.HOST_AUTHORITATIVE,
                EvidenceOrigin.USER_EXPLICIT,
                EvidenceOrigin.PREEXISTING_TASK_CHECKER,
                EvidenceOrigin.DIRECT_BLACK_BOX,
            }
            and _external_or_unresolved_output_is_mentioned(
                criterion=criterion,
                command=command,
                output=output,
            )
        ):
            criterion.status = AcceptanceCriterionStatus.PASSED
            criterion.failure_summary = ""
            matched.append(criterion.criterion_id)
    return matched


def _update_durable_service_criteria(
    *,
    contract: AcceptanceContract,
    result: dict[str, Any],
) -> list[str]:
    matched: list[str] = []
    service_id = str(result.get("service_id") or "").strip()
    durable_payload = str(result.get("ownership") or "") == "DURABLE_SERVICE" and service_id
    if not durable_payload:
        return matched
    for criterion in contract.criteria:
        if criterion.kind == AcceptanceCriterionKind.FUNCTIONAL_API_PROTOCOL:
            if not criterion.ports:
                continue
        elif criterion.kind != AcceptanceCriterionKind.PERSISTENT_SERVICE:
            continue
        if service_id not in criterion.service_ids:
            criterion.service_ids.append(service_id)
        if _durable_service_satisfies_criterion(criterion=criterion, payload=result):
            criterion.status = AcceptanceCriterionStatus.PASSED
            criterion.failure_summary = ""
        else:
            criterion.status = AcceptanceCriterionStatus.BLOCKED
            criterion.failure_summary = _durable_service_failure_summary(result)
        matched.append(criterion.criterion_id)
    return matched


def _block_session_owned_service_criteria(*, contract: AcceptanceContract) -> list[str]:
    matched: list[str] = []
    for criterion in contract.criteria:
        if criterion.kind != AcceptanceCriterionKind.PERSISTENT_SERVICE:
            continue
        criterion.status = AcceptanceCriterionStatus.BLOCKED
        criterion.failure_summary = (
            "shell_background is session-owned and is reaped on AgentSession.close; "
            "use shell_service_start for durable-service evidence"
        )
        matched.append(criterion.criterion_id)
    return matched


def _update_repo_surface_criteria(
    *,
    contract: AcceptanceContract,
    command: str,
    passed: bool | None,
    evidence_allowed: bool | None,
    known_verification_commands: list[str] | None,
    origin: EvidenceOrigin,
    host_covered_commands: tuple[str, ...] = (),
) -> list[str]:
    matched: list[str] = []
    if not command:
        return matched
    for criterion in contract.criteria:
        if criterion.kind not in {
            AcceptanceCriterionKind.PREEXISTING_REPO_CHECK_SURFACE,
            AcceptanceCriterionKind.EXPLICIT_HOST_USER_VERIFICATION_COMMAND,
        }:
            continue
        commands = criterion.commands or tuple(known_verification_commands or ())
        if not any(
            _commands_equivalent(observed, candidate)
            for observed in (command, *host_covered_commands)
            for candidate in commands
        ):
            continue
        if origin == EvidenceOrigin.SELF_AUTHORED:
            criterion.status = AcceptanceCriterionStatus.BLOCKED
            criterion.failure_summary = (
                "Self-authored or mutable_authoritative_checker evidence is supplemental "
                "for this criterion"
            )
        elif evidence_allowed is False:
            criterion.status = AcceptanceCriterionStatus.BLOCKED
            criterion.failure_summary = "Verification evidence was supplemental or unsafe"
        elif passed is True:
            criterion.status = AcceptanceCriterionStatus.PASSED
            criterion.failure_summary = ""
        elif passed is False:
            criterion.status = AcceptanceCriterionStatus.FAILED
            criterion.failure_summary = f"Verification command failed: {command}"
        matched.append(criterion.criterion_id)
    return matched


def _workspace_paths_for_criterion(criterion: AcceptanceCriterion) -> tuple[str, ...]:
    paths: list[str] = []
    if criterion.path_refs:
        for path_ref in criterion.path_refs:
            if path_ref.workspace_relative_path:
                paths.append(path_ref.workspace_relative_path)
        return tuple(dict.fromkeys(paths))
    return tuple(path for path in criterion.paths if path and not path.startswith("/"))


def _required_output_paths(
    criterion: AcceptanceCriterion,
    root: Path,
    external_path_observations: dict[str, str] | None = None,
    observer: ArtifactOutputObserver | None = None,
) -> tuple[str, ...]:
    """Admit attached or observed exact outputs for metadata checks, never reads."""
    from ..host_file_policy import HostFilePolicy, HostFilePolicyError
    from ..project_folders import allowed_roots

    paths = list(_workspace_paths_for_criterion(criterion))
    folders = allowed_roots(root)[1:]
    if folders or external_path_observations:
        policy = HostFilePolicy.capture()
        for ref in criterion.path_refs:
            if ref.path_kind != AcceptancePathKind.ABSOLUTE_EXTERNAL:
                continue
            if observer is not None and ref.absolute_path in observer.current:
                paths.append(ref.absolute_path)
                continue
            target = Path(ref.absolute_path)
            if not target.is_absolute():
                continue
            observed = (external_path_observations or {}).get(ref.absolute_path)
            if not (
                any(target.resolve().is_relative_to(folder) for folder in folders)
                or (observed is not None and str(target.resolve()) == observed)
            ):
                continue
            try:
                policy.guard_read(root, str(target))
            except (HostFilePolicyError, OSError):
                continue
            # Keep the reference's normalized spelling for the membership checks
            # below. str(Path(...)) changes '/' to '\\' on Windows, making an
            # admitted, observed output look like a different untrusted path.
            paths.append(ref.absolute_path)
    return tuple(dict.fromkeys(paths))


def _missing_required_output_paths(
    *,
    criterion: AcceptanceCriterion,
    root: Path,
    external_path_observations: dict[str, str] | None = None,
    observer: ArtifactOutputObserver | None = None,
) -> list[str]:
    missing: list[str] = []
    workspace_paths = _required_output_paths(criterion, root, external_path_observations, observer)
    for path in workspace_paths:
        # An enrolled output has a bounded host observation. Do not fall back
        # to synchronous I/O after that observation timed out or was cancelled.
        bound_ref = next(
            (
                ref
                for ref in criterion.path_refs
                if ref.workspace_relative_path == path or ref.absolute_path == path
            ),
            None,
        )
        observed = (
            observer.current.get(bound_ref.absolute_path)
            if observer is not None and bound_ref is not None
            else None
        )
        if observed is not None:
            allowed_states = (
                {"file"}
                if bound_ref.path_kind == AcceptancePathKind.ABSOLUTE_EXTERNAL
                else {"file", "directory"}
            )
            if observed.state not in allowed_states:
                missing.append(path)
        elif not (root / path).exists():
            missing.append(path)
    external_refs = [
        path_ref
        for path_ref in criterion.path_refs
        if path_ref.path_kind == AcceptancePathKind.ABSOLUTE_EXTERNAL
        and path_ref.role == AcceptancePathRole.REQUIRED_OUTPUT
        and path_ref.absolute_path not in workspace_paths
    ]
    for path_ref in external_refs:
        observed = observer.current.get(path_ref.absolute_path) if observer is not None else None
        if observed is not None:
            if observed.state != "file":
                missing.append(f"{path_ref.display_path} ({observed.state})")
        elif criterion.status != AcceptanceCriterionStatus.PASSED or path_ref.absolute_path in (
            external_path_observations or {}
        ):
            missing.append(f"{path_ref.display_path} (external output requires trusted evidence)")
    unresolved_refs = [
        path_ref
        for path_ref in criterion.path_refs
        if path_ref.path_kind == AcceptancePathKind.UNRESOLVED
        and path_ref.role == AcceptancePathRole.REQUIRED_OUTPUT
    ]
    for path_ref in unresolved_refs:
        if criterion.status != AcceptanceCriterionStatus.PASSED:
            missing.append(f"{path_ref.display_path} (unresolved output path)")
    if not criterion.path_refs:
        missing.extend(path for path in criterion.paths if path and not (root / path).exists())
    return list(dict.fromkeys(missing))


def _update_path_criteria(
    *,
    contract: AcceptanceContract,
    root: Path,
    touched_paths: set[str],
    status: str,
    external_path_observations: dict[str, str] | None = None,
) -> list[str]:
    matched: list[str] = []
    for criterion in contract.criteria:
        if criterion.kind != AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH:
            continue
        paths = _required_output_paths(criterion, root, external_path_observations)
        if not paths:
            continue
        if any(
            ref.path_kind == AcceptancePathKind.ABSOLUTE_EXTERNAL and ref.absolute_path not in paths
            for ref in criterion.path_refs
        ):
            continue
        if all((root / path).exists() for path in paths):
            criterion.status = AcceptanceCriterionStatus.PASSED
            matched.append(criterion.criterion_id)
        elif status == "failed" and any(_path_matches_any(path, paths) for path in touched_paths):
            criterion.status = AcceptanceCriterionStatus.FAILED
            criterion.failure_summary = "Attempted output path update failed"
            matched.append(criterion.criterion_id)
    return matched


def _finalize_persistent_service_criterion(
    *,
    criterion: AcceptanceCriterion,
    durable_service_status: Callable[[str], dict[str, Any]] | None,
) -> None:
    if durable_service_status is None:
        criterion.status = AcceptanceCriterionStatus.BLOCKED
        criterion.failure_summary = "Durable service status recheck is unavailable"
        return
    failures: list[str] = []
    for service_id in list(dict.fromkeys(criterion.service_ids)):
        try:
            payload = durable_service_status(service_id)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{service_id}: status recheck failed: {exc}")
            continue
        if _durable_service_satisfies_criterion(criterion=criterion, payload=payload):
            criterion.status = AcceptanceCriterionStatus.PASSED
            criterion.failure_summary = ""
            return
        failures.append(f"{service_id}: {_durable_service_failure_summary(payload)}")
    criterion.status = AcceptanceCriterionStatus.BLOCKED
    criterion.failure_summary = "Durable service readiness recheck failed: " + "; ".join(
        failures[:4]
    )


def record_consumer_profile_observation(
    *,
    contract: AcceptanceContract | None,
    report: dict[str, Any],
    generation: int,
) -> None:
    """Retain proposed consumer observations without promoting their assertions."""
    if contract is None:
        return
    contract.add_evidence(
        origin=EvidenceOrigin.SELF_AUTHORED,
        summary=f"Fresh {report.get('profile', 'consumer')} profile: {report.get('status', 'unknown')}; supplemental requirements review needed",
        passed=report.get("passed") is True,
        paths=tuple(sorted(str(path) for path in report.get("artifact_hashes", {}))),
        category="consumer_profile",
        task_id=contract.task_id,
        generation=generation,
        tool_name="verify_run",
        result_id=str(report.get("result_id") or ""),
        evidence_allowed=False,
    )


def _durable_service_satisfies_criterion(
    *,
    criterion: AcceptanceCriterion,
    payload: dict[str, Any],
) -> bool:
    if str(payload.get("ownership") or "") != "DURABLE_SERVICE":
        return False
    if str(payload.get("status") or "").casefold() != "running":
        return False
    if payload.get("alive") is not True:
        return False
    if payload.get("identity_valid") is not True:
        return False
    readiness = payload.get("readiness") if isinstance(payload.get("readiness"), dict) else {}
    if str(readiness.get("status") or "").casefold() != "ready":
        return False
    if readiness.get("endpoint_owned") is not True:
        return False
    if readiness.get("strength") != "owned_endpoint":
        return False
    if criterion.ports:
        if str(readiness.get("type") or "").casefold() != "tcp":
            return False
        try:
            port = int(readiness.get("port") or 0)
        except (TypeError, ValueError):
            return False
        if port not in set(criterion.ports):
            return False
    return True


def _durable_service_failure_summary(payload: dict[str, Any]) -> str:
    service_id = str(payload.get("service_id") or "?")
    status = str(payload.get("status") or "?")
    alive = payload.get("alive")
    readiness = payload.get("readiness") if isinstance(payload.get("readiness"), dict) else {}
    readiness_status = str(readiness.get("status") or "?")
    readiness_type = str(readiness.get("type") or "?")
    detail = str(readiness.get("detail") or "").strip()
    suffix = f": {detail}" if detail else ""
    return (
        f"Durable service {service_id} is not ready "
        f"(status={status}, alive={alive}, readiness={readiness_type}/{readiness_status})"
        f"{suffix}"
    )


def _evaluate_thresholds(
    *,
    thresholds: tuple[AcceptanceThreshold, ...],
    output: str,
    passed: bool | None,
    origin: EvidenceOrigin,
) -> tuple[AcceptanceCriterionStatus | None, str]:
    if not thresholds:
        return None, ""
    numbers = [float(match.group(0)) for match in _NUMBER_RE.finditer(output or "")]
    if not numbers:
        if passed is False:
            return AcceptanceCriterionStatus.FAILED, "Threshold command failed"
        return None, ""
    threshold = thresholds[0]
    measured = _median(numbers)
    if (
        threshold.metric in {"latency", "runtime", "time", "duration", "performance"}
        and len(numbers) < 2
    ):
        return (
            AcceptanceCriterionStatus.BLOCKED,
            "Performance threshold has insufficient repeated samples",
        )
    ok = _compare(measured, threshold.operator, threshold.value)
    if ok:
        if origin == EvidenceOrigin.SELF_AUTHORED:
            return (
                AcceptanceCriterionStatus.BLOCKED,
                "Self-authored threshold evidence is supplemental without independent coverage",
            )
        return AcceptanceCriterionStatus.PASSED, ""
    return (
        AcceptanceCriterionStatus.FAILED,
        f"Measured {threshold.metric} {measured:g}{threshold.unit} misses "
        f"{threshold.operator} {threshold.value:g}{threshold.unit}",
    )


def _external_or_unresolved_output_is_mentioned(
    *,
    criterion: AcceptanceCriterion,
    command: str,
    output: str,
) -> bool:
    haystack = f"{command}\n{output}".casefold()
    for path_ref in criterion.path_refs:
        if path_ref.path_kind not in {
            AcceptancePathKind.ABSOLUTE_EXTERNAL,
            AcceptancePathKind.UNRESOLVED,
        }:
            continue
        if path_ref.display_path.casefold() in haystack or path_ref.raw_text.casefold() in haystack:
            return True
    return False


def _compare(measured: float, operator: str, target: float) -> bool:
    if operator == ">=":
        return measured >= target
    if operator == ">":
        return measured > target
    if operator == "<=":
        return measured <= target
    if operator == "<":
        return measured < target
    return False


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _apply_status_from_evidence(
    *,
    criterion: AcceptanceCriterion,
    status: AcceptanceCriterionStatus,
    summary: str,
    origin: EvidenceOrigin,
) -> None:
    if (
        criterion.status == AcceptanceCriterionStatus.FAILED
        and origin == EvidenceOrigin.SELF_AUTHORED
        and status != AcceptanceCriterionStatus.FAILED
    ):
        return
    criterion.status = status
    criterion.failure_summary = summary


def _finalize_content_format_criterion(
    *,
    criterion: AcceptanceCriterion,
    root: Path,
) -> None:
    if not criterion.required:
        if criterion.status == AcceptanceCriterionStatus.UNVERIFIED:
            criterion.status = AcceptanceCriterionStatus.NOT_APPLICABLE
        return
    if not criterion.paths:
        criterion.status = AcceptanceCriterionStatus.BLOCKED
        criterion.failure_summary = "Format/schema requirement has no concrete output path"
        return
    paths = _workspace_paths_for_criterion(criterion)
    external = [
        path_ref.display_path
        for path_ref in criterion.path_refs
        if path_ref.path_kind == AcceptancePathKind.ABSOLUTE_EXTERNAL
    ]
    if external and not paths:
        if criterion.enforcement == AcceptanceCriterionEnforcement.HARD:
            criterion.status = AcceptanceCriterionStatus.BLOCKED
            criterion.failure_summary = (
                "External output format requires trusted command evidence: " + ", ".join(external)
            )
        elif criterion.status == AcceptanceCriterionStatus.UNVERIFIED:
            criterion.status = AcceptanceCriterionStatus.NOT_APPLICABLE
        return
    missing = [path for path in paths if not (root / path).exists()]
    if missing:
        criterion.status = AcceptanceCriterionStatus.UNVERIFIED
        criterion.failure_summary = "Format output path is missing: " + ", ".join(missing)
        return
    lowered = criterion.description.casefold()
    if "json" in lowered:
        invalid: list[str] = []
        for path in paths:
            candidate = root / path
            try:
                json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                invalid.append(path)
        if invalid:
            criterion.status = AcceptanceCriterionStatus.FAILED
            criterion.failure_summary = "Invalid JSON output: " + ", ".join(invalid)
            return
    if criterion.status == AcceptanceCriterionStatus.UNVERIFIED:
        criterion.status = AcceptanceCriterionStatus.PASSED


def _commands_equivalent(left: str, right: str) -> bool:
    left_norm = _normalize_command(left)
    right_norm = _normalize_command(right)
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    return bool(
        _matching_effective_verification_commands(
            observed_command=left_norm,
            effective_verification_commands=[right_norm],
        )
    )


def _evidence_summary(*, command: str, touched_paths: set[str]) -> str:
    if command:
        return f"Executed command: {command}"
    if touched_paths:
        return "Touched paths: " + ", ".join(sorted(touched_paths)[:8])
    return "Observed tool result"


def _path_matches_any(path: str, roots: tuple[str, ...]) -> bool:
    normalized = _normalize_rel_path(path).casefold()
    for root in roots:
        root_norm = _normalize_rel_path(root).casefold()
        if normalized == root_norm or normalized.startswith(root_norm.rstrip("/") + "/"):
            return True
    return False


def _path_is_material_for_scope(path: str, *, root: Path) -> bool:
    normalized = _normalize_rel_path(path)
    return bool(normalized) and not is_runtime_artifact_path(normalized, root=root)
