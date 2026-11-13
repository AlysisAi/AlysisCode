"""Host-registered provenance for memory delivered through artifact projections.

The registry is text-free and durable in the session event log. A locator or a
tool's self-declared metadata is never evidence on its own: validate the saved
artifact digest and the exact returned slice before recording delivery.
"""

from __future__ import annotations

import hashlib
import json

from .memory_evidence import source
from .memory_pages import serialized

EVENT = "memory_artifact_created"
MAX_MEMORY_ARTIFACT_BYTES = 8 * 1024 * 1024


def _records(result):
    return [*result.get("entries", []), *([result["project"]] if result.get("project") else [])]


def _escaped(text):
    return json.dumps(text, ensure_ascii=False)[1:-1]


def _spans(text, result, *, nested):
    """Locate serialized bodies, including their JSON quotes, in exact output."""
    spans = []
    for record in _records(result):
        if not isinstance(record.get("content"), str):
            continue
        encoded = serialized(record)
        value = serialized(record["content"])
        body_start = encoded.index('"content":' + value) + len('"content":')
        transform = _escaped if nested else lambda text: text
        needle = transform(encoded)
        position = text.find(needle)
        if position < 0:
            continue
        start = position + len(transform(encoded[:body_start]))
        end = start + len(transform(value))
        spans.append(
            {
                "record": source("", [record])["records"][0],
                "start": len(text[:start].encode()),
                "end": len(text[:end].encode()),
            }
        )
    return spans


def register_memory_artifact(session, result, offload, scope):
    layout = session.store.session_artifact_layout
    path = layout.resolve_locator(offload.artifact_locator)
    with path.open("rb") as artifact:
        raw = artifact.read(MAX_MEMORY_ARTIFACT_BYTES + 1)
    if len(raw) > MAX_MEMORY_ARTIFACT_BYTES:
        raise ValueError("Memory artifact exceeds the provenance inspection limit.")
    text = raw.decode("utf-8")
    records = _spans(text, result, nested=True)
    if records:
        session.store.append(
            EVENT,
            {
                "locator": offload.artifact_locator,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
                "scope": scope,
                "version": result.get("generation"),
                "records": records,
            },
        )


def _registry(session):
    state = getattr(session, "_memory_artifact_registry", None)
    if state is None:
        state = {"cursor": 0, "artifacts": {}}
        session._memory_artifact_registry = state
    events, state["cursor"] = session.store.events_since(state["cursor"])
    for event in events:
        if event.get("type") in {"conversation_cleared", "subagent_history_rollover_armed"}:
            state["artifacts"].clear()
        elif event.get("type") == EVENT:
            item = event["payload"]
            state["artifacts"][item["locator"]] = item
    return state["artifacts"]


def _delivered(manifest, spans, start, end):
    records = []
    for span in spans:
        left, right = max(start, span["start"]), min(end, span["end"])
        if left >= right:
            continue
        record = dict(span["record"])
        if left != span["start"] or right != span["end"]:
            original_range = record.pop("content_range", None)
            record["body_supplied"] = False
            record["body_fragment"] = {
                "start": left - span["start"],
                "end": right - span["start"],
                "total": span["end"] - span["start"],
                "unit": "serialized_utf8_bytes",
                **({"source_content_range": original_range} if original_range is not None else {}),
            }
        records.append(record)
    if records:
        return {
            "scope": manifest["scope"],
            "version": manifest["version"],
            "records": records,
            "selection_complete": False,
        }
    return None


def artifact_source(session, value):
    """Resolve only a fresh, surviving tool message at the provider boundary."""
    locator = value.get("locator") or value.get("artifact_locator")
    if not isinstance(locator, str):
        return None
    manifest = _registry(session).get(locator)
    if manifest is None:
        return None
    try:
        path = session.store.session_artifact_layout.resolve_locator(locator)
        size = manifest.get("size_bytes")
        if type(size) is not int or not 0 <= size <= MAX_MEMORY_ARTIFACT_BYTES:
            return None
        with path.open("rb") as artifact:
            raw = artifact.read(size + 1)
        if len(raw) != size:
            return None
        if hashlib.sha256(raw).hexdigest() != manifest["sha256"]:
            return None
        if value.get("offloaded") is True:
            payload = json.loads(raw)
            original = payload["content_json"]
            count = value.get("preview_chars")
            if type(count) is not int or count < 0:
                return None
            prefix = original[:count]
            if value.get("preview") not in (prefix, prefix + "...(truncated)"):
                return None
            return _delivered(
                manifest,
                _spans(original, json.loads(original), nested=False),
                0,
                len(prefix.encode()),
            )
        start, size = value.get("offset"), value.get("bytes_returned")
        if type(start) is not int or type(size) is not int or start < 0 or size < 0:
            return None
        end = start + size
        if end > len(raw) or raw[start:end].decode("utf-8") != value.get("content"):
            return None
        return _delivered(manifest, manifest["records"], start, end)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        # Missing, edited, redacted or malformed artifacts are unknown coverage.
        return None
