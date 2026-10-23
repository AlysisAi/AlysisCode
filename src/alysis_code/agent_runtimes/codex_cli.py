from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import uuid
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from ..command_text import command_text
from ..config import AgentRuntimeSettings
from .base import (
    AuthMethod,
    RuntimeAccountStatus,
    RuntimeCapabilities,
    RuntimeProbeStatus,
    RuntimeTurnRequest,
    RuntimeTurnResult,
)

_VERSION_TIMEOUT_SECONDS = 5.0
_STATUS_TIMEOUT_SECONDS = 10.0
_PROCESS_TREE_GRACE_SECONDS = 2.0
_TIMEOUT_EXIT_CODE = 124
_MISSING_EXECUTABLE_EXIT_CODE = 127
_INVALID_REQUEST_EXIT_CODE = 2
_FORCED_CHATGPT_LOGIN_CONFIG = 'forced_login_method="chatgpt"'
_RUNTIME_ENV_ALLOWLIST = frozenset(
    {
        "ALL_PROXY",
        "BROWSER",
        "CODEX_HOME",
        "COLORTERM",
        "COMSPEC",
        "CURL_CA_BUNDLE",
        "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "NIX_SSL_CERT_FILE",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "REQUESTS_CA_BUNDLE",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "USER",
        "USERPROFILE",
        "WINDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_RUNTIME_DIR",
        "WAYLAND_DISPLAY",
    }
)


