from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from alysis_code.config import AppConfig, ConfigError, set_config_value
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.workspace_provisioning import (
    PACKAGE_MISSING_EXIT_CODE,
    DeclaredTestRunner,
    ProvisioningAction,
    ProvisioningDecision,
    ShellProbeResult,
    _workspace_provisioning_enabled,
    build_import_probe_command,
    build_provisioning_command,
    detect_declared_test_runner,
    ini_has_section,
    probe_runner_importable,
    provision_test_runner,
    provisioning_already_attempted,
    reset_provisioning_attempts,
    resolve_provisioning_decision,
    runtime_kind_provisions_autonomously,
)


@pytest.fixture(autouse=True)
def _clear_attempt_ledger():
    reset_provisioning_attempts()
    yield
    reset_provisioning_attempts()


def _declared(config_file: str = "pytest.ini") -> DeclaredTestRunner:
    return DeclaredTestRunner(package="pytest", trigger_config_file=config_file, evidence="test")


class _RecordingShell:
    """A shell command runner that records every command it is asked to run."""

    def __init__(self, *, results: dict[str, ShellProbeResult] | None = None) -> None:
        self.results = results or {}
        self.commands: list[str] = []

    def __call__(self, command: str, timeout_s: float) -> ShellProbeResult:
        self.commands.append(command)
        for fragment, result in self.results.items():
            if fragment in command:
                return result
        return ShellProbeResult(exit_code=0)


def _unavailable_shell(command: str, timeout_s: float) -> ShellProbeResult:
    return ShellProbeResult(exit_code=None, stderr="shell execution is unavailable")


# ---------------------------------------------------------------------------
# Test 1: each config-file trigger is detected, and nothing else is
# ---------------------------------------------------------------------------


