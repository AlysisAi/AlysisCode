from __future__ import annotations

import json
from copy import deepcopy
from itertools import product

import pytest
from jsonschema import Draft202012Validator

from alysis_code.agent.computer_use_files import (
    COMPUTER_USE_FILE_TOOL_NAMES,
    computer_use_file_delivery_status,
)
from alysis_code.agent.computer_use_tools import (
    COMPUTER_USE_TOOL_NAMES,
    build_computer_use_tools,
)
from alysis_code.agent.computer_use_uploads import (
    COMPUTER_USE_UPLOAD_TOOL_NAMES,
    upload_result_projection,
    upload_tool_parameters,
    validate_upload_arguments,
    validate_upload_result,
)
from alysis_code.agent.sensitive_output import (
    collect_computer_use_private_identifiers,
    redact_sensitive_tool_arguments,
    sensitive_tool_boundary,
)
from alysis_code.ide.health import capabilities_payload
from alysis_code.ide.protocol import ProtocolError
from alysis_code.ide.stdio_bridge import ComputerUseHostBinding, _computer_use_host_param
from alysis_code.provider_auth.openai_codex import _sanitize_openai_tool_schema

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


def upload(phase="page_disclosed", identifier="upload-1", length=12):
    return {
        "upload_result": {
            "version": 1,
            "status": "upload",
            "upload": {
                "uploadId": identifier,
                "phase": phase,
                "exportedBytes": 0 if phase == "granted" else length,
                "byteLength": length,
                "pageDisclosed": phase == "page_disclosed",
                "requiresReconciliation": phase
                in {"staged", "release_prepared", "release_unknown", "page_disclosed", "uncertain"},
                "receiverAcknowledged": False,
                "remoteBytesVerified": False,
                "objectiveVerified": False,
            },
        }
    }


def setup():
    return {"upload_result": {"version": 1, "status": "needs_user_setup", "upload": None}}


def state():
    return {
        "snapshot_id": "trusted-snapshot",
        "target": {"bundle_id": "ai.alysis.alysis.chrome-tab"},
        "elements": [],
    }


def test_upload_pair_negotiates_independently_in_all_eight_ordered_sets():
    capability = capabilities_payload()["features"]["computer_use_host"]
    assert capability["upload_tools"] == list(COMPUTER_USE_UPLOAD_TOOL_NAMES)
    assert capability["upload_remote_evidence_version"] == 1
    assert capability["file_tools"] == list(COMPUTER_USE_FILE_TOOL_NAMES)
    assert capability["tools"] == list(COMPUTER_USE_TOOL_NAMES)
    for verify, files, uploads in product([False, True], repeat=3):
        binding = ComputerUseHostBinding(
            operation_id="op",
            broker_fingerprint="sha256:" + "a" * 64,
            authority_epoch=1,
            scope_digest="sha256:" + "b" * 64,
            verification_available=verify,
            files_available=files,
            uploads_available=uploads,
        )
        raw = binding.to_protocol()
        assert _computer_use_host_param({"computer_use_host": raw}, request_id="r") == binding
        assert tuple(
            build_computer_use_tools(
                lambda *_: {},
                verification_available=verify,
                files_available=files,
                uploads_available=uploads,
            )
        ) == tuple(raw["tools"])
    for tools in [
        [*COMPUTER_USE_TOOL_NAMES, "computer_upload"],
        [*COMPUTER_USE_TOOL_NAMES, *reversed(COMPUTER_USE_UPLOAD_TOOL_NAMES)],
        [*COMPUTER_USE_TOOL_NAMES, *COMPUTER_USE_UPLOAD_TOOL_NAMES, *COMPUTER_USE_FILE_TOOL_NAMES],
        [*COMPUTER_USE_TOOL_NAMES, *COMPUTER_USE_UPLOAD_TOOL_NAMES, "computer_upload_status"],
        [*raw["tools"], "file_read"],
    ]:
        with pytest.raises(ProtocolError):
            _computer_use_host_param({"computer_use_host": {**raw, "tools": tools}}, request_id="r")


@pytest.mark.parametrize(
    "name,key", [("computer_upload", "element_id"), ("computer_upload_status", "upload_id")]
)
def test_upload_schema_sanitization_is_closed_and_identifiers_are_private(name, key):
    value = {key: "PRIVATE_UPLOAD_CANARY_762419"}
    assert validate_upload_arguments(name, value)
    for schema in [
        upload_tool_parameters(name),
        _sanitize_openai_tool_schema(upload_tool_parameters(name)),
    ]:
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        assert validator.is_valid(value)
        for extra in [
            "path",
            "url",
            "filename",
            "bytes",
            "snapshot_id",
            "approval_token",
            "approval_phase",
            "grant",
            "destination",
            "retry",
        ]:
            assert not validator.is_valid({**value, extra: "forged"})
            assert not validate_upload_arguments(name, {**value, extra: "forged"})
    for invalid in [{}, {key: None}, {key: ""}, {key: " leading"}, {key: "bad\nline"}, {key: []}]:
        assert not validate_upload_arguments(name, invalid)
    boundary = sensitive_tool_boundary(name, value)
    assert boundary.sensitive
    assert value[key] not in json.dumps(
        redact_sensitive_tool_arguments(name, value, boundary=boundary)
    )
    assert value[key] in collect_computer_use_private_identifiers(
        name, {"upload_id": value[key], "uploadId": value[key]}
    )


