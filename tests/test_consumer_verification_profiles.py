from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from alysis_code.config import AppConfig
from alysis_code.consumer_verification import run_consumer_profile
from alysis_code.durable_service_manager import DurableServiceManager
from alysis_code.sandbox_settings import ShellSandboxSettings


def py(script: str) -> str:
    args = [sys.executable, script]
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def run_profile(tmp_path: Path, spec: dict, timeout: float = 8):
    cfg = AppConfig(model="test")
    cfg.extra_fields["verify_sandbox"] = {"mode": "off"}
    return run_consumer_profile(
        root=tmp_path,
        spec=spec,
        cfg=cfg,
        artifact_path=tmp_path / "receipts" / "result.json",
        timeout_s=timeout,
    )


@pytest.mark.parametrize("artifact", [".env", "bundle/.aws/credentials"])
def test_sensitive_artifacts_require_authorization_before_hashing(tmp_path, monkeypatch, artifact):
    from alysis_code import consumer_verification as consumer

    path = tmp_path / artifact
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("private-token-value", encoding="utf-8")
    monkeypatch.setattr(
        consumer, "_digest", lambda *_args: pytest.fail("unauthorized file was hashed")
    )
    with pytest.raises(PermissionError, match="read authorization"):
        run_profile(
            tmp_path,
            {
                "profile": "cli",
                "artifacts": [artifact.split("/")[0]],
                "checks": [{"command": "echo checked"}],
            },
        )


def test_authorized_nested_sensitive_artifact_has_no_persistent_plaintext(tmp_path):
    secret = "private-consumer-token-value"
    (tmp_path / "bundle" / ".aws").mkdir(parents=True)
    (tmp_path / "bundle" / ".aws" / "credentials").write_text(secret, encoding="utf-8")
    (tmp_path / "client.py").write_text(
        "from pathlib import Path; print(Path('bundle/.aws/credentials').read_text())",
        encoding="utf-8",
    )
    cfg = AppConfig(model="test")
    cfg.extra_fields["verify_sandbox"] = {"mode": "off"}
    authorized = []
    result = run_consumer_profile(
        root=tmp_path,
        spec={
            "profile": "cli",
            "artifacts": ["bundle", "client.py"],
            "checks": [{"command": py("client.py"), "stdout_contains": [secret]}],
        },
        cfg=cfg,
        artifact_path=tmp_path / "receipts" / "result.json",
        timeout_s=5,
        authorize_artifact_read=authorized.append,
    )
    assert result["profile_passed"], result
    assert set(authorized) == {str(Path("bundle/.aws/credentials")), "client.py"}
    assert secret not in str(result)
    assert all(secret not in p.read_text() for p in (tmp_path / "receipts").iterdir())


def test_preprocessing_uses_profile_deadline_before_content_read(tmp_path, monkeypatch):
    from alysis_code import consumer_verification as consumer
    from alysis_code.execution_deadline import DeadlineExhausted

    (tmp_path / "product.py").write_text("VALUE = 1", encoding="utf-8")
    clock = [1.0]
    monkeypatch.setattr(consumer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(consumer, "_digest", lambda *_args: pytest.fail("read after deadline"))
    cfg = AppConfig(model="test")
    with pytest.raises(DeadlineExhausted):
        run_consumer_profile(
            root=tmp_path,
            spec={"profile": "library", "artifacts": ["product.py"], "python_module": "product"},
            cfg=cfg,
            artifact_path=tmp_path / "result.json",
            timeout_s=1,
            authorize_artifact_read=lambda _path: clock.__setitem__(0, 3.0),
        )


@pytest.mark.parametrize("mode", ["readonly", "auto"])
def test_consumer_tool_preserves_readonly_and_sensitive_read_denial(tmp_path, mode):
    from alysis_code.agent.errors import AgentRuntimeError
    from alysis_code.agent_loop import create_session

    (tmp_path / "bundle" / ".aws").mkdir(parents=True)
    (tmp_path / "bundle" / ".aws" / "credentials").write_text("secret", encoding="utf-8")
    cfg = AppConfig(model="test-model")
    cfg.extra_fields = {"verify_sandbox": {"mode": "off"}, "shell_sandbox": {"mode": "off"}}
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode=mode,
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="test",
        non_interactive=True,
        session_log_dir_override=tmp_path / "sessions",
    )
    try:
        if "verify_run" not in session.tools:
            assert mode == "readonly"
            return
        with pytest.raises(AgentRuntimeError, match="readonly|Explicit one-time user approval"):
            session.tools["verify_run"].run(
                {
                    "consumer_profile": {
                        "profile": "cli",
                        "artifacts": ["bundle"],
                        "checks": [{"command": "echo checked"}],
                    }
                }
            )
    finally:
        session.close()


