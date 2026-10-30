from __future__ import annotations

import hashlib
import io
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..host_file_handles import open_host_file_read
from ..project_folders import containing_root
from ..tools.fs import _DEFAULT_FS_READ_MAX_BYTES

LineRange = tuple[int, int]


@dataclass
class _FileReads:
    content_sha256: str
    ranges: list[LineRange]
    numbered_ranges: list[LineRange] = field(default_factory=list)
    deliveries: list[dict[str, Any]] = field(default_factory=list)


class SessionReadLedger:
    """Suppress unchanged ranges already returned in the requested presentation."""

    def __init__(self, *, root: Path, enabled: bool = True) -> None:
        self.root = root.resolve()
        self.enabled = bool(enabled)
        self._files: dict[str, _FileReads] = {}
        self._pending: dict[str, tuple[str, str, str, list[LineRange], bool]] = {}
        self._lock = threading.Lock()

    def content_hash(self, path: str) -> str | None:
        # max_bytes bounds returned content. Optional small-file dedup may read
        # up to the default read budget per hash, even for a smaller window.
        max_bytes = _DEFAULT_FS_READ_MAX_BYTES
        if not self.enabled:
            return None
        path_obj = (self.root / path).resolve()
        try:
            containing_root(self.root, path_obj)
        except ValueError:
            return None
        try:
            # Deduplication is optional. Do not scan an entire large file just
            # to support a bounded read, or trust stat metadata as content proof.
            if path_obj.stat().st_size > max_bytes:
                return None
            with open_host_file_read(path_obj) as handle:
                content = handle.read(max_bytes + 1)
            if len(content) > max_bytes:
                return None
        except (FileNotFoundError, IsADirectoryError, OSError):
            return None
        return hashlib.sha256(content).hexdigest()

    def invalidate(self, *paths: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            for path in paths:
                self._files.pop(str(path), None)
                self._pending = {
                    key: entry for key, entry in self._pending.items() if entry[0] != str(path)
                }

    def reset(self) -> int:
        """Forget every delivered range after a model-history boundary."""

        if not self.enabled:
            return 0
        with self._lock:
            cleared = len(self._files)
            self._files.clear()
            self._pending.clear()
        return cleared

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return an immutable-by-convention copy for session continuation."""
        if not self.enabled:
            return {}
        with self._lock:
            return {
                path: {
                    "content_sha256": entry.content_sha256,
                    "ranges": list(entry.ranges),
                    "numbered_ranges": list(entry.numbered_ranges),
                    "deliveries": [dict(delivery) for delivery in entry.deliveries],
                }
                for path, entry in self._files.items()
            }

    def seed_from_snapshot(
        self,
        snapshot: dict[str, dict[str, Any]],
        *,
        retained_messages: list[dict[str, Any]] | None = None,
    ) -> int:
        """Import still-valid ranges from an earlier incarnation of this session."""
        if not self.enabled:
            return 0
        imported: dict[str, _FileReads] = {}
        retained_hashes = (
            {
                hashlib.sha256(str(message.get("content") or "").encode("utf-8")).hexdigest()
                for message in retained_messages
                if message.get("role") == "tool"
            }
            if retained_messages is not None
            else None
        )
        for path, raw_entry in snapshot.items():
            if not isinstance(path, str) or not isinstance(raw_entry, dict):
                continue
            expected_hash = str(raw_entry.get("content_sha256") or "")
            if not expected_hash or self.content_hash(path) != expected_hash:
                continue
            raw_ranges = raw_entry.get("ranges")
            deliveries = [
                delivery
                for delivery in raw_entry.get("deliveries", [])
                if isinstance(delivery, dict)
            ]
            if retained_hashes is not None:
                deliveries = [
                    delivery
                    for delivery in deliveries
                    if delivery.get("message_sha256") in retained_hashes
                ]
                raw_ranges = [
                    item
                    for delivery in deliveries
                    if not delivery.get("include_line_numbers", False)
                    for item in delivery.get("ranges", [])
                ]
            if not isinstance(raw_ranges, list):
                continue
            ranges = _snapshot_ranges(raw_ranges)
            # Older snapshots did not distinguish presentation. Preserve their
            # content coverage without claiming numbered text was delivered.
            numbered_ranges = _snapshot_ranges(
                [
                    item
                    for delivery in deliveries
                    if delivery.get("include_line_numbers")
                    for item in delivery.get("ranges", [])
                ]
                if retained_hashes is not None
                else raw_entry.get("numbered_ranges")
            )
            if ranges or numbered_ranges:
                imported[path] = _FileReads(
                    content_sha256=expected_hash,
                    ranges=ranges,
                    numbered_ranges=numbered_ranges,
                    deliveries=deliveries,
                )
        with self._lock:
            self._files.update(imported)
        return len(imported)

    def filter_result(
        self,
        *,
        path: str,
        result: dict[str, Any],
        content_hash_before: str | None,
        force: bool,
        include_line_numbers: bool = False,
    ) -> dict[str, Any]:
        if not self.enabled:
            return result
        if content_hash_before is None:
            self.invalidate(path)
            return result
        content_hash_after = self.content_hash(path)
        if content_hash_after is None or content_hash_before != content_hash_after:
            self.invalidate(path)
            return result

        returned_range = _returned_line_range(result)
        if returned_range is None:
            return result

        with self._lock:
            entry = self._files.get(path)
            previous_ranges = (
                list(entry.numbered_ranges if include_line_numbers else entry.ranges)
                if entry is not None and entry.content_sha256 == content_hash_after
                else []
            )
            if force:
                return self._stage_result(
                    path,
                    content_hash_after,
                    {**result, "read_ledger_forced": True},
                    [returned_range],
                    include_line_numbers=include_line_numbers,
                )

            unread_ranges = _subtract_ranges(returned_range, previous_ranges)
            if unread_ranges == [returned_range]:
                return self._stage_result(
                    path,
                    content_hash_after,
                    result,
                    [returned_range],
                    include_line_numbers=include_line_numbers,
                )

            skipped_ranges = _subtract_ranges(returned_range, unread_ranges)
            notice = _notice(
                path=path,
                skipped_ranges=skipped_ranges,
                previous_ranges=previous_ranges,
            )

        if not unread_ranges:
            return {
                **result,
                "content": notice,
                "read_ledger_skipped": True,
                "read_ledger_notice": notice,
                "returned_ranges": [],
                "skipped_ranges": _range_payloads(skipped_ranges),
            }

        unread_content = _slice_content(
            content=str(result.get("content") or ""),
            returned_start=returned_range[0],
            unread_ranges=unread_ranges,
        )
        separator = "" if not unread_content or unread_content.endswith("\n") else "\n"
        filtered = {
            **result,
            "content": f"{unread_content}{separator}{notice}",
            "read_ledger_partial": True,
            "read_ledger_notice": notice,
            "returned_ranges": _range_payloads(unread_ranges),
            "skipped_ranges": _range_payloads(skipped_ranges),
        }
        with self._lock:
            return self._stage_result(
                path,
                content_hash_after,
                filtered,
                unread_ranges,
                include_line_numbers=include_line_numbers,
            )

    def _stage_result(
        self,
        path: str,
        content_hash: str,
        result: dict[str, Any],
        ranges: list[LineRange],
        *,
        include_line_numbers: bool = False,
    ) -> dict[str, Any]:
        """Prepare a receipt; fetching content is not evidence of model delivery."""
        identity = json.dumps(
            [path, content_hash, ranges, result.get("content"), include_line_numbers]
        )
        receipt_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        # A cancelled tool batch must not retain source text indefinitely.
        while len(self._pending) >= 256:
            self._pending.pop(next(iter(self._pending)))
        self._pending[receipt_id] = (
            path,
            content_hash,
            str(result.get("content") or ""),
            ranges,
            include_line_numbers,
        )
        return {**result, "read_receipt_id": receipt_id}

    def record_delivery(
        self, *, result: dict[str, Any], content_for_message: str
    ) -> dict[str, Any] | None:
        """Commit only exact, complete source lines present in the final model message.

        Call after transcript shaping and append. Redaction, clipping, failed appends
        and offloaded storage cannot manufacture delivered ranges.
        """
        if not self.enabled:
            return None
        with self._lock:
            pending = self._pending.pop(str(result.get("read_receipt_id") or ""), None)
        if pending is None:
            return None
        path, expected_hash, original_content, ranges, include_line_numbers = pending
        if self.content_hash(path) != expected_hash:
            self.invalidate(path)
            return None
        try:
            message = json.loads(content_for_message)
        except (ValueError, TypeError):
            return None
        if not isinstance(message, dict):
            return None
        if message.get("preview_format") == "file_content":
            visible = message.get("preview")
        else:
            visible = message.get("content")
        if not isinstance(visible, str):
            return None
        original_lines = list(io.StringIO(original_content, newline=""))
        visible_lines = list(io.StringIO(visible, newline=""))
        line_numbers = [number for start, end in ranges for number in range(start, end + 1)]
        delivered = [
            (number, number)
            for number, original, shown in zip(
                line_numbers, original_lines, visible_lines, strict=False
            )
            if original == shown
        ]
        delivered = _merge_ranges(delivered)
        with self._lock:
            entry = self._files.get(path)
            if delivered:
                if entry is None or entry.content_sha256 != expected_hash:
                    entry = _FileReads(content_sha256=expected_hash, ranges=[])
                    self._files[path] = entry
                if include_line_numbers:
                    entry.numbered_ranges = _merge_ranges([*entry.numbered_ranges, *delivered])
                else:
                    entry.ranges = _merge_ranges([*entry.ranges, *delivered])
                message_hash = hashlib.sha256(content_for_message.encode("utf-8")).hexdigest()
                delivery = {
                    "message_sha256": message_hash,
                    "ranges": delivered,
                    "include_line_numbers": include_line_numbers,
                }
                if delivery not in entry.deliveries:
                    entry.deliveries.append(delivery)
        return {
            "path": path,
            "content_sha256": expected_hash,
            "delivered_ranges": _range_payloads(delivered),
            "visible_chars": len(visible),
            "visible_utf8_bytes": len(visible.encode("utf-8")),
            "retrievable_handle": message.get("artifact_handle"),
            "retrievable_locator": message.get("artifact_locator"),
            "retention": "active_context",
        }


def _snapshot_ranges(raw_ranges: Any) -> list[LineRange]:
    if not isinstance(raw_ranges, list):
        return []
    return _merge_ranges(
        [
            (item[0], item[1])
            for item in raw_ranges
            if isinstance(item, (list, tuple))
            and len(item) == 2
            and isinstance(item[0], int)
            and isinstance(item[1], int)
            and item[0] > 0
            and item[1] >= item[0]
        ]
    )


def _returned_line_range(result: dict[str, Any]) -> LineRange | None:
    returned = result.get("returned_range")
    if isinstance(returned, dict):
        start = returned.get("start_line")
        end = returned.get("end_line")
    elif isinstance(result.get("start_line"), int) and isinstance(result.get("end_line"), int):
        start = result["start_line"]
        end = result["end_line"]
    else:
        content = str(result.get("content") or "")
        start = 1
        end = sum(1 for _line in io.StringIO(content, newline=""))
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    if result.get("line_clipped") is True:
        end -= 1
    if end < start:
        return None
    return start, end


def _merge_ranges(ranges: list[LineRange]) -> list[LineRange]:
    merged: list[LineRange] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _subtract_ranges(target: LineRange, covered: list[LineRange]) -> list[LineRange]:
    remaining = [target]
    for covered_start, covered_end in _merge_ranges(covered):
        next_remaining: list[LineRange] = []
        for start, end in remaining:
            if covered_end < start or covered_start > end:
                next_remaining.append((start, end))
                continue
            if start < covered_start:
                next_remaining.append((start, covered_start - 1))
            if covered_end < end:
                next_remaining.append((covered_end + 1, end))
        remaining = next_remaining
    return remaining


def _range_text(ranges: list[LineRange]) -> str:
    return ", ".join(str(start) if start == end else f"{start}-{end}" for start, end in ranges)


def _range_payloads(ranges: list[LineRange]) -> list[dict[str, int]]:
    return [{"start_line": start, "end_line": end} for start, end in ranges]


def _notice(
    *,
    path: str,
    skipped_ranges: list[LineRange],
    previous_ranges: list[LineRange],
) -> str:
    skipped = _range_text(skipped_ranges)
    previous = _range_text(_merge_ranges(previous_ranges))
    return (
        f"lines {skipped} of {path} were already returned in this session (unchanged); "
        f"re-read skipped; request outside {previous} or set force=true"
    )


def _slice_content(
    *,
    content: str,
    returned_start: int,
    unread_ranges: list[LineRange],
) -> str:
    # Match the reader's physical CR/LF/CRLF boundaries. Unicode separators
    # inside a source line must not shift the range offsets or lose later lines.
    lines = list(io.StringIO(content, newline=""))
    pieces: list[str] = []
    for start, end in unread_ranges:
        first = max(0, start - returned_start)
        last = max(first, end - returned_start + 1)
        pieces.extend(lines[first:last])
    return "".join(pieces)
