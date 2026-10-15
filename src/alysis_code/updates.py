from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

import httpx
from packaging.version import InvalidVersion, Version

from .atomic_io import atomic_write_json
from .branding import (
    PYTHON_PACKAGE_NAME,
    canonical_user_data_dir,
    env_get,
)
from .config import AppConfig, ConfigError
from .safety import SafeHttpError, safe_http_request

PYPI_JSON_URL = f"https://pypi.org/pypi/{PYTHON_PACKAGE_NAME}/json"
UPDATE_CACHE_SCHEMA_VERSION = 1
UPDATE_PROMPT_STATE_SCHEMA_VERSION = 1
DEFAULT_UPDATE_CHECK_INTERVAL_HOURS = 24
DEFAULT_UPDATE_CHECK_TIMEOUT_S = 3.0
DEFAULT_UPDATE_PROMPT_SNOOZE_HOURS = 24
UPDATE_CACHE_MAX_BYTES = 512 * 1024
_update_refresh_lock = threading.Lock()
_update_refresh_started = False


class UpdateCheckError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpdateCacheRecord:
    checked_at: datetime
    package: str
    source: str
    latest_version: str | None = None
    url: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.latest_version) and not self.error

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": UPDATE_CACHE_SCHEMA_VERSION,
            "checked_at": self.checked_at.astimezone(UTC).isoformat(),
            "package": self.package,
            "source": self.source,
            "latest_version": self.latest_version,
            "url": self.url,
            "error": self.error,
        }


@dataclass(frozen=True)
class UpdateStatus:
    current_version: str
    latest_version: str | None
    checked_at: datetime | None
    source: str
    url: str | None = None
    error: str | None = None
    from_cache: bool = False

    @property
    def update_available(self) -> bool:
        if not self.latest_version:
            return False
        return _version_is_newer(self.latest_version, self.current_version)

    @property
    def up_to_date(self) -> bool:
        if not self.latest_version or self.error:
            return False
        return not self.update_available

    @property
    def state(self) -> str:
        if self.error:
            return "error"
        if self.update_available:
            return "update_available"
        if self.up_to_date:
            return "current"
        return "unknown"

    def to_json(self) -> dict[str, Any]:
        return {
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "checked_at": self.checked_at.astimezone(UTC).isoformat() if self.checked_at else None,
            "source": self.source,
            "url": self.url,
            "error": self.error,
            "from_cache": self.from_cache,
            "state": self.state,
            "update_available": self.update_available,
        }


@dataclass(frozen=True)
class UpdatePromptState:
    """Per-user memory of the startup update prompt.

    Lives next to ``update_check.json`` in the data dir (its own file, so the
    background cache refresh can never wipe it).
    """

    skipped_version: str | None = None
    last_prompted_version: str | None = None
    last_prompted_at: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": UPDATE_PROMPT_STATE_SCHEMA_VERSION,
            "package": PYTHON_PACKAGE_NAME,
            "skipped_version": self.skipped_version,
            "last_prompted_version": self.last_prompted_version,
            "last_prompted_at": (
                self.last_prompted_at.astimezone(UTC).isoformat() if self.last_prompted_at else None
            ),
        }


@dataclass(frozen=True)
class InstallerPlan:
    method: str
    supported: bool
    command: tuple[str, ...] = ()
    reason: str = ""

    @property
    def display_command(self) -> str:
        if not self.command:
            return ""
        return shlex.join(self.command)


def update_cache_path() -> Path:
    override = env_get("ALYSIS_UPDATE_CACHE_PATH")
    if override:
        return Path(override)
    data_override = env_get("ALYSIS_DATA_DIR")
    data_dir = Path(data_override) if data_override else canonical_user_data_dir()
    return data_dir / "update_check.json"


def resolve_update_check_enabled(cfg: AppConfig | None) -> bool:
    env_value = env_get("ALYSIS_UPDATE_CHECK_ENABLED")
    if env_value is not None:
        return _parse_bool(env_value, key="ALYSIS_UPDATE_CHECK_ENABLED")
    if cfg is None:
        return True
    return bool(getattr(cfg, "update_check_enabled", True))


