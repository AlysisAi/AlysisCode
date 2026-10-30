"""GitHub write approvals for shell continuations and execution directories."""

import json
import subprocess

import pytest
import test_lr49_github_credential_gate as fixture_owner
from test_approval_review import _Surface

from alysis_code.agent.errors import AgentRuntimeError, ApprovalDeclinedError
from alysis_code.command_review_rules import github_write_reason
from alysis_code.config import ConfigError
from alysis_code.ide.approval_binding import build_approval_binding
from alysis_code.surface import ApprovalDecision

# Share the build_tools fixture used by the credential-gate tests.
harness = fixture_owner.harness


CONTINUATIONS = [
    "git pu\\\nsh origin HEAD:main",
    "git p\\\nush --force-with-lease origin HEAD:main",
    "sh -c 'git pu\\\nsh origin HEAD:main'",
]


@pytest.mark.parametrize("command", CONTINUATIONS)
def test_posix_continuation_calls_only_inert_git_function(command):
    # Verify the shell semantics without executing Git, using a fixed inert function.
    source = 'git() { printf "ARG=<%s>\\n" "$@"; }; ' + command
    if command.startswith("sh -c "):
        source = 'git() { printf "ARG=<%s>\\n" "$@"; }; ' + command[len("sh -c '") : -1]
    p = subprocess.run(["/bin/sh", "-c", source], text=True, capture_output=True, timeout=3)
    assert p.returncode == 0 and p.stdout.splitlines()[0] == "ARG=<push>", p


@pytest.mark.parametrize("command", CONTINUATIONS)
def test_known_push_continuation_needs_independent_write_gate(command):
    assert github_write_reason(command, dialect="posix") is not None


@pytest.mark.parametrize("command", CONTINUATIONS)
@pytest.mark.parametrize("mode,yes", [("auto", True), ("review", True), ("fullaccess", True)])
def test_continued_push_denied_person_zero_dispatch(harness, command, mode, yes):
    build, dispatched, client, root = harness
    surface = _Surface(allow=False)
    error = None
    try:
        build(surface, mode, yes)["shell_run"].run({"cmd": command})
    except (ApprovalDeclinedError, AgentRuntimeError, ConfigError) as e:
        error = e
    print(
        json.dumps(
            {
                "case": "continued-push",
                "mode": mode,
                "command": command,
                "cards": [(x.kind, x.metadata) for x in surface.requests],
                "dispatched": dispatched,
                "error": type(error).__name__ if error else None,
            }
        )
    )
    assert not dispatched, (
        "a continued known push reached captured dispatch without person approval"
    )


@pytest.mark.parametrize("change", ["symlink", "directory-replacement"])
@pytest.mark.parametrize("mode,yes", [("auto", True), ("review", True), ("fullaccess", True)])
def test_independent_write_card_binds_original_cwd_without_auth(
    harness, monkeypatch, change, mode, yes
):
    build, _, client, root = harness
    a = root / "approved"
    a.mkdir()
    b = root / "replacement"
    b.mkdir()
    observed = []
    bindings = []

    def capture(**kw):
        observed.append(
            {
                "command": kw["cmd"],
                "cwd": kw["cwd"],
                "resolved": str((root / (kw["cwd"] or ".")).resolve()),
            }
        )
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr("alysis_code.agent_loop.shell_run", capture)

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            if request.kind == "shell_run" and request.metadata.get("always_ask_person"):
                bindings.append(
                    build_approval_binding(
                        request, project_root=root, runtime_fingerprint="inert", mode=mode
                    ).action
                )
                a.rename(root / "retired-original")
                if change == "symlink":
                    a.symlink_to(b, target_is_directory=True)
                else:
                    a.mkdir()
            return ApprovalDecision(True)

    surface = Surface()
    args = {"cmd": "git push origin HEAD:main", "cwd": "approved"}
    if mode != "fullaccess":
        args["permissions"] = {"network": True, "git_metadata": True}
    error = None
    try:
        build(surface, mode, yes)["shell_run"].run(args)
    except (ApprovalDeclinedError, AgentRuntimeError, ConfigError) as e:
        error = e
    print(
        json.dumps(
            {
                "case": "cwd",
                "mode": mode,
                "change": change,
                "binding": bindings,
                "observed": observed,
                "error": type(error).__name__ if error else None,
            }
        )
    )
    assert len(bindings) == 1 and bindings[0]["cwd"] == str(a)
    assert not observed, "approved cwd identity changed but the remote command reached dispatch"
