from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from alysis_code import forge_host_cli as host_cli
from alysis_code import forge_host_runtime as host_runtime
from alysis_code.cli import app
from alysis_code.cli_impl import forge as legacy_forge
from alysis_code.cli_impl.commands import forge as forge_commands
from alysis_code.forge_host_protocol.codec import validate_envelope
from alysis_code.forge_host_protocol.models import EventEnvelope, PlanReady, RunTerminal
from alysis_code.forge_status_channel import (
    FORGE_OPERATION_ID_ENV,
    FORGE_STATUS_FD_ENV,
    FORGE_STATUS_NONCE_ENV,
)

FIXTURES = Path(__file__).parents[1] / "tests" / "fixtures" / "forge_host_v2" / "valid"


def _event(name: str) -> EventEnvelope:
    value = validate_envelope((FIXTURES / name).read_bytes())
    assert isinstance(value, EventEnvelope)
    return value


def _cli_stub() -> SimpleNamespace:
    return SimpleNamespace(
        _resolve_api_key_override=lambda **_kwargs: None,
        _require_active_subscription_ready=lambda **_kwargs: None,
    )


def test_cli_trusted_pytest_uses_only_the_current_interpreter_directory(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    interpreter = runtime / ("python.exe" if os.name == "nt" else "python")
    trusted = runtime / ("pytest.exe" if os.name == "nt" else "pytest")
    trusted.write_bytes(b"trusted executable identity")
    monkeypatch.setattr(forge_commands.sys, "executable", str(interpreter))
    monkeypatch.delenv("ALYSIS_FORGE_TRUSTED_PYTEST", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "untrusted"))
    assert forge_commands._resolve_cli_host_pytest(None) == trusted.resolve()

    explicit = tmp_path / "explicit-runner"
    explicit.write_bytes(b"explicit identity")
    monkeypatch.setenv("ALYSIS_FORGE_TRUSTED_PYTEST", str(explicit))
    assert forge_commands._resolve_cli_host_pytest(None) == explicit.resolve()
    assert forge_commands._resolve_cli_host_pytest(trusted) == trusted.resolve()

    trusted.unlink()
    monkeypatch.delenv("ALYSIS_FORGE_TRUSTED_PYTEST")
    with pytest.raises(host_runtime.ForgeHostPreflightError, match="unavailable"):
        forge_commands._resolve_cli_host_pytest(None)


@pytest.mark.parametrize("machine", [False])
def test_public_forge_plan_routes_only_to_canonical_host_runtime(
    tmp_path: Path,
    monkeypatch,
    machine: bool,
) -> None:  # type: ignore[no-untyped-def]
    ready = _event("plan-ready-event.json").payload
    assert isinstance(ready, PlanReady)
    paths = SimpleNamespace(run_id="run_canonicalplan", plan_dir=tmp_path / "plan")
    runtime = SimpleNamespace(paths=paths)
    builder_calls: list[dict[str, object]] = []

    def build_runtime(**kwargs):  # type: ignore[no-untyped-def]
        builder_calls.append(dict(kwargs))
        return runtime

    def canonical_plan(_runtime, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["goal"] == "Build the scoped feature."
        return SimpleNamespace(
            planned=SimpleNamespace(plan_ready=ready),
            manifest=SimpleNamespace(host_plan_fingerprint=ready.host_plan_fingerprint),
            manifest_path=tmp_path / "forge-host-v2-resume.json",
        )

    monkeypatch.setattr(forge_commands, "_cli_module", _cli_stub)
    monkeypatch.setattr(forge_commands, "_resolve_cli_host_pytest", lambda _value: tmp_path)
    monkeypatch.setattr(forge_commands, "_plan_snapshot", lambda *_args: {"canonical": True})
    monkeypatch.setattr(forge_commands, "load_plan", lambda _paths: {"tasks": [{}]})
    monkeypatch.setattr(host_runtime, "build_local_forge_host_runtime", build_runtime)
    monkeypatch.setattr(host_cli, "plan_with_runtime", canonical_plan)
    monkeypatch.setattr(
        legacy_forge,
        "forge_plan_impl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy Forge planner was called")
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "forge",
            "plan",
            "--path",
            str(tmp_path),
            "--goal",
            "Build the scoped feature.",
            "--intent",
            "build",
            "--planning-profile",
            "deep",
        ]
        + (["--machine"] if machine else []),
    )

    assert result.exit_code == 0, result.output
    assert len(builder_calls) == 1
    assert builder_calls[0]["intent_kind"] == "build"
    assert builder_calls[0]["planning_profile"] == "deep"
    if machine:
        events = [json.loads(line) for line in result.stdout.splitlines()]
        saved = next(event["data"] for event in events if event["event"] == "plan_saved")
        assert saved["plan_ready"] == ready.model_dump(mode="json", by_alias=True)
        assert saved["resume_manifest"] == str(tmp_path / "forge-host-v2-resume.json")
        assert saved["task_count"] == len(ready.plan_definition.tasks)
        assert saved["planning_profile"] == "deep"
        assert events[-1]["event"] == "run_completed"
    else:
        assert "Planning profile: Deep" in result.output
        assert "Plan ready" in result.output


