from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from alysis_code.forge import add_task, create_plan_run, load_plan, save_plan
from alysis_code.forge_host_cli import ForgeHostResumeManifest, write_resume_manifest
from alysis_code.forge_status_channel import (
    ALLOWED_FORGE_TERMINAL_STATUSES,
    FORGE_OPERATION_ID_ENV,
    FORGE_STATUS_FD_ENV,
    FORGE_STATUS_NONCE_ENV,
    FORGE_STATUS_PROTOCOL,
    FORGE_STATUS_SCHEMA_VERSION,
)
from alysis_code.verification_contract import (
    ForgeTaskVerificationContract,
    ForgeVerificationContractV2,
    ForgeVerificationPhase,
    approved_plan_fingerprint,
    build_verification_command_spec,
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", re.ASCII)
_PROGRESS_KEYS = {
    "protocol",
    "schema_version",
    "operation_id",
    "nonce",
    "event",
    "status",
    "sequence",
    "phase",
    "message",
    "task_id",
    "run_id",
}
_TERMINAL_KEYS = {
    "protocol",
    "schema_version",
    "operation_id",
    "nonce",
    "run_id",
    "status",
    "reason_code",
    "exit_code",
    "message",
    "action",
    "provider_calls",
    "retryable",
}


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", os.fspath(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _electron_compatible_decode(
    payload: bytes,
    *,
    operation_id: str,
    nonce: str,
) -> list[dict[str, Any]]:
    """Mirror the App decoder's authenticated JSONL/type/ordering contract."""

    records: list[dict[str, Any]] = []
    terminal_count = 0
    last_sequence = 0
    for raw_line in payload.splitlines():
        value = json.loads(raw_line)
        assert isinstance(value, dict)
        assert value["protocol"] == FORGE_STATUS_PROTOCOL
        assert value["schema_version"] == FORGE_STATUS_SCHEMA_VERSION
        assert value["operation_id"] == operation_id
        assert value["nonce"] == nonce
        assert isinstance(value.get("message"), str) and value["message"]
        assert len(value["message"].encode("utf-16-le")) // 2 <= 4096
        if value.get("event") in {"progress", "heartbeat"}:
            assert terminal_count == 0
            assert set(value) <= _PROGRESS_KEYS
            assert value["status"] == "running"
            assert isinstance(value.get("sequence"), int)
            assert value["sequence"] == last_sequence + 1
            last_sequence = value["sequence"]
            assert _IDENTIFIER.fullmatch(value["phase"])
        else:
            terminal_count += 1
            assert terminal_count == 1
            assert set(value) <= _TERMINAL_KEYS
            assert value["status"] in ALLOWED_FORGE_TERMINAL_STATUSES
            assert _IDENTIFIER.fullmatch(value["reason_code"])
            assert isinstance(value.get("exit_code"), int)
            assert isinstance(value.get("retryable"), bool)
            provider_calls = value.get("provider_calls")
            assert provider_calls is None or (
                isinstance(provider_calls, int)
                and not isinstance(provider_calls, bool)
                and 0 <= provider_calls <= 2**53 - 1
            )
        records.append(value)
    assert terminal_count == 1
    return records


