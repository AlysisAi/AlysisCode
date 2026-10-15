from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest
from test_computer_use_files import artifact
from test_computer_use_uploads import state, upload

from alysis_code.agent.computer_use_files import computer_use_file_delivery_status
from alysis_code.agent.computer_use_tools import build_computer_use_tools
from alysis_code.ide.health import capabilities_payload
from alysis_code.ide.protocol import ProtocolError
from alysis_code.ide.stdio_bridge import ComputerUseHostBinding, _computer_use_host_param


def binding():
    return {
        "version": 1,
        "objective_digest": "sha256:" + "c" * 64,
        "task_scope_digest": "sha256:" + "d" * 64,
    }


def proof(identifier="upload-1", result_id="result-1"):
    return {
        "verification": {
            "version": 1,
            "status": "verified",
            "reason": None,
            "evidence_scope": "selected_file_upload_readback",
            "upload_objective": {
                **binding(),
                "upload_id": identifier,
                "result_id": result_id,
                "result_digest": "sha256:" + "e" * 64,
            },
        }
    }


def host_binding(**kwargs):
    return ComputerUseHostBinding(
        operation_id="operation-1",
        broker_fingerprint="sha256:" + "a" * 64,
        authority_epoch=7,
        scope_digest="sha256:" + "b" * 64,
        verification_available=True,
        uploads_available=True,
        **kwargs,
    )


def projection_cases():
    cases = [proof(), None, [], {}, {"verification": None}]
    for path in [(), ("verification",), ("verification", "upload_objective")]:
        current = proof()
        for key in path:
            current = current[key]
        for key in current:
            item = proof()
            target = item
            for part in path:
                target = target[part]
            del target[key]
            cases.append(item)
        item = proof()
        target = item
        for part in path:
            target = target[part]
        target["extra"] = True
        cases.append(item)
    for key, values in {
        "version": [True, False, 1.0, 2, None, "1"],
        "objective_digest": ["sha256:" + "C" * 64, "c" * 64, None, [], 1],
        "task_scope_digest": ["sha256:" + "d" * 63, None, {}],
        "upload_id": ["upload-1-suffix", "x" * 128, "x" * 129, "_a", "a/b", " a", "a\n", None],
        "result_id": ["a-b_9", "x" * 129, "../x", "a\n", None],
        "result_digest": ["sha256:" + "0" * 64, "sha256:" + "g" * 64, None],
    }.items():
        for value in values:
            item = proof()
            item["verification"]["upload_objective"][key] = value
            cases.append(item)
    for key, value in [
        ("version", True),
        ("version", 1.0),
        ("status", "accepted"),
        ("reason", "ok"),
        ("evidence_scope", "records"),
    ]:
        item = proof()
        item["verification"][key] = value
        cases.append(item)
    cases.append({**proof(), "alysis_workflow": {}})
    return cases


def test_host_scope_is_explicit_immutable_echoed_and_independently_negotiated():
    assert (
        capabilities_payload()["features"]["computer_use_host"]["upload_objective_evidence_version"]
        == 1
    )
    original = binding()
    actual = host_binding(upload_objective=original)
    raw = actual.to_protocol()
    assert raw["upload_objective"] == original
    assert raw["scope_digest"] != original["task_scope_digest"]
    original["objective_digest"] = "sha256:" + "f" * 64
    raw["upload_objective"]["task_scope_digest"] = "sha256:" + "f" * 64
    assert actual.to_protocol()["upload_objective"] == binding()
    assert (
        _computer_use_host_param({"computer_use_host": actual.to_protocol()}, request_id="r")
        == actual
    )
    with pytest.raises(FrozenInstanceError):
        actual.upload_objective.objective_digest = "changed"
    assert "upload_objective" not in host_binding().to_protocol()
    for value in [None, {}, {**binding(), "extra": True}, {**binding(), "version": True}]:
        with pytest.raises(ProtocolError):
            _computer_use_host_param(
                {"computer_use_host": {**actual.to_protocol(), "upload_objective": value}},
                request_id="r",
            )
    for missing in ["computer_upload", "computer_verify"]:
        value = actual.to_protocol()
        value["tools"] = [
            name
            for name in value["tools"]
            if name != missing
            and not (missing == "computer_upload" and name == "computer_upload_status")
        ]
        with pytest.raises(ProtocolError):
            _computer_use_host_param({"computer_use_host": value}, request_id="r")
    with pytest.raises(ValueError):
        build_computer_use_tools(lambda *_: {}, upload_objective=binding())