def projection_cases():
    cases = [
        setup(),
        *[
            upload(phase)
            for phase in [
                "granted",
                "staging",
                "staged",
                "release_prepared",
                "release_unknown",
                "page_disclosed",
                "not_disclosed",
                "uncertain",
            ]
        ],
        upload(length=0),
    ]
    for field, value in [
        ("byteLength", True),
        ("byteLength", -1),
        ("byteLength", 8 * 1024 * 1024 + 1),
        ("exportedBytes", 13),
        ("exportedBytes", 0),
        ("phase", "delivered"),
        ("phase", []),
        ("pageDisclosed", False),
        ("pageDisclosed", 1),
        ("requiresReconciliation", False),
        ("receiverAcknowledged", 1),
        ("remoteBytesVerified", True),
        ("objectiveVerified", True),
        ("uploadId", "../private"),
        ("uploadId", ""),
    ]:
        item = upload()
        item["upload_result"]["upload"][field] = value
        cases.append(item)
    for value in [
        {**upload(), "path": "/private/canary"},
        {**upload(), "alysis_workflow": {}},
        {"upload_result": {**setup()["upload_result"], "path": "/private/canary"}},
        {"upload_result": {**setup()["upload_result"], "version": True}},
        {
            "upload_result": {
                **setup()["upload_result"],
                "upload": upload()["upload_result"]["upload"],
            }
        },
        {"upload_result": {**setup()["upload_result"], "status": "permission_required"}},
    ]:
        cases.append(value)
    return cases


def test_closed_projection_rejects_false_success_extra_metadata_and_bad_bounds():
    cases = projection_cases()
    assert all(validate_upload_result(value) for value in cases[:10])
    assert not any(validate_upload_result(value) for value in cases[10:])
    assert upload_result_projection({**upload(), "alysis_workflow": {}}) == upload()
    assert upload_result_projection({**upload(), "path": "/private/canary"}) is None


def remote_projection_cases():
    cases = []
    for phase, acknowledged, remote, reconciliation, objective in product(
        [
            "granted",
            "staging",
            "staged",
            "release_prepared",
            "release_unknown",
            "page_disclosed",
            "not_disclosed",
            "uncertain",
        ],
        [False, True],
        [False, True],
        [False, True],
        [False, True],
    ):
        item = upload(phase)
        item["upload_result"]["upload"].update(
            receiverAcknowledged=acknowledged,
            remoteBytesVerified=remote,
            requiresReconciliation=reconciliation,
            objectiveVerified=objective,
        )
        cases.append(item)
    return cases


def test_remote_evidence_requires_disclosure_then_ack_and_never_implies_objective_completion():
    for item in remote_projection_cases():
        value = item["upload_result"]["upload"]
        disclosed = value["phase"] == "page_disclosed"
        remote = value["remoteBytesVerified"]
        expected = (
            not value["objectiveVerified"]
            and (not value["receiverAcknowledged"] or disclosed)
            and (not remote or value["receiverAcknowledged"])
            and (
                (disclosed and remote)
                or value["requiresReconciliation"]
                == (
                    value["phase"]
                    in {
                        "staged",
                        "release_prepared",
                        "release_unknown",
                        "page_disclosed",
                        "uncertain",
                    }
                )
            )
        )
        assert validate_upload_result(item) is expected, value


@pytest.mark.parametrize(
    "ack,remote,reconcile", [(True, False, True), (True, True, True), (True, True, False)]
)
def test_actual_tool_projection_retains_remote_evidence_without_replay_or_original_completion(
    ack, remote, reconcile
):
    projected = upload()
    projected["upload_result"]["upload"].update(
        receiverAcknowledged=ack, remoteBytesVerified=remote, requiresReconciliation=reconcile
    )
    calls = []

    def request(name, arguments):
        calls.append(name)
        return state() if name == "computer_state" else projected

    tools = build_computer_use_tools(request, uploads_available=True)
    tools["computer_state"].run({})
    assert tools["computer_upload"].run({"element_id": "input"}) == projected
    assert tools["computer_upload_status"].run({"upload_id": "upload-1"}) == projected
    assert calls == ["computer_state", "computer_upload", "computer_upload_status"]
    assert computer_use_file_delivery_status(tools)["complete"] is False