def test_library_imports_only_staged_artifact_from_fresh_context(tmp_path, monkeypatch):
    (tmp_path / "product.py").write_text("answer = 7\n", encoding="utf-8")
    poison = tmp_path / "unstaged"
    poison.mkdir()
    (poison / "product.py").write_text(
        "raise RuntimeError('stale dependency cache')\n", encoding="utf-8"
    )
    monkeypatch.setenv("PYTHONPATH", str(poison))
    result = run_profile(
        tmp_path, {"profile": "library", "artifacts": ["product.py"], "python_module": "product"}
    )
    assert result["profile_passed"], result
    assert result["verification_evidence_allowed"] is False
    report = result["consumer_profile_report"]
    assert report["preserved_artifacts"] is True
    assert report["checks"][0]["executed"] is True


def test_import_time_computation_is_a_real_consumer_failure(tmp_path):
    (tmp_path / "product.py").write_text("import time; time.sleep(30)\n", encoding="utf-8")
    before = time.monotonic()
    result = run_profile(
        tmp_path,
        {"profile": "library", "artifacts": ["product.py"], "python_module": "product"},
        timeout=0.4,
    )
    assert not result["profile_passed"]
    assert time.monotonic() - before < 4


def test_library_import_does_not_silently_generate_consumer_outputs(tmp_path):
    (tmp_path / "product.py").write_text(
        "from pathlib import Path; Path('sample-output.txt').write_text('unexpected sample')",
        encoding="utf-8",
    )
    result = run_profile(
        tmp_path, {"profile": "library", "artifacts": ["product.py"], "python_module": "product"}
    )
    assert not result["profile_passed"]
    assert "import created" in result["consumer_profile_report"]["checks"][0]["output"]


