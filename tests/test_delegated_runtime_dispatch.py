from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.agent_runtimes import host as host_mod
from alysis_code.agent_runtimes.base import RuntimeTurnResult
from alysis_code.cli import app
from alysis_code.config import AppConfig, load_config, save_config


def _seed_delegated_config(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    cfg = AppConfig(
        execution={"backend": "delegated", "runtime": "sample-agent"},
        agent_runtimes={
            "sample-agent": {"adapter": "sample-cli", "executable": "sample-agent"},
        },
    )
    cfg.extra_fields = {
        "onboarded": True,
        "default_workspace_path": os.fspath(tmp_path),
    }
    save_config(cfg)


def test_run_dispatches_delegated_backend_without_native_model_or_api_key(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    _seed_delegated_config(tmp_path, monkeypatch)
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        host_mod,
        "prepare_delegated_runtime",
        lambda _cfg, **kwargs: calls.append(("prepare", kwargs)) or "sample-agent",
    )
    monkeypatch.setattr(
        host_mod,
        "run_delegated_once",
        lambda **kwargs: calls.append(("run", kwargs)) or 0,
    )

    result = CliRunner().invoke(
        app,
        ["run", "inspect only", "--path", os.fspath(tmp_path), "--allow-broad-workspace"],
    )

    assert result.exit_code == 0, result.output
    assert [name for name, _payload in calls] == ["prepare", "run"]
    run_payload = calls[-1][1]
    assert isinstance(run_payload, dict)
    assert run_payload["instruction"] == "inspect only"
    assert run_payload["cwd"] == tmp_path.resolve()


def test_chat_dispatches_delegated_backend_before_native_session_creation(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    _seed_delegated_config(tmp_path, monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        host_mod,
        "prepare_delegated_runtime",
        lambda *_args, **_kwargs: calls.append("prepare") or "sample-agent",
    )
    monkeypatch.setattr(
        host_mod,
        "run_delegated_chat",
        lambda **_kwargs: calls.append("chat"),
    )
    monkeypatch.setattr(
        cli_mod,
        "create_session",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("native session must not start")),
    )

    result = CliRunner().invoke(
        app,
        ["chat", "--path", os.fspath(tmp_path), "--allow-broad-workspace"],
    )

    assert result.exit_code == 0, result.output
    assert calls == ["prepare", "chat"]


def test_delegated_backend_rejects_native_only_flags(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    _seed_delegated_config(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        app,
        [
            "run",
            "inspect",
            "--path",
            os.fspath(tmp_path),
            "--base-url",
            "https://example.com/v1",
        ],
    )

    assert result.exit_code == 2
    assert "Delegated execution does not support" in result.output
    assert "--base-url" in result.output


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        ("--stream", "--stream/--no-stream"),
        ("--no-stream", "--stream/--no-stream"),
        ("--yes", "--yes"),
    ],
)
def test_delegated_backend_rejects_explicit_ignored_flags(
    tmp_path: Path,
    monkeypatch,
    flag: str,
    expected: str,
) -> None:  # type: ignore[no-untyped-def]
    _seed_delegated_config(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        app,
        ["run", "inspect", "--path", os.fspath(tmp_path), flag],
    )

    assert result.exit_code == 2
    assert expected in result.output


def test_delegated_fullaccess_is_rejected_before_provider_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    _seed_delegated_config(tmp_path, monkeypatch)
    monkeypatch.setattr(host_mod, "prepare_delegated_runtime", lambda *_a, **_k: "sample-agent")
    monkeypatch.setattr(
        host_mod,
        "run_runtime_turn",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("provider turn must not start for fullaccess")
        ),
    )

    result = CliRunner().invoke(
        app,
        ["run", "inspect", "--path", os.fspath(tmp_path), "--mode", "fullaccess"],
    )

    assert result.exit_code == 2
    assert "does not support fullaccess" in result.output


def test_delegated_default_launch_falls_back_from_native_fullaccess(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    _seed_delegated_config(tmp_path, monkeypatch)
    cfg = load_config()
    cfg.default_mode = "fullaccess"
    save_config(cfg)
    captured: dict[str, object] = {}
    monkeypatch.setattr(host_mod, "prepare_delegated_runtime", lambda *_a, **_k: "sample-agent")
    monkeypatch.setattr(
        host_mod, "run_delegated_once", lambda **kwargs: captured.update(kwargs) or 0
    )

    result = CliRunner().invoke(
        app,
        ["run", "inspect", "--path", os.fspath(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert captured["mode"] == "review"
    assert "using review (read-only)" in result.output


def test_delegated_run_resolves_required_deadline_from_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    _seed_delegated_config(tmp_path, monkeypatch)
    monkeypatch.setenv("ALYSIS_RUN_DEADLINE_SECONDS", "17.5")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        host_mod,
        "prepare_delegated_runtime",
        lambda *_args, **kwargs: captured.update(kwargs) or "sample-agent",
    )
    monkeypatch.setattr(host_mod, "run_delegated_once", lambda **_kwargs: 0)

    result = CliRunner().invoke(
        app,
        ["run", "inspect", "--path", os.fspath(tmp_path), "--require-deadline"],
    )

    assert result.exit_code == 0, result.output
    assert captured["deadline_seconds"] == 17.5


def test_delegated_run_rejects_non_finite_deadline_before_prepare(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    _seed_delegated_config(tmp_path, monkeypatch)
    monkeypatch.setattr(
        host_mod,
        "prepare_delegated_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("runtime preparation must not start for invalid deadline")
        ),
    )

    result = CliRunner().invoke(
        app,
        ["run", "inspect", "--path", os.fspath(tmp_path), "--deadline-seconds", "nan"],
    )

    assert result.exit_code == 2
    assert "finite number > 0" in result.output


def test_provider_output_is_redacted_and_terminal_controls_are_inert() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class RecordingConsole:
        def print(self, value: object = "", **kwargs: object) -> None:
            calls.append((str(value), dict(kwargs)))

    host_mod._render_turn_result(  # noqa: SLF001 - security regression coverage
        RecordingConsole(),
        RuntimeTurnResult(
            runtime_id="fake",
            command=("fake",),
            exit_code=1,
            final_message=(
                "\x1b]0;forged title\x07\x1b[31m[bold]answer[/bold] sk-abcdefghijklmnop\x1b[0m"
            ),
            warnings=("token=provider-secret\rforged",),
            error="authorization: provider-secret",
        ),
    )

    rendered = "\n".join(value for value, _kwargs in calls)
    assert "\x1b" not in rendered
    assert "\r" not in rendered
    assert "provider-secret" not in rendered
    assert "sk-abcdefghijklmnop" not in rendered
    assert "[REDACTED]" in rendered
    assert all(kwargs.get("markup") is False for _value, kwargs in calls)
