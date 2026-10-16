from __future__ import annotations

import hashlib
import json
import math
import re
import shlex
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .verification_command_analysis import (
    VerificationCommandEvidentiaryCapability,
    analyze_verification_command,
)


class VerificationCommandExecutionMode(StrEnum):
    ARGV = "ARGV"
    TRUSTED_SHELL_EXPRESSION = "TRUSTED_SHELL_EXPRESSION"
    INTERPRETER_SNIPPET = "INTERPRETER_SNIPPET"
    INVALID = "INVALID"


class VerificationCommandProvenance(StrEnum):
    HOST_AUTHORITATIVE = "HOST_AUTHORITATIVE"
    EXPLICIT_USER_COMMAND = "EXPLICIT_USER_COMMAND"
    PREEXISTING_REPO_NATIVE = "PREEXISTING_REPO_NATIVE"
    PREEXISTING_TASK_CHECKER = "PREEXISTING_TASK_CHECKER"
    INFERRED_HEURISTIC = "INFERRED_HEURISTIC"


class VerificationCommandTrustLevel(StrEnum):
    TRUSTED = "TRUSTED"
    UNTRUSTED = "UNTRUSTED"


class VerificationCommandRequirement(StrEnum):
    REQUIRED = "REQUIRED"
    ADVISORY = "ADVISORY"


