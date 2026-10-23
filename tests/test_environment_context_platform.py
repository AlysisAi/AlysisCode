"""<environment_context> names the OS, the shell shell_run uses and the local date.

The shell comes from the same backend choice the session's runner is built from, so the
prompt cannot name a shell the commands do not run in.
"""

from __future__ import annotations

import io
import os
import re
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from alysis_code import sandbox_runner
from alysis_code.agent import prompt_context
from alysis_code.agent.prompt_context import (
    _environment_context_message,
    refresh_session_environment_context_message,
    session_shell_environment,
)
from alysis_code.agent_loop import build_tools, create_session
from alysis_code.config import AppConfig
from alysis_code.sandbox_runner import (
    SEATBELT_EXECUTABLE,
    BwrapShellRunner,
    DisabledShellRunner,
    DockerShellRunner,
    HostShellRunner,
    SeatbeltShellRunner,
    build_shell_runner_from_settings,
)
from alysis_code.sandbox_settings import resolve_shell_sandbox_settings
from alysis_code.session_store import SessionStore


@pytest.fixture(autouse=True)
def _no_sandbox_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("ALYSIS_SHELL_SANDBOX_"):
            monkeypatch.delenv(name, raising=False)


def _machine(
    monkeypatch: pytest.MonkeyPatch,
    *,
    system: str,
    tools: tuple[str, ...] = (),
    seatbelt: bool = False,
    comspec: str | None = r"C:\WINDOWS\system32\cmd.exe",
) -> None:
    """Pretend to be ``system`` with only ``tools`` on PATH, for the sandbox runner only."""
    monkeypatch.setattr(sandbox_runner, "platform", SimpleNamespace(system=lambda: system))
    monkeypatch.setattr(
        sandbox_runner,
        "shutil",
        SimpleNamespace(which=lambda name, *a, **k: f"/usr/bin/{name}" if name in tools else None),
    )
    real_exists = os.path.exists
    monkeypatch.setattr(
        os.path,
        "exists",
        lambda path: seatbelt if path == SEATBELT_EXECUTABLE else real_exists(path),
    )
    if comspec is None:
        monkeypatch.delenv("COMSPEC", raising=False)
    else:
        monkeypatch.setenv("COMSPEC", comspec)


def _cfg(**shell_sandbox: str) -> AppConfig:
    cfg = AppConfig(model="test-model")
    if shell_sandbox:
        cfg.extra_fields["shell_sandbox"] = dict(shell_sandbox)
    return cfg


# One case per runner choice: the shell named in the prompt, and the runner the same
# settings build. Both must agree.
RUNNER_CHOICES = [
    pytest.param(
        dict(system="Windows"),
        dict(mode="off"),
        "cmd.exe (host)",
        HostShellRunner,
        id="host-windows-cmd",
    ),
    pytest.param(
        dict(system="Windows", comspec=None),
        dict(mode="off"),
        "cmd.exe (host)",
        HostShellRunner,
        id="host-windows-no-comspec",
    ),
    pytest.param(
        dict(system="Linux"),
        dict(mode="off"),
        "sh (host)",
        HostShellRunner,
        id="host-posix-sh",
    ),
    pytest.param(
        dict(system="Windows"),
        dict(mode="strict", unavailable_fallback="ask"),
        "cmd.exe (host, each command needs approval)",
        HostShellRunner,
        id="host-after-approval",
    ),
    pytest.param(
        dict(system="Windows", tools=("docker",)),
        dict(mode="strict"),
        "sh (Docker sandbox)",
        DockerShellRunner,
        id="docker",
    ),
    pytest.param(
        dict(system="Linux", tools=("bwrap", "docker")),
        dict(mode="strict"),
        "sh (bwrap sandbox)",
        BwrapShellRunner,
        id="bwrap",
    ),
    pytest.param(
        dict(system="Darwin", seatbelt=True),
        dict(mode="strict"),
        "sh (Seatbelt sandbox)",
        SeatbeltShellRunner,
        id="seatbelt",
    ),
    pytest.param(
        dict(system="Windows"),
        dict(mode="warn"),
        "none (no usable sandbox, so shell commands are disabled)",
        DisabledShellRunner,
        id="no-backend",
    ),
]


@pytest.mark.parametrize(("machine", "sandbox", "expected_shell", "runner_type"), RUNNER_CHOICES)
def test_shell_line_matches_the_runner_the_same_settings_build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    machine: dict,
    sandbox: dict,
    expected_shell: str,
    runner_type: type,
) -> None:
    _machine(monkeypatch, **machine)
    cfg = _cfg(**sandbox)

    shell = session_shell_environment(cfg, shell_disabled=False)
    runner = build_shell_runner_from_settings(
        resolve_shell_sandbox_settings(cfg), tmp_path, warning_callback=lambda _msg: None
    )

    assert shell == expected_shell
    assert type(runner) is runner_type
    if runner_type is HostShellRunner:
        assert runner.requires_command_approval is ("approval" in expected_shell)


def test_shell_line_is_none_when_the_session_disables_the_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _machine(monkeypatch, system="Windows")
    assert (
        session_shell_environment(_cfg(mode="off"), shell_disabled=True)
        == "none (shell commands are disabled in this session)"
    )


