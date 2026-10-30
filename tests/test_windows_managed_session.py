from __future__ import annotations

import ctypes
import json
import os
import subprocess
from ctypes import wintypes as w
from pathlib import Path

import pytest

from alysis_code.managed_session import ManagedSessionError, prepare_managed_session_persistence

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows managed-session handles")


def test_missing_readonly_session_pins_directory_without_leaf_handle(tmp_path: Path) -> None:
    directory = tmp_path / "sessions"
    directory.mkdir()
    persistence = prepare_managed_session_persistence(
        sessions_dir=directory, session_id="absent", append=False
    )
    try:
        assert not persistence.path.exists()
        with pytest.raises(PermissionError):
            directory.rename(tmp_path / "moved")
    finally:
        persistence.close()
    directory.rename(tmp_path / "moved")


def test_directory_is_pinned_before_leaf_admission(tmp_path: Path, monkeypatch) -> None:
    from alysis_code._windows_managed_session import _Api

    directory = tmp_path / "sessions"
    directory.mkdir()
    original = _Api.open_relative
    checked = []

    def open_relative(self, parent, name, **kwargs):
        if not kwargs["directory"]:
            with pytest.raises(PermissionError):
                directory.rename(tmp_path / "moved")
            checked.append(name)
        return original(self, parent, name, **kwargs)

    monkeypatch.setattr(_Api, "open_relative", open_relative)
    persistence = prepare_managed_session_persistence(
        sessions_dir=directory, session_id="absent", append=False
    )
    persistence.close()
    assert checked == ["absent.jsonl.uncommitted.json", "absent.jsonl"]


def _sddl(path: Path) -> str:
    from alysis_code._windows_managed_session import _Api

    api = _Api()
    convert = api.advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [w.LPVOID, w.DWORD, w.DWORD, ctypes.POINTER(w.LPWSTR), w.LPVOID]
    convert.restype = w.BOOL
    handle = api.kernel.CreateFileW(str(path), 0x20000, 3, None, 3, 0x02200000, None)
    assert handle != w.HANDLE(-1).value
    descriptor, value = w.LPVOID(), w.LPWSTR()
    try:
        assert (
            api.advapi.GetSecurityInfo(
                handle, 1, 5, None, None, None, None, ctypes.byref(descriptor)
            )
            == 0
        )
        assert convert(descriptor, 1, 5, ctypes.byref(value), None)
        return value.value or ""
    finally:
        api.kernel.LocalFree(value)
        api.kernel.LocalFree(descriptor)
        api.close(handle)


def _junction(link: Path, target: Path) -> None:
    # Test-created local paths only. Directory junctions do not need symlink privilege.
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _assert_private_file_acl(path: Path, expected_owner: str) -> None:
    """Compare native SID/ACE values; SDDL can abbreviate built-in users as LA."""
    from alysis_code._windows_managed_session import _Api

    class AclHeader(ctypes.Structure):
        _fields_ = [
            ("revision", w.BYTE),
            ("reserved", w.BYTE),
            ("size", w.WORD),
            ("count", w.WORD),
            ("reserved2", w.WORD),
        ]

    class AllowAce(ctypes.Structure):
        _fields_ = [("kind", w.BYTE), ("flags", w.BYTE), ("size", w.WORD), ("mask", w.DWORD)]

    api = _Api()
    control_api = api.advapi.GetSecurityDescriptorControl
    control_api.argtypes = [w.LPVOID, ctypes.POINTER(w.WORD), ctypes.POINTER(w.DWORD)]
    control_api.restype = w.BOOL
    ace_api = api.advapi.GetAce
    ace_api.argtypes = [w.LPVOID, w.DWORD, ctypes.POINTER(w.LPVOID)]
    ace_api.restype = w.BOOL
    handle = api.kernel.CreateFileW(str(path), 0x20000, 3, None, 3, 0x02200000, None)
    assert handle != w.HANDLE(-1).value
    descriptor, owner = w.LPVOID(), w.LPVOID()
    try:
        assert (
            api.advapi.GetSecurityInfo(
                handle, 1, 5, ctypes.byref(owner), None, None, None, ctypes.byref(descriptor)
            )
            == 0
        )
        assert api.sid_string(owner.value) == expected_owner
        control, revision = w.WORD(), w.DWORD()
        assert control_api(descriptor, ctypes.byref(control), ctypes.byref(revision))
        assert control.value & 0x1000  # SE_DACL_PROTECTED
        present, defaulted, acl = w.BOOL(), w.BOOL(), w.LPVOID()
        assert api.advapi.GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)
        )
        assert present.value and acl.value and not defaulted.value
        assert AclHeader.from_address(acl.value).count == 1
        ace = w.LPVOID()
        assert ace_api(acl, 0, ctypes.byref(ace))
        entry = AllowAce.from_address(ace.value)
        assert (entry.kind, entry.flags, entry.mask) == (0, 0, 0x001F01FF)
        assert api.sid_string(ace.value + ctypes.sizeof(AllowAce)) == expected_owner
    finally:
        api.kernel.LocalFree(descriptor)
        api.close(handle)