class VerificationCommandValidationStatus(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"


@dataclass(frozen=True)
class VerificationCommandSpec:
    command_id: str
    original_text: str
    display_text: str
    execution_mode: VerificationCommandExecutionMode
    provenance: VerificationCommandProvenance
    trust_level: VerificationCommandTrustLevel
    requirement: VerificationCommandRequirement
    working_directory: str = "."
    timeout_policy: str = "session_verification_timeout"
    acceptance_criterion_ids: tuple[str, ...] = tuple()
    validation_status: VerificationCommandValidationStatus = (
        VerificationCommandValidationStatus.VALID
    )
    rejection_reason: str = ""
    evidentiary_capability: VerificationCommandEvidentiaryCapability = (
        VerificationCommandEvidentiaryCapability.UNKNOWN
    )
    capability_reason: str = ""
    argv: tuple[str, ...] = field(default_factory=tuple)

    def as_payload(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "original_text": self.original_text,
            "display_text": self.display_text,
            "execution_mode": self.execution_mode.value,
            "provenance": self.provenance.value,
            "trust_level": self.trust_level.value,
            "requirement": self.requirement.value,
            "working_directory": self.working_directory,
            "timeout_policy": self.timeout_policy,
            "acceptance_criterion_ids": list(self.acceptance_criterion_ids),
            "validation_status": self.validation_status.value,
            "rejection_reason": self.rejection_reason,
            "evidentiary_capability": self.evidentiary_capability.value,
            "capability_reason": self.capability_reason,
            "argv": list(self.argv),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> VerificationCommandSpec:
        """Rehydrate the stable command payload emitted by :meth:`as_payload`."""

        if not isinstance(payload, Mapping):
            raise ForgeVerificationContractError("command spec must be an object")
        _require_exact_fields(
            payload,
            {
                "command_id",
                "original_text",
                "display_text",
                "execution_mode",
                "provenance",
                "trust_level",
                "requirement",
                "working_directory",
                "timeout_policy",
                "acceptance_criterion_ids",
                "validation_status",
                "rejection_reason",
                "evidentiary_capability",
                "capability_reason",
                "argv",
            },
            "verification command",
        )
        try:
            acceptance_ids = _string_sequence(
                payload.get("acceptance_criterion_ids", ()),
                field_name="acceptance_criterion_ids",
            )
            argv = _string_sequence(payload.get("argv", ()), field_name="argv")
            return cls(
                command_id=_required_string(payload, "command_id"),
                original_text=_string_value(payload.get("original_text", ""), "original_text"),
                display_text=_string_value(payload.get("display_text", ""), "display_text"),
                execution_mode=VerificationCommandExecutionMode(
                    _required_string(payload, "execution_mode")
                ),
                provenance=VerificationCommandProvenance(_required_string(payload, "provenance")),
                trust_level=VerificationCommandTrustLevel(_required_string(payload, "trust_level")),
                requirement=VerificationCommandRequirement(
                    _required_string(payload, "requirement")
                ),
                working_directory=_string_value(
                    payload.get("working_directory", "."), "working_directory"
                ),
                timeout_policy=_string_value(
                    payload.get("timeout_policy", "session_verification_timeout"),
                    "timeout_policy",
                ),
                acceptance_criterion_ids=acceptance_ids,
                validation_status=VerificationCommandValidationStatus(
                    _string_value(
                        payload.get("validation_status", VerificationCommandValidationStatus.VALID),
                        "validation_status",
                    )
                ),
                rejection_reason=_string_value(
                    payload.get("rejection_reason", ""), "rejection_reason"
                ),
                evidentiary_capability=VerificationCommandEvidentiaryCapability(
                    _string_value(
                        payload.get(
                            "evidentiary_capability",
                            VerificationCommandEvidentiaryCapability.UNKNOWN,
                        ),
                        "evidentiary_capability",
                    )
                ),
                capability_reason=_string_value(
                    payload.get("capability_reason", ""), "capability_reason"
                ),
                argv=argv,
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ForgeVerificationContractError):
                raise
            raise ForgeVerificationContractError(f"invalid command spec: {exc}") from exc


# Rejection reasons that mean "Alysis Code could not classify this command",
# as opposed to "Alysis Code recognized this command and refused it". Only the
# former is survivable: an unclassifiable command costs the session a
# verification contract, while a refused one (a vacuous verifier, a masked
# failure, an unsafe pipeline) would actively manufacture false evidence, so
# accepting it is worse than stopping.
UNCLASSIFIABLE_VERIFY_REJECTION_REASONS = frozenset(
    {
        "unknown_verification_capability",
        "unrecognized_command",
    }
)


def rejection_reason_is_unclassifiable(reason: str) -> bool:
    return str(reason or "").strip() in UNCLASSIFIABLE_VERIFY_REJECTION_REASONS


_TRUSTED_SHELL_PROVENANCE = {
    VerificationCommandProvenance.HOST_AUTHORITATIVE,
    VerificationCommandProvenance.EXPLICIT_USER_COMMAND,
    VerificationCommandProvenance.PREEXISTING_REPO_NATIVE,
    VerificationCommandProvenance.PREEXISTING_TASK_CHECKER,
}
_DISALLOWED_VERIFICATION_SHELL_TOKENS = {"||", "&&", ";", "|", "&"}
_SUPPORTED_VERIFY_HEADS = {
    "pytest",
    "py.test",
    "unittest",
    "mypy",
    "ruff",
    "go",
    "cargo",
    "npm",
    "pnpm",
    "yarn",
    "make",
    "just",
    "test",
    "[",
    "cmp",
    "diff",
    "grep",
}
_PYTHON_EXECUTABLES = {"python", "python3", "py"}
_SHELL_WRAPPER_HEADS = {"bash", "sh", "zsh"}
_VACUOUS_SUCCESS_HEADS = {"true", ":"}
_NON_ASSERTIVE_OBSERVATION_HEADS = {
    "cat",
    "echo",
    "find",
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


def build_verification_command_specs(
    commands: tuple[str, ...] | list[str],
    *,
    source: str,
    contract_type: str = "",
    acceptance_criterion_ids: tuple[str, ...] = tuple(),
) -> tuple[VerificationCommandSpec, ...]:
    return tuple(
        build_verification_command_spec(
            command,
            source=source,
            contract_type=contract_type,
            acceptance_criterion_ids=acceptance_criterion_ids,
            index=index,
        )
        for index, command in enumerate(commands, start=1)
        if str(command).strip()
    )


def build_verification_command_spec(
    command: str,
    *,
    source: str,
    contract_type: str = "",
    acceptance_criterion_ids: tuple[str, ...] = tuple(),
    index: int = 1,
) -> VerificationCommandSpec:
    original = str(command or "").strip()
    provenance = _provenance_for_source(source=source, contract_type=contract_type)
    trust_level = (
        VerificationCommandTrustLevel.TRUSTED
        if provenance in _TRUSTED_SHELL_PROVENANCE
        else VerificationCommandTrustLevel.UNTRUSTED
    )
    requirement = (
        VerificationCommandRequirement.ADVISORY
        if provenance == VerificationCommandProvenance.INFERRED_HEURISTIC
        else VerificationCommandRequirement.REQUIRED
    )
    parsed_parts: tuple[str, ...] = tuple()
    validation_status = VerificationCommandValidationStatus.VALID
    rejection_reason = ""
    execution_mode = VerificationCommandExecutionMode.ARGV
    analysis = analyze_verification_command(
        original,
        source=source,
        contract_type=contract_type,
        trusted=_source_is_trusted(source=source, contract_type=contract_type),
    )
    evidentiary_capability = analysis.evidentiary_capability
    capability_reason = analysis.capability_reason
    if analysis.parts:
        parsed_parts = analysis.parts
    if not original:
        validation_status = VerificationCommandValidationStatus.INVALID
        execution_mode = VerificationCommandExecutionMode.INVALID
        rejection_reason = "empty_command"
    else:
        try:
            tuple(shlex.split(original, posix=True))
        except ValueError as exc:
            validation_status = VerificationCommandValidationStatus.INVALID
            execution_mode = VerificationCommandExecutionMode.INVALID
            rejection_reason = f"parse_error: {exc}"
        else:
            requires_shell_expression = (
                analysis.shell_control_flow != "none"
                or analysis.unwrapped_command != analysis.normalized_command
            )
            if not analysis.parts and not analysis.rejection_reason:
                validation_status = VerificationCommandValidationStatus.INVALID
                execution_mode = VerificationCommandExecutionMode.INVALID
                rejection_reason = analysis.inconclusive_reason or "unrecognized_command"
            elif analysis.rejection_reason:
                validation_status = VerificationCommandValidationStatus.INVALID
                execution_mode = VerificationCommandExecutionMode.INVALID
                rejection_reason = analysis.rejection_reason
            elif (
                analysis.evidentiary_capability
                != VerificationCommandEvidentiaryCapability.ASSERTIVE
            ):
                validation_status = VerificationCommandValidationStatus.INVALID
                execution_mode = VerificationCommandExecutionMode.INVALID
                rejection_reason = analysis.inconclusive_reason or "unknown_verification_capability"
            elif requires_shell_expression:
                if provenance in _TRUSTED_SHELL_PROVENANCE:
                    execution_mode = VerificationCommandExecutionMode.TRUSTED_SHELL_EXPRESSION
                else:
                    validation_status = VerificationCommandValidationStatus.INVALID
                    execution_mode = VerificationCommandExecutionMode.INVALID
                    rejection_reason = "untrusted_shell_expression"
            elif _is_python_interpreter_snippet(parsed_parts):
                execution_mode = VerificationCommandExecutionMode.INTERPRETER_SNIPPET
    command_id = _stable_command_id(
        source=source,
        contract_type=contract_type,
        command=original,
        index=index,
    )
    return VerificationCommandSpec(
        command_id=command_id,
        original_text=original,
        display_text=original,
        execution_mode=execution_mode,
        provenance=provenance,
        trust_level=trust_level,
        requirement=requirement,
        acceptance_criterion_ids=acceptance_criterion_ids,
        validation_status=validation_status,
        rejection_reason=rejection_reason,
        evidentiary_capability=evidentiary_capability,
        capability_reason=capability_reason,
        argv=parsed_parts if execution_mode == VerificationCommandExecutionMode.ARGV else tuple(),
    )


def trusted_shell_expression_commands(
    commands: tuple[str, ...] | list[str],
    *,
    source: str,
    contract_type: str = "",
) -> set[str]:
    return {
        _normalize_exact_command(spec.original_text)
        for spec in build_verification_command_specs(
            commands,
            source=source,
            contract_type=contract_type,
        )
        if spec.execution_mode == VerificationCommandExecutionMode.TRUSTED_SHELL_EXPRESSION
        and spec.validation_status == VerificationCommandValidationStatus.VALID
    }


def command_matches_trusted_shell_expression(
    observed_command: str,
    known_commands: tuple[str, ...] | list[str],
    *,
    source: str,
    contract_type: str = "",
) -> set[str]:
    observed = _normalize_exact_command(observed_command)
    if not observed:
        return set()
    trusted = trusted_shell_expression_commands(
        known_commands,
        source=source,
        contract_type=contract_type,
    )
    return {command for command in trusted if observed == command}


def command_specs_payload(specs: tuple[VerificationCommandSpec, ...]) -> list[dict[str, Any]]:
    return [spec.as_payload() for spec in specs]


def _source_is_trusted(*, source: str, contract_type: str) -> bool:
    provenance = _provenance_for_source(source=source, contract_type=contract_type)
    return provenance in _TRUSTED_SHELL_PROVENANCE


def _provenance_for_source(
    *,
    source: str,
    contract_type: str,
) -> VerificationCommandProvenance:
    source = str(source or "")
    contract_type = str(contract_type or "")
    # A scan can suggest a runner from files or layout without finding a
    # declared command. Its legacy repo_native label does not establish an
    # explicit requirement or independent checker provenance.
    if source in {"repo_scan.likely_test_commands", "verification_fallback.detected_runner"}:
        return VerificationCommandProvenance.INFERRED_HEURISTIC
    host_authoritative_sources = {
        "environment.authoritative_verification_commands",
        "forge_host.authoritative_verification_commands",
    }
    if source in host_authoritative_sources or contract_type in {
        "authoritative_override",
        "explicit_override",
    }:
        return (
            VerificationCommandProvenance.HOST_AUTHORITATIVE
            if source in host_authoritative_sources
            else VerificationCommandProvenance.EXPLICIT_USER_COMMAND
        )
    if source in {"cli.verify_cmd", "config.verify_commands"}:
        return VerificationCommandProvenance.EXPLICIT_USER_COMMAND
    if source == "task_refinement.explicit_user_command" or contract_type == "task_acceptance":
        return VerificationCommandProvenance.EXPLICIT_USER_COMMAND
    if contract_type == "repo_native":
        return VerificationCommandProvenance.PREEXISTING_REPO_NATIVE
    if source.startswith("task_refinement.") and contract_type == "task_inferred":
        return VerificationCommandProvenance.INFERRED_HEURISTIC
    return VerificationCommandProvenance.INFERRED_HEURISTIC


def _normalize_exact_command(command: str) -> str:
    return " ".join(str(command or "").strip().split())


def _has_disallowed_shell_control_flow(raw: str) -> bool:
    analysis = analyze_verification_command(raw, trusted=True)
    return analysis.shell_control_flow in {"unsafe", "pipeline"} or analysis.rejection_reason in {
        "disallowed_shell_control_flow",
        "unsafe_pipeline",
    }


def _normalize_and_unwrap_shell_command(raw: str) -> str:
    normalized = _normalize_exact_command(raw)
    while True:
        wrapped = _unwrap_shell_wrapper_command(normalized)
        if not wrapped or wrapped == normalized:
            return normalized
        normalized = wrapped


def _unwrap_shell_wrapper_command(normalized: str) -> str | None:
    try:
        parts = shlex.split(normalized, posix=True)
    except ValueError:
        return None
    if not parts:
        return None
    head = Path(parts[0]).name.casefold()
    if head in _SHELL_WRAPPER_HEADS and len(parts) == 3 and parts[1] == "-lc":
        return _normalize_exact_command(parts[2])
    if head == "fish" and len(parts) == 3 and parts[1] == "-c":
        return _normalize_exact_command(parts[2])
    if head == "cmd" and len(parts) == 3 and parts[1].casefold() == "/c":
        return _normalize_exact_command(parts[2])
    if head in {"powershell", "pwsh"} and len(parts) == 3 and parts[1].casefold() == "-command":
        return _normalize_exact_command(parts[2])
    return None


def _shell_tokens(raw: str) -> list[str] | None:
    normalized = _normalize_and_unwrap_shell_command(raw)
    if not normalized:
        return None
    try:
        lexer = shlex.shlex(normalized, posix=True, punctuation_chars="|&;")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return None


def _semantic_verification_rejection_reason(
    raw: str,
    parsed_parts: tuple[str, ...],
) -> str:
    tokens = _shell_tokens(raw)
    if tokens and _has_failure_masking_control_flow(tokens):
        return "failure_masking_control_flow"

    unwrapped = _normalize_and_unwrap_shell_command(raw)
    try:
        parts = tuple(shlex.split(unwrapped, posix=True))
    except ValueError:
        parts = parsed_parts
    if _is_vacuous_success_command(parts):
        return "vacuous_verifier"
    if _is_non_assertive_observation_command(parts):
        return "non_assertive_observation"
    if _is_non_assertive_python_snippet(parts):
        return "vacuous_verifier"
    if _is_compile_only_python_command(parts):
        return "non_assertive_observation"
    return ""


def _has_failure_masking_control_flow(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens[:-1]):
        if token not in {"||", ";", "|"}:
            continue
        right = _next_simple_command_tokens(tokens[index + 1 :])
        if _is_vacuous_success_command(tuple(right)):
            return True
        if token != "|" and _is_non_assertive_observation_command(tuple(right)):
            return True
    return False


def _next_simple_command_tokens(tokens: list[str]) -> list[str]:
    out: list[str] = []
    for token in tokens:
        if token in _DISALLOWED_VERIFICATION_SHELL_TOKENS:
            break
        out.append(token)
    return out


def _command_head(parts: tuple[str, ...]) -> str:
    if not parts:
        return ""
    head = Path(parts[0].replace("\\", "/")).name.casefold()
    if head.endswith(".exe"):
        head = head[:-4]
    return head


def _is_vacuous_success_command(parts: tuple[str, ...]) -> bool:
    if not parts:
        return False
    head = _command_head(parts)
    if len(parts) == 1 and (head in _VACUOUS_SUCCESS_HEADS or parts[0].strip() == ":"):
        return True
    if len(parts) == 2 and head in {"exit", "return"} and parts[1] == "0":
        return True
    return _is_non_assertive_python_snippet(parts)


def _is_non_assertive_observation_command(parts: tuple[str, ...]) -> bool:
    if not parts:
        return False
    head = _command_head(parts)
    if head in {"grep"}:
        return False
    return head in _NON_ASSERTIVE_OBSERVATION_HEADS


def _is_non_assertive_python_snippet(parts: tuple[str, ...]) -> bool:
    if not _is_python_interpreter_snippet(parts):
        return False
    snippet = " ".join(parts[2:]).strip().casefold()
    compact = "".join(snippet.split())
    if compact in {
        "pass",
        "print('ok')",
        'print("ok")',
        "print(1)",
        "sys.exit(0)",
        "raisesystemexit(0)",
    }:
        return True
    if compact.startswith("importsys;") and compact.endswith("sys.exit(0)"):
        return True
    return False


def _is_compile_only_python_command(parts: tuple[str, ...]) -> bool:
    if len(parts) < 3:
        return False
    head = _command_head(parts)
    return (
        head in _PYTHON_EXECUTABLES
        and parts[1] == "-m"
        and parts[2]
        in {
            "compileall",
            "py_compile",
        }
    )


def _parse_verification_command_shape(raw: str) -> tuple[str, ...] | None:
    try:
        parts = shlex.split(raw, posix=True)
    except ValueError:
        return None
    if not parts:
        return None
    while parts and _looks_like_env_assignment(parts[0]):
        parts = parts[1:]
    if not parts:
        return None
    if parts[0] == "env":
        parts = parts[1:]
        while parts and _looks_like_env_assignment(parts[0]):
            parts = parts[1:]
    if (
        len(parts) >= 3
        and Path(parts[0]).name.casefold() in _PYTHON_EXECUTABLES
        and parts[1] == "-m"
    ):
        parts = [parts[2], *parts[3:]]
    if not parts:
        return None
    head = Path(parts[0]).name.casefold()
    if head not in _SUPPORTED_VERIFY_HEADS:
        return None
    if head in {"test", "["}:
        return tuple(parts) if len(parts) >= 3 else None
    if head in {"cmp", "diff"}:
        operands = [part for part in parts[1:] if part != "--" and not part.startswith("-")]
        return tuple(parts) if len(operands) >= 2 else None
    if head == "grep":
        has_quiet = any(part in {"-q", "--quiet"} for part in parts[1:])
        operands = [part for part in parts[1:] if part != "--" and not part.startswith("-")]
        return tuple(parts) if has_quiet and len(operands) >= 2 else None
    if head == "ruff" and (len(parts) < 2 or parts[1] != "check"):
        return None
    if head in {"go", "cargo", "npm", "pnpm", "yarn"} and len(parts) < 2:
        return None
    if head == "go" and parts[1] != "test":
        return None
    if head == "cargo" and parts[1] not in {"test", "check"}:
        return None
    if head in {"npm", "pnpm", "yarn"} and parts[1] != "test":
        return None
    return tuple(parts)


def _looks_like_env_assignment(part: str) -> bool:
    if "=" not in part or not part:
        return False
    name = part.split("=", 1)[0]
    return name.replace("_", "a").isalnum() and not name[0].isdigit()


def _is_python_interpreter_snippet(parts: tuple[str, ...]) -> bool:
    return (
        len(parts) >= 3
        and Path(parts[0]).name.casefold() in _PYTHON_EXECUTABLES
        and parts[1] == "-c"
    )


def _stable_command_id(
    *,
    source: str,
    contract_type: str,
    command: str,
    index: int,
) -> str:
    digest = hashlib.sha256(f"{source}\0{contract_type}\0{index}\0{command}".encode()).hexdigest()
    return f"vc_{digest[:16]}"


FORGE_VERIFICATION_CONTRACT_SCHEMA = "alysis.forge.verification-contract/v2"
FORGE_VERIFICATION_CONTRACT_PROTOCOL_VERSION = 2
FORGE_INTEGRATION_TASK_ID = "integration"
COMPUTED_FINGERPRINT_FIELDS = frozenset(
    {
        "contract_fingerprint",
        "contractFingerprint",
        "fingerprint",
        "full_content_fingerprint",
    }
)

# Keep this projection in lockstep with the desktop app's
# electron/runtime/forgePlanApproval.ts. Approval binds the authored plan
# definition, not fields that workers and reviewers legitimately update while
# executing that definition.
APPROVED_PLAN_RUNTIME_FIELDS = frozenset(
    {
        "updated_at",
        "execution",
        "execution_summary",
        "last_execution",
        "last_error",
        "status_counts",
    }
)
APPROVED_PLAN_RUNTIME_TASK_FIELDS = frozenset(
    {
        "status",
        "attempts",
        "conflict_attempts",
        "review_attempts",
        "started_at",
        "finished_at",
        "completed_at",
        "last_error",
        "last_attempt",
        "last_execution",
        "verification_retry",
        "result",
        "artifacts",
        "outputs",
        "logs",
        "patches",
        "verification",
        "verification_result",
        "review",
        "review_decision",
        "review_recovery",
        "approval_reconciliation",
        "merge_commit_hash",
        "merge_commit",
        "merge_sha",
        "commit_hash",
        "worktree",
        "worktree_path",
        "remote_pr_url",
        "remote_provider",
    }
)


class ForgeVerificationContractError(ValueError):
    """Raised when a v2 Forge verification contract violates its protocol."""


class CanonicalJSONError(ForgeVerificationContractError):
    """Raised when a value cannot be represented by the canonical JSON profile."""


class ForgeVerificationPhase(StrEnum):
    TASK = "task"
    INTEGRATION = "integration"


def canonical_json_bytes(
    value: Any,
    *,
    exclude_fingerprint_fields: bool = False,
) -> bytes:
    """Serialize a JSON value using the Forge v2 canonical JSON profile.

    Object keys and all string values are NFC-normalized. Object keys are sorted,
    output is compact UTF-8, and values outside the JSON data model are rejected.
    Fingerprint fields are retained unless this function is explicitly being used
    to prepare fingerprint input.
    """

    normalized = _canonical_json_value(
        value,
        exclude_fingerprint_fields=exclude_fingerprint_fields,
        location="$",
    )
    try:
        rendered = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return rendered.encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CanonicalJSONError(f"value is not canonical JSON: {exc}") from exc


def canonical_json_dumps(
    value: Any,
    *,
    exclude_fingerprint_fields: bool = False,
) -> str:
    return canonical_json_bytes(
        value,
        exclude_fingerprint_fields=exclude_fingerprint_fields,
    ).decode("utf-8")


def canonical_json_fingerprint(value: Any) -> str:
    """Return SHA-256 hex over canonical JSON with computed fields removed."""

    canonical = canonical_json_bytes(value, exclude_fingerprint_fields=True)
    return hashlib.sha256(canonical).hexdigest()


def approved_plan_definition(approved_plan: Any) -> Any:
    """Return the immutable plan-definition view used for approval binding.

    Runtime-owned plan and task fields are excluded so status transitions,
    attempts, evidence, review results, and merge metadata do not invalidate a
    legitimate approval. Authored goal, requirements, task content, scope,
    dependencies, and branch identity remain bound.
    """

    if not isinstance(approved_plan, Mapping):
        return approved_plan

    definition = {
        key: value
        for key, value in approved_plan.items()
        if key not in APPROVED_PLAN_RUNTIME_FIELDS
    }
    tasks = definition.get("tasks")
    if not isinstance(tasks, list):
        return definition

    normalized_tasks: list[Any] = []
    for value in tasks:
        if not isinstance(value, Mapping):
            normalized_tasks.append(value)
            continue
        task = dict(value)
        reconciliation = task.get("approval_reconciliation")
        if isinstance(reconciliation, Mapping):
            previous_fields = reconciliation.get("previous_fields")
            if isinstance(previous_fields, Mapping):
                for field_name, field_value in previous_fields.items():
                    if isinstance(field_name, str):
                        task[field_name] = field_value
            absent_fields = reconciliation.get("absent_fields")
            if isinstance(absent_fields, list):
                for field_name in absent_fields:
                    if isinstance(field_name, str):
                        task.pop(field_name, None)
        recovery = task.get("review_recovery")
        widened_scope = (
            {
                item.strip()
                for item in recovery.get("widened_scope", [])
                if isinstance(recovery, Mapping) and isinstance(item, str) and item.strip()
            }
            if isinstance(recovery, Mapping)
            else set()
        )
        if widened_scope:
            for scope_field in ("write_scope", "estimated_files"):
                scope = task.get(scope_field)
                if isinstance(scope, list):
                    task[scope_field] = [
                        item
                        for item in scope
                        if not (isinstance(item, str) and item.strip() in widened_scope)
                    ]
        branch = task.get("branch")
        if branch is None or isinstance(branch, str):
            task["branch"] = (
                branch.strip() if isinstance(branch, str) else ""
            ) or _generated_approved_task_branch(task)
        for field_name in APPROVED_PLAN_RUNTIME_TASK_FIELDS:
            task.pop(field_name, None)
        normalized_tasks.append(task)
    definition["tasks"] = normalized_tasks
    return definition


def _generated_approved_task_branch(task: Mapping[str, Any]) -> str:
    task_id_value = task.get("id")
    task_id = task_id_value.strip().lower() if isinstance(task_id_value, str) else ""
    task_id = task_id or "task"

    title_value = task.get("title")
    title = title_value.strip().lower() if isinstance(title_value, str) else ""
    slug = re.sub(r"[^a-z0-9]+", "-", title).strip("-") or "task"
    if len(slug) > 40:
        slug = slug[:40].rstrip("-")
    return f"feat/{task_id}-{slug}"


def approved_plan_fingerprint(approved_plan: Any) -> str:
    """Return the canonical SHA-256 binding for an approved plan definition."""

    return canonical_json_fingerprint(approved_plan_definition(approved_plan))


# More descriptive alias for callers that expose fingerprinting as an action.
canonical_approved_plan_fingerprint = approved_plan_fingerprint
compute_approved_plan_fingerprint = approved_plan_fingerprint


def validate_approved_plan_fingerprint(approved_plan: Any, expected_fingerprint: str) -> None:
    """Require an approved plan to match an existing canonical binding."""

    _validate_sha256_hex(expected_fingerprint, "approved_plan_fingerprint")
    actual = approved_plan_fingerprint(approved_plan)
    if actual != expected_fingerprint:
        raise ForgeVerificationContractError(
            "approved plan does not match approved_plan_fingerprint"
        )


def contract_fingerprint(contract: Any) -> str:
    """Return the Forge v2 canonical fingerprint for a contract or mapping."""

    if isinstance(contract, ForgeVerificationContractV2):
        contract = contract.to_mapping(include_fingerprint=False)
    return canonical_json_fingerprint(contract)


compute_contract_fingerprint = contract_fingerprint


def normalize_contract_path(value: str) -> str:
    """Normalize a contract-owned repository path and reject escaping forms."""

    if not isinstance(value, str):
        raise ForgeVerificationContractError("contract paths must be strings")
    normalized = unicodedata.normalize("NFC", value)
    if not normalized or "\x00" in normalized:
        raise ForgeVerificationContractError("contract paths cannot be empty")

    slash_path = normalized.replace("\\", "/")
    windows_path = PureWindowsPath(normalized)
    posix_path = PurePosixPath(slash_path)
    if (
        slash_path.startswith("/")
        or posix_path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
    ):
        raise ForgeVerificationContractError(f"absolute contract path is not allowed: {value!r}")

    raw_parts = slash_path.split("/")
    if ".." in raw_parts:
        raise ForgeVerificationContractError(f"contract path traversal is not allowed: {value!r}")
    parts = [part for part in raw_parts if part not in {"", "."}]
    if not parts:
        raise ForgeVerificationContractError("contract paths cannot be empty or '.'")
    return "/".join(parts)


@dataclass(frozen=True)
class ForgeVerificationCriterion:
    criterion_id: str
    text: str
    provenance: VerificationCommandProvenance
    trust_level: VerificationCommandTrustLevel
    requirement: VerificationCommandRequirement

    def __post_init__(self) -> None:
        object.__setattr__(self, "criterion_id", _nonempty_nfc(self.criterion_id, "criterion_id"))
        object.__setattr__(self, "text", _nonempty_nfc(self.text, "criterion text"))
        try:
            object.__setattr__(self, "provenance", VerificationCommandProvenance(self.provenance))
            object.__setattr__(self, "trust_level", VerificationCommandTrustLevel(self.trust_level))
            object.__setattr__(
                self, "requirement", VerificationCommandRequirement(self.requirement)
            )
        except ValueError as exc:
            raise ForgeVerificationContractError(f"invalid criterion policy: {exc}") from exc

    def to_mapping(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "text": self.text,
            "provenance": self.provenance.value,
            "trust_level": self.trust_level.value,
            "requirement": self.requirement.value,
        }

    as_payload = to_mapping

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ForgeVerificationCriterion:
        if not isinstance(payload, Mapping):
            raise ForgeVerificationContractError("criterion must be an object")
        _require_exact_fields(
            payload,
            {"criterion_id", "text", "provenance", "trust_level", "requirement"},
            "verification criterion",
        )
        try:
            return cls(
                criterion_id=_required_string(payload, "criterion_id"),
                text=_required_string(payload, "text"),
                provenance=VerificationCommandProvenance(_required_string(payload, "provenance")),
                trust_level=VerificationCommandTrustLevel(_required_string(payload, "trust_level")),
                requirement=VerificationCommandRequirement(
                    _required_string(payload, "requirement")
                ),
            )
        except ValueError as exc:
            if isinstance(exc, ForgeVerificationContractError):
                raise
            raise ForgeVerificationContractError(f"invalid criterion: {exc}") from exc


@dataclass(frozen=True)
class ForgeTaskVerificationContract:
    phase: ForgeVerificationPhase
    task_id: str | None
    cwd: str
    referenced_paths: tuple[str, ...]
    allowed_outputs: tuple[str, ...]
    declared_inputs: tuple[str, ...]
    criteria: tuple[ForgeVerificationCriterion, ...]
    commands: tuple[VerificationCommandSpec, ...]

    def __post_init__(self) -> None:
        try:
            phase = ForgeVerificationPhase(self.phase)
        except ValueError as exc:
            raise ForgeVerificationContractError(
                f"invalid verification phase: {self.phase!r}"
            ) from exc
        object.__setattr__(self, "phase", phase)

        task_id = self.task_id
        if task_id is not None:
            task_id = _nonempty_nfc(task_id, "task_id")
        if phase == ForgeVerificationPhase.TASK:
            if task_id is None:
                raise ForgeVerificationContractError("task phase requires a task_id")
            if task_id == FORGE_INTEGRATION_TASK_ID:
                raise ForgeVerificationContractError(
                    f"task phase cannot use reserved task_id {FORGE_INTEGRATION_TASK_ID!r}"
                )
        elif task_id not in {None, FORGE_INTEGRATION_TASK_ID}:
            raise ForgeVerificationContractError(
                "integration phase task_id must be 'integration' or null"
            )
        object.__setattr__(self, "task_id", task_id)
        object.__setattr__(self, "cwd", _nonempty_nfc(self.cwd, "cwd"))

        referenced = _normalized_paths(self.referenced_paths, "referenced_paths")
        outputs = _normalized_paths(self.allowed_outputs, "allowed_outputs")
        inputs = _normalized_paths(self.declared_inputs, "declared_inputs")
        outside_scope = set(referenced) - set(outputs) - set(inputs)
        if outside_scope:
            rendered = ", ".join(sorted(outside_scope))
            raise ForgeVerificationContractError(
                "referenced_paths must be a subset of allowed_outputs union declared_inputs; "
                f"out of scope: {rendered}"
            )
        object.__setattr__(self, "referenced_paths", referenced)
        object.__setattr__(self, "allowed_outputs", outputs)
        object.__setattr__(self, "declared_inputs", inputs)

        criteria = tuple(self.criteria)
        if not all(isinstance(item, ForgeVerificationCriterion) for item in criteria):
            raise ForgeVerificationContractError(
                "criteria must contain ForgeVerificationCriterion values"
            )
        criterion_ids = [item.criterion_id for item in criteria]
        _reject_duplicates(criterion_ids, "criterion IDs")
        object.__setattr__(self, "criteria", criteria)

        commands = tuple(_normalized_command_spec(item) for item in self.commands)
        if not all(isinstance(item, VerificationCommandSpec) for item in commands):
            raise ForgeVerificationContractError(
                "commands must contain VerificationCommandSpec values"
            )
        _reject_duplicates([item.command_id for item in commands], "command IDs")
        if phase == ForgeVerificationPhase.TASK:
            known_criteria = set(criterion_ids)
            for command in commands:
                unknown_criteria = set(command.acceptance_criterion_ids) - known_criteria
                if unknown_criteria:
                    rendered = ", ".join(sorted(unknown_criteria))
                    raise ForgeVerificationContractError(
                        f"task command references unknown criterion IDs: {rendered}"
                    )
        object.__setattr__(self, "commands", commands)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "task_id": self.task_id,
            "cwd": self.cwd,
            "referenced_paths": list(self.referenced_paths),
            "allowed_outputs": list(self.allowed_outputs),
            "declared_inputs": list(self.declared_inputs),
            "criteria": [item.to_mapping() for item in self.criteria],
            "commands": [item.as_payload() for item in self.commands],
        }

    as_payload = to_mapping

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ForgeTaskVerificationContract:
        if not isinstance(payload, Mapping):
            raise ForgeVerificationContractError("task verification contract must be an object")
        _require_exact_fields(
            payload,
            {
                "phase",
                "task_id",
                "cwd",
                "referenced_paths",
                "allowed_outputs",
                "declared_inputs",
                "criteria",
                "commands",
            },
            "task verification contract",
        )
        task_id = payload.get("task_id")
        if task_id is not None and not isinstance(task_id, str):
            raise ForgeVerificationContractError("task_id must be a string or null")
        criteria_payload = _object_sequence(payload.get("criteria", ()), "criteria")
        command_payload = _object_sequence(payload.get("commands", ()), "commands")
        try:
            return cls(
                phase=ForgeVerificationPhase(_required_string(payload, "phase")),
                task_id=task_id,
                cwd=_required_string(payload, "cwd"),
                referenced_paths=_string_sequence(
                    payload.get("referenced_paths", ()), "referenced_paths"
                ),
                allowed_outputs=_string_sequence(
                    payload.get("allowed_outputs", ()), "allowed_outputs"
                ),
                declared_inputs=_string_sequence(
                    payload.get("declared_inputs", ()), "declared_inputs"
                ),
                criteria=tuple(
                    ForgeVerificationCriterion.from_mapping(item) for item in criteria_payload
                ),
                commands=tuple(
                    VerificationCommandSpec.from_payload(item) for item in command_payload
                ),
            )
        except ValueError as exc:
            if isinstance(exc, ForgeVerificationContractError):
                raise
            raise ForgeVerificationContractError(f"invalid task contract: {exc}") from exc


@dataclass(frozen=True)
class ForgeVerificationContractV2:
    workspace_root: str
    approved_plan_fingerprint: str
    tasks: tuple[ForgeTaskVerificationContract, ...]
    integration: ForgeTaskVerificationContract | None = None
    schema: str = FORGE_VERIFICATION_CONTRACT_SCHEMA
    protocol_version: int = FORGE_VERIFICATION_CONTRACT_PROTOCOL_VERSION
    contract_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        schema = _nonempty_nfc(self.schema, "schema")
        if schema != FORGE_VERIFICATION_CONTRACT_SCHEMA:
            raise ForgeVerificationContractError(
                f"unsupported schema {schema!r}; expected {FORGE_VERIFICATION_CONTRACT_SCHEMA!r}"
            )
        if type(self.protocol_version) is not int or (
            self.protocol_version != FORGE_VERIFICATION_CONTRACT_PROTOCOL_VERSION
        ):
            raise ForgeVerificationContractError(
                "unsupported Forge verification contract protocol version"
            )
        object.__setattr__(self, "schema", schema)
        workspace_root = _nonempty_nfc(self.workspace_root, "workspace_root")
        if not (
            PurePosixPath(workspace_root).is_absolute()
            or PureWindowsPath(workspace_root).is_absolute()
        ):
            raise ForgeVerificationContractError("workspace_root must be an absolute path")
        object.__setattr__(self, "workspace_root", workspace_root)
        _validate_sha256_hex(self.approved_plan_fingerprint, "approved_plan_fingerprint")

        tasks = tuple(self.tasks)
        if not all(isinstance(item, ForgeTaskVerificationContract) for item in tasks):
            raise ForgeVerificationContractError(
                "tasks must contain ForgeTaskVerificationContract values"
            )
        if any(item.phase != ForgeVerificationPhase.TASK for item in tasks):
            raise ForgeVerificationContractError(
                "tasks collection may contain only task phase entries"
            )
        tasks = tuple(sorted(tasks, key=lambda item: item.task_id or ""))
        _reject_duplicates([item.task_id or "" for item in tasks], "task IDs")
        object.__setattr__(self, "tasks", tasks)

        if self.integration is not None and (
            not isinstance(self.integration, ForgeTaskVerificationContract)
            or self.integration.phase != ForgeVerificationPhase.INTEGRATION
        ):
            raise ForgeVerificationContractError(
                "integration must be an integration-phase ForgeTaskVerificationContract"
            )

        all_commands = [
            command.command_id
            for item in (*tasks, *((self.integration,) if self.integration is not None else ()))
            for command in item.commands
        ]
        _reject_duplicates(all_commands, "command IDs within contract")
        all_criterion_ids = [
            criterion.criterion_id
            for item in (*tasks, *((self.integration,) if self.integration is not None else ()))
            for criterion in item.criteria
        ]
        _reject_duplicates(all_criterion_ids, "criterion IDs within contract")
        if self.integration is not None:
            known_criteria = set(all_criterion_ids)
            for command in self.integration.commands:
                unknown_criteria = set(command.acceptance_criterion_ids) - known_criteria
                if unknown_criteria:
                    rendered = ", ".join(sorted(unknown_criteria))
                    raise ForgeVerificationContractError(
                        f"integration command references unknown criterion IDs: {rendered}"
                    )
        object.__setattr__(
            self,
            "contract_fingerprint",
            contract_fingerprint(self.to_mapping(include_fingerprint=False)),
        )

    def to_mapping(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": self.schema,
            "protocol_version": self.protocol_version,
            "workspace_root": self.workspace_root,
            "approved_plan_fingerprint": self.approved_plan_fingerprint,
            "tasks": {item.task_id: item.to_mapping() for item in self.tasks},
            "integration": self.integration.to_mapping() if self.integration is not None else None,
        }
        if include_fingerprint:
            payload["contract_fingerprint"] = self.contract_fingerprint
        return payload

    as_payload = to_mapping

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def to_json(self) -> str:
        return self.to_json_bytes().decode("utf-8")

    def validate_approved_plan(self, approved_plan: Any) -> None:
        validate_approved_plan_fingerprint(approved_plan, self.approved_plan_fingerprint)

    @classmethod
    def for_approved_plan(
        cls,
        approved_plan: Any,
        *,
        workspace_root: str,
        tasks: Sequence[ForgeTaskVerificationContract],
        integration: ForgeTaskVerificationContract | None = None,
    ) -> ForgeVerificationContractV2:
        return cls(
            workspace_root=workspace_root,
            approved_plan_fingerprint=approved_plan_fingerprint(approved_plan),
            tasks=tuple(tasks),
            integration=integration,
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ForgeVerificationContractV2:
        if not isinstance(payload, Mapping):
            raise ForgeVerificationContractError("verification contract must be an object")
        required_fields = {
            "schema",
            "protocol_version",
            "workspace_root",
            "approved_plan_fingerprint",
            "tasks",
            "integration",
        }
        _require_exact_fields(
            payload,
            required_fields | {"contract_fingerprint"},
            "verification contract",
            required=required_fields,
        )
        tasks_payload = payload.get("tasks")
        if not isinstance(tasks_payload, Mapping):
            raise ForgeVerificationContractError("tasks must be a task-keyed object")

        tasks: list[ForgeTaskVerificationContract] = []
        normalized_task_keys: set[str] = set()
        for raw_task_id, task_payload in tasks_payload.items():
            if not isinstance(raw_task_id, str):
                raise ForgeVerificationContractError("task keys must be strings")
            task_key = _nonempty_nfc(raw_task_id, "task key")
            if task_key in normalized_task_keys:
                raise ForgeVerificationContractError(
                    f"duplicate task key after NFC normalization: {task_key!r}"
                )
            normalized_task_keys.add(task_key)
            task = ForgeTaskVerificationContract.from_mapping(task_payload)
            if task.task_id != task_key:
                raise ForgeVerificationContractError(
                    f"task key {task_key!r} does not match task_id {task.task_id!r}"
                )
            tasks.append(task)

        integration_payload = payload.get("integration")
        if integration_payload is not None and not isinstance(integration_payload, Mapping):
            raise ForgeVerificationContractError("integration must be an object or null")
        integration = (
            ForgeTaskVerificationContract.from_mapping(integration_payload)
            if integration_payload is not None
            else None
        )
        try:
            result = cls(
                schema=_required_string(payload, "schema"),
                protocol_version=_required_int(payload, "protocol_version"),
                workspace_root=_required_string(payload, "workspace_root"),
                approved_plan_fingerprint=_required_string(payload, "approved_plan_fingerprint"),
                tasks=tuple(tasks),
                integration=integration,
            )
        except ValueError as exc:
            if isinstance(exc, ForgeVerificationContractError):
                raise
            raise ForgeVerificationContractError(f"invalid verification contract: {exc}") from exc

        supplied_fingerprint = payload.get("contract_fingerprint")
        if supplied_fingerprint is not None:
            supplied_fingerprint = _string_value(supplied_fingerprint, "contract_fingerprint")
            _validate_sha256_hex(supplied_fingerprint, "contract_fingerprint")
            if supplied_fingerprint != result.contract_fingerprint:
                raise ForgeVerificationContractError(
                    "contract_fingerprint does not match the canonical contract payload"
                )
        return result

    @classmethod
    def from_json(cls, payload: str | bytes | bytearray) -> ForgeVerificationContractV2:
        if isinstance(payload, str):
            raw = payload
        elif isinstance(payload, (bytes, bytearray)):
            try:
                raw = bytes(payload).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ForgeVerificationContractError("contract JSON must be UTF-8") from exc
        else:
            raise ForgeVerificationContractError("contract JSON must be str or bytes")
        try:
            decoded = json.loads(
                raw,
                parse_constant=lambda value: _raise_nonfinite_json(value),
                object_pairs_hook=_json_object_without_normalized_key_collisions,
            )
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ForgeVerificationContractError(f"invalid contract JSON: {exc}") from exc
        return cls.from_mapping(decoded)


# Short aliases keep the v2 vocabulary ergonomic without changing legacy APIs.
VerificationContractV2 = ForgeVerificationContractV2
VerificationTaskContract = ForgeTaskVerificationContract
VerificationCriterion = ForgeVerificationCriterion
VerificationPhase = ForgeVerificationPhase


def _canonical_json_value(
    value: Any,
    *,
    exclude_fingerprint_fields: bool,
    location: str,
) -> Any:
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if value < -(2**53 - 1) or value > 2**53 - 1:
            raise CanonicalJSONError(f"integer outside the safe range at {location}")
        return value
    if type(value) is float:
        if not math.isfinite(value) or not value.is_integer():
            raise CanonicalJSONError(f"non-integer or non-finite number at {location}")
        integer = int(value)
        if integer < -(2**53 - 1) or integer > 2**53 - 1:
            raise CanonicalJSONError(f"integer outside the safe range at {location}")
        return integer
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        original_keys: dict[str, str] = {}
        seen_keys: set[str] = set()
        for key, child in value.items():
            if not isinstance(key, str):
                raise CanonicalJSONError(f"object key at {location} must be a string")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in seen_keys:
                first = original_keys[normalized_key]
                raise CanonicalJSONError(
                    f"duplicate object key after NFC normalization at {location}: "
                    f"{first!r} and {key!r}"
                )
            seen_keys.add(normalized_key)
            original_keys[normalized_key] = key
            if exclude_fingerprint_fields and normalized_key in COMPUTED_FINGERPRINT_FIELDS:
                continue
            normalized[normalized_key] = _canonical_json_value(
                child,
                exclude_fingerprint_fields=exclude_fingerprint_fields,
                location=f"{location}.{normalized_key}",
            )
        return normalized
    if isinstance(value, list):
        return [
            _canonical_json_value(
                child,
                exclude_fingerprint_fields=exclude_fingerprint_fields,
                location=f"{location}[{index}]",
            )
            for index, child in enumerate(value)
        ]
    raise CanonicalJSONError(
        f"unsupported value at {location}: {type(value).__name__}; "
        "expected null, boolean, number, string, array, or object"
    )


def _nonempty_nfc(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ForgeVerificationContractError(f"{field_name} must be a string")
    normalized = unicodedata.normalize("NFC", value)
    if not normalized or "\x00" in normalized:
        raise ForgeVerificationContractError(f"{field_name} cannot be empty")
    return normalized


def _normalized_paths(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise ForgeVerificationContractError(f"{field_name} must be an array of paths")
    normalized = tuple(normalize_contract_path(item) for item in values)
    _reject_duplicates(normalized, f"{field_name} paths")
    return tuple(sorted(normalized))


def _normalized_command_spec(value: Any) -> VerificationCommandSpec:
    if not isinstance(value, VerificationCommandSpec):
        raise ForgeVerificationContractError("commands must contain VerificationCommandSpec values")
    command_id = _nonempty_nfc(value.command_id, "command_id")
    try:
        return VerificationCommandSpec(
            command_id=command_id,
            original_text=_string_value(value.original_text, "original_text"),
            display_text=_string_value(value.display_text, "display_text"),
            execution_mode=VerificationCommandExecutionMode(value.execution_mode),
            provenance=VerificationCommandProvenance(value.provenance),
            trust_level=VerificationCommandTrustLevel(value.trust_level),
            requirement=VerificationCommandRequirement(value.requirement),
            working_directory=_string_value(value.working_directory, "working_directory"),
            timeout_policy=_string_value(value.timeout_policy, "timeout_policy"),
            acceptance_criterion_ids=tuple(
                _nonempty_nfc(item, "acceptance_criterion_id")
                for item in value.acceptance_criterion_ids
            ),
            validation_status=VerificationCommandValidationStatus(value.validation_status),
            rejection_reason=_string_value(value.rejection_reason, "rejection_reason"),
            evidentiary_capability=VerificationCommandEvidentiaryCapability(
                value.evidentiary_capability
            ),
            capability_reason=_string_value(value.capability_reason, "capability_reason"),
            argv=tuple(_string_value(item, "argv") for item in value.argv),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ForgeVerificationContractError):
            raise
        raise ForgeVerificationContractError(f"invalid command spec: {exc}") from exc


def _reject_duplicates(values: Sequence[str], label: str) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        rendered = ", ".join(repr(value) for value in sorted(duplicates))
        raise ForgeVerificationContractError(f"duplicate {label}: {rendered}")


def _require_exact_fields(
    payload: Mapping[str, Any],
    allowed: set[str],
    label: str,
    *,
    required: set[str] | None = None,
) -> None:
    fields = set(payload)
    unknown = fields - allowed
    if unknown:
        raise ForgeVerificationContractError(
            f"{label} contains unknown fields: {', '.join(sorted(unknown))}"
        )
    missing = (required if required is not None else allowed) - fields
    if missing:
        raise ForgeVerificationContractError(
            f"{label} is missing fields: {', '.join(sorted(missing))}"
        )


def _validate_sha256_hex(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ForgeVerificationContractError(f"{field_name} must be lowercase SHA-256 hex")
    if value != value.lower() or any(character not in "0123456789abcdef" for character in value):
        raise ForgeVerificationContractError(f"{field_name} must be lowercase SHA-256 hex")


def _string_value(value: Any, field_name: str) -> str:
    if isinstance(value, StrEnum):
        value = value.value
    if not isinstance(value, str):
        raise ForgeVerificationContractError(f"{field_name} must be a string")
    return unicodedata.normalize("NFC", value)


def _required_string(payload: Mapping[str, Any], field_name: str) -> str:
    if field_name not in payload:
        raise ForgeVerificationContractError(f"missing required field: {field_name}")
    return _nonempty_nfc(payload[field_name], field_name)


def _required_int(payload: Mapping[str, Any], field_name: str) -> int:
    if field_name not in payload or type(payload[field_name]) is not int:
        raise ForgeVerificationContractError(f"{field_name} must be an integer")
    return payload[field_name]


def _string_sequence(value: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ForgeVerificationContractError(f"{field_name} must be an array of strings")
    return tuple(_string_value(item, field_name) for item in value)


def _object_sequence(value: Any, field_name: str) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ForgeVerificationContractError(f"{field_name} must be an array of objects")
    if not all(isinstance(item, Mapping) for item in value):
        raise ForgeVerificationContractError(f"{field_name} must be an array of objects")
    return tuple(value)


def _raise_nonfinite_json(value: str) -> Any:
    raise ForgeVerificationContractError(f"non-finite JSON number is not allowed: {value}")


def _json_object_without_normalized_key_collisions(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    original_keys: dict[str, str] = {}
    for key, value in pairs:
        normalized = unicodedata.normalize("NFC", key)
        if normalized in result:
            raise ForgeVerificationContractError(
                "duplicate JSON object key after NFC normalization: "
                f"{original_keys[normalized]!r} and {key!r}"
            )
        original_keys[normalized] = key
        result[normalized] = value
    return result
