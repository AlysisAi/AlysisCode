import hashlib
import io
import os
import stat
import tarfile

import pytest

from alysis_code.bots import workspace_archive as snapshots
from alysis_code.bots.workspace_archive import (
    WorkspaceArchiveError,
    pack_workspace,
    restore_workspace,
)


def directory(path):
    path.mkdir(mode=0o700)
    return path


def archive(path, entries):
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as output:
        for name, kind, data in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(data)
            member.mode, member.uid, member.gid = 0o6777, 12345, 54321
            member.mtime = 1
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                member.linkname = "../../outside"
            output.addfile(member, io.BytesIO(data))
    return path


def assert_clean(parent):
    assert not list(parent.glob(".alysis-snapshot-*"))
    assert not list(parent.glob(".alysis-restore-*"))


def test_nested_unicode_binary_roundtrip_and_private_metadata(tmp_path):
    source = directory(tmp_path / "source")
    nested = directory(source / "Νεφέλη 東京")
    directory(nested / "empty")
    (nested / "data.bin").write_bytes(bytes(range(256)) * 1025)
    (source / "empty").write_bytes(b"")
    script = source / "実行.sh"
    script.write_bytes(b"#!/bin/sh\nexit 0\n")
    script.chmod(0o6755)
    packed = tmp_path / "snapshot.tar"
    result = pack_workspace(source, packed)
    assert result == {
        "bytes": packed.stat().st_size,
        "sha256": hashlib.sha256(packed.read_bytes()).hexdigest(),
        "files": 3,
    }
    assert stat.S_IMODE(packed.stat().st_mode) == 0o600
    target = directory(tmp_path / "target")
    restored = restore_workspace(packed, target)
    assert restored == {"files": 3, "bytes": 262400 + len(script.read_bytes())}
    assert (target / "Νεφέλη 東京" / "data.bin").read_bytes() == (nested / "data.bin").read_bytes()
    assert (target / "Νεφέλη 東京" / "empty").is_dir()
    assert (target / "empty").read_bytes() == b""
    assert stat.S_IMODE((target / "実行.sh").stat().st_mode) == 0o700
    assert stat.S_IMODE((target / "Νεφέλη 東京" / "data.bin").stat().st_mode) == 0o600
    assert_clean(tmp_path)


def test_empty_workspace_roundtrip(tmp_path):
    source, target = directory(tmp_path / "source"), directory(tmp_path / "target")
    packed = tmp_path / "archive.tar"
    assert pack_workspace(source, packed)["files"] == 0
    assert restore_workspace(packed, target) == {"files": 0, "bytes": 0}


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "socket", "directory_link"])
def test_pack_rejects_special_source_without_reading_or_publishing(tmp_path, kind, monkeypatch):
    import socket

    source = directory(tmp_path / "source")
    outside = tmp_path / "outside"
    outside.write_bytes(b"private")
    path = source / "entry"
    sock = None
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "directory_link":
        path.symlink_to(tmp_path)
    elif kind == "hardlink":
        os.link(outside, path)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        sock = socket.socket(socket.AF_UNIX)
        monkeypatch.chdir(source)
        sock.bind(path.name)
    try:
        with pytest.raises(WorkspaceArchiveError):
            pack_workspace(source, tmp_path / "new.tar")
    finally:
        if sock:
            sock.close()
    assert not (tmp_path / "new.tar").exists()
    assert outside.read_bytes() == b"private"
    assert_clean(tmp_path)


def test_pack_does_not_overwrite_or_include_itself(tmp_path):
    source = directory(tmp_path / "source")
    target = tmp_path / "old.tar"
    target.write_bytes(b"old")
    with pytest.raises(WorkspaceArchiveError):
        pack_workspace(source, target)
    assert target.read_bytes() == b"old"
    with pytest.raises(WorkspaceArchiveError):
        pack_workspace(source, source / "self.tar")
    assert not list(source.iterdir())
    assert_clean(tmp_path)


