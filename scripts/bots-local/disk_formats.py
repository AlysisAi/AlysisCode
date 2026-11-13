"""Small, dependency-free readers and writers for the Windows image recipe.

- ``vhd_footer``: turn a raw disk into a fixed VHD (raw bytes + one 512-byte footer).
- ``fat12_volume``: a tiny FAT12 volume with long file names (the cloud-init "CIDATA" seed).
- ``linux_root``: the first Linux partition of a GPT disk (offset, size, PARTUUID).
- ``Ext4``: read-only ext4 access, enough to copy the kernel and initrd out of /boot.

Formats follow the Microsoft VHD specification, the FAT/VFAT and UEFI GPT specifications and
the Linux ext4 on-disk layout documentation.
"""

from __future__ import annotations

import math
import random
import struct
import time
import uuid

LINUX_PARTITION_TYPES = {
    uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4"),  # Linux filesystem data
    uuid.UUID("4f68bce3-e8cd-4db1-96e7-fbcaf984b709"),  # Linux root (x86-64)
}


def vhd_footer(size: int) -> bytes:
    """A fixed-VHD footer for a disk of ``size`` bytes (CHS geometry and checksum per spec)."""
    if size <= 0 or size % 512:
        raise ValueError("A VHD disk size must be a positive multiple of 512 bytes.")
    total = min(size // 512, 65535 * 16 * 255)
    if total >= 65535 * 16 * 63:
        sectors, heads = 255, 16
        cylinder_heads = total // sectors
    else:
        sectors = 17
        cylinder_heads = total // sectors
        heads = max((cylinder_heads + 1023) // 1024, 4)
        if cylinder_heads >= heads * 1024 or heads > 16:
            sectors, heads = 31, 16
            cylinder_heads = total // sectors
        if cylinder_heads >= heads * 1024:
            sectors, heads = 63, 16
            cylinder_heads = total // sectors
    footer = bytearray(512)
    footer[0:8] = b"conectix"
    struct.pack_into(
        ">IIQI", footer, 8, 2, 0x00010000, 0xFFFFFFFFFFFFFFFF, int(time.time()) - 946684800
    )
    footer[28:32] = b"alys"
    struct.pack_into(">I", footer, 32, 0x00010000)
    footer[36:40] = b"Wi2k"
    struct.pack_into(">QQHBBI", footer, 40, size, size, cylinder_heads // heads, heads, sectors, 2)
    footer[68:84] = uuid.uuid4().bytes
    struct.pack_into(">I", footer, 64, (~sum(footer)) & 0xFFFFFFFF)
    return bytes(footer)


def fat12_volume(files: dict[str, bytes], label: bytes = b"CIDATA     ") -> bytes:
    """A 1.44 MB FAT12 volume holding ``files`` in its root, with long (VFAT) names."""
    if len(label) != 11:
        raise ValueError("A FAT volume label is exactly 11 bytes.")
    bps, reserved, fats, root_entries, total, per_fat = 512, 1, 2, 224, 2880, 9
    root_sector = reserved + fats * per_fat
    data_sector = root_sector + root_entries * 32 // bps
    image = bytearray(total * bps)
    struct.pack_into(
        "<3s8sHBHBHHBHHHIIBBBI11s8s",
        image,
        0,
        b"\xeb\x3c\x90",
        b"MSWIN4.1",
        bps,
        1,
        reserved,
        fats,
        root_entries,
        total,
        0xF0,
        per_fat,
        18,
        2,
        0,
        0,
        0,
        0,
        0x29,
        random.getrandbits(32),
        label,
        b"FAT12   ",
    )
    image[510:512] = b"\x55\xaa"
    table = [0xFF0, 0xFFF]
    entries = [label + bytes([0x08]) + bytes(20)]
    date = ((2026 - 1980) << 9) | (1 << 5) | 1
    for index, (name, data) in enumerate(files.items(), 1):
        short = (name.upper().replace(".", "")[:6] + "~" + str(index)).ljust(11).encode()
        clusters = max(1, math.ceil(len(data) / bps))
        first = len(table)
        if first + clusters > (total - data_sector) + 2:
            raise ValueError("The seed files do not fit a 1.44 MB volume.")
        table += [first + i + 1 for i in range(clusters - 1)] + [0xFFF]
        offset = (data_sector + first - 2) * bps
        image[offset : offset + len(data)] = data
        checksum = 0
        for byte in short:
            checksum = (((checksum & 1) << 7) + (checksum >> 1) + byte) & 0xFF
        chars = [ord(c) for c in name]
        if len(chars) % 13:
            chars.append(0)
            chars += [0xFFFF] * (-len(chars) % 13)
        parts = [chars[i : i + 13] for i in range(0, len(chars), 13)]
        for sequence in range(len(parts), 0, -1):
            entry = bytearray(32)
            entry[0] = sequence | (0x40 if sequence == len(parts) else 0)
            for slot, char in zip(
                (1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 28, 30), parts[sequence - 1], strict=True
            ):
                struct.pack_into("<H", entry, slot, char)
            entry[11], entry[13] = 0x0F, checksum
            entries.append(bytes(entry))
        entry = bytearray(32)
        entry[0:11], entry[11] = short, 0x20
        struct.pack_into("<HHHI", entry, 22, 0, date, first, len(data))  # time, date, cluster, size
        entries.append(bytes(entry))
    if len(entries) > root_entries:
        raise ValueError("Too many seed files.")
    table += [0] * (-len(table) % 2)
    packed = bytearray()
    for low, high in zip(table[0::2], table[1::2], strict=True):
        packed += bytes(
            [low & 0xFF, ((low >> 8) & 0x0F) | ((high & 0x0F) << 4), (high >> 4) & 0xFF]
        )
    for copy in range(fats):
        start = (reserved + copy * per_fat) * bps
        image[start : start + len(packed)] = packed
    root = b"".join(entries)
    image[root_sector * bps : root_sector * bps + len(root)] = root
    return bytes(image)


def linux_root(stream) -> tuple[int, int, str]:
    """(byte offset, byte size, PARTUUID) of the first Linux partition on a GPT disk."""
    stream.seek(512)
    header = stream.read(92)
    if header[:8] != b"EFI PART":
        raise ValueError("The disk has no GPT partition table.")
    entries_lba, count, entry_size = struct.unpack_from("<QII", header, 72)
    stream.seek(entries_lba * 512)
    table = stream.read(count * entry_size)
    for index in range(count):
        entry = table[index * entry_size : (index + 1) * entry_size]
        if uuid.UUID(bytes_le=entry[0:16]) in LINUX_PARTITION_TYPES:
            first, last = struct.unpack_from("<QQ", entry, 32)
            return first * 512, (last - first + 1) * 512, str(uuid.UUID(bytes_le=entry[16:32]))
    raise ValueError("The disk has no Linux partition.")


class Ext4:
    """Read-only access to an ext4 filesystem that starts at ``offset`` in ``stream``."""

    EXTENTS = 0x80000

    def __init__(self, stream, offset: int):
        self.stream, self.offset = stream, offset
        superblock = self._read(1024, 1024)
        if struct.unpack_from("<H", superblock, 56)[0] != 0xEF53:
            raise ValueError("Not an ext4 filesystem.")
        first_data_block, log_block = struct.unpack_from("<II", superblock, 20)
        self.block = 1024 << log_block
        self.inodes_per_group = struct.unpack_from("<I", superblock, 40)[0]
        self.inode_size = struct.unpack_from("<H", superblock, 88)[0]
        wide = bool(struct.unpack_from("<I", superblock, 96)[0] & 0x80)
        self.descriptor = max(struct.unpack_from("<H", superblock, 254)[0], 64) if wide else 32
        self.descriptors = (first_data_block + 1) * self.block

    def _read(self, position: int, count: int) -> bytes:
        self.stream.seek(self.offset + position)
        data = self.stream.read(count)
        if len(data) != count:
            raise ValueError("The filesystem is truncated.")
        return data

    def _inode(self, number: int):
        group, index = divmod(number - 1, self.inodes_per_group)
        descriptor = self._read(self.descriptors + group * self.descriptor, self.descriptor)
        table = struct.unpack_from("<I", descriptor, 8)[0]
        if self.descriptor >= 64:
            table |= struct.unpack_from("<I", descriptor, 0x28)[0] << 32
        raw = self._read(table * self.block + index * self.inode_size, self.inode_size)
        mode, size_low = struct.unpack_from("<HxxI", raw, 0)
        flags = struct.unpack_from("<I", raw, 32)[0]
        size = size_low | (struct.unpack_from("<I", raw, 108)[0] << 32)
        return mode, size, flags, raw[40:100]

    def _extents(self, node: bytes):
        magic, count, _maximum, depth = struct.unpack_from("<HHHH", node, 0)
        if magic != 0xF30A:
            raise ValueError("Damaged extent tree.")
        for index in range(count):
            at = 12 + 12 * index
            if depth == 0:
                logical, length, high, low = struct.unpack_from("<IHHI", node, at)
                unwritten = length > 32768
                yield (
                    logical,
                    length - 32768 if unwritten else length,
                    (high << 32) | low,
                    unwritten,
                )
            else:
                _logical, low, high = struct.unpack_from("<IIH", node, at)
                yield from self._extents(self._read(((high << 32) | low) * self.block, self.block))

    def read(self, number: int) -> bytes:
        _mode, size, flags, block = self._inode(number)
        if not flags & self.EXTENTS:
            raise ValueError("Only extent-mapped files are supported.")
        data = bytearray(size)
        for logical, length, physical, unwritten in self._extents(block):
            start = logical * self.block
            if start >= size or unwritten:
                continue
            count = min(length * self.block, size - start)
            data[start : start + count] = self._read(physical * self.block, count)
        return bytes(data)

    def listdir(self, number: int) -> dict[str, tuple[int, int]]:
        """name -> (inode, file type). Hash-indexed directories read correctly as linear ones."""
        data, entries, position = self.read(number), {}, 0
        while position + 8 <= len(data):
            inode, length, name_length, kind = struct.unpack_from("<IHBB", data, position)
            if length < 8:
                break
            if inode:
                name = data[position + 8 : position + 8 + name_length].decode("utf-8", "replace")
                entries[name] = (inode, kind)
            position += length
        return entries

    def lookup(self, path: str) -> int:
        number = 2  # the root directory
        for part in path.strip("/").split("/"):
            number = self.listdir(number)[part][0]
        return number


def newest_boot_files(filesystem: Ext4) -> tuple[str, bytes, bytes]:
    """(version, vmlinuz, initrd) for the newest kernel in /boot that has both files."""
    boot = filesystem.listdir(filesystem.lookup("/boot"))
    regular = {name: inode for name, (inode, kind) in boot.items() if kind == 1}
    versions = [
        name.removeprefix("vmlinuz-")
        for name in regular
        if name.startswith("vmlinuz-") and "initrd.img-" + name.removeprefix("vmlinuz-") in regular
    ]
    if not versions:
        raise ValueError("No kernel with an initrd in /boot.")

    def order(version):
        return [
            int(part) if part.isdigit() else part
            for part in version.replace("+", ".").replace("-", ".").split(".")
        ]

    version = max(
        versions, key=lambda value: [(0, p) if isinstance(p, int) else (1, p) for p in order(value)]
    )
    return (
        version,
        filesystem.read(regular["vmlinuz-" + version]),
        filesystem.read(regular["initrd.img-" + version]),
    )