class CodexCliRuntimeAdapter:
    """Account and installation boundary for the official Codex CLI.

    The adapter invokes documented Codex commands and intentionally never reads
    Codex's credential files or transports an OAuth token through Alysis Code.
    """

    runtime_id = "openai-codex"
    adapter_id = "codex-cli"
    display_name = "OpenAI Codex account"
    description = "Delegate work to Codex using its official ChatGPT sign-in."
    default_executable = "codex"
    auth_hint = (
        "Browser or device-code sign-in is managed by Codex; Alysis Code never reads its tokens."
    )
    capabilities = RuntimeCapabilities(
        streaming=False,
        session_resume=True,
        image_inputs=True,
        structured_output=True,
        read_only=True,
        workspace_write=True,
    )
    auth_methods = (
        AuthMethod(id="browser", label="Sign in with ChatGPT in a browser"),
        AuthMethod(id="device-code", label="Sign in with a device code"),
    )

    def probe(self, settings: AgentRuntimeSettings) -> RuntimeProbeStatus:
        executable = _resolve_executable(settings.executable)
        if executable is None:
            return RuntimeProbeStatus(
                available=False,
                executable=settings.executable,
                detail="Codex CLI is not installed or is not executable.",
            )
        try:
            result = subprocess.run(
                [executable, "--version"],
                check=False,
                capture_output=True,
                **command_text(),
                timeout=_VERSION_TIMEOUT_SECONDS,
                env=_runtime_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return RuntimeProbeStatus(
                available=False,
                executable=executable,
                detail=f"Codex version check failed: {exc}",
            )
        version = _combined_output(result)
        if result.returncode != 0:
            return RuntimeProbeStatus(
                available=False,
                executable=executable,
                version=version or None,
                detail="Codex version check returned a non-zero exit status.",
            )
        return RuntimeProbeStatus(
            available=True,
            executable=executable,
            version=version or None,
            detail="Codex CLI is available.",
        )

    def account_status(self, settings: AgentRuntimeSettings) -> RuntimeAccountStatus:
        probe = self.probe(settings)
        if not probe.available or not probe.executable:
            return RuntimeAccountStatus(authenticated=False, verified=False, detail=probe.detail)
        try:
            result = subprocess.run(
                [probe.executable, "login", "status"],
                check=False,
                capture_output=True,
                **command_text(),
                timeout=_STATUS_TIMEOUT_SECONDS,
                env=_runtime_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return RuntimeAccountStatus(
                authenticated=False,
                verified=False,
                detail=f"Codex login status check failed: {exc}",
            )
        output = _combined_output(result)
        if result.returncode != 0:
            return RuntimeAccountStatus(
                authenticated=False,
                detail=output or "Codex is not signed in.",
            )
        method = _login_method(output)
        if method != "chatgpt":
            return RuntimeAccountStatus(
                authenticated=False,
                verified=True,
                auth_method_id=method,
                detail=(
                    "Codex is signed in, but not with a ChatGPT account. "
                    "Run `alysis auth login openai-codex --switch-account`."
                ),
            )
        return RuntimeAccountStatus(
            authenticated=True,
            auth_method_id=method,
            account_label=_account_label(output),
            detail=output or "Codex is signed in.",
        )

    def login(
        self,
        settings: AgentRuntimeSettings,
        method_id: str,
    ) -> RuntimeAccountStatus:
        probe = self.probe(settings)
        if not probe.available or not probe.executable:
            return RuntimeAccountStatus(authenticated=False, verified=False, detail=probe.detail)
        normalized = str(method_id or "").strip().lower()
        if normalized not in {method.id for method in self.auth_methods}:
            return RuntimeAccountStatus(
                authenticated=False,
                verified=False,
                detail=f"Unsupported Codex authentication method: {method_id!r}.",
            )
        command = [probe.executable, "login"]
        if normalized == "device-code":
            command.append("--device-auth")
        try:
            result = subprocess.run(command, check=False, env=_runtime_environment())
        except OSError as exc:
            return RuntimeAccountStatus(
                authenticated=False,
                verified=False,
                detail=f"Could not start Codex login: {exc}",
            )
        if result.returncode != 0:
            return RuntimeAccountStatus(
                authenticated=False,
                verified=False,
                detail="Codex login did not complete successfully.",
            )
        return self.account_status(settings)

    def logout(self, settings: AgentRuntimeSettings) -> RuntimeAccountStatus:
        probe = self.probe(settings)
        if not probe.available or not probe.executable:
            return RuntimeAccountStatus(authenticated=False, verified=False, detail=probe.detail)
        try:
            result = subprocess.run(
                [probe.executable, "logout"],
                check=False,
                env=_runtime_environment(),
            )
        except OSError as exc:
            return RuntimeAccountStatus(
                authenticated=True,
                verified=False,
                detail=f"Could not start Codex logout: {exc}",
            )
        if result.returncode != 0:
            return RuntimeAccountStatus(
                authenticated=True,
                verified=False,
                detail="Codex logout did not complete successfully.",
            )
        return RuntimeAccountStatus(authenticated=False, detail="Codex is signed out.")

    def run_turn(
        self,
        settings: AgentRuntimeSettings,
        request: RuntimeTurnRequest,
    ) -> RuntimeTurnResult:
        if not settings.provider_managed_auth:
            raise ValueError("The Codex account runtime requires provider-managed authentication.")
        sandbox = _codex_sandbox(request.mode)
        # Reject malformed resume ids before probing an optional executable.
        # An unavailable CLI must not turn an invalid request into a benign
        # missing-executable result.
        _validated_session_id(request.session_id)
        cwd = Path(request.cwd).expanduser().resolve()
        if not cwd.is_dir():
            return RuntimeTurnResult(
                runtime_id=self.runtime_id,
                command=(),
                exit_code=_INVALID_REQUEST_EXIT_CODE,
                error=f"Delegated runtime cwd is not a directory: {cwd}",
            )
        executable = _resolve_executable(settings.executable)
        if executable is None:
            return RuntimeTurnResult(
                runtime_id=self.runtime_id,
                command=(),
                exit_code=_MISSING_EXECUTABLE_EXIT_CODE,
                error="Codex CLI is not installed or is not executable.",
            )

        with tempfile.TemporaryDirectory(prefix="alysis-codex-turn-") as temp_dir:
            final_message_path = Path(temp_dir) / "last-message.txt"
            command = _build_turn_command(
                executable=executable,
                settings=settings,
                request=request,
                cwd=cwd,
                sandbox=sandbox,
                final_message_path=final_message_path,
            )
            stdout, stderr, exit_code, timed_out, launch_error = _run_process(
                command,
                prompt=request.prompt,
                cwd=cwd,
                timeout_seconds=settings.timeout_seconds,
            )
            events, warnings = _parse_jsonl_events(stdout)
            if request.mode == "review":
                warnings = (
                    "Review mode was enforced as read-only by the Codex runtime.",
                    *warnings,
                )
            final_message = _read_final_message(final_message_path) or _event_final_message(events)
            session_id = (
                None if request.no_log else (_event_session_id(events) or request.session_id)
            )
            usage = _event_usage(events)
            error = launch_error
            if timed_out:
                error = f"Codex turn timed out after {settings.timeout_seconds:g} seconds."
            elif exit_code != 0 and error is None:
                error = (
                    _event_error(events)
                    or stderr.strip()
                    or (f"Codex turn exited with status {exit_code}.")
                )
            return RuntimeTurnResult(
                runtime_id=self.runtime_id,
                command=tuple(command),
                exit_code=exit_code,
                final_message=final_message,
                session_id=session_id,
                stdout=stdout,
                stderr=stderr,
                events=events,
                usage=usage,
                timed_out=timed_out,
                error=error,
                warnings=warnings,
            )


def _codex_sandbox(mode: str) -> str:
    normalized = str(mode or "").strip().lower().replace("_", "-")
    if normalized in {"readonly", "read-only", "review"}:
        return "read-only"
    if normalized in {"auto", "workspace-write"}:
        return "workspace-write"
    if normalized in {"fullaccess", "full-access", "danger-full-access"}:
        raise ValueError(
            "The Codex account runtime does not support fullaccess; use readonly, review, or auto."
        )
    raise ValueError(f"Unsupported delegated runtime mode: {mode!r}.")


def _build_turn_command(
    *,
    executable: str,
    settings: AgentRuntimeSettings,
    request: RuntimeTurnRequest,
    cwd: Path,
    sandbox: str,
    final_message_path: Path,
) -> list[str]:
    session_id = _validated_session_id(request.session_id)
    if session_id:
        command = [
            executable,
            "exec",
            "resume",
            "--json",
            "-c",
            _FORCED_CHATGPT_LOGIN_CONFIG,
            "-c",
            f'sandbox_mode="{sandbox}"',
            "--ignore-user-config",
            "--ignore-rules",
        ]
    else:
        command = [
            executable,
            "exec",
            "--json",
            "--sandbox",
            sandbox,
            "-C",
            os.fspath(cwd),
            "-c",
            _FORCED_CHATGPT_LOGIN_CONFIG,
            "--ignore-user-config",
            "--ignore-rules",
        ]
    command.extend(
        [
            "--skip-git-repo-check",
            "--output-last-message",
            os.fspath(final_message_path),
        ]
    )
    model = str(settings.model or "").strip()
    if model:
        command.extend(["--model", model])
    for image in request.images:
        image_path = Path(image).expanduser()
        if not image_path.is_absolute():
            image_path = cwd / image_path
        command.extend(["--image", os.fspath(image_path.resolve())])
    if request.no_log:
        command.append("--ephemeral")
    if session_id:
        command.append("--")
        command.append(session_id)
    command.append("-")
    return command


def _run_process(
    command: list[str],
    *,
    prompt: str,
    cwd: Path,
    timeout_seconds: float,
) -> tuple[str, str, int, bool, str | None]:
    popen_kwargs: dict[str, Any] = {
        "cwd": os.fspath(cwd),
        "env": _runtime_environment(),
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        **command_text(),
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **popen_kwargs)
    except OSError as exc:
        message = f"Could not start Codex CLI: {exc}"
        return "", message, _MISSING_EXECUTABLE_EXIT_CODE, False, message

    try:
        stdout, stderr = process.communicate(input=str(prompt), timeout=float(timeout_seconds))
    except subprocess.TimeoutExpired as exc:
        partial_stdout = _coerce_process_text(exc.stdout)
        partial_stderr = _coerce_process_text(exc.stderr)
        _terminate_process_tree(process)
        try:
            stdout, stderr = process.communicate()
        except (OSError, ValueError):
            stdout, stderr = partial_stdout, partial_stderr
        return (
            _coerce_process_text(stdout) or partial_stdout,
            _coerce_process_text(stderr) or partial_stderr,
            _TIMEOUT_EXIT_CODE,
            True,
            None,
        )
    except BaseException:
        # KeyboardInterrupt/SystemExit must not orphan a provider process (or
        # any descendants) after Alysis Code returns control to the terminal.
        with suppress(BaseException):
            _terminate_process_tree(process)
        raise
    return (
        _coerce_process_text(stdout),
        _coerce_process_text(stderr),
        int(process.returncode if process.returncode is not None else 1),
        False,
        None,
    )


def _runtime_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = source if source is not None else os.environ
    allowed: dict[str, str] = {}
    for key, value in environment.items():
        normalized = str(key).upper()
        if normalized not in _RUNTIME_ENV_ALLOWLIST:
            continue
        if normalized.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")):
            continue
        allowed[str(key)] = str(value)
    return allowed


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        with suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_PROCESS_TREE_GRACE_SECONDS,
            )
        if process.poll() is None:
            with suppress(OSError):
                process.kill()
        with suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=_PROCESS_TREE_GRACE_SECONDS)
        return

    used_process_group = False
    try:
        os.killpg(process.pid, signal.SIGTERM)
        used_process_group = True
    except ProcessLookupError:
        return
    except OSError:
        with suppress(OSError):
            process.terminate()
    with suppress(OSError, subprocess.TimeoutExpired):
        process.wait(timeout=_PROCESS_TREE_GRACE_SECONDS)
    if used_process_group:
        # The parent may have exited while a child ignored SIGTERM. Because the
        # process started in a dedicated session, killing the group is safe and
        # guarantees that no descendant survives the timed-out turn.
        with suppress(ProcessLookupError, OSError):
            os.killpg(process.pid, signal.SIGKILL)
    elif process.poll() is None:
        with suppress(OSError):
            process.kill()
    with suppress(OSError, subprocess.TimeoutExpired):
        process.wait(timeout=_PROCESS_TREE_GRACE_SECONDS)


