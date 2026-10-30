from __future__ import annotations

import ctypes
import os
from pathlib import Path

import pytest

from alysis_code import host_file_handles as handles
from alysis_code.host_file_policy import HostFilePolicy, HostFilePolicyError

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows handle path resolution")


@pytest.fixture(params=[3, 5])
def dos_denied(monkeypatch, tmp_path, request):
    original = handles._kernel.GetFinalPathNameByHandleW
    calls = []

    def query(handle, buffer, capacity, flags):
        calls.append(flags)
        if flags == 0:
            ctypes.set_last_error(request.param)
            return 0
        return original(handle, buffer, capacity, flags)

    monkeypatch.setattr(handles._kernel, "GetFinalPathNameByHandleW", query)
    with handles.windows_workspace_paths(tmp_path):
        calls.clear()
        yield calls


@pytest.mark.parametrize("directory", [False, True])
def test_nt_lookup_recovers_actual_open_object(tmp_path, dos_denied, directory):
    path = tmp_path / "actual object"
    if directory:
        path.mkdir()
    else:
        path.write_bytes(b"synthetic")
    handle, opened = handles._windows_open(path, directory=directory)
    try:
        assert opened == path
        assert os.path.samestat(path.stat(), opened.stat())
        assert dos_denied == [0, 2]
        with pytest.raises(OSError):
            path.rename(tmp_path / "moved")
    finally:
        handles._kernel.CloseHandle(handle)
    path.rename(tmp_path / "released")


def test_publication_works_with_dos_lookup_denied(tmp_path, dos_denied):
    source, destination = tmp_path / "staged", tmp_path / "published"
    source.write_bytes(b"synthetic publication")
    handles.rename_host_file_no_replace(source, destination)
    assert not source.exists()
    assert destination.read_bytes() == b"synthetic publication"
    assert 2 in dos_denied


def test_nt_lookup_does_not_bypass_private_read_policy(tmp_path, dos_denied):
    private = tmp_path / "private"
    private.mkdir()
    secret = private / "canary.txt"
    secret.write_bytes(b"synthetic private")
    handle, opened = handles._windows_open(secret, directory=False)
    try:
        with pytest.raises(HostFilePolicyError, match="private"):
            HostFilePolicy((private,), ()).guard_opened_read(opened)
    finally:
        handles._kernel.CloseHandle(handle)
    assert secret.read_bytes() == b"synthetic private"
    assert 2 in dos_denied


def test_hardlink_rejected_before_any_path_resolution(tmp_path, dos_denied):
    source, alias = tmp_path / "original", tmp_path / "alias"
    source.write_bytes(b"synthetic")
    alias.hardlink_to(source)
    with pytest.raises(HostFilePolicyError, match="hard-linked"):
        handles._windows_open(alias, directory=False)
    assert dos_denied == []


def test_junction_rejected_before_any_path_resolution(tmp_path, dos_denied):
    import _winapi

    target, alias = tmp_path / "target", tmp_path / "alias"
    target.mkdir()
    _winapi.CreateJunction(str(target), str(alias))
    try:
        with pytest.raises(HostFilePolicyError, match="linked"):
            handles._windows_open(alias, directory=True)
        assert dos_denied == []
    finally:
        alias.rmdir()


def test_translated_path_must_identify_original_handle(tmp_path, monkeypatch, dos_denied):
    source, unrelated = tmp_path / "source", tmp_path / "unrelated"
    source.write_bytes(b"synthetic source")
    unrelated.write_bytes(b"synthetic private")
    monkeypatch.setattr(handles, "_windows_nt_dos_path", lambda name: unrelated)
    with pytest.raises(HostFilePolicyError, match="changed during volume resolution"):
        handles._windows_open(source, directory=False)
    # Both handles were released after refusal.
    source.rename(tmp_path / "source-released")
    unrelated.rename(tmp_path / "unrelated-released")


@pytest.mark.parametrize("suffix", ["", "\\folder\\file.txt"])
def test_workspace_mapping_uses_retained_anchor(suffix):
    token = handles._WINDOWS_PATH_ROOT.set(((Path("C:/work"), r"\Device\HarddiskVolume3\work"),))
    try:
        assert handles._windows_nt_dos_path(r"\Device\HarddiskVolume3\work" + suffix) == Path(
            "C:/work" + suffix
        )
    finally:
        handles._WINDOWS_PATH_ROOT.reset(token)


@pytest.mark.parametrize(
    "name",
    [
        r"\Device\HarddiskVolume3\work-other\file",
        r"\Device\HarddiskVolume30\work\file",
        r"\Device\HarddiskVolume3\private\file",
        r"\Device\HarddiskVolume3",
    ],
)
def test_mapping_rejects_siblings_other_volumes_and_ancestors(name):
    token = handles._WINDOWS_PATH_ROOT.set(((Path("C:/work"), r"\Device\HarddiskVolume3\work"),))
    try:
        with pytest.raises(HostFilePolicyError, match="outside"):
            handles._windows_nt_dos_path(name)
    finally:
        handles._WINDOWS_PATH_ROOT.reset(token)


def test_mapping_without_retained_root_refused():
    with pytest.raises(HostFilePolicyError, match="without a retained workspace"):
        handles._windows_nt_dos_path(r"\Device\HarddiskVolume3\work")