def resolve_update_check_interval_hours(cfg: AppConfig | None) -> int:
    env_value = env_get("ALYSIS_UPDATE_CHECK_INTERVAL_HOURS")
    if env_value is not None:
        return _parse_positive_int(env_value, key="ALYSIS_UPDATE_CHECK_INTERVAL_HOURS")
    if cfg is None:
        return DEFAULT_UPDATE_CHECK_INTERVAL_HOURS
    return _parse_positive_int(
        getattr(cfg, "update_check_interval_hours", DEFAULT_UPDATE_CHECK_INTERVAL_HOURS),
        key="update_check_interval_hours",
    )


def resolve_update_check_timeout_s(cfg: AppConfig | None) -> float:
    env_value = env_get("ALYSIS_UPDATE_CHECK_TIMEOUT_S")
    if env_value is not None:
        return _parse_positive_float(env_value, key="ALYSIS_UPDATE_CHECK_TIMEOUT_S")
    if cfg is None:
        return DEFAULT_UPDATE_CHECK_TIMEOUT_S
    return _parse_positive_float(
        getattr(cfg, "update_check_timeout_s", DEFAULT_UPDATE_CHECK_TIMEOUT_S),
        key="update_check_timeout_s",
    )


def read_update_cache(path: Path | None = None) -> UpdateCacheRecord | None:
    cache_path = path or update_cache_path()
    if not cache_path.exists():
        return None
    try:
        if cache_path.stat().st_size > UPDATE_CACHE_MAX_BYTES:
            return None
    except OSError:
        return None
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    try:
        schema_version = int(raw.get("schema_version") or 0)
    except (TypeError, ValueError):
        return None
    if schema_version != UPDATE_CACHE_SCHEMA_VERSION:
        return None
    checked_at = _parse_datetime(raw.get("checked_at"))
    if checked_at is None:
        return None
    package = str(raw.get("package") or "").strip()
    if package != PYTHON_PACKAGE_NAME:
        return None
    return UpdateCacheRecord(
        checked_at=checked_at,
        package=package,
        source=str(raw.get("source") or "pypi").strip() or "pypi",
        latest_version=_optional_non_empty_string(raw.get("latest_version")),
        url=_optional_non_empty_string(raw.get("url")),
        error=_optional_non_empty_string(raw.get("error")),
    )


def write_update_cache(record: UpdateCacheRecord, path: Path | None = None) -> None:
    atomic_write_json(path or update_cache_path(), record.to_json())


def cache_is_fresh(
    record: UpdateCacheRecord | None,
    *,
    cfg: AppConfig | None,
    now: datetime | None = None,
) -> bool:
    if record is None:
        return False
    current = _utcnow() if now is None else _ensure_utc(now)
    age = current - record.checked_at
    return timedelta(0) <= age < timedelta(hours=resolve_update_check_interval_hours(cfg))


def status_from_cache(
    *,
    current_version: str,
    cfg: AppConfig | None,
    now: datetime | None = None,
    path: Path | None = None,
) -> UpdateStatus:
    if not resolve_update_check_enabled(cfg):
        return UpdateStatus(
            current_version=current_version,
            latest_version=None,
            checked_at=None,
            source="disabled",
            error=None,
            from_cache=True,
        )
    record = read_update_cache(path)
    if record is None:
        return UpdateStatus(
            current_version=current_version,
            latest_version=None,
            checked_at=None,
            source="cache",
            from_cache=True,
        )
    stale = not cache_is_fresh(record, cfg=cfg, now=now)
    error = record.error
    if stale and not record.latest_version:
        error = error or "cached update check is stale"
    return UpdateStatus(
        current_version=current_version,
        latest_version=record.latest_version,
        checked_at=record.checked_at,
        source=record.source,
        url=record.url,
        error=error,
        from_cache=True,
    )


