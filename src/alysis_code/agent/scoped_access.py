"""Reviewed sandbox permissions. Never changes the baseline sandbox configuration.

Model arguments are proposals. Only this host-side admission path may turn a proposal
into runner settings, after path validation and the normal action approval authority.
Operation grants expire with their command tree. Explicit turn grants are held
only in this tool assembly, cleared at the next user message or policy rebuild.
Session grants and human-approved profiles live in protected host storage.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..config import ConfigError
from ..host_file_policy import HostFilePolicy
from ..macos_access import FileIdentity, environment_identity, environment_names, file_identity
from ..macos_network import (
    SocketIdentity,
    network_domains,
    private_network_endpoints,
    socket_identity,
)
from ..project_folders import allowed_roots
from ..sandbox_settings import ShellSandboxSettings
from ..surface import ApprovalRequest
from ..tools.fs import classify_sensitive_path
from .errors import ApprovalDeclinedError
from .sandbox_access_store import SandboxAccessStore


@dataclass(frozen=True, slots=True)
class SandboxAccessApproval(ApprovalRequest):
    """Internal type; untrusted tool metadata cannot opt into external-path review."""

    authentication: bool = False
    # Host-validated identities, never model-supplied preview/metadata or bytes.
    authentication_files: tuple[FileIdentity, ...] = ()


PERMISSIONS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "description": (
        "Request additional access, reviewed under the current "
        "approval mode. The OS sandbox stays on. Use network for downloads/API calls; "
        "local_preview for a session-owned server bound to 127.0.0.1; list exact "
        "folders needed outside the project. Missing read folders are omitted, not granted; "
        "write folders must exist (request their existing parent to create a new directory). "
        "On macOS and Windows, local directory names/metadata can be discovered through shell tools without "
        "extra content access. Then request reads or writes for the discovered target. "
        "The default scope is this operation; "
        "scope=turn reuses access until the next user turn; scope=session until revoked.  Missing OS access may be requested here, "
        "but never bypass an approval or policy denial."
    ),
    "properties": {
        "network": {
            "type": "boolean",
            "description": "Outbound internet for this command and its children; not a domain allowlist. On macOS, combining broad networking with local_preview also enables system certificate validation and its daemon network fetches.",
        },
        "local_preview": {
            "type": "boolean",
            "description": "Localhost access for this command tree, including host access to its preview server. Does not allow LAN ingress. On macOS, combining this with broad network access also enables system certificate validation and its daemon network fetches.",
        },
        "read_folders": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "write_folders": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "reason": {"type": "string", "maxLength": 1000},
        "scope": {"type": "string", "enum": ["operation", "turn", "session"]},
        "profile": {
            "type": "string",
            "maxLength": 64,
            "description": "Apply a previously approved permission profile for this workspace. Use sandbox_permissions to list or revoke profiles. Cannot be combined with new access fields.",
        },
        "save_as": {
            "type": "string",
            "maxLength": 64,
            "description": "Save these exact folder/network permissions as a reusable workspace profile after a person's approval. Authentication inputs, daemon sockets and Git metadata access cannot be saved.",
        },
        "network_domains": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 32,
            "description": "macOS: public DNS destinations for HTTP/HTTPS or CONNECT via an enforced proxy. *.example.com allows subdomains; **.example.com also includes the apex. Direct IP connections and OS trust-service networking stay blocked. Prefer this to broad network=true. Native system TLS validation requires both network=true and local_preview=true because the trust service may fetch certificate URLs outside domain filters; never disable certificate verification.",
        },
        "private_network_endpoints": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 16,
            "description": "macOS: exact private IP:port endpoints through the proxy, e.g. 127.0.0.1:8080. Requires a person for one operation. Does not allow direct sockets, other ports or the rest of the LAN. Link-local metadata services cannot be granted.",
        },
        "unix_sockets": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 8,
            "description": "macOS: exact existing Unix sockets, such as Docker's socket. Requires a person for this operation only: a daemon can act beyond the workspace. Use docker --host unix:///absolute/socket when needed.",
        },
        "read_files": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 16,
            "description": "macOS and Windows MXC: exact canonical files, such as an SSH key, known_hosts or a tool's credential configuration. Requires a person for one operation; never copies their contents or grants siblings. On macOS, approved files under the user's home are linked at their home-relative paths in private HOME. On Windows, use explicit file paths in the command or SSH configuration.",
        },
        "environment": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 16,
            "description": "macOS: names of existing launcher environment variables needed by this command, never secret values. Requires a person for one operation. Missing variables must be configured by the user outside the agent.",
        },
        "ssh_agent": {
            "type": "boolean",
            "description": "macOS: use the launcher's existing SSH agent for this operation after a person's approval. Keys remain in the agent; this does not grant networking or private key files.",
        },
        "github_auth": {
            "type": "boolean",
            "description": "macOS Seatbelt and Windows MXC: use the existing machine-installed GitHub CLI login for one literal git clone/fetch/pull/push/ls-remote command. Requires outbound networking. GitHub SSH remote URLs use HTTPS with this login. The command and its children receive the account credential and can exercise its full authority, not just access one repository. Credential values are never shown or saved by the permission system. Cannot be retained or saved in a profile.",
        },
        "git_metadata": {
            "type": "boolean",
            "description": "macOS: request Git metadata writes for this exact shell command/script when it mixes Git with shell builtins (including cd) or other programs. write_folders does not grant Git metadata access. Prefer cwd with a literal Git command when possible. The whole command is reviewed; other protected app state stays protected. Operation scope only.",
        },
    },
}


@dataclass(frozen=True)
class DirectoryIdentity:
    path: str
    device: int
    inode: int

    def check(self) -> None:
        current = directory_identity(self.path)
        if current != self:
            raise ConfigError(
                "The requested folder changed during approval. No action ran; request access again."
            )


@dataclass(frozen=True)
class ExecutionDirectoryIdentity:
    """Bind an existing cwd without admitting it as an additional permission root."""

    path: str
    device: int
    inode: int

    @classmethod
    def capture(cls, value: str) -> ExecutionDirectoryIdentity:
        path = Path(value)
        try:
            if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_dir():
                raise ConfigError("The execution directory changed; request access again.")
            info = path.stat()
            return cls(str(path), info.st_dev, info.st_ino)
        except OSError as exc:
            raise ConfigError("The execution directory changed; request access again.") from exc

    def check(self) -> None:
        if self.capture(self.path) != self:
            raise ConfigError("The execution directory changed; request access again.")


@dataclass(frozen=True)
class AccessGrant:
    settings: ShellSandboxSettings
    directories: tuple[DirectoryIdentity, ...] = ()
    command: str | None = None
    tool: str | None = None
    sockets: tuple[SocketIdentity, ...] = ()
    cwd: str | None = None
    files: tuple[FileIdentity, ...] = ()
    environment_fingerprint: bytes | None = field(default=None, repr=False)

    validate_current: Callable[[], None] | None = field(default=None, repr=False, compare=False)
    execution_directory: ExecutionDirectoryIdentity | None = None

    def check(self) -> None:
        if self.validate_current is not None:
            self.validate_current()
        if self.execution_directory is not None:
            self.execution_directory.check()
        for directory in self.directories:
            directory.check()
        for item in self.sockets:
            item.check()
        for item in self.files:
            item.check()
        if (
            self.environment_fingerprint is not None
            and environment_identity(self.settings.forward_env) != self.environment_fingerprint
        ):
            raise ConfigError(
                "The requested environment changed during approval. Request access again."
            )


def _reject_metadata_permission_root(path: Path) -> None:
    from ..skills.discovery import project_skill_root_relative_paths
    from ..windows_sandbox import _PROTECTED_DIRS

    protected = {name.casefold() for name in _PROTECTED_DIRS}
    protected.update(parts[0].casefold() for parts in project_skill_root_relative_paths())
    if any(part.casefold() in protected for part in path.parts):
        raise ConfigError(
            "Agent settings, instruction and repository metadata cannot become permission roots. "
            "Request their ordinary project folder instead; its protected paths stay protected."
        )


def directory_identity(value: str) -> DirectoryIdentity:
    if not isinstance(value, str) or not value or len(value) > 32760 or "\0" in value:
        raise ConfigError("Sandbox permissions require an absolute existing folder.")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ConfigError(
            "Sandbox permissions require an absolute folder without parent traversal."
        )
    _reject_metadata_permission_root(path)
    if os.name == "nt" and (not path.drive or path.drive.startswith("\\") or ":" in value[2:]):
        raise ConfigError("Sandbox permission folders must be on a local drive.")
    for ancestor in (path, *path.parents):
        info = ancestor.lstat()
        if (
            not ancestor.is_dir()
            or ancestor.is_symlink()
            or getattr(info, "st_file_attributes", 0) & 0x400
        ):
            raise ConfigError("Sandbox permission folders cannot contain links or reparse points.")
    canonical = path.resolve(strict=True)
    home = Path.home().resolve()
    if canonical == Path(canonical.anchor) or home.is_relative_to(canonical):
        raise ConfigError(
            "Request a specific project/tool folder for file contents or edits, not an entire "
            "drive, home or user tree. On macOS, discover directory names first with shell "
            "tools without an additional read grant, then request the target folder."
        )
    if classify_sensitive_path(str(canonical)).sensitive:
        raise ConfigError(
            "Credential and sensitive folders cannot receive automatic sandbox grants."
        )
    info = canonical.stat()
    return DirectoryIdentity(str(canonical), info.st_dev, info.st_ino)


class ScopedAccess:
    def __init__(
        self,
        *,
        root: Path,
        settings: ShellSandboxSettings,
        approve: Callable[[ApprovalRequest], Any],
        enabled: bool,
        policy: HostFilePolicy | None,
        record: Callable[[str, dict], None],
        events_since: Callable | None = None,
        access_store: SandboxAccessStore | None = None,
        validate_reuse: Callable[[ApprovalRequest], None] | None = None,
    ):
        self.root = root.resolve()
        self.settings = settings
        self.approve = approve
        self.enabled = enabled
        self.policy = policy
        self.record = record
        self.events_since = events_since
        self.access_store = access_store
        self.validate_reuse = validate_reuse
        self._cursor = 0
        self._generation = 0
        self._turn_grant: AccessGrant | None = None
        self._turn_profile: tuple[str, dict] | None = None
        self._current_profile: tuple[str, dict] | None = None

    @staticmethod
    def _profile_fingerprint(payload: dict) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _payload(
        self,
        grant: AccessGrant,
        *,
        profile: str | None = None,
        source_profile: tuple[str, dict] | None = None,
    ) -> dict:
        # Extending a retained grant does not mint independent authority for
        # the access inherited from its profile. Keep that exact revision
        # revocable across engine restarts. Explicit save_as creates a new
        # person-approved profile and therefore a new source of authority.
        if (
            grant.settings.unix_sockets
            or grant.settings.private_network_endpoints
            or grant.settings.forward_env
            or grant.settings.additional_read_files
            or grant.settings.git_metadata_access
            or grant.settings.github_auth
        ):
            raise ConfigError(
                "Authentication, daemon and Git metadata permissions cannot be retained."
            )
        identities = {item.path: asdict(item) for item in grant.directories}
        return {
            "read_folders": [
                identities[p]
                for p in dict.fromkeys(
                    (*grant.settings.additional_read_roots, *grant.settings.windows_read_roots)
                )
                if p
                not in (*self.settings.additional_read_roots, *self.settings.windows_read_roots)
            ],
            "write_folders": [
                identities[p]
                for p in grant.settings.additional_workspace_roots
                if p not in self.settings.additional_workspace_roots
            ],
            "network": grant.settings.network == "on",
            "network_domains": list(grant.settings.network_domains)
            if grant.settings.network_domains is not None
            else None,
            "local_preview": grant.settings.preview_access == "local",
            "profile": profile or (source_profile[0] if source_profile else None),
            **(
                {"profile_fingerprint": self._profile_fingerprint(source_profile[1])}
                if source_profile and profile is None
                else {}
            ),
        }

    def _restore(self, payload: dict) -> AccessGrant:
        if set(payload) - {"profile_fingerprint", "profile_revision"} != {
            "read_folders",
            "write_folders",
            "network",
            "network_domains",
            "local_preview",
            "profile",
        }:
            raise ConfigError("Invalid saved sandbox permission fields.")
        if type(payload["network"]) is not bool or type(payload["local_preview"]) is not bool:
            raise ConfigError("Invalid saved sandbox network policy.")
        if payload["profile"] is not None:
            SandboxAccessStore.profile_name(payload["profile"])
        if "profile_fingerprint" in payload and (
            payload["profile"] is None
            or not isinstance(payload["profile_fingerprint"], str)
            or len(payload["profile_fingerprint"]) != 64
            or any(c not in "0123456789abcdef" for c in payload["profile_fingerprint"])
        ):
            raise ConfigError("Invalid saved sandbox profile identity.")
        if "profile_revision" in payload and (
            payload["profile"] is None
            or not isinstance(payload["profile_revision"], str)
            or len(payload["profile_revision"]) != 32
            or any(c not in "0123456789abcdef" for c in payload["profile_revision"])
        ):
            raise ConfigError("Invalid saved sandbox profile revision.")
        folders = {}
        for kind in ("read_folders", "write_folders"):
            values = payload[kind]
            if not isinstance(values, list) or len(values) > 64:
                raise ConfigError("Invalid saved sandbox folders.")
            items = []
            for value in values:
                if not isinstance(value, dict) or set(value) != {"path", "device", "inode"}:
                    raise ConfigError("Invalid saved folder identity.")
                item = directory_identity(value["path"])
                if asdict(item) != value:
                    raise ConfigError("A saved permission folder changed; request access again.")
                if self.policy is not None:
                    self.policy.guard_read(self.root, item.path)
                    if kind == "write_folders":
                        self.policy.guard_write(self.root, item.path)
                items.append(item)
            folders[kind] = tuple(items)
        domains = (
            network_domains(payload["network_domains"])
            if payload["network_domains"] is not None
            else None
        )
        if domains is not None and not payload["network"]:
            raise ConfigError("Invalid saved sandbox domain policy.")
        from ..sandbox_runner import select_shell_sandbox_backend

        backend = select_shell_sandbox_backend(self.settings).backend
        if backend not in {"windows-mxc", "seatbelt"}:
            raise ConfigError("Saved command permissions require Windows MXC or macOS Seatbelt.")
        if domains is not None and backend != "seatbelt":
            raise ConfigError("Domain grants currently require macOS Seatbelt.")
        reads = tuple(item.path for item in folders["read_folders"])
        settings = replace(
            self.settings,
            backend=backend,
            unavailable_fallback="refuse",
            network="on" if payload["network"] else "off",
            network_domains=domains,
            preview_access="local" if payload["local_preview"] else self.settings.preview_access,
            additional_read_roots=tuple(
                dict.fromkeys((*self.settings.additional_read_roots, *reads))
            )
            if backend == "seatbelt"
            else self.settings.additional_read_roots,
            windows_read_roots=tuple(dict.fromkeys((*self.settings.windows_read_roots, *reads)))
            if backend == "windows-mxc"
            else self.settings.windows_read_roots,
            additional_workspace_roots=tuple(
                dict.fromkeys(
                    (
                        *self.settings.additional_workspace_roots,
                        *(item.path for item in folders["write_folders"]),
                    )
                )
            ),
        )
        return AccessGrant(
            settings, tuple(dict.fromkeys((*folders["read_folders"], *folders["write_folders"])))
        )

    def _reuse(self, grant: AccessGrant, command: str, tool: str, cwd: str | None) -> AccessGrant:
        if self.validate_reuse is not None and grant.settings != self.settings:
            self.validate_reuse(
                SandboxAccessApproval(
                    kind="sandbox_access",
                    reason="Use previously approved sandbox access",
                    command=command,
                    files=[item.path for item in grant.directories],
                    preview=json.dumps(
                        {
                            "tool": tool,
                            "cwd": cwd or str(self.root),
                            "retained_access": self._payload(grant),
                        }
                    ),
                    metadata={"access": "write"},
                )
            )
        return grant

    def manage(self, args: dict) -> dict:
        if not self.enabled or self.access_store is None:
            raise ConfigError("Reusable sandbox permissions are unavailable in this context.")
        action = args.get("action")
        if action == "revoke":
            profile = args.get("profile")
            if profile is not None:
                self.access_store.save(None, self.access_store.profile_name(profile))
            self.access_store.save(None)
            self._turn_grant = None
            self._turn_profile = None
            self._generation += 1
            self.record(
                "sandbox_access_revoked",
                {
                    "profile": profile,
                    "running_children": "Launch-time access remains until the process is stopped.",
                },
            )
            return {
                "revoked": True,
                "profile": profile,
                "running_children_keep_launch_access": True,
            }
        if action != "list":
            raise ConfigError("Sandbox permissions support list or revoke.")
        return {"session": self.access_store.load(), "profiles": self.access_store.profiles()}

    def _current(self) -> AccessGrant:
        if self.events_since is not None:
            events, self._cursor = self.events_since(self._cursor)
            if any(event.get("type") == "user_message" for event in events):
                self._turn_grant = None
                self._turn_profile = None
                self._generation += 1
        session_grant = None
        session_profile = None
        try:
            if self.enabled and self.access_store is not None:
                if self._turn_profile is not None:
                    name, original = self._turn_profile
                    if self.access_store.load(name) != original:
                        self._turn_grant = None
                        self._turn_profile = None
                        self._generation += 1
                payload = self.access_store.load()
                if payload is not None:
                    profile = payload.get("profile")
                    original = self.access_store.load(profile) if profile is not None else None
                    matches = original is not None and (
                        self._profile_fingerprint(original) == payload["profile_fingerprint"]
                        if "profile_fingerprint" in payload
                        else original == payload
                    )
                    if profile is not None and not matches:
                        self.access_store.save(None)
                        self._generation += 1
                    else:
                        session_grant = self._restore(payload)
                        session_profile = (profile, original) if profile is not None else None
            grant = self._turn_grant or session_grant or AccessGrant(self.settings)
            grant.check()
            self._current_profile = self._turn_profile if self._turn_grant else session_profile
        except (ConfigError, OSError):
            self._turn_grant = None
            self._turn_profile = None
            # Discard stale session authority so a subsequent request can recover.
            # Invalid profiles remain visible for explicit revocation/replacement.
            if self.access_store is not None:
                self.access_store.save(None)
            self._generation += 1
            raise
        return grant

    def prepare(
        self,
        proposal: Any,
        *,
        command: str,
        tool: str,
        files: tuple[str, ...] = (),
        cwd: str | None = None,
    ) -> AccessGrant:
        current = self._current()
        generation = self._generation
        source_profile = self._current_profile
        if proposal is None:
            grant = self._reuse(current, command, tool, cwd)
            if self._current() != current or generation != self._generation:
                raise ConfigError("Sandbox access changed during approval. Request access again.")
            return grant
        if not self.enabled:
            raise ConfigError("Scoped access requests are unavailable in this execution context.")
        if not isinstance(proposal, dict) or set(proposal) - set(PERMISSIONS_SCHEMA["properties"]):
            raise ConfigError("Invalid scoped sandbox permission request.")
        if not isinstance(command, str) or not command.strip() or len(command) > 3500:
            raise ConfigError("Scoped access needs a bounded command that can be reviewed in full.")
        if self.settings.mode != "strict" or self.settings.filesystem_access != "workspace-write":
            raise ConfigError(
                "Scoped access requires the strict writable sandbox; it cannot override read-only policy."
            )
        for key in ("network", "local_preview", "ssh_agent", "git_metadata", "github_auth"):
            if key in proposal and type(proposal[key]) is not bool:
                raise ConfigError(f"{key} must be a boolean.")
        reason = proposal.get("reason", "")
        if not isinstance(reason, str) or len(reason) > 1000:
            raise ConfigError("The access reason must be a short string.")
        scope = proposal.get("scope", "operation")
        if (
            not isinstance(scope, str)
            or scope not in {"operation", "turn", "session"}
            or (scope == "turn" and self.events_since is None)
            or (scope == "session" and self.access_store is None)
        ):
            raise ConfigError(
                "Turn/session grants require an available conversation permission store."
            )
        if "profile" in proposal:
            if self.access_store is None or set(proposal) - {"profile", "scope", "reason"}:
                raise ConfigError("A saved profile cannot be combined with new access fields.")
            name = self.access_store.profile_name(proposal["profile"])
            payload = self.access_store.load(name)
            if payload is None:
                raise ConfigError(
                    "No applicable permission profile exists; request and save access first."
                )
            grant = self._reuse(self._restore(payload), command, tool, cwd)
            if self.access_store.load(name) != payload:
                raise ConfigError("The permission profile changed during approval. Apply it again.")
            if self._current() != current or generation != self._generation:
                raise ConfigError("Sandbox access changed during approval. Request access again.")
            grant.check()
            self.record("sandbox_access_profile_applied", {"profile": name, "scope": scope})
            if scope == "session":
                self.access_store.save(payload)
                self._turn_grant = None
                self._turn_profile = None
            elif scope == "turn":
                self._turn_grant = grant
                self._turn_profile = (name, payload)
            return grant
        if scope == "operation" and not (set(proposal) - {"scope", "reason"}):
            # Descriptive fields request no new authority. Reuse still checks
            # any retained grant and its revocation/review requirements.
            return self.prepare(None, command=command, tool=tool, files=files, cwd=cwd)
        save_as = proposal.get("save_as")
        if save_as is not None:
            if self.access_store is None:
                raise ConfigError("Saved permission profiles are unavailable in this context.")
            save_as = self.access_store.profile_name(save_as)
        domains = (
            network_domains(proposal["network_domains"]) if "network_domains" in proposal else None
        )
        private_endpoints = private_network_endpoints(proposal.get("private_network_endpoints", []))
        if (domains is not None or private_endpoints) and proposal.get("network") is False:
            raise ConfigError("Domain access cannot be combined with network=false.")
        socket_values = proposal.get("unix_sockets", [])
        if (
            not isinstance(socket_values, list)
            or len(socket_values) > 8
            or any(not isinstance(v, str) for v in socket_values)
        ):
            raise ConfigError("Request at most eight exact daemon sockets.")
        sockets = tuple(socket_identity(value) for value in dict.fromkeys(socket_values))
        ssh_socket = None
        if proposal.get("ssh_agent"):
            ssh_socket = socket_identity(os.environ.get("SSH_AUTH_SOCK", ""))
            sockets = tuple(dict.fromkeys((*sockets, ssh_socket)))
        file_values = proposal.get("read_files", [])
        if (
            not isinstance(file_values, list)
            or len(file_values) > 16
            or any(not isinstance(v, str) for v in file_values)
        ):
            raise ConfigError("Request at most sixteen exact read files.")
        read_files = tuple(file_identity(value) for value in dict.fromkeys(file_values))
        environment = environment_names(proposal.get("environment", []))
        environment_fingerprint = environment_identity(environment) if environment else None
        if (
            read_files
            or environment
            or private_endpoints
            or proposal.get("git_metadata")
            or proposal.get("github_auth")
        ) and scope != "operation":
            raise ConfigError(
                "Authentication inputs and Git metadata grants are limited to one operation."
            )
        if sockets and scope != "operation":
            raise ConfigError("Daemon socket access is limited to one approved operation.")
        if save_as is not None and (
            sockets
            or read_files
            or environment
            or private_endpoints
            or proposal.get("git_metadata")
            or proposal.get("github_auth")
        ):
            raise ConfigError(
                "Authentication, daemon and Git metadata permissions cannot be saved."
            )
        from ..sandbox_runner import select_shell_sandbox_backend

        choice = select_shell_sandbox_backend(self.settings)
        if choice.backend not in {"windows-mxc", "seatbelt"}:
            raise ConfigError("Scoped command permissions require Windows MXC or macOS Seatbelt.")
        if proposal.get("github_auth"):
            if choice.backend not in {"windows-mxc", "seatbelt"} or tool != "shell_run":
                raise ConfigError(
                    "GitHub authentication requires macOS Seatbelt or Windows MXC shell_run."
                )
            if not proposal.get("network") and current.settings.network != "on":
                raise ConfigError(
                    "GitHub authentication also requires outbound network permission."
                )
            if choice.backend == "seatbelt":
                from ..macos_github_auth import validate_command
            else:
                from ..git_github_auth import validate_command

            validate_command(command)
        if (
            domains is not None
            or private_endpoints
            or sockets
            or environment
            or proposal.get("git_metadata")
        ) and choice.backend != "seatbelt":
            raise ConfigError("Domain and daemon-socket grants currently require macOS Seatbelt.")
        folders: dict[str, tuple[DirectoryIdentity, ...]] = {}
        missing_reads: list[str] = []
        for key in ("read_folders", "write_folders"):
            values = proposal.get(key, [])
            if not isinstance(values, list) or len(values) > 8:
                raise ConfigError("Request at most eight exact folders per permission type.")
            if any(not isinstance(value, str) for value in values):
                raise ConfigError("Permission folders must be strings.")
            identities_list = []
            for value in dict.fromkeys(values):
                try:
                    identities_list.append(directory_identity(value))
                except FileNotFoundError as exc:
                    if key == "read_folders":
                        # Absence grants nothing, even if another process later
                        # creates this path. Other valid read roots still work.
                        missing_reads.append(value)
                    else:
                        raise ConfigError(
                            f"Write folder does not exist: {value}. Request its existing "
                            "parent folder, then create the destination inside that scope."
                        ) from exc
            identities = tuple(identities_list)
            for item in identities:
                if self.policy is not None:
                    # Seatbelt checks every hardlink alias and resolved symlink
                    # under the final union of approved roots before execution.
                    self.policy.guard_read(
                        self.root, item.path, recursive=choice.backend != "seatbelt"
                    )
                    if key == "write_folders":
                        self.policy.guard_write(self.root, item.path)
            folders[key] = identities
        reads = tuple(item.path for item in folders["read_folders"])
        writes = tuple(item.path for item in folders["write_folders"])
        baseline = current.settings
        admitted = replace(
            baseline,
            backend=choice.backend,
            unavailable_fallback="refuse",
            network="on"
            if proposal.get("network") or domains is not None or private_endpoints
            else baseline.network,
            network_domains=domains
            if domains is not None
            else (None if proposal.get("network") else baseline.network_domains),
            private_network_endpoints=private_endpoints,
            unix_sockets=tuple(item.path for item in sockets),
            additional_read_files=tuple(item.path for item in read_files),
            forward_env=environment,
            ssh_agent_socket=ssh_socket.path if ssh_socket else None,
            git_metadata_access=bool(proposal.get("git_metadata")),
            github_auth=bool(proposal.get("github_auth")),
            preview_access="local" if proposal.get("local_preview") else baseline.preview_access,
            windows_read_roots=tuple(dict.fromkeys((*baseline.windows_read_roots, *reads)))
            if choice.backend == "windows-mxc"
            else baseline.windows_read_roots,
            additional_read_roots=tuple(dict.fromkeys((*baseline.additional_read_roots, *reads)))
            if choice.backend == "seatbelt"
            else baseline.additional_read_roots,
            additional_workspace_roots=tuple(
                dict.fromkeys((*baseline.additional_workspace_roots, *writes))
            ),
        )
        system_trust = (
            choice.backend == "seatbelt"
            and admitted.network == "on"
            and admitted.preview_access == "local"
            and admitted.network_domains is None
            and not admitted.private_network_endpoints
        )
        details = {
            "tool": tool,
            "cwd": cwd or str(self.root),
            "scope": "this operation and its child processes only"
            if scope == "operation"
            else (
                "this user turn and its child processes; expires at the next user message"
                if scope == "turn"
                else "this chat until revoked or its workspace/sandbox policy changes; survives restart"
            ),
            "save_as": save_as,
            "retained_access": self._payload(current)
            if current.settings != self.settings
            else None,
            "read_folders": reads,
            "missing_read_folders_not_granted": missing_reads,
            "write_folders": writes,
            "outbound_network": proposal.get("network", False)
            or domains is not None
            or bool(private_endpoints)
            or current.settings.network == "on",
            "network_domains": domains,
            "private_network_endpoints": private_endpoints,
            "unix_sockets": [item.path for item in sockets],
            "read_files": [item.path for item in read_files],
            "environment_names": environment,
            "ssh_agent": ssh_socket.path if ssh_socket else None,
            "git_metadata_write": bool(proposal.get("git_metadata")),
            "github_auth": "Use the saved github.com CLI account over HTTPS, including GitHub SSH URLs. The command and its children receive the account credential with its full authority, not a repository-limited token."
            if proposal.get("github_auth")
            else None,
            "authentication_authority": "This command and its children can use these authentication inputs. Credential values are not shown or saved by the permission system. Approve only the exact intended command."
            if read_files or environment or ssh_socket or proposal.get("github_auth")
            else None,
            "daemon_authority": "The daemon may read/write host files, run containers or reach networks beyond this sandbox. This approval does not sandbox the daemon."
            if socket_values
            else None,
            "system_trust_authority": "macOS certificate validation can fetch certificate and revocation URLs through the system trust daemon, outside the command's network filters."
            if system_trust
            else None,
            "host_loopback": admitted.preview_access == "local",
            "lan_ingress": False,
            "sandbox": f"strict {choice.backend}",
            "reason": reason,
            "files": files,
        }
        preview = json.dumps(details, ensure_ascii=False)
        if len(preview) > 3500:
            raise ConfigError(
                "The access request is too large to review exactly. Request fewer folders."
            )
        summary = [
            "Approve additional sandbox access for "
            + {
                "operation": "this operation",
                "turn": "this user turn",
                "session": "this chat until revoked",
            }[scope]
        ]
        for label, values in (
            ("public destinations", domains or ()),
            ("private endpoints", private_endpoints),
            ("read folders", reads),
            ("write folders", writes),
            ("read files", tuple(item.path for item in read_files)),
            ("environment variable names", environment),
            ("daemon sockets", tuple(socket_values)),
            ("SSH agent", (ssh_socket.path,) if ssh_socket else ()),
        ):
            if values:
                summary.append(label + ": " + ", ".join(values))
        if proposal.get("network") and domains is None:
            summary.append("broad outbound network access")
        if proposal.get("local_preview"):
            summary.append("host loopback access")
        if system_trust:
            summary.append("system certificate validation, including daemon network fetches")
        if proposal.get("git_metadata"):
            summary.append("Git metadata writes for the whole command")
        if proposal.get("github_auth"):
            summary.append(
                "GitHub account authentication over HTTPS for this command and its children"
            )
        if save_as:
            summary.append(f"also save workspace profile {save_as!r} for reuse")
        if current.settings != self.settings:
            summary.append("includes the previously approved access shown below")
        authentication = bool(
            read_files or environment or ssh_socket or proposal.get("github_auth")
        )
        # Bind the execution directory before either human card. A later symlink
        # swap must not make a remote write use a different repository.
        authentication_cwd = (
            ExecutionDirectoryIdentity.capture(cwd or str(self.root)) if authentication else None
        )
        decision = self.approve(
            SandboxAccessApproval(
                kind="sandbox_access",
                authentication=authentication,
                authentication_files=read_files,
                command=command,
                reason="; ".join(summary) + ".",
                preview=preview,
                files=list(
                    dict.fromkeys((*files, *reads, *writes, *(item.path for item in read_files)))
                ),
                metadata={
                    "access": "control"
                    if sockets
                    or read_files
                    or environment
                    or save_as
                    or proposal.get("github_auth")
                    else (
                        "write"
                        if writes or proposal.get("git_metadata")
                        else (
                            "network"
                            if admitted.network == "on" or proposal.get("local_preview")
                            else "read"
                        )
                    ),
                    "cwd": cwd or str(self.root),
                    "scope": scope,
                    **(
                        {
                            "allow_for_session_disabled": True,
                            "mandatory_explicit_approval": True,
                            "always_ask_person": True,
                        }
                        if read_files or environment or ssh_socket or proposal.get("github_auth")
                        else {}
                    ),
                    **(
                        {"mandatory_explicit_approval": True, "allow_for_session_disabled": True}
                        if socket_values or private_endpoints or save_as is not None
                        else {}
                    ),
                },
            )
        )
        if not decision.allow:
            raise ApprovalDeclinedError("sandbox_access")
        latest = self._current()
        if generation != self._generation or latest != current:
            raise ConfigError("The conversation changed during approval. Request access again.")
        for identities in folders.values():
            for item in identities:
                item.check()

        def validate_current() -> None:
            if self._current() != current or self._generation != generation:
                raise ConfigError("Sandbox access changed before dispatch. Request access again.")

        grant = AccessGrant(
            admitted,
            tuple(
                dict.fromkeys(
                    (
                        *current.directories,
                        *(item for items in folders.values() for item in items),
                    )
                )
            ),
            command,
            tool,
            sockets,
            cwd,
            read_files,
            environment_fingerprint,
            validate_current if authentication else None,
            authentication_cwd,
        )
        grant.check()
        self.record("sandbox_access_granted", details)
        saved_payload = None
        if save_as is not None:
            # Recreating the same name/permissions after revocation must not
            # resurrect old consumers that have not yet observed the tombstone.
            saved_payload = {
                **self._payload(grant, profile=save_as),
                "profile_revision": uuid4().hex,
            }
            self.access_store.save(saved_payload, save_as)
        if scope == "session":
            self.access_store.save(
                saved_payload or self._payload(grant, source_profile=source_profile)
            )
            self._turn_grant = None
            self._turn_profile = None
        if scope == "turn":
            self._turn_grant = replace(grant, command=None, tool=None)
            self._turn_profile = (save_as, saved_payload) if save_as else source_profile
        return grant

    def external_file_root(self, name: str, args: dict[str, Any]) -> DirectoryIdentity | None:
        fields = {
            "fs_read": ("path",),
            "fs_write": ("path",),
            "fs_edit": ("path",),
            "fs_delete": ("path",),
            "fs_mkdir": ("path",),
            "fs_move": ("source_path", "destination_path"),
            "fs_copy": ("source_path", "destination_path"),
            "fs_list": ("root_path",),
            "search_rg": ("root_path",),
            "symbol_search": ("root_path",),
        }.get(name, ())
        values = [args[key] for key in fields if isinstance(args.get(key), str) and args[key]]
        if name == "fs_delete" and isinstance(args.get("paths"), list):
            values.extend(value for value in args["paths"] if isinstance(value, str))
        paths = [
            Path(value) if Path(value).is_absolute() else self.root / value for value in values
        ]
        # Host-selected project folders are already authorized for this tool call.
        # Read the live scope here so detached folders require a new approval.
        roots = allowed_roots(self.root)
        external = [
            path for path in paths if not any(path.resolve().is_relative_to(root) for root in roots)
        ]
        if not external:
            return None
        if (
            not self.enabled
            or self.settings.mode != "strict"
            or self.settings.filesystem_access != "workspace-write"
        ):
            raise ConfigError("External file access is unavailable in this execution context.")
        # No common-ancestor widening for copies/moves between unrelated roots.
        # macOS exposes ordinary folders through aliases such as /var ->
        # /private/var. Review and bind the canonical destination, not the alias.
        # Recheck the mapping after approval so retargeting never changes access.
        # Preserve lexical metadata exclusions too: a .git/.agents alias must
        # not turn its target into an ordinary delegated workspace.
        for path in external:
            _reject_metadata_permission_root(path)
        resolved_paths = [path.resolve() for path in paths]
        target = external[0].resolve() if sys.platform == "darwin" else external[0]
        candidate = target if name in {"fs_list", "search_rg", "symbol_search"} else target.parent
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        identity = directory_identity(str(candidate))
        candidate = Path(identity.path)
        if any(not path.resolve().is_relative_to(candidate) for path in paths):
            raise ConfigError(
                "Cross-folder copy/move needs shell_run with the exact read/write folders requested."
            )
        read_only = name in {"fs_read", "fs_list", "search_rg", "symbol_search"}
        current = self._current()
        generation = self._generation
        granted = any(
            candidate.is_relative_to(Path(p).resolve())
            for p in (
                *current.settings.additional_workspace_roots,
                *(current.settings.additional_read_roots if read_only else ()),
            )
        )
        if not granted:
            # Include the proposed edit, not only its destination, in the
            # reviewer's bounded action. Internal dispatch/cancellation objects
            # are neither model arguments nor part of the requested operation.
            public_args = {
                key: value
                for key, value in args.items()
                if isinstance(key, str) and not key.startswith("_")
            }
            self.prepare(
                {
                    "read_folders" if read_only else "write_folders": [str(candidate)],
                    "reason": f"Run {name} on the explicitly requested external path",
                },
                command=f"{name} {json.dumps(public_args, ensure_ascii=False)}",
                tool=name,
                files=tuple(str(p) for p in paths),
            )
        if granted:
            self._reuse(
                current,
                f"{name} {json.dumps({k: v for k, v in args.items() if not k.startswith('_')}, ensure_ascii=False)}",
                name,
                None,
            )
            if self._current() != current or generation != self._generation:
                raise ConfigError("Sandbox access changed during approval. Request access again.")
        identity.check()
        if [path.resolve() for path in paths] != resolved_paths:
            raise ConfigError(
                "The requested path changed during approval. No action ran; request access again."
            )
        return identity