@pytest.mark.parametrize(
    "arguments",
    [["a b"], ['say "hi"'], ["50%", "", "end"], ["C:\\dir with space\\x", "tab\there"]],
)
def test_check_commands_run_exactly_as_the_platform_shell_runs_them(tmp_path, arguments):
    # A staged path with a space forces quoting on every platform, wherever Python lives.
    (tmp_path / "sub dir").mkdir()
    (tmp_path / "sub dir" / "show args.py").write_text(
        "import json, sys; print(json.dumps(sys.argv[1:]))\n", encoding="utf-8"
    )
    words = [sys.executable, "sub dir/show args.py", *arguments]
    command = subprocess.list2cmdline(words) if os.name == "nt" else shlex.join(words)
    terminal = subprocess.run(
        command, shell=True, cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert terminal == json.dumps(arguments)

    result = run_profile(
        tmp_path,
        {
            "profile": "cli",
            "artifacts": ["sub dir/show args.py"],
            "checks": [{"name": "same", "command": command, "stdout_contains": [terminal]}],
        },
    )

    assert result["profile_passed"], result


def test_cli_positive_and_negative_cases_use_real_exit_and_output(tmp_path):
    (tmp_path / "cli.py").write_text(
        "import sys; print('accepted' if len(sys.argv) == 1 else 'rejected'); raise SystemExit(0 if len(sys.argv) == 1 else 3)\n",
        encoding="utf-8",
    )
    result = run_profile(
        tmp_path,
        {
            "profile": "cli",
            "artifacts": ["cli.py"],
            "checks": [
                {"name": "valid", "command": py("cli.py"), "stdout_contains": ["accepted"]},
                {
                    "name": "invalid",
                    "kind": "negative",
                    "command": py("cli.py") + " bad",
                    "expected_exit": 3,
                    "stdout_contains": ["rejected"],
                },
            ],
        },
    )
    assert result["profile_passed"], result


def test_distributed_workers_really_exchange_outputs(tmp_path):
    (tmp_path / "worker.py").write_text(
        """import json, os, time
from pathlib import Path
rank = int(os.environ['ALYSIS_CONSUMER_RANK'])
world = int(os.environ['ALYSIS_CONSUMER_WORLD_SIZE'])
Path(f'rank-{rank}.json').write_text(json.dumps({'rank': rank, 'value': rank + 1}))
deadline = time.monotonic() + 3
while len(list(Path('.').glob('rank-*.json'))) < world:
    assert time.monotonic() < deadline, 'other worker never executed'
    time.sleep(0.02)
assert sorted(json.loads(p.read_text())['value'] for p in Path('.').glob('rank-*.json')) == list(range(1, world + 1))
print('exchange passed')
""",
        encoding="utf-8",
    )
    result = run_profile(
        tmp_path,
        {
            "profile": "distributed",
            "artifacts": ["worker.py"],
            "workers": 3,
            "checks": [{"command": py("worker.py"), "stdout_contains": ["exchange passed"]}],
        },
    )
    assert result["profile_passed"], result
    assert {row["rank"] for row in result["consumer_profile_report"]["checks"]} == {0, 1, 2}


@pytest.mark.parametrize(
    "expected,passed",
    [({"converted": [2, 4]}, True), ({"converted": [4, 2]}, False), (None, False)],
)
def test_data_checks_content_and_preserves_input(tmp_path, expected, passed):
    (tmp_path / "input.json").write_text("[1, 2]", encoding="utf-8")
    (tmp_path / "convert.py").write_text(
        "import json; from pathlib import Path; Path('output.json').write_text(json.dumps({'converted': [2*x for x in json.loads(Path('input.json').read_text())]}))",
        encoding="utf-8",
    )
    result = run_profile(
        tmp_path,
        {
            "profile": "data",
            "artifacts": ["input.json", "convert.py"],
            "checks": [{"command": py("convert.py")}],
            "result_checks": [
                {
                    "path": "output.json",
                    "format": "json",
                    "required_keys": ["converted"],
                    "expected": expected,
                }
            ],
        },
    )
    assert result["profile_passed"] is passed
    assert result["consumer_profile_report"]["preserved_artifacts"]
    assert (tmp_path / "input.json").read_text() == "[1, 2]"


def test_probe_that_rewrites_deliverable_cannot_claim_its_original_version_passed(tmp_path):
    (tmp_path / "product.py").write_text("raise RuntimeError('broken')", encoding="utf-8")
    (tmp_path / "cheat.py").write_text(
        "from pathlib import Path; Path('product.py').write_text('pass'); import product",
        encoding="utf-8",
    )
    result = run_profile(
        tmp_path,
        {
            "profile": "library",
            "artifacts": ["product.py", "cheat.py"],
            "checks": [{"command": py("cheat.py")}],
        },
    )
    assert not result["profile_passed"]
    assert result["consumer_profile_report"]["preserved_artifacts"] is False
    assert "broken" in (tmp_path / "product.py").read_text()


@pytest.mark.parametrize("score,passed", [(0.4, True), (0.9, False)])
def test_optimization_checks_declared_objective_not_last_activity(tmp_path, score, passed):
    (tmp_path / "evaluate.py").write_text(
        f"from pathlib import Path; Path('score.json').write_text('{{\"loss\": {score}}}')",
        encoding="utf-8",
    )
    result = run_profile(
        tmp_path,
        {
            "profile": "optimization",
            "artifacts": ["evaluate.py"],
            "checks": [{"command": py("evaluate.py")}],
            "metric": {
                "path": "score.json",
                "key": "loss",
                "baseline": 0.5,
                "direction": "minimize",
            },
        },
    )
    assert result["profile_passed"] is passed


def test_workspace_boundary_is_enforced_before_execution(tmp_path):
    with pytest.raises(ValueError, match="relative paths"):
        run_profile(
            tmp_path,
            {
                "profile": "cli",
                "artifacts": ["../secret.txt"],
                "checks": [{"command": "echo should-not-run"}],
            },
        )


def test_fresh_wheel_install_and_import_uses_published_distribution(tmp_path):
    uv = shutil.which("uv")
    wheel = tmp_path / "sample_api-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("sample_api/__init__.py", "VERSION = 'published-1.0'\n")
        archive.writestr(
            "sample_api-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: sample-api\nVersion: 1.0\n",
        )
        archive.writestr(
            "sample_api-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: production-regression\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr("sample_api-1.0.dist-info/RECORD", "")
    args = (
        [uv, "--no-cache", "pip", "install", "--python", sys.executable]
        if uv
        else [sys.executable, "-m", "pip", "install", "--no-cache-dir"]
    ) + [
        "--no-index",
        "--no-deps",
        "--target",
        "installed",
        wheel.name,
    ]
    install = subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)
    result = run_profile(
        tmp_path,
        {
            "profile": "library",
            "artifacts": [wheel.name],
            "python_module": "sample_api",
            "import_roots": ["installed"],
            "checks": [{"kind": "setup", "name": "offline-wheel-install", "command": install}],
        },
    )
    assert result["profile_passed"], result
    assert [item["kind"] for item in result["consumer_profile_report"]["checks"]] == [
        "setup",
        "import",
    ]


def test_service_profile_runs_a_fresh_real_client_and_rechecks_ownership(tmp_path):
    (tmp_path / "index.html").write_bytes(b"public-response")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    manager = DurableServiceManager(
        root=tmp_path, state_dir=tmp_path / "services", settings=ShellSandboxSettings(mode="off")
    )
    args = [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"]
    started = manager.start(
        cmd=subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args),
        cwd=tmp_path,
        readiness={"type": "tcp", "host": "127.0.0.1", "port": port, "timeout_s": 3},
    )
    try:
        (tmp_path / "client.py").write_text(
            f"import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:{port}/', timeout=1).read() == b'public-response'; print('client consumed')",
            encoding="utf-8",
        )
        cfg = AppConfig(model="test")
        cfg.extra_fields["verify_sandbox"] = {"mode": "off"}
        result = run_consumer_profile(
            root=tmp_path,
            spec={
                "profile": "service",
                "artifacts": ["client.py"],
                "service_id": started.service_id,
                "checks": [{"command": py("client.py"), "stdout_contains": ["client consumed"]}],
            },
            cfg=cfg,
            artifact_path=tmp_path / "receipt.json",
            timeout_s=5,
            service_status=manager.status,
        )
        assert result["profile_passed"], result
        assert result["consumer_profile_report"]["service_owned_before_and_after"]
    finally:
        manager.stop(started.service_id)


def test_strict_sandbox_unavailable_never_runs_host_consumer(tmp_path, monkeypatch):
    import alysis_code.sandbox_runner as sandbox

    (tmp_path / "probe.py").write_text("raise RuntimeError('HOST_EXECUTED')", encoding="utf-8")
    cfg = AppConfig(model="test")
    cfg.extra_fields = {
        "verify_sandbox": {"mode": "strict"},
        "shell_sandbox": {"mode": "strict", "backend": "bwrap"},
    }
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)
    result = run_consumer_profile(
        root=tmp_path,
        spec={"profile": "cli", "artifacts": ["probe.py"], "checks": [{"command": py("probe.py")}]},
        cfg=cfg,
        artifact_path=tmp_path / "receipt.json",
        timeout_s=1,
    )
    assert not result["profile_passed"]
    assert all(
        "HOST_EXECUTED" not in item["output"]
        for item in result["consumer_profile_report"]["checks"]
    )
    assert all(not item["executed"] for item in result["consumer_profile_report"]["checks"])


