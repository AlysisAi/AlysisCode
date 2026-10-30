from __future__ import annotations

import json
import math
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .branding import default_sandbox_docker_image, env_get
from .config import AppConfig, ConfigError

_VALID_MODES = {"off", "warn", "strict"}
_VALID_BACKENDS = {"auto", "bwrap", "docker", "seatbelt", "windows-native", "windows-mxc"}
# What happens when the mode asks for a sandbox and no backend is usable on this machine:
# "refuse" (no command runs), or "ask" (each command runs on the host only after the user
# approves that exact command). A host sets "ask" only when it can show those approvals.
_VALID_UNAVAILABLE_FALLBACKS = {"refuse", "ask"}
_VALID_NETWORK = {"off", "on"}
_VALID_BWRAP_PROFILES = {"compat", "hardened"}
_VALID_PREVIEW_ACCESS = {"auto", "local", "lan"}
DEFAULT_SHELL_SANDBOX_DOCKER_IMAGE = default_sandbox_docker_image("dev")


@dataclass(frozen=True)
class ShellSandboxSettings:
    mode: str = "strict"
    backend: str = "auto"
    network: str = "off"
    filesystem_access: str = "workspace-write"
    windows_read_roots: tuple[str, ...] = ()
    # Shell discovery is metadata-only. Fixed contained file workers need only
    # their explicit roots, including runtime paths beside private app state.
    windows_directory_discovery: bool = True
    additional_workspace_roots: tuple[str, ...] = ()
    # Host-admitted, operation-local read grants for Seatbelt. Not loaded from
    # project config/environment and never promoted to writable workspaces.
    additional_read_roots: tuple[str, ...] = ()
    # Reviewed Seatbelt capabilities; never populated from project configuration.
    network_domains: tuple[str, ...] | None = None
    private_network_endpoints: tuple[str, ...] = ()
    unix_sockets: tuple[str, ...] = ()
    additional_read_files: tuple[str, ...] = ()
    forward_env: tuple[str, ...] = ()
    ssh_agent_socket: str | None = None
    git_metadata_access: bool = False
    # Reviewed contained Git authentication for one operation. Never read
    # from project config or persisted in a permission profile.
    github_auth: bool = False
    preview_access: str = "auto"
    bwrap_profile: str = "hardened"
    docker_image: str = DEFAULT_SHELL_SANDBOX_DOCKER_IMAGE
    clear_env: bool = True
    docker_pids_limit: int | None = None
    docker_memory: str | None = None
    docker_cpus: str | None = None
    docker_read_only: bool = False
    protect_repo_meta: bool = True
    safe_git_writes: bool = True
    docker_env_allowlist: tuple[str, ...] = ()
    background_max_concurrent: int = 4
    background_output_max_lines: int = 2000
    background_output_max_bytes: int = 256 * 1024
    background_kill_timeout_s: float = 10.0
    unavailable_fallback: str = "refuse"


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ADDITIONAL_WORKSPACE_ROOTS_ENV = "ALYSIS_SHELL_SANDBOX_ADDITIONAL_WORKSPACE_ROOTS"


def additional_workspace_roots_from_environment() -> tuple[str, ...]:
    """Explicit launcher authority, never a project-config or model-tool field."""
    raw = env_get(ADDITIONAL_WORKSPACE_ROOTS_ENV)
    if raw is None:
        return ()
    try:
        if len(raw) > 131_072:
            raise ValueError("oversized folder list")
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) > 32:
            raise ValueError("expected at most 32 folders")
        if any(
            not isinstance(value, str)
            or not value.strip()
            or "\0" in value
            or not Path(value).is_absolute()
            for value in values
        ):
            raise ValueError("folders must be absolute paths")
        return tuple(dict.fromkeys(values))
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"Invalid {ADDITIONAL_WORKSPACE_ROOTS_ENV}: {exc}") from exc


def _parse_choice(
    raw: object,
    *,
    field_name: str,
    allowed: set[str],
    default: str,
) -> str:
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in allowed:
        return value
    opts = ", ".join(sorted(allowed))
    raise ConfigError(f"Invalid {field_name}: {raw!r}. Expected one of: {opts}")


