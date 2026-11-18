from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path

from ..branding import canonical_server_data_dir, env_get
from ..config import ConfigError

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
_WORKER_BACKENDS = {"bwrap", "docker"}
_SANDBOX_MODES = {"off", "warn", "strict"}
_NETWORK_MODES = {"off", "on"}
_DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def _parse_int(raw: object, *, field_name: str, default: int, min_value: int) -> int:
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except ValueError as e:
        raise ConfigError(f"Invalid {field_name}: {raw!r}. Expected integer.") from e
    if value < min_value:
        raise ConfigError(f"Invalid {field_name}: {raw!r}. Expected >= {min_value}.")
    return value


def _parse_choice(raw: object, *, field_name: str, default: str, allowed: set[str]) -> str:
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in allowed:
        return value
    options = ", ".join(sorted(allowed))
    raise ConfigError(f"Invalid {field_name}: {raw!r}. Expected one of: {options}")


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


def _parse_optional_string(raw: object) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _parse_optional_base_url(raw: object, *, field_name: str) -> str | None:
    value = _parse_optional_string(raw)
    if value is None:
        return None
    if value.startswith(("http://", "https://")):
        return value
    raise ConfigError(f"Invalid {field_name}: {value!r}. Expected http:// or https:// URL.")


def _default_data_dir() -> Path:
    return canonical_server_data_dir()


def _default_worker_backend() -> str:
    if platform.system().lower() == "linux":
        return "bwrap"
    return "docker"


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    data_dir: Path
    token: str | None
    max_upload_bytes: int
    max_concurrent_jobs: int
    worker_backend: str
    worker_sandbox_mode: str
    worker_network: str
    default_model: str | None
    default_base_url: str | None
    allow_client_model: bool
    allow_client_base_url: bool

    @property
    def allow_unauthenticated_localhost_only(self) -> bool:
        return self.token is None

    @property
    def host_is_local(self) -> bool:
        return self.host.strip().lower() in _LOCAL_HOSTS

    @property
    def server_model(self) -> str | None:
        return self.default_model

    @property
    def server_base_url(self) -> str | None:
        return self.default_base_url

    @property
    def allow_client_model_override(self) -> bool:
        return self.allow_client_model

    @property
    def allow_client_base_url_override(self) -> bool:
        return self.allow_client_base_url


def resolve_server_settings(
    *,
    host: str = "127.0.0.1",
    port: int = 7070,
    data_dir: Path | None = None,
) -> ServerSettings:
    host_value = host.strip() or "127.0.0.1"
    port_value = _parse_int(port, field_name="port", default=7070, min_value=1)
    if port_value > 65535:
        raise ConfigError(f"Invalid port: {port_value}. Expected <= 65535.")

    env_data_dir = str(env_get("ALYSIS_SERVER_DATA_DIR") or "").strip()
    if data_dir is not None:
        data_path = data_dir.expanduser()
    elif env_data_dir:
        data_path = Path(env_data_dir).expanduser()
    else:
        data_path = _default_data_dir()

    token_raw = str(env_get("ALYSIS_SERVER_TOKEN") or "").strip()
    token = token_raw or None

    max_upload_bytes = _parse_int(
        env_get("ALYSIS_SERVER_MAX_UPLOAD_BYTES"),
        field_name="ALYSIS_SERVER_MAX_UPLOAD_BYTES",
        default=_DEFAULT_MAX_UPLOAD_BYTES,
        min_value=1024,
    )
    max_concurrent_jobs = _parse_int(
        env_get("ALYSIS_SERVER_MAX_JOBS"),
        field_name="ALYSIS_SERVER_MAX_JOBS",
        default=2,
        min_value=1,
    )

    worker_backend = _parse_choice(
        env_get("ALYSIS_SERVER_WORKER_BACKEND"),
        field_name="ALYSIS_SERVER_WORKER_BACKEND",
        default=_default_worker_backend(),
        allowed=_WORKER_BACKENDS,
    )
    if worker_backend == "bwrap" and platform.system().lower() != "linux":
        raise ConfigError("ALYSIS_SERVER_WORKER_BACKEND=bwrap requires Linux. Use docker instead.")

    worker_sandbox_default = "strict"
    worker_sandbox_mode = _parse_choice(
        env_get("ALYSIS_SERVER_WORKER_SANDBOX_MODE"),
        field_name="ALYSIS_SERVER_WORKER_SANDBOX_MODE",
        default=worker_sandbox_default,
        allowed=_SANDBOX_MODES,
    )
    worker_network = _parse_choice(
        env_get("ALYSIS_SERVER_WORKER_NETWORK"),
        field_name="ALYSIS_SERVER_WORKER_NETWORK",
        default="on",
        allowed=_NETWORK_MODES,
    )
    default_model = _parse_optional_string(env_get("ALYSIS_SERVER_MODEL"))
    default_base_url = _parse_optional_base_url(
        env_get("ALYSIS_SERVER_BASE_URL"),
        field_name="ALYSIS_SERVER_BASE_URL",
    )
    allow_client_base_url = _parse_bool(
        env_get("ALYSIS_SERVER_ALLOW_CLIENT_BASE_URL"),
        field_name="ALYSIS_SERVER_ALLOW_CLIENT_BASE_URL",
        default=False,
    )
    allow_client_model = _parse_bool(
        env_get("ALYSIS_SERVER_ALLOW_CLIENT_MODEL"),
        field_name="ALYSIS_SERVER_ALLOW_CLIENT_MODEL",
        default=True,
    )

    return ServerSettings(
        host=host_value,
        port=port_value,
        data_dir=data_path.resolve(),
        token=token,
        max_upload_bytes=max_upload_bytes,
        max_concurrent_jobs=max_concurrent_jobs,
        worker_backend=worker_backend,
        worker_sandbox_mode=worker_sandbox_mode,
        worker_network=worker_network,
        default_model=default_model,
        default_base_url=default_base_url,
        allow_client_model=allow_client_model,
        allow_client_base_url=allow_client_base_url,
    )
