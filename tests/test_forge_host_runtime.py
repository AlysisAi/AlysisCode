from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from alysis_code import cli as cli_mod
from alysis_code.agent import session as agent_session_mod
from alysis_code.cli_impl import forge as forge_impl
from alysis_code.config import AppConfig, clone_cfg, load_config
from alysis_code.forge import load_plan
from alysis_code.forge_host_client import ForgeHostInProcessClient
from alysis_code.forge_host_native_adapter import ForgeHostPreflightError
from alysis_code.forge_host_protocol.codec import encode_envelope, validate_envelope
from alysis_code.forge_host_protocol.models import (
    ApprovalDecision,
    HandshakeResponse,
    PlannerGoal,
    PlannerInput,
    RequestEnvelope,
    RunStart,
)
from alysis_code.forge_host_runtime import (
    ForgeHostProviderAuthority,
    build_local_forge_host_runtime,
    build_local_forge_host_runtime_for_existing_plan,
    resolve_local_trusted_pytest,
)
from alysis_code.forge_planner_engine import ForgePlannerDependencies
from alysis_code.llm.types import (
    BillingMode,
    LLMResponse,
    LLMUsage,
    ToolCall,
    UsageConfidence,
    UsageContract,
)
from alysis_code.plan_assistant import PlannerTurnResult
from alysis_code.plan_reconciliation import PlanReconciliationResult
from alysis_code.profiles import ProfileSpec

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", os.fspath(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    trusted_pytest = tmp_path / "bin" / "pytest"
    trusted_pytest.parent.mkdir()
    trusted_pytest.write_text(
        f"#!{sys.executable}\nfrom pytest import console_main\nraise SystemExit(console_main())\n",
        encoding="utf-8",
    )
    trusted_pytest.chmod(0o755)
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "base.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_feature.py").write_text(
        "from pathlib import Path\n\n"
        "def test_feature():\n"
        "    source = Path(__file__).parents[1] / 'src' / 'feature.py'\n"
        "    assert source.read_text(encoding='utf-8') == 'VALUE = 2\\n'\n",
        encoding="utf-8",
    )
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "add", "src", "tests")
    _git(root, "commit", "-m", "initial")
    return root, trusted_pytest


def _request(payload: object, *, message: str, operation: str) -> RequestEnvelope:
    return RequestEnvelope(
        protocol="alysis.forge-host",
        protocol_version="2.0",
        message_id=f"msg_{message}",
        operation_id=operation,
        sent_at=NOW,
        kind="request",
        request_id=f"req_{message}",
        idempotency_key=f"idem_{message}",
        payload=payload,
    )


class _SilentConsole:
    def print(self, *_args: object, **_kwargs: object) -> None:
        return

    def rule(self, *_args: object, **_kwargs: object) -> None:
        return


class _Knowledge:
    def render_prompt_section(self, *, workspace_root: Path) -> str:
        return f"knowledge:{workspace_root.name}"


class _ScriptedForgeClient:
    temperature = 0.2
    usage_counts_authoritative = True
    usage_contract = UsageContract(
        response_usage_confidence=UsageConfidence.AUTHORITATIVE,
        billing_mode=BillingMode.METERED_API,
    )

    def __init__(
        self,
        *,
        model: str,
        provider: str,
        protocol: str,
        base_url: str,
    ) -> None:
        self.model = model
        self.provider_key = provider
        self.protocol = protocol
        self.base_url = base_url
        self.calls: list[dict[str, object]] = []
        usage = LLMUsage(
            prompt_tokens=11,
            completion_tokens=7,
            total_tokens=18,
            provider_cost_usd=0.0,
            provider_cost_currency="USD",
            confidence=UsageConfidence.AUTHORITATIVE,
        )
        self._responses = [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tool_write_feature",
                        name="fs_write",
                        arguments={"path": "src/feature.py", "content": "VALUE = 2\n"},
                    )
                ],
                raw={},
                response_model=model,
                usage=usage,
            ),
            LLMResponse(
                content="Implemented src/feature.py for the trusted Host verification gate.",
                tool_calls=[],
                raw={},
                response_model=model,
                usage=usage,
            ),
            LLMResponse(
                content=(
                    "Re-read the task: src/feature.py contains VALUE = 2 and no output "
                    "outside the approved scope was created."
                ),
                tool_calls=[],
                raw={},
                response_model=model,
                usage=usage,
            ),
        ]

    def chat(
        self,
        *,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tools,
                "tool_choice": tool_choice,
                "stream": stream,
                "temperature": temperature,
            }
        )
        if not self._responses:
            raise AssertionError("the real Forge loop exceeded the scripted provider budget")
        return self._responses.pop(0)


