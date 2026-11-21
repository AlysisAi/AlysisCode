from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

import pytest

from alysis_code import committed_edits
from alysis_code.ide.event_stream import EventContext, ProtocolEventSurface
from alysis_code.tools import fs


def digest(data):
    return hashlib.sha256(data).hexdigest()


def test_commits_report_exact_versions_in_their_own_turn(tmp_path):
    root = tmp_path / "project"
    other = tmp_path / "sibling"
    root.mkdir()
    other.mkdir()
    events = []
    surface = ProtocolEventSurface(context=EventContext("session"), emit=events.append)
    surface.with_job("first")
    with surface.observe_committed_edits(root):
        fs.fs_write(root=root, path="alpha.txt", content="γειά\r\n")
        fs.fs_write(root=root, path="alpha.txt", content="γειά\r\n")
        fs.fs_write(root=other, path="alpha.txt", content="other")
        (root / "alpha.txt").write_bytes(b"human\r\n")
        edit = fs.prepare_fs_edit(
            root=root,
            path="alpha.txt",
            edits=[{"op": "replace_exact", "target": "human", "replacement": "updated"}],
        )
        fs.write_prepared_fs_edit(edit, root=root)
        with pytest.raises(fs.StaleFileError):
            fs.write_prepared_fs_edit(edit, root=root)
        surface.with_job("second")
        with surface.observe_committed_edits(root):
            fs.fs_delete(root=root, path="alpha.txt")
        # The outer observer retains its original owning job.
        fs.fs_write(root=root, path="empty.txt", content="")
        fs.fs_write(root=root, path=".env", content="fixture")
        fs.fs_write(root=root, path="sk-abcdefghijklmno.txt", content="fixture")
    fs.fs_write(root=root, path="after.txt", content="outside turn")
    assert [(e["session_id"], e["job_id"], e["type"]) for e in events] == [
        ("session", j, "file_edit_committed") for j in ("first", "first", "second", "first")
    ]
    payloads = [dict(e["payload"]) for e in events]
    ids = [p.pop("id") for p in payloads]
    assert len(set(ids)) == 4
    assert all(len(i) == 32 for i in ids)
    assert [p.pop("workspace") for p in payloads] == [str(root)] * 4
    assert payloads == [
        dict(path="alpha.txt", before=None, after=digest("γειά\r\n".encode()), added=1, removed=0),
        dict(
            path="alpha.txt",
            before=digest(b"human\r\n"),
            after=digest(b"updated\r\n"),
            added=1,
            removed=1,
        ),
        dict(path="alpha.txt", before=digest(b"updated\r\n"), after=None, added=0, removed=1),
        dict(path="empty.txt", before=None, after=digest(b""), added=0, removed=0),
    ]


def test_binary_and_large_files_report_only_content_identities(tmp_path):
    receipts = []
    data = bytes(range(256))
    (tmp_path / "binary").write_bytes(data)
    text = "a" * 9000
    with committed_edits.observe(tmp_path, receipts.append):
        fs.fs_delete(root=tmp_path, path="binary")
        fs.fs_write(root=tmp_path, path="large", content=text)
    assert receipts == [
        {"path": "binary", "before": digest(data), "after": None},
        {"path": "large", "before": None, "after": digest(text.encode())},
    ]


def test_worker_envelope_matches_request_and_only_reports_success(tmp_path, monkeypatch, capsys):
    import sys

    from alysis_code import host_read_worker

    monkeypatch.chdir(tmp_path)
    receipts = []
    condition = fs.capture_file_precondition(root=tmp_path, path="x.txt")
    args = {"path": "x.txt", "precondition": asdict(condition), "content": "one"}
    with committed_edits.observe(tmp_path, receipts.append):
        expected = committed_edits.expected_worker_edit(tmp_path, "fs_write_prepared", args)
        assert expected == {"path": "x.txt", "before": None, "after": digest(b"one")}
        packet = tmp_path / "request.json"
        raw = json.dumps({"tool": "fs_write_prepared", "arguments": args}).encode()
        packet.write_bytes(raw)
        monkeypatch.setattr(sys, "argv", ["worker", "--request-file", str(packet), digest(raw)])
        assert host_read_worker.main() == 0
        message = json.loads(capsys.readouterr().out)
        assert message["ok"] and message["result"]["created"]
        evidence = message["committed_edit"]
        for bad in [
            None,  # Older workers return only the mutation result.
            dict(evidence, extra="unexpected"),
            dict(evidence, path="y.txt"),
            dict(evidence, before="f" * 64),
            dict(evidence, added=True),
            {"path": []},
        ]:
            committed_edits.relay_worker(tmp_path, expected, bad)
        assert not receipts
        committed_edits.relay_worker(tmp_path, expected, evidence)
        assert receipts == [dict(expected, added=1, removed=0)]
        assert committed_edits.expected_worker_edit(tmp_path, "fs_mkdir", args) is None
        assert host_read_worker.main() != 0
        assert "committed_edit" not in json.loads(capsys.readouterr().out)
