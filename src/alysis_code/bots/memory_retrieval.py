"""Bounded Bots reference selection and searchable pages, independent of storage.

BM25 over Unicode words and character trigrams is lexical retrieval, not semantic
embedding search. No language-specific dictionaries, stop words or task routing.
Models can reformulate searches and browse pages to recover unmatched concepts.
Stored records are never truncated, rewritten or deleted by selection.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections import Counter


def serialized(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def terms(text):
    text = unicodedata.normalize("NFC", text).casefold()
    words, current = [], []
    for char in text:
        if unicodedata.category(char)[0] in {"L", "N", "M"}:
            current.append(char)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return Counter(
        term
        for word in words
        for term in ["w:" + word, *("g:" + word[i : i + 3] for i in range(len(word) - 2))]
    )


def ranked(records, query="", *, include_unmatched=False):
    """Rank actual text; recency is only a tie-breaker/fallback, not truth."""
    recent = sorted(records, key=lambda r: (r.get("updated_at", 0), r["id"]), reverse=True)
    wanted = terms(query)
    if not query.strip():
        return recent
    documents = [terms(record["title"] + "\n" + record["content"]) for record in recent]
    lengths = [sum(document.values()) for document in documents]
    average = sum(lengths) / max(1, len(lengths)) or 1
    frequency = Counter(term for document in documents for term in wanted if term in document)
    scored = []
    for record, document, length in zip(recent, documents, lengths, strict=True):
        # Exact substrings also cover short queries in scripts without spaces.
        text = unicodedata.normalize("NFC", record["title"] + "\n" + record["content"]).casefold()
        phrase = unicodedata.normalize("NFC", query).casefold().strip()
        score = math.log1p(len(records)) if phrase and phrase in text else 0.0
        for term in wanted.keys() & document.keys():
            inverse = math.log(
                1 + (len(documents) - frequency[term] + 0.5) / (frequency[term] + 0.5)
            )
            count = document[term]
            score += inverse * count * 2.2 / (count + 1.2 * (0.25 + 0.75 * length / average))
        if score or include_unmatched:
            scored.append((score, record))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [record for _, record in scored]


def selected(
    records, *, query="", budget=12000, keys=("id", "title", "updated_at"), preferred_ids=()
):
    """Bound both identity metadata and bodies; never imply a subset is complete."""
    items, used = [], 2
    ordered = ranked(records, query, include_unmatched=True)
    preferred = set(preferred_ids)
    ordered.sort(
        key=lambda r: (
            0
            if r.get("context_scope") == "general"
            else 1
            if r["id"] in preferred
            else 2
            if "context_scope" in r and r["context_scope"] is None
            else 3
        )
    )
    for record in ordered:
        item = {key: record[key] for key in keys if key in record}
        full = {**item, "content": record["content"]}
        if used + len(serialized(full)) + 1 <= budget:
            item = full
        else:
            item["content_available_via_tool"] = True
        size = len(serialized(item)) + 1
        if used + size > budget:
            continue
        items.append(item)
        used += size
    return {
        "items": items,
        "total_count": len(records),
        "index_complete": len(items) == len(records),
        "omitted_count": len(records) - len(items),
    }


def page(records, *, query="", offset=0, limit=20, generation=None, bodies=True):
    """Stable pages within one generation; callers must restart on changes.

    Pending proposal content is never searched or exposed as approved knowledge.
    Fingerprints remain private identifiers, not model-readable record dumps.
    """
    version = hashlib.sha256(serialized(records).encode()).hexdigest()
    if generation is not None and generation != version:
        raise ValueError("Saved references changed. Restart the search from offset zero.")
    matches = ranked(records, query)
    found = []
    for record in matches[offset : offset + limit]:
        item = {
            key: record[key]
            for key in (
                "id",
                "title",
                "status",
                "updated_at",
                "revision",
                "source_task_id",
                "replaces_memory_id",
                "archived",
                "consolidated_into",
            )
            if key in record
        }
        if bodies:
            item["content"] = record["content"]
        else:
            item["content_available_via_tool"] = True
        found.append(item)
    end = offset + len(found)
    return {
        "items": found,
        "total_count": len(records),
        "matched_count": len(matches),
        "next_offset": end if end < len(matches) else None,
        "generation": version,
        "retrieval": "lexical_unicode_bm25" if query else "browse_recent",
    }


def change_snapshot(receipt, *, budget=3000, events_key="events"):
    """Bound audit metadata in model context; full original receipts stay pageable."""
    result = {k: v for k, v in receipt.items() if k != events_key}
    result[events_key] = []
    used = len(serialized(result))
    source = receipt.get(events_key, [])
    for event in source:
        size = len(serialized(event)) + 1
        if used + size > budget:
            break
        result[events_key].append(event)
        used += size
    if len(result[events_key]) < len(source):
        result["next_before_seq"] = (
            result[events_key][-1]["seq"] if result[events_key] else source[0]["seq"] + 1
        )
        result["selection_partial"] = True
    return result


def read_memory_ids(messages):
    """Exact IDs requested through the current tool protocol, not prose guesses."""
    identities = []
    for message in messages:
        for call in message.get("tool_calls", []):
            function = call.get("function", {})
            if function.get("name") != "read_memory":
                continue
            arguments = function.get("arguments", {})
            try:
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                identity = arguments.get("memory_id")
            except (ValueError, AttributeError):
                continue
            if isinstance(identity, str) and identity not in identities:
                identities.append(identity)
    return identities
