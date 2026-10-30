from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from ..config import (
    normalize_verify_module_invocation,
    split_verify_command_parts,
    strip_verify_runner_prefix,
)
from ..verification_command_analysis import (
    VerificationCommandEvidentiaryCapability,
    analyze_verification_command,
)
from ..windows_verification_command import is_windows_verifier_command
from .prompt_context import _normalized_verify_commands


def _normalize_shell_command_for_match(raw: str) -> str:
    # This value participates in authority comparisons. Case and whitespace
    # inside quotes can select different files, expressions or environments.
    # Broader runner classification belongs to analyze_verification_command.
    return str(raw or "").strip()


_VERIFICATION_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


_DISALLOWED_VERIFICATION_SHELL_TOKENS = {"||", "&&", ";", "|", "&"}


_NON_VERIFICATION_META_OPTIONS = {"--help", "-h", "--version"}


_PYTEST_NON_VERIFICATION_OPTIONS = {"--fixtures", "--markers", "--collect-only", "--co"}


_PYTEST_NON_EXECUTING_OPTIONS = {"--setup-plan"}


_CARGO_TEST_NON_EXECUTING_OPTIONS = {"--no-run", "--list"}


_GO_TEST_NON_EXECUTING_OPTIONS = {"-c", "-list"}


_RUFF_CHECK_NON_EXECUTING_OPTIONS = {"--fix", "--fix-only"}


_MYPY_NON_EXECUTING_OPTIONS = {"--install-types"}


_PYTEST_REPORTER_OPTIONS = {"-q", "--quiet", "--verbose"}


_PYTEST_NO_ARGUMENT_OPTIONS = {
    "-x",
    "--exitfirst",
    "--disable-warnings",
    "--disable-pytest-warnings",
    "--strict-config",
    "--strict-markers",
}


@dataclass(frozen=True)
class VerificationCommandShape:
    family: str
    args: tuple[str, ...]
    options: tuple[str, ...]
    positionals: tuple[str, ...]


def _command_options_include(options: tuple[str, ...], flag: str) -> bool:
    return any(option == flag or option.startswith(f"{flag}=") for option in options)


def _verification_shape_is_real_execution_mode(shape: VerificationCommandShape) -> bool:
    if shape.family == "pytest":
        return not any(
            _command_options_include(shape.options, flag) for flag in _PYTEST_NON_EXECUTING_OPTIONS
        )
    if shape.family == "cargo:test":
        return not any(
            _command_options_include(shape.options, flag)
            for flag in _CARGO_TEST_NON_EXECUTING_OPTIONS
        )
    if shape.family == "go:test":
        if any(
            _command_options_include(shape.options, flag) for flag in _GO_TEST_NON_EXECUTING_OPTIONS
        ):
            return False
        for idx, arg in enumerate(shape.args):
            if arg == "-run":
                if idx + 1 >= len(shape.args):
                    return False
                if shape.args[idx + 1] == "^$":
                    return False
            elif arg.startswith("-run=") and arg.partition("=")[2] == "^$":
                return False
        return True
    if shape.family == "ruff:check":
        return not any(
            _command_options_include(shape.options, flag)
            for flag in _RUFF_CHECK_NON_EXECUTING_OPTIONS
        )
    if shape.family == "mypy":
        return not any(
            _command_options_include(shape.options, flag) for flag in _MYPY_NON_EXECUTING_OPTIONS
        )
    return True


def _looks_like_env_assignment_token(part: str) -> bool:
    return bool(_VERIFICATION_ENV_ASSIGNMENT_RE.match(part))


def _strip_verification_env_prefix(parts: list[str]) -> list[str] | None:
    if not parts:
        return None
    out = list(parts)
    if out[0] == "env":
        out = out[1:]
        if not out or out[0].startswith("-"):
            return None
    while out and _looks_like_env_assignment_token(out[0]):
        out = out[1:]
    return out or None


def _strip_verification_runner_prefix(parts: list[str]) -> list[str] | None:
    return strip_verify_runner_prefix(parts)


def _normalize_and_unwrap_verification_command(raw: str) -> str | None:
    normalized = _normalize_shell_command_for_match(raw)
    if not normalized:
        return None

    while True:
        wrapped = _unwrap_shell_wrapper_command(normalized)
        if not wrapped or wrapped == normalized:
            break
        normalized = wrapped
    return normalized


def _has_disallowed_shell_control_flow(raw: str) -> bool:
    analysis = analyze_verification_command(raw, trusted=True)
    return analysis.shell_control_flow in {"unsafe", "pipeline"} or analysis.rejection_reason in {
        "disallowed_shell_control_flow",
        "unsafe_pipeline",
    }


