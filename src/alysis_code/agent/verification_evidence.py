from __future__ import annotations

import shlex
from collections.abc import Collection
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from ..branding import env_get
from ..pipeline_facts import pipeline_meaningful_stage
from ..verification_command_analysis import (
    VerificationCommandEvidentiaryCapability,
    analyze_verification_command,
)
from ..verification_contract import build_verification_command_specs
from ..verify_gate import assess_verification_command_execution
from .verification_commands import (
    _canonicalize_verification_command_for_match,
    _has_disallowed_shell_control_flow,
    _marker_fallback_is_verification_attempt,
    _matching_effective_verification_commands,
    _normalize_shell_command_for_match,
    _parse_verification_command_shape,
)

# Recognized static-analysis families that inspect code without executing it.
# They do not qualify as post-edit *execution* evidence for the ordering rule
# (they are supplementary signals only); test runners and black-box checks do.
_STATIC_ONLY_EVIDENCE_FAMILIES = frozenset({"mypy", "ruff:check"})


def _evidence_v2_enabled(cfg: Any | None) -> bool:
    """Kill-switch for the fact-based evidence classifier (evidence v2).

    ``ALYSIS_EVIDENCE_V2`` (off/0/false/no/disabled) wins over the config
    value; default is on. Mirrors the route-arbitration kill-switch idiom.
    """
    env_value = env_get("ALYSIS_EVIDENCE_V2")
    if env_value is not None:
        normalized = str(env_value).strip().lower()
        if normalized in {"off", "0", "false", "no", "disabled"}:
            return False
        if normalized in {"on", "1", "true", "yes", "enabled"}:
            return True
    return bool(getattr(cfg, "evidence_v2_enabled", True))


def command_is_qualifying_execution_evidence(command: str) -> bool:
    """True when ``command`` executes code as real verification (not syntax-only).

    Used by the ordering rule to decide whether an observed execution counts as
    post-edit execution evidence. Judged from the analyzed command family, not a
    string pattern: an assertive test/validation program qualifies; ``ast.parse``
    / ``py_compile`` (non-assertive) and static analyzers (mypy, ruff check) do
    not. For a pipeline, the meaningful first-stage program is judged.
    """
    target = pipeline_meaningful_stage(command) or command
    analysis = analyze_verification_command(target, trusted=True)
    if analysis.rejection_reason:
        return False
    if analysis.evidentiary_capability != VerificationCommandEvidentiaryCapability.ASSERTIVE:
        return False
    if analysis.command_family in _STATIC_ONLY_EVIDENCE_FAMILIES:
        return False
    # An inline interpreter snippet (e.g. ``python -c '<code>'``) only executes
    # code as a real check when it asserts; ``ast.parse`` / import-only snippets
    # are static and must not qualify as post-edit execution evidence.
    parts = analysis.parts
    if (
        len(parts) >= 2
        and parts[1] == "-c"
        and analysis.command_family != "assertion:python_snippet"
    ):
        return False
    return True


class VerificationEvidenceCategory(StrEnum):
    AUTHORITATIVE = "AUTHORITATIVE"
    REPO_NATIVE = "REPO_NATIVE"
    TASK_ACCEPTANCE = "TASK_ACCEPTANCE"
    NOT_VERIFICATION = "NOT_VERIFICATION"


@dataclass(frozen=True)
class VerificationEvidence:
    category: VerificationEvidenceCategory
    normalized_command: str
    matched_command: str | None = None
    real_execution: bool | None = None
    allowed_to_satisfy_contract: bool = False
    reason: str = ""
    covered_verification_commands: tuple[str, ...] = ()
    supplemental_only: bool = False
    working_directory: str = ""

    @property
    def evidence_verdict(self) -> str:
        """Machine-readable verdict for telemetry.

        ``pass`` — executed and accepted as satisfying verification;
        ``fail`` — executed but did not pass (or mutated a material path);
        ``unavailable`` — a pipeline whose per-stage status could not be observed;
        ``supplemental`` — self-authored, cannot independently confirm the spec;
        ``not_verification`` — the command is not a verification attempt.
        """
        if self.category == VerificationEvidenceCategory.NOT_VERIFICATION:
            if self.reason == "pipeline_stage_status_unavailable":
                return "unavailable"
            return "not_verification"
        if self.supplemental_only:
            return "supplemental"
        return "pass" if self.allowed_to_satisfy_contract else "fail"

    def as_payload(self) -> dict[str, object]:
        return {
            "evidence_category": self.category.value,
            "evidence_verdict": self.evidence_verdict,
            "normalized_command": self.normalized_command,
            "matched_command": self.matched_command,
            "real_execution": self.real_execution,
            "allowed_to_satisfy_contract": self.allowed_to_satisfy_contract,
            "reason": self.reason,
            "covered_verification_commands": list(self.covered_verification_commands),
            "supplemental_only": self.supplemental_only,
            "working_directory": self.working_directory,
        }


