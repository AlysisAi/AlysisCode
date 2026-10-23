from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import alysis_code.forge_host_verification as verification_mod
from alysis_code.execution_deadline import ExecutionDeadline
from alysis_code.forge_host_protocol import VerificationCommand
from alysis_code.forge_host_verification import (
    HostVerificationError,
    capture_pytest_executable_identity,
    run_host_verification,
)
from alysis_code.process_reaping import (
    ProcessGroupRegistry,
    process_group_exists,
)


def _write_executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _fake_pytest(tmp_path: Path, body: str = "#!/bin/sh\necho passed\nexit 0\n") -> Path:
    return _write_executable(tmp_path / "runtime" / "pytest", body)


def _identity(root: Path, executable: Path):  # type: ignore[no-untyped-def]
    return capture_pytest_executable_identity(executable, workspace_root=root)


def _command(
    argv: tuple[str, ...] = ("pytest", "-q"),
    *,
    command_id: str = "cmd_pytest",
    validated: bool = True,
    provenance: str = "PREEXISTING_REPO_NATIVE",
) -> VerificationCommand:
    text = " ".join(argv)
    values = {
        "command_id": command_id,
        "task_id": "T01",
        "scope_id": "scope.T01",
        "original_text": text,
        "display_text": text,
        "execution_mode": "ARGV",
        "provenance": provenance,
        "trust_level": "TRUSTED",
        "requirement": "REQUIRED",
        "timeout_policy": "session_verification_timeout",
        "requirement_ids": ["T01:criterion:1"],
        "validation_status": "VALID",
        "rejection_reason": "",
        "evidentiary_capability": "ASSERTIVE",
        "capability_reason": "recognized_assertive_verification_command",
        "argv": list(argv),
    }
    if validated:
        return VerificationCommand.model_validate(values)
    return VerificationCommand.model_construct(**values)


def _run(
    *,
    root: Path,
    trusted_pytest: Path,
    deadline: ExecutionDeadline | None = None,
    argv: tuple[str, ...] = ("pytest", "-q"),
    provenance: str = "PREEXISTING_REPO_NATIVE",
):
    return run_host_verification(
        "cmd_pytest",
        admitted_command=_command(
            argv, validated=argv in {("pytest",), ("pytest", "-q")}, provenance=provenance
        ),
        workspace_root=root,
        pinned_cwd=root,
        artifact_path=root / ".alysis" / "verify" / "pytest.json",
        execution_deadline=deadline or ExecutionDeadline.from_duration(30.0),
        pytest_executable=capture_pytest_executable_identity(
            trusted_pytest,
            workspace_root=root,
        ),
    )


@pytest.mark.parametrize("provenance", ["PREEXISTING_REPO_NATIVE", "HOST_AUTHORITATIVE"])
def test_runs_real_repo_pytest_and_binds_retained_artifact(tmp_path: Path, provenance: str) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "test_example.py").write_text(
        "def test_example():\n    assert 2 + 2 == 4\n",
        encoding="utf-8",
    )
    trusted = Path(sys.executable).parent / "pytest"
    if not trusted.exists():
        pytest.skip("the test runtime has no pinned pytest entrypoint")

    result = _run(root=root, trusted_pytest=trusted, provenance=provenance)

    assert result.passed is True
    assert result.status == "PASSED"
    assert result.argv == ("pytest", "-q")
    assert result.real_execution is True
    assert result.exit_code == 0
    assert result.timed_out is False
    data = result.artifact.path.read_bytes()
    assert result.artifact.digest == hashlib.sha256(data).hexdigest()
    assert result.artifact.size_bytes == len(data)
    payload = json.loads(data)
    assert payload["argv"] == ["pytest", "-q"]
    assert payload["status"] == "PASSED"
    assert payload["realExecution"] is True