def tools_with(response, *, objective=True):
    return build_computer_use_tools(
        lambda name, args: state() if name == "computer_state" else deepcopy(response[0]),
        verification_available=True,
        uploads_available=True,
        files_available=True,
        upload_objective=binding() if objective else None,
    )


def start(tools, response, identifier="upload-1"):
    tools["computer_state"].run({})
    response[0] = upload(identifier=identifier)
    assert "error" not in tools["computer_upload"].run({"element_id": "input"})


@pytest.mark.parametrize(
    "mode",
    [
        "missing-config",
        "wrong-id",
        "prefix-id",
        "wrong-objective",
        "wrong-scope",
        "extra",
        "workflow",
        "propose",
        "status-only",
        "status-json",
        "model-argument",
        "other-session",
    ],
)
def test_only_actual_evaluate_for_exact_started_upload_and_initial_scope_can_clear(mode):
    response = [upload()]
    tools = tools_with(response, objective=mode != "missing-config")
    if mode == "status-only":
        tools["computer_upload_status"].run({"upload_id": "upload-1"})
    else:
        start(tools, response)
    result = proof()
    p = result["verification"]["upload_objective"]
    if mode == "wrong-id":
        p["upload_id"] = "other"
    if mode == "prefix-id":
        p["upload_id"] += "-suffix"
    if mode == "wrong-objective":
        p["objective_digest"] = "sha256:" + "f" * 64
    if mode == "wrong-scope":
        p["task_scope_digest"] = "sha256:" + "b" * 64
    if mode == "extra":
        p["complete"] = True
    if mode == "workflow":
        result["alysis_workflow"] = {}
    response[0] = result
    name, args = "computer_verify", {"operation": "evaluate"}
    if mode == "propose":
        args = {"operation": "propose", "predicate": {"kind": "form"}}
    if mode == "status-json":
        name, args = "computer_upload_status", {"upload_id": "upload-1"}
    if mode == "model-argument":
        args["upload_objective"] = p
    if mode == "other-session":
        foreign = tools_with(response)
        assert "error" in foreign[name].run(args)
    else:
        assert "error" in tools[name].run(args)
    assert not computer_use_file_delivery_status(tools)["complete"]


def test_verified_result_is_one_use_only_and_preserves_each_other_aggregate_barrier():
    response = [upload()]
    tools = tools_with(response)
    start(tools, response)
    start(tools, response, "upload-2")
    response[0] = proof()
    assert tools["computer_verify"].run({"operation": "evaluate"}) == proof()
    assert computer_use_file_delivery_status(tools)["pending_count"] == 1
    response[0] = proof("upload-2")
    assert "error" in tools["computer_verify"].run({"operation": "evaluate"}), (
        "same result ID cannot clear a different upload"
    )
    response[0] = proof("upload-2", "result-2")
    assert "error" not in tools["computer_verify"].run({"operation": "evaluate"})
    assert computer_use_file_delivery_status(tools)["complete"]
    response[0] = upload(identifier="upload-2")
    tools["computer_upload_status"].run({"upload_id": "upload-2"})
    assert not computer_use_file_delivery_status(tools)["complete"], (
        "status never establishes an objective"
    )
    response[0] = proof("upload-2", "result-2")
    assert "error" in tools["computer_verify"].run({"operation": "evaluate"})


@pytest.mark.parametrize("other", ["unknown", "download", "status-only"])
def test_genuine_upload_proof_never_resolves_unrelated_file_state(other):
    response = [upload()]
    tools = tools_with(response)
    if other == "unknown":
        tools["computer_state"].run({})
        response[0] = {"error": "lost_start"}
        tools["computer_upload"].run({"element_id": "input"})
    elif other == "download":
        tools["computer_state"].run({})
        response[0] = artifact(False)
        tools["computer_download"].run({"element_id": "link"})
    else:
        response[0] = upload(identifier="other")
        tools["computer_upload_status"].run({"upload_id": "other"})
    start(tools, response)
    response[0] = proof()
    assert "error" not in tools["computer_verify"].run({"operation": "evaluate"})
    status = computer_use_file_delivery_status(tools)
    assert not status["complete"]
    assert status["uncertain"] if other == "unknown" else status["pending_count"] == 1


