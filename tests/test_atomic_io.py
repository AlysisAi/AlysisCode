from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import alysis_code.atomic_io as atomic_io_mod
from alysis_code.atomic_io import atomic_write_json, atomic_write_text


def test_atomic_write_text_replaces_existing_file_without_temp_leaks(tmp_path: Path) -> None:
    target = tmp_path / "artifact.txt"
    target.write_text("before\n", encoding="utf-8")

    atomic_write_text(target, "after\n")

    assert target.read_text(encoding="utf-8") == "after\n"
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not portable to Windows")
def test_atomic_write_text_preserves_existing_file_mode(tmp_path: Path) -> None:
    target = tmp_path / "artifact.txt"
    target.write_text("before\n", encoding="utf-8")
    target.chmod(0o640)

    atomic_write_text(target, "after\n")

    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_atomic_write_json_overwrites_existing_file_atomically(tmp_path: Path) -> None:
    target = tmp_path / "artifact.json"
    target.write_text('{"before": true}\n', encoding="utf-8")

    atomic_write_json(target, {"after": True, "value": 3})

    assert target.read_text(encoding="utf-8") == '{\n  "after": true,\n  "value": 3\n}\n'
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


def test_atomic_write_helpers_create_parent_dirs(tmp_path: Path) -> None:
    text_target = tmp_path / "nested" / "artifacts" / "artifact.txt"
    json_target = tmp_path / "nested" / "artifacts" / "artifact.json"

    atomic_write_text(text_target, "created\n")
    atomic_write_json(json_target, {"ok": True})

    assert text_target.read_text(encoding="utf-8") == "created\n"
    assert json_target.read_text(encoding="utf-8") == '{\n  "ok": true\n}\n'
    assert list(text_target.parent.glob(f".{text_target.name}.*.tmp")) == []
    assert list(json_target.parent.glob(f".{json_target.name}.*.tmp")) == []


def test_atomic_write_text_cleans_temp_file_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "artifact.txt"

    def fail_replace(_src: Path, _dst: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("alysis_code.atomic_io.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        atomic_write_text(target, "after\n")

    assert target.exists() is False
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


def test_atomic_write_text_preserves_newlines_exactly(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "artifact.txt"
    original_fdopen = atomic_io_mod.os.fdopen
    captured: dict[str, str | None] = {"newline": None}

    def recording_fdopen(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["newline"] = kwargs.get("newline")
        return original_fdopen(*args, **kwargs)

    monkeypatch.setattr("alysis_code.atomic_io.os.fdopen", recording_fdopen)

    atomic_write_text(target, "line1\r\nline2\n")

    assert captured["newline"] == ""
    assert target.read_bytes() == b"line1\r\nline2\n"


def test_atomic_write_text_raises_on_real_directory_fsync_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("directory fsync is not attempted on Windows")

    target = tmp_path / "artifact.txt"
    real_open = atomic_io_mod.os.open
    real_fsync = atomic_io_mod.os.fsync
    sentinel_dir_fd = 999_999

    def open_wrapper(path, flags, mode=0o777):  # type: ignore[no-untyped-def]
        if Path(path) == tmp_path and flags == atomic_io_mod.os.O_RDONLY:
            return sentinel_dir_fd
        return real_open(path, flags, mode)

    def fsync_wrapper(fd: int) -> None:
        if fd == sentinel_dir_fd:
            raise OSError(5, "directory fsync failed")
        real_fsync(fd)

    monkeypatch.setattr("alysis_code.atomic_io.os.open", open_wrapper)
    monkeypatch.setattr("alysis_code.atomic_io.os.fsync", fsync_wrapper)
    monkeypatch.setattr("alysis_code.atomic_io.os.close", lambda _fd: None)

    with pytest.raises(OSError, match="directory fsync failed"):
        atomic_write_text(target, "after\n")

    assert target.read_text(encoding="utf-8") == "after\n"
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []
