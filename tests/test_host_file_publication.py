from __future__ import annotations

import errno
import os
from contextlib import nullcontext

import pytest

from alysis_code import host_file_handles as handles
from alysis_code.host_file_policy import HostFilePolicy, HostFilePolicyError

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows native handle publication")


@pytest.mark.parametrize("active", [False, True])
def test_sibling_publication_with_read_share_only_parent(tmp_path, active):
    source, destination = tmp_path / "stage.tmp", tmp_path / "published.txt"
    source.write_bytes(b"synthetic staged bytes")
    parent, _ = handles._windows_open(tmp_path, directory=True, deny_writes=True)
    try:
        context = handles.host_file_action(HostFilePolicy((), ())) if active else nullcontext()
        with context:
            handles.rename_host_file_no_replace(source, destination)
    finally:
        handles._kernel.CloseHandle(parent)
    assert not source.exists()
    assert destination.read_bytes() == b"synthetic staged bytes"


@pytest.mark.parametrize("existing_directory", [False, True])
def test_collision_preserves_source_and_destination(tmp_path, existing_directory):
    source, destination = tmp_path / "stage.tmp", tmp_path / "published.txt"
    source.write_bytes(b"staged")
    if existing_directory:
        destination.mkdir()
    else:
        destination.write_bytes(b"winner")
    with pytest.raises(FileExistsError):
        handles.rename_host_file_no_replace(source, destination)
    assert source.read_bytes() == b"staged"
    assert destination.is_dir() if existing_directory else destination.read_bytes() == b"winner"


def test_different_parent_requires_verified_staging(tmp_path):
    source = tmp_path / "stage.tmp"
    source.write_bytes(b"staged")
    nested = tmp_path / "nested"
    nested.mkdir()
    with pytest.raises(OSError) as failure:
        handles.rename_host_file_no_replace(source, nested / "published.txt")
    assert failure.value.errno == errno.EXDEV
    assert source.read_bytes() == b"staged"
    assert not list(nested.iterdir())


@pytest.mark.parametrize("protected_source", [False, True])
def test_active_policy_guards_source_and_destination(tmp_path, protected_source):
    source, destination = tmp_path / "stage.tmp", tmp_path / "published.txt"
    source.write_bytes(b"staged")
    protected = source if protected_source else destination
    with handles.host_file_action(HostFilePolicy((), (protected,))):
        with pytest.raises(HostFilePolicyError, match="protected"):
            handles.rename_host_file_no_replace(source, destination)
    assert source.read_bytes() == b"staged"
    assert not destination.exists()


def test_readonly_action_cannot_publish(tmp_path):
    source, destination = tmp_path / "stage.tmp", tmp_path / "published.txt"
    source.write_bytes(b"staged")
    with handles.host_file_action(HostFilePolicy((), ()), readonly=True):
        with pytest.raises(HostFilePolicyError, match="read-only"):
            handles.rename_host_file_no_replace(source, destination)
    assert source.read_bytes() == b"staged"
    assert not destination.exists()


def test_hardlinked_source_is_rejected(tmp_path):
    source, destination = tmp_path / "stage.tmp", tmp_path / "published.txt"
    source.write_bytes(b"staged")
    other = tmp_path / "other.txt"
    other.hardlink_to(source)
    with pytest.raises(HostFilePolicyError, match="hard-linked"):
        handles.rename_host_file_no_replace(source, destination)
    assert source.read_bytes() == other.read_bytes() == b"staged"
    assert not destination.exists()


def test_parent_junction_is_rejected_without_following(tmp_path):
    import _winapi

    actual, alias = tmp_path / "actual", tmp_path / "alias"
    actual.mkdir()
    (actual / "stage.tmp").write_bytes(b"protected")
    _winapi.CreateJunction(str(actual), str(alias))
    try:
        with pytest.raises(HostFilePolicyError, match="linked"):
            handles.rename_host_file_no_replace(alias / "stage.tmp", alias / "published.txt")
        assert (actual / "stage.tmp").read_bytes() == b"protected"
        assert not (actual / "published.txt").exists()
    finally:
        alias.rmdir()


@pytest.mark.parametrize("active", [False, True])
def test_parent_metadata_and_source_cannot_be_swapped_after_open(tmp_path, monkeypatch, active):
    import ctypes

    parent = tmp_path / "parent"
    parent.mkdir()
    source, destination = parent / "stage.tmp", parent / "published.txt"
    source.write_bytes(b"staged")
    original = handles._windows_opened_path
    attempts = []

    def checked(handle, *, directory):
        result = original(handle, directory=directory)
        if not directory:
            for path in (source, parent):
                with pytest.raises(OSError) as failure:
                    path.rename(path.with_name(path.name + "-moved"))
                attempts.append(failure.value.winerror)
            # FILE_WRITE_DATA is required for FSCTL_SET_REPARSE_POINT. The
            # publication pin must reject acquiring it even with share-all.
            write = handles._kernel.CreateFileW(str(parent), 2, 7, None, 3, 0x02200000, None)
            assert write == ctypes.c_void_p(-1).value
            attempts.append(ctypes.get_last_error())
        return result

    monkeypatch.setattr(handles, "_windows_opened_path", checked)
    context = handles.host_file_action(HostFilePolicy((), ())) if active else nullcontext()
    with context:
        handles.rename_host_file_no_replace(source, destination)
    assert attempts == [32, 32, 32]
    assert not source.exists()
    assert destination.read_bytes() == b"staged"
    # Publication-scoped handles are released, including an active action.
    parent.rename(tmp_path / "released")


def test_source_junction_inserted_before_relative_open_is_rejected(tmp_path, monkeypatch):
    import _winapi

    source, destination = tmp_path / "stage.tmp", tmp_path / "published.txt"
    source.write_bytes(b"staged")
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "secret.txt").write_bytes(b"synthetic protected")
    original = handles._windows_open

    def swap(path, *, directory, deny_writes=False):
        result = original(path, directory=directory, deny_writes=deny_writes)
        if deny_writes:
            source.unlink()
            _winapi.CreateJunction(str(protected), str(source))
        return result

    monkeypatch.setattr(handles, "_windows_open", swap)
    try:
        with pytest.raises((HostFilePolicyError, IsADirectoryError, PermissionError)):
            handles.rename_host_file_no_replace(source, destination)
        assert not destination.exists()
        assert (protected / "secret.txt").read_bytes() == b"synthetic protected"
    finally:
        source.rmdir()