def check_for_updates(
    *,
    current_version: str,
    cfg: AppConfig | None,
    force: bool = False,
    now: datetime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    cache_path: Path | None = None,
) -> UpdateStatus:
    current_time = _utcnow() if now is None else _ensure_utc(now)
    if not force:
        cached = read_update_cache(cache_path)
        if cache_is_fresh(cached, cfg=cfg, now=current_time):
            assert cached is not None
            return UpdateStatus(
                current_version=current_version,
                latest_version=cached.latest_version,
                checked_at=cached.checked_at,
                source=cached.source,
                url=cached.url,
                error=cached.error,
                from_cache=True,
            )

    try:
        latest_version, url = fetch_latest_pypi_version(
            current_version=current_version,
            cfg=cfg,
            transport=transport,
        )
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 - explicit update check should preserve readable errors
        message = str(exc).strip() or exc.__class__.__name__
        record = UpdateCacheRecord(
            checked_at=current_time,
            package=PYTHON_PACKAGE_NAME,
            source="pypi",
            error=message,
        )
        write_update_cache(record, cache_path)
        return UpdateStatus(
            current_version=current_version,
            latest_version=None,
            checked_at=current_time,
            source="pypi",
            error=message,
        )

    record = UpdateCacheRecord(
        checked_at=current_time,
        package=PYTHON_PACKAGE_NAME,
        source="pypi",
        latest_version=latest_version,
        url=url,
    )
    write_update_cache(record, cache_path)
    return UpdateStatus(
        current_version=current_version,
        latest_version=latest_version,
        checked_at=current_time,
        source="pypi",
        url=url,
    )


def maybe_refresh_update_cache_in_background(
    *,
    current_version: str,
    cfg: AppConfig | None,
    now: datetime | None = None,
    path: Path | None = None,
    thread_factory: Callable[..., Any] | None = None,
) -> bool:
    if not resolve_update_check_enabled(cfg):
        return False
    current_time = _utcnow() if now is None else _ensure_utc(now)
    cached = read_update_cache(path)
    if cache_is_fresh(cached, cfg=cfg, now=current_time):
        return False

    global _update_refresh_started
    with _update_refresh_lock:
        if _update_refresh_started:
            return False
        _update_refresh_started = True

    def _refresh() -> None:
        global _update_refresh_started
        try:
            check_for_updates(
                current_version=current_version,
                cfg=cfg,
                force=True,
                now=current_time,
                cache_path=path,
            )
        except Exception:
            pass
        finally:
            with _update_refresh_lock:
                _update_refresh_started = False

    factory = thread_factory or threading.Thread
    try:
        thread = factory(
            target=_refresh,
            name="alysis-update-check",
            daemon=True,
        )
        thread.start()
    except Exception:
        with _update_refresh_lock:
            _update_refresh_started = False
        return False
    return True


def fetch_latest_pypi_version(
    *,
    current_version: str,
    cfg: AppConfig | None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, str | None]:
    timeout_s = resolve_update_check_timeout_s(cfg)
    headers = {
        "Accept": "application/json",
        "User-Agent": f"{PYTHON_PACKAGE_NAME}/{current_version}",
    }
    try:
        response = asyncio.run(
            safe_http_request(
                "GET",
                PYPI_JSON_URL,
                timeout=timeout_s,
                max_bytes=UPDATE_CACHE_MAX_BYTES,
                headers=headers,
                _transport=transport,
            )
        )
    except SafeHttpError as exc:
        raise UpdateCheckError(f"update check blocked: {exc}") from exc
    except httpx.TimeoutException as exc:
        raise UpdateCheckError(f"update check timed out after {timeout_s:g}s") from exc
    except Exception as exc:  # noqa: BLE001
        raise UpdateCheckError(f"update check failed: {exc}") from exc

    if response.status_code == 404:
        raise UpdateCheckError(f"No PyPI release found for {PYTHON_PACKAGE_NAME}.")
    if response.status_code >= 400:
        raise UpdateCheckError(f"PyPI update check failed with HTTP {response.status_code}")
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        raise UpdateCheckError("PyPI update check returned non-JSON response") from exc
    if not isinstance(payload, dict):
        raise UpdateCheckError("PyPI update check returned unexpected payload")
    info = payload.get("info")
    if not isinstance(info, dict):
        raise UpdateCheckError("PyPI update check payload is missing info")
    latest_version = str(info.get("version") or "").strip()
    if not latest_version:
        raise UpdateCheckError("PyPI update check payload is missing latest version")
    _parse_version(latest_version)
    project_url = str(info.get("package_url") or "").strip() or None
    if project_url is None:
        project_urls = info.get("project_urls")
        if isinstance(project_urls, dict):
            project_url = str(
                project_urls.get("Changelog") or project_urls.get("Source") or ""
            ).strip()
            project_url = project_url or None
    return latest_version, project_url