def _top_level_shell_chain_segments(raw: str) -> tuple[list[str], tuple[str, ...]] | None:
    command = str(raw or "")
    segments: list[str] = []
    separators: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote is not None:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if command.startswith("&&", index) or command.startswith("||", index):
            segments.append(command[start:index].strip())
            separators.append(command[index : index + 2])
            index += 2
            start = index
            continue
        if char in {";", "|", "&"}:
            segments.append(command[start:index].strip())
            separators.append(char)
            index += 1
            start = index
            continue
        index += 1
    if quote is not None:
        return None
    if not separators:
        return None
    segments.append(command[start:].strip())
    return segments, tuple(separators)


def _expand_simple_verify_command_chain(
    raw: str,
    *,
    workspace_root: Path | None = None,
) -> list[str]:
    split = _top_level_shell_chain_segments(raw)
    if split is None:
        return [raw]
    segments, separators = split
    if any(separator not in {"&&", ";"} for separator in separators):
        return [raw]
    if any(not segment for segment in segments):
        return [raw]

    expanded: list[str] = []
    for segment in segments:
        analysis = analyze_verification_command(segment, workspace_root=workspace_root)
        if (
            analysis.rejection_reason
            or analysis.shell_control_flow != "none"
            or analysis.evidentiary_capability != VerificationCommandEvidentiaryCapability.ASSERTIVE
        ):
            return [raw]
        expanded.append(segment)
    return expanded


def _canonicalize_verification_command_for_match(raw: str) -> str | None:
    """Legacy capability projection; never use it as a contract coverage key."""

    analysis = analyze_verification_command(raw, trusted=True)
    if analysis.rejection_reason:
        return None
    return analysis.canonical_command