def test_pack_rejects_symlinked_root_or_parent(tmp_path):
    source = directory(tmp_path / "source")
    link = tmp_path / "link"
    link.symlink_to(source)
    nested = directory(source / "nested")
    for path in [link, link / nested.name]:
        with pytest.raises(WorkspaceArchiveError):
            pack_workspace(path, tmp_path / "new.tar")
    assert not (tmp_path / "new.tar").exists()


def test_pack_includes_tar_overhead_in_limit(tmp_path, monkeypatch):
    source = directory(tmp_path / "source")
    (source / "data").write_bytes(bytes(19500))
    monkeypatch.setattr(snapshots, "MAX_ARCHIVE_BYTES", 20000)
    with pytest.raises(WorkspaceArchiveError, match="overhead"):
        pack_workspace(source, tmp_path / "new.tar")
    assert not (tmp_path / "new.tar").exists()
    assert_clean(tmp_path)


def test_pack_rejects_entry_overflow_and_cleans_stage(tmp_path, monkeypatch):
    source = directory(tmp_path / "source")
    for i in range(4):
        (source / str(i)).touch()
    monkeypatch.setattr(snapshots, "MAX_ENTRIES", 3)
    with pytest.raises(WorkspaceArchiveError, match="entries"):
        pack_workspace(source, tmp_path / "new.tar")
    assert_clean(tmp_path)


@pytest.mark.parametrize("change", ["current", "previous", "new_entry"])
def test_pack_detects_writers_during_and_after_file_read(tmp_path, monkeypatch, change):
    source = directory(tmp_path / "source")
    (source / "first").write_bytes(b"one")
    (source / "second").write_bytes(b"two")
    original = tarfile.TarFile.addfile
    read = []

    def add(tar, member, fileobj=None):
        result = original(tar, member, fileobj)
        if member.isreg():
            read.append(member.name)
            if change == "current":
                (source / member.name).write_bytes(b"new")
            elif change == "previous" and len(read) == 2:
                (source / read[0]).write_bytes(b"new")
            elif change == "new_entry":
                (source / "new").touch()
        return result

    monkeypatch.setattr(tarfile.TarFile, "addfile", add)
    with pytest.raises(WorkspaceArchiveError, match="changed"):
        pack_workspace(source, tmp_path / "new.tar")
    assert not (tmp_path / "new.tar").exists()
    assert_clean(tmp_path)


@pytest.mark.parametrize(
    "name",
    ["/absolute", "../escape", "x/../escape", "./file", "x//file", "x\\..\\escape", "bad\nfile"],
)
def test_restore_rejects_noncanonical_paths(tmp_path, name):
    packed = archive(tmp_path / "bad.tar", [(name, tarfile.REGTYPE, b"data")])
    dest = directory(tmp_path / "dest")
    with pytest.raises(WorkspaceArchiveError):
        restore_workspace(packed, dest)
    assert list(dest.iterdir()) == []
    assert_clean(tmp_path)


@pytest.mark.parametrize(
    "kind",
    [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE, b"X"],
)
def test_restore_validates_late_unsafe_entry_before_publication(tmp_path, kind):
    packed = archive(tmp_path / "bad.tar", [("good", tarfile.REGTYPE, b"data"), ("bad", kind, b"")])
    dest = directory(tmp_path / "dest")
    with pytest.raises(WorkspaceArchiveError):
        restore_workspace(packed, dest)
    assert list(dest.iterdir()) == []
    assert_clean(tmp_path)


@pytest.mark.parametrize(
    "entries",
    [
        [("same", tarfile.REGTYPE, b"a"), ("same", tarfile.REGTYPE, b"b")],
        [("dir", tarfile.DIRTYPE, b""), ("dir", tarfile.DIRTYPE, b"")],
        [("parent", tarfile.REGTYPE, b"a"), ("parent/child", tarfile.REGTYPE, b"b")],
        [("parent/child", tarfile.REGTYPE, b"a"), ("parent", tarfile.REGTYPE, b"b")],
    ],
)
def test_restore_rejects_duplicates_and_file_directory_collisions(tmp_path, entries):
    packed = archive(tmp_path / "bad.tar", entries)
    dest = directory(tmp_path / "dest")
    with pytest.raises(WorkspaceArchiveError):
        restore_workspace(packed, dest)
    assert not list(dest.iterdir())
    assert_clean(tmp_path)


