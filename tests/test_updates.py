from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code import updates as updates_mod
from alysis_code.branding import PYTHON_PACKAGE_NAME
from alysis_code.cli import app as alysis_app
from alysis_code.config import (
    AppConfig,
    ConfigError,
    load_config,
    save_config,
    set_config_value,
)
from alysis_code.updates import (
    InstallerPlan,
    UpdateCacheRecord,
    cache_is_fresh,
    check_for_updates,
    detect_installer_plan,
    passive_update_notice,
    read_update_cache,
    update_cache_path,
    write_update_cache,
)

RETIRED_PACKAGE_NAME = "retired-agent"


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
        "ALYSIS_API_KEY": "",
        "OPENAI_API_KEY": "",
    }


class _InlineThread:
    def __init__(self, *, target, name: str, daemon: bool) -> None:
        self._target = target
        self.name = name
        self.daemon = daemon

    def start(self) -> None:
        self._target()


class _UnexpectedThread:
    def __init__(self, **_kwargs) -> None:
        raise AssertionError("background refresh should not start")


def test_update_check_fetches_pypi_version_and_writes_cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path))

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://pypi.org/pypi/alysis-code/json"
        return httpx.Response(
            200,
            json={
                "info": {
                    "version": "0.1.5",
                    "package_url": "https://pypi.org/project/alysis-code/",
                }
            },
        )

    status = check_for_updates(
        current_version="0.1.4",
        cfg=AppConfig(),
        force=True,
        transport=httpx.MockTransport(handler),
    )

    assert status.update_available is True
    assert status.latest_version == "0.1.5"
    cached = read_update_cache()
    assert cached is not None
    assert cached.latest_version == "0.1.5"
    assert cached.error is None


def test_update_check_reports_missing_pypi_release(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path))

    status = check_for_updates(
        current_version="0.1.4",
        cfg=AppConfig(),
        force=True,
        transport=httpx.MockTransport(lambda _request: httpx.Response(404)),
    )

    assert status.error == "No PyPI release found for alysis-code."
    assert status.update_available is False
    cached = read_update_cache()
    assert cached is not None
    assert cached.error == status.error


def test_update_check_uses_fresh_cache_without_network(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path))
    record = UpdateCacheRecord(
        checked_at=datetime.now(UTC),
        package="alysis-code",
        source="pypi",
        latest_version="0.1.5",
        url="https://pypi.org/project/alysis-code/",
    )
    write_update_cache(record)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("fresh cache should avoid network")

    status = check_for_updates(
        current_version="0.1.4",
        cfg=AppConfig(update_check_interval_hours=24),
        force=False,
        transport=httpx.MockTransport(handler),
    )

    assert status.from_cache is True
    assert status.update_available is True
    assert status.latest_version == "0.1.5"


def test_read_update_cache_ignores_invalid_schema(tmp_path: Path) -> None:
    cache_path = tmp_path / "update.json"
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": "not-an-int",
                "checked_at": datetime.now(UTC).isoformat(),
                "package": "alysis-code",
                "source": "pypi",
                "latest_version": "0.1.5",
            }
        ),
        encoding="utf-8",
    )

    assert read_update_cache(cache_path) is None


def test_update_check_invalid_config_raises_config_error() -> None:
    with pytest.raises(ConfigError):
        check_for_updates(
            current_version="0.1.4",
            cfg=AppConfig(update_check_timeout_s=0),
            force=True,
        )


def test_update_cache_freshness_honors_configured_interval() -> None:
    now = datetime(2026, 5, 7, 10, 0, tzinfo=UTC)
    record = UpdateCacheRecord(
        checked_at=now - timedelta(hours=2),
        package="alysis-code",
        source="pypi",
        latest_version="0.1.4",
    )

    assert cache_is_fresh(record, cfg=AppConfig(update_check_interval_hours=3), now=now) is True
    assert cache_is_fresh(record, cfg=AppConfig(update_check_interval_hours=1), now=now) is False


def test_passive_update_notice_uses_cache_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path))
    write_update_cache(
        UpdateCacheRecord(
            checked_at=datetime.now(UTC),
            package="alysis-code",
            source="pypi",
            latest_version="0.1.5",
        )
    )

    assert passive_update_notice(current_version="0.1.4", cfg=AppConfig()) == (
        "Alysis Code 0.1.5 is available; you have 0.1.4. Run `alysis update`."
    )
    assert (
        passive_update_notice(
            current_version="0.1.4",
            cfg=AppConfig(update_check_enabled=False),
        )
        is None
    )