_INTERPRETER_HEADS = {
    "python",
    "python3",
    "py",
    "node",
    "ruby",
    "rscript",
    "r",
    "bash",
    "sh",
    "zsh",
}
_OBSERVATION_HEADS = {
    "cat",
    "echo",
    "find",
    "grep",
    "head",
    "less",
    "ls",
    "more",
    "printf",
    "pwd",
    "tail",
    "type",
    "wc",
    "which",
}
_META_OPTIONS = {"--help", "-h", "--version", "-v", "version", "help"}
_VALIDATION_NAME_MARKERS = (
    "accept",
    "acceptance",
    "check",
    "smoke",
    "test",
    "validate",
    "validation",
    "verify",
)
_VALIDATION_DIR_MARKERS = {
    "acceptance",
    "check",
    "checks",
    "script",
    "scripts",
    "smoke",
    "test",
    "tests",
    "validation",
    "verify",
}


def _execution_allows_acceptance(
    *,
    exit_code: int | None,
    real_execution: bool | None,
) -> bool:
    if real_execution is not True:
        return False
    if exit_code is not None and exit_code != 0:
        return False
    return True


def _invalid_contract_rejection_reason(
    *,
    matches: set[str],
    known_verification_commands: list[str],
    authoritative: bool,
) -> str:
    if not matches:
        return ""
    source = (
        "environment.authoritative_verification_commands"
        if authoritative
        else "task_refinement.explicit_user_command"
    )
    contract_type = "authoritative_override" if authoritative else "task_acceptance"
    normalized_matches = {_normalize_shell_command_for_match(command) for command in matches}
    for spec in build_verification_command_specs(
        tuple(known_verification_commands),
        source=source,
        contract_type=contract_type,
    ):
        if _normalize_shell_command_for_match(spec.original_text) not in normalized_matches:
            continue
        if spec.validation_status.value == "INVALID":
            return spec.rejection_reason or "invalid_verification_command"
    return ""


def _contract_execution_allows_acceptance(
    *,
    exit_code: int | None,
    real_execution: bool | None,
) -> bool:
    if real_execution is not True:
        return False
    if exit_code is not None and exit_code != 0:
        return False
    return True


def _command_parts(command: str) -> list[str]:
    canonical = _canonicalize_verification_command_for_match(command)
    if not canonical:
        return []
    try:
        return shlex.split(canonical, posix=True)
    except ValueError:
        return []


def _path_key(path: str, *, root: Path | None = None) -> str:
    cleaned = str(path or "").strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if root is not None and cleaned:
        try:
            resolved = Path(cleaned).expanduser().resolve()
            cleaned = resolved.relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            pass
    return cleaned.casefold()


def _is_repo_local_path(path: str) -> bool:
    if not path or path.startswith("-"):
        return False
    pure = PurePosixPath(path.replace("\\", "/"))
    return not pure.is_absolute() and ".." not in pure.parts


def _is_repo_local_executable_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return _is_repo_local_path(path) and ("/" in normalized or normalized.startswith("."))


def _path_has_validation_signal(path: str) -> bool:
    pure = PurePosixPath(path.replace("\\", "/"))
    parts = tuple(part.casefold() for part in pure.parts if part not in {"", "."})
    name = pure.stem.casefold()
    if any(marker in name for marker in _VALIDATION_NAME_MARKERS):
        return True
    return bool(set(parts[:-1]) & _VALIDATION_DIR_MARKERS)


