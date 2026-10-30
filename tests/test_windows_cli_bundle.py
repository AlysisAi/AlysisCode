from __future__ import annotations

import hashlib
import os
import platform
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from alysis_code import windows_mxc as mxc
from alysis_code import windows_mxc_bundle as bundle
from alysis_code.background_runner import build_background_shell_runner_from_settings
from alysis_code.config import AppConfig, ConfigError
from alysis_code.durable_service_manager import DurableServiceManager
from alysis_code.sandbox_runner import (
    build_shell_runner_from_settings,
    select_shell_sandbox_backend,
)
from alysis_code.sandbox_settings import ShellSandboxSettings, resolve_shell_sandbox_settings


@pytest.fixture
def no_helper_overrides(monkeypatch):
    for prefix in ("ALYSIS", "SYLLIPTOR"):
        for name in (
            "WINDOWS_MXC_HELPER",
            "WINDOWS_MXC_SHA256",
            "SHELL_SANDBOX_BACKEND",
            "SHELL_SANDBOX_MODE",
        ):
            monkeypatch.delenv(f"{prefix}_{name}", raising=False)


def test_checked_in_bundle_is_complete_and_pinned():
    helper = bundle.verify_bundle()
    assert helper.stat().st_size == bundle.HELPER_BYTES
    assert set(path.name for path in helper.parent.iterdir()) == set(bundle.BUNDLE_FILES)


def test_release_hook_refuses_modified_native_sources(tmp_path, monkeypatch):
    import json

    from hatch_build import CustomBuildHook

    monkeypatch.delenv("ALYSIS_LOCAL_CANDIDATE", raising=False)
    root = Path(__file__).resolve().parents[1]
    candidate = tmp_path / "release"
    module = Path("src/alysis_code/windows_mxc_bundle.py")
    (candidate / module).parent.mkdir(parents=True)
    shutil.copy2(root / module, candidate / module)
    assets = Path("src/alysis_code/assets/windows-mxc")
    shutil.copytree(root / assets, candidate / assets)
    inventory = json.loads((root / assets / "SOURCE.json").read_text(encoding="utf-8"))
    inputs = [
        name
        for name in inventory["files"]
        if name.startswith("native/windows-sandbox/")
        or name in {"src/alysis_code/windows_mxc_contract.py", "scripts/build_windows_mxc.py"}
    ]
    for name in inputs:
        relative = Path(name)
        assert not relative.is_absolute() and ".." not in relative.parts
        (candidate / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, candidate / relative)
    changed = candidate / next(name for name in inputs if name.startswith("native/"))
    changed.write_bytes(changed.read_bytes() + b"\n# modified after qualification\n")
    hook = CustomBuildHook(str(candidate), {}, None, None, "unused", "wheel")
    with pytest.raises(ValueError, match="Rebuild/sign the bundled sandbox"):
        hook.initialize("standard", {})


def test_frozen_file_worker_uses_fixed_dispatch(monkeypatch):
    from alysis_code import contained_read_tools

    monkeypatch.setattr(contained_read_tools.sys, "frozen", True, raising=False)
    assert contained_read_tools._worker_argv() == [
        contained_read_tools.sys.executable,
        "--alysis-internal-file-worker",
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows runtime cache")
def test_shell_policy_protects_all_helper_cache_versions(tmp_path, monkeypatch):
    from alysis_code.windows_sandbox import build_request

    cache = tmp_path / "cache"
    monkeypatch.setattr(bundle, "runtime_cache_root", lambda: cache)
    with pytest.raises(ConfigError, match="uninitialized helper cache"):
        build_request(
            ShellSandboxSettings(),
            root=tmp_path,
            cwd=tmp_path,
            cmd="unused",
            helper=bundle.BUNDLE_ROOT / "alysis-sandbox.exe",
        )
    cache.mkdir()
    request = build_request(
        ShellSandboxSettings(),
        root=tmp_path,
        cwd=tmp_path,
        cmd="unused",
        helper=bundle.BUNDLE_ROOT / "alysis-sandbox.exe",
    )
    assert str(cache) in request["deny_write"]


@pytest.mark.parametrize("name", tuple(bundle.BUNDLE_FILES))
@pytest.mark.parametrize("action", ("missing", "changed"))
def test_missing_or_changed_release_input_is_rejected(tmp_path, name, action):
    shutil.copytree(bundle.BUNDLE_ROOT, tmp_path / "bundle")
    file = tmp_path / "bundle" / name
    if action == "missing":
        file.unlink()
    else:
        file.write_bytes(b"tampered")
    with pytest.raises((OSError, ValueError)):
        bundle.verify_bundle(tmp_path / "bundle")


def test_bundle_rejects_junction_parent(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows junction")
    import subprocess

    linked = tmp_path / "linked"
    subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(linked), str(bundle.BUNDLE_ROOT)],
        check=True,
        capture_output=True,
    )
    try:
        with pytest.raises(ValueError, match="reparse"):
            bundle.verify_bundle(linked)
    finally:
        linked.rmdir()


