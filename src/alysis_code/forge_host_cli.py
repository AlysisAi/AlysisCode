"""CLI presentation helpers for the canonical local Forge Host generation.

This module does not plan, approve, verify, or execute work.  It persists the
minimum non-secret authority needed to re-admit a completed plan in a later CLI
process and drives exact Host-v2 requests through :class:`ForgeHostInProcessClient`.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .atomic_io import atomic_write_json
from .forge import RunPaths
from .forge_host_client import (
    ForgeHostInProcessClient,
    PlannedHostGeneration,
    SubmittedHostCommand,
)
from .forge_host_native_adapter import IntentKind, ScopeKind
from .forge_host_protocol.models import (
    ApprovalDecisionValue,
    ApprovalRequired,
    EventEnvelope,
    EvidenceReady,
    PlannerQuestion,
    PlanReady,
    RunProgress,
    RunTerminal,
)
from .forge_host_runtime import LocalForgeHostRuntime

_RESUME_SCHEMA = "alysis.forge-host.local-resume/v1"
_RESUME_FILENAME = "forge-host-v2-resume.json"
_MAX_RESUME_BYTES = 64 * 1024
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_INTENT_KINDS = frozenset({"build", "fix", "refactor", "audit", "test", "document"})
_SCOPE_KINDS = frozenset({"file", "directory"})
_INTENT_VERBS: dict[IntentKind, frozenset[str]] = {
    "build": frozenset({"add", "build", "create", "implement", "introduce"}),
    "fix": frozenset({"correct", "fix", "patch", "repair", "resolve"}),
    "refactor": frozenset({"refactor", "reorganize", "restructure"}),
    "audit": frozenset({"analyze", "audit", "inspect", "investigate", "review"}),
    "test": frozenset({"check", "test", "verify"}),
    "document": frozenset({"document", "documenting", "docs"}),
}


class ForgeHostCliError(RuntimeError):
    """Stable local presentation error without provider or filesystem detail."""


@dataclass(frozen=True, slots=True)
class ForgeHostResumeManifest:
    native_run_id: str
    planner_input: str
    intent_kind: IntentKind | None
    per_managed_task_max_steps: int
    run_timeout_ms: int
    verification_commands: tuple[str, ...]
    declared_scope_kinds: tuple[tuple[str, ScopeKind], ...]
    host_plan_fingerprint: str
    native_approval_fingerprint: str

    @classmethod
    def from_plan_ready(
        cls,
        *,
        paths: RunPaths,
        planner_input: str,
        plan_ready: PlanReady,
        intent_kind: IntentKind | None,
        verification_commands: Sequence[str],
        declared_scope_kinds: Mapping[str, ScopeKind],
    ) -> ForgeHostResumeManifest:
        return cls._validated(
            native_run_id=paths.run_id,
            planner_input=planner_input,
            intent_kind=intent_kind,
            per_managed_task_max_steps=(plan_ready.execution_admission.per_managed_task_max_steps),
            run_timeout_ms=plan_ready.execution_admission.run_timeout_ms,
            verification_commands=tuple(verification_commands),
            declared_scope_kinds=tuple(sorted(declared_scope_kinds.items())),
            host_plan_fingerprint=plan_ready.host_plan_fingerprint,
            native_approval_fingerprint=plan_ready.native_approval_fingerprint,
        )

    @classmethod
    def from_payload(cls, value: object) -> ForgeHostResumeManifest:
        if not isinstance(value, dict):
            raise ForgeHostCliError("The local Forge resume record is invalid.")
        expected = {
            "schema",
            "nativeRunId",
            "plannerInput",
            "intentKind",
            "perManagedTaskMaxSteps",
            "runTimeoutMs",
            "verificationCommands",
            "declaredScopeKinds",
            "hostPlanFingerprint",
            "nativeApprovalFingerprint",
        }
        if set(value) != expected or value.get("schema") != _RESUME_SCHEMA:
            raise ForgeHostCliError("The local Forge resume record is invalid.")
        declared = value.get("declaredScopeKinds")
        if not isinstance(declared, dict):
            raise ForgeHostCliError("The local Forge resume record is invalid.")
        commands = value.get("verificationCommands")
        if not isinstance(commands, list) or not all(isinstance(item, str) for item in commands):
            raise ForgeHostCliError("The local Forge resume record is invalid.")
        intent = value.get("intentKind")
        if intent is not None and not isinstance(intent, str):
            raise ForgeHostCliError("The local Forge resume record is invalid.")
        return cls._validated(
            native_run_id=value.get("nativeRunId"),
            planner_input=value.get("plannerInput"),
            intent_kind=cast(IntentKind | None, intent),
            per_managed_task_max_steps=value.get("perManagedTaskMaxSteps"),
            run_timeout_ms=value.get("runTimeoutMs"),
            verification_commands=tuple(commands),
            declared_scope_kinds=tuple(sorted(declared.items())),
            host_plan_fingerprint=value.get("hostPlanFingerprint"),
            native_approval_fingerprint=value.get("nativeApprovalFingerprint"),
        )

    @classmethod
    def _validated(
        cls,
        *,
        native_run_id: object,
        planner_input: object,
        intent_kind: IntentKind | None,
        per_managed_task_max_steps: object,
        run_timeout_ms: object,
        verification_commands: tuple[str, ...],
        declared_scope_kinds: tuple[tuple[object, object], ...],
        host_plan_fingerprint: object,
        native_approval_fingerprint: object,
    ) -> ForgeHostResumeManifest:
        run_id = native_run_id if isinstance(native_run_id, str) else ""
        planner = planner_input.strip() if isinstance(planner_input, str) else ""
        if not _RUN_ID.fullmatch(run_id) or not planner or len(planner) > 4096:
            raise ForgeHostCliError("The local Forge resume record is invalid.")
        if intent_kind is not None and intent_kind not in _INTENT_KINDS:
            raise ForgeHostCliError("The local Forge resume record is invalid.")
        if (
            not isinstance(per_managed_task_max_steps, int)
            or isinstance(per_managed_task_max_steps, bool)
            or not 1 <= per_managed_task_max_steps <= 10_000
            or not isinstance(run_timeout_ms, int)
            or isinstance(run_timeout_ms, bool)
            or not 1_000 <= run_timeout_ms <= 86_400_000
        ):
            raise ForgeHostCliError("The local Forge resume record is invalid.")
        if verification_commands not in {("pytest",), ("pytest -q",)}:
            raise ForgeHostCliError("The local Forge resume record is invalid.")
        scopes: list[tuple[str, ScopeKind]] = []
        for raw_path, raw_kind in declared_scope_kinds:
            if (
                not isinstance(raw_path, str)
                or not raw_path
                or len(raw_path) > 4096
                or not isinstance(raw_kind, str)
                or raw_kind not in _SCOPE_KINDS
            ):
                raise ForgeHostCliError("The local Forge resume record is invalid.")
            scopes.append((raw_path, cast(ScopeKind, raw_kind)))
        if len(scopes) != len({path for path, _kind in scopes}):
            raise ForgeHostCliError("The local Forge resume record is invalid.")
        host_fp = host_plan_fingerprint if isinstance(host_plan_fingerprint, str) else ""
        native_fp = (
            native_approval_fingerprint if isinstance(native_approval_fingerprint, str) else ""
        )
        if not _FINGERPRINT.fullmatch(host_fp) or not _FINGERPRINT.fullmatch(native_fp):
            raise ForgeHostCliError("The local Forge resume record is invalid.")
        return cls(
            native_run_id=run_id,
            planner_input=planner,
            intent_kind=intent_kind,
            per_managed_task_max_steps=per_managed_task_max_steps,
            run_timeout_ms=run_timeout_ms,
            verification_commands=verification_commands,
            declared_scope_kinds=tuple(scopes),
            host_plan_fingerprint=host_fp,
            native_approval_fingerprint=native_fp,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": _RESUME_SCHEMA,
            "nativeRunId": self.native_run_id,
            "plannerInput": self.planner_input,
            "intentKind": self.intent_kind,
            "perManagedTaskMaxSteps": self.per_managed_task_max_steps,
            "runTimeoutMs": self.run_timeout_ms,
            "verificationCommands": list(self.verification_commands),
            "declaredScopeKinds": dict(self.declared_scope_kinds),
            "hostPlanFingerprint": self.host_plan_fingerprint,
            "nativeApprovalFingerprint": self.native_approval_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class ForgeHostCliPlanResult:
    planned: PlannedHostGeneration
    manifest: ForgeHostResumeManifest
    manifest_path: Path


@dataclass(frozen=True, slots=True)
class ForgeHostCliRunResult:
    planned: PlannedHostGeneration
    decision: SubmittedHostCommand
    started: SubmittedHostCommand | None


def resume_manifest_path(paths: RunPaths) -> Path:
    return paths.plan_dir / _RESUME_FILENAME


def infer_intent_kind(goal: str) -> IntentKind | None:
    text = goal.strip()
    if not text:
        return None
    first = text.split(maxsplit=1)[0].casefold().strip(".,:;!?")
    matches = [kind for kind, verbs in _INTENT_VERBS.items() if first in verbs]
    return matches[0] if len(matches) == 1 else None


def write_resume_manifest(paths: RunPaths, manifest: ForgeHostResumeManifest) -> Path:
    if manifest.native_run_id != paths.run_id:
        raise ForgeHostCliError("The local Forge resume record does not match this run.")
    target = resume_manifest_path(paths)
    try:
        if target.is_symlink():
            raise ForgeHostCliError("The local Forge resume record path is unsafe.")
        atomic_write_json(target, manifest.to_payload())
    except ForgeHostCliError:
        raise
    except OSError:
        raise ForgeHostCliError("The local Forge resume record could not be saved.") from None
    return target


def load_resume_manifest(paths: RunPaths) -> ForgeHostResumeManifest:
    target = resume_manifest_path(paths)
    try:
        metadata = target.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_RESUME_BYTES:
            raise ForgeHostCliError("The local Forge resume record is invalid.")
        raw = target.read_text(encoding="utf-8")
        manifest = ForgeHostResumeManifest.from_payload(json.loads(raw))
    except ForgeHostCliError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ForgeHostCliError("The local Forge resume record is unavailable.") from None
    if manifest.native_run_id != paths.run_id:
        raise ForgeHostCliError("The local Forge resume record does not match this run.")
    return manifest


def plan_with_runtime(
    runtime: LocalForgeHostRuntime,
    *,
    goal: str,
    answer_provider: Callable[[PlannerQuestion], str],
    intent_kind: IntentKind | None,
    verification_commands: Sequence[str],
    declared_scope_kinds: Mapping[str, ScopeKind],
) -> ForgeHostCliPlanResult:
    explicit_goal = goal.strip()
    if not explicit_goal:
        raise ForgeHostCliError("Forge planning requires an explicit goal.")
    client = ForgeHostInProcessClient(runtime.service)
    planned = client.plan_until_ready(
        explicit_goal,
        answer_provider=answer_provider,
    )
    manifest = ForgeHostResumeManifest.from_plan_ready(
        paths=runtime.paths,
        planner_input=explicit_goal,
        plan_ready=planned.plan_ready,
        intent_kind=intent_kind,
        verification_commands=verification_commands,
        declared_scope_kinds=declared_scope_kinds,
    )
    path = write_resume_manifest(runtime.paths, manifest)
    return ForgeHostCliPlanResult(planned=planned, manifest=manifest, manifest_path=path)


def run_with_existing_runtime(
    runtime: LocalForgeHostRuntime,
    *,
    manifest: ForgeHostResumeManifest,
    approval_provider: Callable[[PlanReady, ApprovalRequired], ApprovalDecisionValue],
) -> ForgeHostCliRunResult:
    client = ForgeHostInProcessClient(runtime.service)

    def unexpected_question(_question: PlannerQuestion) -> str:
        raise ForgeHostCliError("The persisted Forge plan unexpectedly requested replanning.")

    planned = client.plan_until_ready(
        "Execute the exact persisted Forge plan.",
        answer_provider=unexpected_question,
        max_questions=0,
    )
    ready = planned.plan_ready
    if (
        ready.host_plan_fingerprint != manifest.host_plan_fingerprint
        or ready.native_approval_fingerprint != manifest.native_approval_fingerprint
    ):
        raise ForgeHostCliError("The persisted Forge plan authority changed before approval.")
    approval = planned.approval_event.payload
    if not isinstance(approval, ApprovalRequired):
        raise ForgeHostCliError("Forge did not return an approval request.")
    decision_value = approval_provider(ready, approval)
    if decision_value not in {"allow_once", "deny"}:
        raise ForgeHostCliError("Forge received an invalid approval decision.")
    decision = client.decide(planned.approval_event, decision=decision_value)
    started = client.start(ready) if decision_value == "allow_once" else None
    return ForgeHostCliRunResult(planned=planned, decision=decision, started=started)


def plan_ready_lines(ready: PlanReady) -> tuple[str, ...]:
    lines = [
        f"Goal: {ready.plan_definition.project_goal}",
        f"Summary: {ready.plan_definition.summary}",
    ]
    for task in ready.plan_definition.tasks:
        lines.append(f"Task {task.task_id}: {task.title}")
        lines.extend(f"  write {scope.kind}: {scope.path}" for scope in task.write_scopes)
    commands = ready.verification_contract.commands
    lines.extend(f"Verify: {' '.join(command.argv)}" for command in commands)
    admission = ready.execution_admission
    lines.append(
        f"Limits: {admission.per_managed_task_max_steps} model steps, {admission.run_timeout_ms} ms"
    )
    return tuple(lines)


def approval_lines(
    ready: PlanReady,
    approval: ApprovalRequired,
    *,
    provider_display: str,
) -> tuple[str, ...]:
    lines = [
        f"Approval required ({approval.risk}): {approval.summary}",
        f"Provider: {provider_display}",
        "Exact write authority:",
    ]
    if approval.scope.kind != "filesystem":
        raise ForgeHostCliError("Forge returned an unexpected approval scope.")
    lines.extend(
        f"  {resource.access} {resource.kind}: {resource.path}"
        for resource in approval.scope.resources
    )
    lines.append(
        "Execution: "
        f"{ready.execution_admission.execution_strategy.mode}, "
        f"max workers {ready.execution_admission.execution_strategy.max_parallel_workers}"
    )
    lines.extend(
        f"Command: {os.fspath(' '.join(command.argv))}"
        for command in ready.verification_contract.commands
    )
    return tuple(lines)


def run_event_lines(events: Sequence[EventEnvelope]) -> tuple[str, ...]:
    lines: list[str] = []
    for event in events:
        payload = event.payload
        if isinstance(payload, RunProgress):
            detail = f"[{payload.phase}] {payload.summary}"
            if payload.task_id is not None:
                detail += f" ({payload.task_id}, step {payload.step}/{payload.total_steps})"
            lines.append(detail)
        elif isinstance(payload, EvidenceReady):
            lines.extend(
                f"Evidence {record.evidence_id}: {record.status} — {record.summary}"
                for record in payload.evidence
            )
            lines.extend(
                f"Artifact {artifact.role}/{artifact.kind}: "
                f"{artifact.workspace_relative_path} ({artifact.status})"
                for artifact in payload.artifacts
            )
        elif isinstance(payload, RunTerminal):
            result = payload.result
            lines.append(f"Forge {result.status}: {result.reason.message}")
            lines.append(
                f"Usage: {result.usage.model_calls} model calls; "
                f"elapsed {result.timing.elapsed_ms} ms"
            )
    return tuple(lines)


__all__ = [
    "ForgeHostCliError",
    "ForgeHostCliPlanResult",
    "ForgeHostCliRunResult",
    "ForgeHostResumeManifest",
    "approval_lines",
    "infer_intent_kind",
    "load_resume_manifest",
    "plan_ready_lines",
    "plan_with_runtime",
    "resume_manifest_path",
    "run_event_lines",
    "run_with_existing_runtime",
    "write_resume_manifest",
]