def _split_verification_shape_args(args: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    options: list[str] = []
    positionals: list[str] = []
    for arg in args:
        if arg == "--":
            continue
        if arg.startswith("-") and arg != "-":
            options.append(arg)
            continue
        positionals.append(arg)
    return tuple(options), tuple(positionals)


def _parse_verification_command_shape(raw: str) -> VerificationCommandShape | None:
    canonical = _canonicalize_verification_command_for_match(raw)
    if not canonical:
        return None
    try:
        parts = shlex.split(canonical, posix=True)
    except ValueError:
        return None
    if not parts:
        return None

    head = parts[0]
    tail = parts[1:]
    family: str
    args: list[str]

    if head in {"pytest", "py.test"}:
        family = "pytest"
        args = tail
    elif head == "unittest":
        family = "unittest"
        args = tail
    elif head == "mypy":
        family = "mypy"
        args = tail
    elif head == "go" and tail and tail[0] == "test":
        family = "go:test"
        args = tail[1:]
    elif head == "cargo" and tail and tail[0] in {"test", "check"}:
        family = f"cargo:{tail[0]}"
        args = tail[1:]
    elif head in {"npm", "pnpm", "yarn"} and tail and tail[0] == "test":
        family = f"{head}:test"
        args = tail[1:]
    elif head in {"make", "just"} and tail and tail[0] in {"test", "check", "verify"}:
        family = f"{head}:{tail[0]}"
        args = tail[1:]
    elif head == "ruff" and tail and tail[0] == "check":
        family = "ruff:check"
        args = tail[1:]
    else:
        return None

    options, positionals = _split_verification_shape_args(args)
    if _NON_VERIFICATION_META_OPTIONS & set(options):
        return None
    if family == "pytest" and _PYTEST_NON_VERIFICATION_OPTIONS & set(options):
        return None
    shape = VerificationCommandShape(
        family=family,
        args=tuple(args),
        options=options,
        positionals=positionals,
    )
    if not _verification_shape_is_real_execution_mode(shape):
        return None
    return shape


def _verification_command_shapes_match(
    *,
    observed: VerificationCommandShape,
    expected: VerificationCommandShape,
) -> bool:
    if observed.family != expected.family:
        return False
    if observed.family == "pytest":
        return _pytest_coverage_args(observed.args) == _pytest_coverage_args(expected.args)
    return observed.args == expected.args


def _pytest_option_is_reporter_variant(option: str) -> bool:
    return option in _PYTEST_REPORTER_OPTIONS or bool(re.fullmatch(r"-(?:v+|q+)", option))


def _pytest_coverage_args(args: tuple[str, ...]) -> tuple[str, ...]:
    """Ignore only reporting flags, never an option's value or a target.

    Known switches consume no value; selectors consume their exact value.
    Unknown/plugin options may consume the following token. If that token
    is itself option-shaped, keep the remainder unchanged: guessing its
    arity could erase a selection expression later in the argument list.
    """

    kept: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            kept.extend(args[index:])
            break
        if _pytest_option_is_reporter_variant(token):
            index += 1
            continue
        kept.append(token)
        if (
            token.startswith("-")
            and token not in _PYTEST_NO_ARGUMENT_OPTIONS
            and "=" not in token
            and index + 1 < len(args)
        ):
            if args[index + 1].startswith("-") and token not in {"-k", "-m"}:
                kept.extend(args[index + 1 :])
                break
            index += 1
            kept.append(args[index])
        index += 1
    return tuple(kept)


@dataclass(frozen=True)
class _VerificationCoverageKey:
    context: tuple[tuple[str, ...], ...]
    argv: tuple[str, ...]


def _source_command_words(command: str) -> list[str] | None:
    """Keep each word's source spelling for shell assignment comparisons."""

    words: list[str] = []
    start: int | None = None
    quote: str | None = None
    escaped = False
    for index, char in enumerate(command):
        if start is None:
            if char.isspace():
                continue
            start = index
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote is not None:
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char.isspace():
            words.append(command[start:index])
            start = None
    if quote is not None or escaped:
        return None
    if start is not None:
        words.append(command[start:])
    return words


def _verification_coverage_key(raw: str) -> _VerificationCoverageKey | None:
    """A conservative source-derived key for satisfying a required command.

    The capability analyzer intentionally removes environment/runner/cwd
    prefixes to identify what can execute checks. Its canonical command is
    therefore unsuitable for coverage. This key retains those prefixes and
    all case-sensitive argument values. Shell expansion syntax stays on the
    exact-source path because dequoting it can change its meaning.
    """

    command = _normalize_shell_command_for_match(raw)
    if not command or any(char in command for char in "\n\r$`%*?[]{}~#<>"):
        return None
    analysis = analyze_verification_command(command, trusted=True)
    if analysis.rejection_reason or analysis.command_family is None:
        return None
    if is_windows_verifier_command(command):
        # A literal PowerShell call operator and quote style do not change the
        # executable or arguments. Preserve the full path and every argument:
        # another interpreter location or test selection is different evidence.
        parts = split_verify_command_parts(command)
        return _VerificationCoverageKey((("windows_executable",),), tuple(parts)) if parts else None
    chain = _top_level_shell_chain_segments(command)
    if chain is not None:
        segments, separators = chain
        if separators != ("&&",) or len(segments) != 2:
            return None
        prefix = split_verify_command_parts(segments[0])
        if not prefix or len(prefix) != 2 or prefix[0] != "cd":
            return None
        nested = _verification_coverage_key(segments[1])
        if nested is None:
            return None
        return _VerificationCoverageKey((tuple(prefix), *nested.context), nested.argv)

    parts = split_verify_command_parts(command)
    source_words = _source_command_words(command)
    if not parts or source_words is None or len(parts) != len(source_words):
        return None
    context: list[tuple[str, ...]] = []
    while parts:
        # Keep explicit environment assignments and runner selection, including
        # their order. Even two invocations of the same test family may run
        # different checks under these settings.
        if parts[0] == "env":
            context.append(("env",))
            parts = parts[1:]
            source_words = source_words[1:]
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", parts[0]):
            # Quoting/escaping the assignment name can turn it into a command
            # argument. Preserve source syntax, not just its dequoted value.
            context.append(("assignment", source_words[0]))
            parts = parts[1:]
            source_words = source_words[1:]
            continue
        runner_stripped = _strip_verification_runner_prefix(parts)
        if runner_stripped is None:
            return None
        if runner_stripped != parts:
            prefix_len = len(parts) - len(runner_stripped)
            context.append(tuple(parts[:prefix_len]))
            parts = runner_stripped
            source_words = source_words[prefix_len:]
            continue
        if parts[0] == "command":
            context.append(("command",))
            parts = parts[1:]
            source_words = source_words[1:]
            continue
        break
    if not parts:
        return None
    wrapped = _unwrap_shell_wrapper_command(shlex.join(parts))
    if wrapped is not None:
        nested = _verification_coverage_key(wrapped)
        if nested is None:
            return None
        return _VerificationCoverageKey((*context, tuple(parts[:2]), *nested.context), nested.argv)
    normalized_parts = normalize_verify_module_invocation(parts)
    if normalized_parts != parts:
        # Bare standard launchers are the supported CLI/module aliases.
        # Explicit interpreter paths keep their environment identity.
        if parts[0] not in {"python", "python3", "py"}:
            context.append(("python_launcher", parts[0]))
        # Module names are case-sensitive even though the capability analyzer
        # recognizes their family case-insensitively.
        if parts[2] != normalized_parts[0]:
            return None
        parts = normalized_parts
    if parts[0] == "py.test":
        parts[0] = "pytest"
    argv = tuple(parts)
    if parts[0] == "pytest":
        argv = ("pytest", *_pytest_coverage_args(tuple(parts[1:])))
    return _VerificationCoverageKey(tuple(context), argv)


def _effective_verification_command_matches(
    *,
    normalized_cmd: str,
    known_verification_commands: list[str],
) -> bool:
    return bool(
        _matching_effective_verification_commands(
            observed_command=normalized_cmd,
            effective_verification_commands=known_verification_commands,
        )
    )


def _verify_run_commands_match_effective_contract(
    *,
    requested_commands: list[str],
    effective_verification_commands: list[str],
) -> list[str]:
    known = _normalized_verify_commands(effective_verification_commands)
    if not known:
        return []
    incompatible: list[str] = []
    for command in requested_commands:
        normalized_command = _normalize_shell_command_for_match(command)
        if any(
            normalized_command == _normalize_shell_command_for_match(configured)
            for configured in known
        ):
            continue
        if not _effective_verification_command_matches(
            normalized_cmd=normalized_command,
            known_verification_commands=known,
        ):
            incompatible.append(command)
    return incompatible


def _matching_effective_verification_commands(
    *,
    observed_command: str,
    effective_verification_commands: list[str] | None,
) -> set[str]:
    known = _normalized_verify_commands(effective_verification_commands or [])
    if not known:
        return set()
    normalized_observed = _normalize_shell_command_for_match(observed_command)
    exact_matches = {
        configured
        for configured in known
        if normalized_observed == _normalize_shell_command_for_match(configured)
    }
    if exact_matches:
        return exact_matches
    observed = _verification_coverage_key(normalized_observed)
    if observed is None:
        return set()
    matches: set[str] = set()
    for configured in known:
        expected = _verification_coverage_key(configured)
        if expected is None:
            continue
        if observed == expected:
            matches.add(configured)
    return matches


def _unwrap_shell_wrapper_command(normalized_cmd: str) -> str | None:
    if not normalized_cmd:
        return None
    try:
        parts = shlex.split(normalized_cmd, posix=True)
    except ValueError:
        return None
    if not parts:
        return None

    head = parts[0]
    if head in {"bash", "sh", "zsh"}:
        if len(parts) == 3 and parts[1] == "-lc":
            return _normalize_shell_command_for_match(parts[2])
        return None
    if head == "fish":
        if len(parts) == 3 and parts[1] == "-c":
            return _normalize_shell_command_for_match(parts[2])
        return None
    if head == "cmd":
        if len(parts) == 3 and parts[1] == "/c":
            return _normalize_shell_command_for_match(parts[2])
        return None
    if head in {"powershell", "pwsh"}:
        if len(parts) == 3 and parts[1] == "-command":
            return _normalize_shell_command_for_match(parts[2])
        return None
    return None


def _looks_like_verification_entrypoint(parts: list[str]) -> bool:
    if not parts:
        return False
    head = Path(parts[0]).name
    head_lower = head.lower()
    if head_lower in {
        "pytest",
        "py.test",
        "tox",
        "nox",
        "vitest",
        "jest",
        "mypy",
        "flake8",
        "pylint",
        "ruff",
        "mix",
        "sbt",
        "swift",
        "composer",
    }:
        return True
    if len(parts) >= 2 and head_lower in {
        "make",
        "just",
        "npm",
        "pnpm",
        "yarn",
        "cargo",
        "go",
        "mvn",
        "mvnw",
        "gradle",
        "gradlew",
        "dotnet",
    }:
        return parts[1] in {"test", "check", "verify"}
    if len(parts) >= 2 and head_lower in {"python", "python3"} and parts[1] == "-m":
        return len(parts) >= 3 and parts[2] in {"pytest", "unittest"}
    if len(parts) >= 2 and head_lower == "ruby":
        return any(part.endswith("_test.rb") for part in parts[1:])
    if len(parts) >= 2 and head_lower in {"sh", "bash"}:
        return any("test" in Path(part).name for part in parts[1:])
    return False


def _marker_fallback_is_verification_attempt(normalized_cmd: str) -> bool:
    analysis = analyze_verification_command(normalized_cmd, trusted=True)
    if analysis.rejection_reason:
        return False
    if analysis.command_family is not None:
        return True
    canonical = analysis.canonical_command
    if not canonical:
        return False
    try:
        parts = shlex.split(canonical, posix=True)
    except ValueError:
        return False
    return _looks_like_verification_entrypoint(parts)


def _shell_command_is_verification_attempt(
    cmd: str,
    *,
    known_verification_commands: list[str] | None,
) -> bool:
    from .verification_evidence import classify_verification_evidence

    evidence = classify_verification_evidence(
        cmd,
        known_verification_commands=known_verification_commands,
        exit_code=0,
        real_execution=True,
    )
    return bool(evidence.allowed_to_satisfy_contract)
