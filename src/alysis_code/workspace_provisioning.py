"""Pre-provisioning of a workspace's *declared* test runner.

Most autonomous sessions in a Python repo start the same way: run the tests,
watch them fail with ``No module named pytest``, install pytest, run them
again. The gap is knowable before the first command -- the repo declares its
runner in a config file -- so discovering it by failing a test run is pure
waste, repeated once per session.

This module closes exactly that gap and nothing wider:

* Detection is **config-file evidence only**. A repo declares pytest in
  ``pytest.ini``, ``pyproject.toml`` (``[tool.pytest.ini_options]``),
  ``tox.ini`` (``[pytest]``), or ``setup.cfg`` (``[tool:pytest]``). Source
  trees are never scanned, and the presence of ``tests/`` or a ``test_*.py``
  file is *not* evidence -- those say tests exist, not which runner the repo
  declared.
* The only package that can ever be installed is the declared runner, and only
  when it is not importable. An importable runner is never upgraded or
  reinstalled, and no other package is ever touched.
* Both the "is it importable" probe and the install run **through the session's
  own shell execution path**, never through the agent's interpreter. That is the
  environment the agent's test command will actually resolve, so it is the only
  environment whose answer means anything -- and routing through it also keeps
  the operator's shell-sandbox policy (network, filesystem) binding on an action
  the agent would otherwise have taken itself. A session with the shell disabled
  provisions nothing.
* Installation happens only in a **top-level autonomous run**, is attempted
  **once** per process, and is never retried after a failure. Interactive runs
  and nested sessions (subagents, conflict resolvers) never
  install: an interactive run reports the gap and leaves the call to the agent
  or the user, and a nested run inherits whatever its parent already decided.

The decision is a pure function over facts, so the policy is testable without a
filesystem, a subprocess, or an LLM.
"""

from __future__ import annotations

import shlex
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .branding import env_get
from .runtime_kind import RuntimeKind, normalize_runtime_kind

PYTEST_PACKAGE = "pytest"
PROVISION_TIMEOUT_SECONDS = 60.0
IMPORT_PROBE_TIMEOUT_SECONDS = 20.0
# Chosen to collide with nothing a shell or interpreter produces on its own:
# 1/2 are generic errors, 125/126/127 are container and exec failures.
PACKAGE_MISSING_EXIT_CODE = 3
_MAX_CONFIG_BYTES = 1_000_000

# Runtime kinds that are a top-level autonomous execution run. Mirrors the
# non-interactive predicate already used by the deadline logic in agent_loop and
# the turn loop; nested kinds are deliberately absent (see the module docstring).
AUTONOMOUS_PROVISIONING_RUNTIME_KINDS = frozenset(
    {RuntimeKind.ONE_SHOT, RuntimeKind.FORGE_EXEC, RuntimeKind.SWARM_WORKER}
)

_attempt_lock = threading.Lock()
_attempted: set[str] = set()


def _workspace_provisioning_enabled(cfg: Any | None) -> bool:
    """Kill-switch for workspace test-runner pre-provisioning (step 5).

    ``ALYSIS_WORKSPACE_PROVISIONING`` (off/0/false/no/disabled) wins over the
    config value; default is on. When off, nothing is detected, installed, or
    reported -- fully legacy behaviour.
    """
    env_value = env_get("ALYSIS_WORKSPACE_PROVISIONING")
    if env_value is not None:
        normalized = str(env_value).strip().lower()
        if normalized in {"off", "0", "false", "no", "disabled"}:
            return False
        if normalized in {"on", "1", "true", "yes", "enabled"}:
            return True
    return bool(getattr(cfg, "workspace_provisioning_enabled", True))


# ---------------------------------------------------------------------------
# Detection (config files only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeclaredTestRunner:
    package: str
    trigger_config_file: str
    evidence: str


def _read_bounded(path: Path) -> str | None:
    """Read at most ``_MAX_CONFIG_BYTES`` of a repo config file, or ``None``.

    Bounded for every format, TOML included: this runs during session startup on
    a file the repo controls, and startup must not be held hostage to its size.
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_CONFIG_BYTES)
    except (OSError, MemoryError):
        return None
    return raw.decode("utf-8", errors="replace")


def ini_has_section(text: str, section: str) -> bool:
    """True when an INI document contains a ``[section]`` header.

    A hand-rolled header scan rather than :mod:`configparser`: the file belongs
    to the user's repo and may be invalid, use duplicate keys, or use syntax
    configparser rejects. Only the header line matters here, and a malformed
    file must degrade to "not declared" rather than raise.

    Indented lines are skipped, not stripped: in INI an indented line continues
    the previous value, so ``[pytest]`` inside a multi-line value is text, not a
    section -- reading it as a declaration would invent one the repo never made.
    """
    target = f"[{section}]"
    for raw_line in str(text or "").splitlines():
        if raw_line[:1] in {" ", "\t"}:
            continue
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if not line.startswith("["):
            continue
        end = line.find("]")
        if end == -1:
            continue
        if line[: end + 1] == target:
            return True
    return False


def _pyproject_declares_pytest(path: Path) -> bool:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - stdlib on every supported version
        return False
    raw = _read_bounded(path)
    if raw is None:
        return False
    try:
        payload = tomllib.loads(raw)
    except (tomllib.TOMLDecodeError, ValueError):
        return False
    tool = payload.get("tool")
    if not isinstance(tool, dict):
        return False
    pytest_table = tool.get("pytest")
    if not isinstance(pytest_table, dict):
        return False
    return isinstance(pytest_table.get("ini_options"), dict)


def detect_declared_test_runner(root: Path) -> DeclaredTestRunner | None:
    """Return the runner the repo declares in config, or ``None``.

    Files are checked in pytest's own configuration precedence order, so the
    reported trigger is the file pytest would actually honour.
    """
    workspace_root = Path(root)

    pytest_ini = workspace_root / "pytest.ini"
    if pytest_ini.is_file():
        # pytest.ini exists only to configure pytest; its presence is the
        # declaration, and it is valid for the file to be empty.
        return DeclaredTestRunner(
            package=PYTEST_PACKAGE,
            trigger_config_file="pytest.ini",
            evidence="pytest_ini_present",
        )

    pyproject = workspace_root / "pyproject.toml"
    if pyproject.is_file() and _pyproject_declares_pytest(pyproject):
        return DeclaredTestRunner(
            package=PYTEST_PACKAGE,
            trigger_config_file="pyproject.toml",
            evidence="tool.pytest.ini_options",
        )

    tox_ini = workspace_root / "tox.ini"
    if tox_ini.is_file():
        text = _read_bounded(tox_ini)
        if text is not None and ini_has_section(text, "pytest"):
            return DeclaredTestRunner(
                package=PYTEST_PACKAGE,
                trigger_config_file="tox.ini",
                evidence="[pytest]",
            )

    setup_cfg = workspace_root / "setup.cfg"
    if setup_cfg.is_file():
        text = _read_bounded(setup_cfg)
        if text is not None and ini_has_section(text, "tool:pytest"):
            return DeclaredTestRunner(
                package=PYTEST_PACKAGE,
                trigger_config_file="setup.cfg",
                evidence="[tool:pytest]",
            )

    return None


@dataclass(frozen=True)
class ShellProbeResult:
    exit_code: int | None
    stderr: str = ""

    @property
    def ran(self) -> bool:
        return self.exit_code is not None


# A shell command runner: takes a command string and a timeout, reports what
# happened. The session supplies one backed by its own (possibly sandboxed)
# shell runner, so the probe and the install see exactly the environment the
# agent's own commands will see.
ShellCommandRunner = Callable[[str, float], ShellProbeResult]


# Resolve the interpreter the way the workspace itself would. Plenty of hosts
# ship only `python3`; without this the probe exits 127, the answer is "unknown",
# and the whole feature silently never fires there.
_PYTHON_PREFIX = "PY=python; command -v python >/dev/null 2>&1 || PY=python3; "


def build_import_probe_command(package: str) -> str:
    """A probe whose exit code distinguishes "missing" from "could not ask".

    Exit 0 means present and ``PACKAGE_MISSING_EXIT_CODE`` means definitively
    absent. Any other code -- 127 from a missing interpreter, a sandbox backend
    that failed to start, a shell that could not spawn -- means the question was
    never answered, and answering it wrongly would install into an environment
    nobody probed.
    """
    script = (
        "import importlib.util,sys;"
        f"sys.exit(0 if importlib.util.find_spec({package!r}) else {PACKAGE_MISSING_EXIT_CODE})"
    )
    return f'{_PYTHON_PREFIX}"$PY" -c {shlex.quote(script)}'


def build_provisioning_command(package: str) -> str:
    return f'{_PYTHON_PREFIX}"$PY" -m pip install {shlex.quote(package)} --quiet'


def probe_runner_importable(
    package: str,
    *,
    run_command: ShellCommandRunner,
    timeout_s: float = IMPORT_PROBE_TIMEOUT_SECONDS,
) -> bool | None:
    """Is ``package`` importable in the environment the agent's commands use?

    ``None`` when the probe could not answer -- the shell is disabled, the
    sandbox backend is unavailable, the interpreter is not on PATH. The decision
    treats that as "do nothing" rather than guessing in either direction.
    """
    result = run_command(build_import_probe_command(package), timeout_s)
    if not result.ran:
        return None
    if result.exit_code == 0:
        return True
    if result.exit_code == PACKAGE_MISSING_EXIT_CODE:
        return False
    return None


# ---------------------------------------------------------------------------
# Decision (pure)
# ---------------------------------------------------------------------------


class ProvisioningAction(StrEnum):
    PROVISION = "provision"
    REPORT_GAP = "report_gap"
    SKIP = "skip"


@dataclass(frozen=True)
class ProvisioningDecision:
    action: ProvisioningAction
    reason: str
    package: str = ""
    trigger_config_file: str = ""


def runtime_kind_provisions_autonomously(kind: RuntimeKind | str | None) -> bool:
    """True only for a top-level autonomous execution run.

    Nested kinds (subagent, conflict resolver) are excluded on
    purpose: they can be spawned from an interactive chat, and letting them
    install would smuggle into that session exactly the silent environment
    mutation it already declined to make. An unknown kind never installs.
    """
    try:
        normalized = normalize_runtime_kind(kind, fallback=RuntimeKind.INTERACTIVE_CHAT)
    except Exception:  # noqa: BLE001 - an unknown kind must never install
        return False
    return normalized in AUTONOMOUS_PROVISIONING_RUNTIME_KINDS


def resolve_provisioning_decision(
    *,
    declared: DeclaredTestRunner | None,
    importable: bool | None,
    runtime_kind: RuntimeKind | str | None,
    enabled: bool,
    subagent_depth: int = 0,
    already_attempted: bool = False,
) -> ProvisioningDecision:
    """Decide whether to install, report, or do nothing. Pure.

    Only one combination installs: the feature is on, the repo declared a runner
    in a config file, that runner is *confirmed* missing from the environment the
    agent's commands run in, the run is a top-level autonomous one, and no
    attempt has been made yet. Everything else is a no-op or a report.
    """
    if not enabled:
        return ProvisioningDecision(ProvisioningAction.SKIP, "workspace_provisioning_disabled")
    if declared is None:
        return ProvisioningDecision(ProvisioningAction.SKIP, "no_declared_test_runner")
    if importable is None:
        return ProvisioningDecision(
            ProvisioningAction.SKIP,
            "import_probe_unavailable",
            package=declared.package,
            trigger_config_file=declared.trigger_config_file,
        )
    if importable:
        return ProvisioningDecision(
            ProvisioningAction.SKIP,
            "declared_test_runner_importable",
            package=declared.package,
            trigger_config_file=declared.trigger_config_file,
        )
    if subagent_depth > 0:
        return ProvisioningDecision(
            ProvisioningAction.SKIP,
            "nested_session_defers_to_parent",
            package=declared.package,
            trigger_config_file=declared.trigger_config_file,
        )
    if not runtime_kind_provisions_autonomously(runtime_kind):
        return ProvisioningDecision(
            ProvisioningAction.REPORT_GAP,
            "interactive_run_does_not_install",
            package=declared.package,
            trigger_config_file=declared.trigger_config_file,
        )
    if already_attempted:
        return ProvisioningDecision(
            ProvisioningAction.REPORT_GAP,
            "provisioning_already_attempted",
            package=declared.package,
            trigger_config_file=declared.trigger_config_file,
        )
    return ProvisioningDecision(
        ProvisioningAction.PROVISION,
        "declared_test_runner_missing",
        package=declared.package,
        trigger_config_file=declared.trigger_config_file,
    )


# ---------------------------------------------------------------------------
# Execution (one bounded attempt)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProvisioningOutcome:
    package: str
    trigger_config_file: str
    success: bool
    command: str
    exit_code: int | None = None
    error: str = ""

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "package": self.package,
            "trigger_config_file": self.trigger_config_file,
            "success": self.success,
            "command": self.command,
        }
        if self.exit_code is not None:
            out["exit_code"] = self.exit_code
        if self.error:
            out["error"] = self.error
        return out


def provisioning_already_attempted(package: str) -> bool:
    with _attempt_lock:
        return package in _attempted


def _claim_provisioning_attempt(package: str) -> bool:
    """Claim the single attempt for ``package``. False when someone else holds it."""
    with _attempt_lock:
        if package in _attempted:
            return False
        _attempted.add(package)
        return True


def reset_provisioning_attempts() -> None:
    """Clear the one-attempt ledger. Test seam only."""
    with _attempt_lock:
        _attempted.clear()


def provision_test_runner(
    decision: ProvisioningDecision,
    *,
    run_command: ShellCommandRunner,
    timeout_s: float = PROVISION_TIMEOUT_SECONDS,
) -> ProvisioningOutcome | None:
    """Run the single install attempt described by ``decision``.

    Returns ``None`` when another session in this process already claimed the
    attempt. That is not a failure and must not be reported as one, or every
    swarm worker but the first would log an install failure that never happened.
    The claim is taken before the command runs, so a failure is never retried: a
    broken or offline environment costs one install, not one per session.
    """
    command = build_provisioning_command(decision.package)
    if not _claim_provisioning_attempt(decision.package):
        return None
    result = run_command(command, timeout_s)
    if not result.ran:
        return ProvisioningOutcome(
            package=decision.package,
            trigger_config_file=decision.trigger_config_file,
            success=False,
            command=command,
            error=result.stderr.strip()[:2000] or "the install command could not be executed",
        )
    exit_code = int(result.exit_code or 0)
    return ProvisioningOutcome(
        package=decision.package,
        trigger_config_file=decision.trigger_config_file,
        success=exit_code == 0,
        command=command,
        exit_code=exit_code,
        error=("" if exit_code == 0 else result.stderr.strip()[:2000]),
    )