def test_upload_injects_only_trusted_snapshot_and_page_disclosed_status_never_completes():
    calls = []

    def request(name, args):
        calls.append((name, args.copy()))
        return state() if name == "computer_state" else upload()

    tools = build_computer_use_tools(request, uploads_available=True)
    assert (
        tools["computer_upload"].run({"element_id": "input"})["error"]
        == "computer_use_fresh_observation_required"
    )
    tools["computer_state"].run({})
    assert (
        tools["computer_upload"].run({"element_id": "input", "snapshot_id": "model"})["error"]
        == "invalid_upload_arguments"
    )
    assert tools["computer_upload"].run({"element_id": "input"}) == upload()
    assert calls[-1] == (
        "computer_upload",
        {"element_id": "input", "snapshot_id": "trusted-snapshot"},
    )
    assert tools["computer_upload_status"].run({"upload_id": "upload-1"}) == upload()
    assert not computer_use_file_delivery_status(tools)["complete"]
    assert computer_use_file_delivery_status(tools)["pending_count"] == 1
    assert (
        tools["computer_upload"].run({"element_id": "input"})["error"]
        == "computer_use_fresh_observation_required"
    )
    assert (
        tools["computer_action"].run({"action": "press", "element_id": "input"})["error"]
        == "computer_use_fresh_observation_required"
    )
    assert len(calls) == 3


def test_malformed_wrong_id_failed_and_setup_results_cannot_clear_unknown_upload():
    response = [upload()]
    tools = build_computer_use_tools(
        lambda name, _: state() if name == "computer_state" else deepcopy(response[0]),
        uploads_available=True,
    )
    tools["computer_state"].run({})
    response[0] = {"error": "computer_use_timeout"}
    tools["computer_upload"].run({"element_id": "input"})
    response[0] = upload(identifier="other")
    assert (
        tools["computer_upload_status"].run({"upload_id": "upload-1"})["error"]
        == "invalid_upload_response"
    )
    assert computer_use_file_delivery_status(tools)["uncertain"]
    response[0] = upload()
    tools["computer_upload_status"].run({"upload_id": "upload-1"})
    assert computer_use_file_delivery_status(tools)["uncertain"], (
        "unrelated status cannot join an uncorrelated failed start"
    )
    response[0] = setup()
    tools["computer_upload_status"].run({"upload_id": "upload-1"})
    assert computer_use_file_delivery_status(tools)["needs_user_setup"]
    binding = tools["computer_upload_status"].metadata["_computer_use_upload_binding"]
    response[0] = {"error": "unknown_upload"}
    for i in range(100):
        tools["computer_upload_status"].run({"upload_id": f"unknown-{i}"})
    assert len(binding.uploads) == 1


@pytest.mark.parametrize(
    "mode",
    [
        "setup",
        "page_disclosed",
        "independent-status",
        "unknown",
        "remote-ack",
        "remote-bytes",
        "remote-effect",
    ],
)
def test_real_turn_rejects_model_success_after_upload_or_setup(tmp_path, mode):
    from test_ide_computer_use_host import _RecordingSurface, _ScriptedComputerUseClient

    from alysis_code.agent_loop import create_session
    from alysis_code.config import AppConfig
    from alysis_code.llm.types import LLMResponse, ToolCall

    calls = []

    def request(name, args):
        calls.append((name, args.copy()))
        if name == "computer_state":
            return state()
        if mode == "setup":
            return setup()
        result = upload("uncertain" if mode == "unknown" else "page_disclosed")
        if mode.startswith("remote-"):
            result["upload_result"]["upload"].update(
                receiverAcknowledged=True,
                remoteBytesVerified=mode != "remote-ack",
                requiresReconciliation=mode != "remote-effect",
            )
        return result

    sequence = [("computer_state", {}), ("computer_upload", {"element_id": "input"})]
    if mode == "independent-status" or mode.startswith("remote-"):
        sequence.append(("computer_upload_status", {"upload_id": "upload-1"}))
    responses = [
        LLMResponse(
            content="", tool_calls=[ToolCall(id=f"call-{i}", name=name, arguments=args)], raw={}
        )
        for i, (name, args) in enumerate(sequence)
    ]
    responses.append(LLMResponse(content="All remote uploads succeeded.", tool_calls=[], raw={}))
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
        computer_use_uploads_available=True,
    )
    session.client = _ScriptedComputerUseClient(responses)
    try:
        assert session.run_turn("Upload the file I select to this exact native input.") == 1
    finally:
        session.close()
    assert [name for name, _ in calls] == [name for name, _ in sequence]
    assert all("All remote uploads succeeded" not in text for text in surface.final_messages)
    assert any(
        "not complete" in text or "not independently verified" in text
        for text in surface.final_messages
    )