def test_provider_authority_rejects_subscription_profile_until_auth_handle_is_bound() -> None:
    cfg = AppConfig(model="gpt-codex-test", llm_reasoning_effort="high")
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        auth_provider="openai-codex",
        default_model="gpt-codex-test",
        reasoning_effort="high",
    )
    cfg.extra_fields = {
        "profiles": {profile.name: profile.to_dict()},
        "active_profile": profile.name,
        "onboarded": True,
    }

    with pytest.raises(ForgeHostPreflightError) as exc_info:
        ForgeHostProviderAuthority.capture(
            config_loader=lambda: clone_cfg(cfg),
            console=_SilentConsole(),
        )

    assert exc_info.value.code == "subscription_provider_deferred"
    assert str(exc_info.value) == "Forge Host Phase 0 requires a generation-bound API credential."


def test_provider_authority_is_repr_safe_and_rejects_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    secret = "runtime-secret-marker"
    current = clone_cfg(load_config())
    current.model = "gpt-4.1-mini"
    authority = ForgeHostProviderAuthority.capture(
        config_loader=lambda: clone_cfg(current),
        console=_SilentConsole(),
        api_key_override=secret,
        credential_source="test:runtime-authority",
        account_fingerprint="acct_runtime_authority",
        base_environment={"PRIVATE_MARKER": "do-not-render"},
        generation_nonce="runtime-generation",
    )

    rendered = repr(authority)
    assert secret not in rendered
    assert "do-not-render" not in rendered
    assert authority.binding.model not in rendered
    assert repr(authority._runtime_identity) == (  # noqa: SLF001
        "HostProviderRuntimeIdentity(<opaque>)"
    )
    assert authority.current_provider_binding() == authority.binding

    current.model = "gpt-4.1"
    with pytest.raises(ForgeHostPreflightError, match="changed"):
        authority.current_provider_binding()


def test_provider_authority_suppresses_loader_causes_and_url_userinfo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    marker = "CFG_SECRET at /Users/private/config.json"

    def broken_loader():  # type: ignore[no-untyped-def]
        raise RuntimeError(marker)

    try:
        ForgeHostProviderAuthority.capture(
            config_loader=broken_loader,
            console=_SilentConsole(),
            api_key_override="private-key",
        )
    except ForgeHostPreflightError as exc:
        rendered = "".join(traceback.format_exception(exc))
    else:  # pragma: no cover - explicit branch keeps traceback assertion local
        raise AssertionError("the broken provider loader was accepted")
    assert marker not in rendered
    assert "/Users/private" not in rendered

    unsafe = clone_cfg(load_config())
    unsafe.model = "gpt-4.1-mini"
    active_profile = str(unsafe.extra_fields["active_profile"])
    unsafe.extra_fields["profiles"][active_profile]["base_url"] = (
        "https://runtime-user:URL_SECRET@api.openai.com/v1"
    )
    with pytest.raises(ForgeHostPreflightError, match="invalid"):
        ForgeHostProviderAuthority.capture(
            config_loader=lambda: clone_cfg(unsafe),
            console=_SilentConsole(),
            api_key_override="private-key",
            credential_source="test:url-userinfo",
        )


def test_provider_revalidation_and_pytest_errors_do_not_reflect_private_causes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    config = clone_cfg(load_config())
    config.model = "gpt-4.1-mini"
    calls = 0

    def drifting_loader():  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("FRESH_SECRET at /Users/private/provider.json")
        return clone_cfg(config)

    authority = ForgeHostProviderAuthority.capture(
        config_loader=drifting_loader,
        console=_SilentConsole(),
        api_key_override="private-key",
        credential_source="test:loader-drift",
    )
    try:
        authority.current_provider_binding()
    except ForgeHostPreflightError as exc:
        rendered = "".join(traceback.format_exception(exc))
    else:  # pragma: no cover
        raise AssertionError("the unavailable fresh provider binding was accepted")
    assert "FRESH_SECRET" not in rendered
    assert "/Users/private" not in rendered

    missing = Path("/private/tmp/PRIVATE_PYTEST_SECRET/pytest")
    try:
        resolve_local_trusted_pytest(missing)
    except ForgeHostPreflightError as exc:
        rendered = "".join(traceback.format_exception(exc))
    else:  # pragma: no cover
        raise AssertionError("the missing trusted pytest path was accepted")
    assert "PRIVATE_PYTEST_SECRET" not in rendered
    assert "/private/tmp" not in rendered


