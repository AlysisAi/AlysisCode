from __future__ import annotations

import json
from pathlib import Path

import pytest

from alysis_code import windows_private_state as policy
from alysis_code.config import ConfigError
from alysis_code.windows_mxc_contract import engine_capabilities


@pytest.fixture
def scope(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    config = tmp_path / "settings"
    data = tmp_path / "data"
    config.mkdir()
    data.mkdir()
    monkeypatch.setattr(policy.config, "config_path", lambda: config / "config.json")
    monkeypatch.setattr(policy.config, "_data_dir", lambda: data)
    monkeypatch.delenv("ALYSIS_DESKTOP_CONTROL_PATHS", raising=False)
    monkeypatch.delenv("SYLLIPTOR_DESKTOP_CONTROL_PATHS", raising=False)
    monkeypatch.delenv("ALYSIS_DESKTOP_USER_DATA", raising=False)
    monkeypatch.delenv("SYLLIPTOR_DESKTOP_USER_DATA", raising=False)
    return work, config, data


def request(work):
    return {
        "cwd": str(work),
        "read": [str(work)],
        "write": [str(work)],
        "deny_read": [],
        "deny_write": [],
    }


def test_engine_advertises_private_control_path_enforcement():
    assert engine_capabilities()["private_state_protection"] == "host-control-paths-v1"


def test_ordinary_workspace_does_not_acquire_private_grants(scope):
    work, _, _ = scope
    value = request(work)
    policy.protect_private_state(value)
    assert value == request(work)


def test_directory_discovery_keeps_private_contents_and_metadata_denied(scope):
    work, config, data = scope
    value = request(work)
    value["enumerate"] = [str(work.parent)]
    policy.protect_private_state(value)
    assert set(value["deny_read"]) == {str(config), str(data)}
    assert value["deny_write"] == value["deny_read"]
    assert value["read"] == value["write"] == [str(work)]


def test_discovery_protects_future_private_state_without_creating_it(scope, monkeypatch):
    work, _, _ = scope
    parent = work.parent / "private-parent"
    parent.mkdir()
    missing = parent / "future" / "settings"
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", json.dumps([str(missing)]))
    value = request(work)
    value["enumerate"] = [str(work.parent)]
    policy.protect_private_state(value)
    assert str(parent) in value["discovery_deny"]
    assert not missing.exists()


def test_discovery_with_shared_private_parent_keeps_existing_content_policy(scope, monkeypatch):
    work, _, _ = scope
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", json.dumps([str(work.parent / "future")]))
    value = request(work)
    value["enumerate"] = [str(work.parent)]
    policy.protect_private_state(value)
    assert value["enumerate"] == [str(work.parent)]
    assert value["discovery_deny"] == [str(work.parent)]
    assert value["read"] == value["write"] == [str(work)]
    assert str(work.parent) not in value["deny_read"]


@pytest.mark.parametrize("scope_kind", ["private-root", "private-parent"])
def test_legacy_native_rejects_private_secondary_before_reservations(
    scope, monkeypatch, scope_kind
):
    from alysis_code import windows_sandbox as native
    from alysis_code.sandbox_settings import ShellSandboxSettings

    work, config, _ = scope
    helper = work.parent / "helper"
    helper.mkdir()
    monkeypatch.setattr(native, "resolve_helper", lambda: helper / "helper.exe")
    # This unit test exercises private-root admission, not host PowerShell
    # startup. Policy-query failures have dedicated tests in test_windows_sandbox.
    monkeypatch.setattr(native, "_powershell_execution_policy", lambda _shell: "RemoteSigned")
    selected = config if scope_kind == "private-root" else config.parent
    runner = native.WindowsNativeRunner(
        ShellSandboxSettings(additional_workspace_roots=(str(selected),))
    )
    with pytest.raises(ConfigError, match="[Pp]rivate|trusted engine"):
        runner._prepare(root=work, cwd=work, cmd="MUST_NOT_RUN")
    assert list(work.iterdir()) == []
    assert list(config.iterdir()) == []


def test_enclosing_workspace_denies_and_pins_effective_cli_state(scope):
    work, config, data = scope
    value = request(work.parent)
    policy.protect_private_state(value)
    assert set(value["deny_read"]) == {str(config), str(data)}
    assert value["deny_write"] == value["deny_read"]


@pytest.mark.parametrize("descendant", [False, True])
def test_private_working_directory_refused(scope, descendant):
    _, config, _ = scope
    cwd = config / "nested" if descendant else config
    cwd.mkdir(exist_ok=True)
    with pytest.raises(ConfigError, match="cannot be a sandbox working directory"):
        policy.protect_private_state(request(cwd))


def test_desktop_controls_protected_without_blocking_managed_work_tasks(scope, monkeypatch):
    work, _, _ = scope
    control = work / "runtime-supervisor.json"
    control.write_text("synthetic settings")
    tasks = work / "work-tasks" / "task"
    tasks.mkdir(parents=True)
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", json.dumps([str(control)]))
    broad = request(work)
    policy.protect_private_state(broad)
    assert broad["deny_read"] == broad["deny_write"] == [str(control)]
    narrow = request(tasks)
    policy.protect_private_state(narrow)
    assert narrow["deny_read"] == narrow["deny_write"] == []


def test_absent_control_target_inside_grant_fails_without_creation(scope, monkeypatch):
    work, _, _ = scope
    missing = work / "future-config"
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", json.dumps([str(missing)]))
    with pytest.raises(ConfigError, match="state.*missing"):
        policy.protect_private_state(request(work))
    assert not missing.exists()


@pytest.mark.parametrize("exists", [False, True])
def test_custom_memory_inside_workspace_is_private(scope, monkeypatch, exists):
    work, _, _ = scope
    memory = work / "custom-memory"
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(memory))
    value = request(work)
    if exists:
        memory.mkdir()
        policy.protect_private_state(value)
        assert value["deny_read"] == value["deny_write"] == [str(memory)]
    else:
        with pytest.raises(ConfigError, match="state.*missing"):
            policy.protect_private_state(value)
        assert not memory.exists()


