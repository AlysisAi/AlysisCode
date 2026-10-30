import json
import os
import time

import httpx

from alysis_code.llm import focused_input_diagnostic as diagnostic


def response():
    return httpx.Response(
        200,
        headers={"x-request-id": "req_trial-123"},
        request=httpx.Request(
            "POST",
            "https://example.invalid/responses",
            json={
                "model": "trial",
                "input": [{"secret": "not logged"}],
                "tools": [{}],
                "previous_response_id": "private-response-id",
            },
        ),
    )


def test_opt_in_expiration_and_no_side_effect_without_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostic, "diagnostic_root", lambda: tmp_path)
    for value in (None, "invalid", str(time.time() - 1), str(time.time() + 9000)):
        if value is not None:
            (tmp_path / "enabled-until.txt").write_text(value)
        diagnostic.record_stream_diagnostic(response())
        assert not (tmp_path / "provider.jsonl").exists()


def test_error_preserved_and_request_content_excluded(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostic, "diagnostic_root", lambda: tmp_path)
    (tmp_path / "enabled-until.txt").write_text(str(time.time() + 300))
    diagnostic.record_stream_diagnostic(response())
    diagnostic.record_stream_diagnostic(
        response(),
        {
            "type": "error",
            "error": {
                "code": "unfamiliar_error",
                "message": "Bad Authorization: Bearer abcdefghijklmno",
                "param": "input.0",
                "private_extra": "must not persist",
            },
        },
        event_count=9,
    )
    path = tmp_path / "provider.jsonl"
    raw = path.read_text()
    rows = [json.loads(line) for line in raw.splitlines()]
    assert rows[1]["error_code"] == "unfamiliar_error"
    assert rows[1]["event_count"] == 9
    assert rows[1]["x-request-id"] == "req_trial-123"
    assert rows[0]["previous_response_id_used"] is True
    for excluded in ("abcdefghijklmno", "not logged", "private-response-id", "must not persist"):
        assert excluded not in raw
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_log_cap_and_symlink_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostic, "diagnostic_root", lambda: tmp_path)
    (tmp_path / "enabled-until.txt").write_text(str(time.time() + 300))
    path = tmp_path / "provider.jsonl"
    target = tmp_path / "target"
    target.write_text("preserved")
    path.symlink_to(target)
    diagnostic.record_stream_diagnostic(response())
    assert target.read_text() == "preserved"
    path.unlink()
    path.write_bytes(b" " * 1_048_576)
    diagnostic.record_stream_diagnostic(response())
    assert path.stat().st_size == 1_048_576
