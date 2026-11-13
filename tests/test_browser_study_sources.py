from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

SnapshotSources = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/cloud-dev/browser_study_worker.py")
)["SnapshotSources"]


def test_source_paging_returns_exact_immutable_multilingual_observation():
    sources = SnapshotSources(chunk_chars=19)
    original = {"dom": {"text": "  Ελληνικά — 日本語\n" * 20}, "text": "source"}
    serialized = json.dumps(original, ensure_ascii=False)
    source_id, size = sources.add(original)
    original["dom"]["text"] = "changed after recording"
    offset = 0
    chunks = []
    while offset is not None:
        result = sources.read(source_id, offset)
        assert result["offset"] == offset
        assert result["total_chars"] == size == len(serialized)
        assert len(result["content"]) <= 19
        chunks.append(result["content"])
        offset = result["next_offset"]
    assert "".join(chunks) == serialized


def test_source_eviction_and_invalid_offsets_fail_instead_of_reading_latest():
    sources = SnapshotSources(capacity=2)
    first, size = sources.add({"text": "first"})
    second, _ = sources.add({"text": "second"})
    sources.read(first, 0)  # Reading is not a fresh observation or a lease renewal.
    third, _ = sources.add({"text": "third"})
    with pytest.raises(KeyError):
        sources.read(first, 0)
    assert json.loads(sources.read(second, 0)["content"]) == {"text": "second"}
    assert json.loads(sources.read(third, 0)["content"]) == {"text": "third"}
    for offset in (-1, size + 100, True, "0"):
        with pytest.raises(ValueError):
            sources.read(third, offset)


def target_snapshot():
    return {
        "dom": {
            "data": {
                "strings": [
                    "http://example.test/",
                    "study",
                    "#document",
                    "HTML",
                    "BODY",
                    "INPUT",
                    "#text",
                ],
                "documents": [
                    {
                        "documentURL": 0,
                        "title": 1,
                        "nodes": {
                            "parentIndex": [-1, 0, 1, 2, 2],
                            "nodeType": [9, 1, 1, 1, 3],
                            "nodeName": [2, 3, 4, 5, 6],
                            "nodeValue": [-1] * 5,
                            "backendNodeId": [10, 11, 12, 13, 14],
                            "attributes": [[], [], [], [], []],
                        },
                        "layout": {"nodeIndex": [0, 1, 2, 3, 4]},
                    }
                ],
            }
        }
    }


def test_target_binds_selector_to_immutable_original_identity():
    sources = SnapshotSources()
    original = target_snapshot()
    source_id, _ = sources.add(original)
    original["dom"]["data"]["documents"][0]["nodes"]["backendNodeId"][3] = 999
    assert sources.target(source_id, 3) == (":root > :nth-child(1) > :nth-child(1)", 13)
    for node in (0, 4, 100, -1, True, "3"):
        with pytest.raises(ValueError):
            sources.target(source_id, node)


def test_target_expiry_cannot_fall_back_to_current_observation():
    sources = SnapshotSources(capacity=1)
    previous, _ = sources.add(target_snapshot())
    current, _ = sources.add(target_snapshot())
    with pytest.raises(KeyError):
        sources.target(previous, 3)
    assert sources.target(current, 3)[1] == 13
    empty, _ = sources.add({"dom": {"data": {"strings": [], "documents": []}}})
    with pytest.raises(ValueError):
        sources.target(empty, 3)
