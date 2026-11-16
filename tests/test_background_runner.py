from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest

import alysis_code.background_runner as background_runner_mod
from alysis_code.background_runner import (
    BackgroundProcessSpawn,
    BackgroundShellRunner,
    DisabledBackgroundRunner,
    HostBackgroundRunner,
    LazyBackgroundShellRunner,
)


class FakePopen:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs


class RaisingRunner:
    def start(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        env_overrides: dict[str, str] | None = None,
    ) -> BackgroundProcessSpawn:
        raise RuntimeError("started")


class DummyRunner:
    def __init__(self) -> None:
        self.starts = 0

    def start(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        env_overrides: dict[str, str] | None = None,
    ) -> BackgroundProcessSpawn:
        self.starts += 1
        return BackgroundProcessSpawn(
            popen=FakePopen(),  # type: ignore[arg-type]
            cleanup=lambda: None,
            started_argv=(cmd,),
        )


def _patch_popen(monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]) -> None:
    def fake_popen(*args: object, **kwargs: object) -> FakePopen:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakePopen(*args, **kwargs)

    monkeypatch.setattr(background_runner_mod.subprocess, "Popen", fake_popen)


def test_host_runner_spawns_with_session_isolation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    _patch_popen(monkeypatch, captured)

    HostBackgroundRunner().start(root=tmp_path, cwd=tmp_path, cmd="echo hi")

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    if os.name == "nt":
        assert kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kwargs["start_new_session"] is True


def test_host_runner_filters_sensitive_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    _patch_popen(monkeypatch, captured)
    monkeypatch.setenv("ALYSIS_API_KEY", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")

    HostBackgroundRunner().start(
        root=tmp_path,
        cwd=tmp_path,
        cmd="echo hi",
        env_overrides={"ALYSIS_API_KEY": "override", "SAFE_VALUE": "ok"},
    )

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert "ALYSIS_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["SAFE_VALUE"] == "ok"


def test_disabled_runner_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="disabled"):
        DisabledBackgroundRunner().start(root=tmp_path, cwd=tmp_path, cmd="echo hi")


def test_lazy_background_runner_defers_construction_until_start(tmp_path: Path) -> None:
    calls = 0

    def loader() -> BackgroundShellRunner:
        nonlocal calls
        calls += 1
        return RaisingRunner()

    runner = LazyBackgroundShellRunner(loader)

    assert calls == 0
    with pytest.raises(RuntimeError, match="started"):
        runner.start(root=tmp_path, cwd=tmp_path, cmd="echo hi")
    assert calls == 1


def test_lazy_background_runner_caches_resolved_runner(
    tmp_path: Path,
) -> None:
    calls = 0
    resolved = DummyRunner()

    def loader() -> BackgroundShellRunner:
        nonlocal calls
        calls += 1
        return resolved

    runner = LazyBackgroundShellRunner(loader)

    first = runner.start(root=tmp_path, cwd=tmp_path, cmd="echo first")
    second = runner.start(root=tmp_path, cwd=tmp_path, cmd="echo second")

    assert calls == 1
    assert resolved.starts == 2
    assert isinstance(first, BackgroundProcessSpawn)
    assert isinstance(second, BackgroundProcessSpawn)


def test_lazy_background_runner_caches_load_error(tmp_path: Path) -> None:
    calls = 0
    error = RuntimeError("load failed")

    def loader() -> BackgroundShellRunner:
        nonlocal calls
        calls += 1
        raise error

    runner = LazyBackgroundShellRunner(loader)

    with pytest.raises(RuntimeError) as first:
        runner.start(root=tmp_path, cwd=tmp_path, cmd="echo hi")
    with pytest.raises(RuntimeError) as second:
        runner.start(root=tmp_path, cwd=tmp_path, cmd="echo hi")

    assert first.value is error
    assert second.value is error
    assert calls == 1


def test_lazy_background_runner_resolves_once_for_concurrent_starts(tmp_path: Path) -> None:
    calls = 0
    loader_entered = threading.Event()
    release_loader = threading.Event()
    resolved = DummyRunner()
    errors: list[BaseException] = []

    def loader() -> BackgroundShellRunner:
        nonlocal calls
        calls += 1
        loader_entered.set()
        if not release_loader.wait(1.0):
            raise RuntimeError("loader was not released")
        return resolved

    def start() -> None:
        try:
            runner.start(root=tmp_path, cwd=tmp_path, cmd="echo hi")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    runner = LazyBackgroundShellRunner(loader)
    first = threading.Thread(target=start)
    second = threading.Thread(target=start)

    first.start()
    assert loader_entered.wait(1.0)
    second.start()
    release_loader.set()
    first.join(1.0)
    second.join(1.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert calls == 1
    assert resolved.starts == 2
