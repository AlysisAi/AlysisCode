"""Exercise the real Windows filesystem and ACL boundary on hosted Windows CI."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from alysis_code.managed_session import ManagedSessionError, prepare_managed_session_persistence

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows filesystem contract")


def _acl(path: Path) -> dict:
    # Inspect through an independent OS client, not the implementation's ctypes structs.
    # A Python child of pwsh inherits PS7's module path, which breaks WinPS5
    # cmdlet loading. Let Windows PowerShell construct its own module path.
    environment = {key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"}
    environment["ALYSIS_TEST_ACL_PATH"] = str(path)
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference = 'Stop'; Set-StrictMode -Version Latest; "
            "$acl = Get-Acl -LiteralPath $env:ALYSIS_TEST_ACL_PATH; "
            "$owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value; "
            "$rules = @($acl.GetAccessRules($true,$true,"
            "[System.Security.Principal.SecurityIdentifier]) | ForEach-Object { "
            "@{sid=$_.IdentityReference.Value; inherited=$_.IsInherited; "
            "type=$_.AccessControlType.ToString()} }); "
            "@{owner=$owner; protected=$acl.AreAccessRulesProtected; rules=$rules} | "
            "ConvertTo-Json -Depth 4 -Compress",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("append", [False, True])
def test_owner_only_acl_and_unicode_replay(tmp_path: Path, existing: bool, append: bool) -> None:
    directory = tmp_path / "συνομιλίες-日本語"
    path = directory / "session.jsonl"
    prior = {"type": "user_message", "payload": {"content": "Καλημέρα 世界"}}
    if existing:
        directory.mkdir()
        path.write_text(json.dumps(prior, ensure_ascii=False) + "\n", encoding="utf-8")
    elif not append:
        directory.mkdir()
    persistence = prepare_managed_session_persistence(
        sessions_dir=directory,
        session_id="session",
        append=append,
    )
    try:
        assert persistence.events_snapshot() == ([prior] if existing else [])
        persistence.seal()
        if append:
            writer = persistence.claim_text_writer()
            writer.write('{"type":"final"}\n')
            writer.flush()
    finally:
        persistence.close()
    if existing or append:
        acl = _acl(path)
        assert acl["protected"] is True
        assert acl["rules"] == [{"sid": acl["owner"], "inherited": False, "type": "Allow"}]
        resumed = prepare_managed_session_persistence(
            sessions_dir=directory,
            session_id="session",
            append=False,
        )
        try:
            assert resumed.events_snapshot() == (
                ([prior] if existing else []) + ([{"type": "final"}] if append else [])
            )
        finally:
            resumed.close()
    else:
        assert not path.exists()
    if not existing and append:
        assert _acl(directory)["protected"] is True


def test_junction_directory_is_rejected_without_touching_target(tmp_path: Path) -> None:
    target, junction = tmp_path / "foreign", tmp_path / "sessions"
    target.mkdir()
    subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    with pytest.raises(ManagedSessionError, match="symlink|reparse"):
        prepare_managed_session_persistence(sessions_dir=junction, session_id="session")
    assert list(target.iterdir()) == []


def test_parent_swap_during_leaf_open_cannot_redirect_creation(tmp_path: Path, monkeypatch) -> None:
    from alysis_code import _windows_session_files as native

    directory, detached, foreign = (
        tmp_path / "sessions",
        tmp_path / "detached",
        tmp_path / "foreign",
    )
    directory.mkdir()
    foreign.mkdir()
    original_open = native.open_session_leaf

    def swap_parent(handle, filename, *, append):
        directory.rename(detached)
        subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(directory), str(foreign)],
            check=True,
            capture_output=True,
            timeout=10,
        )
        return original_open(handle, filename, append=append)

    monkeypatch.setattr(native, "open_session_leaf", swap_parent)
    persistence = prepare_managed_session_persistence(sessions_dir=directory, session_id="session")
    try:
        with pytest.raises(ManagedSessionError, match="directory changed"):
            persistence.seal()
    finally:
        persistence.close()
    assert list(foreign.iterdir()) == []
    assert (detached / "session.jsonl").read_bytes() == b""


def test_hard_link_is_rejected_before_acl_mutation(tmp_path: Path) -> None:
    directory = tmp_path / "sessions"
    directory.mkdir()
    foreign = tmp_path / "foreign.jsonl"
    foreign.write_text("private foreign data", encoding="utf-8")
    before = _acl(foreign)
    os.link(foreign, directory / "session.jsonl")
    with pytest.raises(ManagedSessionError, match="hard-linked"):
        prepare_managed_session_persistence(sessions_dir=directory, session_id="session")
    assert foreign.read_text(encoding="utf-8") == "private foreign data"
    assert _acl(foreign) == before


def test_append_handle_cannot_overwrite_history_after_seek(tmp_path: Path) -> None:
    import msvcrt

    directory = tmp_path / "sessions"
    directory.mkdir()
    path = directory / "session.jsonl"
    path.write_bytes(b'{"type":"prior"}\n')
    persistence = prepare_managed_session_persistence(sessions_dir=directory, session_id="session")
    try:
        writer = persistence.claim_text_writer()
        assert msvcrt.get_osfhandle(writer.fileno()) != -1
        os.lseek(writer.fileno(), 0, os.SEEK_SET)
        os.write(writer.fileno(), b'{"type":"new"}\n')
    finally:
        persistence.close()
    assert path.read_bytes() == b'{"type":"prior"}\n{"type":"new"}\n'


@pytest.mark.parametrize("stage", ["descriptor-transfer", "acl"])
def test_admission_failure_releases_handles(tmp_path: Path, monkeypatch, stage: str) -> None:
    import ctypes
    from ctypes import wintypes

    from alysis_code import _windows_session_files as native

    directory = tmp_path / "sessions"
    captured = []
    original_open = native._open_relative

    def capture(*args, **kwargs):
        result = original_open(*args, **kwargs)
        captured.append(result[0])
        return result

    monkeypatch.setattr(native, "_open_relative", capture)
    if stage == "descriptor-transfer":

        def reject_transfer(*_args):
            raise OSError("simulated descriptor allocation failure")

        original_leaf = native.open_session_leaf

        def reject_leaf_transfer(*args, **kwargs):
            with monkeypatch.context() as patch:
                patch.setattr(native.msvcrt, "open_osfhandle", reject_transfer)
                return original_leaf(*args, **kwargs)

        monkeypatch.setattr(native, "open_session_leaf", reject_leaf_transfer)
    else:
        monkeypatch.setattr(native, "_SetSecurityInfo", lambda *_args: 5)
    with pytest.raises(ManagedSessionError):
        prepare_managed_session_persistence(sessions_dir=directory, session_id="session")
    assert captured
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_flags = kernel.GetHandleInformation
    get_flags.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_flags.restype = wintypes.BOOL
    for handle in captured:
        assert not get_flags(handle, ctypes.byref(wintypes.DWORD()))
        assert ctypes.get_last_error() == 6  # ERROR_INVALID_HANDLE


def test_directory_swaps_cannot_substitute_initial_identity(tmp_path: Path, monkeypatch) -> None:
    from alysis_code import _windows_session_files as native

    directory, replacement, detached = (
        tmp_path / "sessions",
        tmp_path / "replacement",
        tmp_path / "detached",
    )
    directory.mkdir()
    replacement.mkdir()
    original_open = native.open_directory
    calls = 0

    def switch_before_open(path):
        nonlocal calls
        calls += 1
        directory.rename(detached)
        replacement.rename(directory)
        fd = original_open(path)
        if calls == 1:
            # Restore A for lstat after opening B. A second path-based probe
            # would see B again; fstat of the first handle must expose the swap.
            directory.rename(replacement)
            detached.rename(directory)
        return fd

    monkeypatch.setattr(native, "open_directory", switch_before_open)
    with pytest.raises(ManagedSessionError, match="directory changed"):
        prepare_managed_session_persistence(sessions_dir=directory, session_id="session")
    assert calls == 1
    assert list(directory.iterdir()) == []
    assert list(replacement.iterdir()) == []