def _interpreter_script_path(parts: list[str]) -> str | None:
    if not parts:
        return None
    head = Path(parts[0]).name.casefold()
    if head.endswith(".exe"):
        head = head[:-4]
    if head not in _INTERPRETER_HEADS:
        return None
    tail = parts[1:]
    if not tail:
        return None
    if tail[0] in _META_OPTIONS:
        return None
    if head in {"python", "python3", "py"} and tail[0] == "-m":
        return None
    for item in tail:
        if item == "--":
            continue
        if _is_repo_local_path(item):
            return item
    return None


def _diff_or_cmp_has_real_operands(parts: list[str]) -> bool:
    if not parts:
        return False
    head = Path(parts[0]).name.casefold()
    if head not in {"cmp", "diff"}:
        return False
    operands = [part for part in parts[1:] if part != "--" and not part.startswith("-")]
    return len(operands) >= 2 and all(_is_repo_local_path(item) for item in operands[:2])


def _is_known_non_executing_verification_form(parts: list[str]) -> bool:
    lowered = [part.casefold() for part in parts]
    if not lowered:
        return True
    if any(part in {"--help", "-h", "--version"} for part in lowered):
        return True
    if lowered[0] in {"pytest", "py.test"}:
        return any(part in {"--collect-only", "--co", "--setup-plan"} for part in lowered[1:])
    if len(lowered) >= 3 and lowered[0] in {"python", "python3", "py"} and lowered[1] == "-m":
        if lowered[2] == "pytest":
            return any(part in {"--collect-only", "--co", "--setup-plan"} for part in lowered[3:])
    if lowered[:2] == ["go", "test"]:
        if any(part in {"-c", "-list"} or part.startswith("-list=") for part in lowered[2:]):
            return True
        for index, part in enumerate(lowered[2:], start=2):
            if part == "-run" and index + 1 < len(lowered) and lowered[index + 1] == "^$":
                return True
            if part == "-run=^$":
                return True
    if lowered[:2] == ["cargo", "test"]:
        return any(part in {"--no-run", "--list"} for part in lowered[2:])
    if lowered[:2] == ["ruff", "check"]:
        return any(part in {"--fix", "--fix-only"} for part in lowered[2:])
    if lowered[0] == "mypy":
        return "--install-types" in lowered[1:]
    return False


def _task_specific_reason(
    *,
    parts: list[str],
    changed_paths: set[str],
    root: Path | None,
) -> str | None:
    if not parts:
        return None
    head = Path(parts[0]).name.casefold()
    if head.endswith(".exe"):
        head = head[:-4]
    if head in _OBSERVATION_HEADS:
        return None
    if any(part.casefold() in _META_OPTIONS for part in parts[1:]):
        return None
    if _diff_or_cmp_has_real_operands(parts):
        return "real_output_comparison"

    script_path = _interpreter_script_path(parts)
    if script_path:
        script_key = _path_key(script_path, root=root)
        changed_keys = {_path_key(item, root=root) for item in changed_paths}
        if script_key in changed_keys:
            return "changed_repo_local_script_execution"
        if _path_has_validation_signal(script_path):
            return "repo_local_validation_script"
        return None

    executable = parts[0]
    if _is_repo_local_executable_path(executable) and _path_has_validation_signal(executable):
        return "repo_local_validation_executable"
    return None


def classify_verification_evidence(
    command: str,
    *,
    known_verification_commands: list[str] | tuple[str, ...] | None = None,
    required_verification_commands: Collection[str] | None = None,
    authoritative: bool = False,
    changed_paths: set[str] | list[str] | tuple[str, ...] | None = None,
    material_touched_paths: set[str] | list[str] | tuple[str, ...] | None = None,
    exit_code: int | None = None,
    output: str = "",
    real_execution: bool | None = None,
    root: Path | None = None,
    stage_status: list[int] | tuple[int, ...] | None = None,
    evidence_v2: bool = True,
    working_directory: str | None = None,
) -> VerificationEvidence:
    """Classify an observed command as verification evidence.

    Evidence v2 (default) judges observed *execution facts*: for a shell
    pipeline the meaningful program is the first stage and its per-stage exit
    status (``stage_status``, captured via PIPESTATUS) is the ground truth, so a
    piped ``pytest ... | tail`` counts as passing/failing evidence per the
    pipeline's real first-stage code rather than being rejected for containing a
    ``|``. When per-stage status is unavailable the pipeline is treated as
    unverified (``pipeline_stage_status_unavailable``) so it can never be
    silently accepted. With ``evidence_v2`` off, legacy string-shape
    classification applies. In both modes a supplied required-command collection
    separates mandatory selections from recommendations. ``None`` preserves
    conservative behavior for callers that only know the selected commands' text.
    """
    if evidence_v2:
        first_stage = pipeline_meaningful_stage(command)
        if first_stage is not None:
            return _classify_pipeline_evidence(
                command=command,
                first_stage=first_stage,
                stage_status=list(stage_status) if stage_status is not None else None,
                known_verification_commands=known_verification_commands,
                required_verification_commands=required_verification_commands,
                authoritative=authoritative,
                changed_paths=changed_paths,
                material_touched_paths=material_touched_paths,
                output=output,
                root=root,
                working_directory=working_directory,
            )
    return _classify_verification_evidence_legacy(
        command,
        known_verification_commands=known_verification_commands,
        required_verification_commands=required_verification_commands,
        authoritative=authoritative,
        changed_paths=changed_paths,
        material_touched_paths=material_touched_paths,
        exit_code=exit_code,
        output=output,
        real_execution=real_execution,
        root=root,
        working_directory=working_directory,
    )