def test_mapping_accepts_each_attached_root_without_granting_their_parent():
    token = handles._WINDOWS_PATH_ROOT.set(
        (
            (Path("C:/work"), r"\Device\HarddiskVolume3\work"),
            (Path("D:/extra"), r"\Device\HarddiskVolume4\extra"),
        )
    )
    try:
        assert handles._windows_nt_dos_path(r"\Device\HarddiskVolume4\extra\file") == Path(
            "D:/extra/file"
        )
        assert handles._windows_nt_dos_path(r"\Device\HarddiskVolume3\work\file") == Path(
            "C:/work/file"
        )
        with pytest.raises(HostFilePolicyError, match="outside"):
            handles._windows_nt_dos_path(r"\Device\HarddiskVolume4\sibling\file")
    finally:
        handles._WINDOWS_PATH_ROOT.reset(token)


@pytest.mark.parametrize(
    "name",
    [
        r"\Device\Mup\server\share\file",
        r"\??\C:\file",
        r"C:\file",
        r"\Device\HarddiskVolume3\..\private",
        r"\Device\HarddiskVolume3\file:stream",
        r"\Device\HarddiskVolume3/relative",
        "\\Device\\HarddiskVolume3\\file\0hidden",
    ],
)
def test_unsupported_nt_paths_refused_before_mapping(name):
    with pytest.raises(HostFilePolicyError):
        handles._windows_nt_dos_path(name)


def test_retained_context_releases_root_and_resets_after_failure(tmp_path):
    child = tmp_path / "workspace"
    child.mkdir()
    with pytest.raises(ValueError, match="synthetic"):
        with handles.windows_workspace_paths(child):
            with pytest.raises(OSError):
                child.rename(tmp_path / "moved")
            raise ValueError("synthetic")
    assert handles._WINDOWS_PATH_ROOT.get() is None
    child.rename(tmp_path / "released")


def test_failed_workspace_nt_query_never_enters_context_and_releases_pin(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def denied(handle, buffer, capacity, flags):
        assert flags == 2
        ctypes.set_last_error(5)
        return 0

    monkeypatch.setattr(handles._kernel, "GetFinalPathNameByHandleW", denied)
    with pytest.raises(HostFilePolicyError, match="retained workspace.*error 5"):
        with handles.windows_workspace_paths(workspace):
            pytest.fail("dispatch must not begin without a verified workspace")
    assert handles._WINDOWS_PATH_ROOT.get() is None
    workspace.rename(tmp_path / "released")


@pytest.mark.parametrize("error,result", [(87, 0), (8, 0), (0, 32768)])
def test_unexpected_dos_errors_and_truncation_are_not_masked(tmp_path, monkeypatch, error, result):
    source = tmp_path / "source"
    source.write_bytes(b"synthetic")
    calls = []

    def query(handle, buffer, capacity, flags):
        calls.append(flags)
        ctypes.set_last_error(error)
        return result

    monkeypatch.setattr(handles._kernel, "GetFinalPathNameByHandleW", query)
    with pytest.raises(HostFilePolicyError, match="identify"):
        handles._windows_open(source, directory=False)
    assert calls == [0]
    source.rename(tmp_path / "released")


def test_both_queries_failing_preserves_errors_and_closes_handle(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"synthetic")

    def query(handle, buffer, capacity, flags):
        ctypes.set_last_error(5 if flags == 0 else 87)
        return 0

    monkeypatch.setattr(handles._kernel, "GetFinalPathNameByHandleW", query)
    with pytest.raises(HostFilePolicyError, match="DOS error 5; NT error 87"):
        handles._windows_open(source, directory=False)
    source.rename(tmp_path / "released")


def test_fixed_worker_publishes_large_file_when_dos_and_drive_queries_are_denied(
    tmp_path, monkeypatch, capsys
):
    import hashlib
    import json
    import sys
    from dataclasses import asdict

    from alysis_code import host_read_worker
    from alysis_code.tools.fs import capture_file_precondition

    original = handles._kernel.GetFinalPathNameByHandleW
    attempts = []

    def query(handle, buffer, capacity, flags):
        attempts.append(flags)
        if flags == 0:
            ctypes.set_last_error(5)
            return 0
        return original(handle, buffer, capacity, flags)

    def no_drive_query(*args):
        pytest.fail("contained resolution must not need QueryDosDevice")

    content = "synthetic\n" * 12000
    raw = json.dumps(
        {
            "tool": "fs_write_prepared",
            "arguments": {
                "path": "nested/large.txt",
                "content": content,
                "precondition": asdict(
                    capture_file_precondition(root=tmp_path, path="nested/large.txt")
                ),
            },
        }
    ).encode()
    packet = tmp_path / "request.json"
    packet.write_bytes(raw)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["worker", "--request-file", str(packet), hashlib.sha256(raw).hexdigest()]
    )
    monkeypatch.setattr(handles._kernel, "GetFinalPathNameByHandleW", query)
    monkeypatch.setattr(handles._kernel, "QueryDosDeviceW", no_drive_query)
    assert handles._WINDOWS_PATH_ROOT.get() is None
    assert host_read_worker.main() == 0
    assert json.loads(capsys.readouterr().out)["result"]["created"] is True
    assert (tmp_path / "nested/large.txt").read_text() == content
    assert 0 in attempts and 2 in attempts
    assert handles._WINDOWS_PATH_ROOT.get() is None
    # Recovery must preserve the approval-time stale-file precondition.
    assert host_read_worker.main() == 1
    assert json.loads(capsys.readouterr().out)["stale_path"] == "nested/large.txt"
    assert (tmp_path / "nested/large.txt").read_text() == content
    assert handles._WINDOWS_PATH_ROOT.get() is None