def test_background_refresh_updates_cache_when_stale(tmp_path: Path, monkeypatch) -> None:
    cache_path = tmp_path / "update.json"
    monkeypatch.setattr(updates_mod, "_update_refresh_started", False)
    calls: list[dict[str, object]] = []

    def fake_check_for_updates(**kwargs) -> cli_mod.UpdateStatus:
        calls.append(kwargs)
        write_update_cache(
            UpdateCacheRecord(
                checked_at=datetime.now(UTC),
                package="alysis-code",
                source="pypi",
                latest_version="0.1.5",
            ),
            path=cache_path,
        )
        return cli_mod.UpdateStatus(
            current_version="0.1.4",
            latest_version="0.1.5",
            checked_at=datetime.now(UTC),
            source="pypi",
        )

    monkeypatch.setattr(updates_mod, "check_for_updates", fake_check_for_updates)

    started = updates_mod.maybe_refresh_update_cache_in_background(
        current_version="0.1.4",
        cfg=AppConfig(),
        path=cache_path,
        thread_factory=_InlineThread,
    )

    assert started is True
    assert calls and calls[0]["force"] is True
    assert read_update_cache(cache_path).latest_version == "0.1.5"


@pytest.mark.parametrize("fetch_fails", [False, True])
def test_update_check_keeps_cache_destination_during_fetch(
    tmp_path: Path, monkeypatch, fetch_fails: bool
) -> None:
    original = tmp_path / "original" / "update_check.json"
    changed = tmp_path / "changed" / "update_check.json"
    monkeypatch.setenv("ALYSIS_UPDATE_CACHE_PATH", str(original))

    def fetch(**_kwargs):
        monkeypatch.setenv("ALYSIS_UPDATE_CACHE_PATH", str(changed))
        if fetch_fails:
            raise updates_mod.UpdateCheckError("offline")
        return "0.1.5", "https://pypi.org/project/alysis-code/"

    monkeypatch.setattr(updates_mod, "fetch_latest_pypi_version", fetch)
    status = check_for_updates(current_version="0.1.4", cfg=AppConfig(), force=True)

    record = read_update_cache(original)
    assert record is not None
    assert record.error == status.error == ("offline" if fetch_fails else None)
    assert record.latest_version == (None if fetch_fails else "0.1.5")
    assert not changed.exists()


def test_background_refresh_keeps_the_scheduling_data_directory(
    tmp_path: Path, monkeypatch
) -> None:
    original = tmp_path / "original"
    changed = tmp_path / "changed"
    monkeypatch.delenv("ALYSIS_UPDATE_CACHE_PATH", raising=False)
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(original))
    monkeypatch.setattr(updates_mod, "_update_refresh_started", False)
    monkeypatch.setattr(
        updates_mod,
        "fetch_latest_pypi_version",
        lambda **_kwargs: ("0.1.5", "https://pypi.org/project/alysis-code/"),
    )

    class DelayedThread(_InlineThread):
        def start(self) -> None:
            monkeypatch.setenv("ALYSIS_DATA_DIR", str(changed))
            super().start()

    assert updates_mod.maybe_refresh_update_cache_in_background(
        current_version="0.1.4", cfg=AppConfig(), thread_factory=DelayedThread
    )
    assert read_update_cache(original / "update_check.json").latest_version == "0.1.5"
    assert not changed.exists()


