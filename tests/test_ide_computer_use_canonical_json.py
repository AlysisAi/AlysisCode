from __future__ import annotations

import json
import threading
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_ide_computer_use_host import (
    _bridge_session,
    _invoke_host_request,
    _lines,
    _request,
    _wait_for_event,
)

from alysis_code import computer_use_platform_policy
from alysis_code.ide.canonical_json import (
    CanonicalJsonError,
    canonical_json,
    canonical_json_fingerprint,
)
from alysis_code.ide.stdio_bridge import BridgeCancellationError


@pytest.fixture(autouse=True)
def legacy_mac_host_contract(monkeypatch):
    """Legacy Mac inert host contract; Windows/new-route denials stay separate."""
    monkeypatch.setattr(computer_use_platform_policy, "sys", SimpleNamespace(platform="darwin"))


# The app's tests/fixtures/computer-use-canonical-json-v1.json is an identical copy.
VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "computer_use_canonical_json_v1.json").read_text("utf-8")
)["vectors"]


def _vector(name: str) -> dict[str, str]:
    return next(vector for vector in VECTORS if vector["name"] == name)


@pytest.mark.parametrize("vector", VECTORS, ids=[vector["name"] for vector in VECTORS])
def test_shared_vectors_have_the_same_canonical_form_as_the_app(vector: dict[str, str]) -> None:
    value = json.loads(vector["input"])

    assert canonical_json(value) == vector["canonical"]
    assert canonical_json_fingerprint(value) == vector["fingerprint"]


@pytest.mark.parametrize(
    "value",
    [
        {"x": float("nan")},
        {"x": float("inf")},
        {"x": [float("-inf")]},
        {"x": 10**400},
        {1: "integer key"},
        {"x": object()},
        {"x": b"bytes"},
        {chr(0xD83D) + chr(0xDE00): 1, chr(0x1F600): 2},
    ],
    ids=[
        "nan",
        "infinity",
        "negative_infinity",
        "huge_integer",
        "integer_key",
        "object",
        "bytes",
        "surrogate_key_collision",
    ],
)
def test_values_outside_finite_json_have_no_canonical_form(value: Any) -> None:
    with pytest.raises(CanonicalJsonError):
        canonical_json(value)


def test_host_request_fingerprint_matches_the_app_canonical_form(tmp_path: Path) -> None:
    bridge, out, session, _job = _bridge_session(tmp_path)
    vector = _vector("computer_action_drag")
    result: dict[str, Any] = {}

    def invoke() -> None:
        # The test only inspects the emitted request, then cancels the waiting call.
        with suppress(BridgeCancellationError):
            _invoke_host_request(
                bridge, session, "computer_action", json.loads(vector["input"]), result
            )

    worker = threading.Thread(target=invoke)
    worker.start()
    try:
        payload = _wait_for_event(out, "computer_use_request")["payload"]

        assert payload["arguments_fingerprint"] == vector["fingerprint"]
    finally:
        session.active_job.cancellation_event.set()
        worker.join(timeout=2)
    assert not worker.is_alive()


def test_non_finite_arguments_are_refused_before_any_host_request(tmp_path: Path) -> None:
    bridge, out, session, _job = _bridge_session(tmp_path)
    result: dict[str, Any] = {}
    worker = threading.Thread(
        target=_invoke_host_request,
        args=(
            bridge,
            session,
            "computer_action",
            {"snapshot_id": "snapshot-1", "action": "click", "x": float("nan"), "y": 1.0},
            result,
        ),
    )
    worker.start()
    worker.join(timeout=2)
    if worker.is_alive():
        session.active_job.cancellation_event.set()
        worker.join(timeout=2)

    assert result.get("error") == "invalid_tool_arguments_json"
    assert all(line.get("type") != "computer_use_request" for line in _lines(out))
    assert session.pending_computer_use == {}


def test_a_refused_request_resolves_only_its_own_tool_call(tmp_path: Path) -> None:
    # The app answers a mismatched request with computer.respond ok:false; the engine
    # returns that error to the one tool call and keeps serving the session.
    bridge, out, session, _job = _bridge_session(tmp_path)
    result: dict[str, Any] = {}
    worker = threading.Thread(
        target=_invoke_host_request,
        args=(bridge, session, "computer_state", {}, result),
    )
    worker.start()
    event = _wait_for_event(out, "computer_use_request")
    payload = event["payload"]
    bridge.process_line(
        _request(
            "computer.respond",
            {
                "session_id": session.session_id,
                "request_id": payload["request_id"],
                "response_nonce": payload["response_nonce"],
                "operation_id": payload["operation_id"],
                "job_id": event["job_id"],
                "tool_call_id": payload["tool_call_id"],
                "tool_name": payload["tool_name"],
                "arguments_fingerprint": payload["arguments_fingerprint"],
                "expires_at": payload["expires_at"],
                "ok": False,
                "error": {
                    "code": "computer_use_binding_mismatch",
                    "message": (
                        "Computer Use request arguments do not match their fingerprint; "
                        "it was not dispatched"
                    ),
                },
            },
            "refuse-1",
        )
        + "\n"
    )
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result["error"] == "computer_use_binding_mismatch"
    response = next(line for line in _lines(out) if line.get("id") == "refuse-1")
    assert response["ok"] is True
    assert response["result"]["ok"] is False
    assert not session.closed