@pytest.mark.parametrize(
    "system,expected", [("Windows", "windows-mxc"), ("Linux", "auto"), ("Darwin", "auto")]
)
def test_default_settings_select_platform_backend(
    no_helper_overrides, monkeypatch, system, expected
):
    monkeypatch.setattr(platform, "system", lambda: system)
    settings = resolve_shell_sandbox_settings(AppConfig(model="fixture"))
    assert settings.backend == expected
    assert settings.mode == "strict"


@pytest.mark.parametrize("backend", ("docker", "bwrap", "windows-native"))
def test_explicit_backend_is_preserved(no_helper_overrides, monkeypatch, backend):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    cfg = AppConfig(model="fixture", extra_fields={"shell_sandbox": {"backend": backend}})
    assert resolve_shell_sandbox_settings(cfg).backend == backend


def test_full_access_remains_explicit(no_helper_overrides, monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    cfg = AppConfig(model="fixture", extra_fields={"shell_sandbox": {"mode": "off"}})
    settings = resolve_shell_sandbox_settings(cfg)
    assert select_shell_sandbox_backend(settings).backend == "host"


@pytest.mark.parametrize(
    "factory", (build_shell_runner_from_settings, build_background_shell_runner_from_settings)
)
def test_default_cannot_fall_back_even_with_docker_and_host_approval(
    tmp_path, monkeypatch, factory
):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(shutil, "which", lambda name: f"fixture-{name}")

    def broken():
        raise ConfigError("broken installed bundle")

    monkeypatch.setattr(mxc, "resolve_helper", broken)
    for mode in ("warn", "strict"):
        with pytest.raises(ConfigError, match="broken installed bundle"):
            factory(ShellSandboxSettings(mode=mode, unavailable_fallback="ask"), tmp_path)


def test_default_durable_service_cannot_bypass_windows_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(shutil, "which", lambda name: f"fixture-{name}")
    manager = DurableServiceManager(
        root=tmp_path, state_dir=tmp_path / "state", settings=ShellSandboxSettings()
    )
    with pytest.raises(RuntimeError, match="Host fallback is disabled"):
        manager._build_launch(cmd="must-not-run", cwd=tmp_path, service_id="fixture")


@pytest.mark.parametrize("name", ("ALYSIS_WINDOWS_MXC_HELPER", "ALYSIS_WINDOWS_MXC_SHA256"))
def test_incomplete_override_never_uses_bundled_fallback(
    no_helper_overrides, monkeypatch, tmp_path, name
):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setenv(name, str(tmp_path / "unpaired.exe"))
    monkeypatch.setattr(mxc, "_helper_json", lambda *a: pytest.fail("unexpected helper execution"))
    with pytest.raises(ConfigError, match="requires both"):
        mxc.resolve_helper()


def test_unsupported_windows_arch_fails_before_execution(no_helper_overrides, monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "machine", lambda: "ARM64")
    monkeypatch.setattr(mxc, "_helper_json", lambda *a: pytest.fail("unexpected helper execution"))
    with pytest.raises(ConfigError, match="x64 only"):
        mxc.resolve_helper()


@pytest.mark.skipif(os.name != "nt", reason="Windows hardlink package installation")
def test_hardlinked_installer_payload_gets_private_verified_copy(tmp_path, monkeypatch):
    source = bundle.BUNDLE_ROOT / "alysis-sandbox.exe"
    linked = tmp_path / "shared.exe"
    os.link(source, linked)
    monkeypatch.setattr(bundle, "verify_bundle", lambda: linked)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    try:
        first = bundle.runtime_helper()
        assert first != linked and first.stat().st_nlink == 1
        assert hashlib.sha256(first.read_bytes()).hexdigest() == bundle.HELPER_SHA256
        assert bundle.runtime_helper() == first
        first.write_bytes(b"tampered")
        with pytest.raises(ValueError, match="cache is damaged"):
            bundle.runtime_helper()
    finally:
        linked.unlink()


@pytest.mark.skipif(os.name != "nt", reason="actual Windows CLI sandbox")
def test_default_bundled_helper_runs_without_desktop(no_helper_overrides, tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    cfg = AppConfig(model="fixture")
    assert resolve_shell_sandbox_settings(cfg).backend == "windows-mxc"
    assert mxc.resolve_helper() == bundle.BUNDLE_ROOT / "alysis-sandbox.exe"
    # A read-only workspace runs with the same default backend.
    settings = replace(resolve_shell_sandbox_settings(cfg), filesystem_access="read-only")
    workspace = tmp_path / "readonly"
    workspace.mkdir()
    completed = build_shell_runner_from_settings(settings, workspace).run(
        root=workspace,
        cwd=workspace,
        cmd="try { [IO.File]::WriteAllText('"
        + str(workspace / "bad.txt").replace("'", "''")
        + "','bad'); exit 42 } catch { Write-Output 'blocked' }",
        timeout_s=20,
    )
    assert completed.returncode == 0 and "blocked" in completed.stdout
    assert not (workspace / "bad.txt").exists()