def test_public_forge_run_routes_only_to_existing_plan_host_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    terminal_event = _event("terminal-event.json")
    assert isinstance(terminal_event.payload, RunTerminal)
    paths = SimpleNamespace(run_id="run_canonicalrun", plan_dir=tmp_path / "plan")
    manifest = SimpleNamespace(
        planner_input="Build the scoped feature.",
        host_plan_fingerprint="a" * 64,
        native_approval_fingerprint="b" * 64,
        per_managed_task_max_steps=64,
        run_timeout_ms=3_600_000,
        verification_commands=("pytest -q",),
        intent_kind="build",
        declared_scope_kinds=(),
    )
    runtime = SimpleNamespace(paths=paths)
    builder_calls: list[dict[str, object]] = []

    def build_runtime(**kwargs):  # type: ignore[no-untyped-def]
        builder_calls.append(dict(kwargs))
        return runtime

    def canonical_run(_runtime, **_kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            started=SimpleNamespace(outcome=SimpleNamespace(events=(terminal_event,))),
        )

    monkeypatch.setattr(forge_commands, "_cli_module", _cli_stub)
    monkeypatch.setattr(forge_commands, "_resolve_cli_host_pytest", lambda _value: tmp_path)
    monkeypatch.setattr(forge_commands, "load_current_run_paths", lambda _path: paths)
    monkeypatch.setattr(host_cli, "load_resume_manifest", lambda _paths: manifest)
    monkeypatch.setattr(host_cli, "run_with_existing_runtime", canonical_run)
    monkeypatch.setattr(
        host_runtime,
        "build_local_forge_host_runtime_for_existing_plan",
        build_runtime,
    )
    monkeypatch.setattr(
        legacy_forge,
        "forge_run_impl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy Forge executor was called")
        ),
    )

    result = CliRunner().invoke(
        app,
        ["forge", "run", "--path", str(tmp_path), "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert len(builder_calls) == 1
    assert builder_calls[0]["planner_input"] == manifest.planner_input
    assert builder_calls[0]["expected_host_plan_fingerprint"] == "a" * 64
    assert "Forge succeeded" in result.output


def test_public_forge_run_rejects_legacy_authority_widening_flags(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    paths = SimpleNamespace(run_id="run_canonicalrun", plan_dir=tmp_path / "plan")
    manifest = SimpleNamespace(
        planner_input="Build it.",
        host_plan_fingerprint="a" * 64,
        native_approval_fingerprint="b" * 64,
        per_managed_task_max_steps=64,
        run_timeout_ms=3_600_000,
        verification_commands=("pytest -q",),
        intent_kind="build",
        declared_scope_kinds=(),
    )
    monkeypatch.setattr(forge_commands, "_cli_module", _cli_stub)
    monkeypatch.setattr(forge_commands, "load_current_run_paths", lambda _path: paths)
    monkeypatch.setattr(host_cli, "load_resume_manifest", lambda _paths: manifest)

    result = CliRunner().invoke(
        app,
        ["forge", "run", "--path", str(tmp_path), "--scope", "off", "--yes"],
    )

    assert result.exit_code == 2
    assert "does not permit" in result.output
    assert "--scope" in result.output


@pytest.mark.parametrize(
    ("host_status", "host_exit_code", "expected_status", "expected_exit_code"),
    [
        ("succeeded", 0, "succeeded", 0),
        ("failed", 1, "execution_failed", 1),
        ("cancelled", 130, "cancelled", 130),
        ("interrupted", 1, "execution_failed", 1),
    ],
)
def test_public_forge_run_emits_one_authenticated_terminal_for_every_host_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_status: str,
    host_exit_code: int,
    expected_status: str,
    expected_exit_code: int,
) -> None:
    terminal_event = _event("terminal-event.json")
    assert isinstance(terminal_event.payload, RunTerminal)
    terminal_result = terminal_event.payload.result.model_copy(
        update={"status": host_status, "exit_code": host_exit_code}
    )
    terminal_event = terminal_event.model_copy(
        update={"payload": terminal_event.payload.model_copy(update={"result": terminal_result})}
    )
    paths = SimpleNamespace(run_id="run_canonicalrun", plan_dir=tmp_path / "plan")
    manifest = SimpleNamespace(
        native_run_id=paths.run_id,
        planner_input="Build the scoped feature.",
        host_plan_fingerprint="a" * 64,
        native_approval_fingerprint="b" * 64,
        per_managed_task_max_steps=64,
        run_timeout_ms=3_600_000,
        verification_commands=("pytest -q",),
        intent_kind="build",
        declared_scope_kinds=(),
    )
    runtime = SimpleNamespace(paths=paths)

    monkeypatch.setattr(forge_commands, "_cli_module", _cli_stub)
    monkeypatch.setattr(forge_commands, "_resolve_cli_host_pytest", lambda _value: tmp_path)
    monkeypatch.setattr(forge_commands, "load_current_run_paths", lambda _path: paths)
    monkeypatch.setattr(host_cli, "load_resume_manifest", lambda _paths: manifest)
    monkeypatch.setattr(
        host_cli,
        "run_with_existing_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(
            started=SimpleNamespace(outcome=SimpleNamespace(events=(terminal_event,)))
        ),
    )
    monkeypatch.setattr(
        host_runtime,
        "build_local_forge_host_runtime_for_existing_plan",
        lambda **_kwargs: runtime,
    )

    read_fd, write_fd = os.pipe()
    try:
        result = CliRunner().invoke(
            app,
            ["forge", "run", "--path", str(tmp_path), "--yes"],
            env={
                FORGE_STATUS_FD_ENV: str(write_fd),
                FORGE_OPERATION_ID_ENV: "desktop-operation",
                FORGE_STATUS_NONCE_ENV: "0123456789abcdef0123456789abcdef",
            },
        )
    finally:
        os.close(write_fd)
    try:
        records = [json.loads(line) for line in os.read(read_fd, 65536).splitlines()]
    finally:
        os.close(read_fd)

    terminals = [record for record in records if "event" not in record]
    assert result.exit_code == expected_exit_code, result.output
    assert len(terminals) == 1
    assert terminals[0]["status"] == expected_status
    assert terminals[0]["exit_code"] == expected_exit_code
    assert terminals[0]["run_id"] == paths.run_id
    assert terminals[0]["provider_calls"] == terminal_result.usage.model_calls


def test_public_forge_run_invalid_hidden_run_id_emits_authenticated_terminal_with_exit_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = SimpleNamespace(run_id="run_canonicalrun", plan_dir=tmp_path / "plan")
    manifest = SimpleNamespace(native_run_id=paths.run_id)
    monkeypatch.setattr(forge_commands, "load_current_run_paths", lambda _path: paths)
    monkeypatch.setattr(forge_commands, "load_plan", lambda _paths: {"tasks": []})
    monkeypatch.setattr(host_cli, "load_resume_manifest", lambda _paths: manifest)

    operation_id = "desktop-invalid-run-id"
    nonce = "0123456789abcdef0123456789abcdef"
    read_fd, write_fd = os.pipe()
    try:
        result = CliRunner().invoke(
            app,
            [
                "forge",
                "run",
                "--path",
                str(tmp_path),
                "--verification-contract",
                str(tmp_path / "unused-contract.json"),
                "--run-id",
                "../invalid",
                "--yes",
            ],
            env={
                FORGE_STATUS_FD_ENV: str(write_fd),
                FORGE_OPERATION_ID_ENV: operation_id,
                FORGE_STATUS_NONCE_ENV: nonce,
            },
        )
    finally:
        os.close(write_fd)
    try:
        records = [json.loads(line) for line in os.read(read_fd, 65536).splitlines()]
    finally:
        os.close(read_fd)

    assert result.exit_code == 2, result.output
    assert "run ID does not match" in result.output
    assert len(records) == 1
    [terminal] = records
    assert "event" not in terminal
    assert terminal["operation_id"] == operation_id
    assert terminal["nonce"] == nonce
    assert terminal["status"] == "preflight_blocked"
    assert terminal["reason_code"] == "forge_run_preflight_blocked"
    assert terminal["exit_code"] == result.exit_code
