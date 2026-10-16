from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_computer_use_upload_objectives import binding, projection_cases, proof

from alysis_code.agent.computer_use_upload_objectives import (
    validate_upload_objective_binding,
    validate_upload_objective_proof,
    validate_upload_objective_result,
)


def test_actual_typescript_and_python_closed_objective_contract_agree():
    source = os.environ.get("ALYSIS_APP_SOURCE") or os.environ.get("ALYSIS_APP_SOURCE")
    node = (
        os.environ.get("ALYSIS_TEST_NODE") or os.environ.get("ALYSIS_TEST_NODE")
    ) or shutil.which("node")
    if not source or not node:
        pytest.skip("Requires actual matching app checkout and Node")
    app = Path(source).resolve()
    results = projection_cases()
    bindings = [
        binding(),
        None,
        {},
        {**binding(), "version": 1.0},
        {**binding(), "version": True},
        {**binding(), "extra": None},
    ]
    proofs = [proof()["verification"]["upload_objective"], None, {}, binding()]
    for value in results:
        if isinstance(value, dict) and isinstance(value.get("verification"), dict):
            proofs.append(value["verification"].get("upload_objective"))
    script = """
      import { readFileSync } from 'node:fs';
      import { pathToFileURL } from 'node:url';
      const { validateComputerUseUploadObjectiveBinding: b, validateComputerUseUploadObjectiveProof: p,
        validateComputerUseUploadObjectiveToolResult: r } = await import(pathToFileURL(process.argv[1] + '/shared/computerUseUploadObjective.ts'));
      const v = JSON.parse(readFileSync(0, 'utf8'));
      process.stdout.write(JSON.stringify({bindings:v.bindings.map(b),proofs:v.proofs.map(p),results:v.results.map(r)}));
    """
    completed = subprocess.run(
        [node, "--experimental-strip-types", "--input-type=module", "-e", script, str(app)],
        input=json.dumps({"bindings": bindings, "proofs": proofs, "results": results}),
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert json.loads(completed.stdout) == {
        "bindings": list(map(validate_upload_objective_binding, bindings)),
        "proofs": list(map(validate_upload_objective_proof, proofs)),
        "results": list(map(validate_upload_objective_result, results)),
    }


def test_actual_app_host_parser_matches_new_optional_objective_binding():
    from test_computer_use_upload_objectives import host_binding

    from alysis_code.ide.protocol import ProtocolError
    from alysis_code.ide.stdio_bridge import _computer_use_host_param

    source = os.environ.get("ALYSIS_APP_SOURCE") or os.environ.get("ALYSIS_APP_SOURCE")
    node = (
        os.environ.get("ALYSIS_TEST_NODE") or os.environ.get("ALYSIS_TEST_NODE")
    ) or shutil.which("node")
    if not source or not node:
        pytest.skip("Requires actual matching app checkout and Node")
    app = Path(source).resolve()
    scopes = [
        binding(),
        None,
        {},
        {**binding(), "version": True},
        {**binding(), "version": 1.0},
        {**binding(), "extra": 1},
    ]
    values = [host_binding().to_protocol(), host_binding(upload_objective=binding()).to_protocol()]
    for scope in scopes:
        values.append({**host_binding().to_protocol(), "upload_objective": scope})
    for missing in ["computer_verify", "computer_upload"]:
        v = host_binding(upload_objective=binding()).to_protocol()
        v["tools"] = [
            name
            for name in v["tools"]
            if name != missing
            and not (missing == "computer_upload" and name == "computer_upload_status")
        ]
        values.append(v)

    def admitted(v):
        try:
            parsed = _computer_use_host_param({"computer_use_host": v}, request_id="r")
            return parsed.to_protocol().get("upload_objective") or "absent"
        except ProtocolError:
            return "refused"

    script = """
      import { readFileSync } from 'node:fs'; import { pathToFileURL } from 'node:url';
      const { parseIdeComputerUseHostBinding } = await import(pathToFileURL(process.argv[1] + '/electron/runtime/ideBridgeProtocol.ts'));
      process.stdout.write(JSON.stringify(JSON.parse(readFileSync(0,'utf8')).map(v=>{try{return parseIdeComputerUseHostBinding(v).uploadObjective ?? 'absent';}catch{return 'refused';}})));
    """
    result = subprocess.run(
        [
            node,
            "--experimental-strip-types",
            "--experimental-loader",
            str(app / "tests/ts-extension-loader.mjs"),
            "--input-type=module",
            "-e",
            script,
            str(app),
        ],
        input=json.dumps(values),
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert json.loads(result.stdout) == list(map(admitted, values))


@pytest.mark.parametrize("other_file", [False, True])
def test_actual_budgeted_app_controller_result_enters_real_agent_evaluate(tmp_path, other_file):
    """Actual controller/renewal/read accounting; its terminal main port is controlled.

    This exercises the real result envelope, not source admission or a new upload.
    Expected scope is fixed by the fixture setup, never learned from the result.
    """
    from test_computer_use_files import artifact
    from test_computer_use_uploads import state, upload
    from test_ide_computer_use_host import _RecordingSurface, _ScriptedComputerUseClient

    from alysis_code.agent_loop import create_session
    from alysis_code.config import AppConfig
    from alysis_code.llm.types import LLMResponse, ToolCall

    source = os.environ.get("ALYSIS_APP_SOURCE") or os.environ.get("ALYSIS_APP_SOURCE")
    node = (
        os.environ.get("ALYSIS_TEST_NODE") or os.environ.get("ALYSIS_TEST_NODE")
    ) or shutil.which("node")
    if not source or not node:
        pytest.skip("Requires actual matching app checkout and Node")
    app = Path(source).resolve()
    output = tmp_path / "actual-controller-result.json"
    run = subprocess.run(
        [
            node,
            "--experimental-strip-types",
            "--test",
            "--test-name-pattern=actual budgeted controller preserves",
            "tests/computer-use-upload-objective-host.test.mjs",
        ],
        cwd=app,
        env={**os.environ, "ALYSIS_UPLOAD_OBJECTIVE_RESULT_OUT": str(output)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    actual = json.loads(output.read_text())
    assert validate_upload_objective_result(actual)
    expected = {
        "version": 1,
        "objective_digest": "sha256:" + "a" * 64,
        "task_scope_digest": "sha256:" + "b" * 64,
    }
    calls = []

    def request(name, args):
        calls.append(name)
        if name == "computer_state":
            return state()
        if name == "computer_upload":
            return upload(identifier="upload_original")
        if name == "computer_download":
            return artifact(False)
        assert name == "computer_verify" and args == {"operation": "evaluate"}
        return actual

    sequence = [("computer_state", {}), ("computer_upload", {"element_id": "input"})]
    if other_file:
        sequence += [("computer_state", {}), ("computer_download", {"element_id": "link"})]
    sequence += [("computer_verify", {"operation": "evaluate"})]
    responses = [
        LLMResponse(content="", tool_calls=[ToolCall(id=f"call-{i}", name=n, arguments=a)], raw={})
        for i, (n, a) in enumerate(sequence)
    ]
    responses.append(
        LLMResponse(content="The original selected upload is verified.", tool_calls=[], raw={})
    )
    surface = _RecordingSurface()
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=10,
        no_log=True,
        api_key_override="test-key",
        one_shot_execution=True,
        surface=surface,
        sensitive_media=True,
        computer_use_host_request=request,
        computer_use_response_mode="host_sanitized_text",
        computer_use_uploads_available=True,
        computer_use_files_available=True,
        computer_use_verification_available=True,
        computer_use_upload_objective=expected,
    )
    session.client = _ScriptedComputerUseClient(responses)
    try:
        assert session.run_turn("Upload my selected file into the approved folder.") == (
            1 if other_file else 0
        )
    finally:
        session.close()
    assert calls == [n for n, _ in sequence]
    assert (
        any("The original selected upload is verified." in text for text in surface.final_messages)
        is not other_file
    )
