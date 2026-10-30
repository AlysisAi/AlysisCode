"""Verification commands remain bound to their original execution directory."""

import json
import subprocess

import pytest
import test_lr49_github_credential_gate as owner
from test_approval_review import _Surface

from alysis_code.config import AppConfig
from alysis_code.surface import ApprovalDecision

harness = owner.harness


@pytest.mark.parametrize("replace_root", [False, True])
def test_original_cwd_binding_reaches_each_verification_command(
    harness, monkeypatch, replace_root, record_property
):
    build, _, _, root = harness
    commands = ["pytest -q tests/test_a.py", "pytest -q tests/test_b.py"]
    cfg = AppConfig(model="inert", verify_commands=commands)
    cfg.extra_fields = {"verify_sandbox": {"mode": "off"}}
    original = owner.build_tools
    monkeypatch.setattr(owner, "build_tools", lambda **kw: original(**kw, cfg=cfg))
    (root / "tests").mkdir()
    for name in ["test_a.py", "test_b.py"]:
        (root / "tests" / name).write_text("def test_inert(): pass\n")
    original_inode = root.stat().st_ino
    cards = []

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            if request.metadata.get("always_ask_person"):
                cards.append(
                    {
                        "command": request.command,
                        "cwd": request.metadata.get("cwd"),
                        "inode": root.stat().st_ino,
                    }
                )
            return ApprovalDecision(True)

    surface = Surface()
    calls = []

    def capture(self, *, root: object, cwd, cmd, **kw):
        path = __import__("pathlib").Path(cwd)
        calls.append({"cmd": cmd, "cwd": str(path), "inode": path.stat().st_ino})
        if replace_root and len(calls) == 1:
            path.rename(path.parent / "retired-original")
            path.mkdir()
            (path / "tests").mkdir()
            for name in ["test_a.py", "test_b.py"]:
                (path / "tests" / name).write_text("def test_inert(): pass\n")
        return subprocess.CompletedProcess(cmd, 0, "1 passed in 0.01s", "")

    monkeypatch.setattr("alysis_code.sandbox_runner.HostShellRunner.run", capture)
    error = None
    try:
        build(surface, "fullaccess", True, verification=True)["verify_run"].run({})
    except Exception as e:
        error = type(e).__name__ + ": " + str(e)
    record_property(
        "admission_evidence",
        json.dumps(
            {
                "replace": replace_root,
                "original_inode": original_inode,
                "cards": cards,
                "calls": calls,
                "error": error,
            }
        ),
    )
    assert len(cards) == 2, cards
    assert all(card["inode"] == original_inode for card in cards)
    if replace_root:
        assert len(calls) == 1, (
            "second verification command dispatched after original cwd inode replacement: "
            + json.dumps(calls)
        )
    else:
        assert len(calls) == 2, calls
        assert all(c["inode"] == original_inode for c in calls)
