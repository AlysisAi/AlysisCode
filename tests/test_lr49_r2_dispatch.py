"""Inert follow-up controls at shell syntax and real service dispatch boundaries."""

from types import SimpleNamespace

import pytest
import test_lr49_github_credential_gate as owner
from test_approval_review import _Surface

from alysis_code.agent.errors import ApprovalDeclinedError
from alysis_code.config import ConfigError
from alysis_code.durable_service_manager import DurableServiceManager
from alysis_code.sandbox_settings import ShellSandboxSettings

harness = owner.harness


@pytest.mark.parametrize(
    "command",
    [
        "# '\ngit push origin main\n# '",
        "# '\ngit pu\\\nsh origin main\n# '",
        "sh -ce 'git push origin main'",
        "bash -ec 'git push origin main'",
        "env LABEL=x sh -ce 'git pu\\\nsh origin main'",
        "sh -c \"sh -ce 'git push origin main'\"",
        "bash -c 'exec -a fixture git push origin HEAD:main'",
        "bash -c 'exec -a fixture git pu\\\nsh origin main'",
        "env LABEL=x bash -c 'exec -a fixture gh pr create --title fixture'",
        "if true; then git push origin main; fi",
        "while true; do git push origin main; break; done",
    ],
)
@pytest.mark.parametrize("mode", ["auto", "fullaccess"])
def test_unsupported_shell_grammar_cannot_bypass_person(harness, command, mode):
    build, dispatched, _, _ = harness
    surface = _Surface(allow=False)
    with pytest.raises(ApprovalDeclinedError):
        build(surface, mode, True)["shell_run"].run({"cmd": command})
    assert not dispatched
    assert surface.requests[-1].metadata["always_ask_person"]
    assert surface.requests[-1].command == command


@pytest.mark.parametrize(
    "stage", ["prepare", "launch", "metadata", "status", "legacy", "unchanged"]
)
@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_real_service_manager_binds_launch_and_repeated_readiness(
    tmp_path, monkeypatch, stage, replacement
):
    from alysis_code import durable_service_manager as module

    root = tmp_path / "project"
    root.mkdir()
    cwd = root / "child"
    cwd.mkdir()
    other = root / "other"
    other.mkdir()
    manager = DurableServiceManager(
        root=root, state_dir=tmp_path / "state", settings=ShellSandboxSettings(mode="off")
    )
    launches, probes = [], []
    swapped = False

    def swap():
        nonlocal swapped
        if swapped:
            return
        swapped = True
        cwd.rename(root / "original")
        if replacement == "symlink":
            cwd.symlink_to(other, target_is_directory=True)
        else:
            cwd.mkdir()

    def launch(**kwargs):
        if stage == "prepare":
            swap()
        return {"popen_args": "inert", "shell": True, "popen_cwd": cwd, "env": {}, "backend": "off"}

    def popen(*args, **kwargs):
        launches.append(kwargs)
        if stage == "launch":
            swap()
        return SimpleNamespace(pid=987654321)

    def run(*args, **kwargs):
        probes.append(kwargs)
        return SimpleNamespace(returncode=0)

    write = manager._write_metadata

    def write_metadata(metadata):
        write(metadata)
        if stage == "metadata":
            swap()

    monkeypatch.setattr(manager, "_build_launch", launch)
    monkeypatch.setattr(manager, "_write_metadata", write_metadata)
    monkeypatch.setattr(manager, "_metadata_process_alive", lambda metadata: (True, True))
    monkeypatch.setattr(manager, "_terminate_loaded_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_terminate_popen_tree", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_process_group_id", lambda pid: pid)
    monkeypatch.setattr(module, "_pid_start_token", lambda pid: "fixture")
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(module.subprocess, "run", run)
    try:
        started = manager.start(
            cmd="inert",
            cwd=cwd,
            readiness={"type": "command", "command": "git push origin main", "timeout_s": 0.01},
        )
    except ConfigError:
        assert stage == "prepare"
    else:
        if stage in {"status", "legacy", "unchanged"}:
            assert probes
            probes.clear()
            if stage == "status":
                swap()
            elif stage == "legacy":
                metadata = manager._read_metadata(started.service_id)
                metadata.pop("execution_directory_identity", None)
                manager._write_metadata(metadata)
            status = manager.status(started.service_id, timeout_s=0)
            assert status["readiness"]["status"] == ("ready" if stage == "unchanged" else "failed")
    if stage == "prepare":
        assert not launches
    if stage != "unchanged":
        assert not probes
    else:
        assert probes and probes[-1]["cwd"] == str(cwd)


def test_wrapper_options_do_not_guess_argument_arity():
    from alysis_code.command_review_rules import github_write_reason

    # The real tool separately blocks sudo before reaching this gate.
    assert github_write_reason("sudo --chdir fixture git push origin main", dialect="posix")
