from __future__ import annotations

import hashlib
import os
import re
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from .config import (
    VERIFY_PYTHON_LAUNCHER_RE,
    normalize_verify_module_invocation,
    split_verify_command_parts,
    strip_verify_runner_prefix,
)
from .windows_verification_command import (
    is_windows_verifier_command,
    split_windows_verifier_command,
)


class VerificationCommandEvidentiaryCapability(StrEnum):
    ASSERTIVE = "ASSERTIVE"
    NON_ASSERTIVE = "NON_ASSERTIVE"
    UNKNOWN = "UNKNOWN"


class VerificationCommandStatus(StrEnum):
    PASSED = "passed"
    SKIPPED = "skipped"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    NOT_EXECUTED = "not_executed"
    STALE = "stale"


@dataclass(frozen=True)
class CheckerEntrypointFingerprint:
    display_path: str
    resolved_path: str
    is_regular_file: bool
    size: int | None
    sha256: str | None

    def as_payload(self) -> dict[str, object]:
        return {
            "display_path": self.display_path,
            "resolved_path": self.resolved_path,
            "is_regular_file": self.is_regular_file,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class VerificationCommandAnalysis:
    original_command: str
    normalized_command: str
    unwrapped_command: str
    primary_command: str
    canonical_command: str | None
    parts: tuple[str, ...]
    command_family: str | None
    provenance_source: str = ""
    trust_source: str = ""
    evidentiary_capability: VerificationCommandEvidentiaryCapability = (
        VerificationCommandEvidentiaryCapability.UNKNOWN
    )
    capability_reason: str = ""
    shell_control_flow: str = "none"
    pipeline_policy: str = "none"
    checker_entrypoint_paths: tuple[str, ...] = tuple()
    checker_integrity_required: bool = False
    checker_fingerprints: tuple[CheckerEntrypointFingerprint, ...] = tuple()
    real_execution_required: bool = True
    rejection_reason: str = ""
    inconclusive_reason: str = ""
    cd_target: str | None = None

    @property
    def is_valid_verifier(self) -> bool:
        return (
            not self.rejection_reason
            and self.evidentiary_capability == VerificationCommandEvidentiaryCapability.ASSERTIVE
        )

    @property
    def uses_opaque_inline_code(self) -> bool:
        """Whether a non-shell interpreter receives opaque inline source."""
        return _command_uses_opaque_inline_code(self.normalized_command)


_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_SHELL_CONTROL_TOKENS = {"||", "&&", ";", "|", "&"}
_SHELL_WRAPPER_HEADS = {"bash", "sh", "zsh"}
_SHELL_CLUSTERABLE_SHORT_OPTIONS = frozenset("abcefhiklmnprstuvxBCEHPT")
_PYTHON_EXECUTABLES = {"python", "python3", "py"}
_INTERPRETER_INLINE_CODE_OPTIONS = {
    "bash": {"-c"},
    "node": {"-e", "--eval", "-p", "--print"},
    "python": {"-c"},
    "python3": {"-c"},
    "py": {"-c"},
    "r": {"-e", "--expression"},
    "rscript": {"-e", "--expression"},
    "ruby": {"-e", "--eval"},
    "sh": {"-c"},
    "zsh": {"-c"},
}
_INTERPRETER_OPTIONS_WITH_SEPARATE_VALUES = {
    "bash": {"-o", "-O"},
    "node": {"-r", "--require", "--import", "--loader"},
    "python": {"-W", "-X", "--check-hash-based-pycs"},
    "python3": {"-W", "-X", "--check-hash-based-pycs"},
    "py": {"-W", "-X", "--check-hash-based-pycs"},
    "r": {"-f", "--file"},
    "rscript": {"-f", "--file"},
    "ruby": {"-C", "-I", "-r"},
    "sh": {"-o"},
    "zsh": {"-o"},
}
_VACUOUS_SUCCESS_HEADS = {"true", ":"}
_OBSERVATION_HEADS = {
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
    "tee",
    "type",
    "wc",
    "which",
}
_VALIDATION_COMMAND_PATH_MARKERS = {
    "accept",
    "acceptance",
    "check",
    "smoke",
    "test",
    "validate",
    "validation",
    "verify",
}
_META_OPTIONS = {"--help", "-h", "--version", "version", "help"}
_PYTEST_NON_VERIFICATION_OPTIONS = {"--fixtures", "--markers", "--collect-only", "--co"}
_PYTEST_NON_EXECUTING_OPTIONS = {"--setup-plan"}
_CARGO_TEST_NON_EXECUTING_OPTIONS = {"--no-run", "--list"}
_GO_TEST_NON_EXECUTING_OPTIONS = {"-c", "-list"}
_RUFF_CHECK_NON_EXECUTING_OPTIONS = {"--fix", "--fix-only"}
_MYPY_NON_EXECUTING_OPTIONS = {"--install-types"}
_NODE_ASSERTIVE_SCRIPTS = {"test", "build", "lint", "typecheck", "check", "verify"}
_TEST_SURFACE_PARTS = frozenset(
    {"__tests__", "fixture", "fixtures", "spec", "specs", "test", "tests"}
)

_PYTHON_VERIFICATION_SUFFIXES = frozenset({".py", ".pyi", ".toml", ".ini", ".cfg"})
_NODE_VERIFICATION_SUFFIXES = frozenset(
    {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".json"}
)
_GO_VERIFICATION_SUFFIXES = frozenset({".go", ".mod", ".sum"})
_RUST_VERIFICATION_SUFFIXES = frozenset({".rs", ".toml", ".lock"})
_JVM_VERIFICATION_SUFFIXES = frozenset(
    {".java", ".kt", ".kts", ".groovy", ".scala", ".xml", ".gradle"}
)
_DOTNET_VERIFICATION_SUFFIXES = frozenset({".cs", ".fs", ".vb", ".csproj", ".fsproj", ".sln"})
_ELIXIR_VERIFICATION_SUFFIXES = frozenset({".ex", ".exs"})
_ASSERTION_VERIFICATION_SUFFIXES = frozenset(
    {
        *_PYTHON_VERIFICATION_SUFFIXES,
        *_NODE_VERIFICATION_SUFFIXES,
        *_GO_VERIFICATION_SUFFIXES,
        *_RUST_VERIFICATION_SUFFIXES,
        *_JVM_VERIFICATION_SUFFIXES,
        *_DOTNET_VERIFICATION_SUFFIXES,
        *_ELIXIR_VERIFICATION_SUFFIXES,
    }
)
_TOOLCHAIN_BASENAMES: dict[str, frozenset[str]] = {
    "python": frozenset({"pyproject.toml", "setup.cfg", "setup.py", "tox.ini"}),
    "node": frozenset({"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"}),
    "go": frozenset({"go.mod", "go.sum"}),
    "rust": frozenset({"cargo.toml", "cargo.lock"}),
    "jvm": frozenset({"pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle"}),
    "dotnet": frozenset({"global.json", "nuget.config"}),
    "elixir": frozenset({"mix.exs", "mix.lock"}),
    "broad": frozenset({"makefile", "justfile"}),
}
_VERIFICATION_FAMILY_TOOLCHAINS: dict[str, str] = {
    "pytest": "python",
    "unittest": "python",
    "mypy": "python",
    "ruff:check": "python",
    "node:test": "node",
    "npm:test": "node",
    "npm:build": "node",
    "npm:lint": "node",
    "npm:typecheck": "node",
    "npm:check": "node",
    "npm:verify": "node",
    "pnpm:test": "node",
    "pnpm:build": "node",
    "pnpm:lint": "node",
    "pnpm:typecheck": "node",
    "pnpm:check": "node",
    "pnpm:verify": "node",
    "yarn:test": "node",
    "yarn:build": "node",
    "yarn:lint": "node",
    "yarn:typecheck": "node",
    "yarn:check": "node",
    "yarn:verify": "node",
    "go:test": "go",
    "cargo:test": "rust",
    "cargo:check": "rust",
    "maven:test": "jvm",
    "maven:verify": "jvm",
    "gradle:test": "jvm",
    "dotnet:test": "dotnet",
    "mix:test": "elixir",
    "make:test": "broad",
    "make:check": "broad",
    "make:verify": "broad",
    "just:test": "broad",
    "just:check": "broad",
    "just:verify": "broad",
}
_TOOLCHAIN_SUFFIXES: dict[str, frozenset[str]] = {
    "python": _PYTHON_VERIFICATION_SUFFIXES,
    "node": _NODE_VERIFICATION_SUFFIXES,
    "go": _GO_VERIFICATION_SUFFIXES,
    "rust": _RUST_VERIFICATION_SUFFIXES,
    "jvm": _JVM_VERIFICATION_SUFFIXES,
    "dotnet": _DOTNET_VERIFICATION_SUFFIXES,
    "elixir": _ELIXIR_VERIFICATION_SUFFIXES,
    "broad": _ASSERTION_VERIFICATION_SUFFIXES,
}


def _normalize_rel_match_path(raw: str, *, strip_trailing_slash: bool = True) -> str:
    cleaned = str(raw).strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if strip_trailing_slash:
        cleaned = cleaned.rstrip("/")
    return cleaned


def path_matches_verification_toolchain(rel_path: str, toolchain: str) -> bool:
    normalized = _normalize_rel_match_path(rel_path)
    if not normalized:
        return False
    lowered = normalized.casefold()
    path = PurePosixPath(lowered)
    basename = path.name
    if basename in _TOOLCHAIN_BASENAMES.get(toolchain, frozenset()):
        return True
    if toolchain != "broad" and any(part in _TEST_SURFACE_PARTS for part in path.parts[:-1]):
        return True
    return path.suffix in _TOOLCHAIN_SUFFIXES.get(toolchain, frozenset())


def path_has_known_verification_surface(rel_path: str) -> bool:
    return any(
        path_matches_verification_toolchain(rel_path, toolchain)
        for toolchain in _TOOLCHAIN_SUFFIXES
        if toolchain != "broad"
    )


def verification_commands_apply_to_paths(
    paths: set[str] | frozenset[str],
    commands: list[str] | tuple[str, ...] | set[str] | None,
) -> bool:
    if not paths or not commands:
        return False
    toolchains: set[str] = set()
    for command in commands:
        family = analyze_verification_command(str(command), trusted=True).command_family
        if family is None:
            continue
        toolchain = _VERIFICATION_FAMILY_TOOLCHAINS.get(family)
        if toolchain is None and family.startswith("assertion:"):
            toolchain = "broad"
        if toolchain is not None:
            toolchains.add(toolchain)
    return any(
        path_matches_verification_toolchain(path, toolchain)
        for path in paths
        for toolchain in toolchains
    )


def paths_require_verification(paths: set[str] | frozenset[str]) -> bool:
    if not paths:
        return False
    return any(path_has_known_verification_surface(path) for path in paths)


def analyze_verification_command(
    command: str,
    *,
    source: str = "",
    contract_type: str = "",
    trusted: bool = False,
    workspace_root: Path | None = None,
    capture_checker_fingerprints: bool = False,
) -> VerificationCommandAnalysis:
    original = str(command or "").strip()
    normalized = _normalize_command(original)
    if not normalized:
        return _analysis(
            original,
            normalized,
            "",
            "",
            rejection_reason="empty_command",
            capability=VerificationCommandEvidentiaryCapability.NON_ASSERTIVE,
            capability_reason="empty_command",
            source=source,
            contract_type=contract_type,
        )
    if "\n" in original or "\r" in original:
        return _analysis(
            original,
            normalized,
            normalized,
            normalized,
            rejection_reason="disallowed_shell_control_flow",
            capability=VerificationCommandEvidentiaryCapability.UNKNOWN,
            capability_reason="multi_line_shell_expression",
            shell_control_flow="unsafe",
            source=source,
            contract_type=contract_type,
        )

    unwrapped = normalized
    while True:
        wrapped = _unwrap_shell_wrapper_command(unwrapped)
        if not wrapped or wrapped == unwrapped:
            break
        unwrapped = wrapped

    tokens = _shell_tokens(unwrapped)
    if tokens is None:
        return _analysis(
            original,
            normalized,
            unwrapped,
            unwrapped,
            rejection_reason="parse_error",
            capability=VerificationCommandEvidentiaryCapability.UNKNOWN,
            capability_reason="parse_error",
            source=source,
            contract_type=contract_type,
        )
    if not tokens:
        return _analysis(
            original,
            normalized,
            unwrapped,
            unwrapped,
            rejection_reason="empty_command",
            capability=VerificationCommandEvidentiaryCapability.NON_ASSERTIVE,
            capability_reason="empty_command",
            source=source,
            contract_type=contract_type,
        )

    shell_control_flow = "none"
    pipeline_policy = "none"
    cd_target: str | None = None
    primary_tokens = tokens
    if "|" in tokens:
        pipeline_policy = "rejected_without_pipefail"
        return _analysis(
            original,
            normalized,
            unwrapped,
            _join_tokens(tokens),
            rejection_reason="unsafe_pipeline",
            capability=VerificationCommandEvidentiaryCapability.UNKNOWN,
            capability_reason="pipeline_exit_status_can_mask_upstream_failure",
            shell_control_flow="pipeline",
            pipeline_policy=pipeline_policy,
            source=source,
            contract_type=contract_type,
        )
    if len(tokens) >= 4 and tokens[0] == "cd" and tokens[2] == "&&":
        shell_control_flow = "safe_cd_and"
        cd_target = tokens[1]
        cd_rejection = _cd_target_rejection_reason(cd_target, workspace_root=workspace_root)
        primary_tokens = tokens[3:]
        if cd_rejection:
            return _analysis(
                original,
                normalized,
                unwrapped,
                _join_tokens(primary_tokens),
                rejection_reason=cd_rejection,
                capability=VerificationCommandEvidentiaryCapability.UNKNOWN,
                capability_reason=cd_rejection,
                shell_control_flow=shell_control_flow,
                cd_target=cd_target,
                source=source,
                contract_type=contract_type,
            )
        if any(token in _SHELL_CONTROL_TOKENS for token in primary_tokens):
            reason = "unsafe_pipeline" if "|" in primary_tokens else "disallowed_shell_control_flow"
            return _analysis(
                original,
                normalized,
                unwrapped,
                _join_tokens(primary_tokens),
                rejection_reason=reason,
                capability=VerificationCommandEvidentiaryCapability.UNKNOWN,
                capability_reason=reason,
                shell_control_flow="unsafe",
                pipeline_policy=(
                    "rejected_without_pipefail" if reason == "unsafe_pipeline" else "none"
                ),
                cd_target=cd_target,
                source=source,
                contract_type=contract_type,
            )
    elif any(token in _SHELL_CONTROL_TOKENS for token in tokens):
        reason = "unsafe_pipeline" if "|" in tokens else "disallowed_shell_control_flow"
        return _analysis(
            original,
            normalized,
            unwrapped,
            _join_tokens(tokens),
            rejection_reason=reason,
            capability=VerificationCommandEvidentiaryCapability.UNKNOWN,
            capability_reason=reason,
            shell_control_flow="unsafe",
            pipeline_policy=(
                "rejected_without_pipefail" if reason == "unsafe_pipeline" else "none"
            ),
            source=source,
            contract_type=contract_type,
        )

    primary = _join_tokens(primary_tokens) if shell_control_flow == "safe_cd_and" else unwrapped
    parts = _canonical_parts(primary)
    if not parts:
        return _analysis(
            original,
            normalized,
            unwrapped,
            primary,
            rejection_reason="unrecognized_command",
            capability=VerificationCommandEvidentiaryCapability.UNKNOWN,
            capability_reason="unrecognized_command",
            shell_control_flow=shell_control_flow,
            cd_target=cd_target,
            source=source,
            contract_type=contract_type,
        )
    canonical = shlex.join(parts)
    rejection = _non_assertive_rejection_reason(parts)
    if rejection:
        return _analysis(
            original,
            normalized,
            unwrapped,
            primary,
            canonical_command=canonical,
            parts=tuple(parts),
            rejection_reason=rejection,
            capability=VerificationCommandEvidentiaryCapability.NON_ASSERTIVE,
            capability_reason=rejection,
            shell_control_flow=shell_control_flow,
            cd_target=cd_target,
            source=source,
            contract_type=contract_type,
        )

    family = _command_family(parts, trusted=trusted)
    checker_paths = _checker_entrypoint_paths(parts, trusted=trusted)
    fingerprints = (
        tuple(
            _fingerprint_checker_path(path, workspace_root=workspace_root) for path in checker_paths
        )
        if capture_checker_fingerprints
        else tuple()
    )
    if family is not None:
        return _analysis(
            original,
            normalized,
            unwrapped,
            primary,
            canonical_command=canonical,
            parts=tuple(parts),
            family=family,
            capability=VerificationCommandEvidentiaryCapability.ASSERTIVE,
            capability_reason="recognized_assertive_verification_command",
            shell_control_flow=shell_control_flow,
            checker_paths=checker_paths,
            checker_integrity_required=bool(checker_paths),
            checker_fingerprints=fingerprints,
            cd_target=cd_target,
            source=source,
            contract_type=contract_type,
        )

    inconclusive = _unknown_capability_reason(parts)
    return _analysis(
        original,
        normalized,
        unwrapped,
        primary,
        canonical_command=canonical,
        parts=tuple(parts),
        capability=VerificationCommandEvidentiaryCapability.UNKNOWN,
        capability_reason=inconclusive,
        inconclusive_reason=inconclusive,
        shell_control_flow=shell_control_flow,
        checker_paths=checker_paths,
        checker_integrity_required=bool(checker_paths),
        checker_fingerprints=fingerprints,
        cd_target=cd_target,
        source=source,
        contract_type=contract_type,
    )


_BENIGN_NON_EXECUTION_REASONS = frozenset(
    {
        "dotnet_test_zero_tests",
        "go_test_no_test_files",
        "go_test_no_tests_to_run",
        "gradle_test_zero_tests",
        "maven_test_zero_tests",
        "maven_verify_zero_tests",
        "no_applicable_verification",
        "npm_test_zero_tests",
        "pnpm_test_zero_tests",
        "pytest_no_tests_collected",
        "unittest_no_tests_run",
        "verification_nothing_to_do",
        "yarn_test_zero_tests",
    }
)


def is_benign_non_execution_reason(reason: str | None) -> bool:
    return str(reason or "").strip() in _BENIGN_NON_EXECUTION_REASONS


# Actionable guidance appended to a verify_run rejection so the model is told
# how to fix the command instead of only being handed the machine-readable
# reason. verify_run rejects a command because its exit code cannot be trusted
# as proof (a pipe reports the last stage's status, `&&`/`;` hide an earlier
# failure), not because the code under test is wrong -- but a bare
# "verification command is invalid: unsafe_pipeline" reads to the model like a
# test failure and, in the TB 2.1 focus-64 campaign, sent it into git-stash /
# re-explore thrash loops (e.g. fix-code-vulnerability went from a 4-minute
# solve to two 60-minute timeouts). Each message names the concrete fix:
# re-run the single underlying command so its own exit code is the verdict.
_VERIFICATION_REJECTION_GUIDANCE: dict[str, str] = {
    "unsafe_pipeline": (
        "A pipeline reports only the last stage's exit code, so a failing test "
        "piped into `tail`/`head`/`grep` looks like a pass. Pass just the test "
        "command itself (e.g. `pytest -q`, not `pytest 2>&1 | tail`); it is not "
        "a failure of your code."
    ),
    "disallowed_shell_control_flow": (
        "verify_run needs a single command whose own exit code is the result, so "
        "`&&`, `||`, `;`, `&` and multi-line scripts are not allowed. Drop the "
        "chaining and pass just the test command (a leading `cd <dir> &&` is "
        "fine; put any setup in a separate shell_run first, and move a multi-line "
        "script into a file and run that file). This is not a failure of your code."
    ),
    "non_assertive_observation": (
        "This command only observes state (e.g. `ls`, `cat`, `grep`) and cannot "
        "prove the task is done. Pass a command that fails on a wrong result -- a "
        "test runner, or a `python -c '... assert ...'` check."
    ),
    "parse_error": (
        "The command could not be parsed as a single shell command (often "
        "unbalanced quotes). Simplify it to one runnable command, or run it with "
        "shell_run first to confirm it parses."
    ),
}


def verification_rejection_guidance(reason: str | None) -> str:
    """Actionable one-line fix for a verify_run rejection ``reason``, or ``""``.

    Kept beside the rejection reasons it explains so the two never drift. The
    empty string for an unknown reason lets callers append unconditionally.
    """
    return _VERIFICATION_REJECTION_GUIDANCE.get(str(reason or "").strip(), "")


def command_status_from_execution(
    *,
    exit_code: int,
    real_execution: bool | None,
    non_execution_reason: str | None = None,
) -> VerificationCommandStatus:
    if non_execution_reason == "stale_verification":
        return VerificationCommandStatus.STALE
    if is_benign_non_execution_reason(non_execution_reason):
        return VerificationCommandStatus.SKIPPED
    if exit_code != 0:
        return VerificationCommandStatus.FAILED
    if real_execution is True:
        return VerificationCommandStatus.PASSED
    if real_execution is False:
        return VerificationCommandStatus.NOT_EXECUTED
    return VerificationCommandStatus.INCONCLUSIVE


def _analysis(
    original: str,
    normalized: str,
    unwrapped: str,
    primary: str,
    *,
    canonical_command: str | None = None,
    parts: tuple[str, ...] = tuple(),
    family: str | None = None,
    capability: VerificationCommandEvidentiaryCapability,
    capability_reason: str,
    shell_control_flow: str = "none",
    pipeline_policy: str = "none",
    checker_paths: tuple[str, ...] = tuple(),
    checker_integrity_required: bool = False,
    checker_fingerprints: tuple[CheckerEntrypointFingerprint, ...] = tuple(),
    rejection_reason: str = "",
    inconclusive_reason: str = "",
    cd_target: str | None = None,
    source: str = "",
    contract_type: str = "",
) -> VerificationCommandAnalysis:
    return VerificationCommandAnalysis(
        original_command=original,
        normalized_command=normalized,
        unwrapped_command=unwrapped,
        primary_command=primary,
        canonical_command=canonical_command,
        parts=parts,
        command_family=family,
        provenance_source=source,
        trust_source=contract_type,
        evidentiary_capability=capability,
        capability_reason=capability_reason,
        shell_control_flow=shell_control_flow,
        pipeline_policy=pipeline_policy,
        checker_entrypoint_paths=checker_paths,
        checker_integrity_required=checker_integrity_required,
        checker_fingerprints=checker_fingerprints,
        real_execution_required=True,
        rejection_reason=rejection_reason,
        inconclusive_reason=inconclusive_reason,
        cd_target=cd_target,
    )


def _normalize_command(command: str) -> str:
    # The shell parser handles spacing between tokens; spacing inside an
    # operand, quoted expression, or inline program must reach it unchanged.
    return str(command or "").strip()


def _shell_tokens(command: str) -> list[str] | None:
    if is_windows_verifier_command(command):
        return split_windows_verifier_command(command)
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return None


def _join_tokens(tokens: list[str]) -> str:
    return shlex.join(tokens)


def _unwrap_shell_wrapper_command(command: str) -> str | None:
    parts = split_verify_command_parts(command)
    if not parts:
        return None
    head = _command_head(parts[0])
    if head in _SHELL_WRAPPER_HEADS and len(parts) == 3 and parts[1] in {"-c", "-lc"}:
        return _normalize_command(parts[2])
    if head == "fish" and len(parts) == 3 and parts[1] == "-c":
        return _normalize_command(parts[2])
    if head == "cmd" and len(parts) == 3 and parts[1].casefold() == "/c":
        return _normalize_command(parts[2])
    if head in {"powershell", "pwsh"} and len(parts) == 3 and parts[1].casefold() == "-command":
        return _normalize_command(parts[2])
    return None


def _cd_target_rejection_reason(target: str, *, workspace_root: Path | None) -> str:
    if not target or target.startswith("-"):
        return "disallowed_cd_target"
    if workspace_root is None:
        return ""
    if "$" in target or "`" in target:
        return "disallowed_cd_target"
    if target.startswith("/"):
        return ""
    try:
        root = workspace_root.resolve()
        raw = Path(os.path.expanduser(target))
        if raw.is_absolute():
            return ""
        candidate = raw if raw.is_absolute() else root / raw
        candidate.resolve().relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return "disallowed_cd_target"
    return ""


def _canonical_parts(command: str) -> list[str] | None:
    parts = split_verify_command_parts(command)
    if not parts:
        return None
    while True:
        changed = False
        stripped_env = _strip_env_prefix(parts)
        if stripped_env is None:
            return None
        if stripped_env != parts:
            parts = stripped_env
            changed = True
        stripped_runner = strip_verify_runner_prefix(parts)
        if stripped_runner is None:
            return None
        if stripped_runner != parts:
            parts = stripped_runner
            changed = True
        if parts and _command_head(parts[0]) == "command":
            parts = parts[1:]
            changed = True
            if not parts:
                return None
        if not changed:
            break
    parts = normalize_verify_module_invocation(parts)
    return parts or None


def _strip_env_prefix(parts: list[str]) -> list[str] | None:
    out = list(parts)
    if not out:
        return None
    if out[0] == "env":
        out = out[1:]
        if not out or out[0].startswith("-"):
            return None
    while out and _ENV_ASSIGNMENT_RE.match(out[0]):
        out = out[1:]
    return out or None


def _command_head(token: str) -> str:
    head = Path(str(token).replace("\\", "/")).name.casefold()
    if head.endswith((".exe", ".cmd")):
        head = head[:-4]
    return head


def _non_assertive_rejection_reason(parts: list[str]) -> str:
    if _is_vacuous_success_command(parts):
        return "vacuous_verifier"
    if _is_non_assertive_observation_command(parts):
        return "non_assertive_observation"
    if _is_non_assertive_python_snippet(parts):
        return "vacuous_verifier"
    if _is_compile_only_python_command(parts):
        return "non_assertive_observation"
    if _is_non_executing_verification_form(parts):
        return "non_assertive_verification_mode"
    return ""


def _is_vacuous_success_command(parts: list[str]) -> bool:
    if not parts:
        return False
    head = _command_head(parts[0])
    if len(parts) == 1 and (head in _VACUOUS_SUCCESS_HEADS or parts[0].strip() == ":"):
        return True
    if len(parts) == 2 and head in {"exit", "return"} and parts[1] == "0":
        return True
    return _is_non_assertive_python_snippet(parts)


def _is_non_assertive_observation_command(parts: list[str]) -> bool:
    if not parts:
        return False
    head = _command_head(parts[0])
    if head in {"grep", "curl"}:
        return False
    return head in _OBSERVATION_HEADS


def _is_python_interpreter_snippet(parts: list[str]) -> bool:
    return len(parts) >= 3 and _command_head(parts[0]) in _PYTHON_EXECUTABLES and parts[1] == "-c"


def _is_non_assertive_python_snippet(parts: list[str]) -> bool:
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
    return compact.startswith("importsys;") and compact.endswith("sys.exit(0)")


def _is_compile_only_python_command(parts: list[str]) -> bool:
    return (
        len(parts) >= 3
        and _command_head(parts[0]) in _PYTHON_EXECUTABLES
        and parts[1] == "-m"
        and parts[2] in {"compileall", "py_compile"}
    )


def _is_non_executing_verification_form(parts: list[str]) -> bool:
    lowered = [part.casefold() for part in parts]
    if not lowered:
        return True
    if any(part in _META_OPTIONS for part in lowered):
        return True
    if lowered[0] in {"pytest", "py.test"}:
        return any(
            part in _PYTEST_NON_VERIFICATION_OPTIONS | _PYTEST_NON_EXECUTING_OPTIONS
            for part in lowered[1:]
        )
    if lowered[:2] == ["go", "test"]:
        if any(
            part in _GO_TEST_NON_EXECUTING_OPTIONS or part.startswith("-list=")
            for part in lowered[2:]
        ):
            return True
        for index, part in enumerate(lowered[2:], start=2):
            if part == "-run" and index + 1 < len(lowered) and lowered[index + 1] == "^$":
                return True
            if part == "-run=^$":
                return True
    if lowered[:2] == ["cargo", "test"]:
        return any(part in _CARGO_TEST_NON_EXECUTING_OPTIONS for part in lowered[2:])
    if lowered[:2] == ["ruff", "check"]:
        return any(part in _RUFF_CHECK_NON_EXECUTING_OPTIONS for part in lowered[2:])
    if lowered[0] == "mypy":
        return any(part in _MYPY_NON_EXECUTING_OPTIONS for part in lowered[1:])
    return False


def _command_family(parts: list[str], *, trusted: bool) -> str | None:
    if not parts:
        return None
    head = _command_head(parts[0])
    tail = [part.casefold() for part in parts[1:]]
    if head in {"pytest", "py.test"}:
        return "pytest"
    if head == "unittest":
        return "unittest"
    if head == "mypy":
        return "mypy"
    if head == "go" and tail and tail[0] == "test":
        return "go:test"
    if head == "node" and tail and tail[0] == "--test":
        return "node:test"
    if head == "cargo" and tail and tail[0] in {"test", "check"}:
        return f"cargo:{tail[0]}"
    if head in {"mvn", "mvnw", "./mvnw"} and tail and tail[0] in {"test", "verify"}:
        return f"maven:{tail[0]}"
    if head in {"gradle", "gradlew", "./gradlew"} and tail and tail[0] == "test":
        return "gradle:test"
    if head == "dotnet" and tail and tail[0] == "test":
        return "dotnet:test"
    if head == "mix" and tail and tail[0] == "test":
        return "mix:test"
    if head in {"npm", "pnpm", "yarn"}:
        script = _node_assertive_script(parts)
        if script:
            return f"{head}:{script}"
    if head in {"make", "just"} and tail and tail[0] in {"test", "check", "verify"}:
        return f"{head}:{tail[0]}"
    if head == "ruff" and tail and tail[0] == "check":
        return "ruff:check"
    if head in {"test", "["} and len(tail) >= 2:
        return "assertion:test"
    if head in {"cmp", "diff"}:
        operands = [part for part in parts[1:] if part != "--" and not part.startswith("-")]
        if len(operands) >= 2:
            return f"assertion:{head}"
    if head == "grep":
        has_quiet = any(part in {"-q", "--quiet"} for part in tail)
        operands = [part for part in parts[1:] if part != "--" and not part.startswith("-")]
        if has_quiet and len(operands) >= 2:
            return "assertion:grep"
    if head == "curl" and _curl_has_fail_semantics(parts[1:]):
        return "assertion:http_readiness"
    if _is_assertive_python_snippet(parts):
        return "assertion:python_snippet"
    if _repo_local_validation_executable(parts):
        return "assertion:repo_local_executable"
    if _interpreter_validation_script(parts):
        return "assertion:repo_local_script"
    if trusted and _checker_entrypoint_paths(parts, trusted=trusted):
        return "assertion:custom_checker"
    return None


def _curl_has_fail_semantics(args: list[str]) -> bool:
    for arg in args:
        if arg == "--fail" or arg == "--fail-with-body":
            return True
        if arg.startswith("--fail-"):
            return True
        if arg.startswith("-") and not arg.startswith("--") and "f" in arg[1:]:
            return True
    return False


def _node_assertive_script(parts: list[str]) -> str | None:
    if not parts:
        return None
    head = _command_head(parts[0])
    idx = 1
    while idx < len(parts):
        token = parts[idx].casefold()
        if token in {"--prefix", "--dir", "--cwd", "-c"} and idx + 1 < len(parts):
            idx += 2
            continue
        if (
            token.startswith("--prefix=")
            or token.startswith("--dir=")
            or token.startswith("--cwd=")
        ):
            idx += 1
            continue
        break
    if idx >= len(parts):
        return None
    command = parts[idx].casefold()
    if command == "run" and idx + 1 < len(parts):
        script = parts[idx + 1].casefold()
    elif head == "yarn" and command not in {"run", "exec"}:
        script = command
    else:
        script = command
    if script.startswith("test:"):
        return "test"
    return script if script in _NODE_ASSERTIVE_SCRIPTS else None


def _unknown_capability_reason(parts: list[str]) -> str:
    if parts and _command_head(parts[0]) == "curl":
        return "http_probe_requires_curl_fail"
    return "unknown_verification_capability"


def _is_assertive_python_snippet(parts: list[str]) -> bool:
    if not _is_python_interpreter_snippet(parts):
        return False
    snippet = " ".join(parts[2:]).strip()
    compact = snippet.replace(" ", "")
    return "assert " in snippet or ";assert" in compact or compact.startswith("assert")


def _repo_local_validation_executable(parts: list[str]) -> bool:
    if not parts:
        return False
    executable = PurePosixPath(parts[0].replace("\\", "/"))
    if "/" not in parts[0] and not parts[0].startswith("."):
        return False
    markers = {executable.stem.casefold(), *(part.casefold() for part in executable.parts[:-1])}
    return any(marker in value for value in markers for marker in _VALIDATION_COMMAND_PATH_MARKERS)


def _interpreter_validation_script(parts: list[str]) -> bool:
    path = _interpreter_script_path(parts)
    if not path:
        return False
    script = PurePosixPath(path.replace("\\", "/"))
    markers = {script.stem.casefold(), *(part.casefold() for part in script.parts[:-1])}
    return any(marker in value for value in markers for marker in _VALIDATION_COMMAND_PATH_MARKERS)


def _checker_entrypoint_paths(parts: list[str], *, trusted: bool) -> tuple[str, ...]:
    if not parts:
        return tuple()
    script = _interpreter_script_path(parts)
    if script:
        return (script,)
    head = parts[0]
    if _is_repo_local_path(head) and ("/" in head.replace("\\", "/") or head.startswith(".")):
        return (head,)
    return tuple()


def _interpreter_script_path(parts: list[str]) -> str | None:
    if not parts:
        return None
    head = _command_head(parts[0])
    if head not in {"python", "python3", "py", "node", "ruby", "rscript", "r", "bash", "sh", "zsh"}:
        return None
    tail = parts[1:]
    if not tail or tail[0].casefold() in _META_OPTIONS:
        return None
    if _interpreter_uses_inline_code(head, tail):
        return None
    if head in _PYTHON_EXECUTABLES and tail[0] == "-m":
        return None
    for item in tail:
        if item == "--" or item.startswith("-"):
            continue
        if _is_repo_local_path(item):
            return item
    return None


def _interpreter_uses_inline_code(head: str, tail: list[str]) -> bool:
    inline_options = _INTERPRETER_INLINE_CODE_OPTIONS.get(head, set())
    options_with_values = _INTERPRETER_OPTIONS_WITH_SEPARATE_VALUES.get(head, set())
    index = 0
    while index < len(tail):
        token = tail[index]
        if token == "--" or token == "-" or not token.startswith("-"):
            return False
        if token in inline_options:
            return True
        if head in _SHELL_WRAPPER_HEADS and _shell_short_options_use_inline_code(token):
            return True
        if head == "ruby" and _ruby_short_options_use_inline_code(token):
            return True
        if any(
            token.startswith(f"{option}=")
            or (option.startswith("-") and not option.startswith("--") and token.startswith(option))
            for option in inline_options
        ):
            return True
        if token in options_with_values:
            index += 2
            continue
        index += 1
    return False


def _command_uses_opaque_inline_code(command: str, *, depth: int = 0) -> bool:
    if depth > 4:
        return False
    parts = split_verify_command_parts(command) or []
    for index, token in enumerate(parts):
        if _ENV_ASSIGNMENT_RE.fullmatch(token):
            continue
        head = _command_head(token)
        if VERIFY_PYTHON_LAUNCHER_RE.fullmatch(head):
            head = "python"
        if head not in _INTERPRETER_INLINE_CODE_OPTIONS:
            continue
        tail = parts[index + 1 :]
        if not _interpreter_uses_inline_code(head, tail):
            return False
        if head not in _SHELL_WRAPPER_HEADS:
            return True
        for option_index, option in enumerate(tail):
            if option == "-c" or _shell_short_options_use_inline_code(option):
                source = tail[option_index + 1] if option_index + 1 < len(tail) else ""
                return _command_uses_opaque_inline_code(source, depth=depth + 1)
        return False
    return False


def _shell_short_options_use_inline_code(token: str) -> bool:
    if not token.startswith("-") or token.startswith("--"):
        return False
    cluster = token[1:]
    return (
        "c" in cluster
        and bool(cluster)
        and all(option in _SHELL_CLUSTERABLE_SHORT_OPTIONS for option in cluster)
    )


def _ruby_short_options_use_inline_code(token: str) -> bool:
    if not token.startswith("-") or token.startswith("--"):
        return False
    for option in token[1:]:
        if option == "e":
            return True
        if option in {"0", "C", "E", "F", "I", "K", "T", "W", "r"}:
            return False
    return False


def _is_repo_local_path(path: str) -> bool:
    if not path or path.startswith("-"):
        return False
    pure = PurePosixPath(path.replace("\\", "/"))
    return not pure.is_absolute() and ".." not in pure.parts


def _fingerprint_checker_path(
    display_path: str,
    *,
    workspace_root: Path | None,
    max_bytes: int = 2_000_000,
) -> CheckerEntrypointFingerprint:
    base = workspace_root.resolve() if workspace_root is not None else Path.cwd().resolve()
    raw = Path(display_path)
    candidate = raw if raw.is_absolute() else base / raw
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return CheckerEntrypointFingerprint(
            display_path=display_path,
            resolved_path=os.fspath(candidate),
            is_regular_file=False,
            size=None,
            sha256=None,
        )
    try:
        stat = resolved.stat()
    except OSError:
        return CheckerEntrypointFingerprint(
            display_path=display_path,
            resolved_path=os.fspath(resolved),
            is_regular_file=False,
            size=None,
            sha256=None,
        )
    if not resolved.is_file() or stat.st_size > max_bytes:
        return CheckerEntrypointFingerprint(
            display_path=display_path,
            resolved_path=os.fspath(resolved),
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
        resolved_path=os.fspath(resolved),
        is_regular_file=True,
        size=int(stat.st_size),
        sha256=digest.hexdigest(),
    )
