"""Service approval cards identify the directory used for execution."""

import json
from types import SimpleNamespace

import pytest
import test_lr49_github_credential_gate as fixture_owner
import test_lr49_github_credential_gate as owner
from test_approval_review import _Surface

from alysis_code.ide.approval_binding import build_approval_binding
from alysis_code.surface import ApprovalDecision

# Share the build_tools fixture used by the credential-gate tests.
harness = fixture_owner.harness


@pytest.mark.parametrize("readiness", [False, True])
def test_service_write_card_names_actual_execution_directory(harness, monkeypatch, readiness):
    build, _, _, root = harness
    child = root / "child-repository"
    child.mkdir()
    calls = []
    bindings = []

    class Manager:
        def start(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(payload={"status": "started"})

    original = owner.build_tools
    monkeypatch.setattr(
        owner, "build_tools", lambda **kw: original(**kw, durable_service_manager=Manager())
    )

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            if request.metadata.get("always_ask_person"):
                bindings.append(
                    build_approval_binding(
                        request, project_root=root, runtime_fingerprint="inert", mode="fullaccess"
                    ).action
                )
            return ApprovalDecision(True)

    args = {
        "cmd": "echo inert-service" if readiness else "git push origin HEAD:main",
        "cwd": "child-repository",
    }
    if readiness:
        args["readiness"] = {"type": "command", "command": "git push origin HEAD:main"}
    build(Surface(), "fullaccess", True)["shell_service_start"].run(args)
    print(
        json.dumps(
            {
                "readiness": readiness,
                "binding": bindings,
                "dispatch_cwd": [str(x["cwd"]) for x in calls],
            }
        )
    )
    assert len(calls) == 1 and len(bindings) == 1
    assert bindings[0]["cwd"] == str(calls[0]["cwd"]), (
        "write card described project root but the service executes in a different repository"
    )
