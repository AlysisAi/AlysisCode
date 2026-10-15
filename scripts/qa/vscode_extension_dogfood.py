#!/usr/bin/env python3
"""Dogfood harness for the Alysis Code VS Code extension release gate."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "vscode_dogfood" / "simple-python-project"
DEFAULT_REPORT_ROOT = PROJECT_ROOT / "qa_reports" / "vscode_dogfood"
DEFAULT_EXTENSION_DIR = PROJECT_ROOT / "extensions" / "vscode-alysis"
VSCODE_DOWNLOAD_HOST = "update.code.visualstudio.com"
EXTENSION_HOST_DOWNLOAD_FAILURE = (
    "Extension Host could not run because VS Code executable was not provided and "
    "download failed/unavailable."
)
MANUAL_REAL_PROVIDER_REPORT_SCHEMA_NAME = "completed-manual-report"
MANUAL_REAL_PROVIDER_REPORT_SCHEMA_VERSION = 5
SUPPORTED_PLATFORM_TARGETS = frozenset(
    {
        "win32-x64",
        "win32-arm64",
        "darwin-x64",
        "darwin-arm64",
        "linux-x64",
        "linux-arm64",
    }
)

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?i)(authorization)(\s*:\s*bearer\s+)([^\s,;]+)"),
    re.compile(r"(?i)(bearer)(\s+)([A-Za-z0-9._\-]{8,})"),
    re.compile(r"(?i)(api[_-]?key|authorization|bearer)(\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(token)(\s*[:=]\s*)([^\s,;]+)"),
]


class DogfoodError(RuntimeError):
    pass


class ExtensionHostDogfoodError(DogfoodError):
    def __init__(self, message: str, record: dict[str, Any]) -> None:
        super().__init__(message)
        self.record = record


MANUAL_REAL_PROVIDER_REQUIRED_CHECKS: tuple[dict[str, str], ...] = (
    {
        "id": "install_vsix",
        "step": "Install the generated VSIX with `code --install-extension <vsix>` or Install from VSIX.",
    },
    {
        "id": "open_disposable_workspace",
        "step": "Open the disposable fixture repository in VS Code.",
    },
    {
        "id": "managed_runtime_clean_install",
        "step": (
            "In a clean VS Code profile, leave `alysis.cliPath` empty and verify the "
            "installed target VSIX selects its signed managed runtime, reports production "
            "provenance, and passes `alysis ide-bridge health`."
        ),
    },
    {
        "id": "configure_provider_secret_storage",
        "step": "Configure the provider through `Alysis Code: Configure Provider` so credentials stay in SecretStorage.",
    },
    {
        "id": "doctor",
        "step": "Run `/doctor` and verify Output, Timeline, and Diagnostics are redacted.",
    },
    {
        "id": "create_session",
        "step": "Create a live IDE session in the Cockpit.",
    },
    {
        "id": "normal_chat",
        "step": "Send a normal chat message and verify it uses the live `chat.send` bridge path with redacted Timeline/Output rendering.",
    },
    {
        "id": "run_task",
        "step": "Run `Alysis Code: Run Task` for the fixture and verify it uses structured `run.start`, not terminal output or `chat.send` text forwarding.",
    },
    {
        "id": "model_info",
        "step": "Run `/model-info` and verify redacted provider/model metadata without API keys or secret headers.",
    },
    {
        "id": "subagent_status_on_off",
        "step": "Run `/subagent status`, `/subagent on`, and `/subagent off`; verify trusted toggles affect later live turns only.",
    },
    {
        "id": "image_path",
        "step": "Run `/image <path>` and verify workspace path, symlink, MIME, and size validation.",
    },
    {
        "id": "paste_image",
        "step": "Run `/paste-image` using file-picker fallback or `/paste-image <path>`; verify no image binary crosses JSONL.",
    },
    {
        "id": "trace_lifecycle",
        "step": "Run `/trace`, `/trace compact`, `/trace events`, and confirmed `/trace full`; verify redacted bounded output.",
    },
    {
        "id": "terminals_lifecycle",
        "step": "Run `/terminals list` and `/terminals show <id>` or verify structured unavailable behavior; kill only with trust and confirmation.",
    },
    {
        "id": "slash_plan",
        "step": "Run `/plan` for the fixture task and inspect the generated Forge plan.",
    },
    {
        "id": "forge_show_status",
        "step": "Run `/forge show` or `/plan show` and refresh Forge plan status; verify show/status are structured, active-plan scoped, and bounded.",
    },
    {
        "id": "forge_plan_state",
        "step": "Run `/forge plan state` and verify active plan state is structured and bounded.",
    },
    {
        "id": "forge_plan_validate",
        "step": "Run `/forge plan validate` and verify validation/review metadata.",
    },
    {
        "id": "forge_plan_regenerate",
        "step": "Run `/plan regenerate focus <text>` or `/forge plan regenerate focus <text>` and verify Workspace Trust plus optimistic revision behavior.",
    },
    {
        "id": "assistant_show_update",
        "step": "Run `/assistant show` and one trusted `/assistant <instruction>` update; verify persistence and audit metadata.",
    },
    {
        "id": "goal_show_update",
        "step": "Run `/goal show` and one trusted `/goal <goal>` update; verify non-empty validation and persistence.",
    },
    {
        "id": "task_show_update",
        "step": "Run `/task <task_id> show`, status, title, and body safe updates; verify task-id validation and no path traversal.",
    },
    {
        "id": "execute_preview",
        "step": "Run `/execute preview` and verify it is non-mutating and reports readiness, blockers, sandbox, risks, and checks.",
    },
    {
        "id": "execute_plan_review_mode",
        "step": "Run `/execute plan` and verify Preview plus explicit Execute Review confirmation before mutation.",
    },
    {
        "id": "run_swarm_review_flow",
        "step": (
            "Run Swarm from the cockpit (alysis.runSwarm) against a multi-task plan; verify the task "
            "grid shows parallel workers by glyph, at least one worker raises a NON-MODAL task-attributed "
            "approval (Allow once / Deny), cancel shows cooperative-checkpoint copy, and per-task review "
            "cards offer Keep/Discard with the untracked-files-not-applied list shown. Confirm the working "
            "tree stays unchanged until you Keep, that Keep applies one task's diff without committing, and "
            "that a failed/interrupted task offers an editable regenerate-subtree instruction."
        ),
    },
    {
        "id": "run_swarm_capability_gate",
        "step": (
            "On a CLI without forge.swarm support, verify Run Swarm is dimmed with a structured "
            "'needs a newer Alysis Code CLI' reason (never a working control)."
        ),
    },
    {
        "id": "forge_exec_unsupported_warning",
        "step": "Run broad `/forge exec` and `/forge execute` aliases; verify IDE v1 unsupported warnings for broad/unsafe modes.",
    },
    {
        "id": "diffs_artifacts",
        "step": "Open diffs and artifacts from the Cockpit/Activity Bar; verify they are scoped to the current session/job/plan.",
    },
    {
        "id": "manage_tree_domains",
        "step": "Refresh Manage tree tools, skills, hooks, MCP, conventions, and extension packages; verify exact capability/state/trust gating.",
    },
    {
        "id": "mcp_oauth_local_lifecycle",
        "step": (
            "On a local extension host, verify MCP OAuth start/status/cancel/logout uses an owned "
            "loopback callback, state + S256 PKCE, bounded polling, encrypted persistence, and "
            "returns no code/token through JSONL. Verify Remote-SSH renders the documented "
            "unavailable state without opening a callback."
        ),
    },
    {
        "id": "hooks_watch_not_advertised",
        "step": "Verify `hooks.watch` is not advertised and no watch starts passively on activation or refresh.",
    },
    {
        "id": "active_cancellation_cooperative",
        "step": "Verify active cancellation is displayed honestly as cooperative checkpoint cancellation, with no hard-interrupt claim.",
    },
    {
        "id": "secret_redaction",
        "step": "Confirm Output, Timeline, Diagnostics, dogfood reports, and generated artifacts contain no API keys, bearer tokens, or secrets.",
    },
    {
        "id": "missing_provider_recovery",
        "step": "Clear or withhold provider credentials, then verify missing-provider recovery routes to Configure Provider/SecretStorage without logging secrets.",
    },
    {
        "id": "broken_cli_recovery",
        "step": "Point `alysis.cliPath` at a broken or old CLI fixture, then verify setup/upgrade recovery appears and no API key is forwarded.",
    },
)


def redact(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 3:
            redacted = pattern.sub(r"\1\2[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def truncate(text: str, limit: int = 4000) -> str:
    clean = redact(text)
    if len(clean) <= limit:
        return clean
    return clean[:limit] + "\n...[truncated]"


def report_stream_copy(text: str | bytes | None, limit: int = 4000) -> dict[str, Any]:
    if text is None:
        text = ""
    if isinstance(text, bytes):
        raw = text.decode("utf-8", errors="replace")
    else:
        raw = text
    redacted = redact(raw)
    truncated = len(redacted) > limit
    return {
        "value": redacted[:limit] + ("\n...[truncated]" if truncated else ""),
        "raw_length": len(raw),
        "redacted_length": len(redacted),
        "truncated": truncated,
    }


def shell_join(argv: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return " ".join(shlex.quote(part) for part in argv)


def repo_venv_python() -> Path:
    if os.name == "nt":
        return PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    return PROJECT_ROOT / ".venv" / "bin" / "python"


def default_cli_command() -> list[str]:
    venv_python = repo_venv_python()
    python = venv_python if venv_python.exists() else Path(sys.executable)
    return [os.fspath(python), "-m", "alysis_code.cli"]


def parse_cli_command(raw: str | None) -> list[str]:
    if not raw:
        return default_cli_command()
    return shlex.split(raw, posix=os.name != "nt")


def create_report_dir(
    report_root: Path,
    now: dt.datetime | None = None,
    suffix: str | None = None,
) -> Path:
    stamp_time = now or dt.datetime.now(dt.UTC)
    stamp = stamp_time.strftime("%Y%m%dT%H%M%SZ")
    if suffix:
        stamp = f"{stamp}-{suffix}"
    report_dir = report_root / stamp
    index = 1
    while report_dir.exists():
        report_dir = report_root / f"{stamp}-{index}"
        index += 1
    report_dir.mkdir(parents=True)
    return report_dir


def copy_fixture(fixture: Path, destination: Path) -> None:
    if not fixture.is_dir():
        raise DogfoodError(f"Dogfood fixture does not exist: {fixture}")
    if destination.exists():
        raise DogfoodError(f"Destination already exists: {destination}")
    shutil.copytree(fixture, destination)


def dogfood_env() -> dict[str, str]:
    env = os.environ.copy()
    src = str(PROJECT_ROOT / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src if not existing else os.pathsep.join([src, existing])
    env.setdefault("ALYSIS_SHELL_SANDBOX_MODE", "off")
    return env


def manual_dogfood_env() -> dict[str, str]:
    safe_keys = (
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    )
    env = {key: os.environ[key] for key in safe_keys if key in os.environ}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def run_command(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 120,
    allow_failure: bool = False,
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout_report = report_stream_copy(exc.stdout)
        stderr_report = report_stream_copy(exc.stderr)
        record = {
            "command": shell_join(argv),
            "cwd": str(cwd),
            "exit_code": None,
            "timed_out": True,
            "timeout_s": timeout,
            "stdout": stdout_report["value"],
            "stderr": stderr_report["value"],
            "stdout_raw_length": stdout_report["raw_length"],
            "stderr_raw_length": stderr_report["raw_length"],
            "stdout_truncated": stdout_report["truncated"],
            "stderr_truncated": stderr_report["truncated"],
            "_stdout_raw": exc.stdout.decode("utf-8", errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or ""),
            "_stderr_raw": exc.stderr.decode("utf-8", errors="replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or ""),
        }
        if not allow_failure:
            raise DogfoodError(
                f"Command timed out after {timeout}s: {record['command']}\n"
                f"{record['stderr'] or record['stdout']}"
            ) from exc
        return record
    stdout_report = report_stream_copy(result.stdout)
    stderr_report = report_stream_copy(result.stderr)
    record = {
        "command": shell_join(argv),
        "cwd": str(cwd),
        "exit_code": result.returncode,
        "stdout": stdout_report["value"],
        "stderr": stderr_report["value"],
        "stdout_raw_length": stdout_report["raw_length"],
        "stderr_raw_length": stderr_report["raw_length"],
        "stdout_truncated": stdout_report["truncated"],
        "stderr_truncated": stderr_report["truncated"],
        "_stdout_raw": result.stdout,
        "_stderr_raw": result.stderr,
    }
    if result.returncode != 0 and not allow_failure:
        raise DogfoodError(
            f"Command failed with exit code {result.returncode}: {record['command']}\n"
            f"{record['stderr'] or record['stdout']}"
        )
    return record


def require_executable(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise DogfoodError(f"Required executable not found on PATH: {name}")
    return path


def init_git_repo(repo: Path, env: dict[str, str]) -> list[dict[str, Any]]:
    git = require_executable("git")
    commands = [
        [git, "init"],
        [git, "config", "user.email", "dogfood@example.invalid"],
        [git, "config", "user.name", "Alysis Code Dogfood"],
        [git, "add", "."],
        [git, "commit", "-m", "Initial dogfood fixture"],
    ]
    return [run_command(command, cwd=repo, env=env, timeout=60) for command in commands]


def newest_vsix(extension_dir: Path) -> Path | None:
    candidates = sorted(
        extension_dir.glob("*.vsix"),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    return candidates[0] if candidates else None


def extension_release_inputs(extension_dir: Path) -> tuple[Path, ...]:
    candidates: set[Path] = set()
    for name in (
        ".vscodeignore",
        "package.json",
        "package-lock.json",
        "README.md",
        "CHANGELOG.md",
        "RELEASE_CHECKLIST.md",
        "SUPPORT.md",
        "LICENSE",
    ):
        path = extension_dir / name
        if path.is_file() and not path.is_symlink():
            candidates.add(path)
    for relative_root in ("src", "out/src", "media", "resources"):
        root = extension_dir / relative_root
        if not root.is_dir() or root.is_symlink():
            continue
        candidates.update(
            path for path in root.rglob("*") if path.is_file() and not path.is_symlink()
        )
    return tuple(sorted(candidates, key=lambda path: path.relative_to(extension_dir).as_posix()))


def extension_source_snapshot(extension_dir: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    newest_mtime_ns = 0
    inputs = extension_release_inputs(extension_dir)
    if not inputs:
        raise DogfoodError(f"No extension release inputs found in {extension_dir}")
    for path in inputs:
        relative = path.relative_to(extension_dir).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
        newest_mtime_ns = max(newest_mtime_ns, path.stat().st_mtime_ns)
    return digest.hexdigest(), newest_mtime_ns


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_vsix(
    extension_dir: Path,
    package_mode: str,
    env: dict[str, str],
    *,
    explicit_vsix: Path | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    if explicit_vsix is not None:
        supplied = explicit_vsix.expanduser()
        if supplied.is_symlink():
            raise DogfoodError("--vsix-path must not be a symlink.")
        candidate = supplied.resolve(strict=True)
        if not candidate.is_file() or candidate.suffix.lower() != ".vsix":
            raise DogfoodError("--vsix-path must identify a regular .vsix file.")
        if candidate.stat().st_size <= 0:
            raise DogfoodError("--vsix-path must identify a non-empty VSIX.")
        return candidate, {
            "status": "ok",
            "path": str(candidate),
            "source": "explicit",
            "vsix_sha256": file_sha256(candidate),
            "source_snapshot_sha256": None,
        }
    if package_mode == "skip":
        return None, {"status": "skipped", "reason": "package mode is skip"}

    source_snapshot_sha256, newest_source_mtime_ns = extension_source_snapshot(extension_dir)
    existing = newest_vsix(extension_dir)
    if existing and package_mode in {"auto", "require"}:
        if existing.stat().st_mtime_ns >= newest_source_mtime_ns:
            return existing, {
                "status": "ok",
                "path": str(existing),
                "source": "existing",
                "vsix_sha256": file_sha256(existing),
                "source_snapshot_sha256": source_snapshot_sha256,
            }
        if package_mode == "require":
            raise DogfoodError(
                "Existing VSIX is older than extension release inputs; rebuild the candidate."
            )

    if package_mode == "require":
        raise DogfoodError(f"No VSIX package found in {extension_dir}")

    npm = require_executable("npm")
    package_record = run_command(
        [npm, "run", "package:dev-vsix", "--", "--pre-release"],
        cwd=extension_dir,
        env=env,
        timeout=300,
    )
    built = newest_vsix(extension_dir)
    if not built:
        raise DogfoodError("npm run package completed but no VSIX was produced")
    source_snapshot_sha256, _newest_source_mtime_ns = extension_source_snapshot(extension_dir)
    return built, {
        "status": "ok",
        "path": str(built),
        "source": "built",
        "package": package_record,
        "vsix_sha256": file_sha256(built),
        "source_snapshot_sha256": source_snapshot_sha256,
    }


def validate_bridge_health(cli_command: list[str], env: dict[str, str]) -> dict[str, Any]:
    record = run_command(
        [*cli_command, "ide-bridge", "health"],
        cwd=PROJECT_ROOT,
        env=env,
        timeout=60,
    )
    raw_stdout = str(record.get("_stdout_raw") or record.get("stdout") or "")
    try:
        payload = json.loads(raw_stdout or "{}")
    except json.JSONDecodeError as exc:
        raise DogfoodError(f"ide-bridge health did not return JSON: {exc}") from exc

    if payload.get("ok") is not True:
        raise DogfoodError(f"ide-bridge health returned not ok: {payload!r}")

    capabilities = (
        payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else {}
    )
    if not payload.get("protocol_version"):
        raise DogfoodError("ide-bridge health is missing protocol_version")
    methods = capabilities.get("methods") if isinstance(capabilities.get("methods"), list) else []
    features = (
        capabilities.get("features") if isinstance(capabilities.get("features"), dict) else {}
    )
    if not methods:
        raise DogfoodError("ide-bridge health is missing capabilities.methods")
    if not features:
        raise DogfoodError("ide-bridge health is missing capabilities.features")
    required_methods = {
        "initialize",
        "health",
        "getCapabilities",
        "session.create",
        "chat.send",
        "run.start",
        "forge.plan",
        "forge.executePreview",
        "forge.swarm.resume",
        "forge.swarm.list",
        "mcp.auth.login.start",
        "mcp.auth.login.status",
        "mcp.auth.login.cancel",
        "browser.start",
        "browser.navigate",
        "browser.close",
        "config.get",
        "doctor.summary",
    }
    missing = sorted(required_methods - set(str(method) for method in methods))
    if missing:
        raise DogfoodError(f"ide-bridge health is missing required methods: {', '.join(missing)}")

    _validate_unsafe_features_not_advertised(methods, features)

    record["protocol_version"] = payload.get("protocol_version")
    record["methods"] = methods
    return record


def _nested_mapping(mapping: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    current: Any = mapping
    for key in path:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _supported_flag(mapping: dict[str, Any], path: tuple[str, ...]) -> bool | None:
    value = _nested_mapping(mapping, path)
    supported = value.get("supported")
    return supported if isinstance(supported, bool) else None


def _validate_unsafe_features_not_advertised(methods: list[Any], features: dict[str, Any]) -> None:
    method_names = {str(method) for method in methods}
    if "hooks.watch" in method_names:
        raise DogfoodError(
            "ide-bridge health must not advertise hooks.watch without lifecycle support"
        )
    advertised_hook_watch_lifecycle = sorted(
        method for method in method_names if method.startswith("hooks.watch.")
    )
    if advertised_hook_watch_lifecycle:
        raise DogfoodError(
            "ide-bridge health must not advertise hooks.watch lifecycle methods without "
            f"subscription support: {', '.join(advertised_hook_watch_lifecycle)}"
        )
    forge = _nested_mapping(features, ("forge",))
    # SW6: Forge swarm IS now supported in the IDE — but only as a review-only,
    # cooperatively-cancellable, approval-routed workflow. Validate the honest
    # contract rather than asserting it is unavailable.
    swarm = _nested_mapping(forge, ("swarm",))
    if swarm.get("supported") is True:
        if swarm.get("cancellation") != "cooperative_checkpoint_cancellation":
            raise DogfoodError(
                "ide-bridge health advertises forge.swarm without cooperative checkpoint cancellation"
            )
        if swarm.get("merge_behavior") != "review_only_per_task_apply_discard":
            raise DogfoodError("ide-bridge health advertises Forge swarm without review-only merge")
        if swarm.get("workspace_trust_required") is not True:
            raise DogfoodError(
                "ide-bridge health advertises Forge swarm without Workspace Trust gating"
            )
        swarm_approvals = _nested_mapping(swarm, ("approvals",))
        if swarm_approvals.get("yes_auto_approval") is not False:
            raise DogfoodError("ide-bridge health advertises Forge swarm with --yes auto-approval")
    resumable_swarm = _nested_mapping(features, ("resumable_swarm",))
    if resumable_swarm.get("supported") is True:
        required_swarm_methods = {
            "forge.swarm.start",
            "forge.swarm.resume",
            "forge.swarm.list",
            "forge.swarm.status",
            "forge.swarm.result",
            "forge.swarm.cancel",
        }
        if not required_swarm_methods <= method_names:
            raise DogfoodError("ide-bridge durable Forge swarm lifecycle is incomplete")
        for guarantee in (
            "durable",
            "explicit_resume",
            "fresh_permission_fingerprint_required",
            "fenced_worker_leases",
            "restart_recovery",
            "atomic_cancellation",
            "exactly_once_usage_events",
        ):
            if resumable_swarm.get(guarantee) is not True:
                raise DogfoodError(f"ide-bridge durable Forge swarm is missing {guarantee}")
    forge_cancel = _nested_mapping(forge, ("cancel",))
    if _supported_flag(features, ("forge", "cancel")) is True:
        if forge_cancel.get("behavior") != "cooperative_checkpoint_cancellation":
            raise DogfoodError(
                "ide-bridge health advertises Forge cancellation without cooperative checkpoint behavior"
            )
        if forge_cancel.get("hard_interrupt") is True:
            raise DogfoodError(
                "ide-bridge health falsely advertises hard-interrupt Forge cancellation"
            )
        lifecycle_states = set(
            str(item) for item in forge_cancel.get("lifecycle_states", []) if item
        )
        if not {"cancellation_requested", "cancelled", "completed", "failed"} <= lifecycle_states:
            raise DogfoodError(
                "ide-bridge health omits required Forge cancellation lifecycle states"
            )
    execute = _nested_mapping(forge, ("execute",))
    unsafe_modes = _nested_mapping(execute, ("unsafe_modes",))
    if unsafe_modes.get("supported") is True:
        raise DogfoodError("ide-bridge health falsely advertises Forge unsafe execution modes")
    supported_modes = execute.get("supported_modes")
    if isinstance(supported_modes, list) and any(
        str(mode).casefold() in {"auto", "fullaccess"} for mode in supported_modes
    ):
        raise DogfoodError("ide-bridge health falsely advertises Forge auto/fullaccess execution")

    management_methods = _nested_mapping(features, ("management", "methods"))
    mcp_login_feature = _nested_mapping(features, ("management", "mcp", "auth_login"))
    advertised_mcp_login_lifecycle = {
        method for method in method_names if method.startswith("mcp.auth.login.")
    }
    if mcp_login_feature.get("supported") is True:
        required_oauth_methods = {
            "mcp.auth.login.start",
            "mcp.auth.login.status",
            "mcp.auth.login.cancel",
        }
        if not required_oauth_methods <= advertised_mcp_login_lifecycle:
            raise DogfoodError("ide-bridge MCP OAuth lifecycle methods are incomplete")
        for guarantee in (
            "flow_state_persistent",
            "loopback_callback_owned",
            "pkce_s256",
            "state_validation",
            "host_header_validation",
            "timeout_expiry",
            "cancel_cleanup",
            "encrypted_token_store",
            "logout_fences_late_token_writes",
        ):
            if mcp_login_feature.get(guarantee) is not True:
                raise DogfoodError(f"ide-bridge MCP OAuth lifecycle is missing {guarantee}")
        if mcp_login_feature.get("authorization_code_in_protocol") is not False:
            raise DogfoodError("ide-bridge MCP OAuth exposes authorization codes in protocol")
        if mcp_login_feature.get("tokens_in_protocol_params") is not False:
            raise DogfoodError("ide-bridge MCP OAuth accepts token material in protocol params")
        for method in required_oauth_methods:
            method_contract = management_methods.get(method)
            if (
                not isinstance(method_contract, dict)
                or method_contract.get("supported") is not True
            ):
                raise DogfoodError(f"ide-bridge MCP OAuth method contract is missing {method}")
    elif advertised_mcp_login_lifecycle:
        raise DogfoodError(
            "ide-bridge advertises MCP login lifecycle methods without a supported OAuth contract"
        )

    browser = _nested_mapping(features, ("managed_browser",))
    if browser.get("supported") is True:
        required_browser_methods = {
            "browser.start",
            "browser.navigate",
            "browser.snapshot",
            "browser.screenshot",
            "browser.artifact.read",
            "browser.diagnostics",
            "browser.click",
            "browser.type",
            "browser.status",
            "browser.list",
            "browser.close",
        }
        if not required_browser_methods <= method_names:
            raise DogfoodError("ide-bridge managed browser lifecycle methods are incomplete")
        for guarantee in (
            "owned_chromium_processes",
            "loopback_cdp_only",
            "private_profiles_outside_workspaces",
            "guarded_navigation",
            "redirect_and_subresource_interception",
            "persistent_child_target_interception",
            "validating_egress_proxy",
            "dns_resolution_pinned_to_numeric_connect",
            "no_direct_network_fallback",
            "loopback_proxy_bypass_removed",
            "non_proxied_udp_disabled",
            "public_destinations_by_default",
            "local_destinations_require_confirmation",
            "bounded_snapshots",
            "chunked_screenshot_artifacts",
            "bounded_diagnostics",
            "session_cleanup",
        ):
            if browser.get(guarantee) is not True:
                raise DogfoodError(f"ide-bridge managed browser is missing {guarantee}")
    hooks_watch = _nested_mapping(features, ("management", "hooks", "watch"))
    if hooks_watch.get("supported") is True:
        raise DogfoodError("ide-bridge health falsely advertises hooks.watch support")


def run_fixture_verification(repo: Path, env: dict[str, str]) -> dict[str, Any]:
    return run_command(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=repo,
        env=env,
        timeout=60,
    )


def _path_is_executable(path: Path) -> bool:
    if not path.is_file():
        return False
    if os.name == "nt":
        return True
    return os.access(path, os.X_OK)


def resolve_vscode_executable(
    *,
    explicit_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[Path | None, str]:
    raw_path = os.fspath(explicit_path) if explicit_path is not None else ""
    source = "--vscode-executable"
    if not raw_path:
        source = "VSCODE_TEST_EXECUTABLE_PATH"
        raw_path = (env or os.environ).get("VSCODE_TEST_EXECUTABLE_PATH", "").strip()
    if not raw_path:
        return None, "not_provided"
    path = Path(raw_path).expanduser().resolve()
    if not _path_is_executable(path):
        raise DogfoodError(
            f"VS Code executable from {source} is missing or not executable: {path}. "
            "Set VSCODE_TEST_EXECUTABLE_PATH to an existing VS Code executable path, "
            "or pass --vscode-executable with that path."
        )
    return path, source


def extension_host_preflight(
    mode: str,
    *,
    vscode_executable: Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    if mode == "skip":
        return {
            "status": "skipped",
            "mode": mode,
            "release_valid": False,
            "message": "Extension Host skipped; this is a local smoke only.",
        }
    try:
        executable, executable_source = resolve_vscode_executable(
            explicit_path=vscode_executable,
            env=env,
        )
    except DogfoodError as exc:
        return {
            "status": "failed",
            "mode": mode,
            "release_valid": False,
            "message": str(exc),
        }
    if executable is not None:
        return {
            "status": "passed",
            "mode": mode,
            "release_valid": True,
            "vscode_executable": os.fspath(executable),
            "vscode_executable_source": executable_source,
            "download_required": False,
            "message": f"Using VS Code executable from {executable_source}.",
        }
    if mode == "require-cached":
        return {
            "status": "failed",
            "mode": mode,
            "release_valid": False,
            "message": (
                "Extension Host could not run because VS Code executable was not provided. "
                "Set VSCODE_TEST_EXECUTABLE_PATH to an existing VS Code executable path, "
                "or pass --vscode-executable with that path. Example: "
                "VSCODE_TEST_EXECUTABLE_PATH=/path/to/code scripts/qa/check_vscode_extension_release_candidate.sh"
            ),
        }
    download_ok, download_reason = check_vscode_download_available()
    if not download_ok:
        return {
            "status": "failed",
            "mode": mode,
            "release_valid": False,
            "download_required": True,
            "download_available": False,
            "download_reason": download_reason,
            "message": (
                f"{EXTENSION_HOST_DOWNLOAD_FAILURE} {download_reason}. "
                "Set VSCODE_TEST_EXECUTABLE_PATH to an existing VS Code executable path, "
                "or pass --vscode-executable with that path."
            ),
        }
    return {
        "status": "passed",
        "mode": mode,
        "release_valid": True,
        "vscode_executable": None,
        "vscode_executable_source": "download",
        "download_required": True,
        "download_available": True,
        "download_reason": download_reason,
        "message": "VS Code executable was not provided; download preflight succeeded.",
    }


def check_vscode_download_available(timeout_s: float = 5.0) -> tuple[bool, str]:
    try:
        with socket.create_connection((VSCODE_DOWNLOAD_HOST, 443), timeout=timeout_s):
            return True, f"{VSCODE_DOWNLOAD_HOST}:443 reachable"
    except OSError as exc:
        return False, f"{VSCODE_DOWNLOAD_HOST}:443 unavailable: {exc}"


def summarize_extension_host_failure(record: dict[str, Any]) -> str:
    combined = "\n".join(str(record.get(key) or "") for key in ("stderr", "stdout"))
    lowered = combined.casefold()
    preflight = record.get("preflight")
    download_required = (
        bool(preflight.get("download_required"))
        if isinstance(preflight, dict)
        else record.get("vscode_executable_source") == "download"
    )
    download_markers = (
        VSCODE_DOWNLOAD_HOST,
        "getaddrinfo",
        "enotfound",
        "eai_again",
        "econnreset",
        "etimedout",
        "download",
        "resolving version",
        "failed to fetch",
    )
    if download_required and any(marker.casefold() in lowered for marker in download_markers):
        return (
            f"{EXTENSION_HOST_DOWNLOAD_FAILURE} Set VSCODE_TEST_EXECUTABLE_PATH to an existing "
            "VS Code executable path, or pass --vscode-executable with that path."
        )
    return (
        "Extension Host integration test failed. "
        f"Exit code: {record.get('exit_code')}. "
        f"{truncate(combined, limit=1200).strip()}"
    ).strip()


def run_extension_host(
    extension_dir: Path,
    env: dict[str, str],
    mode: str,
    *,
    vsix: Path | None = None,
    vscode_executable: Path | None = None,
    preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    preflight = preflight or extension_host_preflight(
        mode,
        vscode_executable=vscode_executable,
        env=env,
    )
    if preflight["status"] == "skipped":
        return {
            "status": "skipped",
            "extension_host": "skipped",
            "release_valid": False,
            "preflight": preflight,
            "reason": "extension host mode is skip; this is a local smoke only and is not release-valid",
        }
    if preflight["status"] == "failed":
        raise DogfoodError(str(preflight["message"]))

    host_env = dict(env)
    executable_value = preflight.get("vscode_executable")
    executable = Path(str(executable_value)).resolve() if executable_value else None
    if executable is not None:
        host_env["VSCODE_TEST_EXECUTABLE_PATH"] = os.fspath(executable)

    npm = require_executable("npm")
    with (
        unpacked_vsix_extension(vsix)
        if vsix is not None
        else _source_extension(extension_dir) as extension_under_test
    ):
        host_env["ALYSIS_TEST_EXTENSION_PATH"] = os.fspath(extension_under_test)
        record = run_command(
            [npm, "run", "test:integration"],
            cwd=extension_dir,
            env=host_env,
            timeout=600,
            allow_failure=True,
        )
    record["extension_under_test"] = "packaged_vsix" if vsix is not None else "source"
    if vsix is not None:
        record["vsix"] = os.fspath(vsix.resolve())
    record["release_valid"] = True
    record["preflight"] = preflight
    if executable is not None:
        record["vscode_executable"] = os.fspath(executable)
        record["vscode_executable_source"] = preflight.get("vscode_executable_source")
    else:
        record["vscode_executable"] = None
        record["vscode_executable_source"] = "download"
    if record.get("exit_code") != 0:
        record["status"] = "failed"
        record["extension_host"] = "failed"
        record["failure_message"] = summarize_extension_host_failure(record)
        raise ExtensionHostDogfoodError(str(record["failure_message"]), record)
    launch_output = "\n".join(
        str(record.get(name, "")) for name in ("_stdout_raw", "_stderr_raw", "stdout", "stderr")
    )
    if "DEP0190" in launch_output or "shell option true" in launch_output:
        record["status"] = "failed"
        record["extension_host"] = "failed"
        record["failure_message"] = (
            "Extension Host integration used an unsafe shell-based child-process launch."
        )
        raise ExtensionHostDogfoodError(str(record["failure_message"]), record)
    record["status"] = "passed"
    record["extension_host"] = "passed"
    return record


@contextmanager
def _source_extension(extension_dir: Path):
    yield extension_dir.resolve()


@contextmanager
def unpacked_vsix_extension(vsix: Path):
    """Yield the packaged extension root after a zip-slip-safe extraction."""

    archive_path = vsix.expanduser().resolve()
    if not archive_path.is_file():
        raise DogfoodError(
            f"VSIX for packaged Extension Host dogfood was not found: {archive_path}"
        )
    with tempfile.TemporaryDirectory(prefix="alysis-vsix-dogfood-") as raw_temp:
        extraction_root = Path(raw_temp).resolve()
        try:
            with zipfile.ZipFile(archive_path) as archive:
                members = archive.infolist()
                if (
                    len(members) > 10_000
                    or sum(item.file_size for item in members) > 512 * 1024 * 1024
                ):
                    raise DogfoodError("VSIX exceeds packaged dogfood extraction limits.")
                for member in members:
                    normalized_name = member.filename.replace("\\", "/")
                    member_path = PurePosixPath(normalized_name)
                    if (
                        member_path.is_absolute()
                        or ".." in member_path.parts
                        or not member_path.parts
                        or re.match(r"^[A-Za-z]:", normalized_name)
                    ):
                        raise DogfoodError("VSIX contains an unsafe archive path.")
                archive.extractall(extraction_root)
        except (OSError, zipfile.BadZipFile) as exc:
            raise DogfoodError("VSIX could not be unpacked for Extension Host dogfood.") from exc
        extension_root = extraction_root / "extension"
        package_path = extension_root / "package.json"
        if not package_path.is_file():
            raise DogfoodError("VSIX is missing extension/package.json.")
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DogfoodError("VSIX extension/package.json is invalid.") from exc
        if not isinstance(package, dict):
            raise DogfoodError("VSIX extension/package.json must be an object.")
        raw_main = package.get("main")
        if not isinstance(raw_main, str) or not raw_main.strip():
            raise DogfoodError("VSIX is missing the extension main entry point.")
        normalized_main = raw_main.replace("\\", "/").removeprefix("./")
        main_parts = PurePosixPath(normalized_main)
        if (
            main_parts.is_absolute()
            or ".." in main_parts.parts
            or not main_parts.parts
            or re.match(r"^[A-Za-z]:", normalized_main)
        ):
            raise DogfoodError("VSIX extension main entry point is unsafe.")
        main_path = (extension_root / Path(*main_parts.parts)).resolve()
        if not main_path.is_relative_to(extension_root.resolve()) or not main_path.is_file():
            raise DogfoodError("VSIX is missing the extension main entry point.")
        yield extension_root


def validate_release_valid_summary(payload: dict[str, Any]) -> None:
    errors: list[str] = []
    if payload.get("schema_name") != "installed-production-vsix":
        errors.append("schema_name must be installed-production-vsix")
    if payload.get("schema_version") not in {2, 3}:
        errors.append("schema_version must be 2 or 3")
    if payload.get("mode") != "installed-production-vsix":
        errors.append("mode must be installed-production-vsix")
    if payload.get("status") != "passed":
        errors.append(f"status must be passed, got {payload.get('status')!r}")
    if payload.get("release_valid") is not True:
        errors.append(f"release_valid must be true, got {payload.get('release_valid')!r}")
    if payload.get("extension_host") != "passed":
        errors.append(f"extension_host must be passed, got {payload.get('extension_host')!r}")
    if payload.get("extension_under_test") != "packaged_vsix":
        errors.append(
            "extension_under_test must be packaged_vsix, "
            f"got {payload.get('extension_under_test')!r}"
        )
    if not isinstance(payload.get("vsix"), str) or not str(payload.get("vsix")).strip():
        errors.append("vsix must identify the packaged artifact under test")
    if re.fullmatch(r"[0-9a-f]{64}", str(payload.get("vsix_sha256") or "")) is None:
        errors.append("vsix_sha256 must identify the exact packaged artifact")
    if payload.get("clean_install") is not True:
        errors.append("clean_install must prove code --install-extension in an isolated profile")
    if payload.get("restart_check") != "passed":
        errors.append("restart_check must prove a second Extension Host launch passed")
    if payload.get("restart_profile_reused") is not True:
        errors.append("restart_profile_reused must prove the isolated profile was reused")
    if payload.get("restart_runtime_identity_check") != "passed":
        errors.append(
            "restart_runtime_identity_check must prove managed runtime identity stability"
        )
    if payload.get("extension_host_launches") != 2:
        errors.append("extension_host_launches must be exactly 2")
    if payload.get("runtime_origin") != "managed":
        errors.append("runtime_origin must be managed")
    if payload.get("runtime_production") is not True:
        errors.append("runtime_production must be true")
    if payload.get("cli_path_override") not in (None, ""):
        errors.append("cli_path_override must be empty for production dogfood")
    if payload.get("native_signature_check") != "passed":
        errors.append("native_signature_check must be passed")
    if payload.get("release_signature_check") != "passed":
        errors.append("release_signature_check must be passed")
    if payload.get("package_install_check") != "passed":
        errors.append("package_install_check must be passed")
    if payload.get("bridge_health") != "passed":
        errors.append("bridge_health must be passed")
    if payload.get("extension_mode") != "production":
        errors.append("extension_mode must be production")
    if (
        re.fullmatch(
            r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", str(payload.get("extension_version") or "")
        )
        is None
    ):
        errors.append("extension_version must be a semantic version")
    if re.fullmatch(r"v\d+\.\d+\.\d+", str(payload.get("release_tag") or "")) is None:
        errors.append("release_tag must identify the signed managed release")
    if payload.get("source_repository") != "https://github.com/AlysisAi/AlysisCode":
        errors.append("source_repository must identify the official repository")
    if re.fullmatch(r"[0-9a-f]{40}", str(payload.get("source_sha") or "")) is None:
        errors.append("source_sha must identify the exact release commit")
    if payload.get("managed_manifest_signature_check") != "passed":
        errors.append("managed_manifest_signature_check must be passed")
    if (
        re.fullmatch(
            r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", str(payload.get("vscode_version") or "")
        )
        is None
    ):
        errors.append("vscode_version must identify the exercised VS Code build")
    if payload.get("host_platform") not in {"win32", "darwin", "linux"}:
        errors.append("host_platform must identify the Extension Host OS")
    if payload.get("host_arch") not in {"x64", "arm64"}:
        errors.append("host_arch must identify the Extension Host architecture")
    if payload.get("remote_name") != "":
        errors.append("clean target dogfood must run in a local Extension Host")
    if payload.get("workspace_trusted") is not True:
        errors.append("clean target dogfood workspace must be trusted")
    if payload.get("workspace_scheme") != "file" or payload.get("workspace_authority") != "":
        errors.append("clean target dogfood must use a local file workspace")
    timestamp_pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z"
    for field in ("started_at", "completed_at"):
        if re.fullmatch(timestamp_pattern, str(payload.get(field) or "")) is None:
            errors.append(f"{field} must be a UTC RFC3339 timestamp")
    if (
        not isinstance(payload.get("managed_artifact_version"), str)
        or not str(payload.get("managed_artifact_version")).strip()
    ):
        errors.append("managed_artifact_version must identify the bundled release")
    if re.fullmatch(r"[0-9a-f]{64}", str(payload.get("managed_cli_sha256") or "")) is None:
        errors.append("managed_cli_sha256 must identify the selected managed runtime")
    if payload.get("platform_target") not in SUPPORTED_PLATFORM_TARGETS:
        errors.append("platform_target must identify a supported target VSIX")
    target = str(payload.get("platform_target") or "")
    expected_platform = (
        "win32"
        if target.startswith("win32-")
        else "darwin"
        if target.startswith("darwin-")
        else "linux"
    )
    expected_arch = "arm64" if target.endswith("-arm64") else "x64"
    if (
        payload.get("host_platform") != expected_platform
        or payload.get("host_arch") != expected_arch
    ):
        errors.append("Extension Host OS/architecture must match platform_target")
    if errors:
        raise DogfoodError("Dogfood summary is not release-valid: " + "; ".join(errors))


def validate_release_valid_summary_file(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise DogfoodError(f"Dogfood summary must be a JSON object: {path}")
    validate_release_valid_summary(payload)
    return payload


def validate_production_vscode_compatibility(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != 3:
        raise DogfoodError("Production compatibility evidence requires schema_version 3.")
    compatibility = payload.get("vscode_compatibility")
    if not isinstance(compatibility, dict) or set(compatibility) != {
        "minimum",
        "current_stable",
    }:
        raise DogfoodError(
            "Production compatibility evidence must contain exact minimum/current_stable rows."
        )
    target = str(payload.get("platform_target") or "")
    expected_arch = "arm64" if target.endswith("-arm64") else "x64"
    expected_keys = {
        "requested_version",
        "version",
        "commit",
        "arch",
        "executable_sha256",
    }
    rows: dict[str, dict[str, Any]] = {}
    for label in ("minimum", "current_stable"):
        row = compatibility.get(label)
        if not isinstance(row, dict) or set(row) != expected_keys:
            raise DogfoodError(f"VS Code compatibility row {label} has an invalid schema.")
        if (
            re.fullmatch(r"\d+\.\d+\.\d+", str(row.get("version") or "")) is None
            or re.fullmatch(r"[0-9a-f]{40}", str(row.get("commit") or "")) is None
            or re.fullmatch(r"[0-9a-f]{64}", str(row.get("executable_sha256") or "")) is None
            or row.get("arch") != expected_arch
        ):
            raise DogfoodError(f"VS Code compatibility row {label} has invalid identity fields.")
        rows[label] = row
    if (
        rows["minimum"].get("requested_version") != "1.90.0"
        or rows["minimum"].get("version") != "1.90.0"
        or rows["current_stable"].get("requested_version") != "stable"
    ):
        raise DogfoodError(
            "VS Code compatibility rows do not prove the declared minimum and current Stable."
        )
    minimum_version = tuple(int(part) for part in str(rows["minimum"]["version"]).split("."))
    current_version = tuple(int(part) for part in str(rows["current_stable"]["version"]).split("."))
    if current_version < minimum_version:
        raise DogfoodError("Resolved current VS Code Stable is older than the declared minimum.")


def validate_production_dogfood_candidate_binding(
    payload: dict[str, Any],
    candidate_vsix: Path,
    *,
    trusted_public_key_path: Path | None = None,
    require_vscode_compatibility: bool = False,
    channel: str = "stable",
) -> None:
    """Bind installed-production evidence to the exact signed candidate package."""

    validate_release_valid_summary(payload)
    if require_vscode_compatibility or payload.get("schema_version") == 3:
        validate_production_vscode_compatibility(payload)
    from scripts.qa.validate_vscode_manual_provider_report import (
        ManualReportValidationError,
        validate_candidate_binding,
    )

    try:
        validate_candidate_binding(
            payload,
            candidate_vsix,
            trusted_public_key_path=trusted_public_key_path,
            channel=channel,
        )
        with zipfile.ZipFile(candidate_vsix) as archive:
            manifest = json.loads(
                archive.read("extension/resources/managed-cli/manifest.json").decode("utf-8")
            )
    except (
        ManualReportValidationError,
        OSError,
        KeyError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
    ) as exc:
        raise DogfoodError(f"Production dogfood is not bound to the candidate: {exc}") from exc
    release = manifest.get("release") if isinstance(manifest, dict) else None
    if not isinstance(release, dict) or release != {
        "sourceCommit": payload.get("source_sha"),
        "sourceRepository": payload.get("source_repository"),
        "tag": payload.get("release_tag"),
    }:
        raise DogfoodError(
            "Production dogfood release identity does not match the signed candidate manifest."
        )


def validate_component_valid_summary(payload: dict[str, Any]) -> None:
    errors: list[str] = []
    if payload.get("status") != "passed":
        errors.append(f"status must be passed, got {payload.get('status')!r}")
    if payload.get("component_valid") is not True:
        errors.append(f"component_valid must be true, got {payload.get('component_valid')!r}")
    if payload.get("release_valid") is not False:
        errors.append("component dogfood must never claim release_valid")
    if payload.get("extension_host") != "passed":
        errors.append(f"extension_host must be passed, got {payload.get('extension_host')!r}")
    if payload.get("extension_under_test") != "packaged_vsix":
        errors.append("component dogfood must exercise packaged_vsix bytes")
    if re.fullmatch(r"[0-9a-f]{64}", str(payload.get("vsix_sha256") or "")) is None:
        errors.append("vsix_sha256 must identify the exact component artifact")
    if errors:
        raise DogfoodError("Dogfood summary is not component-valid: " + "; ".join(errors))


def manual_steps() -> list[str]:
    task = "Add a pure clamp(value, lower, upper) function to src/simple_math.py and cover it with tests."
    steps = [check["step"] for check in MANUAL_REAL_PROVIDER_REQUIRED_CHECKS]
    steps.append(
        "Confirm the Timeline shows command started, validation, bridge start/reuse, planning started, and planning completed."
    )
    steps.append(
        "Inspect Forge Plan cards, selected task detail, file scope, acceptance criteria, dependencies, and verification commands."
    )
    steps.append("Run the fixture verification command: `python -m unittest discover -s tests`.")
    steps.append(
        "Confirm the status bar, Bridge chip, CLI Origin, CLI Health, Sandbox chip, and Diagnostics agree."
    )
    steps.append(
        "Uninstall the VSIX or reset the Extension Development Host profile after testing."
    )
    return [step.replace("Run `/plan`", f"Run `/plan {task}`") for step in steps]


def manual_required_checks() -> list[dict[str, str]]:
    return [dict(check) for check in MANUAL_REAL_PROVIDER_REQUIRED_CHECKS]


def manual_report_template(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_name": MANUAL_REAL_PROVIDER_REPORT_SCHEMA_NAME,
        "schema_version": MANUAL_REAL_PROVIDER_REPORT_SCHEMA_VERSION,
        "mode": "manual-real-provider",
        "status": "draft",
        "provider": "",
        "extension_version": "",
        "vsix": report.get("vsix") or "",
        "vsix_sha256": report.get("vsix_sha256") or "",
        "candidate_run_id": None,
        "release_tag": "",
        "source_repository": "https://github.com/AlysisAi/AlysisCode",
        "source_sha": "",
        "production_dogfood_sha256": "",
        "platform_target": "",
        "vscode_version": "",
        "host_platform": "",
        "host_arch": "",
        "remote_name": "",
        "workspace_trusted": None,
        "workspace_scheme": "",
        "workspace_authority": "",
        "runtime_origin": "",
        "runtime_production": False,
        "managed_artifact_version": "",
        "managed_cli_sha256": "",
        "release_signature_check": "pending",
        "native_signature_check": "pending",
        "package_install_check": "pending",
        "bridge_health": "pending",
        "cli_path_override": "",
        "completed_checks": {
            check["id"]: {
                "status": "pending",
                "completed_at": "",
                "artifact_sha256": "",
                "event_id": "",
                "notes": "",
                "rationale": "",
            }
            for check in MANUAL_REAL_PROVIDER_REQUIRED_CHECKS
        },
        "known_limitations_confirmed": False,
        "no_p0_p1_blockers": False,
        "secret_leak_check": "pending",
        "reviewer": "",
        "started_at": "",
        "completed_at": "",
    }


def write_reports(report_dir: Path, report: dict[str, Any]) -> None:
    report_json = report_dir / "report.json"
    report_md = report_dir / "report.md"
    summary_json = report_dir / "summary.json"
    report_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = {
        "mode": report.get("mode"),
        "status": report.get("status"),
        "component_valid": bool(report.get("component_valid", False)),
        "release_valid": bool(report.get("release_valid", False)),
        "release_valid_reason": report.get("release_valid_reason", "n/a"),
        "extension_host": report.get("extension_host", "unknown"),
        "extension_under_test": report.get("extension_under_test", "unknown"),
        "extension_host_preflight": (report.get("extension_host_preflight") or {}).get(
            "status", "unknown"
        ),
        "report_dir": str(report_dir),
        "report_json": str(report_json),
        "report_md": str(report_md),
        "vsix": report.get("vsix"),
        "vsix_sha256": report.get("vsix_sha256"),
        "source_snapshot_sha256": report.get("source_snapshot_sha256"),
        "clean_install": bool(report.get("clean_install", False)),
        "runtime_origin": report.get("runtime_origin"),
        "runtime_production": bool(report.get("runtime_production", False)),
        "managed_artifact_version": report.get("managed_artifact_version"),
        "managed_cli_sha256": report.get("managed_cli_sha256"),
        "cli_path_override": report.get("cli_path_override"),
        "native_signature_check": report.get("native_signature_check"),
        "release_signature_check": report.get("release_signature_check"),
        "package_install_check": report.get("package_install_check"),
        "bridge_health": report.get("bridge_health"),
        "platform_target": report.get("platform_target"),
        "worktree": report.get("worktree"),
        "step_count": len(report.get("steps", [])),
    }
    if report.get("error"):
        summary["error"] = report.get("error")
    summary_json.write_text(
        json.dumps(sanitize_report_value(summary), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# VS Code Extension Dogfood Report",
        "",
        f"- Mode: `{report['mode']}`",
        f"- Status: `{report['status']}`",
        f"- Release valid: `{str(report.get('release_valid', False)).lower()}`",
        f"- Release validity: `{report.get('release_valid_reason', 'n/a')}`",
        f"- Extension Host: `{report.get('extension_host', 'unknown')}`",
        f"- Extension under test: `{report.get('extension_under_test', 'unknown')}`",
        f"- Extension Host preflight: `{(report.get('extension_host_preflight') or {}).get('status', 'unknown')}`",
        f"- API key required: `{str(report.get('api_key_required', False)).lower()}`",
        f"- Disposable repo: `{report.get('worktree', 'n/a')}`",
        f"- VSIX: `{report.get('vsix', 'n/a')}`",
        f"- VSIX SHA-256: `{report.get('vsix_sha256', 'n/a')}`",
        "",
        "## Steps",
        "",
    ]
    for step in report.get("steps", []):
        lines.append(f"- `{step.get('name')}`: `{step.get('status')}`")
        if step.get("command"):
            lines.append(f"  - command: `{step['command']}`")
        if step.get("exit_code") is not None:
            lines.append(f"  - exit code: `{step['exit_code']}`")
        if step.get("message"):
            lines.append(f"  - message: {step['message']}")
    if report.get("manual_steps"):
        lines.extend(["", "## Manual Real-Provider Checklist", ""])
        required_checks = report.get("required_checks") or []
        if required_checks:
            for check in required_checks:
                lines.append(f"- [ ] `{check['id']}` - {check['step']}")
        else:
            for index, step in enumerate(report["manual_steps"], start=1):
                lines.append(f"{index}. [ ] {step}")
    if report.get("error"):
        lines.extend(["", "## Failure", "", report["error"]])
    report_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if report.get("manual_steps"):
        checklist = report_dir / "manual_checklist.md"
        checklist_lines = [
            "# Manual Real-Provider Dogfood Checklist",
            "",
            "Complete every required check below, then write a separate completed JSON report and",
            "validate it with `python scripts/qa/validate_vscode_manual_provider_report.py "
            "<report.json> <target.vsix> <production-dogfood.json>`.",
            "The generated dogfood `report.json` with `status: manual_steps_written` is not completed evidence.",
            "",
        ]
        required_checks = report.get("required_checks") or []
        if required_checks:
            checklist_lines.extend(
                f"- [ ] `{check['id']}` - {check['step']}" for check in required_checks
            )
        else:
            checklist_lines.extend(
                f"{index}. [ ] {step}" for index, step in enumerate(report["manual_steps"], start=1)
            )
        checklist.write_text("\n".join(checklist_lines) + "\n", encoding="utf-8")

        template = report_dir / "manual_report_template.json"
        template.write_text(
            json.dumps(manual_report_template(report), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def record_step(report: dict[str, Any], name: str, status: str, **extra: Any) -> None:
    report.setdefault("steps", []).append(
        {"name": name, "status": status, **sanitize_report_value(extra)}
    )


def sanitize_report_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): sanitize_report_value(child)
            for key, child in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [sanitize_report_value(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def record_named_result(report: dict[str, Any], name: str, result: dict[str, Any]) -> None:
    step_status = result.get("status", "ok")
    extra = {key: value for key, value in result.items() if key != "status"}
    record_step(report, name, step_status, **extra)


def prepare_disposable_repo(
    args: argparse.Namespace, report: dict[str, Any], env: dict[str, str]
) -> Path:
    worktree = report["report_dir"] / "workdir" / "simple-python-project"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    copy_fixture(args.fixture, worktree)
    record_step(report, "copy fixture", "ok", message=str(worktree))
    for command_record in init_git_repo(worktree, env):
        record_step(report, "git init fixture", "ok", **command_record)
    report["worktree"] = str(worktree)
    return worktree


def run_mock_mode(args: argparse.Namespace, report_dir: Path) -> int:
    env = dogfood_env()
    report: dict[str, Any] = {
        "mode": "mock",
        "status": "running",
        "api_key_required": False,
        "component_valid": False,
        "release_valid": False,
        "release_valid_reason": (
            "Mock CLI component dogfood cannot prove a clean production install or managed runtime."
        ),
        "extension_host": "pending" if args.extension_host != "skip" else "skipped",
        "extension_host_preflight": None,
        "report_dir": report_dir,
        "steps": [],
    }
    try:
        worktree = prepare_disposable_repo(args, report, env)

        verification = run_fixture_verification(worktree, env)
        record_step(report, "fixture verification", "ok", **verification)

        vsix, vsix_record = ensure_vsix(
            args.extension_dir,
            args.package_mode,
            env,
            explicit_vsix=args.vsix_path,
        )
        record_named_result(report, "vsix package", vsix_record)
        report["vsix"] = str(vsix) if vsix else None
        report["vsix_sha256"] = vsix_record.get("vsix_sha256")
        report["source_snapshot_sha256"] = vsix_record.get("source_snapshot_sha256")
        report["extension_under_test"] = "packaged_vsix" if vsix else "source"

        health = validate_bridge_health(parse_cli_command(args.cli_command), env)
        record_step(report, "ide bridge health", "ok", **health)

        preflight = extension_host_preflight(
            args.extension_host,
            vscode_executable=args.vscode_executable,
            env=env,
        )
        report["extension_host_preflight"] = preflight
        record_step(
            report,
            "extension host preflight",
            preflight["status"],
            **{key: value for key, value in preflight.items() if key != "status"},
        )

        host = run_extension_host(
            args.extension_dir,
            env,
            args.extension_host,
            vsix=vsix,
            vscode_executable=args.vscode_executable,
            preflight=preflight,
        )
        record_named_result(report, "cockpit mock extension host", host)
        report["extension_host"] = host["extension_host"]
        report["extension_under_test"] = host.get("extension_under_test", "unknown")
        if "vscode_executable" in host:
            report["vscode_executable"] = host["vscode_executable"]
            report["vscode_executable_source"] = host.get("vscode_executable_source")

        if args.extension_host == "skip":
            report["status"] = "passed_local_smoke"
            report["component_valid"] = False
            report["release_valid"] = False
            report["extension_host"] = "skipped"
            report["release_valid_reason"] = (
                "Extension Host was skipped; this report is neither component-valid nor release-valid. "
                "Rerun with --extension-host run or "
                "--extension-host require-cached for release."
            )
        else:
            report["status"] = "passed"
            report["component_valid"] = True
            report["release_valid"] = False
            report["extension_host"] = "passed"
            report["release_valid_reason"] = (
                "Packaged component Extension Host dogfood passed with a development CLI override; "
                "only installed target VSIX managed-runtime dogfood can be release-valid."
            )
        return_code = 0
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, ExtensionHostDogfoodError):
            record_named_result(report, "cockpit mock extension host", exc.record)
        report["status"] = "failed"
        report["component_valid"] = False
        report["release_valid"] = False
        if report.get("extension_host") not in {"passed", "skipped"}:
            report["extension_host"] = "failed"
        report["release_valid_reason"] = "Dogfood failed."
        report["error"] = truncate(str(exc))
        record_step(report, "failure", "failed", message=report["error"])
        return_code = 1
    finally:
        report["report_dir"] = str(report["report_dir"])
        write_reports(report_dir, report)
        print(f"Dogfood report: {report_dir}")
        print(f"Dogfood status: {report['status']}")
    return return_code


def run_manual_mode(args: argparse.Namespace, report_dir: Path) -> int:
    env = manual_dogfood_env()
    report: dict[str, Any] = {
        "mode": "manual-real-provider",
        "status": "running",
        "api_key_required": True,
        "release_valid": False,
        "release_valid_reason": "Manual real-provider mode writes a checklist; completion is external.",
        "extension_host": "skipped",
        "extension_host_preflight": {
            "status": "skipped",
            "mode": "manual-real-provider",
            "release_valid": False,
            "message": "Manual mode writes a checklist and does not run Extension Host.",
        },
        "report_dir": report_dir,
        "steps": [],
        "manual_steps": manual_steps(),
        "required_checks": manual_required_checks(),
        "completed_report_schema_version": MANUAL_REAL_PROVIDER_REPORT_SCHEMA_VERSION,
    }
    try:
        prepare_disposable_repo(args, report, env)
        vsix, vsix_record = ensure_vsix(
            args.extension_dir,
            args.package_mode,
            env,
            explicit_vsix=args.vsix_path,
        )
        record_named_result(report, "vsix package", vsix_record)
        report["vsix"] = str(vsix) if vsix else None
        report["vsix_sha256"] = vsix_record.get("vsix_sha256")

        code_path = shutil.which("code")
        if code_path:
            record_step(report, "vscode executable", "ok", message=code_path)
        else:
            record_step(report, "vscode executable", "warning", message="`code` not found on PATH")

        record_step(
            report,
            "installed managed runtime health",
            "deferred",
            message=(
                "Manual checklist generation does not run the repository CLI. Health evidence "
                "must come from the candidate's installed-production dogfood summary."
            ),
        )

        report["status"] = "manual_steps_written"
        return_code = 0
    except Exception as exc:  # noqa: BLE001
        report["status"] = "failed"
        report["error"] = truncate(str(exc))
        record_step(report, "failure", "failed", message=report["error"])
        return_code = 1
    finally:
        report["report_dir"] = str(report["report_dir"])
        write_reports(report_dir, report)
        print(f"Dogfood report: {report_dir}")
        print("Manual real-provider checklist:")
        for index, step in enumerate(report["manual_steps"], start=1):
            print(f"{index}. {step}")
        print(f"Dogfood status: {report['status']}")
    return return_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run or prepare the VS Code extension Cockpit dogfood release gate."
    )
    parser.add_argument("--mode", choices=["mock", "manual-real-provider"], default="mock")
    parser.add_argument("--report-root", type=Path, default=DEFAULT_REPORT_ROOT)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--extension-dir", type=Path, default=DEFAULT_EXTENSION_DIR)
    parser.add_argument(
        "--vsix-path",
        type=Path,
        help=(
            "Exact retained VSIX to test. Required for manual-real-provider evidence so the "
            "report binds one target-specific candidate instead of selecting a nearby package."
        ),
    )
    parser.add_argument(
        "--package-mode",
        choices=["auto", "require", "build", "skip"],
        default="auto",
        help="How to obtain the VSIX. CI should use require after npm run package.",
    )
    parser.add_argument(
        "--extension-host",
        choices=["run", "skip", "require-cached"],
        default="run",
        help=(
            "Run Extension Host, skip it for local smoke, or require a cached VS Code executable. "
            "skip is not release-valid."
        ),
    )
    parser.add_argument(
        "--vscode-executable",
        type=Path,
        help=(
            "Path to a cached VS Code executable. Also accepted through "
            "VSCODE_TEST_EXECUTABLE_PATH."
        ),
    )
    parser.add_argument(
        "--cli-command",
        help="Command prefix for the Alysis Code CLI, for example `alysis` or `python -m alysis_code.cli`.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    report_dir = create_report_dir(args.report_root, suffix=args.mode.replace("-", "_"))
    args.fixture = args.fixture.resolve()
    args.extension_dir = args.extension_dir.resolve()
    if args.vsix_path is not None:
        args.vsix_path = args.vsix_path.expanduser().resolve()
    if args.mode == "manual-real-provider" and args.vsix_path is None:
        parser.error(
            "--mode manual-real-provider requires --vsix-path for the exact target candidate"
        )

    if args.mode == "mock":
        return run_mock_mode(args, report_dir)
    return run_manual_mode(args, report_dir)


if __name__ == "__main__":
    raise SystemExit(main())
