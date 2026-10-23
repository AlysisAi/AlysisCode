from __future__ import annotations

import hashlib
import importlib.util
import io
import struct
import tarfile
from pathlib import Path

import pytest

pytest.importorskip("elftools", reason="Optional pinned sandbox filesystem reviewer dependency")

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/sandbox/review_image_files.py"
SPEC = importlib.util.spec_from_file_location("image_inventory", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
REVIEWER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REVIEWER)


def write_archive(path, members):
    with tarfile.open(path, "w") as archive:
        for name, content, link_type, target in members:
            member = tarfile.TarInfo(name)
            member.type = link_type
            member.linkname = target
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content) if member.isfile() else None)
    return path


def test_hashes_files_resolves_forward_hardlinks_and_never_follows_symlinks(tmp_path):
    path = write_archive(
        tmp_path / "export.tar",
        [
            ("usr/bin/alias", b"", tarfile.LNKTYPE, "usr/bin/tool"),
            ("usr/bin/outside", b"", tarfile.SYMTYPE, "/outside/private-key"),
            ("usr/bin/tool", b"retained payload", tarfile.REGTYPE, ""),
        ],
    )
    report = REVIEWER.inspect_archive(path)
    rows = {row["path"]: row for row in report["files"]}
    expected = hashlib.sha256(b"retained payload").hexdigest()
    assert rows["usr/bin/alias"]["sha256"] == rows["usr/bin/tool"]["sha256"] == expected
    assert rows["usr/bin/outside"]["target"] == "/outside/private-key"
    assert "sha256" not in rows["usr/bin/outside"]
    assert report["regular_files"] == 1


@pytest.mark.parametrize("name", ["../escape", "/absolute", "usr/../../escape"])
def test_rejects_unsafe_member_paths(tmp_path, name):
    path = write_archive(tmp_path / "export.tar", [(name, b"x", tarfile.REGTYPE, "")])
    with pytest.raises(ValueError, match="Unsafe archive"):
        REVIEWER.inspect_archive(path)


def test_rejects_duplicate_members_and_cyclic_links(tmp_path):
    path = write_archive(tmp_path / "duplicate.tar", [("same", b"x", tarfile.REGTYPE, "")] * 2)
    with pytest.raises(ValueError, match="Duplicate"):
        REVIEWER.inspect_archive(path)
    path = write_archive(
        tmp_path / "cycle.tar",
        [("a", b"", tarfile.LNKTYPE, "b"), ("b", b"", tarfile.LNKTYPE, "a")],
    )
    with pytest.raises(ValueError, match="Cyclic hardlink"):
        REVIEWER.inspect_archive(path)


def test_recognizes_elf_architecture_and_kernel_module(tmp_path):
    ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + bytes(8)
    header = ident + struct.pack("<HHIQQQIHHHHHH", 2, 62, 1, 0, 0, 0, 0, 64, 0, 0, 0, 0, 0)
    path = write_archive(
        tmp_path / "elf.tar",
        [("lib/modules/7.2/test.ko", header, tarfile.REGTYPE, "")],
    )
    report = REVIEWER.inspect_archive(path)
    assert report["kernel_payload_paths"] == ["lib/modules/7.2/test.ko"]
    assert report["files"][0]["elf"]["machine"] == "EM_X86_64"
    assert report["elf_files_and_hardlinks"] == 1


def test_debug_only_dynamic_section_names_are_not_symbol_tables():
    names = b"\x00.shstrtab\x00.dynsym\x00.dynamic\x00"
    ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + bytes(8)
    header = ident + struct.pack("<HHIQQQIHHHHHH", 2, 62, 1, 0, 0, 64, 0, 64, 0, 0, 64, 4, 1)
    sections = bytes(64)
    sections += struct.pack("<IIQQQQIIQQ", 1, 3, 0, 0, 320, len(names), 0, 0, 1, 0)
    sections += struct.pack("<IIQQQQIIQQ", 11, 8, 0, 0, 0, 24, 0, 0, 8, 24)
    sections += struct.pack("<IIQQQQIIQQ", 19, 8, 0, 0, 0, 16, 0, 0, 8, 16)
    report = REVIEWER.inspect_elf(io.BytesIO(header + sections + names))
    assert report["has_dynamic_symbols"] is False
    assert report["acl_imports"] == report["needed"] == []
    assert report["named_dynamic_sections"] == [
        {"name": ".dynsym", "type": "SHT_NOBITS"},
        {"name": ".dynamic", "type": "SHT_NOBITS"},
    ]
