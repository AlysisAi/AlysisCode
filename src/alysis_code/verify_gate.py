from __future__ import annotations

import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

from .branding import env_get
from .config import (
    AppConfig,
    ConfigError,
    is_generic_configured_verify_preset,
    is_generic_verify_command_fallback,
    normalize_verify_command_list,
    normalize_verify_module_invocation,
    split_verify_command_parts,
    strip_verify_runner_prefix,
)
from .failure_category import (
    FailureCategory,
    failure_category_value,
    is_infra_unavailable_error,
)
from .file_classification import SOURCE_EXTENSIONS_BY_LANGUAGE
from .process_reaping import CommandCancelledError, ProcessGroupRegistry, runner_takes_cancel_check
from .repo_scan import RepoScanResult, detect_fallback_test_commands, scan_workspace
from .sandbox_runner import (
    HostShellRunner,
    build_shell_runner_from_settings,
    execution_scope_fingerprint,
    with_closed_stdin,
)
from .sandbox_settings import resolve_shell_sandbox_settings
from .verification_command_analysis import (
    VerificationCommandEvidentiaryCapability,
    VerificationCommandStatus,
    analyze_verification_command,
    command_status_from_execution,
    path_has_known_verification_surface,
    verification_commands_apply_to_paths,
)
from .verification_contract import (
    VerificationCommandExecutionMode,
    VerificationCommandRequirement,
    VerificationCommandSpec,
    VerificationCommandValidationStatus,
    build_verification_command_specs,
    command_specs_payload,
    rejection_reason_is_unclassifiable,
)
from .verification_failure_summary import summarize_verification_failure
from .windows_verification_command import (
    is_windows_verifier_command,
    quote_windows_verifier_parts,
)
from .workspace_context import WorkspaceContext, WorkspaceContextError, resolve_workspace_context

VERIFY_MODES = {"off", "warn", "strict"}
VERIFY_SANDBOX_MODES = {"off", "warn", "strict"}
VERIFY_OUTPUT_PREVIEW_CHARS = 400
VERIFICATION_FAILURE_SNIPPET_MAX_CHARS = 240
_NODE_JS_EXTENSIONS = set(SOURCE_EXTENSIONS_BY_LANGUAGE["node"])
_NODE_BOOTSTRAP_FILENAMES = {
    "package-lock.json",
    "package.json",
    "pnpm-lock.yaml",
    "tsconfig.json",
    "yarn.lock",
}
_NODE_PACKAGE_HINTS = {"bun", "node", "npm", "pnpm", "yarn"}
_DOC_ONLY_DIR_NAMES = {
    "doc",
    "docs",
    "documentation",
    "manual",
    "manuals",
}
_DOC_ONLY_EXTENSIONS = {".adoc", ".md", ".mdx", ".rst", ".txt"}
_DOC_ONLY_FILENAMES = {
    "authors",
    "changelog",
    "code_of_conduct",
    "contributing",
    "license",
    "notice",
    "readme",
    "security",
}
_DOC_ONLY_FILENAME_PREFIXES = {
    "authors",
    "changelog",
    "code_of_conduct",
    "contributing",
    "license",
    "notice",
    "readme",
    "security",
}
_PYTHON_VERIFY_FILENAMES = {
    "pyproject.toml",
    "pytest.ini",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
    "tox.ini",
}
_CI_ONLY_DIR_NAMES = {
    ".buildkite",
    ".circleci",
    ".github",
    ".gitlab",
    ".woodpecker",
}
_CI_ONLY_FILENAMES = {
    ".gitlab-ci.yml",
    ".travis.yml",
    "buildkite.yaml",
    "buildkite.yml",
    "drone.yaml",
    "drone.yml",
}
_CI_ONLY_WORKFLOW_FILENAMES = {".yaml", ".yml"}
_TERRAFORM_EXTENSIONS = {".tf", ".tfvars"}
_TERRAFORM_FILENAMES = {"terraform.lock.hcl"}
_COMPOSE_FILENAMES = {
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
}
_NODE_TEST_PATH_RE = re.compile(r"(^|/)(?:test|tests)/.*\.(?:test|spec)\.(?:[cm]?[jt]sx?|[cm]?ts)$")
_NODE_TEST_SUFFIX_RE = re.compile(r"\.(?:test|spec)\.(?:[cm]?[jt]sx?|[cm]?ts)$")
_NODE_TEST_TEXT_POSITIVE_RE = re.compile(
    r"\b(?:use|verify(?:\s+(?:with|using))?)\s+node\s+--test\b"
)
_NODE_TEST_TEXT_NEGATIVE_RE = re.compile(
    r"\b(?:do\s+not|don't|never|avoid)\s+(?:use\s+)?node\s+--test\b"
)
_BACKTICK_COMMAND_RE = re.compile(r"`([^`]+)`")
_PYTEST_TEXT_COMMAND_RE = re.compile(
    r"\b((?:(?:python|python3|py)\s+-m\s+pytest)|pytest|py\.test)"
    r"(?:\s+(?:-[A-Za-z0-9][A-Za-z0-9_./:=/-]*|[A-Za-z0-9_/-]+/[A-Za-z0-9_./-]*|[A-Za-z0-9_/-]+\.[A-Za-z0-9][A-Za-z0-9_./-]*))*",
    re.IGNORECASE,
)
_VERIFY_PYTEST_ENTRYPOINTS = {"pytest", "py.test"}
_VERIFY_SHELL_CONTROL_FLOW_TOKENS = {"||", "&&", ";", "|", "&"}
_VERIFY_EXECUTION_LAYER_ERROR_MARKERS = (
    "command not found",
    "not recognized as an internal or external command",
    "permission denied",
    "no such file or directory",
    "cannot execute",
    "exec format error",
    "operation not permitted",
)
_GO_TEST_NON_EXECUTION_MARKERS = {
    "[no tests to run]": "go_test_no_tests_to_run",
    "[no test files]": "go_test_no_test_files",
}
_PYTEST_NO_TESTS_RE = re.compile(
    r"\b(?:collected\s+0\s+items|no\s+tests\s+ran|no\s+tests\s+collected)\b",
    re.IGNORECASE,
)
_UNITTEST_NO_TESTS_RE = re.compile(r"\bRan\s+0\s+tests\b", re.IGNORECASE)
_JUNIT_ZERO_TESTS_RE = re.compile(r"\bTests\s+run:\s*0\b", re.IGNORECASE)
_NODE_ZERO_TESTS_RE = re.compile(r"^\s*[#ℹ]\s+tests\s+0\s*$", re.IGNORECASE | re.MULTILINE)
_NOTHING_TO_DO_RE = re.compile(
    r"\b(?:nothing to be done|nothing to do|no work to do)\b",
    re.IGNORECASE,
)
_VERIFICATION_FAILURE_PRIORITY_MARKERS = (
    "ImportError",
    "ModuleNotFoundError",
    "NameError",
    "AttributeError",
    "SyntaxError",
    "TypeError",
    "ValueError",
    "AssertionError",
)
_GO_TEST_OK_LINE_RE = re.compile(r"^ok\s+\S+\s+\S+")
_GO_TEST_NO_TEST_FILES_LINE_RE = re.compile(r"^\?\s+\S+\s+\[no test files\]$")
_GO_TEST_NO_TESTS_TO_RUN_LINE_RE = re.compile(r"^ok\s+\S+\s+\S+\s+\[no tests to run\]$")
_TOOLCHAIN_UNAVAILABLE_RE = re.compile(
    r"\b(?:requires|supports only|needs|need)\s+"
    r"(?:go|elixir|erlang|otp|node|npm|java|jdk|gradle|maven|ruby|python|swift)"
    r"\b.*\b(?:\d+(?:\.\d+)*)",
    re.IGNORECASE,
)
_LANGUAGE_VERSION_MISMATCH_RE = re.compile(
    r"\b(?:go|elixir|erlang|otp|node|npm|java|jdk|gradle|maven|ruby|python|swift)"
    r"\b.*\b(?:version|v\d+)\b.*\b(?:required|requires|unsupported|not supported)",
    re.IGNORECASE,
)
_LEGACY_PYTHON_RUNTIME_INCOMPATIBILITY_RE = re.compile(
    r"(?:attributeerror:\s*module\s+['\"]collections['\"]\s+has\s+no\s+attribute\s+"
    r"['\"](?:mapping|mutablemapping|sequence|iterable|callable)['\"]|"
    r"importerror:\s*cannot\s+import\s+name\s+['\"]"
    r"(?:mapping|mutablemapping|sequence|iterable|callable)['\"]\s+from\s+['\"]collections['\"])",
    re.IGNORECASE,
)
_DOCS_ONLY_TEXT_RE = re.compile(
    r"\b(?:doc|docs|documentation|readme|markdown|mdx|rst|changelog|contributing)\b"
)
_DOCTEST_TEXT_RE = re.compile(r"\bdoctests?\b|python\s+-m\s+doctest", re.IGNORECASE)
_CI_ONLY_TEXT_RE = re.compile(
    r"\b(?:github\s+actions?|gitlab\s+ci|circleci|buildkite|workflow|ci\s+pipeline|ci)\b"
)
_TERRAFORM_TEXT_RE = re.compile(r"\bterraform\b|\.tfvars?\b|terraform\.lock\.hcl")
_COMPOSE_TEXT_RE = re.compile(
    r"\b(?:docker\s+compose|docker-compose|compose\.ya?ml|compose\s+stack)\b"
)
_JS_FRONTEND_TEXT_RE = re.compile(
    r"\b(?:frontend|front-end|ui|client(?:-side)?|browser|react|preact|next(?:\.js)?|nextjs|vue|nuxt|svelte|angular|vite|webpack|javascript|typescript|node|npm|pnpm|yarn|component)\b"
)
_STATIC_WEB_TEXT_RE = re.compile(
    r"\b(?:static\s+(?:site|page|html)|html|css|landing\s+page|single\s+page|browser\s+page)\b"
)
_PYTHON_CODE_TASK_TEXT_RE = re.compile(
    r"\b(?:python|fastapi|flask|django|pydantic|sqlalchemy|alembic|uvicorn|endpoint|handler|route|serializer|schema|logging|logger|exception|traceback|import|upload|api)\b"
)
_STATIC_WEB_DIR_NAMES = {
    "assets",
    "css",
    "img",
    "images",
    "js",
    "public",
    "scripts",
    "static",
}
_STATIC_WEB_FILENAMES = {
    "favicon.ico",
    "manifest.json",
    "robots.txt",
    "site.webmanifest",
}
_STATIC_WEB_EXTENSIONS = {
    ".avif",
    ".css",
    ".gif",
    ".htm",
    ".html",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".mjs",
    ".png",
    ".svg",
    ".webmanifest",
    ".webp",
}
CONFIG_VERIFY_COMMANDS_FALLBACK_SOURCE = "config.verify_commands_fallback"
CONFIG_VERIFY_COMMANDS_GENERIC_PRESET_SOURCE = "config.verify_commands_generic_preset"
REPO_SCAN_NO_AUTHORITATIVE_SOURCE = "repo_scan.no_authoritative_commands"
GENERIC_VERIFY_FALLBACK_SOURCES = {
    CONFIG_VERIFY_COMMANDS_FALLBACK_SOURCE,
    CONFIG_VERIFY_COMMANDS_GENERIC_PRESET_SOURCE,
}
AUTHORITATIVE_VERIFY_CONTRACT_TYPES = {
    "authoritative_override",
    "explicit_override",
    "repo_native",
}
VERIFICATION_FALLBACK_DETECTED_SOURCE = "verification_fallback.detected_runner"
VERIFICATION_FALLBACK_BEST_EFFORT_SOURCE = "verification_fallback.best_effort"
# Sources whose commands a human or the managed host stated outright. Alysis Code
# may refuse to run them, but must never silently substitute its own guess for
# what somebody explicitly asked to be run.
EXPLICIT_VERIFY_COMMAND_SOURCES = {
    "environment.authoritative_verification_commands",
    "cli.verify_cmd",
    "config.verify_commands",
}
_DOCTEST_MODULE_NAMES = {"doctest"}
_PYTEST_DOCTEST_GLOB_PREFIX = "--doctest-glob"
_NODE_REPO_CLASSIFICATION_ALLOWED_MANIFEST_KINDS = {"build", "docker", "node", "typescript"}
_NODE_REPO_CLASSIFICATION_ALLOWED_LANGUAGE_HINTS = {"docker", "javascript", "typescript"}
_NODE_REPO_CLASSIFICATION_ALLOWED_PACKAGE_HINTS = _NODE_PACKAGE_HINTS | {"docker", "just", "make"}
_PYTHON_REPO_CLASSIFICATION_ALLOWED_MANIFEST_KINDS = {"build", "docker", "python"}
_PYTHON_REPO_CLASSIFICATION_ALLOWED_LANGUAGE_HINTS = {"docker", "python"}
_PYTHON_REPO_CLASSIFICATION_ALLOWED_PACKAGE_HINTS = {
    "docker",
    "hatch",
    "just",
    "make",
    "poetry",
    "python",
    "setuptools",
    "uv",
}
_MIXED_NODE_PYTHON_REPO_CLASSIFICATION_ALLOWED_MANIFEST_KINDS = {
    "build",
    "docker",
    "node",
    "python",
    "typescript",
}
_MIXED_NODE_PYTHON_REPO_CLASSIFICATION_ALLOWED_LANGUAGE_HINTS = {
    "docker",
    "javascript",
    "python",
    "typescript",
}
_MIXED_NODE_PYTHON_REPO_CLASSIFICATION_ALLOWED_PACKAGE_HINTS = (
    _NODE_REPO_CLASSIFICATION_ALLOWED_PACKAGE_HINTS
    | _PYTHON_REPO_CLASSIFICATION_ALLOWED_PACKAGE_HINTS
)
_REPO_CLASSIFICATION_NEUTRAL_NAMES = {"codeowners"}
_NODE_VERIFY_SCRIPT_PRIORITY = ("test", "build", "lint", "typecheck", "check")
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


class VerifyError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerificationExecutionAssessment:
    real_execution: bool | None
    non_execution_reason: str | None = None


@dataclass(frozen=True)
class VerifyCommandResult:
    command: str
    exit_code: int
    output: str
    stdout: str = ""
    stderr: str = ""
    effective_command: str | None = None
    fallback_used: bool = False
    fallback_reason: str | None = None
    real_execution: bool | None = None
    non_execution_reason: str | None = None
    host_launcher_unavailable: bool = False
    execution_context: str | None = None
    verification_execution_scope: str | None = None

    @property
    def status(self) -> VerificationCommandStatus:
        return command_status_from_execution(
            exit_code=self.exit_code,
            real_execution=self.real_execution,
            non_execution_reason=self.non_execution_reason,
        )

    @property
    def ok(self) -> bool:
        return self.status in {
            VerificationCommandStatus.PASSED,
            VerificationCommandStatus.SKIPPED,
        }


@dataclass(frozen=True)
class VerifyRunResult:
    commands: list[str]
    command_results: list[VerifyCommandResult]
    artifact_path: Path
    failure_category: FailureCategory | str | None = None

    @property
    def executed_count(self) -> int:
        """Commands that produced a real pass/fail outcome (skips excluded)."""
        return len(
            [
                item
                for item in self.command_results
                if item.status
                in {VerificationCommandStatus.PASSED, VerificationCommandStatus.FAILED}
            ]
        )

    @property
    def skipped_count(self) -> int:
        return len(
            [
                item
                for item in self.command_results
                if item.status == VerificationCommandStatus.SKIPPED
            ]
        )

    @property
    def status(self) -> str:
        """Tri-state run outcome: ``passed`` / ``failed`` / ``not_run``.

        A skip is not a pass: an empty command list, or a run
        where every command was benignly skipped, is ``not_run`` — never a
        green light. Any non-ok command (failed, inconclusive, not-executed,
        stale) makes the run ``failed``.
        """
        if any(not item.ok for item in self.command_results):
            return "failed"
        if self.executed_count > 0:
            return "passed"
        return "not_run"

    @property
    def all_passed(self) -> bool:
        """True only when at least one command really ran and everything is ok.

        Previously ``all([])`` made an empty or all-skipped run vacuously True
        . Consumers that need to distinguish "failed" from
        "never ran" should read ``status`` instead of this boolean.
        """
        return self.status == "passed"

    @property
    def failure_category_value(self) -> str | None:
        if self.status != "failed":
            return None
        return (
            failure_category_value(self.failure_category)
            or FailureCategory.VERIFICATION_FAILED.value
        )

    @property
    def failed_commands(self) -> list[str]:
        return [item.command for item in self.command_results if not item.ok]

    @property
    def summary(self) -> str:
        if not self.command_results:
            return "verification skipped: no commands"
        passed = len([item for item in self.command_results if item.ok])
        total = len(self.command_results)
        skipped = [
            item.command
            for item in self.command_results
            if item.status == VerificationCommandStatus.SKIPPED
        ]
        if self.status == "not_run":
            return f"verification skipped: nothing to verify ({passed}/{total})"
        if self.status == "passed":
            if skipped:
                return f"verification passed ({passed}/{total}); skipped: {', '.join(skipped)}"
            return f"verification passed ({passed}/{total})"
        return f"verification failed ({passed}/{total}); failed: {', '.join(self.failed_commands)}"


def verify_run_status(result: object) -> str:
    """Tri-state run status for any VerifyRunResult-like object.

    Falls back to deriving from ``all_passed`` for legacy or test-stub results
    that predate the ``status`` property, preserving their old semantics
    (a stub claiming ``all_passed=True`` keeps meaning "passed").
    """
    status = str(getattr(result, "status", "") or "")
    if status in {"passed", "failed", "not_run"}:
        return status
    return "passed" if bool(getattr(result, "all_passed", False)) else "failed"


@dataclass(frozen=True)
class VerifyArtifactPayload:
    artifact_path: str | None
    artifact_saved: bool
    artifact_readable_via_fs: bool
    artifact_location: str


@dataclass(frozen=True)
class ResolvedVerifyCommands:
    commands: tuple[str, ...]
    source: str
    reason: str = field(default="", compare=False)
    contract_type: str = field(default="", compare=False)
    command_specs: tuple[VerificationCommandSpec, ...] = field(
        default_factory=tuple,
        compare=False,
    )
    # True once selection has degraded: the session still runs, but no command
    # was found that Alysis Code can hold the result of a change against.
    best_effort: bool = field(default=False, compare=False)


def _default_verify_contract_type(source: str, *, commands: tuple[str, ...]) -> str:
    if source == "session.verification_disabled":
        return "disabled"
    if source == "environment.authoritative_verification_commands":
        return "authoritative_override"
    if source == "cli.verify_cmd":
        return "explicit_override"
    if source == "config.verify_commands":
        return "repo_native"
    if source == "repo_scan.likely_test_commands":
        return "selected"
    if source.startswith(
        (
            "task_refinement.node_test",
            "task_refinement.doctest",
            "task_refinement.explicit_pytest",
        )
    ):
        return "task_inferred"
    if not commands or source.endswith("no_authoritative_commands"):
        return "unavailable"
    if source in GENERIC_VERIFY_FALLBACK_SOURCES:
        return "generic_fallback"
    return "selected"


def _default_verify_selection_reason(source: str) -> str:
    return {
        "session.verification_disabled": "verification is disabled for this session",
        "environment.authoritative_verification_commands": (
            "managed runtime injected authoritative verification commands"
        ),
        "cli.verify_cmd": "explicit verification override supplied by the user",
        "config.verify_commands": "repo-specific verify_commands configuration is authoritative",
        "repo_scan.likely_test_commands": (
            "repo scan suggested verification commands from the existing workspace"
        ),
        CONFIG_VERIFY_COMMANDS_FALLBACK_SOURCE: (
            "using the configured generic fallback because repo scan found no repo-native command"
        ),
        CONFIG_VERIFY_COMMANDS_GENERIC_PRESET_SOURCE: (
            "using the configured generic verify preset because repo scan found no repo-native command"
        ),
        REPO_SCAN_NO_AUTHORITATIVE_SOURCE: (
            "repo scan invalidated the generic fallback because the workspace exposes no authoritative verification surface"
        ),
        "task_refinement.node_test": (
            "task-aware refinement preferred node --test over a generic Python fallback"
        ),
        "task_refinement.doctest": (
            "task-aware refinement selected doctest because the task explicitly requests it"
        ),
        "task_refinement.explicit_pytest": (
            "task-aware refinement selected the pytest command explicitly named by the task"
        ),
        "task_refinement.no_authoritative_commands": (
            "task-aware refinement suppressed the generic fallback because no confident verification command exists"
        ),
        VERIFICATION_FALLBACK_DETECTED_SOURCE: (
            "the originally selected verification command was unusable, so detection fell "
            "through to the runner this workspace exposes"
        ),
        VERIFICATION_FALLBACK_BEST_EFFORT_SOURCE: (
            "no usable verification command was found, so verification is best-effort for "
            "this session"
        ),
    }.get(source, "")


def _resolved_verify_commands(
    *,
    commands: tuple[str, ...] | list[str],
    source: str,
    reason: str | None = None,
    contract_type: str | None = None,
    best_effort: bool = False,
) -> ResolvedVerifyCommands:
    normalized_commands = tuple(str(item).strip() for item in commands if str(item).strip())
    resolved_reason = (
        reason.strip()
        if isinstance(reason, str) and reason.strip()
        else _default_verify_selection_reason(source)
    )
    resolved_contract_type = (
        contract_type.strip()
        if isinstance(contract_type, str) and contract_type.strip()
        else _default_verify_contract_type(source, commands=normalized_commands)
    )
    return ResolvedVerifyCommands(
        commands=normalized_commands,
        source=source,
        reason=resolved_reason,
        contract_type=resolved_contract_type,
        command_specs=build_verification_command_specs(
            normalized_commands,
            source=source,
            contract_type=resolved_contract_type,
        ),
        best_effort=best_effort,
    )


def verification_command_specs_for_selection(
    selection: ResolvedVerifyCommands | None,
) -> tuple[VerificationCommandSpec, ...]:
    if selection is None:
        return tuple()
    if selection.command_specs:
        return selection.command_specs
    return build_verification_command_specs(
        selection.commands,
        source=selection.source,
        contract_type=selection.contract_type,
    )


def verification_command_specs_payload(
    selection: ResolvedVerifyCommands | None,
) -> dict[str, Any]:
    specs = verification_command_specs_for_selection(selection)
    return {"verification_command_specs": command_specs_payload(specs)}


def required_verify_commands(selection: ResolvedVerifyCommands | None) -> tuple[str, ...]:
    """Keep recommendations available without turning them into requirements."""
    return tuple(
        spec.original_text
        for spec in verification_command_specs_for_selection(selection)
        if spec.requirement == VerificationCommandRequirement.REQUIRED
    )


def trusted_shell_expression_command_set(selection: ResolvedVerifyCommands | None) -> set[str]:
    return {
        " ".join(spec.original_text.split())
        for spec in verification_command_specs_for_selection(selection)
        if spec.execution_mode == VerificationCommandExecutionMode.TRUSTED_SHELL_EXPRESSION
        and spec.validation_status == VerificationCommandValidationStatus.VALID
    }


def validation_errors_for_selection(selection: ResolvedVerifyCommands | None) -> list[str]:
    if selection is None:
        return []
    errors: list[str] = []
    for spec in verification_command_specs_for_selection(selection):
        if spec.validation_status != VerificationCommandValidationStatus.INVALID:
            continue
        errors.append(f"{spec.original_text}: {spec.rejection_reason or 'invalid_command'}")
    return errors


def command_is_docs_doctest(command: str) -> bool:
    """True when a command only runs doctest over documentation files.

    Running the ``>>>`` examples in a README or a docs page proves the prose is
    still accurate; it proves nothing about the change that was just made to the
    code, so such a command is never an authoritative verification surface. Both
    shapes are covered: ``python -m doctest <docs>`` and pytest driven at doc
    files through ``--doctest-glob``.
    """
    parts = split_verify_command_parts(str(command or ""))
    if not parts:
        return False
    while parts and _is_env_assignment_token(parts[0]):
        parts = parts[1:]
    if parts and parts[0] == "env":
        parts = parts[1:]
        while parts and _is_env_assignment_token(parts[0]):
            parts = parts[1:]
    parts = strip_verify_runner_prefix(parts) or []
    if not parts:
        return False
    module_args: list[str]
    if _verify_command_basename(parts[0]) in _DOCTEST_MODULE_NAMES:
        module_args = list(parts[1:])
    elif len(parts) >= 3 and parts[1] == "-m" and parts[2].casefold() in _DOCTEST_MODULE_NAMES:
        module_args = list(parts[3:])
    else:
        normalized = normalize_verify_module_invocation(parts)
        if _verify_command_basename(normalized[0]) not in {"pytest", "py.test"}:
            return False
        module_args = list(normalized[1:])
        if not any(
            str(arg).casefold().startswith(_PYTEST_DOCTEST_GLOB_PREFIX) for arg in module_args
        ):
            return False
    targets = [arg for arg in module_args if not str(arg).startswith("-") and str(arg) != "--"]
    if not targets:
        return False
    return all(_looks_like_docs_only_target(str(target)) for target in targets)


def _verify_command_basename(token: str) -> str:
    name = PurePosixPath(str(token).replace("\\", "/")).name.casefold()
    return name[:-4] if name.endswith(".exe") else name


def verification_selection_payload(
    selection: ResolvedVerifyCommands,
    *,
    authoritative: bool,
) -> dict[str, Any]:
    return {
        "verification_selection_source": selection.source,
        "verification_selection_reason": selection.reason,
        "verification_contract_type": selection.contract_type,
        "verification_authoritative": authoritative,
        "verification_best_effort": selection.best_effort,
    }


def is_authoritative_verify_command_selection(selection: ResolvedVerifyCommands) -> bool:
    return selection.contract_type in AUTHORITATIVE_VERIFY_CONTRACT_TYPES and all(
        spec.requirement == VerificationCommandRequirement.REQUIRED
        for spec in verification_command_specs_for_selection(selection)
    )


def is_generic_fallback_verify_command_selection(selection: ResolvedVerifyCommands) -> bool:
    return selection.source in GENERIC_VERIFY_FALLBACK_SOURCES


def _task_signal_paths(task: dict[str, Any] | None) -> list[str]:
    if not isinstance(task, dict):
        return []
    signals: list[str] = []
    for key in ("estimated_files", "write_scope"):
        raw = task.get(key)
        if not isinstance(raw, list):
            continue
        for item in raw:
            value = str(item or "").strip().replace("\\", "/")
            while value.startswith("./"):
                value = value[2:]
            if value:
                signals.append(value)
    return signals


def _task_signal_texts(
    task: dict[str, Any] | None,
    *,
    plan_requirements: list[str] | None = None,
) -> list[str]:
    texts: list[str] = []
    if isinstance(task, dict):
        raw_acceptance = task.get("acceptance_criteria")
        if isinstance(raw_acceptance, list):
            for item in raw_acceptance:
                value = str(item or "").strip()
                if value:
                    texts.append(value)
    if isinstance(plan_requirements, list):
        for item in plan_requirements:
            value = str(item or "").strip()
            if value:
                texts.append(value)
    return texts