def _build_dry_run_fixture(tmp_path: Path) -> tuple[Path, Any, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Status Boundary Test")
    _git(repo, "config", "user.email", "status-boundary@example.test")
    (repo / "README.md").write_text("# status boundary\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "initial")

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["project_goal"] = "Create hello.txt."
    plan["requirements"] = ["Create the approved file."]
    task = add_task(
        plan,
        title="Create hello.txt",
        description="Create the approved hello.txt output.",
        acceptance_criteria=["hello.txt exists and is non-empty"],
        estimated_files=["hello.txt"],
        write_scope=["hello.txt"],
    )
    save_plan(paths, plan)

    manifest = ForgeHostResumeManifest._validated(  # noqa: SLF001 - exact persisted fixture
        native_run_id=paths.run_id,
        planner_input="Create hello.txt.",
        intent_kind="build",
        per_managed_task_max_steps=12,
        run_timeout_ms=60_000,
        verification_commands=("pytest -q",),
        declared_scope_kinds=(("hello.txt", "file"),),
        host_plan_fingerprint="a" * 64,
        native_approval_fingerprint="b" * 64,
    )
    write_resume_manifest(paths, manifest)

    task_command = build_verification_command_spec(
        "test -s hello.txt",
        source="cli.verify_cmd",
        contract_type="explicit_override",
        acceptance_criterion_ids=(),
    )
    integration_command = build_verification_command_spec(
        "test -s hello.txt",
        source="environment.authoritative_verification_commands",
        contract_type="authoritative_override",
        acceptance_criterion_ids=(),
    )
    task_contract = ForgeTaskVerificationContract(
        phase=ForgeVerificationPhase.TASK,
        task_id=str(task["id"]),
        cwd=".",
        referenced_paths=("hello.txt",),
        allowed_outputs=("hello.txt",),
        declared_inputs=(),
        criteria=(),
        commands=(task_command,),
    )
    integration_contract = ForgeTaskVerificationContract(
        phase=ForgeVerificationPhase.INTEGRATION,
        task_id=None,
        cwd=".",
        referenced_paths=("hello.txt",),
        allowed_outputs=("hello.txt",),
        declared_inputs=(),
        criteria=(),
        commands=(integration_command,),
    )
    contract = ForgeVerificationContractV2(
        workspace_root=os.fspath(repo.resolve()),
        approved_plan_fingerprint=approved_plan_fingerprint(plan),
        tasks=(task_contract,),
        integration=integration_contract,
    )
    contract_path = tmp_path / "verification-contract.json"
    contract_path.write_bytes(contract.to_json_bytes())
    return repo, paths, contract_path


@pytest.mark.skipif(os.name != "posix", reason="pass_fds is a POSIX inherited-fd contract")
def test_real_forge_command_emits_one_authenticated_cross_process_terminal(
    tmp_path: Path,
) -> None:
    repo, paths, contract_path = _build_dry_run_fixture(tmp_path)
    operation_id = "operation_statusboundary01"
    nonce = "0123456789abcdef0123456789abcdef"
    generation_id = "gen_abcdefgh"
    read_fd, write_fd = os.pipe()
    env = os.environ.copy()
    source_root = Path(__file__).parents[1] / "src"
    env.update(
        {
            FORGE_STATUS_FD_ENV: str(write_fd),
            FORGE_OPERATION_ID_ENV: operation_id,
            FORGE_STATUS_NONCE_ENV: nonce,
            "PYTHONPATH": os.pathsep.join(
                filter(None, (os.fspath(source_root), env.get("PYTHONPATH", "")))
            ),
            "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "config"),
            "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
        }
    )
    command = [
        sys.executable,
        "-m",
        "alysis_code",
        "forge",
        "run",
        "--path",
        os.fspath(repo),
        "--dry-run",
        "--mode",
        "auto",
        "--scope",
        "strict",
        "--verify",
        "strict",
        "--max-attempts",
        "4",
        "--base-branch",
        str(paths.current_branch),
        "--integration-verify",
        "strict",
        "--verification-contract",
        os.fspath(contract_path),
        "--run-id",
        paths.run_id,
        "--generation-id",
        generation_id,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=repo,
            env=env,
            pass_fds=(write_fd,),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        os.close(write_fd)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(read_fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(read_fd)

    assert completed.returncode == 0, completed.stderr or completed.stdout
    records = _electron_compatible_decode(
        b"".join(chunks),
        operation_id=operation_id,
        nonce=nonce,
    )
    progress = [record for record in records if record.get("event") == "progress"]
    terminals = [record for record in records if record.get("event") is None]
    assert [record["phase"] for record in progress] == ["preflight"]
    assert [record["sequence"] for record in progress] == [1]
    assert len(terminals) == 1
    terminal = terminals[0]
    assert all(record.get("run_id") == paths.run_id for record in records)
    assert terminal["status"] == "succeeded"
    assert terminal["reason_code"] == "forge_run_dry_run_completed"
    assert terminal["exit_code"] == completed.returncode
    # Dry-run makes no provider call, and the producer knows that count exactly.
    expected_provider_calls = 0
    assert terminal.get("provider_calls") == expected_provider_calls