def test_default_memory_is_covered_by_data_root(scope, monkeypatch):
    work, config, data = scope
    monkeypatch.delenv("ALYSIS_MEMORY_HOME", raising=False)
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(data))
    value = request(work.parent)
    policy.protect_private_state(value)
    assert set(value["deny_read"]) == {str(config), str(data)}
    assert not (data / "memory").exists()


def test_read_only_tool_root_cannot_disclose_private_state(scope):
    work, config, data = scope
    value = request(work)
    value["read"].append(str(work.parent))
    policy.protect_private_state(value)
    assert set(value["deny_read"]) == {str(config), str(data)}


@pytest.mark.parametrize("ancestor", [False, True])
def test_desktop_data_ancestor_grant_refused_even_with_narrow_cwd(scope, monkeypatch, ancestor):
    work, _, _ = scope
    parent = work / "desktop"
    tasks = parent / "work-tasks" / "task"
    tasks.mkdir(parents=True)
    monkeypatch.setenv("ALYSIS_DESKTOP_USER_DATA", str(parent))
    value = request(tasks)
    value["read"].append(str(work if ancestor else parent))
    with pytest.raises(ConfigError, match="cannot contain.*data folder"):
        policy.protect_private_state(value)


def test_desktop_managed_workspace_stays_available(scope, monkeypatch):
    work, _, _ = scope
    tasks = work / "desktop" / "work-tasks" / "task"
    tasks.mkdir(parents=True)
    monkeypatch.setenv("ALYSIS_DESKTOP_USER_DATA", str(work / "desktop"))
    value = request(tasks)
    policy.protect_private_state(value)
    assert value["deny_read"] == []


@pytest.mark.parametrize("raw", ["{", "{}", "[42]", "[null]", '["relative"]', '["\\u0000"]'])
def test_invalid_desktop_policy_fails_closed(scope, monkeypatch, raw):
    work, _, _ = scope
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", raw)
    with pytest.raises(ConfigError, match="private application|Private application"):
        policy.protect_private_state(request(work))


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows path casing")
def test_case_variant_control_is_protected(scope, monkeypatch):
    work, _, _ = scope
    private = work / "private"
    private.mkdir()
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", json.dumps([str(private).upper()]))
    value = request(work)
    policy.protect_private_state(value)
    assert Path(value["deny_read"][0]) == private
    assert value["deny_write"] == value["deny_read"]