def _looks_like_js_or_ts_target(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/")
    if not normalized:
        return False
    name = PurePosixPath(normalized).name
    if name in _NODE_BOOTSTRAP_FILENAMES:
        return True
    return PurePosixPath(normalized).suffix.lower() in _NODE_JS_EXTENSIONS


def _looks_like_node_test_target(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/")
    if not normalized:
        return False
    return bool(_NODE_TEST_PATH_RE.search(normalized) or _NODE_TEST_SUFFIX_RE.search(normalized))


def _looks_like_python_verify_target(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/")
    if not normalized:
        return False
    pure = PurePosixPath(normalized)
    name = pure.name
    if name in _PYTHON_VERIFY_FILENAMES:
        return True
    return pure.suffix.lower() in {".py", ".pyi"}


def _looks_like_docs_only_target(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/")
    if not normalized:
        return False
    pure = PurePosixPath(normalized)
    if path_has_known_verification_surface(normalized):
        return False
    parts = [part.casefold() for part in pure.parts]
    name = pure.name.casefold()
    stem = pure.stem.casefold()
    if any(part in _DOC_ONLY_DIR_NAMES for part in parts[:-1]):
        return pure.suffix.casefold() in _DOC_ONLY_EXTENSIONS
    if name in _DOC_ONLY_DIR_NAMES:
        return True
    if name in _DOC_ONLY_FILENAMES or stem in _DOC_ONLY_FILENAMES:
        return True
    return any(
        name == prefix
        or name.startswith(f"{prefix}.")
        or name.startswith(f"{prefix}-")
        or name.startswith(f"{prefix}_")
        for prefix in _DOC_ONLY_FILENAME_PREFIXES
    )


def _looks_like_ci_target(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/")
    if not normalized:
        return False
    pure = PurePosixPath(normalized)
    parts = [part.casefold() for part in pure.parts]
    name = pure.name.casefold()
    if name in _CI_ONLY_FILENAMES:
        return True
    if name in _CI_ONLY_DIR_NAMES:
        return True
    if len(parts) >= 3 and parts[0] == ".github" and parts[1] == "workflows":
        return pure.suffix.casefold() in _CI_ONLY_WORKFLOW_FILENAMES
    return any(part in _CI_ONLY_DIR_NAMES for part in parts[:-1])


def _looks_like_terraform_target(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/")
    if not normalized:
        return False
    pure = PurePosixPath(normalized)
    if pure.name.casefold() in _TERRAFORM_FILENAMES:
        return True
    return pure.suffix.casefold() in _TERRAFORM_EXTENSIONS


def _looks_like_compose_target(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/")
    if not normalized:
        return False
    pure = PurePosixPath(normalized)
    return pure.name.casefold() in _COMPOSE_FILENAMES


def _looks_like_static_web_target(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/")
    if not normalized:
        return False
    pure = PurePosixPath(normalized)
    parts = [part.casefold() for part in pure.parts]
    name = pure.name.casefold()
    if name in _STATIC_WEB_DIR_NAMES:
        return True
    if name in _STATIC_WEB_FILENAMES:
        return True
    if pure.suffix.casefold() in _STATIC_WEB_EXTENSIONS:
        return True
    return bool(parts and parts[0] in _STATIC_WEB_DIR_NAMES)


def _explicitly_requests_node_test(texts: list[str]) -> bool:
    for raw_text in texts:
        normalized = re.sub(r'[`"]', "", str(raw_text or "")).strip().casefold()
        if not normalized:
            continue
        if _NODE_TEST_TEXT_NEGATIVE_RE.search(normalized):
            continue
        if _NODE_TEST_TEXT_POSITIVE_RE.search(normalized):
            return True
    return False


def _normalize_explicit_pytest_command(command: str) -> str | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if not parts:
        return None
    lowered = [part.casefold() for part in parts]
    if (
        len(parts) >= 3
        and lowered[0] in {"python", "python3", "py"}
        and lowered[1:3] == ["-m", "pytest"]
    ):
        return shlex.join([sys.executable, *parts[1:]])
    if lowered[0] in _VERIFY_PYTEST_ENTRYPOINTS:
        return shlex.join(parts)
    return None


def _explicit_pytest_commands(texts: list[str]) -> tuple[str, ...]:
    commands: list[str] = []
    seen: set[str] = set()
    for raw_text in texts:
        text = str(raw_text or "")
        candidates: list[tuple[str, bool]] = [
            (match.group(1).strip(), True) for match in _BACKTICK_COMMAND_RE.finditer(text)
        ]
        candidates.extend(
            (match.group(0).strip(), False) for match in _PYTEST_TEXT_COMMAND_RE.finditer(text)
        )
        for candidate, from_backticks in candidates:
            if "pytest" not in candidate.casefold():
                continue
            normalized = _normalize_explicit_pytest_command(candidate)
            if not normalized:
                continue
            if not from_backticks and len(shlex.split(normalized)) <= 1:
                continue
            key = normalized.casefold()
            if key in seen:
                continue
            seen.add(key)
            commands.append(normalized)
    return tuple(commands[:3])


def _texts_look_docs_only(texts: list[str]) -> bool:
    return any(_DOCS_ONLY_TEXT_RE.search(str(item or "").casefold()) for item in texts)


def _texts_request_doctest(texts: list[str]) -> bool:
    return any(_DOCTEST_TEXT_RE.search(str(item or "")) for item in texts)


def _doctest_target_paths(task_paths: list[str], *, root: Path | None) -> tuple[str, ...]:
    candidates: list[str] = []
    for raw_path in task_paths:
        normalized = str(raw_path or "").strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized or not _looks_like_docs_only_target(normalized):
            continue
        if normalized == "README" and root is not None and (root / "README.md").is_file():
            normalized = "README.md"
        candidates.append(normalized)
    if not candidates and root is not None:
        for readme_name in ("README.md", "README.rst", "README.txt", "README"):
            if (root / readme_name).is_file():
                candidates.append(readme_name)
                break
    seen: set[str] = set()
    out: list[str] = []
    for path in candidates:
        key = path.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return tuple(out)


def _doctest_verify_commands(paths: tuple[str, ...]) -> tuple[str, ...]:
    commands: list[str] = []
    if paths:
        commands.append(shlex.join([sys.executable, "-m", "doctest", *paths]))
    for path in paths:
        glob = PurePosixPath(path).name
        commands.append(
            shlex.join([sys.executable, "-m", "pytest", f"--doctest-glob={glob}", "-q", path])
        )
    return tuple(commands)


def _texts_look_ci_only(texts: list[str]) -> bool:
    return any(_CI_ONLY_TEXT_RE.search(str(item or "").casefold()) for item in texts)


def _texts_look_terraform_or_compose(texts: list[str]) -> bool:
    for item in texts:
        normalized = str(item or "").casefold()
        if _TERRAFORM_TEXT_RE.search(normalized) or _COMPOSE_TEXT_RE.search(normalized):
            return True
    return False


def _texts_mention_compose_shorthand(texts: list[str]) -> bool:
    return any(re.search(r"\bcompose\b", str(item or "").casefold()) for item in texts)


def _texts_look_js_frontend_task(texts: list[str]) -> bool:
    return any(_JS_FRONTEND_TEXT_RE.search(str(item or "").casefold()) for item in texts)


def _texts_look_static_web_task(texts: list[str]) -> bool:
    return any(_STATIC_WEB_TEXT_RE.search(str(item or "").casefold()) for item in texts)


def _texts_look_python_code_task(texts: list[str]) -> bool:
    return any(_PYTHON_CODE_TASK_TEXT_RE.search(str(item or "").casefold()) for item in texts)


def _repo_has_node_hints(scan: RepoScanResult | None) -> bool:
    if scan is None:
        return False
    language_hints = {str(item).strip().lower() for item in scan.language_hints}
    package_hints = {str(item).strip().lower() for item in scan.package_hints}
    return bool(
        {"javascript", "typescript"} & language_hints
        or _NODE_PACKAGE_HINTS & package_hints
        or any(
            PurePosixPath(str(item.get("path") or "")).name in _NODE_BOOTSTRAP_FILENAMES
            for item in scan.manifests
        )
    )


def _repo_has_python_hints(scan: RepoScanResult | None) -> bool:
    if scan is None:
        return False
    language_hints = {str(item).strip().lower() for item in scan.language_hints}
    package_hints = {str(item).strip().lower() for item in scan.package_hints}
    return bool(
        "python" in language_hints
        or "python" in package_hints
        or any(str(item.get("kind") or "").strip().lower() == "python" for item in scan.manifests)
    )


def _repo_has_authoritative_verify_commands(scan: RepoScanResult | None) -> bool:
    if scan is None:
        return False
    return bool(normalize_verify_command_list(scan.likely_test_commands))


def _repo_has_compose_hints(scan: RepoScanResult | None) -> bool:
    if scan is None:
        return False
    return any(
        PurePosixPath(str(item.get("path") or "")).name.casefold() in _COMPOSE_FILENAMES
        for item in scan.manifests
    )


def _scan_manifest_kind_set(scan: RepoScanResult | None) -> set[str]:
    if scan is None:
        return set()
    return {
        str(item.get("kind") or "").strip().lower()
        for item in scan.manifests
        if str(item.get("kind") or "").strip()
    }


def _scan_language_hint_set(scan: RepoScanResult | None) -> set[str]:
    if scan is None:
        return set()
    return {str(item).strip().lower() for item in scan.language_hints if str(item).strip()}


def _scan_package_hint_set(scan: RepoScanResult | None) -> set[str]:
    if scan is None:
        return set()
    return {str(item).strip().lower() for item in scan.package_hints if str(item).strip()}


def _scan_classification_paths(scan: RepoScanResult | None) -> list[str]:
    if scan is None:
        return []
    seen: set[str] = set()
    paths: list[str] = []
    raw_paths = [
        *[str(item.get("path") or "") for item in scan.top_level_entries],
        *[str(item.get("path") or "") for item in scan.manifests],
        *[str(item or "") for item in scan.readme_paths],
        str(scan.conventions_path or ""),
    ]
    for raw in raw_paths:
        normalized = str(raw or "").strip().replace("\\", "/")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        paths.append(normalized)
    return paths


def _looks_like_neutral_repo_classification_target(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/")
    if not normalized:
        return True
    if (
        _looks_like_docs_only_target(normalized)
        or _looks_like_ci_target(normalized)
        or _looks_like_terraform_target(normalized)
        or _looks_like_compose_target(normalized)
        or _looks_like_js_or_ts_target(normalized)
        or _looks_like_python_verify_target(normalized)
    ):
        return False
    pure = PurePosixPath(normalized)
    name = pure.name.casefold()
    if name in _REPO_CLASSIFICATION_NEUTRAL_NAMES:
        return True
    return name.startswith(".")


def _repo_paths_match_confident_shape(
    scan: RepoScanResult | None,
    *,
    path_matcher: Any,
    allow_docs: bool = False,
) -> bool:
    matched = False
    for path in _scan_classification_paths(scan):
        if _looks_like_neutral_repo_classification_target(path):
            continue
        if path_matcher(path):
            matched = True
            continue
        if allow_docs and _looks_like_docs_only_target(path):
            continue
        return False
    return matched


def _repo_is_confident_docs_only_without_verify_surface(scan: RepoScanResult | None) -> bool:
    if scan is None or _repo_has_authoritative_verify_commands(scan):
        return False
    if (
        _scan_manifest_kind_set(scan)
        or _scan_language_hint_set(scan)
        or _scan_package_hint_set(scan)
    ):
        return False
    return _repo_paths_match_confident_shape(scan, path_matcher=_looks_like_docs_only_target)


def _repo_is_confident_static_web_without_verify_surface(scan: RepoScanResult | None) -> bool:
    if scan is None or _repo_has_authoritative_verify_commands(scan):
        return False
    if (
        _scan_manifest_kind_set(scan)
        or _scan_language_hint_set(scan)
        or _scan_package_hint_set(scan)
    ):
        return False
    return _repo_paths_match_confident_shape(
        scan,
        path_matcher=_looks_like_static_web_target,
        allow_docs=True,
    )


def _repo_is_confident_ci_only_without_verify_surface(scan: RepoScanResult | None) -> bool:
    if scan is None or _repo_has_authoritative_verify_commands(scan):
        return False
    if (
        _scan_manifest_kind_set(scan)
        or _scan_language_hint_set(scan)
        or _scan_package_hint_set(scan)
    ):
        return False
    return _repo_paths_match_confident_shape(
        scan,
        path_matcher=_looks_like_ci_target,
        allow_docs=True,
    )


def _repo_is_confident_terraform_only_without_verify_surface(scan: RepoScanResult | None) -> bool:
    if scan is None or _repo_has_authoritative_verify_commands(scan):
        return False
    if _repo_has_node_hints(scan) or _repo_has_python_hints(scan):
        return False
    if _scan_manifest_kind_set(scan) - {"build"}:
        return False
    if _scan_language_hint_set(scan):
        return False
    if _scan_package_hint_set(scan) - {"just", "make"}:
        return False
    return _repo_paths_match_confident_shape(
        scan,
        path_matcher=_looks_like_terraform_target,
        allow_docs=True,
    )


def _repo_is_confident_compose_only_without_verify_surface(scan: RepoScanResult | None) -> bool:
    if scan is None or _repo_has_authoritative_verify_commands(scan):
        return False
    if _repo_has_node_hints(scan) or _repo_has_python_hints(scan):
        return False
    if _scan_manifest_kind_set(scan) - {"build", "docker"}:
        return False
    if _scan_language_hint_set(scan) - {"docker"}:
        return False
    if _scan_package_hint_set(scan) - {"docker", "just", "make"}:
        return False
    return _repo_paths_match_confident_shape(
        scan,
        path_matcher=_looks_like_compose_target,
        allow_docs=True,
    )


def _repo_is_confident_node_workspace_without_tests(scan: RepoScanResult | None) -> bool:
    if (
        scan is None
        or _repo_has_authoritative_verify_commands(scan)
        or not _repo_has_node_hints(scan)
        or _repo_has_python_hints(scan)
    ):
        return False
    manifest_kinds = _scan_manifest_kind_set(scan)
    language_hints = _scan_language_hint_set(scan)
    package_hints = _scan_package_hint_set(scan)
    if manifest_kinds and not manifest_kinds <= _NODE_REPO_CLASSIFICATION_ALLOWED_MANIFEST_KINDS:
        return False
    if language_hints and not language_hints <= _NODE_REPO_CLASSIFICATION_ALLOWED_LANGUAGE_HINTS:
        return False
    if package_hints and not package_hints <= _NODE_REPO_CLASSIFICATION_ALLOWED_PACKAGE_HINTS:
        return False
    return bool(
        manifest_kinds & {"node", "typescript"} or package_hints & (_NODE_PACKAGE_HINTS | {"node"})
    )


def _repo_is_confident_python_workspace_without_tests(scan: RepoScanResult | None) -> bool:
    if (
        scan is None
        or _repo_has_authoritative_verify_commands(scan)
        or not _repo_has_python_hints(scan)
        or _repo_has_node_hints(scan)
    ):
        return False
    manifest_kinds = _scan_manifest_kind_set(scan)
    language_hints = _scan_language_hint_set(scan)
    package_hints = _scan_package_hint_set(scan)
    if manifest_kinds and not manifest_kinds <= _PYTHON_REPO_CLASSIFICATION_ALLOWED_MANIFEST_KINDS:
        return False
    if language_hints and not language_hints <= _PYTHON_REPO_CLASSIFICATION_ALLOWED_LANGUAGE_HINTS:
        return False
    if package_hints and not package_hints <= _PYTHON_REPO_CLASSIFICATION_ALLOWED_PACKAGE_HINTS:
        return False
    return "python" in language_hints or "python" in package_hints or "python" in manifest_kinds


def _repo_is_confident_mixed_node_python_workspace_without_tests(
    scan: RepoScanResult | None,
) -> bool:
    if (
        scan is None
        or _repo_has_authoritative_verify_commands(scan)
        or not _repo_has_node_hints(scan)
        or not _repo_has_python_hints(scan)
    ):
        return False
    manifest_kinds = _scan_manifest_kind_set(scan)
    language_hints = _scan_language_hint_set(scan)
    package_hints = _scan_package_hint_set(scan)
    if (
        manifest_kinds
        and not manifest_kinds <= _MIXED_NODE_PYTHON_REPO_CLASSIFICATION_ALLOWED_MANIFEST_KINDS
    ):
        return False
    if (
        language_hints
        and not language_hints <= _MIXED_NODE_PYTHON_REPO_CLASSIFICATION_ALLOWED_LANGUAGE_HINTS
    ):
        return False
    if (
        package_hints
        and not package_hints <= _MIXED_NODE_PYTHON_REPO_CLASSIFICATION_ALLOWED_PACKAGE_HINTS
    ):
        return False
    has_node_surface = bool(
        manifest_kinds & {"node", "typescript"}
        or language_hints & {"javascript", "typescript"}
        or package_hints & (_NODE_PACKAGE_HINTS | {"node"})
    )
    has_python_surface = bool(
        "python" in manifest_kinds or "python" in language_hints or "python" in package_hints
    )
    return has_node_surface and has_python_surface


def _repo_grounded_no_authoritative_selection(
    scan: RepoScanResult | None,
) -> ResolvedVerifyCommands | None:
    if scan is None or _repo_has_authoritative_verify_commands(scan):
        return None
    if _repo_is_confident_docs_only_without_verify_surface(scan):
        return _resolved_verify_commands(
            commands=(),
            source=REPO_SCAN_NO_AUTHORITATIVE_SOURCE,
            reason="repo scan found a docs-only workspace with no authoritative verification surface",
            contract_type="unavailable",
        )
    if _repo_is_confident_static_web_without_verify_surface(scan):
        return _resolved_verify_commands(
            commands=(),
            source=REPO_SCAN_NO_AUTHORITATIVE_SOURCE,
            reason="repo scan found a static web workspace with no authoritative verification surface",
            contract_type="unavailable",
        )
    if _repo_is_confident_ci_only_without_verify_surface(scan):
        return _resolved_verify_commands(
            commands=(),
            source=REPO_SCAN_NO_AUTHORITATIVE_SOURCE,
            reason="repo scan found a CI-only workspace with no authoritative verification surface",
            contract_type="unavailable",
        )
    if _repo_is_confident_terraform_only_without_verify_surface(scan):
        return _resolved_verify_commands(
            commands=(),
            source=REPO_SCAN_NO_AUTHORITATIVE_SOURCE,
            reason="repo scan found a Terraform-only workspace with no authoritative verification surface",
            contract_type="unavailable",
        )
    if _repo_is_confident_compose_only_without_verify_surface(scan):
        return _resolved_verify_commands(
            commands=(),
            source=REPO_SCAN_NO_AUTHORITATIVE_SOURCE,
            reason="repo scan found a Compose-only workspace with no authoritative verification surface",
            contract_type="unavailable",
        )
    if _repo_is_confident_mixed_node_python_workspace_without_tests(scan):
        return _resolved_verify_commands(
            commands=(),
            source=REPO_SCAN_NO_AUTHORITATIVE_SOURCE,
            reason="repo scan found a mixed workspace without an authoritative verification surface",
            contract_type="unavailable",
        )
    if _repo_is_confident_node_workspace_without_tests(scan):
        return _resolved_verify_commands(
            commands=(),
            source=REPO_SCAN_NO_AUTHORITATIVE_SOURCE,
            reason="repo scan found a JS/Node workspace without a real repo-native test command",
            contract_type="unavailable",
        )
    if _repo_is_confident_python_workspace_without_tests(scan):
        return _resolved_verify_commands(
            commands=(),
            source=REPO_SCAN_NO_AUTHORITATIVE_SOURCE,
            reason="repo scan found a Python workspace without a discoverable test surface",
            contract_type="unavailable",
        )
    return None


def refine_generic_fallback_verify_command_selection(
    *,
    selection: ResolvedVerifyCommands,
    task: dict[str, Any] | None,
    root: Path | None = None,
    repo_scan: RepoScanResult | None = None,
    plan_requirements: list[str] | None = None,
) -> ResolvedVerifyCommands:
    if not is_generic_fallback_verify_command_selection(selection):
        return selection
    suppressible_generic_fallback = is_generic_fallback_verify_command_selection(selection)

    scan = repo_scan
    if scan is None and root is not None:
        try:
            scan = scan_workspace(context=resolve_workspace_context(root))
        except (WorkspaceContextError, OSError):
            scan = None

    task_paths = _task_signal_paths(task)
    generic_commands_apply_to_task_paths = bool(
        task_paths
    ) and verification_commands_apply_to_paths(
        set(task_paths),
        selection.commands,
    )
    task_texts = _task_signal_texts(task, plan_requirements=plan_requirements)
    repo_grounded_no_authoritative = _repo_grounded_no_authoritative_selection(scan)
    task_has_python_signals = any(_looks_like_python_verify_target(path) for path in task_paths)
    task_has_docs_only_signals = bool(task_paths) and all(
        _looks_like_docs_only_target(path) for path in task_paths
    )
    task_has_static_web_signals = bool(task_paths) and all(
        _looks_like_static_web_target(path) or _looks_like_docs_only_target(path)
        for path in task_paths
    )
    task_has_ci_only_signals = bool(task_paths) and all(
        _looks_like_ci_target(path) for path in task_paths
    )
    task_has_terraform_only_signals = bool(task_paths) and all(
        _looks_like_terraform_target(path) for path in task_paths
    )
    task_has_compose_only_signals = bool(task_paths) and all(
        _looks_like_compose_target(path) for path in task_paths
    )
    repo_has_authoritative_commands = _repo_has_authoritative_verify_commands(scan)
    repo_node_hints = _repo_has_node_hints(scan)
    repo_python_hints = _repo_has_python_hints(scan)

    task_has_node_test_targets = any(_looks_like_node_test_target(path) for path in task_paths)
    if task_has_node_test_targets:
        return _resolved_verify_commands(
            commands=("node --test",),
            source="task_refinement.node_test",
            reason="task targets Node test files, so node --test is a better fit than generic pytest",
            contract_type="task_inferred",
        )

    task_has_node_bootstrap_targets = any(
        PurePosixPath(path).name in _NODE_BOOTSTRAP_FILENAMES for path in task_paths
    )
    task_has_js_targets = any(_looks_like_js_or_ts_target(path) for path in task_paths)
    task_explicitly_requests_node_test = _explicitly_requests_node_test(task_texts)
    task_has_pathless_js_frontend_signals = (
        not task_paths
        and repo_node_hints
        and not repo_has_authoritative_commands
        and _texts_look_js_frontend_task(task_texts)
    )

    if task_explicitly_requests_node_test and (
        task_has_node_bootstrap_targets or task_has_js_targets or repo_node_hints
    ):
        return _resolved_verify_commands(
            commands=("node --test",),
            source="task_refinement.node_test",
            reason="task text explicitly requests node --test in a JS/Node-compatible context",
            contract_type="task_inferred",
        )

    repo_has_compose_hints = _repo_has_compose_hints(scan)
    task_has_pathless_compose_shorthand = (
        not task_paths and repo_has_compose_hints and _texts_mention_compose_shorthand(task_texts)
    )
    explicit_pytest_commands = _explicit_pytest_commands(task_texts)
    if explicit_pytest_commands and (
        task_has_python_signals
        or repo_python_hints
        or task_has_docs_only_signals
        or _texts_look_python_code_task(task_texts)
    ):
        return _resolved_verify_commands(
            commands=explicit_pytest_commands,
            source="task_refinement.explicit_pytest",
            reason="task text explicitly names pytest verification command(s)",
            contract_type="task_inferred",
        )
    doctest_paths = (
        _doctest_target_paths(task_paths, root=root) if _texts_request_doctest(task_texts) else ()
    )
    if doctest_paths:
        return _resolved_verify_commands(
            commands=_doctest_verify_commands(doctest_paths),
            source="task_refinement.doctest",
            reason="task text explicitly requests doctest for documentation targets",
            contract_type="task_inferred",
        )
    if task_has_docs_only_signals or (not task_paths and _texts_look_docs_only(task_texts)):
        if not suppressible_generic_fallback:
            return selection
        return _resolved_verify_commands(
            commands=(),
            source="task_refinement.no_authoritative_commands",
            reason="docs-only task does not expose a confident verification command",
            contract_type="unavailable",
        )
    if task_has_static_web_signals or (not task_paths and _texts_look_static_web_task(task_texts)):
        if not suppressible_generic_fallback:
            return selection
        return _resolved_verify_commands(
            commands=(),
            source="task_refinement.no_authoritative_commands",
            reason="static web task does not expose a confident verification command",
            contract_type="unavailable",
        )
    if task_has_ci_only_signals or (not task_paths and _texts_look_ci_only(task_texts)):
        if not suppressible_generic_fallback:
            return selection
        return _resolved_verify_commands(
            commands=(),
            source="task_refinement.no_authoritative_commands",
            reason="CI-only task does not expose a confident repo-native verification command",
            contract_type="unavailable",
        )
    if (
        task_has_terraform_only_signals
        or task_has_compose_only_signals
        or (not task_paths and _texts_look_terraform_or_compose(task_texts))
        or task_has_pathless_compose_shorthand
    ):
        if not suppressible_generic_fallback:
            return selection
        return _resolved_verify_commands(
            commands=(),
            source="task_refinement.no_authoritative_commands",
            reason="Terraform/Compose task does not expose a confident repo-native verification command",
            contract_type="unavailable",
        )
    task_has_pathless_python_code_signals = (
        not task_paths and repo_python_hints and _texts_look_python_code_task(task_texts)
    )
    if task_has_python_signals or task_has_pathless_python_code_signals:
        if (
            scan is not None
            and not repo_has_authoritative_commands
            and suppressible_generic_fallback
        ):
            return _resolved_verify_commands(
                commands=(),
                source="task_refinement.no_authoritative_commands",
                reason="Python task has no discoverable test surface, so generic pytest is not trusted",
                contract_type="unavailable",
            )
        return selection

    node_project_script_commands = (
        normalize_verify_command_list(
            _infer_node_project_script_verify_commands(root=root, repo_scan=scan)
        )
        if root is not None
        and scan is not None
        and repo_node_hints
        and not repo_has_authoritative_commands
        else ()
    )
    task_points_to_node_surface = (
        task_has_node_bootstrap_targets
        or task_has_js_targets
        or task_has_pathless_js_frontend_signals
        or (not task_paths and not repo_python_hints)
    )
    if node_project_script_commands and (not repo_python_hints or task_points_to_node_surface):
        return _resolved_verify_commands(
            commands=node_project_script_commands,
            source="repo_scan.likely_test_commands",
            reason="repo scan discovered package.json verification scripts",
        )

    if (
        task_has_node_bootstrap_targets
        or task_has_js_targets
        or task_has_pathless_js_frontend_signals
    ):
        if not suppressible_generic_fallback:
            return selection
        return _resolved_verify_commands(
            commands=(),
            source="task_refinement.no_authoritative_commands",
            reason="frontend/JS task should not inherit a generic Python verification fallback",
            contract_type="unavailable",
        )

    if repo_grounded_no_authoritative is not None and suppressible_generic_fallback:
        return repo_grounded_no_authoritative

    if task_paths and suppressible_generic_fallback and not generic_commands_apply_to_task_paths:
        return _resolved_verify_commands(
            commands=(),
            source="task_refinement.no_authoritative_commands",
            reason=("configured generic verification commands do not apply to the task paths"),
            contract_type="unavailable",
        )

    if (
        scan is not None
        and suppressible_generic_fallback
        and not repo_has_authoritative_commands
        and not repo_python_hints
        and not task_has_python_signals
        and not task_has_pathless_python_code_signals
        and not explicit_pytest_commands
        and not generic_commands_apply_to_task_paths
    ):
        return _resolved_verify_commands(
            commands=(),
            source=REPO_SCAN_NO_AUTHORITATIVE_SOURCE,
            reason=(
                "generic pytest fallback requires a trustworthy pre-existing Python test surface"
            ),
            contract_type="unavailable",
        )

    return selection


def resolve_task_aware_verify_command_selection(
    *,
    cfg: AppConfig,
    verify_cmd: list[str] | None,
    task: dict[str, Any] | None,
    root: Path | None = None,
    repo_scan: RepoScanResult | None = None,
    plan_requirements: list[str] | None = None,
    selection: ResolvedVerifyCommands | None = None,
    allow_empty_config: bool = False,
) -> ResolvedVerifyCommands:
    resolved = selection
    if resolved is None:
        resolved = resolve_verify_command_selection(
            cfg=cfg,
            verify_cmd=verify_cmd,
            root=root,
            repo_scan=repo_scan,
            allow_empty_config=allow_empty_config,
        )
    return refine_generic_fallback_verify_command_selection(
        selection=resolved,
        task=task,
        root=root,
        repo_scan=repo_scan,
        plan_requirements=plan_requirements,
    )


def resolve_authoritative_task_verify_command_selection(
    *,
    cfg: AppConfig,
    verify_cmd: list[str] | None,
    task: dict[str, Any] | None,
    root: Path | None = None,
    repo_scan: RepoScanResult | None = None,
    plan_requirements: list[str] | None = None,
    selection: ResolvedVerifyCommands | None = None,
    allow_empty_config: bool = False,
) -> ResolvedVerifyCommands:
    resolved = selection
    if resolved is not None and verify_cmd:
        normalized_commands = tuple(
            resolve_verify_commands(
                cfg=cfg,
                verify_cmd=verify_cmd,
                root=root,
                repo_scan=repo_scan,
            )
        )
        if normalized_commands != resolved.commands:
            resolved = resolve_verify_command_selection(
                cfg=cfg,
                verify_cmd=verify_cmd,
                root=root,
                repo_scan=repo_scan,
            )
    return resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=(verify_cmd if resolved is None else None),
        task=task,
        root=root,
        repo_scan=repo_scan,
        plan_requirements=plan_requirements,
        selection=resolved,
        allow_empty_config=allow_empty_config,
    )


@dataclass(frozen=True)
class VerificationCommandExecution:
    exit_code: int
    stdout: str
    stderr: str
    host_launcher_unavailable: bool = False
    execution_context: str | None = None
    admission_refused: bool = False

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


def _clip_verification_failure_snippet(text: str, *, max_chars: int) -> str:
    snippet = str(text or "").strip()
    if not snippet:
        return ""
    if len(snippet) <= max_chars:
        return snippet
    return snippet[: max_chars - 3].rstrip() + "..."


def _extract_failure_snippet_line(
    text: str,
    *,
    max_chars: int,
    allow_fallback: bool,
) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return ""

    for line in lines:
        if any(marker in line for marker in _VERIFICATION_FAILURE_PRIORITY_MARKERS):
            return _clip_verification_failure_snippet(line, max_chars=max_chars)

    for line in lines:
        if line.startswith("E   ") or line.startswith("FAILED "):
            return _clip_verification_failure_snippet(line, max_chars=max_chars)

    for line in lines:
        lowered = line.casefold()
        if "error" in lowered or "failed" in lowered or "exception" in lowered:
            return _clip_verification_failure_snippet(line, max_chars=max_chars)

    if not allow_fallback:
        return ""
    return _clip_verification_failure_snippet(lines[0], max_chars=max_chars)


def extract_actionable_failure_snippet(
    text: str,
    *,
    max_chars: int = VERIFICATION_FAILURE_SNIPPET_MAX_CHARS,
) -> str:
    return _extract_failure_snippet_line(
        text,
        max_chars=max(1, int(max_chars)),
        allow_fallback=True,
    )


def build_primary_verification_failure(
    *,
    result: VerifyRunResult,
    output_preview_chars: int = VERIFY_OUTPUT_PREVIEW_CHARS,
    snippet_chars: int = VERIFICATION_FAILURE_SNIPPET_MAX_CHARS,
) -> dict[str, object] | None:
    if result.status != "failed":
        # not_run has nothing to report as a failure; it is a skip, not a fail.
        return None

    preview_chars = max(1, int(output_preview_chars))
    max_snippet_chars = max(1, int(snippet_chars))
    first_failed_item: VerifyCommandResult | None = None

    for item in result.command_results:
        if item.ok:
            continue
        if first_failed_item is None:
            first_failed_item = item
        snippet = _extract_failure_snippet_line(
            item.output,
            max_chars=max_snippet_chars,
            allow_fallback=False,
        )
        if not snippet:
            continue
        effective_command = item.effective_command or item.command
        return {
            "command": item.command,
            "effective_command": effective_command,
            "snippet": snippet,
            "output_truncated": len(item.output) > preview_chars,
            "fallback_used": item.fallback_used,
        }

    if first_failed_item is not None:
        prefer_raw_output = first_failed_item.non_execution_reason in {
            "execution_layer_failure",
            "host_launcher_unavailable",
        }
        snippet = ""
        if prefer_raw_output:
            snippet = _extract_failure_snippet_line(
                first_failed_item.output,
                max_chars=max_snippet_chars,
                allow_fallback=True,
            )
        if not snippet and not prefer_raw_output:
            snippet = _extract_failure_snippet_line(
                result.summary,
                max_chars=max_snippet_chars,
                allow_fallback=True,
            )
        if not snippet:
            failed_command_hint = str(result.failed_commands[0]) if result.failed_commands else ""
            snippet = _clip_verification_failure_snippet(
                failed_command_hint,
                max_chars=max_snippet_chars,
            )
        if not snippet:
            snippet = _extract_failure_snippet_line(
                first_failed_item.output,
                max_chars=max_snippet_chars,
                allow_fallback=True,
            )
        if snippet:
            effective_command = first_failed_item.effective_command or first_failed_item.command
            return {
                "command": first_failed_item.command,
                "effective_command": effective_command,
                "snippet": snippet,
                "output_truncated": len(first_failed_item.output) > preview_chars,
                "fallback_used": first_failed_item.fallback_used,
            }

    if result.failed_commands:
        snippet = _clip_verification_failure_snippet(
            str(result.failed_commands[0]),
            max_chars=max_snippet_chars,
        )
        if snippet:
            return {
                "command": str(result.failed_commands[0]),
                "effective_command": str(result.failed_commands[0]),
                "snippet": snippet,
                "output_truncated": False,
                "fallback_used": False,
            }
    return None


def extract_verification_failure_snippet(
    *,
    tool_name: str,
    result: dict[str, Any],
    max_chars: int = VERIFICATION_FAILURE_SNIPPET_MAX_CHARS,
) -> str:
    normalized_tool = str(tool_name or "").strip().lower()
    snippet_limit = max(1, int(max_chars))

    if normalized_tool == "verify_run":
        primary_failure = result.get("primary_failure")
        if isinstance(primary_failure, dict):
            snippet = _extract_failure_snippet_line(
                str(primary_failure.get("snippet") or ""),
                max_chars=snippet_limit,
                allow_fallback=True,
            )
            if snippet:
                return snippet

        command_results = result.get("command_results")
        if isinstance(command_results, list):
            for item in command_results:
                if not isinstance(item, dict):
                    continue
                ok = item.get("ok")
                if isinstance(ok, bool):
                    failed = not ok
                else:
                    exit_code = item.get("exit_code")
                    failed = not (isinstance(exit_code, int) and exit_code == 0)
                if not failed:
                    continue
                snippet = _extract_failure_snippet_line(
                    str(item.get("output_preview") or ""),
                    max_chars=snippet_limit,
                    allow_fallback=True,
                )
                if snippet:
                    return snippet
        summary = _extract_failure_snippet_line(
            str(result.get("summary") or ""),
            max_chars=snippet_limit,
            allow_fallback=True,
        )
        if summary:
            return summary
        failed_commands = result.get("failed_commands")
        if isinstance(failed_commands, list) and failed_commands:
            return _clip_verification_failure_snippet(
                str(failed_commands[0]),
                max_chars=snippet_limit,
            )
        return ""

    if normalized_tool == "shell_run":
        combined = "\n".join(
            [
                str(result.get("stderr") or "").strip(),
                str(result.get("stdout") or "").strip(),
            ]
        ).strip()
        return _extract_failure_snippet_line(
            combined,
            max_chars=snippet_limit,
            allow_fallback=True,
        )

    return ""


def _truncate_verify_output(text: str, *, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return (text, False)
    if max_chars <= len("...(truncated)"):
        return (text[:max_chars], True)
    return (text[: max_chars - len("...(truncated)")].rstrip() + "...(truncated)", True)


def _is_env_assignment_token(token: str) -> bool:
    return re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token) is not None


def _split_shell_command_parts(command: str) -> list[str] | None:
    return split_verify_command_parts(command)


def _normalize_shell_command(command: str) -> str:
    return " ".join(str(command or "").split())


def _normalize_command_for_control_flow_detection(command: str) -> str | None:
    normalized = _normalize_shell_command(command)
    if not normalized:
        return None

    current = normalized
    while True:
        parts = _split_shell_command_parts(current)
        if not parts:
            return None

        stripped_env = _strip_execution_env_prefix(parts)
        if stripped_env is None:
            return None
        if stripped_env != parts:
            current = shlex.join(stripped_env)
            continue

        stripped_runner = _strip_execution_runner_prefix(parts)
        if stripped_runner is None:
            return None
        if stripped_runner != parts:
            current = shlex.join(stripped_runner)
            continue

        wrapped = _unwrap_shell_wrapper_command(current)
        if wrapped and wrapped != current:
            current = wrapped
            continue

        return current


def _unwrap_shell_wrapper_command(command: str) -> str | None:
    parts = _split_shell_command_parts(command)
    if not parts:
        return None

    head = parts[0].strip().lower()
    if head in {"bash", "sh", "zsh"}:
        if len(parts) == 3 and parts[1] == "-lc":
            return _normalize_shell_command(parts[2])
        return None
    if head == "fish":
        if len(parts) == 3 and parts[1] == "-c":
            return _normalize_shell_command(parts[2])
        return None
    if head == "cmd":
        if len(parts) == 3 and parts[1].lower() == "/c":
            return _normalize_shell_command(parts[2])
        return None
    if head in {"powershell", "pwsh"}:
        if len(parts) == 3 and parts[1].lower() == "-command":
            return _normalize_shell_command(parts[2])
        return None
    return None


def _strip_execution_env_prefix(parts: list[str]) -> list[str] | None:
    out = list(parts)
    if not out:
        return None
    if out[0].lower() == "env":
        out = out[1:]
        if not out or out[0].startswith("-"):
            return None
    while out and _is_env_assignment_token(out[0]):
        out = out[1:]
    return out or None


def _strip_execution_runner_prefix(parts: list[str]) -> list[str] | None:
    return strip_verify_runner_prefix(parts)


def _normalize_execution_semantics_parts(command: str) -> list[str] | None:
    analysis = analyze_verification_command(command, trusted=True)
    return list(analysis.parts) if analysis.parts else None


def _verification_family_for_result(command: str) -> str | None:
    return analyze_verification_command(command, trusted=True).command_family


def _has_shell_control_flow(command: str) -> bool:
    analysis = analyze_verification_command(command, trusted=True)
    return analysis.shell_control_flow in {"unsafe", "pipeline"} or analysis.rejection_reason in {
        "disallowed_shell_control_flow",
        "unsafe_pipeline",
    }


def assess_verification_command_execution(
    *,
    command: str,
    exit_code: int,
    output: str,
) -> VerificationExecutionAssessment:
    analysis = analyze_verification_command(command, trusted=True)
    family = analysis.command_family
    if _is_execution_layer_failure(exit_code=exit_code, output=output):
        return VerificationExecutionAssessment(
            real_execution=False,
            non_execution_reason="execution_layer_failure",
        )
    if analysis.rejection_reason:
        if exit_code == 0:
            return VerificationExecutionAssessment(
                real_execution=False,
                non_execution_reason=analysis.rejection_reason,
            )
        return VerificationExecutionAssessment(real_execution=None)
    if family == "pytest" and (exit_code == 5 or _PYTEST_NO_TESTS_RE.search(str(output or ""))):
        return VerificationExecutionAssessment(
            real_execution=False,
            non_execution_reason="pytest_no_tests_collected",
        )
    if family == "unittest" and _UNITTEST_NO_TESTS_RE.search(str(output or "")):
        return VerificationExecutionAssessment(
            real_execution=False,
            non_execution_reason="unittest_no_tests_run",
        )
    if family in {"maven:test", "maven:verify", "gradle:test", "dotnet:test"}:
        if _JUNIT_ZERO_TESTS_RE.search(str(output or "")):
            return VerificationExecutionAssessment(
                real_execution=False,
                non_execution_reason=f"{family.replace(':', '_')}_zero_tests",
            )
    if family in {"node:test", "npm:test", "pnpm:test", "yarn:test"} and _NODE_ZERO_TESTS_RE.search(
        str(output or "")
    ):
        return VerificationExecutionAssessment(
            real_execution=False,
            non_execution_reason=f"{family.replace(':', '_')}_zero_tests",
        )
    if (
        family
        in {
            "make:test",
            "make:check",
            "make:verify",
            "just:test",
            "just:check",
            "just:verify",
        }
        and exit_code == 0
        and _NOTHING_TO_DO_RE.search(str(output or ""))
    ):
        return VerificationExecutionAssessment(
            real_execution=False,
            non_execution_reason="verification_nothing_to_do",
        )

    if exit_code != 0:
        return VerificationExecutionAssessment(real_execution=None)

    if analysis.evidentiary_capability == VerificationCommandEvidentiaryCapability.NON_ASSERTIVE:
        return VerificationExecutionAssessment(
            real_execution=False,
            non_execution_reason=analysis.capability_reason or "non_assertive_verifier",
        )

    if family == "go:test":
        saw_zero_work = False
        zero_work_reason: str | None = None
        recognized_summary = False
        for raw_line in str(output or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if _GO_TEST_NO_TESTS_TO_RUN_LINE_RE.match(line):
                recognized_summary = True
                saw_zero_work = True
                zero_work_reason = "go_test_no_tests_to_run"
                continue
            if _GO_TEST_NO_TEST_FILES_LINE_RE.match(line):
                recognized_summary = True
                saw_zero_work = True
                if zero_work_reason is None:
                    zero_work_reason = "go_test_no_test_files"
                continue
            if _GO_TEST_OK_LINE_RE.match(line):
                return VerificationExecutionAssessment(real_execution=True)
        if recognized_summary and saw_zero_work:
            return VerificationExecutionAssessment(
                real_execution=False,
                non_execution_reason=zero_work_reason or "go_test_no_tests_to_run",
            )
    if family is None:
        return VerificationExecutionAssessment(
            real_execution=None,
            non_execution_reason=analysis.inconclusive_reason
            or analysis.capability_reason
            or "unknown_verification_capability",
        )
    return VerificationExecutionAssessment(real_execution=True)


def _build_pytest_module_fallback_command(command: str) -> str | None:
    parts = _split_shell_command_parts(command)
    if not parts:
        return None

    env_prefix: list[str] = []
    idx = 0
    while idx < len(parts) and _is_env_assignment_token(parts[idx]):
        env_prefix.append(parts[idx])
        idx += 1

    if idx >= len(parts):
        return None

    entrypoint = parts[idx].strip().lower()
    if entrypoint not in _VERIFY_PYTEST_ENTRYPOINTS:
        return None

    fallback_tokens = [*env_prefix, sys.executable, "-m", "pytest", *parts[idx + 1 :]]
    return shlex.join(fallback_tokens)


def _token_has_shell_glob(token: str) -> bool:
    return any(char in token for char in "*?[")


def _expand_verification_command_globs(command: str, *, root: Path) -> str:
    parts = _split_shell_command_parts(command)
    if not parts:
        return command

    root_abs = root.resolve()
    expanded_parts: list[str] = []
    changed = False
    for token in parts:
        if (
            not _token_has_shell_glob(token)
            or _is_env_assignment_token(token)
            or token.startswith("-")
            or "://" in token
        ):
            expanded_parts.append(token)
            continue

        token_path = Path(token)
        if token_path.is_absolute() or ".." in token_path.parts:
            expanded_parts.append(token)
            continue

        matches: list[str] = []
        for match in glob.glob(os.fspath(root_abs / token), recursive=True):
            match_path = Path(match)
            if not match_path.exists():
                continue
            try:
                matches.append(match_path.resolve().relative_to(root_abs).as_posix())
            except ValueError:
                continue
        if not matches:
            expanded_parts.append(token)
            continue

        expanded_parts.extend(sorted(set(matches)))
        changed = True

    if not changed:
        return command
    if is_windows_verifier_command(command):
        return quote_windows_verifier_parts(expanded_parts)
    return shlex.join(expanded_parts)


def _is_execution_layer_failure(*, exit_code: int, output: str) -> bool:
    lowered = str(output or "").casefold()
    if exit_code in {126, 127}:
        return True
    if exit_code != 1:
        return False
    if not any(marker in lowered for marker in _VERIFY_EXECUTION_LAYER_ERROR_MARKERS):
        return False
    return (
        "/bin/sh:" in lowered
        or "not recognized as an internal or external command" in lowered
        or "cannot execute" in lowered
    )


def _is_pytest_entrypoint_import_failure(*, command: str, output: str) -> bool:
    if _build_pytest_module_fallback_command(command) is None:
        return False
    lowered = str(output or "").casefold()
    return (
        "modulenotfounderror: no module named 'pytest'" in lowered
        or 'modulenotfounderror: no module named "pytest"' in lowered
    )


def _is_toolchain_unavailable_failure(*, output: str) -> bool:
    lowered = str(output or "").casefold()
    if not lowered:
        return False
    if _TOOLCHAIN_UNAVAILABLE_RE.search(lowered):
        return True
    if _LANGUAGE_VERSION_MISMATCH_RE.search(lowered):
        return True
    if _LEGACY_PYTHON_RUNTIME_INCOMPATIBILITY_RE.search(lowered):
        return True
    return any(
        marker in lowered
        for marker in (
            "unsupported class file major version",
            "invalid source release",
            "module requires go",
            "declared in its mix.exs file it supports only elixir",
            "could not determine java version",
            "gradle version",
        )
    )


def is_toolchain_unavailable_verification_output(output: str) -> bool:
    return _is_toolchain_unavailable_failure(output=output)


CANCELLED_VERIFICATION_MESSAGE = "Verification stopped because the run was cancelled."


def _raise_if_cancelled(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise VerifyError(CANCELLED_VERIFICATION_MESSAGE)


def _run_verify_command_once(
    *,
    command: str,
    root: Path,
    runner: object | None,
    runner_build_error: str | None,
    timeout_s: float,
    cancel_check: Callable[[], bool] | None = None,
    before_command_launch: Callable[[], None] | None = None,
) -> VerificationCommandExecution:
    if runner_build_error is not None:
        return VerificationCommandExecution(
            exit_code=127,
            stdout="",
            stderr=f"verify sandbox unavailable: {runner_build_error}",
        )
    if runner is None:
        return VerificationCommandExecution(
            exit_code=127,
            stdout="",
            stderr="verify runner is missing; implicit host execution is disabled.",
        )

    # A Stop ends a check that is still running; a stopped check is no result,
    # so it raises instead of reporting a failure.
    run_kwargs: dict[str, Any] = {}
    if cancel_check is not None and runner_takes_cancel_check(runner):
        run_kwargs["cancel_check"] = cancel_check
    cwd = root.resolve()
    try:
        if before_command_launch is not None:
            before_command_launch()
    except ConfigError as exc:
        return VerificationCommandExecution(
            exit_code=126, stdout="", stderr=str(exc), admission_refused=True
        )
    try:
        cp = runner.run(root=root, cwd=cwd, cmd=command, timeout_s=timeout_s, **run_kwargs)
        return VerificationCommandExecution(
            exit_code=cp.returncode,
            stdout=cp.stdout or "",
            stderr=cp.stderr or "",
            host_launcher_unavailable=getattr(cp, "host_launcher_unavailable", False) is True,
            execution_context=getattr(cp, "execution_context", None),
        )
    except subprocess.TimeoutExpired as exc:
        return VerificationCommandExecution(
            exit_code=124,
            stdout="",
            stderr=f"Command timed out after {timeout_s:g}s"
            + ("; process cleanup pending" if getattr(exc, "cleanup_pending", False) else ""),
        )
    except CommandCancelledError as e:
        raise VerifyError(CANCELLED_VERIFICATION_MESSAGE) from e
    except OSError as e:
        return VerificationCommandExecution(exit_code=127, stdout="", stderr=str(e))
    except Exception as e:  # noqa: BLE001
        return VerificationCommandExecution(exit_code=127, stdout="", stderr=str(e))


def verify_run_result_to_payload(
    *,
    root: Path,
    result: VerifyRunResult,
    output_preview_chars: int = VERIFY_OUTPUT_PREVIEW_CHARS,
) -> dict[str, object]:
    artifact_ref = resolve_verify_artifact_payload(root=root, artifact_path=result.artifact_path)
    primary_failure = build_primary_verification_failure(
        result=result,
        output_preview_chars=output_preview_chars,
    )

    command_results: list[dict[str, object]] = []
    fallback_details: list[dict[str, object]] = []
    primary_failure_summary: dict[str, Any] | None = None
    for item in result.command_results:
        preview, was_truncated = _truncate_verify_output(
            item.output,
            max_chars=max(1, int(output_preview_chars)),
        )
        effective_command = item.effective_command or item.command
        failure_summary = None
        if not item.ok:
            failure_summary = summarize_verification_failure(
                root=root,
                command=item.command,
                effective_command=effective_command,
                output=item.output,
                output_truncated=was_truncated,
            )
            if failure_summary is not None and primary_failure_summary is None:
                primary_failure_summary = failure_summary
        command_payload = {
            "command": item.command,
            "effective_command": effective_command,
            "exit_code": item.exit_code,
            "status": item.status.value,
            "ok": item.ok,
            "real_execution": item.real_execution,
            "output_preview": preview,
            "output_chars": len(item.output),
            "output_truncated": was_truncated,
            "fallback_used": item.fallback_used,
        }
        if failure_summary is not None:
            command_payload["failure_summary"] = failure_summary
        if item.fallback_reason:
            command_payload["fallback_reason"] = item.fallback_reason
        if item.non_execution_reason:
            command_payload["non_execution_reason"] = item.non_execution_reason
        if item.host_launcher_unavailable:
            command_payload["host_launcher_unavailable"] = True
        if item.execution_context:
            command_payload["execution_context"] = item.execution_context
        if item.verification_execution_scope is not None:
            command_payload["verification_execution_scope"] = item.verification_execution_scope
        command_results.append(command_payload)
        if item.fallback_used:
            fallback_details.append(
                {
                    "command": item.command,
                    "effective_command": effective_command,
                    "exit_code": item.exit_code,
                    "status": item.status.value,
                    "ok": item.ok,
                    "reason": item.fallback_reason or "pytest_entrypoint_unavailable",
                }
            )

    payload: dict[str, object] = {
        "commands": list(result.commands),
        "command_results": command_results,
        "all_passed": result.all_passed,
        "status": result.status,
        "executed_count": result.executed_count,
        "skipped_count": result.skipped_count,
        "failed_commands": list(result.failed_commands),
        "summary": result.summary,
        "failure_category": result.failure_category_value,
        "artifact_path": artifact_ref.artifact_path,
        "artifact_saved": artifact_ref.artifact_saved,
        "artifact_readable_via_fs": artifact_ref.artifact_readable_via_fs,
        "artifact_location": artifact_ref.artifact_location,
        "fallback_used": bool(fallback_details),
        "fallback_count": len(fallback_details),
        "fallback_details": fallback_details,
    }
    if primary_failure is not None:
        payload["primary_failure"] = primary_failure
    if primary_failure_summary is not None:
        payload["failure_summary"] = primary_failure_summary
    return payload


def compact_verification_payload(
    payload: dict[str, object] | None,
    *,
    max_command_results: int = 8,
    output_preview_chars: int = 240,
) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None

    max_results = max(1, int(max_command_results))
    max_preview_chars = max(1, int(output_preview_chars))
    raw_results = payload.get("command_results")
    compact_results: list[dict[str, Any]] = []
    total_results = 0
    if isinstance(raw_results, list):
        total_results = len(raw_results)
        for item in raw_results[:max_results]:
            if not isinstance(item, dict):
                continue
            preview_text = str(item.get("output_preview") or "")
            preview, preview_truncated = _truncate_verify_output(
                preview_text,
                max_chars=max_preview_chars,
            )
            compact_item: dict[str, Any] = {
                "command": str(item.get("command") or ""),
                "effective_command": str(
                    item.get("effective_command") or item.get("command") or ""
                ),
                "exit_code": item.get("exit_code"),
                "ok": item.get("ok"),
                "real_execution": item.get("real_execution"),
                "fallback_used": bool(item.get("fallback_used", False)),
                "output_preview": preview,
            }
            if item.get("fallback_reason") is not None:
                compact_item["fallback_reason"] = str(item.get("fallback_reason") or "")
            if item.get("non_execution_reason") is not None:
                compact_item["non_execution_reason"] = str(item.get("non_execution_reason") or "")
            if item.get("host_launcher_unavailable") is True:
                compact_item["host_launcher_unavailable"] = True
            raw_failure_summary = item.get("failure_summary")
            if isinstance(raw_failure_summary, dict):
                compact_summary = _compact_failure_summary(raw_failure_summary)
                if compact_summary:
                    compact_item["failure_summary"] = compact_summary
            if preview_truncated or bool(item.get("output_truncated")):
                compact_item["output_truncated"] = True
            compact_results.append(compact_item)

    compact_payload: dict[str, object] = {
        "summary": str(payload.get("summary") or ""),
        "all_passed": payload.get("all_passed"),
        "failed_commands": list(payload.get("failed_commands") or []),
        "failure_category": payload.get("failure_category"),
        "command_results": compact_results,
        "command_results_total": total_results,
        "command_results_truncated": total_results > len(compact_results),
        "fallback_used": bool(payload.get("fallback_used", False)),
        "fallback_count": int(payload.get("fallback_count") or 0),
    }
    raw_primary_failure = payload.get("primary_failure")
    if isinstance(raw_primary_failure, dict):
        compact_primary_failure: dict[str, object] = {}
        command = str(raw_primary_failure.get("command") or "").strip()
        effective_command = str(raw_primary_failure.get("effective_command") or "").strip()
        snippet = _extract_failure_snippet_line(
            str(raw_primary_failure.get("snippet") or ""),
            max_chars=max_preview_chars,
            allow_fallback=True,
        )
        if command:
            compact_primary_failure["command"] = command
        if effective_command:
            compact_primary_failure["effective_command"] = effective_command
        if snippet:
            compact_primary_failure["snippet"] = snippet
        if raw_primary_failure.get("output_truncated") is not None:
            compact_primary_failure["output_truncated"] = bool(
                raw_primary_failure.get("output_truncated")
            )
        if raw_primary_failure.get("fallback_used") is not None:
            compact_primary_failure["fallback_used"] = bool(
                raw_primary_failure.get("fallback_used")
            )
        if compact_primary_failure:
            compact_payload["primary_failure"] = compact_primary_failure
    if payload.get("artifact_path") is not None:
        compact_payload["artifact_path"] = payload.get("artifact_path")
    if payload.get("artifact_saved") is not None:
        compact_payload["artifact_saved"] = bool(payload.get("artifact_saved"))
    if payload.get("artifact_location") is not None:
        compact_payload["artifact_location"] = str(payload.get("artifact_location") or "")
    raw_failure_summary = payload.get("failure_summary")
    if isinstance(raw_failure_summary, dict):
        compact_summary = _compact_failure_summary(raw_failure_summary)
        if compact_summary:
            compact_payload["failure_summary"] = compact_summary
    return compact_payload


def _compact_failure_summary(summary: dict[str, Any]) -> dict[str, object]:
    compact: dict[str, object] = {}
    framework = str(summary.get("framework") or "").strip()
    primary_error = str(summary.get("primary_error") or "").strip()
    if framework:
        compact["framework"] = framework
    if primary_error:
        compact["primary_error"] = _clip_verification_failure_snippet(
            primary_error,
            max_chars=VERIFICATION_FAILURE_SNIPPET_MAX_CHARS,
        )
    for key, limit in (
        ("failing_tests", 4),
        ("stack_frames", 6),
        ("likely_next_files", 8),
    ):
        value = summary.get(key)
        if isinstance(value, list) and value:
            compact[key] = value[:limit]
    if summary.get("output_truncated") is not None:
        compact["output_truncated"] = bool(summary.get("output_truncated"))
    if summary.get("heuristic") is not None:
        compact["heuristic"] = bool(summary.get("heuristic"))
    if summary.get("confidence") is not None:
        compact["confidence"] = summary.get("confidence")
    return compact


def resolve_verify_artifact_payload(*, root: Path, artifact_path: Path) -> VerifyArtifactPayload:
    if not artifact_path.exists():
        return VerifyArtifactPayload(
            artifact_path=None,
            artifact_saved=False,
            artifact_readable_via_fs=False,
            artifact_location="missing",
        )

    root_abs = root.resolve()
    artifact_abs = artifact_path.resolve()
    try:
        rel_path = artifact_abs.relative_to(root_abs).as_posix()
    except ValueError:
        return VerifyArtifactPayload(
            artifact_path=None,
            artifact_saved=True,
            artifact_readable_via_fs=False,
            artifact_location="external_session_store",
        )

    return VerifyArtifactPayload(
        artifact_path=rel_path,
        artifact_saved=True,
        artifact_readable_via_fs=True,
        artifact_location="workspace_root",
    )


def normalize_verify_mode(mode: str) -> str:
    value = mode.strip().lower()
    if value not in VERIFY_MODES:
        raise VerifyError("Invalid --verify. Use one of: off, warn, strict.")
    return value


def resolve_verify_command_selection(
    *,
    cfg: AppConfig,
    verify_cmd: list[str] | None,
    root: Path | None = None,
    repo_scan: RepoScanResult | None = None,
    allow_empty_config: bool = False,
) -> ResolvedVerifyCommands:
    if verify_cmd:
        commands = normalize_verify_command_list(verify_cmd)
        if not commands:
            raise VerifyError("--verify-cmd values cannot be empty.")
        return _resolved_verify_commands(commands=commands, source="cli.verify_cmd")

    commands = normalize_verify_command_list(cfg.verify_commands)
    if not commands:
        managed_host_verifier_unavailable = bool(
            (cfg.extra_fields or {}).get("managed_host_verifier_unavailable")
        )
        if not allow_empty_config and not managed_host_verifier_unavailable:
            raise VerifyError("Configured verify_commands is empty.")
        inferred = _resolve_repo_inferred_verify_commands(root=root, repo_scan=repo_scan)
        if inferred:
            return _resolved_verify_commands(
                commands=inferred,
                source="repo_scan.likely_test_commands",
                reason=(
                    "managed host verifier is unavailable; repo scan suggested "
                    "verification commands from the existing workspace"
                    if managed_host_verifier_unavailable
                    else (
                        "configured verify_commands is empty; repo scan suggested "
                        "verification commands from the existing workspace"
                    )
                ),
            )
        return _resolved_verify_commands(
            commands=(),
            source=(
                "environment.managed_host_verifier_unavailable"
                if managed_host_verifier_unavailable
                else REPO_SCAN_NO_AUTHORITATIVE_SOURCE
            ),
            reason=(
                "managed host verifier is unavailable and no repo-native verification "
                "commands were discovered"
                if managed_host_verifier_unavailable
                else (
                    "configured verify_commands is empty and no repo-native verification "
                    "commands were discovered"
                )
            ),
            contract_type="unavailable",
        )
    generic_config_preset = is_generic_configured_verify_preset(commands)
    if commands and not generic_config_preset:
        return _resolved_verify_commands(commands=commands, source="config.verify_commands")

    inferred = _resolve_repo_inferred_verify_commands(root=root, repo_scan=repo_scan)
    if inferred:
        return _resolved_verify_commands(commands=inferred, source="repo_scan.likely_test_commands")

    verify_commands_explicitly_set = "verify_commands" in getattr(cfg, "model_fields_set", set())
    source = (
        CONFIG_VERIFY_COMMANDS_FALLBACK_SOURCE
        if is_generic_verify_command_fallback(commands) and not verify_commands_explicitly_set
        else CONFIG_VERIFY_COMMANDS_GENERIC_PRESET_SOURCE
    )
    return _resolved_verify_commands(commands=commands, source=source)


def resolve_verify_commands(
    *,
    cfg: AppConfig,
    verify_cmd: list[str] | None,
    root: Path | None = None,
    repo_scan: RepoScanResult | None = None,
) -> list[str]:
    return list(
        resolve_verify_command_selection(
            cfg=cfg,
            verify_cmd=verify_cmd,
            root=root,
            repo_scan=repo_scan,
        ).commands
    )


@dataclass(frozen=True)
class VerificationSelectionRepair:
    selection: ResolvedVerifyCommands
    dropped_commands: tuple[str, ...] = tuple()
    warning: str = ""


def repair_invalid_verify_command_selection(
    selection: ResolvedVerifyCommands,
    *,
    root: Path | None = None,
    repo_scan: RepoScanResult | None = None,
) -> VerificationSelectionRepair:
    """Make an unusable verification selection survivable instead of fatal.

    Selecting a verification command is an optimization over the work the agent
    is about to do, not a precondition for it: a session that cannot name an
    authoritative command must still run the task and report honestly on what it
    could verify. That applies to a command Alysis Code could not *classify*. A
    command Alysis Code recognized and *refused* -- a vacuous verifier, a masked
    failure, an unsafe pipeline -- is a different thing entirely: running it
    would manufacture passing evidence, so it still fails fast rather than
    quietly becoming the session's contract.

    Among survivable commands, explicitly stated ones (managed host,
    ``--verify-cmd``, configured ``verify_commands``) are kept as-is with a
    warning -- Alysis Code does not substitute its own guess for what somebody
    asked for. Commands Alysis Code inferred itself are dropped, detection falls
    through to whatever runner the workspace exposes, and an empty result
    degrades to a best-effort contract rather than an error.
    """
    errors = validation_errors_for_selection(selection)
    if not errors:
        return VerificationSelectionRepair(selection=selection)

    specs = verification_command_specs_for_selection(selection)
    # Scoped to authoritative selections exactly as the original guard was: this
    # change narrows what is fatal (by reason), it must never widen it.
    if is_authoritative_verify_command_selection(selection):
        refused = [
            f"{spec.original_text}: {spec.rejection_reason or 'invalid_command'}"
            for spec in specs
            if spec.validation_status == VerificationCommandValidationStatus.INVALID
            and not rejection_reason_is_unclassifiable(spec.rejection_reason)
        ]
        if refused:
            raise VerifyError(
                "authoritative verification command is invalid: " + "; ".join(refused[:3])
            )

    invalid = tuple(
        spec.original_text
        for spec in specs
        if spec.validation_status == VerificationCommandValidationStatus.INVALID
    )
    detail = "; ".join(errors[:3])

    if selection.source in EXPLICIT_VERIFY_COMMAND_SOURCES:
        return VerificationSelectionRepair(
            selection=selection,
            warning=(
                "verification command was explicitly requested but cannot be treated as "
                f"authoritative evidence ({detail}); keeping it and continuing"
            ),
        )

    remaining = tuple(command for command in selection.commands if command not in invalid)
    if remaining:
        return VerificationSelectionRepair(
            selection=_resolved_verify_commands(
                commands=remaining,
                source=selection.source,
                reason=_appended_reason(selection.reason, f"dropped unusable command(s): {detail}"),
                contract_type=selection.contract_type,
            ),
            dropped_commands=invalid,
            warning=f"ignoring unusable verification command(s): {detail}",
        )

    detected = _detect_fallback_verify_commands(root=root, repo_scan=repo_scan)
    if detected:
        return VerificationSelectionRepair(
            selection=_resolved_verify_commands(
                commands=detected,
                source=VERIFICATION_FALLBACK_DETECTED_SOURCE,
                contract_type="selected",
            ),
            dropped_commands=invalid,
            warning=(
                f"ignoring unusable verification command(s): {detail}; "
                f"using detected command(s) instead: {', '.join(detected)}"
            ),
        )

    return VerificationSelectionRepair(
        selection=_resolved_verify_commands(
            commands=(),
            source=VERIFICATION_FALLBACK_BEST_EFFORT_SOURCE,
            contract_type="unavailable",
            best_effort=True,
        ),
        dropped_commands=invalid,
        warning=(
            f"ignoring unusable verification command(s): {detail}; no verification command was "
            "detected, continuing with best-effort verification"
        ),
    )


def _detect_fallback_verify_commands(
    *,
    root: Path | None,
    repo_scan: RepoScanResult | None,
) -> tuple[str, ...]:
    """Detection chain consulted after a selected command turns out unusable.

    Declared pytest config and ``unittest`` discovery come first: they answer
    "what does this workspace say it runs" for exactly the old-style layouts the
    primary inference stays silent about. Anything else Alysis Code already knows
    how to recognize (make, just, npm, maven, go, cargo, a pytest test layout)
    comes from the repo scan behind them.
    """
    layers: tuple[Callable[[], tuple[str, ...] | list[str]], ...] = (
        (lambda: detect_fallback_test_commands(root=root)) if root is not None else (lambda: []),
        lambda: _resolve_repo_inferred_verify_commands(root=root, repo_scan=repo_scan),
    )
    # Evaluated lazily: the repo-scan layer can trigger a full rescan of the
    # workspace, which must not happen once an earlier layer has already answered.
    for layer in layers:
        detected = _valid_verify_commands(
            layer(),
            source=VERIFICATION_FALLBACK_DETECTED_SOURCE,
            contract_type="selected",
        )
        if detected:
            return detected
    return ()


def _appended_reason(reason: str, note: str) -> str:
    base = str(reason or "").strip()
    return f"{base}; {note}" if base else note


def _valid_verify_commands(
    commands: tuple[str, ...] | list[str],
    *,
    source: str,
    contract_type: str,
) -> tuple[str, ...]:
    return tuple(
        spec.original_text
        for spec in build_verification_command_specs(
            commands,
            source=source,
            contract_type=contract_type,
        )
        if spec.validation_status == VerificationCommandValidationStatus.VALID
    )


def _repo_scan_describes_root(scan: RepoScanResult, root: Path) -> bool:
    raw_scan_root = str(getattr(scan, "workspace_root", "") or "").strip()
    if not raw_scan_root:
        return False
    try:
        scan_root = Path(raw_scan_root).expanduser().resolve()
        target_root = root.resolve()
    except OSError:
        return False
    return os.path.normcase(str(scan_root)) == os.path.normcase(str(target_root))


def _resolve_repo_inferred_verify_commands(
    *,
    root: Path | None,
    repo_scan: RepoScanResult | None,
) -> tuple[str, ...]:
    # A scan of the requested root is authoritative as-is: files created after it
    # was captured must not retroactively establish a verification contract. A scan
    # of a different root (e.g. the base workspace while verifying a merge-candidate
    # copy) is stale for command inference, so rescan the candidate root while
    # preserving the original scan's focus.
    scan = repo_scan
    if root is not None and (scan is None or not _repo_scan_describes_root(scan, root)):
        try:
            scan = scan_workspace(context=_resolve_verify_workspace_context(root, repo_scan))
        except (WorkspaceContextError, OSError):
            scan = repo_scan
    if scan is None:
        return ()
    # A scan may predate this filter (persisted scans are replayed verbatim), so
    # docs-doctest commands are rejected here as well as at inference time.
    likely_commands = tuple(
        command
        for command in normalize_verify_command_list(scan.likely_test_commands)
        if not command_is_docs_doctest(command)
    )
    if likely_commands:
        return likely_commands
    return ()


def _infer_node_project_script_verify_commands(
    *,
    root: Path,
    repo_scan: RepoScanResult,
) -> tuple[str, ...]:
    package_json_paths = [
        str(item.get("path") or "").strip()
        for item in repo_scan.manifests
        if PurePosixPath(str(item.get("path") or "")).name == "package.json"
    ]
    commands: list[str] = []
    for package_json_path in sorted(package_json_paths, key=_node_package_manifest_sort_key):
        scripts = _read_package_json_scripts(root / package_json_path)
        if not scripts:
            continue
        manager = _node_package_manager_for_manifest(
            root=root,
            package_json_path=package_json_path,
            repo_scan=repo_scan,
        )
        selected_scripts = _select_node_verify_scripts(scripts)
        for script_name in selected_scripts:
            commands.append(
                _node_package_script_command(
                    manager=manager,
                    package_json_path=package_json_path,
                    script_name=script_name,
                )
            )
        if commands:
            break
    return tuple(commands)


def _node_package_manifest_sort_key(path: str) -> tuple[int, int, str]:
    pure = PurePosixPath(str(path or "."))
    return (0 if pure.parent.as_posix() in {"", "."} else 1, len(pure.parts), str(path))


def _read_package_json_scripts(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        return {}
    return {
        str(name).strip(): str(command).strip()
        for name, command in scripts.items()
        if str(name).strip() and isinstance(command, str) and str(command).strip()
    }


def _select_node_verify_scripts(scripts: dict[str, str]) -> tuple[str, ...]:
    selected: list[str] = []
    for script_name in _NODE_VERIFY_SCRIPT_PRIORITY:
        command = scripts.get(script_name)
        if not command:
            continue
        if script_name == "test" and "no test specified" in command.casefold():
            continue
        selected.append(script_name)
        if script_name == "test":
            return tuple(selected)
    return tuple(selected)


def _node_package_manager_for_manifest(
    *,
    root: Path,
    package_json_path: str,
    repo_scan: RepoScanResult,
) -> str:
    try:
        payload = json.loads((root / package_json_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        payload = None
    if isinstance(payload, dict):
        raw_manager = payload.get("packageManager")
        if isinstance(raw_manager, str):
            lowered = raw_manager.strip().casefold()
            for manager in ("pnpm", "yarn", "bun", "npm"):
                if lowered.startswith(f"{manager}@"):
                    return manager
    manifest_paths = {str(item.get("path") or "") for item in repo_scan.manifests}
    package_dir = PurePosixPath(package_json_path).parent
    for candidate_dir in (package_dir, *package_dir.parents):
        prefix = "" if candidate_dir.as_posix() in {"", "."} else f"{candidate_dir.as_posix()}/"
        if f"{prefix}pnpm-lock.yaml" in manifest_paths:
            return "pnpm"
        if f"{prefix}yarn.lock" in manifest_paths:
            return "yarn"
        if f"{prefix}bun.lockb" in manifest_paths:
            return "bun"
        if f"{prefix}package-lock.json" in manifest_paths:
            return "npm"
    return "npm"


def _node_package_script_command(
    *,
    manager: str,
    package_json_path: str,
    script_name: str,
) -> str:
    package_dir = PurePosixPath(package_json_path).parent.as_posix()
    verb = "test" if script_name == "test" else f"run {script_name}"
    if package_dir in {"", "."}:
        return f"{manager} {verb}"
    quoted_dir = shlex.quote(package_dir)
    if manager == "pnpm":
        return f"pnpm --dir {quoted_dir} {verb}"
    if manager == "yarn":
        return f"yarn --cwd {quoted_dir} {verb}"
    if manager == "bun":
        return f"bun --cwd {quoted_dir} {verb}"
    return f"npm --prefix {quoted_dir} {verb}"


def _resolve_verify_workspace_context(
    root: Path, repo_scan: RepoScanResult | None
) -> WorkspaceContext:
    root = root.resolve()
    focus_relpath = str(getattr(repo_scan, "focus_relpath", "") or ".").strip()
    if focus_relpath and focus_relpath not in {".", ""}:
        focus_path = (root / focus_relpath).resolve()
        try:
            focus_path.relative_to(root)
        except ValueError:
            focus_path = root
        if focus_path.exists():
            return _workspace_context_for_root_focus(root=root, focus_path=focus_path)
    inferred_plain_focus = _plain_scan_focus_path_from_candidate_root(root, repo_scan)
    if inferred_plain_focus is not None:
        return _workspace_context_for_root_focus(root=root, focus_path=inferred_plain_focus)
    return resolve_workspace_context(root)


def _workspace_context_for_root_focus(*, root: Path, focus_path: Path) -> WorkspaceContext:
    root_context = resolve_workspace_context(root)
    focus_path = focus_path.resolve()
    if focus_path == root_context.workspace_root:
        return root_context
    if root_context.git_root is not None:
        return resolve_workspace_context(focus_path)
    focus_relpath = focus_path.relative_to(root_context.workspace_root).as_posix()
    return WorkspaceContext(
        input_path=focus_path,
        focus_path=focus_path,
        workspace_root=root_context.workspace_root,
        git_root=root_context.git_root,
        focus_relpath=focus_relpath,
        workspace_kind=root_context.workspace_kind,
        has_head_commit=root_context.has_head_commit,
        current_branch=root_context.current_branch,
    )


def _plain_scan_focus_path_from_candidate_root(
    root: Path,
    repo_scan: RepoScanResult | None,
) -> Path | None:
    if repo_scan is None:
        return None
    if str(repo_scan.focus_relpath or ".").strip() not in {"", "."}:
        return None
    previous_root = Path(str(repo_scan.workspace_root or "")).expanduser()
    focus_name = previous_root.name
    if not focus_name or focus_name == root.name:
        return None
    candidate = (root / focus_name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_dir():
        return None
    signal_paths = [
        item.get("path", "")
        for item in [*repo_scan.manifests, *({"path": path} for path in repo_scan.readme_paths)]
    ]
    signal_paths.extend(repo_scan.observed_paths)
    for raw_path in signal_paths:
        rel_path = str(raw_path or "").strip()
        if rel_path and (candidate / rel_path).exists():
            return candidate
    return None


def _parse_verify_sandbox_mode(
    raw: object,
    *,
    field_name: str,
    default: str,
) -> str:
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in VERIFY_SANDBOX_MODES:
        return value
    opts = ", ".join(sorted(VERIFY_SANDBOX_MODES))
    raise VerifyError(f"Invalid {field_name}: {raw!r}. Expected one of: {opts}.")


def resolve_verify_sandbox_mode(cfg: AppConfig) -> str:
    mode = "strict"
    raw_cfg = cfg.extra_fields.get("verify_sandbox")
    if raw_cfg is not None and not isinstance(raw_cfg, dict):
        raise VerifyError("Invalid verify_sandbox config: expected object.")
    cfg_map = raw_cfg if isinstance(raw_cfg, dict) else {}
    mode = _parse_verify_sandbox_mode(
        cfg_map.get("mode"),
        field_name="verify_sandbox.mode",
        default=mode,
    )
    mode = _parse_verify_sandbox_mode(
        env_get("ALYSIS_VERIFY_SANDBOX_MODE"),
        field_name="ALYSIS_VERIFY_SANDBOX_MODE",
        default=mode,
    )
    return mode


@dataclass(frozen=True)
class _PathSnapshot:
    existed: bool
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    snapshot_path: Path | None = None


def _snapshot_path(path: Path) -> _PathSnapshot:
    if not path.exists():
        return _PathSnapshot(existed=False)
    temp_dir = tempfile.TemporaryDirectory(prefix="alysis-verify-snapshot-")
    snapshot_path = Path(temp_dir.name) / path.name
    if path.is_dir():
        shutil.copytree(path, snapshot_path)
    else:
        shutil.copy2(path, snapshot_path)
    return _PathSnapshot(existed=True, temp_dir=temp_dir, snapshot_path=snapshot_path)


def _restore_path_snapshot(path: Path, snapshot: _PathSnapshot) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
        if snapshot.existed and snapshot.snapshot_path is not None:
            if snapshot.snapshot_path.is_dir():
                shutil.copytree(snapshot.snapshot_path, path)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(snapshot.snapshot_path, path)
    finally:
        if snapshot.temp_dir is not None:
            snapshot.temp_dir.cleanup()


def run_task_verification(
    *,
    root: Path,
    commands: list[str],
    artifact_path: Path,
    cfg: AppConfig | None = None,
    timeout_s: float = 900,
    process_group_registry: ProcessGroupRegistry | None = None,
    cancel_check: Callable[[], bool] | None = None,
    before_command_launch: Callable[[], None] | None = None,
) -> VerifyRunResult:
    from .agent.scoped_access import ExecutionDirectoryIdentity

    execution_directory = ExecutionDirectoryIdentity.capture(str(root.resolve()))

    def check_launch_directory() -> None:
        # The caller retains the approval-bound original identity. This local
        # identity additionally binds standalone callers and staged consumers.
        execution_directory.check()
        if before_command_launch is not None:
            before_command_launch()

    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    results: list[VerifyCommandResult] = []
    lines: list[str] = [
        "# Verification Output",
        f"root: {os.fspath(root.resolve())}",
        "",
    ]
    effective_cfg = cfg or AppConfig(model="")
    verify_sandbox_mode = resolve_verify_sandbox_mode(effective_cfg)
    # The verifier's own commands are tracked too: a verify_run that hits its
    # timeout orphans workers exactly the way an agent-started test run does.
    # Verification is automated: its commands run with stdin closed so an
    # interactive prompt fails immediately and is reported, instead of waiting
    # on a terminal no one is reading until the per-command timeout expires.
    runner = (
        HostShellRunner(
            process_group_registry=process_group_registry,
            close_stdin=True,
        )
        if verify_sandbox_mode == "off"
        else None
    )
    runner_build_error: str | None = None
    failure_category: FailureCategory | None = None
    pytest_cache_path = root / ".pytest_cache"
    pytest_cache_snapshot = _snapshot_path(pytest_cache_path)
    try:
        if verify_sandbox_mode != "off":
            try:
                base_settings = resolve_shell_sandbox_settings(effective_cfg)
                verify_settings = replace(base_settings, mode=verify_sandbox_mode)
                runner = with_closed_stdin(
                    build_shell_runner_from_settings(
                        verify_settings,
                        root,
                        warning_callback=None,
                        process_group_registry=process_group_registry,
                    )
                )
            except (ConfigError, VerifyError) as e:
                runner_build_error = str(e)
                failure_category = FailureCategory.INFRA_UNAVAILABLE

        for idx, command in enumerate(commands, start=1):
            _raise_if_cancelled(cancel_check)
            effective_command = _expand_verification_command_globs(command, root=root)
            initial_execution = _run_verify_command_once(
                command=effective_command,
                root=root,
                runner=runner,
                runner_build_error=runner_build_error,
                timeout_s=timeout_s,
                cancel_check=cancel_check,
                before_command_launch=check_launch_directory,
            )
            exit_code = initial_execution.exit_code
            stdout = initial_execution.stdout
            stderr = initial_execution.stderr
            output = initial_execution.output
            final_execution = initial_execution
            fallback_used = False
            fallback_reason: str | None = None

            fallback_command = _build_pytest_module_fallback_command(command)
            if (
                runner_build_error is None
                and not initial_execution.admission_refused
                and fallback_command
                and (
                    _is_execution_layer_failure(
                        exit_code=initial_execution.exit_code,
                        output=output,
                    )
                    or _is_pytest_entrypoint_import_failure(command=command, output=output)
                )
            ):
                fallback_used = True
                fallback_reason = "pytest_entrypoint_unavailable"
                effective_command = fallback_command
                fallback_execution = _run_verify_command_once(
                    command=fallback_command,
                    root=root,
                    runner=runner,
                    runner_build_error=runner_build_error,
                    timeout_s=timeout_s,
                    cancel_check=cancel_check,
                    before_command_launch=check_launch_directory,
                )
                exit_code = fallback_execution.exit_code
                stdout = fallback_execution.stdout
                stderr = fallback_execution.stderr
                output = fallback_execution.output
                final_execution = fallback_execution

            execution_assessment = assess_verification_command_execution(
                command=effective_command,
                exit_code=exit_code,
                output=output,
            )
            if final_execution.host_launcher_unavailable:
                execution_assessment = VerificationExecutionAssessment(
                    real_execution=False, non_execution_reason="host_launcher_unavailable"
                )
            if final_execution.admission_refused:
                execution_assessment = VerificationExecutionAssessment(
                    real_execution=False, non_execution_reason="execution_directory_changed"
                )
            result = VerifyCommandResult(
                command=command,
                effective_command=effective_command,
                exit_code=exit_code,
                output=output,
                stdout=stdout,
                stderr=stderr,
                fallback_used=fallback_used,
                fallback_reason=fallback_reason,
                real_execution=execution_assessment.real_execution,
                non_execution_reason=execution_assessment.non_execution_reason,
                host_launcher_unavailable=final_execution.host_launcher_unavailable,
                execution_context=final_execution.execution_context,
                verification_execution_scope=execution_scope_fingerprint(runner),
            )
            results.append(result)
            if failure_category is None and (
                result.non_execution_reason
                in {"execution_layer_failure", "host_launcher_unavailable"}
                or is_infra_unavailable_error(result.output)
                or _is_toolchain_unavailable_failure(output=result.output)
            ):
                failure_category = FailureCategory.INFRA_UNAVAILABLE

            lines.extend(
                [
                    f"## Command {idx}",
                    f"requested_command: {command}",
                    f"effective_command: {effective_command}",
                    f"fallback_used: {str(fallback_used).lower()}",
                ]
            )
            if fallback_reason:
                lines.append(f"fallback_reason: {fallback_reason}")
                lines.extend(
                    [
                        "----- initial output -----",
                        initial_execution.output.rstrip() or "(no output)",
                    ]
                )
            if result.non_execution_reason:
                lines.append(f"non_execution_reason: {result.non_execution_reason}")
            lines.extend(
                [
                    f"exit_code: {exit_code}",
                    f"status: {result.status.value}",
                    f"real_execution: {str(result.real_execution).lower() if result.real_execution is not None else 'unknown'}",
                    "----- output -----",
                    output.rstrip() or "(no output)",
                    "",
                ]
            )
            if final_execution.admission_refused:
                # Retain earlier results and this refusal; never send another
                # original/fallback command after observing identity loss.
                break

        run_result = VerifyRunResult(
            commands=commands,
            command_results=results,
            artifact_path=artifact_path,
            failure_category=(
                failure_category
                if failure_category is not None or all(item.ok for item in results)
                else FailureCategory.VERIFICATION_FAILED
            ),
        )
        lines.extend(["Summary:", run_result.summary, ""])
        artifact_path.write_text("\n".join(lines), encoding="utf-8")
        return run_result
    finally:
        try:
            execution_directory.check()
        except ConfigError:
            # Do not restore the old cache over a replacement project.
            if pytest_cache_snapshot.temp_dir is not None:
                pytest_cache_snapshot.temp_dir.cleanup()
        else:
            _restore_path_snapshot(pytest_cache_path, pytest_cache_snapshot)