def test_real_pytest_leaves_workspace_unchanged_outside_retained_runtime_artifact(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "test_example.py").write_text(
        "def test_example():\n    assert True\n",
        encoding="utf-8",
    )
    trusted = Path(sys.executable).parent / "pytest"
    if not trusted.exists():
        pytest.skip("the test runtime has no pinned pytest entrypoint")

    def snapshot_non_runtime() -> dict[str, tuple[str, str]]:
        snapshot: dict[str, tuple[str, str]] = {}
        for candidate in root.rglob("*"):
            relative = candidate.relative_to(root)
            if relative.parts and relative.parts[0] == ".alysis":
                continue
            if candidate.is_symlink():
                snapshot[relative.as_posix()] = ("symlink", os.readlink(candidate))
            elif candidate.is_dir():
                snapshot[relative.as_posix()] = ("directory", "")
            elif candidate.is_file():
                snapshot[relative.as_posix()] = (
                    "file",
                    hashlib.sha256(candidate.read_bytes()).hexdigest(),
                )
        return snapshot

    before = snapshot_non_runtime()
    result = _run(root=root, trusted_pytest=trusted)
    after = snapshot_non_runtime()

    assert result.passed is True
    assert after == before
    assert not (root / ".pytest_cache").exists()
    assert result.artifact.path.is_relative_to(root / ".alysis" / "verify")


@pytest.mark.parametrize(
    "argv",
    [
        (),
        ("pytest", "-x"),
        ("pytest", "tests/test_one.py"),
        ("python", "-m", "pytest"),
        ("pytest -q",),
    ],
)
def test_rejects_every_noncanonical_argv_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: tuple[str, ...],
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    trusted = _fake_pytest(tmp_path)
    monkeypatch.setattr(
        verification_mod,
        "run_in_tracked_process_group",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not execute")),
    )

    with pytest.raises(HostVerificationError, match="not canonical|not admitted"):
        _run(root=root, trusted_pytest=trusted, argv=argv)


def test_invocation_is_shell_free_closed_stdin_pinned_cwd_and_controlled_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    trusted = _fake_pytest(tmp_path)
    seen: dict[str, object] = {}

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        seen["args"] = args
        seen.update(kwargs)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="1 passed\n", stderr="")

    monkeypatch.setattr(verification_mod, "run_in_tracked_process_group", fake_run)
    result = run_host_verification(
        "cmd_pytest",
        admitted_command=_command(("pytest",)),
        workspace_root=root,
        pinned_cwd=Path("."),
        artifact_path=Path(".alysis/verify/result.json"),
        execution_deadline=ExecutionDeadline.from_duration(12.0),
        pytest_executable=_identity(root, trusted),
        base_environment={
            "PATH": os.fspath(root / "attacker-bin"),
            "OPENAI_API_KEY": "must-not-cross",
            "LANG": "C",
        },
    )

    assert result.passed is True
    assert seen["args"] == ["pytest"]
    assert seen["shell"] is False
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["cwd"] == os.fspath(root.resolve())
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["PATH"].split(os.pathsep)[0] == os.fspath(trusted.parent.resolve())
    assert os.fspath(root / "attacker-bin") not in env["PATH"]
    assert "OPENAI_API_KEY" not in env
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert env["PYTEST_ADDOPTS"] == "-p no:cacheprovider"
    assert 0 < float(seen["timeout"]) <= 12.0


