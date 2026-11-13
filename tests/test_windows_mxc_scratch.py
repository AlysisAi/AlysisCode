"""A Windows command's scratch home and temp folder, without the real helper.

``tests/test_windows_mxc.py`` runs the same arrangement in the real sandbox
when a helper is supplied.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from alysis_code import windows_mxc as mxc
from alysis_code import windows_mxc_scratch as scratch_module
from alysis_code import windows_sandbox as native
from alysis_code.config import ConfigError
from alysis_code.sandbox_settings import ShellSandboxSettings


@pytest.fixture
def base(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(native, "_local_path", lambda path, **_: Path(path))
    monkeypatch.setattr(scratch_module, "_swept", False)
    monkeypatch.setattr(scratch_module, "_locks", {})
    monkeypatch.setattr(scratch_module, "_left_behind", set())
    return tmp_path / "local" / "alysis-code" / "scratch"


def test_scratch_folders_are_where_windows_puts_them(tmp_path):
    scratch = tmp_path / "ab12"
    env = scratch_module.scratch_environment(scratch)
    home = scratch / "home"
    assert env["USERPROFILE"] == str(home)
    assert env["HOMEDRIVE"] + env["HOMEPATH"] == str(home)
    assert env["APPDATA"] == str(home / "AppData" / "Roaming")
    assert env["LOCALAPPDATA"] == str(home / "AppData" / "Local")
    assert env["TEMP"] == env["TMP"] == env["TMPDIR"] == str(home / "AppData" / "Local" / "Temp")


def test_a_scratch_folder_is_created_outside_any_project_and_removed(base):
    scratch = scratch_module.create_scratch()
    assert scratch is not None and scratch.parent == base
    for value in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP"):
        assert Path(scratch_module.scratch_environment(scratch)[value]).is_dir()
    assert (base / (scratch.name + ".lock")).is_file()
    (scratch / "home" / "left-behind.txt").write_text("x")
    scratch_module.remove_scratch(scratch)
    assert list(base.iterdir()) == []


def test_every_command_gets_its_own_scratch_folder(base):
    first, second = scratch_module.create_scratch(), scratch_module.create_scratch()
    assert first != second and first.is_dir() and second.is_dir()
    scratch_module.remove_scratch(first)
    assert not first.exists() and second.is_dir()
    scratch_module.remove_scratch(second)


def test_removing_nothing_is_harmless():
    scratch_module.remove_scratch(None)


def test_no_scratch_folder_when_the_location_is_unknown(base, monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA")
    assert scratch_module.create_scratch() is None
    monkeypatch.setenv("LOCALAPPDATA", "relative")
    assert scratch_module.create_scratch() is None


def test_no_scratch_folder_when_the_sandbox_could_not_contain_it(base, monkeypatch):
    def refuse(path, **_):
        raise ConfigError("Sandbox roots and working directories cannot traverse reparse points")

    monkeypatch.setattr(native, "_local_path", refuse)
    assert scratch_module.create_scratch() is None


def test_a_folder_left_by_a_crashed_process_is_removed_by_the_next(base):
    base.mkdir(parents=True)
    abandoned = base / "0123456789"
    (abandoned / "home").mkdir(parents=True)
    (abandoned / "home" / "cache.bin").write_bytes(b"x")
    (base / "0123456789.lock").write_bytes(b"")
    unlocked = base / "aaaaaaaaaa"
    unlocked.mkdir()
    old = time.time() - 3 * 24 * 60 * 60
    os.utime(unlocked, (old, old))
    recent = base / "bbbbbbbbbb"
    recent.mkdir()
    scratch = scratch_module.create_scratch()
    assert not abandoned.exists() and not (base / "0123456789.lock").exists()
    assert not unlocked.exists()
    # A folder with no lock that is still fresh may be one being removed.
    assert recent.is_dir()
    scratch_module.remove_scratch(scratch)


@pytest.mark.skipif(os.name != "nt", reason="Windows refuses to rename a file held open")
def test_a_folder_another_process_still_uses_is_kept(base):
    base.mkdir(parents=True)
    in_use = base / "cccccccccc"
    in_use.mkdir()
    held = os.open(base / "cccccccccc.lock", os.O_CREAT | os.O_WRONLY)
    try:
        scratch = scratch_module.create_scratch()
        assert in_use.is_dir() and (base / "cccccccccc.lock").exists()
        scratch_module.remove_scratch(scratch)
    finally:
        os.close(held)


@pytest.mark.skipif(os.name != "nt", reason="Windows refuses to delete a file held open")
def test_a_folder_that_cannot_be_deleted_yet_gives_up_its_lock_and_goes_later(base):
    """A stopped background command can still have a process holding a file."""
    scratch = scratch_module.create_scratch()
    lock = base / (scratch.name + ".lock")
    held = (scratch / "home" / "held.txt").open("w")
    try:
        scratch_module.remove_scratch(scratch)
        assert scratch.exists() and scratch in scratch_module._left_behind
        # The lock is no longer held: a later process may take the folder over.
        os.replace(lock, str(lock) + ".probe")
        os.replace(str(lock) + ".probe", lock)
    finally:
        held.close()
    following = scratch_module.create_scratch()
    assert not scratch.exists() and not lock.exists()
    assert scratch_module._left_behind == set()
    scratch_module.remove_scratch(following)
    assert list(base.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows refuses to delete a file held open")
def test_removal_can_wait_for_a_process_on_its_way_out(base):
    import threading

    scratch = scratch_module.create_scratch()
    held = (scratch / "home" / "held.txt").open("w")
    threading.Timer(0.5, held.close).start()
    started = time.monotonic()
    scratch_module.remove_scratch(scratch, wait_s=5.0)
    assert not scratch.exists() and list(base.iterdir()) == []
    assert 0.3 < time.monotonic() - started < 4.0


def test_the_sweep_runs_once_per_process(base):
    first = scratch_module.create_scratch()
    late = base / "dddddddddd"
    late.mkdir()
    (base / "dddddddddd.lock").write_bytes(b"")
    second = scratch_module.create_scratch()
    assert late.is_dir()
    scratch_module.remove_scratch(first)
    scratch_module.remove_scratch(second)


class _Reservations:
    def __init__(self):
        self.reserved = []

    def reserve(self, workspace, deny_write):
        self.reserved.append(Path(workspace))


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    """``WindowsMxcRunner._prepare`` with the helper and the host lookups replaced."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    helper = tmp_path / "helper.exe"
    helper.write_bytes(b"helper")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    for name, value in {"SYSTEMDRIVE": "C:", "PROGRAMFILES": "C:\\Program Files"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("COMPUTERNAME", raising=False)
    monkeypatch.setattr(mxc, "resolve_helper", lambda: helper)
    monkeypatch.setattr(mxc, "_python_tool_root", lambda: None)
    monkeypatch.setattr(mxc, "_local_path", lambda path, **_: Path(path))
    monkeypatch.setattr(mxc, "_directory_discovery_roots", lambda: [])
    monkeypatch.setattr(mxc, "_parent_identity", lambda: {"parent_pid": 99, "parent_created": 1})
    monkeypatch.setattr(
        "alysis_code.windows_toolchain.known_folder", lambda _: Path("C:\\ProgramData")
    )
    monkeypatch.setattr(
        "alysis_code.windows_private_state.protect_private_state", lambda request: None
    )

    def build_request(settings, *, root, cwd, cmd, **_):
        writable = settings.filesystem_access == "workspace-write"
        return {
            "protocol": 3,
            "cwd": str(cwd),
            "argv": ["powershell.exe", "-Command", "Set-Location x; " + cmd],
            "read": [str(root)],
            "traverse": [],
            "write": [str(root)] if writable else [],
            "deny_read": [],
            "deny_write": [str(root / ".git") + os.sep],
            "env": [("PATH", "C:\\Windows\\System32"), ("SYSTEMROOT", "C:\\Windows")],
            "network": "off",
        }

    monkeypatch.setattr(mxc, "build_request", build_request)
    reservations = _Reservations()
    token = mxc._foreground_reservations.set(reservations)
    yield workspace, reservations
    mxc._foreground_reservations.reset(token)


def _request(workspace, *, scratch=None, access="workspace-write"):
    runner = mxc.WindowsMxcRunner(ShellSandboxSettings(filesystem_access=access))
    token = mxc._command_scratch.set(scratch)
    try:
        argv, payload = runner._prepare(root=workspace, cwd=workspace, cmd="npm test")
        context = runner._execution_context([str(mxc.resolve_helper())], payload)
    finally:
        mxc._command_scratch.reset(token)
    return json.loads(payload), context


def test_a_command_is_pointed_at_its_scratch_home_and_temp(prepared, tmp_path):
    workspace, reservations = prepared
    scratch = tmp_path / "local" / "alysis-code" / "scratch" / "ab12"
    request, _ = _request(workspace, scratch=scratch)
    env = dict(request["env"])
    for name, value in scratch_module.scratch_environment(scratch).items():
        assert env[name] == value
    # The command may write its project and its scratch folder, nothing else,
    # and only the project is treated as a project.
    assert request["write"] == [str(workspace), str(scratch)]
    assert reservations.reserved == [workspace]
    assert all(not Path(path).is_relative_to(scratch) for path in request["deny_write"])
    # Windows replaces these three at start; the shell restores them first.
    source = request["argv"][-1]
    temp = env["TEMP"]
    assert source.startswith(
        f"$env:TEMP='{temp}'; $env:TMP=$env:TEMP; $env:TMPDIR=$env:TEMP; "
        f"$env:LOCALAPPDATA='{env['LOCALAPPDATA']}'; "
    )
    assert source.endswith("; Set-Location x; npm test")
    assert str(workspace) not in (env["TEMP"], env["USERPROFILE"])


def test_the_folders_the_windows_api_answers_with_are_made_before_the_command(prepared, tmp_path):
    """The API does not read LOCALAPPDATA; an empty answer sends writes into the project."""
    workspace, _ = prepared
    scratch = tmp_path / "local" / "alysis-code" / "scratch" / "ab12"
    source = _request(workspace, scratch=scratch)[0]["argv"][-1]
    made = "[void][Environment]::GetFolderPath($n,'Create')"
    assert "'LocalApplicationData','InternetCache'" in source and made in source
    assert source.index(made) < source.index("Set-Location x")
    # Nothing between the start and that statement runs a cmdlet: PowerShell
    # would look for its module cache before the folder exists.
    before = source[: source.index(made)]
    assert not any(cmdlet in before for cmdlet in ("New-", "Set-", "Get-", "Join-Path"))
    # Without a scratch home the API's answer is Windows' own and exists.
    assert made not in _request(workspace)[0]["argv"][-1]


def test_a_command_is_told_about_the_computer(prepared):
    workspace, _ = prepared
    env = dict(_request(workspace)[0]["env"])
    assert env["SYSTEMDRIVE"] == "C:" and env["PROGRAMFILES"] == "C:\\Program Files"
    # A variable this computer does not have is not invented.
    assert "COMPUTERNAME" not in env


def test_no_variable_of_the_persons_profile_reaches_a_command(prepared, tmp_path, monkeypatch):
    workspace, _ = prepared
    for name in ("USERPROFILE", "APPDATA", "HOMEPATH", "OPENAI_API_KEY", "GITHUB_TOKEN"):
        monkeypatch.setenv(name, "C:\\Users\\person\\private")
    for scratch in (None, tmp_path / "local" / "alysis-code" / "scratch" / "ab12"):
        request, _ = _request(workspace, scratch=scratch)
        assert "C:\\Users\\person\\private" not in json.dumps(request)


def test_without_a_scratch_folder_the_working_directory_stays_the_temp_folder(prepared):
    workspace, reservations = prepared
    request, _ = _request(workspace)
    env = dict(request["env"])
    assert env["TEMP"] == env["TMP"] == env["TMPDIR"] == str(workspace)
    assert "USERPROFILE" not in env and "APPDATA" not in env
    assert request["write"] == [str(workspace)]
    assert "$env:LOCALAPPDATA" not in request["argv"][-1]
    assert reservations.reserved == [workspace]


def test_a_command_that_cannot_write_gets_no_writable_scratch(prepared, tmp_path):
    workspace, reservations = prepared
    request, _ = _request(workspace, scratch=tmp_path / "ab12", access="read-only")
    assert request["write"] == [] and reservations.reserved == []


def test_two_runs_with_the_same_rights_compare_as_the_same_execution(prepared, tmp_path):
    workspace, _ = prepared
    base = tmp_path / "local" / "alysis-code" / "scratch"
    first = _request(workspace, scratch=base / "aaaa")
    second = _request(workspace, scratch=base / "bbbb")
    assert first[0]["env"] != second[0]["env"]
    assert first[1] == second[1]
    # Different rights still compare as different.
    assert _request(workspace)[1] != first[1]


def test_a_quote_in_the_scratch_path_cannot_add_statements(prepared, tmp_path):
    workspace, _ = prepared
    scratch = tmp_path / "local’s" / "it's" / "ab12"
    source = _request(workspace, scratch=scratch)[0]["argv"][-1]
    temp = str(scratch / "home" / "AppData" / "Local" / "Temp")
    assert f"$env:TEMP='{native.powershell_literal(temp)}'; " in source
    assert "’’" in source and "''" in source