def passive_update_notice(
    *,
    current_version: str,
    cfg: AppConfig | None,
    now: datetime | None = None,
    path: Path | None = None,
) -> str | None:
    status = status_from_cache(current_version=current_version, cfg=cfg, now=now, path=path)
    if not status.update_available or not status.latest_version:
        return None
    return (
        f"Alysis Code {status.latest_version} is available; you have {status.current_version}. "
        "Run `alysis update`."
    )


def update_prompt_state_path() -> Path:
    override = env_get("ALYSIS_UPDATE_PROMPT_STATE_PATH")
    if override:
        return Path(override)
    data_override = env_get("ALYSIS_DATA_DIR")
    data_dir = Path(data_override) if data_override else canonical_user_data_dir()
    return data_dir / "update_prompt.json"


def resolve_update_prompt_enabled(cfg: AppConfig | None) -> bool:
    env_value = env_get("ALYSIS_UPDATE_PROMPT_ENABLED")
    if env_value is not None:
        return _parse_bool(env_value, key="ALYSIS_UPDATE_PROMPT_ENABLED")
    if cfg is None:
        return True
    return bool(getattr(cfg, "update_prompt_enabled", True))


def read_update_prompt_state(path: Path | None = None) -> UpdatePromptState:
    state_path = path or update_prompt_state_path()
    if not state_path.exists():
        return UpdatePromptState()
    try:
        if state_path.stat().st_size > UPDATE_CACHE_MAX_BYTES:
            return UpdatePromptState()
    except OSError:
        return UpdatePromptState()
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return UpdatePromptState()
    if not isinstance(raw, dict):
        return UpdatePromptState()
    try:
        schema_version = int(raw.get("schema_version") or 0)
    except (TypeError, ValueError):
        return UpdatePromptState()
    if schema_version != UPDATE_PROMPT_STATE_SCHEMA_VERSION:
        return UpdatePromptState()
    package = str(raw.get("package") or "").strip()
    if package != PYTHON_PACKAGE_NAME:
        return UpdatePromptState()
    return UpdatePromptState(
        skipped_version=_optional_non_empty_string(raw.get("skipped_version")),
        last_prompted_version=_optional_non_empty_string(raw.get("last_prompted_version")),
        last_prompted_at=_parse_datetime(raw.get("last_prompted_at")),
    )


def write_update_prompt_state(state: UpdatePromptState, path: Path | None = None) -> None:
    atomic_write_json(path or update_prompt_state_path(), state.to_json())


def record_update_prompt_shown(
    latest_version: str,
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> None:
    state = read_update_prompt_state(path)
    write_update_prompt_state(
        UpdatePromptState(
            skipped_version=state.skipped_version,
            last_prompted_version=latest_version,
            last_prompted_at=_utcnow() if now is None else _ensure_utc(now),
        ),
        path,
    )


def record_update_skipped(
    latest_version: str,
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> None:
    state = read_update_prompt_state(path)
    write_update_prompt_state(
        UpdatePromptState(
            skipped_version=latest_version,
            last_prompted_version=state.last_prompted_version,
            last_prompted_at=state.last_prompted_at,
        ),
        path,
    )


def should_prompt_for_update(
    *,
    status: UpdateStatus,
    state: UpdatePromptState,
    cfg: AppConfig | None,
    now: datetime | None = None,
) -> bool:
    """Pure gate for the startup update prompt.

    True only when a newer release is known from the cache and the user has
    neither skipped that release nor been prompted about it within the snooze
    window.
    """
    if not resolve_update_check_enabled(cfg):
        return False
    if not resolve_update_prompt_enabled(cfg):
        return False
    if not status.update_available or not status.latest_version:
        return False
    latest = status.latest_version
    if state.skipped_version == latest:
        return False
    if state.last_prompted_version == latest and state.last_prompted_at is not None:
        current = _utcnow() if now is None else _ensure_utc(now)
        age = current - state.last_prompted_at
        if timedelta(0) <= age < timedelta(hours=DEFAULT_UPDATE_PROMPT_SNOOZE_HOURS):
            return False
    return True


def installed_distribution_name(preferred: str = PYTHON_PACKAGE_NAME) -> str:
    """Return the canonical distribution used for update introspection."""
    return preferred


def detect_installer_plan(
    *,
    package_name: str = PYTHON_PACKAGE_NAME,
    executable: str | None = None,
    prefix: str | None = None,
    base_prefix: str | None = None,
    env: Mapping[str, str] | None = None,
) -> InstallerPlan:
    executable_value = executable or sys.executable
    prefix_value = prefix or sys.prefix
    base_prefix_value = base_prefix or getattr(sys, "base_prefix", sys.prefix)
    env_map = env or os.environ

    # Introspect the distribution that is really on disk, but always upgrade
    # *to* the current package. Upgrading the legacy name in place would keep
    # the user on a package that no longer receives releases; upgrading the new
    # name without replacing the old one would leave two copies fighting over
    # the same console script.
    installed_name = installed_distribution_name(package_name)
    if installed_name != package_name:
        return _legacy_distribution_plan(
            installed_name=installed_name,
            target_name=package_name,
            executable=executable_value,
            prefix=prefix_value,
            env=env_map,
        )

    editable_reason = _editable_install_reason(package_name)
    if editable_reason:
        return InstallerPlan(
            method="editable",
            supported=False,
            reason=editable_reason,
        )

    if _looks_like_pipx_install(prefix_value, executable_value, package_name, env_map):
        if shutil.which("pipx") is None:
            return InstallerPlan(
                method="pipx",
                supported=False,
                reason="This looks like a pipx install, but `pipx` is not on PATH.",
            )
        return InstallerPlan(
            method="pipx",
            supported=True,
            command=("pipx", "upgrade", package_name),
            reason="Detected a pipx-managed virtual environment.",
        )

    installer = _distribution_installer(package_name)
    if installer == "uv":
        return _uv_installer_plan(
            package_name=package_name,
            executable=executable_value,
            prefix=prefix_value,
            env=env_map,
        )

    command = (executable_value, "-m", "pip", "install", "--upgrade", package_name)
    if prefix_value != base_prefix_value:
        return InstallerPlan(
            method="venv-pip",
            supported=True,
            command=command,
            reason="Detected a Python virtual environment.",
        )

    if installer and installer != "pip":
        return InstallerPlan(
            method=installer,
            supported=False,
            reason=f"This install reports installer `{installer}`; update it with that tool.",
        )

    return InstallerPlan(
        method="pip",
        supported=True,
        command=command,
        reason="Detected a pip-style Python install.",
    )


def _uv_installer_plan(
    *,
    package_name: str,
    executable: str,
    prefix: str,
    env: Mapping[str, str],
) -> InstallerPlan:
    if shutil.which("uv") is None:
        return InstallerPlan(
            method="uv",
            supported=False,
            reason="This install reports installer `uv`, but `uv` is not on PATH.",
        )
    if _looks_like_uv_tool_install(prefix, package_name, env):
        return InstallerPlan(
            method="uv-tool",
            supported=True,
            command=("uv", "tool", "upgrade", package_name),
            reason="Detected a uv-managed tool install.",
        )
    return InstallerPlan(
        method="uv-pip",
        supported=True,
        command=("uv", "pip", "install", "--python", executable, "--upgrade", package_name),
        reason="Detected a uv-managed Python environment.",
    )


def run_installer_plan(
    plan: InstallerPlan,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    if not plan.supported or not plan.command:
        raise UpdateCheckError(plan.reason or "No supported update command is available.")
    completed = runner(list(plan.command), check=False)
    return int(getattr(completed, "returncode", 0) or 0)


def _looks_like_pipx_install(
    prefix: str,
    executable: str,
    package_name: str,
    env: Mapping[str, str],
) -> bool:
    package_variants = {_normalize_package_name(package_name), "alysis"}
    for raw_path in (prefix, executable):
        parts = [part.casefold() for part in Path(raw_path).parts]
        for index, part in enumerate(parts):
            if part != "pipx":
                continue
            if index + 2 < len(parts) and parts[index + 1] == "venvs":
                return _normalize_package_name(parts[index + 2]) in package_variants
    pipx_home = str(env.get("PIPX_HOME") or "").strip()
    return bool(pipx_home and str(Path(prefix)).startswith(os.fspath(Path(pipx_home))))


def _looks_like_uv_tool_install(prefix: str, package_name: str, env: Mapping[str, str]) -> bool:
    package_variants = {_normalize_package_name(package_name), "alysis"}
    prefix_path = Path(prefix)
    for configured_root in _uv_tool_roots(env):
        try:
            relative = prefix_path.resolve().relative_to(configured_root.resolve())
        except (OSError, ValueError):
            continue
        first_part = relative.parts[0] if relative.parts else ""
        if _normalize_package_name(first_part) in package_variants:
            return True

    parts = [part.casefold() for part in prefix_path.parts]
    for index, part in enumerate(parts):
        if part == "uv" and index + 2 < len(parts) and parts[index + 1] == "tools":
            return _normalize_package_name(parts[index + 2]) in package_variants
    return False


def _uv_tool_roots(env: Mapping[str, str]) -> tuple[Path, ...]:
    raw_roots = [
        str(env.get("UV_TOOL_DIR") or "").strip(),
        os.fspath(Path.home() / ".local" / "share" / "uv" / "tools"),
    ]
    return tuple(Path(raw) for raw in raw_roots if raw)


def _legacy_distribution_plan(
    *,
    installed_name: str,
    target_name: str,
    executable: str,
    prefix: str,
    env: Mapping[str, str],
) -> InstallerPlan:
    """Plan the move from the pre-rebrand distribution to the current one.

    The reasons below stay purely operational — what to run — rather than
    narrating the rename at the user. They are shown in the TUI update prompt,
    where the history of the package name is not what anyone needs.
    """
    if _editable_install_reason(installed_name):
        return InstallerPlan(
            method="editable",
            supported=False,
            reason=(
                f"This is an editable install of `{installed_name}`. Pull the latest "
                f"source and reinstall it as `{target_name}`."
            ),
        )

    if _looks_like_pipx_install(prefix, executable, installed_name, env):
        if shutil.which("pipx") is None:
            return InstallerPlan(
                method="pipx",
                supported=False,
                reason="This looks like a pipx install, but `pipx` is not on PATH.",
            )
        # `pipx upgrade` cannot rename an app; the old one must go first or its
        # legacy command script would shadow the new package's alias.
        return InstallerPlan(
            method="pipx",
            supported=False,
            command=("pipx", "uninstall", installed_name),
            reason=(f"Run `pipx uninstall {installed_name}`, then `pipx install {target_name}`."),
        )

    return InstallerPlan(
        method="venv-pip",
        supported=False,
        command=(executable, "-m", "pip", "install", "--upgrade", target_name),
        reason=(
            f"Install `{target_name}`, then `pip uninstall {installed_name}` to remove the old one."
        ),
    )


def _editable_install_reason(package_name: str) -> str | None:
    try:
        dist = importlib_metadata.distribution(package_name)
    except importlib_metadata.PackageNotFoundError:
        return "Package metadata was not found; update from the source checkout or reinstall."
    raw = dist.read_text("direct_url.json")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    dir_info = payload.get("dir_info")
    if isinstance(dir_info, dict) and dir_info.get("editable") is True:
        url = str(payload.get("url") or "").strip()
        suffix = f" ({url})" if url else ""
        return f"This is an editable/source install{suffix}; update the checkout manually."
    return None


def _distribution_installer(package_name: str) -> str | None:
    try:
        dist = importlib_metadata.distribution(package_name)
    except importlib_metadata.PackageNotFoundError:
        return None
    raw = dist.read_text("INSTALLER")
    value = str(raw or "").strip().lower()
    return value or None


def _normalize_package_name(value: str) -> str:
    return str(value or "").strip().replace("_", "-").casefold()


def _version_is_newer(latest: str, current: str) -> bool:
    try:
        return _parse_version(latest) > _parse_version(current)
    except InvalidVersion:
        return False


def _parse_version(value: str) -> Version:
    try:
        return Version(str(value or "").strip())
    except InvalidVersion as exc:
        raise UpdateCheckError(f"Invalid package version: {value!r}") from exc


def _parse_bool(value: object, *, key: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be true/false")


def _parse_positive_int(value: object, *, key: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} must be an integer") from exc
    if parsed <= 0:
        raise ConfigError(f"{key} must be > 0")
    return parsed


def _parse_positive_float(value: object, *, key: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} must be a number") from exc
    if parsed <= 0:
        raise ConfigError(f"{key} must be > 0")
    return parsed


def _optional_non_empty_string(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _parse_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _ensure_utc(parsed)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _utcnow() -> datetime:
    return datetime.now(UTC)