def _classify_pipeline_evidence(
    *,
    command: str,
    first_stage: str,
    stage_status: list[int] | None,
    known_verification_commands: list[str] | tuple[str, ...] | None,
    required_verification_commands: Collection[str] | None,
    authoritative: bool,
    changed_paths: set[str] | list[str] | tuple[str, ...] | None,
    material_touched_paths: set[str] | list[str] | tuple[str, ...] | None,
    output: str,
    root: Path | None,
    working_directory: str | None,
) -> VerificationEvidence:
    normalized = _normalize_shell_command_for_match(command)
    if not stage_status:
        # No observed per-stage status and no ground truth obtained: the
        # pipeline's real first-stage outcome is unknown, so it is not evidence.
        return VerificationEvidence(
            category=VerificationEvidenceCategory.NOT_VERIFICATION,
            normalized_command=normalized,
            real_execution=None,
            reason="pipeline_stage_status_unavailable",
        )
    first_stage_exit = stage_status[0]
    evidence = _classify_verification_evidence_legacy(
        first_stage,
        known_verification_commands=known_verification_commands,
        required_verification_commands=required_verification_commands,
        authoritative=authoritative,
        changed_paths=changed_paths,
        material_touched_paths=material_touched_paths,
        exit_code=first_stage_exit,
        output=output,
        real_execution=None,
        root=root,
        working_directory=working_directory,
    )
    # Keep the observed (piped) command as the normalized command for telemetry;
    # the covered contract commands already reflect the matched contract text.
    return replace(evidence, normalized_command=normalized)