def _parse_bool(raw: object, *, field_name: str, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"Invalid {field_name}: {raw!r}. Expected one of: 0, 1")


def _parse_docker_image(raw: object, *, field_name: str, default: str) -> str:
    if raw is None:
        return default
    value = str(raw).strip()
    if value:
        return value
    raise ConfigError(f"Invalid {field_name}: value cannot be empty")


def _parse_optional_positive_int(
    raw: object,
    *,
    field_name: str,
    default: int | None,
) -> int | None:
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except ValueError as e:
        raise ConfigError(f"Invalid {field_name}: {raw!r}. Expected positive integer.") from e
    if value <= 0:
        raise ConfigError(f"Invalid {field_name}: {raw!r}. Expected positive integer.")
    return value


def _parse_positive_float(
    raw: object,
    *,
    field_name: str,
    default: float,
) -> float:
    if raw is None:
        return default
    try:
        value = float(str(raw).strip())
    except ValueError as e:
        raise ConfigError(f"Invalid {field_name}: {raw!r}. Expected positive number.") from e
    if value <= 0 or not math.isfinite(value):
        raise ConfigError(f"Invalid {field_name}: {raw!r}. Expected positive number.")
    return value


def _parse_optional_string(
    raw: object,
    *,
    field_name: str,
    default: str | None,
) -> str | None:
    if raw is None:
        return default
    value = str(raw).strip()
    if not value:
        raise ConfigError(f"Invalid {field_name}: value cannot be empty")
    return value