@pytest.mark.parametrize("profile_passed", [True, False])
def test_consumer_observation_never_promotes_or_overwrites_required_check_authority(
    tmp_path, profile_passed
):
    from alysis_code.agent.acceptance_contract import (
        AcceptanceCriterionStatus,
        EvidenceOrigin,
        build_acceptance_contract,
    )
    from alysis_code.agent.verification import TurnExecutionState, _record_tool_effect

    contract = build_acceptance_contract(root=tmp_path, instruction="Run `pytest -q`.")
    contract.task_id = "host-task"
    state = TurnExecutionState(
        execution_requested=True,
        acceptance_contract=contract,
        last_verification_passed=True,
        verification_relevant_edit_generation=4,
    )
    report = {
        "profile": "cli",
        "passed": profile_passed,
        "status": "passed" if profile_passed else "failed",
        "result_id": "sample-receipt",
        "artifact_hashes": {"cli.py": "digest"},
        "authority": "user",
    }
    _record_tool_effect(
        root=tmp_path,
        state=state,
        tool_name="verify_run",
        arguments={},
        status="ok",
        result={"consumer_profile_report": report},
        known_verification_commands=["pytest -q"],
        verification_authoritative=True,
    )
    assert state.last_verification_passed is True
    assert state.verification_attempt_count == 0
    assert all(
        criterion.status != AcceptanceCriterionStatus.PASSED for criterion in contract.criteria
    )
    evidence = contract.evidence[-1]
    assert evidence.origin == EvidenceOrigin.SELF_AUTHORED
    assert evidence.evidence_allowed is False
    assert evidence.task_id == "host-task" and evidence.generation == 4
    assert report["authority"] == "supplemental"