def test_trusted_pytest_resolution_prefers_explicit_engine_binding(tmp_path: Path) -> None:
    executable = tmp_path / "pytest-host"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    assert (
        resolve_local_trusted_pytest(
            environment={"ALYSIS_FORGE_TRUSTED_PYTEST": os.fspath(executable)}
        )
        == executable.resolve()
    )
    with pytest.raises(ForgeHostPreflightError, match="explicitly trusted"):
        resolve_local_trusted_pytest(environment={})


def test_hidden_stdio_entrypoint_starts_the_same_runtime_and_negotiates(
    tmp_path: Path,
) -> None:
    root, trusted_pytest = _repo(tmp_path)
    handshake_fixture = (
        Path(__file__).parents[1]
        / "schemas"
        / "forge-host-v2"
        / "fixtures"
        / "valid"
        / "handshake-request.json"
    ).read_bytes()
    handshake = encode_envelope(validate_envelope(handshake_fixture))
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.fspath(Path(__file__).parents[1] / "src"),
            "ALYSIS_API_KEY": "stdio-entrypoint-key",
            "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "stdio-config"),
            "ALYSIS_DATA_DIR": os.fspath(tmp_path / "stdio-data"),
        }
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "alysis_code.cli",
            "forge",
            "_host-stdio",
            "--path",
            os.fspath(root),
            "--trusted-pytest",
            os.fspath(trusted_pytest),
            "--model",
            "gpt-4.1-mini",
        ],
        input=handshake + b"\n",
        capture_output=True,
        env=environment,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert completed.stderr == b""
    lines = completed.stdout.splitlines()
    assert len(lines) == 1
    response = validate_envelope(lines[0])
    assert isinstance(response.payload, HandshakeResponse)
    assert response.payload.identity.run_id is None


