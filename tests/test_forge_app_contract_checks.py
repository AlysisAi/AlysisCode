from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from _fs_link_helpers import directory_link
from typer.testing import CliRunner

from alysis_code import forge_host_cli as host_cli
from alysis_code.capabilities import build_capabilities_payload
from alysis_code.cli import app
from alysis_code.cli_impl.commands import forge as forge_commands
from alysis_code.config import AppConfig
from alysis_code.forge import (
    add_task,
    create_plan_run,
    load_current_run_paths,
    load_plan,
    save_plan,
)
from alysis_code.forge_contract_verification import run_forge_contract_verification
from alysis_code.forge_status_channel import (
    FORGE_OPERATION_ID_ENV,
    FORGE_STATUS_FD_ENV,
    FORGE_STATUS_NONCE_ENV,
)
from alysis_code.verification_contract import (
    ForgeTaskVerificationContract,
    ForgeVerificationContractV2,
    ForgeVerificationPhase,
    approved_plan_fingerprint,
    build_verification_command_spec,
)

WORKSPACE_ASSESSMENT_KEYS = {
    "action",
    "can_override",
    "candidates",
    "git_root",
    "message",
    "ok",
    "requested_path",
    "risk_level",
    "risk_reasons",
    "workspace_root",
}
VERIFICATION_ASSESSMENT_KEYS = {
    "integration_commands",
    "integration_error",
    "integration_source",
    "integration_verify_mode",
    "invalid_task_ids",
    "message",
    "missing_task_ids",
    "ok",
    "suggested_commands",
    "tasks",
    "verify_mode",
}
VERIFICATION_TASK_KEYS = {
    "commands",
    "id",
    "reason",
    "source",
    "status",
    "validation_errors",
}
PREFLIGHT_ASSESSMENT_KEYS = {
    "action",
    "approved_plan_fingerprint",
    "contract_fingerprint",
    "integration_verify_mode",
    "message",
    "ok",
    "permission_mode",
    "policy",
    "preflight_fingerprint",
    "reason_code",
    "schema_version",
    "surfaces",
    "verify_mode",
    "workspace_root",
}


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "config"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
        "ALYSIS_API_KEY": "",
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
    }


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", os.fspath(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _repo_with_plan(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Forge Contract Test")
    _git(repo, "config", "user.email", "forge-contract@example.test")
    (repo / "README.md").write_text("# Forge contract test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "initial")

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["project_goal"] = "Create hello.txt containing the verified greeting."
    plan["requirements"] = ["Create the requested greeting file."]
    add_task(
        plan,
        title="Create the verified greeting",
        description="Create hello.txt and verify its contents.",
        acceptance_criteria=["hello.txt contains the expected greeting"],
        estimated_files=["hello.txt"],
        write_scope=["hello.txt"],
    )
    save_plan(paths, plan)
    return repo


def _invoke_json(args: list[str], *, env: dict[str, str]) -> tuple[dict[str, object], str]:
    result = CliRunner().invoke(app, args, env=env)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout), result.output


def _contract_for_current_plan(
    repo: Path,
    contract_path: Path,
    *,
    command_text: str = "test -s hello.txt",
    include_integration: bool = False,
) -> ForgeVerificationContractV2:
    plan = load_plan(load_current_run_paths(repo))
    command = build_verification_command_spec(
        command_text,
        source="cli.verify_cmd",
        contract_type="explicit_override",
        acceptance_criterion_ids=(),
    )
    task_contract = ForgeTaskVerificationContract(
        phase=ForgeVerificationPhase.TASK,
        task_id="T01",
        cwd=".",
        referenced_paths=("hello.txt",),
        allowed_outputs=("hello.txt",),
        declared_inputs=(),
        criteria=(),
        commands=(command,),
    )
    integration = None
    if include_integration:
        integration_command = build_verification_command_spec(
            command_text,
            source="environment.authoritative_verification_commands",
            contract_type="authoritative_override",
            acceptance_criterion_ids=(),
        )
        integration = ForgeTaskVerificationContract(
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
        integration=integration,
    )
    contract_path.write_bytes(contract.to_json_bytes())
    return contract


