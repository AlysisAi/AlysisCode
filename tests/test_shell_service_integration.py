from __future__ import annotations

import os
import shlex
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent_loop import AgentRuntimeError, create_session
from alysis_code.config import AppConfig
from alysis_code.execution_deadline import ExecutionDeadline
from alysis_code.surface import ApprovalDecision
from alysis_code.surface.noop_surface import NoopSurface


class _LocalServiceSurface(NoopSurface):
    def request_approval(self, request):
        return ApprovalDecision(request.kind in {"shell_service_start", "shell_background"})


def _cfg() -> AppConfig:
    cfg = AppConfig(model="test-model")
    cfg.extra_fields = {"shell_sandbox": {"mode": "off"}}
    return cfg


def _shell_join(args: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _assert_http_ready(port: int) -> None:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
        assert response.status == 200


def _create_service_session(
    root: Path,
    sessions_dir: Path,
    *,
    mode: str = "auto",
    execution_deadline: ExecutionDeadline | None = None,
) -> Any:
    return create_session(
        cfg=_cfg(),
        root=root,
        mode=mode,
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        non_interactive=False,
        surface=_LocalServiceSurface(),
        session_log_dir_override=sessions_dir,
        execution_deadline=execution_deadline,
    )


def _wait_loaded_popen(session: Any, service_id: str) -> None:
    manager = session.durable_service_manager
    if manager is None:
        return
    popen = manager._popens.get(service_id)
    if popen is not None:
        popen.wait(timeout=3)


def test_shell_service_survives_close_and_can_stop_from_fresh_session(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    sessions_dir = tmp_path / "sessions"
    root.mkdir()
    (root / "index.html").write_bytes(b"ready\n")
    port = _free_tcp_port()
    session = _create_service_session(root, sessions_dir)
    second_session = None
    service_id = ""
    session_closed = False

    try:
        started = session.tools["shell_service_start"].run(
            {
                "cmd": _shell_join(
                    [
                        sys.executable,
                        "-m",
                        "http.server",
                        str(port),
                        "--bind",
                        "127.0.0.1",
                    ]
                ),
                "readiness": {
                    "type": "tcp",
                    "host": "127.0.0.1",
                    "port": port,
                    "timeout_s": 5,
                },
            }
        )
        service_id = str(started["service_id"])

        assert started["ownership"] == "DURABLE_SERVICE"
        assert started["lifetime"] == "durable"
        assert started["status"] == "running"
        assert started["readiness"]["status"] == "ready"
        assert Path(str(started["log_paths"]["stdout"])).exists()
        assert Path(str(started["log_paths"]["stderr"])).exists()

        session.close()
        session_closed = True
        _assert_http_ready(port)
        consumer = subprocess.run(
            [
                sys.executable,
                "-c",
                "import urllib.request; "
                f"assert urllib.request.urlopen('http://127.0.0.1:{port}/', timeout=2).read() == b'ready\\n'",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert consumer.returncode == 0, consumer.stderr
        assert started["readiness"]["endpoint_owned"] is True
        assert any(
            event["type"] == "durable_services_left_active"
            and event["payload"]["services"][0]["service_id"] == service_id
            for event in session.store.events_snapshot()
        )

        second_session = _create_service_session(root, sessions_dir)
        status = second_session.tools["shell_service_status"].run({"service_id": service_id})
        assert status["status"] == "running"
        assert status["readiness"]["status"] == "ready"

        stopped = second_session.tools["shell_service_stop"].run({"service_id": service_id})
        assert stopped["stopped"] is True
        _wait_loaded_popen(session, service_id)
    finally:
        if second_session is not None:
            if service_id:
                second_session.durable_service_manager.stop(service_id)
            second_session.close()
        if service_id and session.durable_service_manager is not None:
            session.durable_service_manager.stop(service_id)
        if not session_closed:
            session.close()


@pytest.mark.parametrize("tool_name", ["shell_service_start", "workspace_preview_start"])
def test_service_deadline_denial_prevents_new_launch_and_preserves_existing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    root = tmp_path / "workspace"
    sessions_dir = tmp_path / "sessions"
    root.mkdir()
    deadline = ExecutionDeadline.from_absolute(
        started_at_monotonic=0.0,
        deadline_monotonic=4.0,
        configured_duration_seconds=4.0,
        clock=lambda: 3.5,
    )
    session = _create_service_session(root, sessions_dir, execution_deadline=deadline)
    manager = session.durable_service_manager
    assert manager is not None
    existing = manager.start(
        cmd=_shell_join([sys.executable, "-c", "import time; time.sleep(30)"]),
        cwd=root,
    )
    service_id = existing.service_id
    monkeypatch.setattr(manager, "start", lambda **_: pytest.fail("new service launched"))
    monkeypatch.setattr(manager, "start_preview", lambda **_: pytest.fail("new preview launched"))
    try:
        started = session.tools[tool_name].run(
            {
                "cmd": _shell_join([sys.executable, "-c", "import time; time.sleep(30)"]),
                "replace_service_id": service_id,
            }
        )
        assert started["deadline_prevented_launch"] is True
        assert started["failure_category"] == "deadline"
        assert "error" in started
        assert "service_id" not in started
        assert started["deadline_start_decision"]["allowed"] is False
        assert started["deadline_start_decision"]["reason"] == "finalization_disallows_operation"
        assert manager.status(service_id)["status"] == "running"
    finally:
        if service_id and session.durable_service_manager is not None:
            session.durable_service_manager.stop(service_id)
        session.close()


@pytest.mark.parametrize("now", [3.5, 5.0])
def test_persistent_background_deadline_denial_prevents_durable_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, now: float
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    deadline = ExecutionDeadline.from_absolute(
        started_at_monotonic=0,
        deadline_monotonic=4,
        configured_duration_seconds=4,
        clock=lambda: now,
    )
    session = _create_service_session(root, tmp_path / "sessions", execution_deadline=deadline)
    monkeypatch.setattr(
        session.durable_service_manager, "start", lambda **_: pytest.fail("durable launch reached")
    )
    try:
        result = session.tools["shell_background"].run({"cmd": "echo ready", "persist": True})
        assert result["deadline_prevented_launch"] is True
        assert result["failure_category"] == "deadline"
        assert result["deadline_start_decision"]["allowed"] is False
        assert "service_id" not in result
    finally:
        session.close()


def test_persistent_background_forwards_shared_deadline_through_launch_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    now = [0.0]
    deadline = ExecutionDeadline.from_absolute(
        started_at_monotonic=0,
        deadline_monotonic=10,
        configured_duration_seconds=10,
        clock=lambda: now[0],
    )
    session = _create_service_session(root, tmp_path / "sessions", execution_deadline=deadline)
    manager = session.durable_service_manager
    assert manager is not None
    build_launch = manager._build_launch
    observed = []

    def slow_preparation(**kwargs):
        launch = build_launch(**kwargs)
        observed.append(deadline.remaining_seconds())
        now[0] = 11.0
        return launch

    monkeypatch.setattr(manager, "_build_launch", slow_preparation)
    monkeypatch.setattr(
        "alysis_code.durable_service_manager.subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail("expired preparation reached process launch"),
    )
    try:
        with pytest.raises(AgentRuntimeError, match="deadline"):
            session.tools["shell_background"].run({"cmd": "echo ready", "persist": True})
        assert observed == [10.0]
        assert manager._popens == {}
    finally:
        session.close()


def test_persistent_background_readiness_uses_remaining_shared_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    now = [0.0]
    deadline = ExecutionDeadline.from_absolute(
        started_at_monotonic=0,
        deadline_monotonic=10,
        configured_duration_seconds=10,
        clock=lambda: now[0],
    )
    session = _create_service_session(root, tmp_path / "sessions", execution_deadline=deadline)
    manager = session.durable_service_manager
    assert manager is not None
    build_launch, check_readiness = manager._build_launch, manager._check_readiness
    observed = []

    def slow_preparation(**kwargs):
        launch = build_launch(**kwargs)
        now[0] = 8.8  # Leave 0.2 seconds after the existing one-second cleanup reserve.
        return launch

    def observe_readiness(**kwargs):
        observed.append(kwargs["timeout_s"])
        return check_readiness(**kwargs)

    monkeypatch.setattr(manager, "_build_launch", slow_preparation)
    monkeypatch.setattr(manager, "_check_readiness", observe_readiness)
    try:
        result = session.tools["shell_background"].run(
            {
                "cmd": _shell_join([sys.executable, "-c", "import time; time.sleep(3)"]),
                "persist": True,
                "probe_port": _free_tcp_port(),
            }
        )
        assert observed[0] == pytest.approx(0.2)
        assert result["failure_category"] == "readiness_failed"
        assert all(process.poll() is not None for process in manager._popens.values())
    finally:
        for service_id in tuple(manager._popens):
            manager.stop(service_id)
        session.close()


def test_workspace_preview_tool_works_with_strict_unavailable_docker_image(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    sessions_dir = tmp_path / "sessions"
    root.mkdir()
    (root / "index.html").write_bytes(b"preview-tool-ok\n")
    cfg = AppConfig(model="test-model")
    cfg.extra_fields = {
        "shell_sandbox": {
            "mode": "strict",
            "backend": "docker",
            "network": "off",
            "docker_image": "private.invalid/alysis-sandbox:dev",
        }
    }
    session = create_session(
        cfg=cfg,
        root=root,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        non_interactive=True,
        session_log_dir_override=sessions_dir,
    )
    service_id = ""
    try:
        started = session.tools["workspace_preview_start"].run({"access": "auto"})
        service_id = str(started["service_id"])

        assert started["backend"] == "host-preview"
        assert started["status"] == "running"
        assert started["preview_access"] == "local"
        assert started["preview_port"] > 0
        with urllib.request.urlopen(started["access_url"], timeout=2) as response:
            assert response.read() == b"preview-tool-ok\n"
    finally:
        if service_id and session.durable_service_manager is not None:
            session.durable_service_manager.stop(service_id)
        session.close()


def test_workspace_preview_lan_access_requires_approval_in_noninteractive_mode(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    sessions_dir = tmp_path / "sessions"
    root.mkdir()
    cfg = _cfg()
    cfg.extra_fields["shell_sandbox"]["preview_access"] = "lan"
    session = create_session(
        cfg=cfg,
        root=root,
        mode="auto",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        non_interactive=True,
        session_log_dir_override=sessions_dir,
    )
    service_id = ""
    try:
        with pytest.raises(AgentRuntimeError, match="LAN preview exposure requires"):
            session.tools["workspace_preview_start"].run({"access": "auto"})
        started = session.tools["workspace_preview_start"].run({"access": "local"})
        service_id = str(started["service_id"])
        assert started["preview_access"] == "local"
    finally:
        if service_id and session.durable_service_manager is not None:
            session.durable_service_manager.stop(service_id)
        session.close()


def test_shell_service_tools_hidden_in_readonly_mode(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    sessions_dir = tmp_path / "sessions"
    root.mkdir()
    session = _create_service_session(root, sessions_dir, mode="readonly")
    try:
        assert "shell_service_start" not in session.tools
        assert "workspace_preview_start" not in session.tools
        assert "shell_service_status" not in session.tools
        assert "shell_service_stop" not in session.tools
    finally:
        session.close()


def test_shell_service_start_uses_shell_policy(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    sessions_dir = tmp_path / "sessions"
    root.mkdir()
    session = _create_service_session(root, sessions_dir)
    try:
        with pytest.raises(AgentRuntimeError, match="Blocked"):
            session.tools["shell_service_start"].run({"cmd": "mkfs.ext4 /dev/sda"})
    finally:
        session.close()


def test_shell_service_command_readiness_uses_shell_policy(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    sessions_dir = tmp_path / "sessions"
    root.mkdir()
    session = _create_service_session(root, sessions_dir)
    try:
        with pytest.raises(AgentRuntimeError, match="Blocked"):
            session.tools["shell_service_start"].run(
                {
                    "cmd": _shell_join([sys.executable, "-c", "import time; time.sleep(30)"]),
                    "readiness": {"type": "command", "command": "mkfs.ext4 /dev/sda"},
                }
            )
    finally:
        session.close()