def _parse_env_allowlist(
    raw: object,
    *,
    field_name: str,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    if raw is None:
        return default
    if isinstance(raw, str):
        candidates = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        candidates = [str(item).strip() for item in raw]
    else:
        raise ConfigError(
            f"Invalid {field_name}: {raw!r}. Expected comma-separated string or list."
        )
    cleaned: list[str] = []
    for item in candidates:
        if not item:
            continue
        if not _ENV_KEY_RE.match(item):
            raise ConfigError(
                f"Invalid {field_name}: {item!r}. Expected env var names like FOO_BAR."
            )
        cleaned.append(item.upper())
    # Preserve order while deduplicating.
    return tuple(dict.fromkeys(cleaned))


def resolve_shell_sandbox_settings(cfg: AppConfig) -> ShellSandboxSettings:
    mode = "strict"
    backend = "auto"
    network = "off"
    preview_access = "auto"
    bwrap_profile = "hardened"
    docker_image = DEFAULT_SHELL_SANDBOX_DOCKER_IMAGE
    clear_env = True
    docker_pids_limit: int | None = None
    docker_memory: str | None = None
    docker_cpus: str | None = None
    docker_read_only = False
    protect_repo_meta = True
    safe_git_writes = True
    docker_env_allowlist: tuple[str, ...] = ()
    background_max_concurrent = 4
    background_output_max_lines = 2000
    background_output_max_bytes = 256 * 1024
    background_kill_timeout_s = 10.0
    unavailable_fallback = "refuse"

    raw_cfg = cfg.extra_fields.get("shell_sandbox")
    if raw_cfg is not None and not isinstance(raw_cfg, dict):
        raise ConfigError("Invalid shell_sandbox config: expected object")

    cfg_map: dict[str, Any] = raw_cfg if isinstance(raw_cfg, dict) else {}
    filesystem_access = _parse_choice(
        env_get("ALYSIS_SHELL_SANDBOX_FILESYSTEM_ACCESS") or cfg_map.get("filesystem_access"),
        field_name="shell_sandbox.filesystem_access",
        allowed={"read-only", "workspace-write"},
        default="workspace-write",
    )
    read_roots = cfg_map.get("windows_read_roots", [])
    if not isinstance(read_roots, list) or any(
        not isinstance(p, str) or not p.strip() for p in read_roots
    ):
        raise ConfigError(
            "shell_sandbox.windows_read_roots must be a list of absolute directory paths"
        )
    mode = _parse_choice(
        cfg_map.get("mode"),
        field_name="shell_sandbox.mode",
        allowed=_VALID_MODES,
        default=mode,
    )
    backend = _parse_choice(
        cfg_map.get("backend"),
        field_name="shell_sandbox.backend",
        allowed=_VALID_BACKENDS,
        default=backend,
    )
    network = _parse_choice(
        cfg_map.get("network"),
        field_name="shell_sandbox.network",
        allowed=_VALID_NETWORK,
        default=network,
    )
    preview_access = _parse_choice(
        cfg_map.get("preview_access"),
        field_name="shell_sandbox.preview_access",
        allowed=_VALID_PREVIEW_ACCESS,
        default=preview_access,
    )
    bwrap_profile = _parse_choice(
        cfg_map.get("bwrap_profile"),
        field_name="shell_sandbox.bwrap_profile",
        allowed=_VALID_BWRAP_PROFILES,
        default=bwrap_profile,
    )
    docker_image = _parse_docker_image(
        cfg_map.get("docker_image"),
        field_name="shell_sandbox.docker_image",
        default=docker_image,
    )
    clear_env = _parse_bool(
        cfg_map.get("clear_env"),
        field_name="shell_sandbox.clear_env",
        default=clear_env,
    )
    docker_pids_limit = _parse_optional_positive_int(
        cfg_map.get("docker_pids_limit"),
        field_name="shell_sandbox.docker_pids_limit",
        default=docker_pids_limit,
    )
    docker_memory = _parse_optional_string(
        cfg_map.get("docker_memory"),
        field_name="shell_sandbox.docker_memory",
        default=docker_memory,
    )
    docker_cpus = _parse_optional_string(
        cfg_map.get("docker_cpus"),
        field_name="shell_sandbox.docker_cpus",
        default=docker_cpus,
    )
    docker_read_only = _parse_bool(
        cfg_map.get("docker_read_only"),
        field_name="shell_sandbox.docker_read_only",
        default=docker_read_only,
    )
    protect_repo_meta = _parse_bool(
        cfg_map.get("protect_repo_meta"),
        field_name="shell_sandbox.protect_repo_meta",
        default=protect_repo_meta,
    )
    safe_git_writes = _parse_bool(
        cfg_map.get("safe_git_writes"),
        field_name="shell_sandbox.safe_git_writes",
        default=safe_git_writes,
    )
    docker_env_allowlist = _parse_env_allowlist(
        cfg_map.get("docker_env_allowlist"),
        field_name="shell_sandbox.docker_env_allowlist",
        default=docker_env_allowlist,
    )
    background_max_concurrent = _parse_optional_positive_int(
        cfg_map.get("background_max_concurrent"),
        field_name="shell_sandbox.background_max_concurrent",
        default=background_max_concurrent,
    )
    background_output_max_lines = _parse_optional_positive_int(
        cfg_map.get("background_output_max_lines"),
        field_name="shell_sandbox.background_output_max_lines",
        default=background_output_max_lines,
    )
    background_output_max_bytes = _parse_optional_positive_int(
        cfg_map.get("background_output_max_bytes"),
        field_name="shell_sandbox.background_output_max_bytes",
        default=background_output_max_bytes,
    )
    background_kill_timeout_s = _parse_positive_float(
        cfg_map.get("background_kill_timeout_s"),
        field_name="shell_sandbox.background_kill_timeout_s",
        default=background_kill_timeout_s,
    )

    unavailable_fallback = _parse_choice(
        cfg_map.get("unavailable_fallback"),
        field_name="shell_sandbox.unavailable_fallback",
        allowed=_VALID_UNAVAILABLE_FALLBACKS,
        default=unavailable_fallback,
    )

    mode = _parse_choice(
        env_get("ALYSIS_SHELL_SANDBOX_MODE"),
        field_name="ALYSIS_SHELL_SANDBOX_MODE",
        allowed=_VALID_MODES,
        default=mode,
    )
    backend = _parse_choice(
        env_get("ALYSIS_SHELL_SANDBOX_BACKEND"),
        field_name="ALYSIS_SHELL_SANDBOX_BACKEND",
        allowed=_VALID_BACKENDS,
        default=backend,
    )
    network = _parse_choice(
        env_get("ALYSIS_SHELL_SANDBOX_NETWORK"),
        field_name="ALYSIS_SHELL_SANDBOX_NETWORK",
        allowed=_VALID_NETWORK,
        default=network,
    )
    preview_access = _parse_choice(
        env_get("ALYSIS_SHELL_SANDBOX_PREVIEW_ACCESS"),
        field_name="ALYSIS_SHELL_SANDBOX_PREVIEW_ACCESS",
        allowed=_VALID_PREVIEW_ACCESS,
        default=preview_access,
    )
    bwrap_profile = _parse_choice(
        env_get("ALYSIS_SHELL_SANDBOX_BWRAP_PROFILE"),
        field_name="ALYSIS_SHELL_SANDBOX_BWRAP_PROFILE",
        allowed=_VALID_BWRAP_PROFILES,
        default=bwrap_profile,
    )
    docker_image = _parse_docker_image(
        env_get("ALYSIS_SHELL_SANDBOX_DOCKER_IMAGE"),
        field_name="ALYSIS_SHELL_SANDBOX_DOCKER_IMAGE",
        default=docker_image,
    )
    clear_env = _parse_bool(
        env_get("ALYSIS_SHELL_SANDBOX_CLEAR_ENV"),
        field_name="ALYSIS_SHELL_SANDBOX_CLEAR_ENV",
        default=clear_env,
    )
    docker_pids_limit = _parse_optional_positive_int(
        env_get("ALYSIS_SHELL_SANDBOX_DOCKER_PIDS_LIMIT"),
        field_name="ALYSIS_SHELL_SANDBOX_DOCKER_PIDS_LIMIT",
        default=docker_pids_limit,
    )
    docker_memory = _parse_optional_string(
        env_get("ALYSIS_SHELL_SANDBOX_DOCKER_MEMORY"),
        field_name="ALYSIS_SHELL_SANDBOX_DOCKER_MEMORY",
        default=docker_memory,
    )
    docker_cpus = _parse_optional_string(
        env_get("ALYSIS_SHELL_SANDBOX_DOCKER_CPUS"),
        field_name="ALYSIS_SHELL_SANDBOX_DOCKER_CPUS",
        default=docker_cpus,
    )
    docker_read_only = _parse_bool(
        env_get("ALYSIS_SHELL_SANDBOX_DOCKER_READ_ONLY"),
        field_name="ALYSIS_SHELL_SANDBOX_DOCKER_READ_ONLY",
        default=docker_read_only,
    )
    protect_repo_meta = _parse_bool(
        env_get("ALYSIS_SHELL_SANDBOX_PROTECT_REPO_META"),
        field_name="ALYSIS_SHELL_SANDBOX_PROTECT_REPO_META",
        default=protect_repo_meta,
    )
    safe_git_writes = _parse_bool(
        env_get("ALYSIS_SHELL_SANDBOX_SAFE_GIT_WRITES"),
        field_name="ALYSIS_SHELL_SANDBOX_SAFE_GIT_WRITES",
        default=safe_git_writes,
    )
    docker_env_allowlist = _parse_env_allowlist(
        env_get("ALYSIS_SHELL_SANDBOX_DOCKER_ENV_ALLOWLIST"),
        field_name="ALYSIS_SHELL_SANDBOX_DOCKER_ENV_ALLOWLIST",
        default=docker_env_allowlist,
    )
    background_max_concurrent = _parse_optional_positive_int(
        env_get("ALYSIS_SHELL_SANDBOX_BACKGROUND_MAX_CONCURRENT"),
        field_name="ALYSIS_SHELL_SANDBOX_BACKGROUND_MAX_CONCURRENT",
        default=background_max_concurrent,
    )
    background_output_max_lines = _parse_optional_positive_int(
        env_get("ALYSIS_SHELL_SANDBOX_BACKGROUND_OUTPUT_MAX_LINES"),
        field_name="ALYSIS_SHELL_SANDBOX_BACKGROUND_OUTPUT_MAX_LINES",
        default=background_output_max_lines,
    )
    background_output_max_bytes = _parse_optional_positive_int(
        env_get("ALYSIS_SHELL_SANDBOX_BACKGROUND_OUTPUT_MAX_BYTES"),
        field_name="ALYSIS_SHELL_SANDBOX_BACKGROUND_OUTPUT_MAX_BYTES",
        default=background_output_max_bytes,
    )
    background_kill_timeout_s = _parse_positive_float(
        env_get("ALYSIS_SHELL_SANDBOX_BACKGROUND_KILL_TIMEOUT_S"),
        field_name="ALYSIS_SHELL_SANDBOX_BACKGROUND_KILL_TIMEOUT_S",
        default=background_kill_timeout_s,
    )

    unavailable_fallback = _parse_choice(
        env_get("ALYSIS_SHELL_SANDBOX_FALLBACK"),
        field_name="ALYSIS_SHELL_SANDBOX_FALLBACK",
        allowed=_VALID_UNAVAILABLE_FALLBACKS,
        default=unavailable_fallback,
    )

    # Resolve once for all consumers, including file tools and durable services.
    # Never probe availability here: a broken Windows bundle must not select host
    # execution or Docker as an automatic fallback.
    if backend == "auto" and platform.system() == "Windows":
        backend = "windows-mxc"
    return ShellSandboxSettings(
        mode=mode,
        backend=backend,
        network=network,
        filesystem_access=filesystem_access,
        windows_read_roots=tuple(read_roots),
        additional_workspace_roots=additional_workspace_roots_from_environment(),
        preview_access=preview_access,
        bwrap_profile=bwrap_profile,
        docker_image=docker_image,
        clear_env=clear_env,
        docker_pids_limit=docker_pids_limit,
        docker_memory=docker_memory,
        docker_cpus=docker_cpus,
        docker_read_only=docker_read_only,
        protect_repo_meta=protect_repo_meta,
        safe_git_writes=safe_git_writes,
        docker_env_allowlist=docker_env_allowlist,
        background_max_concurrent=background_max_concurrent,
        background_output_max_lines=background_output_max_lines,
        background_output_max_bytes=background_output_max_bytes,
        background_kill_timeout_s=background_kill_timeout_s,
        unavailable_fallback=unavailable_fallback,
    )


def normalize_sandbox_mode(value: object, *, default: str = "strict") -> str:
    """Return a valid sandbox mode, falling back to ``default`` for unknown input."""
    text = str(value or "").strip().lower()
    return text if text in _VALID_MODES else default


def sandbox_mode_from_config(cfg: AppConfig) -> str:
    """Read the persisted shell sandbox mode from config (``strict`` if unset).

    This reports the configured value only; the runtime resolution in
    :func:`resolve_shell_sandbox_settings` still lets the ``ALYSIS_SHELL_SANDBOX_MODE``
    environment variable override it.
    """
    extra = cfg.extra_fields if isinstance(cfg.extra_fields, dict) else {}
    section = extra.get("shell_sandbox")
    if isinstance(section, dict):
        return normalize_sandbox_mode(section.get("mode"))
    return "strict"


def apply_sandbox_mode_to_config(cfg: AppConfig, mode: object) -> AppConfig:
    """Persist ``mode`` into both ``shell_sandbox`` and ``verify_sandbox`` config sections.

    Both sections must be written together: the verify/completion gate resolves its
    sandbox mode independently from the shell sandbox, so writing only one would leave
    the other defaulting to ``strict`` and still fail closed.
    """
    normalized = normalize_sandbox_mode(mode)
    extra = cfg.extra_fields
    if not isinstance(extra, dict):
        extra = {}
        cfg.extra_fields = extra
    for key in ("shell_sandbox", "verify_sandbox"):
        section = extra.get(key)
        if not isinstance(section, dict):
            section = {}
        section["mode"] = normalized
        extra[key] = section
    return cfg
