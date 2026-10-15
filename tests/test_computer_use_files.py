from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator

from alysis_code.agent.computer_use_files import (
    COMPUTER_USE_FILE_TOOL_NAMES,
    computer_use_file_delivery_status,
    file_tool_parameters,
    validate_file_arguments,
    validate_file_result,
)
from alysis_code.agent.computer_use_tools import COMPUTER_USE_TOOL_NAMES, build_computer_use_tools
from alysis_code.agent.sensitive_output import (
    redact_sensitive_tool_arguments,
    sensitive_tool_boundary,
)
from alysis_code.ide.health import capabilities_payload
from alysis_code.ide.protocol import ProtocolError
from alysis_code.ide.stdio_bridge import ComputerUseHostBinding, _computer_use_host_param
from alysis_code.provider_auth.openai_codex import _sanitize_openai_tool_schema


def artifact(delivered=False, identifier="artifact-1"):
    return {
        "file_result": {
            "version": 1,
            "operation": "download",
            "status": "artifact",
            "artifact": {
                "artifactId": identifier,
                "status": "delivered" if delivered else "start_unknown",
                "browserComplete": delivered,
                "localIntegrity": "local_hash_recorded" if delivered else "not_verified",
                "sourceIdentity": "not_verified",
                "proofScope": "selected_link_bytes",
                "businessRecordVerified": False,
                "historicalDeliveryObserved": False,
                "byteLength": 12 if delivered else None,
                "requiresReconciliation": not delivered,
                "cancelRequired": False,
                "deliveryStatus": "delivered" if delivered else "not_requested",
                "delivered": delivered,
            },
        }
    }


def setup(status="needs_user_setup"):
    return {
        "file_result": {"version": 1, "operation": "download", "status": status, "artifact": None}
    }


def state():
    return {
        "snapshot_id": "trusted-snapshot",
        "target": {"bundle_id": "ai.alysis.alysis.chrome-tab"},
        "elements": [],
    }


def test_file_pair_is_explicitly_negotiated_independently_of_verification():
    feature = capabilities_payload()["features"]["computer_use_host"]
    assert feature["tools"] == list(COMPUTER_USE_TOOL_NAMES)
    assert feature["file_tools"] == list(COMPUTER_USE_FILE_TOOL_NAMES)
    assert tuple(build_computer_use_tools(lambda *_: {})) == COMPUTER_USE_TOOL_NAMES
    for verification in [False, True]:
        for files in [False, True]:
            binding = ComputerUseHostBinding(
                operation_id="operation",
                broker_fingerprint="sha256:" + "a" * 64,
                authority_epoch=1,
                scope_digest="sha256:" + "b" * 64,
                verification_available=verification,
                files_available=files,
            )
            raw = binding.to_protocol()
            parsed = _computer_use_host_param({"computer_use_host": raw}, request_id="r")
            assert parsed.files_available is files and parsed.verification_available is verification
            assert tuple(
                build_computer_use_tools(
                    lambda *_: {}, verification_available=verification, files_available=files
                )
            ) == tuple(raw["tools"])
    for names in [
        [*COMPUTER_USE_TOOL_NAMES, "computer_download"],
        [*COMPUTER_USE_TOOL_NAMES, *reversed(COMPUTER_USE_FILE_TOOL_NAMES)],
        [*COMPUTER_USE_TOOL_NAMES, *COMPUTER_USE_FILE_TOOL_NAMES, "computer_verify"],
        [*COMPUTER_USE_TOOL_NAMES, "shell"],
    ]:
        with pytest.raises(ProtocolError):
            _computer_use_host_param({"computer_use_host": {**raw, "tools": names}}, request_id="r")


@pytest.mark.parametrize(
    "name,key", [("computer_download", "element_id"), ("computer_file_status", "artifact_id")]
)
def test_model_schema_is_closed_and_transient_ids_are_private(name, key):
    value = {key: "PRIVATE_OBSERVED_ID"}
    for schema in [
        file_tool_parameters(name),
        _sanitize_openai_tool_schema(file_tool_parameters(name)),
    ]:
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        assert validator.is_valid(value)
        for extra in [
            "url",
            "path",
            "filename",
            "snapshot_id",
            "grant",
            "sha256",
            "proof",
            "retry",
        ]:
            assert not validator.is_valid({**value, extra: "forged"})
            assert not validate_file_arguments(name, {**value, extra: "forged"})
    assert validate_file_arguments(name, value)
    boundary = sensitive_tool_boundary(name, value)
    assert boundary.sensitive
    assert "PRIVATE_OBSERVED_ID" not in json.dumps(
        redact_sensitive_tool_arguments(name, value, boundary=boundary)
    )
    for invalid in [{}, {key: ""}, {key: " leading"}, {key: "bad\nline"}, {key: []}]:
        assert not validate_file_arguments(name, invalid)