def test_restore_accepts_unordered_directories_and_ignores_metadata(tmp_path):
    packed = archive(
        tmp_path / "good.tar",
        [("parent/child", tarfile.REGTYPE, b"data"), ("parent", tarfile.DIRTYPE, b"")],
    )
    dest = directory(tmp_path / "dest")
    assert restore_workspace(packed, dest) == {"files": 1, "bytes": 4}
    info = (dest / "parent/child").stat()
    assert info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700
    assert info.st_mtime != 1
    assert stat.S_IMODE((dest / "parent").stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "damage", ["truncated_payload", "no_footer", "trailing_archive", "compressed", "too_large"]
)
def test_restore_rejects_incomplete_hidden_or_oversized_archive(tmp_path, damage):
    packed = archive(tmp_path / "bad.tar", [("file", tarfile.REGTYPE, b"payload")])
    if damage == "truncated_payload":
        packed.write_bytes(packed.read_bytes()[:515])
    elif damage == "no_footer":
        packed.write_bytes(packed.read_bytes()[:1024])
    elif damage == "trailing_archive":
        packed.write_bytes(packed.read_bytes() * 2)
    elif damage == "compressed":
        import gzip

        packed.write_bytes(gzip.compress(packed.read_bytes()))
    else:
        with packed.open("r+b") as data:
            data.truncate(snapshots.MAX_ARCHIVE_BYTES + 1)
    dest = directory(tmp_path / "dest")
    with pytest.raises(WorkspaceArchiveError):
        restore_workspace(packed, dest)
    assert not list(dest.iterdir())
    assert_clean(tmp_path)


def test_restore_counts_implicit_directories_and_rejects_4097_entries(tmp_path):
    packed = archive(tmp_path / "bad.tar", [(str(i), tarfile.REGTYPE, b"") for i in range(4097)])
    dest = directory(tmp_path / "dest")
    with pytest.raises(WorkspaceArchiveError, match="4096"):
        restore_workspace(packed, dest)
    packed.unlink()
    packed = archive(packed, [(str(i) + "/child", tarfile.REGTYPE, b"") for i in range(2049)])
    with pytest.raises(WorkspaceArchiveError, match="4096"):
        restore_workspace(packed, dest)
    assert not list(dest.iterdir())


@pytest.mark.parametrize(
    "condition", ["populated", "public", "symlink", "archive_symlink", "archive_hardlink"]
)
def test_restore_requires_private_empty_destination_and_no_linked_archive(tmp_path, condition):
    packed = archive(tmp_path / "good.tar", [("file", tarfile.REGTYPE, b"payload")])
    dest = directory(tmp_path / "dest")
    if condition == "populated":
        (dest / "existing").write_bytes(b"keep")
    elif condition == "public":
        dest.chmod(0o755)
    elif condition == "symlink":
        linked = tmp_path / "link"
        linked.symlink_to(dest)
        dest = linked
    elif condition == "archive_symlink":
        linked = tmp_path / "linked.tar"
        linked.symlink_to(packed)
        packed = linked
    else:
        os.link(packed, tmp_path / "alias.tar")
    with pytest.raises(WorkspaceArchiveError):
        restore_workspace(packed, dest)
    assert not (dest / "file").exists()
    if condition == "populated":
        assert (dest / "existing").read_bytes() == b"keep"
    assert_clean(tmp_path)


