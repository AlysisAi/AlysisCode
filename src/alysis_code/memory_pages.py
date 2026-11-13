"""Size-bounded memory pages and version-bound continuation, without text ranking."""

from __future__ import annotations

import hashlib
import json

MAX_PAGE_CHARS = 64_000


def serialized(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def page_version(snapshot, query=""):
    project = snapshot.get("project")
    identity = [
        snapshot["owner_id"],
        snapshot["device_id"],
        snapshot["generation"],
        snapshot["general_accessible"],
        query,
        [project[k] for k in ("id", "revision", "policy_revision", "include_general", "enabled")]
        if project
        else None,
    ]
    return hashlib.sha256(serialized(identity).encode()).hexdigest()


def continuation(version, offset, content_offset=0):
    payload = f"{version}:{offset}:{content_offset}"
    # Detect altered positions; this is a protocol checksum, not authorization.
    return f"{payload}:{hashlib.sha256(payload.encode()).hexdigest()[:16]}"


def parse_continuation(value):
    try:
        version, offset, content_offset, checksum = value.split(":")
        if len(version) != 64 or any(c not in "0123456789abcdef" for c in version):
            raise ValueError
        offset, content_offset = int(offset), int(content_offset)
        if offset < 0 or content_offset < 0:
            raise ValueError
        if continuation(version, offset, content_offset) != value:
            raise ValueError
        return version, offset, content_offset
    except (AttributeError, TypeError, ValueError):
        raise ValueError("Invalid memory cursor. Restart the read without a cursor.") from None


def bounded_page(snapshot, *, offset=0, content_offset=0, budget=5999, query=""):
    """Return complete records or explicit character ranges, including the envelope.

    The snapshot starts at offset and is already access-filtered. Project prose
    follows general records; its identity/policy remains visible on every page.
    """
    version = page_version(snapshot, query)
    project = snapshot.get("project")
    total = snapshot["total"] + bool(project)
    result = {k: v for k, v in snapshot.items() if k not in {"entries", "project", "next_offset"}}
    result.update(
        entries=[],
        project={k: v for k, v in project.items() if k != "content"} if project else None,
        next_cursor=None,
        next_offset=None,
    )
    if project:
        result["project"]["content_available_via_tool"] = True
    records = [("entries", r) for r in snapshot["entries"]]
    if project and offset + len(records) >= snapshot["total"]:
        records.append(("project", project))
    index = offset

    def append(section, record):
        if section == "entries":
            result["entries"].append(record)
        else:
            result["project"] = record

    def remove(section):
        if section == "entries":
            result["entries"].pop()
        else:
            result["project"] = {k: v for k, v in project.items() if k != "content"}
            result["project"]["content_available_via_tool"] = True

    def next_page(at, char=0):
        result["next_cursor"] = continuation(version, at, char) if at < total else None
        result["next_offset"] = at if at < snapshot["total"] else None

    for section, record in records:
        text = record["content"]
        if content_offset > len(text):
            raise ValueError("Memory cursor exceeds the record. Restart without a cursor.")
        # Omit duplicated store-owner fields; preserve identity and provenance.
        item = {k: v for k, v in record.items() if k not in {"owner", "deleted"}}
        item["content"] = text[content_offset:]
        if content_offset:
            item["content_range"] = {"start": content_offset, "end": len(text), "total": len(text)}
        append(section, item)
        next_page(index + 1)
        if len(serialized(result)) <= budget:
            index += 1
            content_offset = 0
            continue
        remove(section)
        next_page(index, content_offset)
        # Prefer a whole record on the next page over splitting every boundary.
        if result["entries"] or (result["project"] and "content" in result["project"]):
            break
        low, high = content_offset, len(text)
        while low < high:
            end = (low + high + 1) // 2
            chunk = {
                **item,
                "content": text[content_offset:end],
                "content_range": {"start": content_offset, "end": end, "total": len(text)},
            }
            append(section, chunk)
            next_page(index, end)
            fits = len(serialized(result)) <= budget
            remove(section)
            if fits:
                low = end
            else:
                high = end - 1
        if low == content_offset:
            raise ValueError(
                "The memory output budget cannot fit a record section. Increase the tool output budget."
            )
        append(
            section,
            {
                **item,
                "content": text[content_offset:low],
                "content_range": {"start": content_offset, "end": low, "total": len(text)},
            },
        )
        next_page(index, low)
        break
    if len(serialized(result)) > budget:
        raise ValueError(
            "The memory output budget cannot fit page metadata. Increase the tool output budget."
        )
    return result
