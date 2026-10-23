"""Fail-closed execution of App-authored Forge verification contracts.

This is the narrow contract executor used after canonical Host-v2 execution.
It deliberately reuses the existing verification gate instead of introducing a
second shell or planner authority.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig
from .verification_contract import (
    ForgeTaskVerificationContract,
    VerificationCommandExecutionMode,
    VerificationCommandRequirement,
    VerificationCommandSpec,
    VerificationCommandValidationStatus,
    build_verification_command_spec,
)
from .verify_gate import VerifyRunResult, run_task_verification, verify_run_result_to_payload


@dataclass(frozen=True, slots=True)
class ForgeContractCommandEvidence:
    command_id: str
    requirement: VerificationCommandRequirement
    validation_status: VerificationCommandValidationStatus
    executed: bool
    rejection_reason: str = ""
    result: VerifyRunResult | None = None

    @property
    def required(self) -> bool:
        return self.requirement == VerificationCommandRequirement.REQUIRED

    @property
    def passed(self) -> bool | None:
        return self.result.all_passed if self.result is not None else None

    def to_payload(self, *, root: Path) -> dict[str, object]:
        payload: dict[str, object] = {
            "command_id": self.command_id,
            "requirement": self.requirement.value,
            "required": self.required,
            "validation_status": self.validation_status.value,
            "executed": self.executed,
            "passed": self.passed,
            "rejection_reason": self.rejection_reason,
        }
        if self.result is not None:
            payload["verification"] = verify_run_result_to_payload(root=root, result=self.result)
        return payload


@dataclass(frozen=True, slots=True)
class ForgeContractVerificationResult:
    task_id: str | None
    phase: str
    command_evidence: tuple[ForgeContractCommandEvidence, ...]
    artifact_path: Path
    terminal_rejection_reason: str | None = None

    @property
    def all_passed(self) -> bool:
        return not self.terminal_rejection_reason and all(
            item.result is not None and item.result.all_passed
            for item in self.command_evidence
            if item.required
        )

    def to_payload(self, *, root: Path) -> dict[str, object]:
        commands = [item.to_payload(root=root) for item in self.command_evidence]
        return {
            "task_id": self.task_id,
            "phase": self.phase,
            "all_passed": self.all_passed,
            "failed_command_ids": [
                item.command_id
                for item in self.command_evidence
                if item.required and (item.result is None or not item.result.all_passed)
            ],
            "terminal_rejection_reason": self.terminal_rejection_reason,
            "commands": commands,
        }


_SHELL_EXPANSION_CHARACTERS = frozenset("$`<>()")


def run_forge_contract_verification(
    *,
    root: Path,
    contract: ForgeTaskVerificationContract,
    artifact_path: Path,
    cfg: AppConfig | None = None,
    timeout_s: float = 900,
) -> ForgeContractVerificationResult:
    """Execute one already-bound typed contract with all-command prevalidation."""

    workspace_root = root.resolve()
    terminal_rejection: str | None = None
    if contract.cwd != ".":
        terminal_rejection = "contract cwd must be the canonical workspace root"

    validated: list[tuple[VerificationCommandSpec, str | None]] = []
    for spec in contract.commands:
        rejection: str | None = None
        rebuilt = build_verification_command_spec(
            spec.original_text,
            source=(
                "environment.authoritative_verification_commands"
                if str(spec.provenance) == "HOST_AUTHORITATIVE"
                else "cli.verify_cmd"
            ),
            contract_type=(
                "authoritative_override"
                if str(spec.provenance) == "HOST_AUTHORITATIVE"
                else "explicit_override"
            ),
            acceptance_criterion_ids=tuple(spec.acceptance_criterion_ids),
        )
        if (
            spec.validation_status != VerificationCommandValidationStatus.VALID
            or spec.execution_mode == VerificationCommandExecutionMode.INVALID
            or rebuilt.validation_status != VerificationCommandValidationStatus.VALID
            or rebuilt.execution_mode == VerificationCommandExecutionMode.INVALID
            or rebuilt.execution_mode != spec.execution_mode
            or tuple(rebuilt.argv) != tuple(spec.argv)
            or spec.working_directory != "."
        ):
            rejection = spec.rejection_reason or "command did not survive canonical revalidation"
        elif _SHELL_EXPANSION_CHARACTERS.intersection(spec.original_text):
            # The text runs through a shell, but revalidation only compares the split
            # argv, which is identical with or without substitutions and redirections.
            # Contract commands must be plain argument lists.
            rejection = "command uses shell expansion or redirection"
        validated.append((spec, rejection))
        if rejection and terminal_rejection is None:
            terminal_rejection = f"{spec.command_id}: {rejection}"

    evidence: list[ForgeContractCommandEvidence] = []
    if terminal_rejection is not None:
        for spec, rejection in validated:
            evidence.append(
                ForgeContractCommandEvidence(
                    command_id=spec.command_id,
                    requirement=spec.requirement,
                    validation_status=spec.validation_status,
                    executed=False,
                    rejection_reason=rejection or terminal_rejection,
                )
            )
    else:
        for index, (spec, _rejection) in enumerate(validated, start=1):
            safe_id = (
                "".join(
                    character if character.isalnum() or character in "_.-" else "-"
                    for character in spec.command_id
                ).strip("-.")
                or "command"
            )
            command_artifact = artifact_path.with_name(
                f"{artifact_path.stem}.command-{index:02d}-{safe_id}.txt"
            )
            result = run_task_verification(
                root=workspace_root,
                commands=[spec.original_text],
                artifact_path=command_artifact,
                cfg=cfg,
                timeout_s=timeout_s,
            )
            evidence.append(
                ForgeContractCommandEvidence(
                    command_id=spec.command_id,
                    requirement=spec.requirement,
                    validation_status=spec.validation_status,
                    executed=True,
                    result=result,
                )
            )

    result = ForgeContractVerificationResult(
        task_id=contract.task_id,
        phase=contract.phase.value,
        command_evidence=tuple(evidence),
        artifact_path=artifact_path,
        terminal_rejection_reason=terminal_rejection,
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(result.to_payload(root=workspace_root), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result