def test_download_injects_only_trusted_snapshot_and_status_never_mints_one():
    calls = []

    def request(name, args):
        calls.append((name, args.copy()))
        return state() if name == "computer_state" else artifact(True)

    tools = build_computer_use_tools(request, files_available=True)
    assert (
        tools["computer_download"].run({"element_id": "link"})["error"]
        == "computer_use_fresh_observation_required"
    )
    tools["computer_state"].run({})
    assert (
        tools["computer_download"].run({"element_id": "link", "snapshot_id": "model"})["error"]
        == "invalid_file_arguments"
    )
    assert tools["computer_download"].run({"element_id": "link"}) == artifact(True)
    assert calls[-1] == (
        "computer_download",
        {"element_id": "link", "snapshot_id": "trusted-snapshot"},
    )
    assert computer_use_file_delivery_status(tools)["complete"] is False, (
        "start response cannot be independent delivery proof"
    )
    assert tools["computer_file_status"].run({"artifact_id": "artifact-1"}) == artifact(True)
    assert computer_use_file_delivery_status(tools)["complete"] is True
    assert (
        tools["computer_download"].run({"element_id": "link"})["error"]
        == "computer_use_fresh_observation_required"
    )
    assert (
        tools["computer_action"].run({"action": "press", "element_id": "link"})["error"]
        == "computer_use_fresh_observation_required"
    )
    assert len(calls) == 3


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(path="/private"),
        lambda r: r.update(snapshot_id="manufactured"),
        lambda r: r["file_result"].update(token="SECRET"),
        lambda r: r["file_result"].update(status=[]),
        lambda r: r["file_result"]["artifact"].update(filename="private.pdf"),
        lambda r: r["file_result"]["artifact"].update(delivered=False),
        lambda r: r["file_result"]["artifact"].update(businessRecordVerified=True),
        lambda r: r["file_result"]["artifact"].update(browserComplete=False),
        lambda r: r["file_result"]["artifact"].update(byteLength=True),
        lambda r: r["file_result"]["artifact"].update(proofScope="legacy_unqualified"),
    ],
)
def test_file_result_rejects_leaks_and_inconsistent_claims_before_dom_validation(mutation):
    response = artifact(True)
    mutation(response)
    assert not validate_file_result(response)
    tools = build_computer_use_tools(lambda *_: response, files_available=True)
    assert (
        tools["computer_file_status"].run({"artifact_id": "artifact-1"})["error"]
        == "invalid_file_response"
    )


def test_setup_unknown_start_and_failed_reads_do_not_reconcile_each_other_or_grow_unbounded():
    response = [state()]
    tools = build_computer_use_tools(lambda *_: deepcopy(response[0]), files_available=True)
    tools["computer_state"].run({})
    response[0] = {"error": "transport_lost"}
    tools["computer_download"].run({"element_id": "link"})
    response[0] = state()
    tools["computer_state"].run({})
    response[0] = setup()
    tools["computer_download"].run({"element_id": "link"})
    status = computer_use_file_delivery_status(tools)
    assert not status["complete"] and status["uncertain"] and status["needs_user_setup"]
    response[0] = artifact(True)
    tools["computer_file_status"].run({"artifact_id": "artifact-1"})
    assert not computer_use_file_delivery_status(tools)["complete"]
    binding = tools["computer_file_status"].metadata["_computer_use_file_binding"]
    response[0] = {}
    for index in range(100):
        tools["computer_file_status"].run({"artifact_id": f"unknown-{index}"})
    assert len(binding.artifacts) == 1
    assert binding.artifacts["artifact-1"] is True  # Other failed IDs did not falsify this receipt.
    tools["computer_file_status"].run({"artifact_id": "artifact-1"})
    assert binding.artifacts["artifact-1"] is False