def test_advertised_app_contract_checks_have_canonical_cli_commands() -> None:
    payload = build_capabilities_payload()
    assert payload["features"]["forge.workspace-check"] is True
    assert payload["features"]["forge.verification-check"] is True
    assert payload["features"]["forge.preflight"] is True

    help_result = CliRunner().invoke(app, ["forge", "--help"])

    assert help_result.exit_code == 0, help_result.output
    assert "workspace-check" in help_result.output
    assert "verification-check" in help_result.output
    assert "preflight" in help_result.output


def test_workspace_check_canonicalizes_git_scope_and_matches_app_schema(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    alias = tmp_path / "repo-alias"
    directory_link(alias, repo)

    payload, _output = _invoke_json(
        [
            "forge",
            "workspace-check",
            "--path",
            os.fspath(alias),
            "--action",
            "forge_plan",
            "--json",
        ],
        env=_env(tmp_path),
    )

    assert set(payload) == WORKSPACE_ASSESSMENT_KEYS
    assert payload == {
        "action": "forge_plan",
        "can_override": False,
        "candidates": [],
        "git_root": os.fspath(repo.resolve()),
        "message": "",
        "ok": True,
        "requested_path": os.fspath(repo.resolve()),
        "risk_level": "healthy",
        "risk_reasons": [],
        "workspace_root": os.fspath(repo.resolve()),
    }


def test_workspace_check_reports_guarded_override_and_project_candidates(
    tmp_path: Path,
) -> None:
    broad = tmp_path / "broad"
    broad.mkdir()
    for index in range(12):
        (broad / f"unrelated-{index:02d}").mkdir()
    candidate = broad / "law-project"
    candidate.mkdir()
    (candidate / "package.json").write_text('{"name":"law-project"}\n', encoding="utf-8")

    planner, _output = _invoke_json(
        [
            "forge",
            "workspace-check",
            "--path",
            os.fspath(broad),
            "--action",
            "forge_plan",
            "--json",
        ],
        env=_env(tmp_path),
    )
    direct, _output = _invoke_json(
        [
            "forge",
            "workspace-check",
            "--path",
            os.fspath(broad),
            "--action",
            "forge",
            "--json",
        ],
        env=_env(tmp_path),
    )

    assert planner["ok"] is False
    assert planner["risk_level"] == "guarded"
    assert planner["can_override"] is True
    assert planner["risk_reasons"]
    assert planner["message"]
    assert planner["candidates"] == [
        {
            "path": os.fspath(candidate.resolve()),
            "project_signals": ["package.json"],
            "summary": "package.json",
        }
    ]
    assert direct["ok"] is False
    assert direct["risk_level"] == "guarded"
    assert direct["can_override"] is False


def test_workspace_check_returns_blocked_schema_for_an_unavailable_path(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "does-not-exist"

    payload, _output = _invoke_json(
        ["forge", "workspace-check", "--path", os.fspath(missing), "--json"],
        env=_env(tmp_path),
    )

    assert set(payload) == WORKSPACE_ASSESSMENT_KEYS
    assert payload["ok"] is False
    assert payload["risk_level"] == "blocked"
    assert payload["can_override"] is False
    assert payload["requested_path"] == os.fspath(missing.resolve())
    assert payload["workspace_root"] == os.fspath(missing.resolve())
    assert payload["git_root"] is None
    assert payload["risk_reasons"] == [payload["message"]]


def test_verification_check_reports_missing_strict_authority_without_credentials(
    tmp_path: Path,
) -> None:
    repo = _repo_with_plan(tmp_path)

    payload, _output = _invoke_json(
        [
            "forge",
            "verification-check",
            "--path",
            os.fspath(repo),
            "--verify",
            "strict",
            "--integration-verify",
            "strict",
            "--json",
        ],
        env=_env(tmp_path),
    )

    assert set(payload) == VERIFICATION_ASSESSMENT_KEYS
    assert payload["ok"] is False
    assert payload["verify_mode"] == "strict"
    assert payload["integration_verify_mode"] == "strict"
    assert payload["missing_task_ids"] == ["T01"]
    assert payload["invalid_task_ids"] == []
    assert payload["integration_commands"] == []
    assert payload["integration_error"]
    assert payload["suggested_commands"] == []
    assert isinstance(payload["message"], str) and payload["message"]
    assert len(payload["tasks"]) == 1
    assert set(payload["tasks"][0]) == VERIFICATION_TASK_KEYS
    assert payload["tasks"][0]["id"] == "T01"
    assert payload["tasks"][0]["commands"] == []
    assert payload["tasks"][0]["validation_errors"] == []


def test_verification_check_accepts_explicit_task_command_and_integration_fallback(
    tmp_path: Path,
) -> None:
    repo = _repo_with_plan(tmp_path)
    command = "test -s hello.txt"

    payload, _output = _invoke_json(
        [
            "forge",
            "verification-check",
            "--path",
            os.fspath(repo),
            "--verify",
            "strict",
            "--verify-cmd",
            command,
            "--integration-verify",
            "strict",
            "--json",
        ],
        env=_env(tmp_path),
    )

    assert set(payload) == VERIFICATION_ASSESSMENT_KEYS
    assert payload["ok"] is True
    assert payload["missing_task_ids"] == []
    assert payload["invalid_task_ids"] == []
    assert payload["tasks"][0]["commands"] == [command]
    assert payload["tasks"][0]["source"] == "cli.verify_cmd"
    assert payload["tasks"][0]["validation_errors"] == []
    assert payload["integration_commands"] == [command]
    assert payload["integration_source"] == "cli.verify_cmd_fallback"
    assert payload["integration_error"] == ""
    assert payload["message"] == ""


def test_verification_check_rejects_vacuous_commands_and_unknown_tasks(
    tmp_path: Path,
) -> None:
    repo = _repo_with_plan(tmp_path)

    invalid, _output = _invoke_json(
        [
            "forge",
            "verification-check",
            "--path",
            os.fspath(repo),
            "--verify",
            "strict",
            "--verify-cmd",
            "true",
            "--json",
        ],
        env=_env(tmp_path),
    )
    unknown, _output = _invoke_json(
        [
            "forge",
            "verification-check",
            "--path",
            os.fspath(repo),
            "--verify",
            "strict",
            "--verify-cmd",
            "test -s hello.txt",
            "--task",
            "T99",
            "--json",
        ],
        env=_env(tmp_path),
    )

    assert invalid["ok"] is False
    assert invalid["invalid_task_ids"] == ["T01"]
    assert invalid["missing_task_ids"] == []
    assert "vacuous_verifier" in invalid["tasks"][0]["validation_errors"][0]
    assert unknown["ok"] is False
    assert unknown["tasks"] == []
    assert unknown["missing_task_ids"] == ["T99"]
    assert "Task not found" in unknown["message"]


def test_preflight_plan_is_credential_free_and_matches_app_schema(tmp_path: Path) -> None:
    repo = _repo_with_plan(tmp_path)

    payload, _output = _invoke_json(
        [
            "forge",
            "preflight",
            "--path",
            os.fspath(repo),
            "--permission-mode",
            "workspace-write",
            "--action",
            "plan",
            "--verify",
            "strict",
            "--integration-verify",
            "off",
            "--json",
        ],
        env=_env(tmp_path),
    )

    assert set(payload) == PREFLIGHT_ASSESSMENT_KEYS
    assert payload["schema_version"] == 1
    assert payload["ok"] is True
    assert payload["permission_mode"] == "workspace-write"
    assert payload["reason_code"] is None
    assert payload["action"] is None
    assert payload["workspace_root"] == os.fspath(repo.resolve())
    assert payload["contract_fingerprint"] is None
    assert payload["approved_plan_fingerprint"] is None
    assert isinstance(payload["preflight_fingerprint"], str)
    assert len(payload["preflight_fingerprint"]) == 64
    assert payload["policy"]["action"] == "plan"
    assert [surface["surface"] for surface in payload["surfaces"]] == ["planner"]
    assert payload["surfaces"][0]["backend"] == "sandbox"


def test_preflight_execution_deterministically_binds_exact_contract(tmp_path: Path) -> None:
    repo = _repo_with_plan(tmp_path)
    contract_path = tmp_path / "verification-contract.json"
    contract = _contract_for_current_plan(repo, contract_path)
    args = [
        "forge",
        "preflight",
        "--path",
        os.fspath(repo),
        "--permission-mode",
        "full-access",
        "--action",
        "execute",
        "--verify",
        "strict",
        "--integration-verify",
        "off",
        "--verification-contract",
        os.fspath(contract_path),
        "--approved-plan-fingerprint",
        contract.approved_plan_fingerprint,
        "--json",
    ]

    first = CliRunner().invoke(app, args, env=_env(tmp_path))
    second = CliRunner().invoke(app, args, env=_env(tmp_path))

    assert first.exit_code == second.exit_code == 0, first.output
    assert first.stdout == second.stdout
    payload = json.loads(first.stdout)
    assert set(payload) == PREFLIGHT_ASSESSMENT_KEYS
    assert payload["ok"] is True
    assert payload["contract_fingerprint"] == contract.contract_fingerprint
    assert payload["approved_plan_fingerprint"] == contract.approved_plan_fingerprint
    assert payload["preflight_fingerprint"] and len(payload["preflight_fingerprint"]) == 64
    assert payload["surfaces"][0]["backend"] == "host"
    assert payload["surfaces"][0]["network_enforced"] is False


@pytest.mark.parametrize("case", [0, 1, 2])
def test_preflight_blocks_missing_contract_read_only_and_contract_symlink(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    case: int,
) -> None:
    if case == 2:
        request.getfixturevalue("require_symlinks")
    repo = _repo_with_plan(tmp_path)
    contract_path = tmp_path / "verification-contract.json"
    contract = _contract_for_current_plan(repo, contract_path)
    contract_alias = tmp_path / "contract-alias.json"
    if case == 2:
        contract_alias.symlink_to(contract_path)

    cases = [
        [
            "forge",
            "preflight",
            "--path",
            os.fspath(repo),
            "--permission-mode",
            "workspace-write",
            "--action",
            "execute",
            "--verify",
            "off",
            "--integration-verify",
            "off",
            "--json",
        ],
        [
            "forge",
            "preflight",
            "--path",
            os.fspath(repo),
            "--permission-mode",
            "read-only",
            "--action",
            "plan",
            "--verify",
            "off",
            "--integration-verify",
            "off",
            "--json",
        ],
        [
            "forge",
            "preflight",
            "--path",
            os.fspath(repo),
            "--permission-mode",
            "workspace-write",
            "--action",
            "execute",
            "--verify",
            "strict",
            "--integration-verify",
            "off",
            "--verification-contract",
            os.fspath(contract_alias),
            "--approved-plan-fingerprint",
            contract.approved_plan_fingerprint,
            "--json",
        ],
    ]

    result = CliRunner().invoke(app, cases[case], env=_env(tmp_path))
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert set(payload) == PREFLIGHT_ASSESSMENT_KEYS
    assert payload["ok"] is False
    assert payload["reason_code"] == "preflight_blocked"
    assert payload["preflight_fingerprint"] is None
    expected = [
        "requires --verification-contract",
        "requires workspace-write or full-access",
        "non-symlink",
    ]
    assert expected[case] in payload["message"]


def test_app_contract_verifier_executes_the_exact_typed_command_and_retains_evidence(
    tmp_path: Path,
) -> None:
    repo = _repo_with_plan(tmp_path)
    contract_path = tmp_path / "verification-contract.json"
    command = "python check_greeting.py"
    (repo / "check_greeting.py").write_text(
        "from pathlib import Path\nassert Path('hello.txt').stat().st_size > 0\n",
        encoding="utf-8",
    )
    contract = _contract_for_current_plan(
        repo,
        contract_path,
        command_text=command,
        include_integration=True,
    )
    (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
    cfg = AppConfig(model="test-model")
    cfg.extra_fields = {"verify_sandbox": {"mode": "off"}}

    task_result = run_forge_contract_verification(
        root=repo,
        contract=contract.tasks[0],
        artifact_path=tmp_path / "evidence" / "task.json",
        cfg=cfg,
    )
    integration_result = run_forge_contract_verification(
        root=repo,
        contract=contract.integration,
        artifact_path=tmp_path / "evidence" / "integration.json",
        cfg=cfg,
    )

    assert task_result.all_passed is True
    assert integration_result.all_passed is True
    assert task_result.command_evidence[0].result is not None
    assert task_result.command_evidence[0].result.commands == [command]
    assert task_result.artifact_path.is_file()
    assert integration_result.artifact_path.is_file()

    (repo / "hello.txt").write_text("", encoding="utf-8")
    failed = run_forge_contract_verification(
        root=repo,
        contract=contract.integration,
        artifact_path=tmp_path / "evidence" / "integration-failed.json",
        cfg=cfg,
    )
    assert failed.all_passed is False


def test_contract_backed_dry_run_binds_app_run_generation_and_integration_without_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo_with_plan(tmp_path)
    paths = load_current_run_paths(repo)
    contract_path = tmp_path / "verification-contract.json"
    contract = _contract_for_current_plan(
        repo,
        contract_path,
        include_integration=True,
    )
    generation_id = "gen_abcdefgh"
    manifest = SimpleNamespace(
        native_run_id=paths.run_id,
        planner_input="Create the verified greeting.",
        host_plan_fingerprint="a" * 64,
        native_approval_fingerprint="b" * 64,
        per_managed_task_max_steps=64,
        run_timeout_ms=3_600_000,
        verification_commands=("pytest -q",),
        intent_kind="build",
        declared_scope_kinds=(),
    )
    monkeypatch.setattr(host_cli, "load_resume_manifest", lambda _paths: manifest)
    monkeypatch.setattr(
        forge_commands,
        "_cli_module",
        lambda: SimpleNamespace(
            _resolve_api_key_override=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("dry-run requested provider credentials")
            )
        ),
    )

    read_fd, write_fd = os.pipe()
    try:
        result = CliRunner().invoke(
            app,
            [
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
            ],
            env={
                **_env(tmp_path),
                FORGE_STATUS_FD_ENV: str(write_fd),
                FORGE_OPERATION_ID_ENV: "desktop-contract-dry-run",
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
    assert result.exit_code == 0, result.output
    assert contract.contract_fingerprint in result.output
    assert generation_id in result.output
    assert len(terminals) == 1
    assert terminals[0]["status"] == "succeeded"
    assert terminals[0]["run_id"] == paths.run_id


@pytest.mark.parametrize(
    ("run_id", "generation_id", "message"),
    [
        ("another-run", "gen_abcdefgh", "run ID does not match"),
        (None, "generation-7", "generation ID is invalid"),
    ],
)
def test_app_bound_run_rejects_run_or_generation_identity_drift(
    tmp_path: Path,
    run_id: str | None,
    generation_id: str,
    message: str,
) -> None:
    repo = _repo_with_plan(tmp_path)
    paths = load_current_run_paths(repo)
    contract_path = tmp_path / "verification-contract.json"
    _contract_for_current_plan(repo, contract_path, include_integration=True)

    with pytest.raises(Exception, match=message):
        forge_commands._load_app_bound_forge_contract(
            path=repo,
            verification_contract=contract_path,
            run_id=run_id or paths.run_id,
            generation_id=generation_id,
            verify_mode="strict",
            integration_verify_mode="strict",
        )