def test_real_verify_run_profile_tool_keeps_mandatory_commands_separate(tmp_path):
    from alysis_code.agent_loop import create_session
    from alysis_code.verify_gate import VerifyError

    (tmp_path / "product.py").write_text("VALUE = 1", encoding="utf-8")
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q"])
    cfg.extra_fields = {"verify_sandbox": {"mode": "off"}, "shell_sandbox": {"mode": "off"}}
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="test",
        non_interactive=True,
        session_log_dir_override=tmp_path / "sessions",
    )
    profile = {"profile": "library", "artifacts": ["product.py"], "python_module": "product"}
    try:
        result = session.tools["verify_run"].run({"consumer_profile": profile})
        assert result["profile_passed"], result
        assert result["verification_evidence_allowed"] is False
        with pytest.raises(VerifyError, match="separate checks"):
            session.tools["verify_run"].run(
                {"consumer_profile": profile, "commands": ["pytest -q"]}
            )
    finally:
        session.close()


def test_non_network_daemon_checks_real_operation_without_claiming_endpoint_readiness(tmp_path):
    queue = tmp_path / "queue"
    queue.mkdir()
    (tmp_path / "worker.py").write_text(
        f"""import time
from pathlib import Path
queue = Path({str(queue)!r})
while True:
    request = queue / 'request'
    if request.exists():
        data = request.read_text()
        # Publish only complete files; existence alone does not imply a write finished.
        response = queue / 'response.tmp'
        response.write_text(data.upper())
        response.replace(queue / 'response')
        request.unlink()
    time.sleep(0.02)
""",
        encoding="utf-8",
    )
    manager = DurableServiceManager(
        root=tmp_path, state_dir=tmp_path / "services", settings=ShellSandboxSettings(mode="off")
    )
    started = manager.start(cmd=py("worker.py"), cwd=tmp_path)
    try:
        (tmp_path / "client.py").write_text(
            f"""import time
from pathlib import Path
queue = Path({str(queue)!r})
request = queue / 'request.tmp'
request.write_text('public operation')
request.replace(queue / 'request')
deadline = time.monotonic() + 2
while not (queue / 'response').exists():
    assert time.monotonic() < deadline
    time.sleep(0.02)
assert (queue / 'response').read_text() == 'PUBLIC OPERATION'
""",
            encoding="utf-8",
        )
        cfg = AppConfig(model="test")
        cfg.extra_fields["verify_sandbox"] = {"mode": "off"}
        result = run_consumer_profile(
            root=tmp_path,
            spec={
                "profile": "service",
                "service_scope": "process",
                "artifacts": ["client.py"],
                "service_id": started.service_id,
                "checks": [{"command": py("client.py")}],
            },
            cfg=cfg,
            artifact_path=tmp_path / "receipt.json",
            timeout_s=5,
            service_status=manager.status,
        )
        assert result["profile_passed"], result
        report = result["consumer_profile_report"]
        assert report["service_scope"] == "process"
        assert report["service_identity_before_and_after"]
        assert "service_owned_before_and_after" not in report
        assert report["service"]["readiness"]["endpoint_owned"] is False
        assert result["verification_evidence_allowed"] is False
    finally:
        manager.stop(started.service_id)