def test_failed_or_mismatched_status_cannot_preserve_old_delivery():
    response = [artifact(True)]

    def request(*_):
        if isinstance(response[0], Exception):
            raise response[0]
        return response[0]

    tools = build_computer_use_tools(request, files_available=True)
    tools["computer_file_status"].run({"artifact_id": "artifact-1"})
    response[0] = artifact(True, "other")
    assert (
        tools["computer_file_status"].run({"artifact_id": "artifact-1"})["error"]
        == "invalid_file_response"
    )
    assert not computer_use_file_delivery_status(tools)["complete"]
    response[0] = artifact(True)
    tools["computer_file_status"].run({"artifact_id": "artifact-1"})
    response[0] = RuntimeError("lost private data")
    with pytest.raises(RuntimeError):
        tools["computer_file_status"].run({"artifact_id": "artifact-1"})
    assert not computer_use_file_delivery_status(tools)["complete"]


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("setup", 1),
        ("permission", 1),
        ("pending", 1),
        ("delivered-start", 1),
        ("independent-status", 0),
    ],
)
def test_actual_sensitive_turn_cannot_finalize_setup_or_unverified_start(tmp_path, mode, expected):
    from test_ide_computer_use_host import _RecordingSurface, _ScriptedComputerUseClient

    from alysis_code.agent_loop import create_session
    from alysis_code.config import AppConfig
    from alysis_code.llm.types import LLMResponse, ToolCall

    calls = []

    def request(name, args):
        calls.append((name, args.copy()))
        if name == "computer_state":
            return state()
        if name == "computer_file_status":
            return artifact(True)
        if mode == "setup":
            return setup()
        if mode == "permission":
            return setup("permission_required")
        return artifact(mode in {"delivered-start", "independent-status"})

    sequence = [("computer_state", {}), ("computer_download", {"element_id": "link"})]
    if mode == "independent-status":
        sequence.append(("computer_file_status", {"artifact_id": "artifact-1"}))
    responses = [
        LLMResponse(
            content="", tool_calls=[ToolCall(id=f"call-{i}", name=name, arguments=args)], raw={}
        )
        for i, (name, args) in enumerate(sequence)
    ]
    responses.append(
        LLMResponse(content="All invoices downloaded successfully.", tool_calls=[], raw={})
    )
    surface = _RecordingSurface()
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=6,
        no_log=True,
        api_key_override="test-key",
        one_shot_execution=True,
        surface=surface,
        sensitive_media=True,
        computer_use_host_request=request,
        computer_use_files_available=True,
    )
    session.client = _ScriptedComputerUseClient(responses)
    try:
        assert session.run_turn("Download the selected link into my chosen folder.") == expected
    finally:
        session.close()
    assert [name for name, _ in calls] == [name for name, _ in sequence]
    assert all("All invoices downloaded" not in text for text in surface.final_messages)
    if expected:
        assert any(
            "not complete" in text or "not independently verified" in text
            for text in surface.final_messages
        )


@pytest.mark.parametrize("review_seconds", [18, 19])
def test_download_exact_approval_uses_original_host_deadline(tmp_path, monkeypatch, review_seconds):
    from test_computer_use_approval_expiry import TOKEN, fixture

    bridge, session, job, clock, records, waits, events, calls, handler, allow, _run = fixture(
        tmp_path, monkeypatch, transport=11
    )
    session.computer_use_host = replace(session.computer_use_host, files_available=True)

    def review(event):
        if event["type"] == "approval.required":
            clock.advance(review_seconds)
            allow()

    handler[0] = review
    result = bridge._request_computer_use(
        session, "computer_download", {"element_id": "link", "snapshot_id": "original"}
    )
    assert len(calls) == (2 if review_seconds == 18 else 1)
    if review_seconds == 18:
        assert calls[-1] == {
            "element_id": "link",
            "snapshot_id": "original",
            "approval_phase": "execute",
            "approval_token": TOKEN,
        }
    else:
        assert result["error"] == "computer_use_approval_expired"
    assert TOKEN not in json.dumps(events)


def test_download_setup_does_not_wait_for_an_approval_or_send_execute(tmp_path, monkeypatch):
    from test_computer_use_approval_expiry import fixture

    bridge, session, _job, _clock, records, waits, _events, _calls, _handler, _allow, _run = (
        fixture(tmp_path, monkeypatch)
    )
    calls = []
    monkeypatch.setattr(
        bridge,
        "_exchange_computer_use",
        lambda session, tool, args: calls.append(args.copy()) or setup(),
    )
    assert (
        bridge._request_computer_use(
            session, "computer_download", {"element_id": "link", "snapshot_id": "original"}
        )
        == setup()
    )
    assert len(calls) == 1 and calls[0]["approval_phase"] == "prepare"
    assert not records and not waits


def test_file_terminal_diagnostics_preserve_closed_setup_and_delivery_codes():
    from alysis_code.computer_use_diagnostics import (
        computer_use_local_result_failure_code,
        computer_use_terminal_failure_code,
    )

    for reason in ["computer_use_file_setup_required", "computer_use_file_delivery_unverified"]:
        assert computer_use_terminal_failure_code(1, reason) == reason
        assert computer_use_terminal_failure_code(0, reason) is None
    assert (
        computer_use_terminal_failure_code(1, "private filename.pdf") == "computer_use_turn_failed"
    )
    assert (
        computer_use_local_result_failure_code(
            "computer_download", {"error": "computer_use_approval_expired"}
        )
        == "computer_use_approval_expired"
    )