def test_pytest_no_tests_is_failed_and_never_skipped_pass(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    trusted = _fake_pytest(tmp_path, "#!/bin/sh\necho 'no tests ran'\nexit 5\n")

    result = _run(root=root, trusted_pytest=trusted)

    assert result.passed is False
    assert result.status == "FAILED"
    assert result.real_execution is False
    assert result.exit_code == 5
    assert result.timed_out is False


def test_exhausted_shared_deadline_returns_retained_timeout_without_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    trusted = _fake_pytest(tmp_path)

    class Clock:
        now = 10.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    deadline = ExecutionDeadline.from_duration(1.0, clock=clock)
    clock.now = 12.0
    monkeypatch.setattr(
        verification_mod,
        "run_in_tracked_process_group",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not execute")),
    )

    result = _run(root=root, trusted_pytest=trusted, deadline=deadline)

    assert result.status == "TIMED_OUT"
    assert result.timed_out is True
    assert result.real_execution is False
    assert result.exit_code is None
    assert result.artifact.path.is_file()


def test_artifact_retention_that_crosses_deadline_finalizes_as_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    trusted = _fake_pytest(tmp_path)

    class Clock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    deadline = ExecutionDeadline.from_duration(1.0, clock=clock)
    monkeypatch.setattr(
        verification_mod,
        "run_in_tracked_process_group",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="1 passed\n",
            stderr="",
        ),
    )
    native_retain = verification_mod._retain_artifact

    def slow_retain(path: Path, payload):  # type: ignore[no-untyped-def]
        artifact = native_retain(path, payload)
        clock.now += 1.1
        return artifact

    monkeypatch.setattr(verification_mod, "_retain_artifact", slow_retain)

    result = run_host_verification(
        "cmd_pytest",
        admitted_command=_command(("pytest", "-q")),
        workspace_root=root,
        pinned_cwd=root,
        artifact_path=root / ".alysis" / "verify" / "result.json",
        execution_deadline=deadline,
        pytest_executable=_identity(root, trusted),
    )

    payload = json.loads(result.artifact.path.read_text(encoding="utf-8"))
    assert result.status == "TIMED_OUT"
    assert result.timed_out is True
    assert result.elapsed_ms == 1100
    assert result.passed is False
    assert payload["status"] == result.status
    assert payload["timedOut"] is True
    assert payload["elapsedMs"] == result.elapsed_ms
    assert deadline.is_exhausted() is True