@pytest.mark.parametrize(
    "mode",
    [
        "verified",
        "wrong-id",
        "wrong-scope",
        "no-config",
        "model-text",
        "two-uploads",
        "pending-download",
        "lost-start",
        "compound-no-admission",
        "no-upload",
        "generic-verification",
    ],
)
def test_real_agent_turn_uses_only_actual_evaluate_objective_result(tmp_path, mode):
    from test_ide_computer_use_host import _RecordingSurface, _ScriptedComputerUseClient

    from alysis_code.agent_loop import create_session
    from alysis_code.config import AppConfig
    from alysis_code.llm.types import LLMResponse, ToolCall

    calls = []
    upload_count = 0

    def request(name, args):
        nonlocal upload_count
        calls.append((name, args.copy()))
        if name == "computer_state":
            return state()
        if name == "computer_upload":
            upload_count += 1
            if mode == "lost-start" and upload_count == 1:
                return {"error": "lost_start"}
            return upload(identifier=f"upload-{upload_count}")
        if name == "computer_download":
            return artifact(False)
        if mode == "generic-verification":
            return {
                "verification": {
                    "version": 1,
                    "status": "verified",
                    "reason": None,
                    "evidence_scope": "records",
                }
            }
        result = proof(
            "other" if mode == "wrong-id" else ("upload-2" if mode == "lost-start" else "upload-1")
        )
        if mode == "wrong-scope":
            result["verification"]["upload_objective"]["task_scope_digest"] = "sha256:" + "f" * 64
        return result

    sequence = [("computer_state", {}), ("computer_upload", {"element_id": "input"})]
    if mode in {"no-upload", "generic-verification"}:
        sequence = [("computer_state", {})]
    if mode in {"two-uploads", "lost-start"}:
        sequence += [("computer_state", {}), ("computer_upload", {"element_id": "input"})]
    if mode == "pending-download":
        sequence += [("computer_state", {}), ("computer_download", {"element_id": "link"})]
    if mode not in {"model-text", "no-upload"}:
        sequence += [("computer_verify", {"operation": "evaluate"})]
    final = "The selected file is verified in the approved folder."
    responses = [
        LLMResponse(content="", tool_calls=[ToolCall(id=f"call-{i}", name=n, arguments=a)], raw={})
        for i, (n, a) in enumerate(sequence)
    ]
    responses += [
        LLMResponse(
            content=final + (str(proof()) if mode == "model-text" else ""), tool_calls=[], raw={}
        )
    ]
    surface = _RecordingSurface()
    initial = None if mode in {"no-config", "compound-no-admission"} else binding()
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=12,
        no_log=True,
        api_key_override="test-key",
        one_shot_execution=True,
        surface=surface,
        sensitive_media=True,
        computer_use_host_request=request,
        computer_use_uploads_available=True,
        computer_use_response_mode="host_sanitized_text",
        computer_use_files_available=True,
        computer_use_verification_available=True,
        computer_use_upload_objective=initial,
    )
    if initial is not None:
        initial["task_scope_digest"] = "sha256:" + "f" * 64
    session.client = _ScriptedComputerUseClient(responses)
    try:
        rc = session.run_turn(
            "Upload the selected file to this folder."
            if mode != "compound-no-admission"
            else "Upload the file and delete the old project."
        )
    finally:
        session.close()
    assert rc == (0 if mode == "verified" else 1)
    assert [name for name, _ in calls] == [
        name for name, _ in (sequence[:2] if mode == "lost-start" else sequence)
    ]
    assert any(final in text for text in surface.final_messages) == (mode == "verified")


def test_initial_objective_is_pending_but_legacy_empty_file_state_remains_complete():
    response = [upload()]
    assert computer_use_file_delivery_status(tools_with(response, objective=False))["complete"]
    actual = computer_use_file_delivery_status(tools_with(response))
    assert not actual["complete"]
    assert actual["pending_count"] == 0, "No invented upload ID or count before dispatch"
    assert not actual["uncertain"]
