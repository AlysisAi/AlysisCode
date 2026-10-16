from __future__ import annotations

import json
import os
import shutil
import subprocess
from copy import deepcopy
from itertools import product
from pathlib import Path

import pytest
from test_computer_use_uploads import projection_cases, remote_projection_cases, upload

from alysis_code.agent.computer_use_uploads import validate_upload_result
from alysis_code.ide.stdio_bridge import ComputerUseHostBinding


def test_actual_typescript_and_python_upload_projection_and_eight_host_toolsets_agree():
    """Run with ALYSIS_APP_SOURCE pointing to the matching app checkout."""
    source = os.environ.get("ALYSIS_APP_SOURCE") or os.environ.get("ALYSIS_APP_SOURCE")
    node = (
        os.environ.get("ALYSIS_TEST_NODE") or os.environ.get("ALYSIS_TEST_NODE")
    ) or shutil.which("node")
    if not source or not node:
        pytest.skip("Cross-repository contract check requires ALYSIS_APP_SOURCE and Node")
    app = Path(source).resolve()
    cases = projection_cases() + remote_projection_cases()
    for key, value in [
        ("byteLength", 12.0),
        ("exportedBytes", 12.0),
        ("byteLength", 12.5),
        ("exportedBytes", "12"),
        ("receiverAcknowledged", 0),
    ]:
        item = upload()
        item["upload_result"]["upload"][key] = value
        cases.append(item)
    integral_version = deepcopy(upload())
    integral_version["upload_result"]["version"] = 1.0
    cases.append(integral_version)
    combinations = list(product([False, True], repeat=3))
    bindings = [
        ComputerUseHostBinding(
            operation_id="op",
            broker_fingerprint="sha256:" + "a" * 64,
            authority_epoch=1,
            scope_digest="sha256:" + "b" * 64,
            verification_available=verify,
            files_available=files,
            uploads_available=uploads,
        ).to_protocol()
        for verify, files, uploads in combinations
    ]
    script = """
      import { readFileSync } from 'node:fs';
      import { pathToFileURL } from 'node:url';
      const app = process.argv[1];
      const { isComputerUseUploadToolResult } = await import(pathToFileURL(app + '/shared/computerUseUploadTools.ts'));
      const { computerUseToolSet, parseIdeComputerUseHostBinding } = await import(pathToFileURL(app + '/electron/runtime/ideBridgeProtocol.ts'));
      const input = JSON.parse(readFileSync(0, 'utf8'));
      process.stdout.write(JSON.stringify({valid: input.cases.map(isComputerUseUploadToolResult),
        parsed: input.bindings.map(value => [...parseIdeComputerUseHostBinding(value).tools]),
        offered: input.combinations.map(flags => [...computerUseToolSet(...flags)])}));
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
        input=json.dumps({"cases": cases, "bindings": bindings, "combinations": combinations}),
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    actual = json.loads(result.stdout)
    assert actual["valid"] == [validate_upload_result(value) for value in cases]
    assert actual["parsed"] == actual["offered"] == [binding["tools"] for binding in bindings]