def _classify_verification_evidence_legacy(
    command: str,
    *,
    known_verification_commands: list[str] | tuple[str, ...] | None = None,
    required_verification_commands: Collection[str] | None = None,
    authoritative: bool = False,
    changed_paths: set[str] | list[str] | tuple[str, ...] | None = None,
    material_touched_paths: set[str] | list[str] | tuple[str, ...] | None = None,
    exit_code: int | None = None,
    output: str = "",
    real_execution: bool | None = None,
    root: Path | None = None,
    working_directory: str | None = None,
) -> VerificationEvidence:
    normalized = _normalize_shell_command_for_match(command)
    if not normalized:
        return VerificationEvidence(
            category=VerificationEvidenceCategory.NOT_VERIFICATION,
            normalized_command="",
            real_execution=real_execution,
            reason="empty_command",
        )

    if real_execution is None and exit_code is not None:
        assessment = assess_verification_command_execution(
            command=command,
            exit_code=exit_code,
            output=output,
        )
        real_execution = assessment.real_execution

    known = list(
        dict.fromkeys(
            str(item)
            for item in [
                *(known_verification_commands or []),
                *(required_verification_commands or []),
            ]
            if str(item).strip()
        )
    )
    matching_command = command
    if working_directory and working_directory != ".":
        same_root = False
        if root is not None:
            try:
                same_root = (root / working_directory).resolve() == root.resolve()
            except (OSError, RuntimeError, ValueError):
                pass
        if not same_root:
            matching_command = f"cd {shlex.quote(working_directory)} && {command}"
    analysis = analyze_verification_command(
        command,
        trusted=authoritative or bool(known),
    )
    material_touched = tuple(
        sorted(str(item) for item in (material_touched_paths or []) if str(item).strip())
    )
    execution_ok = _execution_allows_acceptance(
        exit_code=exit_code,
        real_execution=real_execution,
    )
    mutation_reason = "mutated_material_paths" if material_touched else ""
    trusted_shell_matches = _matching_trusted_shell_expressions(
        observed_command=matching_command,
        known_verification_commands=known,
        authoritative=authoritative,
    )
    if trusted_shell_matches:
        contract_execution_ok = _contract_execution_allows_acceptance(
            exit_code=exit_code,
            real_execution=real_execution,
        )
        capability_ok = (
            analysis.evidentiary_capability == VerificationCommandEvidentiaryCapability.ASSERTIVE
            and not analysis.rejection_reason
        )
        allowed = contract_execution_ok and capability_ok and not material_touched
        category = (
            VerificationEvidenceCategory.AUTHORITATIVE
            if authoritative
            else VerificationEvidenceCategory.REPO_NATIVE
        )
        if not capability_ok:
            reason = (
                analysis.rejection_reason
                or analysis.inconclusive_reason
                or "unknown_verification_capability"
            )
        elif not contract_execution_ok:
            reason = "non_executing_or_failed_contract_command"
        elif mutation_reason:
            reason = mutation_reason
        else:
            reason = "matched_authoritative_contract" if authoritative else "matched_contract"
        return VerificationEvidence(
            category=category,
            normalized_command=normalized,
            matched_command=sorted(trusted_shell_matches)[0],
            real_execution=real_execution,
            allowed_to_satisfy_contract=allowed,
            reason=reason,
            covered_verification_commands=tuple(sorted(trusted_shell_matches)),
        )

    if _has_disallowed_shell_control_flow(command):
        return VerificationEvidence(
            category=VerificationEvidenceCategory.NOT_VERIFICATION,
            normalized_command=normalized,
            real_execution=real_execution,
            reason=analysis.rejection_reason or "disallowed_shell_control_flow",
        )

    matches = _matching_effective_verification_commands(
        observed_command=matching_command,
        effective_verification_commands=known,
    )
    if matches:
        invalid_contract_reason = _invalid_contract_rejection_reason(
            matches=matches,
            known_verification_commands=known,
            authoritative=authoritative,
        )
        contract_execution_ok = _contract_execution_allows_acceptance(
            exit_code=exit_code,
            real_execution=real_execution,
        )
        capability_ok = (
            analysis.evidentiary_capability == VerificationCommandEvidentiaryCapability.ASSERTIVE
            and not analysis.rejection_reason
        )
        allowed = (
            not invalid_contract_reason
            and contract_execution_ok
            and capability_ok
            and not material_touched
        )
        category = (
            VerificationEvidenceCategory.AUTHORITATIVE
            if authoritative
            else VerificationEvidenceCategory.REPO_NATIVE
        )
        if invalid_contract_reason:
            reason = invalid_contract_reason
        elif not capability_ok:
            reason = (
                analysis.rejection_reason
                or analysis.inconclusive_reason
                or "unknown_verification_capability"
            )
        elif not contract_execution_ok:
            reason = "non_executing_or_failed_contract_command"
        elif mutation_reason:
            reason = mutation_reason
        else:
            reason = "matched_authoritative_contract" if authoritative else "matched_contract"
        return VerificationEvidence(
            category=category,
            normalized_command=normalized,
            matched_command=sorted(matches)[0],
            real_execution=real_execution,
            allowed_to_satisfy_contract=allowed,
            reason=reason,
            covered_verification_commands=tuple(sorted(matches)),
        )

    parts = _command_parts(normalized)
    task_reason = _task_specific_reason(
        parts=parts,
        changed_paths={str(item) for item in (changed_paths or []) if str(item).strip()},
        root=root,
    )
    # A different selection cannot cover a required command. Recommendations
    # alone do not make an independent real check supplemental: retain exact
    # matching above, but assess unmatched checks on their execution facts.
    exclusive_commands = (
        known if required_verification_commands is None else required_verification_commands
    )
    if exclusive_commands:
        if task_reason:
            return VerificationEvidence(
                category=VerificationEvidenceCategory.TASK_ACCEPTANCE,
                normalized_command=normalized,
                real_execution=real_execution,
                allowed_to_satisfy_contract=False,
                reason="supplemental_only_contract_exists",
                supplemental_only=True,
            )
        if (
            analysis.evidentiary_capability == VerificationCommandEvidentiaryCapability.ASSERTIVE
            and not analysis.rejection_reason
        ):
            return VerificationEvidence(
                category=VerificationEvidenceCategory.REPO_NATIVE,
                normalized_command=normalized,
                real_execution=real_execution,
                allowed_to_satisfy_contract=False,
                reason="supplemental_only_contract_selection_differs",
                supplemental_only=True,
            )
        return VerificationEvidence(
            category=VerificationEvidenceCategory.NOT_VERIFICATION,
            normalized_command=normalized,
            real_execution=real_execution,
            reason="does_not_match_effective_contract",
        )

    if task_reason:
        if (
            analysis.evidentiary_capability
            == VerificationCommandEvidentiaryCapability.NON_ASSERTIVE
            or analysis.rejection_reason
        ):
            return VerificationEvidence(
                category=VerificationEvidenceCategory.NOT_VERIFICATION,
                normalized_command=normalized,
                real_execution=real_execution,
                reason=analysis.rejection_reason
                or analysis.capability_reason
                or "non_assertive_verifier",
            )
        allowed = execution_ok and not material_touched
        reason = task_reason if allowed else mutation_reason or "non_executing_or_failed_task_check"
        return VerificationEvidence(
            category=VerificationEvidenceCategory.TASK_ACCEPTANCE,
            normalized_command=normalized,
            real_execution=real_execution,
            allowed_to_satisfy_contract=allowed,
            reason=reason,
        )

    if (
        _parse_verification_command_shape(normalized) is not None
        and analysis.evidentiary_capability == VerificationCommandEvidentiaryCapability.ASSERTIVE
        and not analysis.rejection_reason
    ):
        allowed = execution_ok and not material_touched
        reason = (
            "repo_native_command"
            if allowed
            else mutation_reason or "non_executing_or_failed_repo_native_command"
        )
        return VerificationEvidence(
            category=VerificationEvidenceCategory.REPO_NATIVE,
            normalized_command=normalized,
            real_execution=real_execution,
            allowed_to_satisfy_contract=allowed,
            reason=reason,
        )

    if _marker_fallback_is_verification_attempt(
        normalized
    ) and not _is_known_non_executing_verification_form(parts):
        if (
            analysis.evidentiary_capability != VerificationCommandEvidentiaryCapability.ASSERTIVE
            or analysis.rejection_reason
        ):
            return VerificationEvidence(
                category=VerificationEvidenceCategory.NOT_VERIFICATION,
                normalized_command=normalized,
                real_execution=real_execution,
                reason=analysis.rejection_reason
                or analysis.inconclusive_reason
                or "unknown_verification_capability",
            )
        allowed = execution_ok and not material_touched
        reason = (
            "repo_native_command"
            if allowed
            else mutation_reason or "non_executing_or_failed_repo_native_command"
        )
        return VerificationEvidence(
            category=VerificationEvidenceCategory.REPO_NATIVE,
            normalized_command=normalized,
            real_execution=real_execution,
            allowed_to_satisfy_contract=allowed,
            reason=reason,
        )

    return VerificationEvidence(
        category=VerificationEvidenceCategory.NOT_VERIFICATION,
        normalized_command=normalized,
        real_execution=real_execution,
        reason="no_verification_signal",
    )


def _matching_trusted_shell_expressions(
    *,
    observed_command: str,
    known_verification_commands: list[str],
    authoritative: bool,
) -> set[str]:
    if not known_verification_commands:
        return set()
    source = (
        "environment.authoritative_verification_commands"
        if authoritative
        else "task_refinement.explicit_user_command"
    )
    contract_type = "authoritative_override" if authoritative else "task_acceptance"
    observed = _normalize_shell_command_for_match(observed_command)
    out: set[str] = set()
    for spec in build_verification_command_specs(
        tuple(known_verification_commands),
        source=source,
        contract_type=contract_type,
    ):
        if (
            spec.execution_mode.value == "TRUSTED_SHELL_EXPRESSION"
            and spec.validation_status.value == "VALID"
            and observed == _normalize_shell_command_for_match(spec.original_text)
        ):
            out.add(spec.original_text)
    return out
