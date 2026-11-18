from __future__ import annotations

import hashlib
import shlex
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
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
            raise VerificationContractError("command spec must be an object")
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
            if isinstance(exc, VerificationContractError):
                raise
            raise VerificationContractError(f"invalid command spec: {exc}") from exc


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


class VerificationContractError(ValueError):
    """Raised when a verification command specification is invalid."""


def _nonempty_nfc(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise VerificationContractError(f"{field_name} must be a string")
    normalized = unicodedata.normalize("NFC", value)
    if not normalized or "\x00" in normalized:
        raise VerificationContractError(f"{field_name} cannot be empty")
    return normalized


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
        raise VerificationContractError(
            f"{label} contains unknown fields: {', '.join(sorted(unknown))}"
        )
    missing = (required if required is not None else allowed) - fields
    if missing:
        raise VerificationContractError(f"{label} is missing fields: {', '.join(sorted(missing))}")


def _string_value(value: Any, field_name: str) -> str:
    if isinstance(value, StrEnum):
        value = value.value
    if not isinstance(value, str):
        raise VerificationContractError(f"{field_name} must be a string")
    return unicodedata.normalize("NFC", value)


def _required_string(payload: Mapping[str, Any], field_name: str) -> str:
    if field_name not in payload:
        raise VerificationContractError(f"missing required field: {field_name}")
    return _nonempty_nfc(payload[field_name], field_name)


def _string_sequence(value: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise VerificationContractError(f"{field_name} must be an array of strings")
    return tuple(_string_value(item, field_name) for item in value)
