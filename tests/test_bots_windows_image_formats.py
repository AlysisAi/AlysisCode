"""The Windows image recipe's dependency-free disk helpers (scripts/bots-local/disk_formats.py).

The ext4 reader was qualified against the pinned Debian image (byte-identical kernel and initrd
to the files the running VM exported); these tests pin the formats it writes and the selection.
"""

from __future__ import annotations

import io
import struct
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "bots-local"))

import disk_formats  # noqa: E402


def test_vhd_footer_is_a_valid_fixed_disk_footer():
    footer = disk_formats.vhd_footer(8 * 1024**3)
    assert len(footer) == 512 and footer[:8] == b"conectix"
    assert struct.unpack_from(">QQ", footer, 40) == (8 * 1024**3, 8 * 1024**3)
    assert struct.unpack_from(">I", footer, 60)[0] == 2  # fixed
    zeroed = footer[:64] + b"\0\0\0\0" + footer[68:]
    assert struct.unpack_from(">I", footer, 64)[0] == (~sum(zeroed)) & 0xFFFFFFFF
    with pytest.raises(ValueError):
        disk_formats.vhd_footer(1000)


def read_fat12(image: bytes) -> tuple[bytes, dict[str, bytes]]:
    label = image[43:54]
    root = image[19 * 512 : 33 * 512]
    fat = image[512 : 10 * 512]

    def next_cluster(cluster):
        offset = cluster * 3 // 2
        value = fat[offset] | fat[offset + 1] << 8
        return value >> 4 if cluster % 2 else value & 0xFFF

    files, long_name = {}, []
    for at in range(0, len(root), 32):
        entry = root[at : at + 32]
        if entry[0] == 0:
            break
        if entry[11] == 0x0F:
            chars = (
                struct.unpack_from("<5H", entry, 1)
                + struct.unpack_from("<6H", entry, 14)
                + struct.unpack_from("<2H", entry, 28)
            )
            long_name = [c for c in chars if c not in (0, 0xFFFF)] + long_name
            continue
        if entry[11] == 0x08:
            continue
        name = "".join(map(chr, long_name))
        long_name = []
        cluster, size = struct.unpack_from("<HI", entry, 26)
        data = bytearray()
        while cluster < 0xFF8:
            data += image[(33 + cluster - 2) * 512 : (34 + cluster - 2) * 512]
            cluster = next_cluster(cluster)
        files[name] = bytes(data[:size])
    return label, files


def test_fat12_seed_round_trips_long_names_and_contents():
    user_data = b"#cloud-config\n" + b"x" * 3000
    image = disk_formats.fat12_volume(
        {"user-data": user_data, "meta-data": b"instance-id: a\n", "network-config": b"{}"}
    )
    assert len(image) == 1474560 and image[510:512] == b"\x55\xaa"
    label, files = read_fat12(image)
    assert label == b"CIDATA     "
    assert files == {
        "user-data": user_data,
        "meta-data": b"instance-id: a\n",
        "network-config": b"{}",
    }


def test_fat12_refuses_oversized_seeds():
    with pytest.raises(ValueError):
        disk_formats.fat12_volume({"user-data": b"x" * 2_000_000})


def test_linux_root_reads_the_gpt():
    disk = bytearray(64 * 512)
    struct.pack_into("<8s", disk, 512, b"EFI PART")
    struct.pack_into("<QII", disk, 512 + 72, 2, 4, 128)
    esp = uuid.UUID("c12a7328-f81f-11d2-ba4b-00a0c93ec93b")
    linux = uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4")
    unique = uuid.uuid4()
    disk[1024 : 1024 + 16] = esp.bytes_le
    struct.pack_into("<QQ", disk, 1024 + 32, 34, 40)
    disk[1152 : 1152 + 16] = linux.bytes_le
    disk[1152 + 16 : 1152 + 32] = unique.bytes_le
    struct.pack_into("<QQ", disk, 1152 + 32, 41, 63)
    assert disk_formats.linux_root(io.BytesIO(disk)) == (41 * 512, 23 * 512, str(unique))
    with pytest.raises(ValueError):
        disk_formats.linux_root(io.BytesIO(bytes(4096)))


class FakeFilesystem:
    def __init__(self, names):
        self.names = names

    def lookup(self, path):
        assert path == "/boot"
        return 2

    def listdir(self, number):
        return {name: (index + 10, kind) for index, (name, kind) in enumerate(self.names)}

    def read(self, number):
        return f"inode-{number}".encode()


def test_newest_boot_files_needs_a_matching_initrd():
    names = [
        ("vmlinuz-6.12.9+deb13-amd64", 1),
        ("initrd.img-6.12.9+deb13-amd64", 1),
        ("vmlinuz-6.12.115+deb13.1-amd64", 1),
        ("initrd.img-6.12.115+deb13.1-amd64", 1),
        ("vmlinuz-6.13.0-amd64", 1),  # no initrd: never chosen
        ("vmlinuz", 7),  # a symbolic link: ignored
    ]
    version, kernel, initrd = disk_formats.newest_boot_files(FakeFilesystem(names))
    assert version == "6.12.115+deb13.1-amd64"
    assert kernel == b"inode-12" and initrd == b"inode-13"
    with pytest.raises(ValueError):
        disk_formats.newest_boot_files(FakeFilesystem([("vmlinuz-1", 1)]))