def test_background_refresh_skips_when_disabled_or_cache_fresh(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache_path = tmp_path / "update.json"
    monkeypatch.setattr(updates_mod, "_update_refresh_started", False)
    write_update_cache(
        UpdateCacheRecord(
            checked_at=datetime.now(UTC),
            package="alysis-code",
            source="pypi",
            latest_version="0.1.4",
        ),
        path=cache_path,
    )

    assert (
        updates_mod.maybe_refresh_update_cache_in_background(
            current_version="0.1.4",
            cfg=AppConfig(),
            path=cache_path,
            thread_factory=_UnexpectedThread,
        )
        is False
    )
    assert (
        updates_mod.maybe_refresh_update_cache_in_background(
            current_version="0.1.4",
            cfg=AppConfig(update_check_enabled=False),
            path=tmp_path / "missing.json",
            thread_factory=_UnexpectedThread,
        )
        is False
    )


@pytest.fixture(autouse=True)
def _assume_current_distribution(monkeypatch):
    """Pin the installed-distribution lookup for the installer-plan tests.

    detect_installer_plan() branches on which distribution is actually
    registered, and the dev checkout may still carry the pre-rebrand one.
    These tests cover the normal path; the rename path has its own tests.
    """
    monkeypatch.setattr(
        updates_mod, "installed_distribution_name", lambda preferred=None: PYTHON_PACKAGE_NAME
    )


def test_detect_installer_plan_recognizes_pipx(monkeypatch) -> None:
    monkeypatch.setattr(updates_mod, "_editable_install_reason", lambda _package_name: None)
    monkeypatch.setattr(updates_mod, "_distribution_installer", lambda _package_name: "pip")
    monkeypatch.setattr(
        updates_mod.shutil,
        "which",
        lambda name: "/usr/bin/pipx" if name == "pipx" else None,
    )

    plan = detect_installer_plan(
        executable="/home/me/.local/share/pipx/venvs/alysis-code/bin/python",
        prefix="/home/me/.local/share/pipx/venvs/alysis-code",
        base_prefix="/usr",
        env={},
    )

    assert plan.supported is True
    assert plan.method == "pipx"
    assert plan.command == ("pipx", "upgrade", "alysis-code")


def test_detect_installer_plan_recognizes_virtualenv(monkeypatch) -> None:
    monkeypatch.setattr(updates_mod, "_editable_install_reason", lambda _package_name: None)
    monkeypatch.setattr(updates_mod, "_distribution_installer", lambda _package_name: "pip")
    monkeypatch.setattr(updates_mod.shutil, "which", lambda _name: None)

    plan = detect_installer_plan(
        executable="/tmp/venv/bin/python",
        prefix="/tmp/venv",
        base_prefix="/usr",
        env={},
    )

    assert plan.supported is True
    assert plan.method == "venv-pip"
    assert plan.command == (
        "/tmp/venv/bin/python",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "alysis-code",
    )


def test_detect_installer_plan_recognizes_uv_tool_install(monkeypatch) -> None:
    monkeypatch.setattr(updates_mod, "_editable_install_reason", lambda _package_name: None)
    monkeypatch.setattr(updates_mod, "_distribution_installer", lambda _package_name: "uv")
    monkeypatch.setattr(
        updates_mod.shutil,
        "which",
        lambda name: "/usr/bin/uv" if name == "uv" else None,
    )

    plan = detect_installer_plan(
        executable="/home/me/.local/share/uv/tools/alysis-code/bin/python",
        prefix="/home/me/.local/share/uv/tools/alysis-code",
        base_prefix="/usr",
        env={},
    )

    assert plan.supported is True
    assert plan.method == "uv-tool"
    assert plan.command == ("uv", "tool", "upgrade", "alysis-code")


def test_detect_installer_plan_recognizes_uv_python_environment(monkeypatch) -> None:
    monkeypatch.setattr(updates_mod, "_editable_install_reason", lambda _package_name: None)
    monkeypatch.setattr(updates_mod, "_distribution_installer", lambda _package_name: "uv")
    monkeypatch.setattr(
        updates_mod.shutil,
        "which",
        lambda name: "/usr/bin/uv" if name == "uv" else None,
    )

    plan = detect_installer_plan(
        executable="/tmp/project/.venv/bin/python",
        prefix="/tmp/project/.venv",
        base_prefix="/usr",
        env={},
    )

    assert plan.supported is True
    assert plan.method == "uv-pip"
    assert plan.command == (
        "uv",
        "pip",
        "install",
        "--python",
        "/tmp/project/.venv/bin/python",
        "--upgrade",
        "alysis-code",
    )


def test_detect_installer_plan_reports_uv_missing(monkeypatch) -> None:
    monkeypatch.setattr(updates_mod, "_editable_install_reason", lambda _package_name: None)
    monkeypatch.setattr(updates_mod, "_distribution_installer", lambda _package_name: "uv")
    monkeypatch.setattr(updates_mod.shutil, "which", lambda _name: None)

    plan = detect_installer_plan(
        executable="/tmp/project/.venv/bin/python",
        prefix="/tmp/project/.venv",
        base_prefix="/usr",
        env={},
    )

    assert plan.supported is False
    assert plan.method == "uv"
    assert "`uv` is not on PATH" in plan.reason


def test_update_check_config_keys_round_trip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = AppConfig()
    cfg = set_config_value(cfg, "update_check_enabled", "false")
    cfg = set_config_value(cfg, "update_check_interval_hours", "12")
    cfg = set_config_value(cfg, "update_check_timeout_s", "5")
    save_config(cfg)

    loaded = load_config()
    assert loaded.update_check_enabled is False
    assert loaded.update_check_interval_hours == 12
    assert loaded.update_check_timeout_s == 5.0


def test_update_check_command_emits_json(monkeypatch, tmp_path: Path) -> None:
    status = cli_mod.UpdateStatus(
        current_version="0.1.4",
        latest_version="0.1.5",
        checked_at=datetime(2026, 5, 7, tzinfo=UTC),
        source="pypi",
        url="https://pypi.org/project/alysis-code/",
    )
    monkeypatch.setattr(cli_mod, "check_for_updates", lambda **_kwargs: status)

    result = CliRunner().invoke(
        alysis_app,
        ["update", "check", "--json"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["state"] == "update_available"
    assert payload["latest_version"] == "0.1.5"


def test_update_dry_run_shows_detected_command(monkeypatch, tmp_path: Path) -> None:
    status = cli_mod.UpdateStatus(
        current_version="0.1.4",
        latest_version="0.1.5",
        checked_at=datetime(2026, 5, 7, tzinfo=UTC),
        source="pypi",
    )
    monkeypatch.setattr(cli_mod, "check_for_updates", lambda **_kwargs: status)
    monkeypatch.setattr(
        cli_mod,
        "detect_installer_plan",
        lambda: InstallerPlan(
            method="pipx",
            supported=True,
            command=("pipx", "upgrade", "alysis-code"),
            reason="test",
        ),
    )

    result = CliRunner().invoke(
        alysis_app,
        ["update", "--dry-run"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Alysis Code 0.1.5 is available" in result.output
    assert "pipx upgrade alysis-code" in result.output
    assert "Dry run only" in result.output


def test_update_yes_runs_detected_command(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    status = cli_mod.UpdateStatus(
        current_version="0.1.4",
        latest_version="0.1.5",
        checked_at=datetime(2026, 5, 7, tzinfo=UTC),
        source="pypi",
    )
    monkeypatch.setattr(cli_mod, "check_for_updates", lambda **_kwargs: status)
    monkeypatch.setattr(
        cli_mod,
        "detect_installer_plan",
        lambda: InstallerPlan(
            method="pipx",
            supported=True,
            command=("pipx", "upgrade", "alysis-code"),
            reason="test",
        ),
    )

    def fake_run(plan: InstallerPlan) -> int:
        calls.append(list(plan.command))
        return subprocess.CompletedProcess(list(plan.command), 0).returncode

    monkeypatch.setattr(cli_mod, "run_installer_plan", fake_run)

    result = CliRunner().invoke(
        alysis_app,
        ["update", "--yes"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert calls == [["pipx", "upgrade", "alysis-code"]]
    assert "Update command completed" in result.output


def test_update_cache_path_respects_data_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))

    assert update_cache_path() == tmp_path / "data" / "update_check.json"


def test_installer_plan_directs_pipx_users_to_reinstall_under_the_new_name(monkeypatch) -> None:
    """`pipx upgrade` cannot rename an app, so the plan must say so explicitly."""
    monkeypatch.setattr(
        updates_mod,
        "installed_distribution_name",
        lambda preferred=None: RETIRED_PACKAGE_NAME,
    )
    monkeypatch.setattr(updates_mod, "_editable_install_reason", lambda _package_name: None)
    monkeypatch.setattr(
        updates_mod.shutil, "which", lambda name: "/usr/bin/pipx" if name == "pipx" else None
    )

    plan = detect_installer_plan(
        executable=f"/home/me/.local/share/pipx/venvs/{RETIRED_PACKAGE_NAME}/bin/python",
        prefix=f"/home/me/.local/share/pipx/venvs/{RETIRED_PACKAGE_NAME}",
        base_prefix="/usr",
        env={},
    )

    assert plan.supported is False
    assert RETIRED_PACKAGE_NAME in plan.reason
    assert PYTHON_PACKAGE_NAME in plan.reason
    assert plan.command == ("pipx", "uninstall", RETIRED_PACKAGE_NAME)


def test_installer_plan_upgrades_pip_users_to_the_new_package(monkeypatch) -> None:
    monkeypatch.setattr(
        updates_mod,
        "installed_distribution_name",
        lambda preferred=None: RETIRED_PACKAGE_NAME,
    )
    monkeypatch.setattr(updates_mod, "_editable_install_reason", lambda _package_name: None)
    monkeypatch.setattr(updates_mod.shutil, "which", lambda _name: None)

    plan = detect_installer_plan(
        executable="/tmp/project/.venv/bin/python",
        prefix="/tmp/project/.venv",
        base_prefix="/usr",
        env={},
    )

    # The upgrade must target the new package, never the retired one.
    assert PYTHON_PACKAGE_NAME in plan.command
    assert RETIRED_PACKAGE_NAME not in plan.command
    assert f"pip uninstall {RETIRED_PACKAGE_NAME}" in plan.reason


def test_update_check_polls_the_current_package(monkeypatch) -> None:
    """A stale endpoint would report 'up to date' forever."""
    assert updates_mod.PYPI_JSON_URL.endswith(f"/{PYTHON_PACKAGE_NAME}/json")
    assert RETIRED_PACKAGE_NAME not in updates_mod.PYPI_JSON_URL