def test_existing_plan_runtime_reissues_approval_without_a_second_planner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    root, trusted_pytest = _repo(tmp_path)
    effective = clone_cfg(load_config())
    effective.model = "gpt-4.1-mini"
    planner_input = "Build the scoped feature module in src/feature.py."
    planner_calls: list[str] = []

    def synthetic_planner(**kwargs: Any) -> PlannerTurnResult:
        planner_calls.append(str(kwargs["transcript_tail"][-1]["content"]))
        return PlannerTurnResult(
            assistant_message="The scoped implementation task is ready.",
            questions=[],
            plan_update={
                "project_goal": "Build a feature module.",
                "summary": "Add and verify one scoped module.",
                "requirements_append": ["Focused tests pass."],
                "tasks_add": [
                    {
                        "title": "Implement feature",
                        "description": "Create the scoped feature module.",
                        "acceptance_criteria": ["Focused tests pass."],
                        "dependencies": [],
                        "estimated_files": ["src/feature.py"],
                        "write_scope": ["src/feature.py"],
                    }
                ],
            },
        )

    planner_dependencies = ForgePlannerDependencies(
        turn_runner=synthetic_planner,
        knowledge_preparer=lambda **_kwargs: _Knowledge(),
        knowledge_root_resolver=lambda paths: paths.root,
        plan_reconciler=lambda **_kwargs: (
            PlanReconciliationResult(changed=False, warnings=[], task_updates={}),
            {},
        ),
    )

    def execution_must_not_start(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        raise AssertionError("admission-only test unexpectedly started execution")

    first_runtime = build_local_forge_host_runtime(
        root=root,
        cli_module=cli_mod,
        console=_SilentConsole(),
        trusted_pytest=trusted_pytest,
        config_loader=lambda: clone_cfg(effective),
        api_key_override="existing-plan-key",
        credential_source="test:existing-plan",
        account_fingerprint="acct_existing_plan",
        verification_commands=("pytest -q",),
        declared_scope_kinds={"src/feature.py": "file"},
        planner_dependencies=planner_dependencies,
        dependency_factory=execution_must_not_start,
        clock=lambda: NOW,
        session_id="ses_existingplan1",
        generation_id="gen_existingplan1",
    )
    first = ForgeHostInProcessClient(first_runtime.service, clock=lambda: NOW).plan_until_ready(
        planner_input,
        answer_provider=lambda _question: "",
    )
    persisted_plan = load_plan(first_runtime.paths)

    second_runtime = build_local_forge_host_runtime_for_existing_plan(
        root=root,
        planner_input=planner_input,
        expected_host_plan_fingerprint=first.plan_ready.host_plan_fingerprint,
        expected_native_approval_fingerprint=first.plan_ready.native_approval_fingerprint,
        cli_module=cli_mod,
        console=_SilentConsole(),
        trusted_pytest=trusted_pytest,
        config_loader=lambda: clone_cfg(effective),
        api_key_override="existing-plan-key",
        credential_source="test:existing-plan",
        account_fingerprint="acct_existing_plan",
        verification_commands=("pytest -q",),
        declared_scope_kinds={"src/feature.py": "file"},
        dependency_factory=execution_must_not_start,
        clock=lambda: NOW,
        session_id="ses_existingplan2",
        generation_id="gen_existingplan2",
    )
    second = ForgeHostInProcessClient(second_runtime.service, clock=lambda: NOW).plan_until_ready(
        "Approve the exact persisted Forge plan for execution.",
        answer_provider=lambda _question: "",
    )

    assert planner_calls == [planner_input]
    assert load_plan(second_runtime.paths) == persisted_plan
    assert second.plan_ready.host_plan_fingerprint == first.plan_ready.host_plan_fingerprint
    assert (
        second.plan_ready.native_approval_fingerprint
        == first.plan_ready.native_approval_fingerprint
    )
    assert second.approval_event.payload.type == "approval.required"
    assert (
        second.plan_ready.execution_admission.provider_binding_ref
        != first.plan_ready.execution_admission.provider_binding_ref
    )


def test_canonical_runtime_runs_real_planner_approval_agent_pytest_git_and_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_REMOTE_SYNC", "off")
    root, trusted_pytest = _repo(tmp_path)
    effective = clone_cfg(load_config())
    effective.model = "gpt-4.1-mini"
    credential = "synthetic-runtime-key"
    credential_source = "test:canonical-runtime"
    planner_calls: list[dict[str, Any]] = []

    def synthetic_planner(**kwargs: Any) -> PlannerTurnResult:
        planner_calls.append(dict(kwargs))
        return PlannerTurnResult(
            assistant_message="The scoped implementation task is ready.",
            questions=[],
            plan_update={
                "project_goal": "Build a feature module.",
                "summary": "Add and verify one scoped module.",
                "requirements_append": ["Focused tests pass."],
                "tasks_add": [
                    {
                        "title": "Implement feature",
                        "description": "Create the scoped feature module.",
                        "acceptance_criteria": ["Focused tests pass."],
                        "dependencies": [],
                        "estimated_files": ["src/feature.py"],
                        "write_scope": ["src/feature.py"],
                    }
                ],
            },
        )

    planner_dependencies = ForgePlannerDependencies(
        turn_runner=synthetic_planner,
        knowledge_preparer=lambda **_kwargs: _Knowledge(),
        knowledge_root_resolver=lambda paths: paths.root,
        plan_reconciler=lambda **_kwargs: (
            PlanReconciliationResult(changed=False, warnings=[], task_updates={}),
            {},
        ),
    )
    clients: list[_ScriptedForgeClient] = []
    client_factory_calls: list[dict[str, object]] = []

    def scripted_client_factory(**kwargs: Any) -> _ScriptedForgeClient:
        client_factory_calls.append(dict(kwargs))
        identity = forge_impl.derive_host_provider_runtime_identity(
            kwargs["cfg"],
            account_fingerprint="acct_runtime_e2e",
            credential_value=str(kwargs["api_key"]),
            credential_source=credential_source,
        )
        client = _ScriptedForgeClient(
            model=str(kwargs["model"]),
            provider=identity.provider,
            protocol=identity.protocol,
            base_url=identity.normalized_base_url,
        )
        clients.append(client)
        return client

    monkeypatch.setattr(agent_session_mod, "make_llm_client", scripted_client_factory)
    runtime = build_local_forge_host_runtime(
        root=root,
        cli_module=cli_mod,
        console=_SilentConsole(),
        trusted_pytest=trusted_pytest,
        config_loader=lambda: clone_cfg(effective),
        api_key_override=credential,
        credential_source=credential_source,
        account_fingerprint="acct_runtime_e2e",
        base_environment={"PYTHONDONTWRITEBYTECODE": "1"},
        per_managed_task_max_steps=12,
        run_timeout_ms=60_000,
        verification_commands=("pytest -q",),
        declared_scope_kinds={"src/feature.py": "file"},
        planner_dependencies=planner_dependencies,
        clock=lambda: NOW,
        host_run_id_factory=lambda: "run_runtimee2e1",
        session_id="ses_runtimee2e1",
        generation_id="gen_runtimee2e1",
    )
    assert credential not in repr(runtime.planner._settings)  # noqa: SLF001
    real_run_agent = forge_impl.run_agent
    agent_calls: list[dict[str, object]] = []

    def observed_agent(**kwargs: Any) -> int:
        agent_calls.append(
            {
                "model": kwargs["cfg"].model,
                "max_steps": kwargs["max_steps"],
                "no_log": kwargs["no_log"],
                "deadline": kwargs["execution_deadline"],
            }
        )
        return int(real_run_agent(**kwargs))

    monkeypatch.setattr(forge_impl, "run_agent", observed_agent)

    initial_identity = runtime.service.identity
    plan_request = _request(
        PlannerInput(
            type="planner.input",
            identity=initial_identity,
            expected_revision=initial_identity.revision,
            input=PlannerGoal(
                kind="goal",
                text="Build the scoped feature module in src/feature.py.",
            ),
        ),
        message="planner0001",
        operation="operation_planner01",
    )
    plan_outcome = runtime.service.handle_request(plan_request)
    plan_event = next(event for event in plan_outcome.events if event.payload.type == "plan.ready")
    approval_event = next(
        event for event in plan_outcome.events if event.payload.type == "approval.required"
    )
    approval = approval_event.payload
    runtime.service.handle_request(
        _request(
            ApprovalDecision(
                type="approval.decision",
                identity=runtime.service.identity,
                expected_revision=runtime.service.identity.revision,
                approval_operation_id=approval_event.operation_id,
                approval_id=approval.approval_id,
                action_fingerprint=approval.action_fingerprint,
                scope_fingerprint=approval.scope.fingerprint,
                expires_at=approval.expires_at,
                decision="allow_once",
            ),
            message="approval001",
            operation="operation_decide01",
        )
    )
    ready = plan_event.payload
    run_request = _request(
        RunStart(
            type="run.start",
            identity=runtime.service.identity,
            expected_revision=runtime.service.identity.revision,
            host_plan_fingerprint=ready.host_plan_fingerprint,
            native_approval_fingerprint=ready.native_approval_fingerprint,
            verification_contract_fingerprint=ready.verification_contract.contract_fingerprint,
            execution_admission=ready.execution_admission,
        ),
        message="runstart001",
        operation="operation_runstart1",
    )

    first = runtime.service.handle_request(run_request)
    replay = runtime.service.handle_request(run_request)

    assert len(planner_calls) == 1
    assert planner_calls[0]["api_key_override"] == credential
    assert planner_calls[0]["planning_profile"] == "quick"
    assert len(agent_calls) == 1
    assert agent_calls[0]["model"] == runtime.provider_authority.binding.model
    assert agent_calls[0]["max_steps"] == 12
    assert agent_calls[0]["no_log"] is False
    assert len(client_factory_calls) == len(clients) >= 1
    assert all(call["api_key"] == credential for call in client_factory_calls)
    active_clients = [client for client in clients if client.calls]
    assert len(active_clients) == 1
    # One tool turn and one final answer; the optional spec-faithfulness pass only runs
    # while background processes are still alive.
    assert len(active_clients[0].calls) == 2
    assert replay.replayed is True
    assert replay.event_bytes == first.event_bytes
    terminal = first.events[-1].payload.result
    assert terminal.status == "succeeded"
    assert terminal.exit_code == 0
    assert terminal.usage.model_calls == 2
    verification = next(
        item for item in first.events[-2].payload.evidence if item.role == "verification"
    )
    assert verification.real_execution is True
    assert _git(root, "show", "refs/heads/main:src/feature.py") == "VALUE = 2"
    assert load_plan(runtime.paths)["tasks"][0]["status"] == "done"
    journals = list((runtime.paths.execution_integration_dir / "transactions").glob("*.json"))
    assert len(journals) == 1
    journal = json.loads(journals[0].read_text(encoding="utf-8"))
    assert journal["state"] == "SEALED"