def test_missing_published_input_is_exposed_by_fresh_directory(tmp_path):
    (tmp_path / "local-only.txt").write_text("unpublished dependency", encoding="utf-8")
    (tmp_path / "probe.py").write_text(
        "from pathlib import Path; assert Path('local-only.txt').read_text()", encoding="utf-8"
    )
    result = run_profile(
        tmp_path,
        {"profile": "cli", "artifacts": ["probe.py"], "checks": [{"command": py("probe.py")}]},
    )
    assert not result["profile_passed"]
    assert "FileNotFoundError" in result["consumer_profile_report"]["checks"][0]["output"]


def test_csv_result_checks_columns_rows_and_values(tmp_path):
    (tmp_path / "emit.py").write_text(
        "from pathlib import Path; Path('output.csv').write_text('name,value\\nsample,4\\n')",
        encoding="utf-8",
    )
    result = run_profile(
        tmp_path,
        {
            "profile": "data",
            "artifacts": ["emit.py"],
            "checks": [{"command": py("emit.py")}],
            "result_checks": [
                {
                    "path": "output.csv",
                    "format": "csv",
                    "columns": ["name", "value"],
                    "min_rows": 1,
                    "expected_rows": [{"name": "sample", "value": "4"}],
                }
            ],
        },
    )
    assert result["profile_passed"], result


def test_multiple_checks_consume_one_deadline(tmp_path):
    (tmp_path / "slow.py").write_text("import time; time.sleep(0.3)", encoding="utf-8")
    before = time.monotonic()
    result = run_profile(
        tmp_path,
        {
            "profile": "cli",
            "artifacts": ["slow.py"],
            "checks": [{"command": py("slow.py")}, {"command": py("slow.py")}],
        },
        timeout=0.5,
    )
    assert not result["profile_passed"]
    assert time.monotonic() - before < 4


def test_consumer_cannot_rewrite_runtime_probe_and_claim_a_pass(tmp_path):
    (tmp_path / "tamper.py").write_text(
        "from pathlib import Path; Path('.consumer-import.py').write_text('print(\"consumer import completed\")')",
        encoding="utf-8",
    )
    (tmp_path / "product.py").write_text("raise RuntimeError('not usable')", encoding="utf-8")
    result = run_profile(
        tmp_path,
        {
            "profile": "library",
            "artifacts": ["product.py", "tamper.py"],
            "python_module": "product",
            "checks": [{"command": py("tamper.py")}],
        },
    )
    assert not result["profile_passed"]
    assert result["consumer_profile_report"]["runtime_metadata_unchanged"] is False