@pytest.mark.parametrize("delay_point", ["final_write", "final_bind"])
def test_final_artifact_write_or_bind_crossing_deadline_is_rewritten_as_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delay_point: str,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    trusted = _fake_pytest(tmp_path)

    class Clock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    deadline = ExecutionDeadline.from_duration(1.0, clock=clock)
    monkeypatch.setattr(
        verification_mod,
        "run_in_tracked_process_group",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="1 passed\n",
            stderr="",
        ),
    )
    operation_calls = 0
    if delay_point == "final_write":
        native_operation = verification_mod.atomic_write_text

        def delayed_operation(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal operation_calls
            operation_calls += 1
            result = native_operation(*args, **kwargs)
            if operation_calls == 2:
                clock.now += 1.1
            return result

        monkeypatch.setattr(verification_mod, "atomic_write_text", delayed_operation)
    else:
        native_operation = verification_mod._bind_artifact

        def delayed_operation(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal operation_calls
            operation_calls += 1
            result = native_operation(*args, **kwargs)
            if operation_calls == 2:
                clock.now += 1.1
            return result

        monkeypatch.setattr(verification_mod, "_bind_artifact", delayed_operation)

    result = run_host_verification(
        "cmd_pytest",
        admitted_command=_command(("pytest", "-q")),
        workspace_root=root,
        pinned_cwd=root,
        artifact_path=root / ".alysis" / "verify" / "result.json",
        execution_deadline=deadline,
        pytest_executable=_identity(root, trusted),
    )

    payload = json.loads(result.artifact.path.read_text(encoding="utf-8"))
    assert operation_calls >= 3
    assert result.status == "TIMED_OUT"
    assert result.timed_out is True
    assert result.elapsed_ms == 1100
    assert payload["status"] == "TIMED_OUT"
    assert payload["timedOut"] is True
    assert payload["elapsedMs"] == 1100
    assert deadline.is_exhausted() is True


@pytest.mark.parametrize("delay_point", ["final_write", "final_bind"])
def test_final_artifact_timing_converges_without_crossing_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delay_point: str,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    trusted = _fake_pytest(tmp_path)

    class Clock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    deadline = ExecutionDeadline.from_duration(1.0, clock=clock)
    monkeypatch.setattr(
        verification_mod,
        "run_in_tracked_process_group",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="1 passed\n",
            stderr="",
        ),
    )
    operation_calls = 0
    if delay_point == "final_write":
        native_operation = verification_mod.atomic_write_text

        def delayed_operation(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal operation_calls
            operation_calls += 1
            result = native_operation(*args, **kwargs)
            if operation_calls == 2:
                clock.now += 0.5
            return result

        monkeypatch.setattr(verification_mod, "atomic_write_text", delayed_operation)
    else:
        native_operation = verification_mod._bind_artifact

        def delayed_operation(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal operation_calls
            operation_calls += 1
            result = native_operation(*args, **kwargs)
            if operation_calls == 2:
                clock.now += 0.5
            return result

        monkeypatch.setattr(verification_mod, "_bind_artifact", delayed_operation)

    result = run_host_verification(
        "cmd_pytest",
        admitted_command=_command(("pytest", "-q")),
        workspace_root=root,
        pinned_cwd=root,
        artifact_path=root / ".alysis" / "verify" / "result.json",
        execution_deadline=deadline,
        pytest_executable=_identity(root, trusted),
    )

    payload = json.loads(result.artifact.path.read_text(encoding="utf-8"))
    assert operation_calls >= 3
    assert result.status == "PASSED"
    assert result.timed_out is False
    assert result.elapsed_ms == 500
    assert payload["status"] == result.status
    assert payload["timedOut"] is result.timed_out
    assert payload["elapsedMs"] == result.elapsed_ms
    assert deadline.is_exhausted() is False


def test_rejects_cwd_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "escaped"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    trusted = _fake_pytest(tmp_path)

    with pytest.raises(HostVerificationError, match="outside the workspace"):
        run_host_verification(
            "cmd_pytest",
            admitted_command=_command(("pytest",)),
            workspace_root=root,
            pinned_cwd=link,
            artifact_path=root / "verify.json",
            execution_deadline=ExecutionDeadline.from_duration(30.0),
            pytest_executable=_identity(root, trusted),
        )


def test_rejects_artifact_symlink_escape_before_creating_outside_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "artifacts"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    trusted = _fake_pytest(tmp_path)

    with pytest.raises(HostVerificationError, match="escapes the workspace"):
        run_host_verification(
            "cmd_pytest",
            admitted_command=_command(("pytest",)),
            workspace_root=root,
            pinned_cwd=root,
            artifact_path=link / "must-not-exist" / "verify.json",
            execution_deadline=ExecutionDeadline.from_duration(30.0),
            pytest_executable=_identity(root, trusted),
        )

    assert not (outside / "must-not-exist").exists()


def test_command_id_must_match_the_immutable_admitted_command_binding(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    trusted = _fake_pytest(tmp_path)

    with pytest.raises(HostVerificationError, match="does not match"):
        run_host_verification(
            "cmd_other",
            admitted_command=_command(("pytest", "-q"), command_id="cmd_pytest"),
            workspace_root=root,
            pinned_cwd=root,
            artifact_path=root / "verify.json",
            execution_deadline=ExecutionDeadline.from_duration(30.0),
            pytest_executable=_identity(root, trusted),
        )


def test_post_validation_nested_mutation_is_revalidated_at_ingress(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    trusted = _fake_pytest(tmp_path)
    command = _command(("pytest", "-q"))
    command.argv.append("-x")

    with pytest.raises(HostVerificationError, match="canonical|not admitted"):
        run_host_verification(
            "cmd_pytest",
            admitted_command=command,
            workspace_root=root,
            pinned_cwd=root,
            artifact_path=root / "verify.json",
            execution_deadline=ExecutionDeadline.from_duration(30.0),
            pytest_executable=_identity(root, trusted),
        )


def test_caller_mutation_after_ingress_cannot_change_executed_or_reported_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    trusted = _fake_pytest(tmp_path)
    admitted = _command(("pytest", "-q"))

    def fake_run(args, **_kwargs):  # type: ignore[no-untyped-def]
        admitted.argv.append("-x")
        admitted.requirement_ids.append("attacker-controlled")
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="1 passed\n",
            stderr="",
        )

    monkeypatch.setattr(verification_mod, "run_in_tracked_process_group", fake_run)

    result = run_host_verification(
        "cmd_pytest",
        admitted_command=admitted,
        workspace_root=root,
        pinned_cwd=root,
        artifact_path=root / ".alysis" / "verify" / "result.json",
        execution_deadline=ExecutionDeadline.from_duration(30.0),
        pytest_executable=_identity(root, trusted),
    )

    assert admitted.argv == ["pytest", "-q", "-x"]
    assert result.argv == ("pytest", "-q")
    assert result.passed is True
    payload = json.loads(result.artifact.path.read_text(encoding="utf-8"))
    assert payload["argv"] == ["pytest", "-q"]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS /var alias behavior")
def test_absolute_artifact_path_accepts_macos_var_private_var_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    resolved_root = root.resolve()
    private_var = Path("/private/var")
    if not resolved_root.is_relative_to(private_var):
        pytest.skip("temporary directory is not beneath /private/var")
    alias_root = Path("/var") / resolved_root.relative_to(private_var)
    assert alias_root.resolve(strict=True) == resolved_root
    trusted = _fake_pytest(tmp_path)
    monkeypatch.setattr(
        verification_mod,
        "run_in_tracked_process_group",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="1 passed\n",
            stderr="",
        ),
    )

    result = run_host_verification(
        "cmd_pytest",
        admitted_command=_command(("pytest", "-q")),
        workspace_root=alias_root,
        pinned_cwd=alias_root,
        artifact_path=alias_root / ".alysis" / "verify" / "result.json",
        execution_deadline=ExecutionDeadline.from_duration(30.0),
        pytest_executable=_identity(root, trusted),
    )

    assert result.passed is True
    assert result.artifact.path.resolve().is_relative_to(resolved_root)


def test_rejects_unbounded_deadline_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    trusted = _fake_pytest(tmp_path)
    monkeypatch.setattr(
        verification_mod,
        "run_in_tracked_process_group",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not execute")),
    )

    with pytest.raises(HostVerificationError, match="bounded run deadline"):
        _run(
            root=root,
            trusted_pytest=trusted,
            deadline=ExecutionDeadline.from_duration(None),
        )


def test_negative_returncode_is_retained_as_signal_not_protocol_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    trusted = _fake_pytest(tmp_path)
    monkeypatch.setattr(
        verification_mod,
        "run_in_tracked_process_group",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=-9,
            stdout="",
            stderr="killed",
        ),
    )

    result = _run(root=root, trusted_pytest=trusted)

    assert result.status == "FAILED"
    assert result.real_execution is True
    assert result.exit_code is None
    assert result.termination_signal == 9
    payload = json.loads(result.artifact.path.read_text(encoding="utf-8"))
    assert payload["exitCode"] is None
    assert payload["terminationSignal"] == 9


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
def test_timeout_reaps_the_verifier_process_tree(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    trusted = _fake_pytest(
        tmp_path,
        "#!/bin/sh\necho $$ > verifier-pgid\nsleep 60 &\necho $! > verifier-child\nwait\n",
    )

    class AuditedRegistry(ProcessGroupRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.released: list[int] = []

        def release(self, pgid: int) -> None:
            assert not process_group_exists(pgid), "registry released a live process group"
            self.released.append(pgid)
            super().release(pgid)

    registry = AuditedRegistry()

    result = run_host_verification(
        "cmd_pytest",
        admitted_command=_command(("pytest",)),
        workspace_root=root,
        pinned_cwd=root,
        artifact_path=root / "verify.json",
        execution_deadline=ExecutionDeadline.from_duration(0.75),
        pytest_executable=_identity(root, trusted),
        process_registry=registry,
    )

    assert result.status == "TIMED_OUT"
    assert result.timed_out is True
    assert result.real_execution is True
    assert registry.live() == ()
    pgid = int((root / "verifier-pgid").read_text(encoding="utf-8"))
    child_pid = int((root / "verifier-child").read_text(encoding="utf-8"))
    assert pgid in registry.released
    assert not process_group_exists(pgid)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    assert result.artifact.path.is_file()


def test_controlled_path_must_resolve_literal_pytest_to_exact_pin(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    # The directory has no literal `pytest`, so the controlled PATH must not
    # silently execute a later system entrypoint instead of this claimed pin.
    trusted = _write_executable(
        tmp_path / "runtime" / "pytest-pinned",
        "#!/bin/sh\nexit 0\n",
    )

    with pytest.raises(HostVerificationError, match="different executable|cannot resolve"):
        _run(root=root, trusted_pytest=trusted)


def test_rejects_pytest_bytes_replaced_after_admission_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    trusted = _fake_pytest(tmp_path)
    admitted_identity = _identity(root, trusted)
    trusted.write_text("#!/bin/sh\necho replaced\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(
        verification_mod,
        "run_in_tracked_process_group",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not execute")),
    )

    with pytest.raises(HostVerificationError, match="identity changed"):
        run_host_verification(
            "cmd_pytest",
            admitted_command=_command(("pytest", "-q")),
            workspace_root=root,
            pinned_cwd=root,
            artifact_path=root / ".alysis" / "verify" / "result.json",
            execution_deadline=ExecutionDeadline.from_duration(30.0),
            pytest_executable=admitted_identity,
        )


def test_executable_replaced_during_runner_cannot_produce_a_passing_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    trusted = _fake_pytest(tmp_path)
    admitted_identity = _identity(root, trusted)

    def fake_run(args, **_kwargs):  # type: ignore[no-untyped-def]
        trusted.write_text("#!/bin/sh\necho replaced\nexit 0\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="1 passed\n",
            stderr="",
        )

    monkeypatch.setattr(verification_mod, "run_in_tracked_process_group", fake_run)

    result = run_host_verification(
        "cmd_pytest",
        admitted_command=_command(("pytest", "-q")),
        workspace_root=root,
        pinned_cwd=root,
        artifact_path=root / ".alysis" / "verify" / "result.json",
        execution_deadline=ExecutionDeadline.from_duration(30.0),
        pytest_executable=admitted_identity,
    )

    assert result.status == "ERROR"
    assert result.passed is False
    assert result.artifact.path.is_file()


def test_base_environment_cannot_inject_python_or_secret_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    trusted = _fake_pytest(tmp_path)
    seen_env: dict[str, str] = {}

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        seen_env.update(kwargs["env"])
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="1 passed\n", stderr="")

    monkeypatch.setattr(verification_mod, "run_in_tracked_process_group", fake_run)
    run_host_verification(
        "cmd_pytest",
        admitted_command=_command(("pytest",)),
        workspace_root=root,
        pinned_cwd=root,
        artifact_path=root / "verify.json",
        execution_deadline=ExecutionDeadline.from_duration(30.0),
        pytest_executable=_identity(root, trusted),
        base_environment={
            "PYTHONPATH": os.fspath(root / "injected"),
            "PYTHONHOME": os.fspath(root / "fake-home"),
            "GITHUB_TOKEN": "secret",
        },
    )

    assert "PYTHONPATH" not in seen_env
    assert "PYTHONHOME" not in seen_env
    assert "GITHUB_TOKEN" not in seen_env
    assert shutil.which("pytest", path=seen_env["PATH"])