@pytest.mark.parametrize("failure", ["write", "archive_change", "destination_change"])
def test_restore_failure_cleans_stage_and_preserves_destination(tmp_path, monkeypatch, failure):
    packed = archive(
        tmp_path / "good.tar",
        [("first", tarfile.REGTYPE, b"abc"), ("second", tarfile.REGTYPE, b"def")],
    )
    dest = directory(tmp_path / "dest")
    original = tarfile.TarFile.extractfile
    calls = 0

    def extract(tar, member):
        nonlocal calls
        calls += 1
        if calls == 2:
            if failure == "write":
                raise OSError("synthetic source failure")
            if failure == "archive_change":
                with packed.open("ab") as output:
                    output.write(b"new")
            else:
                (dest / "external").write_bytes(b"keep")
        return original(tar, member)

    monkeypatch.setattr(tarfile.TarFile, "extractfile", extract)
    with pytest.raises(WorkspaceArchiveError):
        restore_workspace(packed, dest)
    assert not (dest / "first").exists() and not (dest / "second").exists()
    if failure == "destination_change":
        assert (dest / "external").read_bytes() == b"keep"
    assert_clean(tmp_path)


def test_pack_source_replaced_by_link_between_stat_and_open(tmp_path, monkeypatch):
    source = directory(tmp_path / "source")
    (source / "data").write_bytes(b"original")
    outside = tmp_path / "outside"
    outside.write_bytes(b"must not read")
    original = os.open
    replaced = False

    def opening(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == "data" and not replaced:
            replaced = True
            (source / "data").unlink()
            (source / "data").symlink_to(outside)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(snapshots.os, "open", opening)
    with pytest.raises(WorkspaceArchiveError):
        pack_workspace(source, tmp_path / "new.tar")
    assert replaced and not (tmp_path / "new.tar").exists()
    assert_clean(tmp_path)


def test_restore_rejects_deep_tree_before_staging(tmp_path):
    packed = archive(tmp_path / "bad.tar", [("/".join(["d"] * 65), tarfile.REGTYPE, b"")])
    dest = directory(tmp_path / "dest")
    with pytest.raises(WorkspaceArchiveError):
        restore_workspace(packed, dest)
    assert not list(dest.iterdir())
    assert_clean(tmp_path)


def test_restore_rejects_oversized_extension_header_without_allocation(tmp_path):
    member = tarfile.TarInfo("extension")
    member.type, member.size = tarfile.GNUTYPE_LONGNAME, 1 << 80
    packed = tmp_path / "bad.tar"
    packed.write_bytes(member.tobuf(format=tarfile.GNU_FORMAT) + bytes(1024))
    dest = directory(tmp_path / "dest")
    with pytest.raises(WorkspaceArchiveError):
        restore_workspace(packed, dest)
    assert not list(dest.iterdir())
    assert_clean(tmp_path)


def test_restore_rejects_filesystem_directory_aliases(tmp_path):
    probe = directory(tmp_path / "CaseProbe")
    if not (tmp_path / "caseprobe").exists():
        pytest.skip("Filesystem preserves case-distinct directory names")
    probe.rmdir()
    packed = archive(
        tmp_path / "bad.tar",
        [("UPPER/one", tarfile.REGTYPE, b"a"), ("upper/two", tarfile.REGTYPE, b"b")],
    )
    dest = directory(tmp_path / "dest")
    with pytest.raises(WorkspaceArchiveError, match="collide"):
        restore_workspace(packed, dest)
    assert not list(dest.iterdir())
    assert_clean(tmp_path)


def test_maximum_tar_size_roundtrip_includes_overhead(tmp_path):
    source = directory(tmp_path / "source")
    size = snapshots.MAX_ARCHIVE_BYTES - 1536  # Header and required two end blocks.
    with (source / "data").open("wb") as payload:
        payload.truncate(size)
    packed = tmp_path / "max.tar"
    assert pack_workspace(source, packed)["bytes"] == snapshots.MAX_ARCHIVE_BYTES
    dest = directory(tmp_path / "dest")
    assert restore_workspace(packed, dest) == {"files": 1, "bytes": size}
    digest = hashlib.sha256()
    expected = hashlib.sha256()
    with (dest / "data").open("rb") as payload:
        while chunk := payload.read(65536):
            digest.update(chunk)
            expected.update(bytes(len(chunk)))
    assert digest.digest() == expected.digest()
    assert_clean(tmp_path)