def test_pytest_ini_presence_is_a_declaration(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("", encoding="utf-8")
    declared = detect_declared_test_runner(tmp_path)
    assert declared is not None
    assert declared.package == "pytest"
    assert declared.trigger_config_file == "pytest.ini"
    assert declared.evidence == "pytest_ini_present"


def test_pyproject_ini_options_is_a_declaration(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        encoding="utf-8",
    )
    declared = detect_declared_test_runner(tmp_path)
    assert declared is not None
    assert declared.trigger_config_file == "pyproject.toml"
    assert declared.evidence == "tool.pytest.ini_options"


def test_tox_ini_pytest_section_is_a_declaration(tmp_path: Path) -> None:
    (tmp_path / "tox.ini").write_text(
        "[tox]\nenvlist = py311\n\n[pytest]\naddopts = -q\n", encoding="utf-8"
    )
    declared = detect_declared_test_runner(tmp_path)
    assert declared is not None
    assert declared.trigger_config_file == "tox.ini"
    assert declared.evidence == "[pytest]"


def test_setup_cfg_tool_pytest_section_is_a_declaration(tmp_path: Path) -> None:
    (tmp_path / "setup.cfg").write_text(
        "[metadata]\nname = x\n\n[tool:pytest]\naddopts = -q\n", encoding="utf-8"
    )
    declared = detect_declared_test_runner(tmp_path)
    assert declared is not None
    assert declared.trigger_config_file == "setup.cfg"
    assert declared.evidence == "[tool:pytest]"


def test_no_trigger_detects_nothing(tmp_path: Path) -> None:
    assert detect_declared_test_runner(tmp_path) is None


def test_python_project_without_pytest_config_is_not_a_declaration(tmp_path: Path) -> None:
    # Conservative on purpose: a pyproject and a tests/ directory say tests
    # exist, not that the repo declared pytest as its runner.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n[tool.poetry]\nname = "x"\n', encoding="utf-8"
    )
    (tmp_path / "tox.ini").write_text("[tox]\nenvlist = py311\n", encoding="utf-8")
    (tmp_path / "setup.cfg").write_text("[metadata]\nname = x\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_thing.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    assert detect_declared_test_runner(tmp_path) is None


def test_pytest_ini_wins_over_other_config_files(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "tox.ini").write_text("[pytest]\n", encoding="utf-8")
    declared = detect_declared_test_runner(tmp_path)
    assert declared is not None
    assert declared.trigger_config_file == "pytest.ini"


def test_malformed_config_degrades_to_not_declared(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("this is not [valid toml", encoding="utf-8")
    (tmp_path / "setup.cfg").write_text("\x00\x01 garbage [tool:pytest\n", encoding="utf-8")
    assert detect_declared_test_runner(tmp_path) is None


def test_section_header_inside_a_multiline_value_is_not_a_declaration(tmp_path: Path) -> None:
    # An indented line continues the previous value in INI, so this repo never
    # declared pytest -- reading it as one would invent a declaration.
    (tmp_path / "tox.ini").write_text(
        "[tox]\ncommands =\n    echo\n    [pytest]\n    echo done\n", encoding="utf-8"
    )
    assert detect_declared_test_runner(tmp_path) is None


@pytest.mark.parametrize(
    "text,section,expected",
    [
        ("[pytest]\n", "pytest", True),
        ("[pytest]  \n", "pytest", True),
        ("[tox]\n[pytest]\nx=1\n", "pytest", True),
        ("# [pytest]\n", "pytest", False),
        ("; [pytest]\n", "pytest", False),
        ("[pytest-extra]\n", "pytest", False),
        ("addopts = [pytest]\n", "pytest", False),
        ("commands =\n    [pytest]\n", "pytest", False),
        ("commands =\n\t[pytest]\n", "pytest", False),
        ("[tool:pytest]\n", "tool:pytest", True),
        ("", "pytest", False),
    ],
)
def test_ini_section_scan(text: str, section: str, expected: bool) -> None:
    assert ini_has_section(text, section) is expected


# ---------------------------------------------------------------------------
# Test 2: the import probe runs in the agent's command environment
# ---------------------------------------------------------------------------


def test_import_probe_uses_the_shell_not_the_agent_interpreter() -> None:
    shell = _RecordingShell(results={"find_spec": ShellProbeResult(exit_code=0)})
    assert probe_runner_importable("pytest", run_command=shell) is True
    assert len(shell.commands) == 1
    assert '"$PY" -c ' in shell.commands[0]
    assert "find_spec" in shell.commands[0]
    assert "'pytest'" in shell.commands[0]


def test_import_probe_reports_missing_only_on_the_sentinel_exit_code() -> None:
    shell = _RecordingShell(
        results={"find_spec": ShellProbeResult(exit_code=PACKAGE_MISSING_EXIT_CODE)}
    )
    assert probe_runner_importable("pytest", run_command=shell) is False


@pytest.mark.parametrize("exit_code", [1, 2, 125, 126, 127])
def test_import_probe_reports_unknown_when_the_shell_itself_failed(exit_code: int) -> None:
    # A broken sandbox backend or a missing interpreter is not evidence that the
    # package is absent; installing on that basis would target an unprobed env.
    shell = _RecordingShell(results={"find_spec": ShellProbeResult(exit_code=exit_code)})
    assert probe_runner_importable("pytest", run_command=shell) is None


def test_import_probe_reports_unknown_when_the_shell_cannot_run() -> None:
    assert probe_runner_importable("pytest", run_command=_unavailable_shell) is None


def test_unknown_importability_never_installs() -> None:
    decision = resolve_provisioning_decision(
        declared=_declared(),
        importable=None,
        runtime_kind=RuntimeKind.ONE_SHOT,
        enabled=True,
    )
    assert decision.action is ProvisioningAction.SKIP
    assert decision.reason == "import_probe_unavailable"


# ---------------------------------------------------------------------------
# Test 3: the provisioning decision (pure)
# ---------------------------------------------------------------------------


def test_top_level_autonomous_run_with_missing_runner_provisions() -> None:
    decision = resolve_provisioning_decision(
        declared=_declared(), importable=False, runtime_kind=RuntimeKind.ONE_SHOT, enabled=True
    )
    assert decision.action is ProvisioningAction.PROVISION
    assert decision.package == "pytest"
    assert decision.trigger_config_file == "pytest.ini"


@pytest.mark.parametrize(
    "kind",
    (
        RuntimeKind.ONE_SHOT,
        RuntimeKind.SUBAGENT,
    ),
)
def test_importable_runner_is_never_touched(kind: RuntimeKind) -> None:
    decision = resolve_provisioning_decision(
        declared=_declared(), importable=True, runtime_kind=kind, enabled=True
    )
    assert decision.action is ProvisioningAction.SKIP
    assert decision.reason == "declared_test_runner_importable"


def test_interactive_run_reports_the_gap_instead_of_installing() -> None:
    decision = resolve_provisioning_decision(
        declared=_declared(),
        importable=False,
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        enabled=True,
    )
    assert decision.action is ProvisioningAction.REPORT_GAP
    assert decision.reason == "interactive_run_does_not_install"


def test_nested_runtime_kinds_never_install() -> None:
    # A subagent can be spawned from an interactive chat. If it installed, the
    # interactive "never install" rule would be trivially bypassable.
    kind = RuntimeKind.SUBAGENT
    decision = resolve_provisioning_decision(
        declared=_declared(), importable=False, runtime_kind=kind, enabled=True
    )
    assert decision.action is not ProvisioningAction.PROVISION
    assert runtime_kind_provisions_autonomously(kind) is False


def test_subagent_depth_never_installs_even_for_an_autonomous_kind() -> None:
    decision = resolve_provisioning_decision(
        declared=_declared(),
        importable=False,
        runtime_kind=RuntimeKind.ONE_SHOT,
        enabled=True,
        subagent_depth=1,
    )
    assert decision.action is ProvisioningAction.SKIP
    assert decision.reason == "nested_session_defers_to_parent"


def test_no_declaration_means_no_action() -> None:
    decision = resolve_provisioning_decision(
        declared=None, importable=False, runtime_kind=RuntimeKind.ONE_SHOT, enabled=True
    )
    assert decision.action is ProvisioningAction.SKIP
    assert decision.reason == "no_declared_test_runner"


def test_kill_switch_reverts_to_legacy() -> None:
    decision = resolve_provisioning_decision(
        declared=_declared(),
        importable=False,
        runtime_kind=RuntimeKind.ONE_SHOT,
        enabled=False,
    )
    assert decision.action is ProvisioningAction.SKIP
    assert decision.reason == "workspace_provisioning_disabled"


def test_second_pass_reports_instead_of_installing_again() -> None:
    decision = resolve_provisioning_decision(
        declared=_declared(),
        importable=False,
        runtime_kind=RuntimeKind.ONE_SHOT,
        enabled=True,
        already_attempted=True,
    )
    assert decision.action is ProvisioningAction.REPORT_GAP
    assert decision.reason == "provisioning_already_attempted"


def test_unknown_runtime_kind_never_installs() -> None:
    decision = resolve_provisioning_decision(
        declared=_declared(),
        importable=False,
        runtime_kind="something_new",
        enabled=True,
    )
    assert decision.action is ProvisioningAction.REPORT_GAP


# ---------------------------------------------------------------------------
# Test 4: execution -- exactly one package, exactly one attempt, via the shell
# ---------------------------------------------------------------------------


def test_provisioning_command_installs_only_the_declared_package() -> None:
    command = build_provisioning_command("pytest")
    assert command.endswith('"$PY" -m pip install pytest --quiet')
    # Resolves python3 when python is absent, so the feature is not a silent
    # no-op on hosts that ship only python3.
    assert "command -v python" in command
    # No upgrade, no reinstall, no other package, no interpreter path of ours.
    assert "--upgrade" not in command
    assert "--force-reinstall" not in command
    probe = build_import_probe_command("pytest")
    assert '"$PY" -c ' in probe
    assert "find_spec" in probe and "'pytest'" in probe
    assert str(PACKAGE_MISSING_EXIT_CODE) in probe


def test_successful_provisioning_runs_through_the_shell() -> None:
    shell = _RecordingShell()
    decision = ProvisioningDecision(
        ProvisioningAction.PROVISION, "declared_test_runner_missing", "pytest", "pytest.ini"
    )
    outcome = provision_test_runner(decision, run_command=shell)
    assert outcome is not None
    assert outcome.success is True
    assert outcome.package == "pytest"
    assert outcome.trigger_config_file == "pytest.ini"
    assert len(shell.commands) == 1
    assert shell.commands[0].endswith('"$PY" -m pip install pytest --quiet')


def test_failed_provisioning_is_never_retried() -> None:
    shell = _RecordingShell(
        results={"pip install": ShellProbeResult(exit_code=1, stderr="no network")}
    )
    decision = ProvisioningDecision(
        ProvisioningAction.PROVISION, "declared_test_runner_missing", "pytest", "pytest.ini"
    )
    first = provision_test_runner(decision, run_command=shell)
    assert first is not None
    assert first.success is False
    assert first.exit_code == 1
    assert "no network" in first.error
    assert provisioning_already_attempted("pytest") is True

    second = provision_test_runner(decision, run_command=shell)
    assert second is None, "a second caller must not report an install it did not run"
    assert len(shell.commands) == 1, "a failed install must not be retried"


def test_a_losing_concurrent_caller_reports_nothing() -> None:
    shell = _RecordingShell()
    decision = ProvisioningDecision(
        ProvisioningAction.PROVISION, "declared_test_runner_missing", "pytest", "pytest.ini"
    )
    assert provision_test_runner(decision, run_command=shell) is not None
    # The second worker did not install and did not fail; it has nothing to say.
    assert provision_test_runner(decision, run_command=shell) is None


def test_provisioning_reports_an_unavailable_shell_as_a_failure() -> None:
    decision = ProvisioningDecision(
        ProvisioningAction.PROVISION, "declared_test_runner_missing", "pytest", "pytest.ini"
    )
    outcome = provision_test_runner(decision, run_command=_unavailable_shell)
    assert outcome is not None
    assert outcome.success is False
    assert "unavailable" in outcome.error


def test_env_provisioned_payload_shape() -> None:
    shell = _RecordingShell()
    decision = ProvisioningDecision(
        ProvisioningAction.PROVISION, "declared_test_runner_missing", "pytest", "pyproject.toml"
    )
    outcome = provision_test_runner(decision, run_command=shell)
    assert outcome is not None
    payload = outcome.payload()
    assert payload["package"] == "pytest"
    assert payload["trigger_config_file"] == "pyproject.toml"
    assert payload["success"] is True
    assert payload["exit_code"] == 0
    assert payload["command"].endswith('"$PY" -m pip install pytest --quiet')


# ---------------------------------------------------------------------------
# Test 5: kill-switch plumbing
# ---------------------------------------------------------------------------


def test_kill_switch_env_and_config(monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_WORKSPACE_PROVISIONING", raising=False)
    assert (
        _workspace_provisioning_enabled(AppConfig(model="x", workspace_provisioning_enabled=True))
        is True
    )
    assert (
        _workspace_provisioning_enabled(AppConfig(model="x", workspace_provisioning_enabled=False))
        is False
    )
    monkeypatch.setenv("ALYSIS_WORKSPACE_PROVISIONING", "off")
    assert (
        _workspace_provisioning_enabled(AppConfig(model="x", workspace_provisioning_enabled=True))
        is False
    )
    monkeypatch.setenv("ALYSIS_WORKSPACE_PROVISIONING", "on")
    assert (
        _workspace_provisioning_enabled(AppConfig(model="x", workspace_provisioning_enabled=False))
        is True
    )


def test_kill_switch_config_key_roundtrip() -> None:
    cfg = AppConfig(model="x")
    assert cfg.workspace_provisioning_enabled is True
    set_config_value(cfg, "workspace_provisioning_enabled", "off")
    assert cfg.workspace_provisioning_enabled is False
    set_config_value(cfg, "workspace_provisioning_enabled", "true")
    assert cfg.workspace_provisioning_enabled is True
    with pytest.raises(ConfigError):
        set_config_value(cfg, "workspace_provisioning_enabled", "maybe")


# ---------------------------------------------------------------------------
# Test 6: the session warmup hook
# ---------------------------------------------------------------------------


def _session_events(sessions_dir: Path, session_id: str, event_type: str) -> list[dict[str, Any]]:
    from alysis_code.session_store import read_session_events

    return [
        event.get("payload") or {}
        for event in read_session_events(sessions_dir / f"{session_id}.jsonl")
        if str(event.get("type") or "") == event_type
    ]


class _StubShellRunner:
    """Stands in for the session's shell runner, recording what it is asked to run."""

    def __init__(self, *, import_exit_code: int = 0, install_exit_code: int = 0) -> None:
        self.import_exit_code = import_exit_code
        self.install_exit_code = install_exit_code
        self.commands: list[str] = []

    def run(self, *, root: Path, cwd: Path, cmd: str, timeout_s: int):
        self.commands.append(cmd)
        code = self.install_exit_code if "pip install" in cmd else self.import_exit_code
        return subprocess.CompletedProcess(args=cmd, returncode=code, stdout="", stderr="")


def _warm_up_session(
    tmp_path: Path,
    *,
    runtime_kind: RuntimeKind,
    session_id: str,
    shell_runner: Any,
    monkeypatch,
):
    import alysis_code.agent_loop as agent_loop_mod
    from alysis_code.agent_loop import create_session

    # create_session resolves the runner builder through the agent_loop module,
    # which is the established patch seam for shell backends in this suite.
    monkeypatch.setattr(
        agent_loop_mod, "build_shell_runner_from_settings", lambda *a, **k: shell_runner
    )
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=runtime_kind is RuntimeKind.ONE_SHOT,
        runtime_kind=runtime_kind,
        verification_enabled=False,
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
    )
    session.close()
    return sessions_dir


def test_autonomous_session_warmup_provisions_through_the_session_shell(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    shell = _StubShellRunner(import_exit_code=PACKAGE_MISSING_EXIT_CODE, install_exit_code=0)

    sessions_dir = _warm_up_session(
        tmp_path,
        runtime_kind=RuntimeKind.ONE_SHOT,
        session_id="prov-one-shot",
        shell_runner=shell,
        monkeypatch=monkeypatch,
    )

    payloads = _session_events(sessions_dir, "prov-one-shot", "env_provisioned")
    assert len(payloads) == 1
    assert payloads[0]["package"] == "pytest"
    assert payloads[0]["trigger_config_file"] == "pytest.ini"
    assert payloads[0]["success"] is True
    assert payloads[0]["runtime_kind"] == "one_shot"
    assert len(shell.commands) == 2
    assert "find_spec" in shell.commands[0]
    assert shell.commands[1].endswith('"$PY" -m pip install pytest --quiet')
    assert _session_events(sessions_dir, "prov-one-shot", "env_gap_detected") == []


def test_interactive_session_warmup_only_reports_the_gap(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    shell = _StubShellRunner(import_exit_code=PACKAGE_MISSING_EXIT_CODE)

    sessions_dir = _warm_up_session(
        tmp_path,
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        session_id="prov-interactive",
        shell_runner=shell,
        monkeypatch=monkeypatch,
    )

    payloads = _session_events(sessions_dir, "prov-interactive", "env_gap_detected")
    assert len(payloads) == 1
    assert payloads[0]["package"] == "pytest"
    assert payloads[0]["runtime_kind"] == "interactive_chat"
    assert not any("pip install" in command for command in shell.commands)
    assert _session_events(sessions_dir, "prov-interactive", "env_provisioned") == []


def test_session_warmup_is_silent_when_the_runner_is_present(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    shell = _StubShellRunner(import_exit_code=0)

    sessions_dir = _warm_up_session(
        tmp_path,
        runtime_kind=RuntimeKind.ONE_SHOT,
        session_id="prov-present",
        shell_runner=shell,
        monkeypatch=monkeypatch,
    )

    assert not any("pip install" in command for command in shell.commands)
    assert _session_events(sessions_dir, "prov-present", "env_provisioned") == []
    assert _session_events(sessions_dir, "prov-present", "env_gap_detected") == []


def test_session_warmup_does_nothing_without_a_declaration(tmp_path: Path, monkeypatch) -> None:
    shell = _StubShellRunner(import_exit_code=PACKAGE_MISSING_EXIT_CODE)

    sessions_dir = _warm_up_session(
        tmp_path,
        runtime_kind=RuntimeKind.ONE_SHOT,
        session_id="prov-none",
        shell_runner=shell,
        monkeypatch=monkeypatch,
    )

    assert shell.commands == [], "no declaration means the shell is never touched"
    assert _session_events(sessions_dir, "prov-none", "env_provisioned") == []
    assert _session_events(sessions_dir, "prov-none", "env_gap_detected") == []


def test_session_warmup_kill_switch_suppresses_everything(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_WORKSPACE_PROVISIONING", "off")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    shell = _StubShellRunner(import_exit_code=PACKAGE_MISSING_EXIT_CODE)

    sessions_dir = _warm_up_session(
        tmp_path,
        runtime_kind=RuntimeKind.ONE_SHOT,
        session_id="prov-off",
        shell_runner=shell,
        monkeypatch=monkeypatch,
    )

    assert shell.commands == []
    assert _session_events(sessions_dir, "prov-off", "env_provisioned") == []
    assert _session_events(sessions_dir, "prov-off", "env_gap_detected") == []


def test_readonly_session_provisions_nothing(tmp_path: Path, monkeypatch) -> None:
    # A session denied shell execution must not mutate the environment during
    # its own construction.
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    import alysis_code.agent.session as session_mod
    from alysis_code.agent_loop import create_session

    def _must_not_install(_decision, **_kwargs):
        raise AssertionError("a readonly session must never install")

    monkeypatch.setattr(session_mod, "provision_test_runner", _must_not_install)
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="readonly",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=True,
        runtime_kind=RuntimeKind.ONE_SHOT,
        verification_enabled=False,
        session_log_dir_override=sessions_dir,
        session_id_override="prov-readonly",
    )
    session.close()

    assert _session_events(sessions_dir, "prov-readonly", "env_provisioned") == []


def test_session_warmup_survives_a_broken_installer(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    import alysis_code.agent.session as session_mod

    def _explode(_decision, **_kwargs):
        raise RuntimeError("pip subsystem is broken")

    monkeypatch.setattr(session_mod, "provision_test_runner", _explode)
    sessions_dir = _warm_up_session(
        tmp_path,
        runtime_kind=RuntimeKind.ONE_SHOT,
        session_id="prov-broken",
        shell_runner=_StubShellRunner(import_exit_code=PACKAGE_MISSING_EXIT_CODE),
        monkeypatch=monkeypatch,
    )

    warnings = [
        payload
        for payload in _session_events(sessions_dir, "prov-broken", "warning")
        if payload.get("warning") == "workspace_provisioning_failed"
    ]
    assert len(warnings) == 1
    assert "pip subsystem is broken" in warnings[0]["error"]
