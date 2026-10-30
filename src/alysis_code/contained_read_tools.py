"""Run workspace readers and approved mutations within the OS sandbox."""

from __future__ import annotations

import base64
import hashlib
import json
import shlex
import sys
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from .config import ConfigError
from .host_read_worker import MAX_REQUEST_BYTES, MAX_RESULT_BYTES, READ_TOOLS, WRITE_TOOLS
from .host_tool_packet import stage_packet
from .macos_access import FileIdentity
from .project_folders import allowed_roots
from .sandbox_runner import build_shell_runner_from_settings, select_shell_sandbox_backend
from .sandbox_settings import ShellSandboxSettings


class ContainedGitDirectoryError(ConfigError):
    """The fixed read worker reported an explicit native Git directory denial."""


def _worker_argv() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--alysis-internal-file-worker"]
    return [sys.executable, "-I", "-B", "-m", "alysis_code.host_read_worker"]


class ContainedReadTools:
    def __init__(
        self,
        settings: ShellSandboxSettings,
        root: Path,
        *,
        opaque_files: tuple[FileIdentity, ...] = (),
    ):
        self.root = root
        self.opaque_files = opaque_files
        self.write_allowed = settings.filesystem_access == "workspace-write"
        self.settings = replace(
            settings,
            mode="strict",
            filesystem_access="read-only",
            network="off",
            network_domains=None,
            private_network_endpoints=(),
            additional_read_files=(),
            unix_sockets=(),
            forward_env=(),
            ssh_agent_socket=None,
            git_metadata_access=False,
            github_auth=False,
            preview_access="auto",
            clear_env=True,
            protect_repo_meta=True,
            safe_git_writes=False,
            unavailable_fallback="refuse",
            windows_directory_discovery=False,
            windows_read_roots=tuple(
                dict.fromkeys(
                    (
                        *settings.windows_read_roots,
                        *[
                            str(Path(p).resolve())
                            for p in (
                                sys.prefix,
                                sys.exec_prefix,
                                sys.base_prefix,
                                sys.base_exec_prefix,
                            )
                        ],
                        str(Path(__file__).parent.resolve()),
                    )
                )
            ),
        )
        self.runner = None
        self.write_runner = None

    @classmethod
    def for_settings(cls, settings: ShellSandboxSettings, root: Path) -> ContainedReadTools | None:
        if settings.mode == "off":
            return None
        # Resolve lazily at first use. Unsupported strict configurations must
        # refuse the read, never silently return to an unsandboxed host reader.
        return cls(settings, root)

    def run(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        cancel_check: Callable[[], bool] | None = None,
        timeout_s: int = 30,
    ) -> dict[str, Any]:
        if name not in READ_TOOLS:
            raise ConfigError("This tool cannot run in the contained read worker")
        raw = self._request(name, arguments)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ConfigError("Contained read request is too large; narrow the request")
        encoded = base64.b64encode(raw).decode("ascii")
        argv = [*_worker_argv(), encoded]
        return self._invoke(argv, write=False, cancel_check=cancel_check, timeout_s=timeout_s)

    def run_write(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        cancel_check: Callable[[], bool] | None = None,
        timeout_s: int = 30,
    ) -> dict[str, Any]:
        if not self.write_allowed:
            raise ConfigError("Contained mutations are disabled by the read-only filesystem policy")
        if name not in WRITE_TOOLS:
            raise ConfigError("This tool cannot run in the contained mutation worker")
        raw = self._request(name, arguments)
        with stage_packet(self.root, raw) as packet:
            # Use cwd-relative names: bubblewrap's primary root is /workspace.
            argv = [
                *_worker_argv(),
                "--request-file",
                packet.name,
                hashlib.sha256(raw).hexdigest(),
            ]
            return self._invoke(argv, write=True, cancel_check=cancel_check, timeout_s=timeout_s)

    def _request(self, name: str, arguments: dict[str, Any]) -> bytes:
        request: dict[str, Any] = {"tool": name, "arguments": arguments}
        if self.opaque_files:
            if name != "fs_read":
                raise ConfigError("Opaque authentication evidence only permits file inspection")
            request["opaque_files"] = [asdict(identity) for identity in self.opaque_files]
        folders = [str(path) for path in allowed_roots(self.root)[1:]]
        if folders:
            request["project_folders"] = folders
        return json.dumps(request, ensure_ascii=True).encode()

    def _invoke(
        self, argv: list[str], *, write: bool, cancel_check, timeout_s: int
    ) -> dict[str, Any]:
        choice = select_shell_sandbox_backend(self.settings)
        if choice.backend not in {"windows-mxc", "windows-native", "bwrap", "seatbelt"}:
            raise ConfigError(
                "No qualified contained file worker is available; no host fallback was used"
            )
        if choice.backend in {"windows-mxc", "windows-native"}:
            command = "& " + " ".join("'" + item.replace("'", "''") + "'" for item in argv)
            command += "; exit $LASTEXITCODE"
        else:
            command = shlex.join(argv)
        if write:
            if self.write_runner is None:
                self.write_runner = build_shell_runner_from_settings(
                    replace(self.settings, filesystem_access="workspace-write"), self.root
                )
            runner = self.write_runner
        else:
            if self.runner is None:
                self.runner = build_shell_runner_from_settings(self.settings, self.root)
            runner = self.runner
        completed = runner.run(
            root=self.root,
            cwd=self.root,
            cmd=command,
            timeout_s=max(1, min(timeout_s, 30)),
            cancel_check=cancel_check,
        )
        if len(completed.stdout) > MAX_RESULT_BYTES + 256:
            raise ConfigError("Contained read returned an oversized result")
        try:
            envelope = json.loads(completed.stdout)
        except (TypeError, ValueError) as exc:
            raise ConfigError("Contained read worker failed; no host fallback was used") from exc
        if (
            completed.returncode != 0
            or not isinstance(envelope, dict)
            or envelope.get("ok") is not True
            or not isinstance(envelope.get("result"), dict)
        ):
            detail = (
                envelope.get("error", "invalid response")
                if isinstance(envelope, dict)
                else "invalid response"
            )
            if isinstance(envelope, dict) and isinstance(envelope.get("stale_path"), str):
                from .tools.fs import StaleFileError

                raise StaleFileError(envelope["stale_path"])
            if (
                not write
                and choice.backend == "windows-mxc"
                and completed.returncode == 1
                and isinstance(envelope, dict)
                and envelope.get("ok") is False
                and isinstance(envelope.get("error"), str)
                and envelope.get("error_code") == "git_directory_access_denied"
            ):
                raise ContainedGitDirectoryError(str(detail)[:2000])
            raise ConfigError(f"Contained read refused: {str(detail)[:2000]}")
        return envelope["result"]