@pytest.mark.parametrize("append", [True, False])
def test_windows_replay_uses_private_retained_file(tmp_path: Path, append: bool) -> None:
    from alysis_code._windows_managed_session import _Api

    path = tmp_path / "chat.jsonl"
    prior = {"type": "user_message", "payload": {"content": "prior"}}
    path.write_text(json.dumps(prior) + "\n", encoding="utf-8")
    persistence = prepare_managed_session_persistence(
        sessions_dir=tmp_path, session_id="chat", append=append
    )
    try:
        assert persistence.events_snapshot() == [prior]
        persistence.seal()
        _assert_private_file_acl(path, _Api().current_user())
        if append:
            writer = persistence.claim_text_writer()
            writer.write('{"type":"final"}\n')
            writer.flush()
        else:
            with pytest.raises(ManagedSessionError, match="not open for append"):
                persistence.claim_text_writer()
    finally:
        persistence.close()
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert events == ([prior, {"type": "final"}] if append else [prior])


def test_windows_creates_private_nested_directory_and_log(tmp_path: Path) -> None:
    sessions = tmp_path / "new-parent" / "sessions"
    persistence = prepare_managed_session_persistence(sessions_dir=sessions, session_id="chat")
    try:
        assert persistence.events_snapshot() == []
        assert "D:P" in _sddl(sessions)
        assert "D:P" in _sddl(sessions / "chat.jsonl")
        persistence.seal()
        assert os.get_inheritable(persistence.claim_text_writer().fileno()) is False
    finally:
        persistence.close()


@pytest.mark.parametrize("sealed", [False, True])
@pytest.mark.parametrize("target", ["leaf", "directory", "ancestor"])
def test_windows_pins_every_path_component_until_close(
    tmp_path: Path, sealed: bool, target: str
) -> None:
    parent = tmp_path / "parent"
    sessions = parent / "sessions"
    persistence = prepare_managed_session_persistence(sessions_dir=sessions, session_id="chat")
    path = {"leaf": sessions / "chat.jsonl", "directory": sessions, "ancestor": parent}[target]
    replacement = path.with_name(path.name + "-moved")
    try:
        if sealed:
            persistence.seal()
        with pytest.raises(PermissionError) as error:
            path.rename(replacement)
        assert error.value.winerror in {5, 32}  # Directory rename reports ACCESS_DENIED.
        persistence.validate_bound_path()
        writer = persistence.claim_text_writer()
        writer.write('{"type":"final"}\n')
        writer.flush()
    finally:
        persistence.close()
    path.rename(replacement)  # All pins were actually released.


@pytest.mark.parametrize("alias_component", ["directory", "ancestor", "leaf"])
def test_windows_reparse_rejection_does_not_create_or_read_target(
    tmp_path: Path, alias_component: str
) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    original = '{"private":true}\n'
    (foreign / "chat.jsonl").write_text(original, encoding="utf-8")
    if alias_component == "leaf":
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        alias = sessions / "chat.jsonl"
    else:
        alias = tmp_path / "alias"
        sessions = alias / "would-create" if alias_component == "ancestor" else alias
    _junction(alias, foreign)
    before = _sddl(foreign / "chat.jsonl")
    try:
        with pytest.raises(ManagedSessionError):
            prepare_managed_session_persistence(sessions_dir=sessions, session_id="chat")
        assert not (foreign / "would-create").exists()
        assert (foreign / "chat.jsonl").read_text(encoding="utf-8") == original
        assert _sddl(foreign / "chat.jsonl") == before
    finally:
        alias.rmdir()  # Remove only the fixture junction, never its target tree.


def test_windows_hardlink_rejected_before_dacl_or_content_change(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign.jsonl"
    foreign.write_text('{"private":true}\n', encoding="utf-8")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "chat.jsonl").hardlink_to(foreign)
    before = _sddl(foreign)
    with pytest.raises(ManagedSessionError, match="hard-linked"):
        prepare_managed_session_persistence(sessions_dir=sessions, session_id="chat")
    assert foreign.read_text(encoding="utf-8") == '{"private":true}\n'
    assert _sddl(foreign) == before


def test_windows_foreign_owner_rejected_before_mutation(tmp_path: Path, monkeypatch) -> None:
    from alysis_code._windows_managed_session import _Api

    path = tmp_path / "foreign.jsonl"
    path.write_text('{"private":true}\n', encoding="utf-8")
    before = _sddl(path)
    api = _Api()
    owner = api.current_user()
    descriptor = api.private_descriptor(owner)
    handle = api.kernel.CreateFileW(str(path), 0x60080, 3, None, 3, 0x00200000, None)
    assert handle != w.HANDLE(-1).value
    # No account/owner mutation is needed to exercise the foreign-owner decision.
    monkeypatch.setattr(api, "sid_string", lambda _sid: "S-1-5-18")
    try:
        with pytest.raises(ManagedSessionError, match="owned by the current user"):
            api.admit_private_file(handle, owner=owner, descriptor=descriptor)
    finally:
        api.close(handle)
        api.kernel.LocalFree(descriptor)
    assert _sddl(path) == before
    assert path.read_text(encoding="utf-8") == '{"private":true}\n'


def test_windows_replay_missing_file_does_not_create_it(tmp_path: Path) -> None:
    persistence = prepare_managed_session_persistence(
        sessions_dir=tmp_path, session_id="missing", append=False
    )
    try:
        persistence.seal()
        assert persistence.events_snapshot() == []
        assert not (tmp_path / "missing.jsonl").exists()
    finally:
        persistence.close()
