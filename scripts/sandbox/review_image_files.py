"""Inventory a Docker export without extracting or executing its contents."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from elftools.elf.elffile import ELFFile

LEGACY_ACL = {"acl_get_file", "acl_set_file", "acl_extended_file", "acl_delete_def_file"}
KERNEL_FILE = re.compile(
    r"^(?:(?:usr/)?lib/modules/.*\.ko(?:\.(?:gz|xz|zst))?"
    r"|boot/(?:vmlinu[xz]|bzImage|System\.map)(?:[-.].*)?)$"
)


def safe_name(name: str) -> str:
    name = name.removeprefix("./")
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\x00" in name:
        raise ValueError(f"Unsafe archive path: {name!r}")
    return str(path)


def inspect_elf(stream: BinaryIO) -> dict:
    elf = ELFFile(stream)
    # Separate debug files can preserve these names as empty SHT_NOBITS sections.
    # Classify by ELF section type, and retain the named section metadata below.
    sections = list(elf.iter_sections())
    dynamic = [section for section in sections if section["sh_type"] == "SHT_DYNAMIC"]
    symbols = [section for section in sections if section["sh_type"] == "SHT_DYNSYM"]
    needed = sorted(
        {
            tag.needed
            for section in dynamic
            for tag in section.iter_tags()
            if tag.entry.d_tag == "DT_NEEDED"
        }
    )
    imports = sorted(
        {
            symbol.name
            for section in symbols
            for symbol in section.iter_symbols()
            if symbol.entry.st_shndx == "SHN_UNDEF" and symbol.name.startswith("acl_")
        }
    )
    return {
        "machine": elf.header.e_machine,
        "type": elf.header.e_type,
        "has_dynamic_symbols": bool(symbols),
        "named_dynamic_sections": [
            {"name": section.name, "type": section["sh_type"]}
            for section in sections
            if section.name in {".dynamic", ".dynsym"}
        ],
        "needed": needed,
        "acl_imports": imports,
        "legacy_acl_imports": sorted(LEGACY_ACL.intersection(imports)),
    }


def inspect_archive(archive_path: Path) -> dict:
    rows = []
    by_name = {}
    with tarfile.open(archive_path, "r|*") as archive:
        for member in archive:
            name = safe_name(member.name)
            if member.isdir():
                continue
            if name in by_name:
                raise ValueError(f"Duplicate archive member: {name}")
            row = {
                "path": name,
                "mode": oct(member.mode),
                "uid": member.uid,
                "gid": member.gid,
                "setuid_or_setgid": bool(member.mode & 0o6000),
            }
            if member.isfile():
                row.update(type="file", bytes=member.size)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f"Missing archive content: {name}")
                with source, tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as data:
                    digest = hashlib.sha256()
                    prefix = source.read(4)
                    digest.update(prefix)
                    is_elf = prefix == b"\x7fELF"
                    if is_elf:
                        data.write(prefix)
                    size = len(prefix)
                    while block := source.read(1024 * 1024):
                        size += len(block)
                        digest.update(block)
                        if is_elf:
                            data.write(block)
                    if size != member.size:
                        raise ValueError(f"Truncated archive member: {name}")
                    row["sha256"] = digest.hexdigest()
                    if is_elf:
                        data.seek(0)
                        try:
                            row["elf"] = inspect_elf(data)
                        except Exception as exc:
                            raise ValueError(f"Cannot inspect ELF member {name}: {exc}") from exc
            elif member.issym():
                row.update(type="symlink", target=member.linkname)
            elif member.islnk():
                row.update(type="hardlink", target=safe_name(member.linkname))
            else:
                row.update(type="special", tar_type=member.type.decode("ascii"))
            rows.append(row)
            by_name[name] = row

    for row in rows:
        if row["type"] != "hardlink":
            continue
        target = by_name.get(row["target"])
        seen = {row["path"]}
        while target is not None and target["type"] == "hardlink":
            if target["path"] in seen:
                raise ValueError(f"Cyclic hardlink: {row['path']}")
            seen.add(target["path"])
            target = by_name.get(target["target"])
        if target is None or target["type"] != "file":
            raise ValueError(f"Hardlink target is not a regular archive file: {row['path']}")
        row["sha256"] = target["sha256"]
        if "elf" in target:
            row["elf"] = target["elf"]

    rows.sort(key=lambda row: row["path"])
    return {
        "schema_version": 1,
        "scope": "Complete Docker export; files are inspected without extraction or execution.",
        "file_count": len(rows),
        "regular_files": sum(row["type"] == "file" for row in rows),
        "elf_files_and_hardlinks": sum("elf" in row for row in rows),
        "kernel_payload_paths": [row["path"] for row in rows if KERNEL_FILE.match(row["path"])],
        "legacy_acl_callers": [
            row["path"] for row in rows if row.get("elf", {}).get("legacy_acl_imports")
        ],
        "expat_consumers": [
            row["path"]
            for row in rows
            if any("libexpat" in item for item in row.get("elf", {}).get("needed", []))
        ],
        "setuid_or_setgid_paths": [row["path"] for row in rows if row["setuid_or_setgid"]],
        "files": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", args.image_id) is None:
        parser.error("--image-id must be an exact Docker image ID")
    result = inspect_archive(args.archive)
    result["image_id"] = args.image_id
    with args.archive.open("rb") as source:
        result["export_sha256"] = hashlib.file_digest(source, "sha256").hexdigest()
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "files"}))


if __name__ == "__main__":
    main()