def _coerce_process_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _parse_jsonl_events(
    stdout: str,
) -> tuple[tuple[Mapping[str, object], ...], tuple[str, ...]]:
    events: list[Mapping[str, object]] = []
    warnings: list[str] = []
    for line_number, line in enumerate(str(stdout or "").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            warnings.append(f"Ignored invalid Codex JSONL event on line {line_number}.")
            continue
        if not isinstance(parsed, dict):
            warnings.append(f"Ignored non-object Codex JSONL event on line {line_number}.")
            continue
        events.append(parsed)
    return tuple(events), tuple(warnings)


def _read_final_message(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _event_final_message(events: tuple[Mapping[str, object], ...]) -> str:
    for event in reversed(events):
        item = event.get("item")
        if isinstance(item, Mapping) and str(item.get("type") or "") == "agent_message":
            text = _message_text(item)
            if text:
                return text
        event_type = str(event.get("type") or "")
        if event_type in {"agent_message", "assistant_message", "message.completed"}:
            text = _message_text(event)
            if text:
                return text
    return ""


def _message_text(value: Mapping[str, object]) -> str:
    direct = value.get("text") or value.get("message")
    if isinstance(direct, str):
        return direct.strip()
    content = value.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping):
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts)
    return ""


def _event_session_id(events: tuple[Mapping[str, object], ...]) -> str | None:
    for event in events:
        for key in ("thread_id", "session_id"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        thread = event.get("thread")
        if isinstance(thread, Mapping):
            value = thread.get("id")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _event_usage(events: tuple[Mapping[str, object], ...]) -> Mapping[str, object] | None:
    for event in reversed(events):
        usage = event.get("usage")
        if isinstance(usage, Mapping):
            return dict(usage)
    return None


def _event_error(events: tuple[Mapping[str, object], ...]) -> str | None:
    for event in reversed(events):
        event_type = str(event.get("type") or "")
        if event_type not in {"error", "turn.failed", "item.failed"}:
            continue
        error = event.get("error")
        if isinstance(error, Mapping):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        message = event.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return None


def _resolve_executable(configured: str) -> str | None:
    value = str(configured or "").strip()
    if not value:
        return None
    if os.path.basename(value) == value:
        return shutil.which(value)
    path = Path(value).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        return None
    return os.fspath(path)


def _validated_session_id(raw: str | None) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError("Delegated Codex session ids must be UUIDs.") from exc


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    lines = []
    for line in str(result.stdout or result.stderr or "").splitlines():
        if line.strip().casefold().startswith("warning: proceeding"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _login_method(output: str) -> str | None:
    normalized = str(output or "").casefold()
    if "chatgpt" in normalized or "chat gpt" in normalized:
        return "chatgpt"
    if "api key" in normalized or "api-key" in normalized:
        return "api-key"
    return "provider-managed"


def _account_label(output: str) -> str | None:
    for line in str(output or "").splitlines():
        stripped = line.strip()
        if "@" in stripped and len(stripped) <= 200:
            return stripped
    return None


__all__ = ["CodexCliRuntimeAdapter"]