def test_session_names_the_shell_of_its_own_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    _machine(monkeypatch, system="Windows")
    session = create_session(
        cfg=_cfg(mode="off"),
        root=tmp_path,
        mode="auto",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    try:
        env_block = next(
            str(m["content"])
            for m in session.messages
            if str(m.get("content") or "").startswith("<environment_context>")
        )
        assert session.shell_environment == "cmd.exe (host)"
        assert "shell: cmd.exe (host)" in env_block.splitlines()
        assert type(session.shell_runner._resolve_runner()) is HostShellRunner
        description = session.tools["shell_run"].as_openai_tool()["function"]["description"]
        assert description.endswith("Commands run in cmd.exe (host), so use that shell's syntax.")

        # A config change cannot swap the runner of a live session, so a refresh (here
        # after a mode change) keeps naming the shell the commands actually run in.
        session.cfg.extra_fields["shell_sandbox"] = {"mode": "strict"}
        session.mode = "review"
        assert refresh_session_environment_context_message(session) is True
        refreshed = next(
            str(m["content"])
            for m in session.messages
            if str(m.get("content") or "").startswith("<environment_context>")
        )
        assert "mode: review" in refreshed.splitlines()
        assert "shell: cmd.exe (host)" in refreshed.splitlines()
    finally:
        session.close()


def test_readonly_session_says_the_shell_is_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    _machine(monkeypatch, system="Windows")
    session = create_session(
        cfg=_cfg(mode="off"),
        root=tmp_path,
        mode="readonly",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    try:
        assert type(session.shell_runner) is DisabledShellRunner
        assert session.shell_environment == "none (shell commands are disabled in this session)"
    finally:
        session.close()


def _env_context(**overrides: object) -> str:
    kwargs: dict[str, object] = dict(
        mode="auto",
        yes=False,
        non_interactive=False,
        deny_write_prefixes=[],
        allow_write_globs=None,
        verification_enabled=False,
        recommended_verification_commands=None,
        authoritative_verification_commands=None,
        verification_selection_source=None,
        verification_selection_reason=None,
        verification_contract_type=None,
        verification_authoritative=False,
        one_shot_execution=False,
        shell="cmd.exe (host)",
    )
    kwargs.update(overrides)
    return _environment_context_message(**kwargs)  # type: ignore[arg-type]


class _Clock(date):
    now = datetime(2026, 10, 10, 0, 0, 5)

    @classmethod
    def today(cls) -> date:  # type: ignore[override]
        return cls.now.date()


def test_environment_context_has_os_shell_and_date_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prompt_context, "date", _Clock)
    monkeypatch.setattr(prompt_context, "platform", SimpleNamespace(system=lambda: "Windows"))
    _Clock.now = datetime(2026, 10, 10, 9, 30)

    lines = _env_context().splitlines()

    assert lines[:5] == [
        "<environment_context>",
        "os: Windows",
        "shell: cmd.exe (host)",
        "date: 2026-10-10",
        "mode: auto",
    ]


def test_environment_context_is_identical_all_day_and_changes_only_the_date_next_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prompt_context, "date", _Clock)

    _Clock.now = datetime(2026, 10, 10, 0, 0, 5)
    just_after_midnight = _env_context()
    _Clock.now = datetime(2026, 10, 10, 23, 59, 55)
    just_before_midnight = _env_context()
    _Clock.now = datetime(2026, 10, 11, 0, 0, 5)
    next_day = _env_context()

    assert just_after_midnight == just_before_midnight
    assert not re.search(r"\b\d{1,2}:\d{2}\b", just_after_midnight)
    changed = [
        (before, after)
        for before, after in zip(
            just_after_midnight.splitlines(), next_day.splitlines(), strict=True
        )
        if before != after
    ]
    assert changed == [("date: 2026-10-10", "date: 2026-10-11")]


def test_refresh_within_a_day_leaves_the_prompt_bytes_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prompt_context, "date", _Clock)
    session = SimpleNamespace(
        mode="auto",
        shell_environment="sh (Docker sandbox)",
        messages=[{"role": "user", "content": "<environment_context>\n</environment_context>\n"}],
    )

    _Clock.now = datetime(2026, 10, 10, 8, 0)
    assert refresh_session_environment_context_message(session) is True
    first = session.messages[0]["content"]
    _Clock.now = datetime(2026, 10, 10, 22, 0)
    assert refresh_session_environment_context_message(session) is True

    assert session.messages[0]["content"] == first
    assert "shell: sh (Docker sandbox)" in first.splitlines()


def test_system_prompt_answers_environment_questions_from_the_block() -> None:
    # The rule names the block's fields; each must exist, so the model is never sent
    # looking for a line the block does not have.
    rule = next(
        line
        for line in prompt_context.SYSTEM_PROMPT.splitlines()
        if "Environment context is authoritative" in line
    )
    block = _env_context().splitlines()

    assert "without running a command" in rule
    for field in ("os", "shell", "date", "mode"):
        assert re.search(rf"\b{field}\b", rule)
        assert any(line.startswith(f"{field}: ") for line in block)


def _store(root: Path) -> SessionStore:
    return SessionStore(
        enabled=True,
        sessions_dir=root / "sessions",
        session_id="env-context-shell",
        cwd=str(root),
        repo_root=str(root),
    )


@pytest.mark.parametrize(
    ("shell_environment", "expected"),
    [
        (
            "cmd.exe (host)",
            "Run a policy-checked shell command. "
            "Commands run in cmd.exe (host), so use that shell's syntax.",
        ),
        (
            "sh (Docker sandbox)",
            "Run a policy-checked shell command. "
            "Commands run in sh (Docker sandbox), so use that shell's syntax.",
        ),
        (
            "none (shell commands are disabled in this session)",
            "Run a policy-checked shell command.",
        ),
        (None, "Run a policy-checked shell command."),
    ],
)
def test_shell_run_description_names_the_shell(
    tmp_path: Path, shell_environment: str | None, expected: str
) -> None:
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=False,
        non_interactive=True,
        shell_runner=HostShellRunner(),
        shell_environment=shell_environment,
    )

    assert tools["shell_run"].as_openai_tool()["function"]["description"] == expected
