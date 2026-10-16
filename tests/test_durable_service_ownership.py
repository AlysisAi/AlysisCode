from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from alysis_code import durable_service_manager as dsm
from alysis_code.agent.acceptance_contract import (
    AcceptanceCriterionKind,
    AcceptanceCriterionStatus,
    build_acceptance_contract,
    record_acceptance_tool_effect,
)
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.service_persistence import PersistentServiceRecord, check_service


def command(code: str) -> str:
    args = [sys.executable, "-c", code]
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def readiness(port: int, timeout: float = 2) -> dict:
    return {
        "type": "tcp",
        "host": "127.0.0.1",
        "port": port,
        "timeout_s": timeout,
        "interval_s": 0.03,
    }


def server(port: int, delay: float = 0) -> str:
    return command(
        "import time; from http.server import HTTPServer, SimpleHTTPRequestHandler; "
        f"time.sleep({delay}); HTTPServer(('127.0.0.1', {port}), SimpleHTTPRequestHandler).serve_forever()"
    )


@pytest.fixture
def manager(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "index.html").write_text("owned-service", encoding="utf-8")
    result = dsm.DurableServiceManager(
        root=root, state_dir=tmp_path / "services", settings=ShellSandboxSettings(mode="off")
    )
    yield result
    for service_id in tuple(result._popens):
        # Restore metadata in tests which intentionally change a creation token.
        popen = result._popens[service_id]
        metadata = result._read_metadata(service_id)
        if metadata and popen.poll() is None:
            metadata["pid_start_token"] = dsm._pid_start_token(popen.pid)
            result._write_metadata(metadata)
        result.stop(service_id)


def test_unrelated_listener_cannot_prove_sleeping_child_ready(manager):
    with socket.socket() as unrelated:
        unrelated.bind(("127.0.0.1", 0))
        unrelated.listen(16)
        port = unrelated.getsockname()[1]
        started = manager.start(
            cmd=command("import time; time.sleep(30)"),
            cwd=manager.root,
            readiness=readiness(port, 0.2),
        )
        assert started.payload["failure_category"] == "readiness_failed"
        assert started.payload["readiness"]["status"] == "failed"
        assert "unrelated" in started.payload["readiness"]["detail"]
        assert not started.payload["alive"]
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            pass


def test_bind_failure_preserves_existing_listener(manager):
    port = free_port()
    previous = manager.start(cmd=server(port), cwd=manager.root, readiness=readiness(port))
    failed = manager.start(cmd=server(port), cwd=manager.root, readiness=readiness(port, 0.3))
    assert failed.payload["readiness"]["status"] == "failed"
    assert not failed.payload["alive"]
    assert manager.status(previous.service_id)["readiness"]["endpoint_owned"]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
        assert response.read() == b"owned-service"


def test_delayed_bind_is_not_ready_until_its_owned_listener_exists(manager):
    port = free_port()
    before = time.monotonic()
    started = manager.start(cmd=server(port, 0.25), cwd=manager.root, readiness=readiness(port))
    assert started.payload["readiness"]["status"] == "ready", json.dumps(started.payload, indent=2)
    assert time.monotonic() - before >= 0.25
    assert started.payload["readiness"]["strength"] == "owned_endpoint"
    assert started.payload["readiness"]["endpoint_owned"] is True
    assert started.payload["readiness"]["listener_pids"]


def test_failed_transaction_preserves_previous_working_service(manager):
    port = free_port()
    previous = manager.start(cmd=server(port), cwd=manager.root, readiness=readiness(port))
    failed = manager.start(
        cmd=command("raise SystemExit(7)"),
        cwd=manager.root,
        readiness=readiness(free_port()),
        replace_service_id=previous.service_id,
    )
    assert failed.payload["handoff"]["status"] == "rolled_back"
    assert manager.status(previous.service_id)["readiness"]["endpoint_owned"]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
        assert response.read() == b"owned-service"


def test_same_port_transaction_does_not_stop_previous_service(manager):
    port = free_port()
    previous = manager.start(cmd=server(port), cwd=manager.root, readiness=readiness(port))
    failed = manager.start(
        cmd=server(port),
        cwd=manager.root,
        readiness=readiness(port, 0.3),
        replace_service_id=previous.service_id,
    )
    assert failed.payload["handoff"]["status"] == "rolled_back"
    assert manager.status(previous.service_id)["readiness"]["endpoint_owned"]


def test_replacement_proves_candidate_before_stopping_old_instance(manager):
    previous = manager.start(
        cmd=server(port := free_port()), cwd=manager.root, readiness=readiness(port)
    )
    replacement_port = free_port()
    replacement = manager.start(
        cmd=server(replacement_port, 0.1),
        cwd=manager.root,
        readiness=readiness(replacement_port),
        replace_service_id=previous.service_id,
    )
    assert replacement.payload["handoff"]["status"] == "committed"
    assert manager._popens[previous.service_id].poll() is not None
    fresh = dsm.DurableServiceManager(
        root=manager.root, state_dir=manager.state_dir, settings=manager.settings
    )
    assert fresh.status(replacement.service_id)["readiness"]["endpoint_owned"]
    consumer = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:{replacement_port}/', timeout=2).read() == b'owned-service'",
        ],
        cwd=manager.root.parent,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert consumer.returncode == 0, consumer.stderr


@pytest.mark.parametrize("token", [None, "process:recycled-pid"])
def test_fresh_manager_refuses_unverifiable_pid_and_preserves_process(manager, token):
    started = manager.start(cmd=command("import time; time.sleep(30)"), cwd=manager.root)
    metadata = manager._read_metadata(started.service_id)
    metadata["pid_start_token"] = token
    manager._write_metadata(metadata)
    fresh = dsm.DurableServiceManager(
        root=manager.root, state_dir=manager.state_dir, settings=manager.settings
    )
    status = fresh.status(started.service_id)
    assert status["identity_valid"] is False
    assert status["readiness"]["status"] == "failed"
    stopped = fresh.stop(started.service_id)
    assert not stopped["stopped"]
    assert stopped["failure_category"] == "stale_metadata_identity_mismatch"
    assert manager._popens[started.service_id].poll() is None


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets unavailable")
def test_stale_socket_path_is_not_readiness(manager):
    with tempfile.TemporaryDirectory(prefix="alysis-socket-") as short_dir:
        path = Path(short_dir) / "stale.sock"
        path.write_text("not a listening socket", encoding="utf-8")
        started = manager.start(
            cmd=command("import time; time.sleep(30)"),
            cwd=manager.root,
            readiness={"type": "unix_socket", "path": str(path), "timeout_s": 0.1},
        )
        assert started.payload["readiness"]["status"] == "failed"
        assert path.read_text(encoding="utf-8") == "not a listening socket"


def test_missing_os_listener_visibility_fails_closed(manager, monkeypatch):
    port = free_port()
    started = manager.start(cmd=server(port), cwd=manager.root, readiness=readiness(port))
    assert started.payload["status"] == "running"

    def denied(*_args, **_kwargs):
        raise psutil.AccessDenied()

    monkeypatch.setattr(dsm.psutil, "net_connections", denied)
    monkeypatch.setattr(dsm, "_macos_tcp_listeners", lambda _: denied())
    result = manager._check_readiness(
        metadata=manager._read_metadata(started.service_id), timeout_s=0
    )
    assert result["status"] == "failed"
    assert "AccessDenied" in result["detail"]


@pytest.mark.skipif(sys.platform != "darwin", reason="requires the native macOS listener table")
def test_macos_process_listener_inspection_without_global_privileges(manager, monkeypatch):

    def denied(*_args, **_kwargs):
        raise psutil.AccessDenied()

    monkeypatch.setattr(dsm.psutil, "net_connections", denied)
    port = free_port()
    started = manager.start(cmd=server(port), cwd=manager.root, readiness=readiness(port))
    assert started.payload["status"] == "running", (
        f"readiness={started.payload['readiness']!r}; "
        f"startup_error={started.payload.get('startup_error')!r}; "
        f"stderr={Path(started.payload['stderr_log_path']).read_text(encoding='utf-8')[-1000:]!r}"
    )
    assert started.payload["readiness"]["endpoint_owned"] is True

    with socket.socket() as unrelated:
        unrelated.bind(("127.0.0.1", 0))
        unrelated.listen(16)
        failed = manager.start(
            cmd=command("import time; time.sleep(30)"),
            cwd=manager.root,
            readiness=readiness(unrelated.getsockname()[1], 0.2),
        )
        assert failed.payload["readiness"]["status"] == "failed"
        assert "unrelated" in failed.payload["readiness"]["detail"]


@pytest.mark.parametrize("probe", [None, {"type": "command", "command": command("pass")}])
def test_weak_readiness_cannot_satisfy_endpoint_acceptance(manager, probe):
    started = manager.start(
        cmd=command("import time; time.sleep(30)"), cwd=manager.root, readiness=probe
    )
    assert started.payload["alive"]
    assert started.payload["readiness"]["endpoint_owned"] is False
    contract = build_acceptance_contract(
        root=manager.root, instruction="Keep the service running on port 8080."
    )
    record_acceptance_tool_effect(
        contract=contract,
        root=manager.root,
        tool_name="shell_service_start",
        arguments={},
        status="ok",
        result=started.payload,
        touched_paths=set(),
    )
    endpoint_criteria = [
        c
        for c in contract.criteria
        if c.kind
        in {
            AcceptanceCriterionKind.PERSISTENT_SERVICE,
            AcceptanceCriterionKind.FUNCTIONAL_API_PROTOCOL,
        }
    ]
    assert endpoint_criteria
    assert all(c.status != AcceptanceCriterionStatus.PASSED for c in endpoint_criteria)


def test_persistent_finalization_uses_manager_ownership_not_generic_port_probe():
    record = PersistentServiceRecord(
        "svc_test",
        "serve",
        123,
        8080,
        status_probe=lambda: {
            "alive": True,
            "identity_valid": True,
            "readiness": {"status": "ready", "endpoint_owned": False},
        },
    )
    report = check_service(record, pid_probe=lambda _pid: True, port_probe=lambda _port: True)
    assert report.healthy is False
    assert report.endpoint_owned is False


@pytest.mark.parametrize("stage", ["initial", "handoff"])
def test_metadata_write_failure_cleans_candidate_and_preserves_previous(
    manager, monkeypatch, stage
):
    previous = manager.start(
        cmd=server(port := free_port()), cwd=manager.root, readiness=readiness(port)
    )
    original = manager._write_metadata

    def fail_candidate(metadata):
        if metadata["service_id"] != previous.service_id and (
            stage == "initial" or metadata.get("replaces_service_id")
        ):
            raise OSError("simulated metadata storage failure")
        original(metadata)

    monkeypatch.setattr(manager, "_write_metadata", fail_candidate)
    with pytest.raises(OSError, match="metadata storage"):
        manager.start(
            cmd=server(new_port := free_port()),
            cwd=manager.root,
            readiness=readiness(new_port),
            replace_service_id=previous.service_id,
        )
    for service_id, popen in manager._popens.items():
        if service_id != previous.service_id:
            assert popen.poll() is not None
    assert manager.status(previous.service_id)["readiness"]["endpoint_owned"]


def test_readiness_container_identity_write_failure_cleans_owned_candidate(manager, monkeypatch):
    original_check = manager._check_readiness
    original_write = manager._write_metadata
    terminated = []
    original_terminate = manager._terminate_loaded_metadata

    def check(**kwargs):
        result = original_check(**kwargs)
        if result.get("status") == "ready":
            result["container_id"] = "proven-container-id"
        return result

    def write(metadata):
        if metadata.get("container_id"):
            raise OSError("identity persistence failure")
        original_write(metadata)

    def terminate(metadata, **kwargs):
        terminated.append(dict(metadata))
        return original_terminate(metadata, **kwargs)

    monkeypatch.setattr(manager, "_check_readiness", check)
    monkeypatch.setattr(manager, "_write_metadata", write)
    monkeypatch.setattr(manager, "_terminate_loaded_metadata", terminate)
    with pytest.raises(OSError, match="identity persistence failure"):
        manager.start(cmd=server(port := free_port()), cwd=manager.root, readiness=readiness(port))
    assert terminated and terminated[0]["container_id"] == "proven-container-id"
    assert all(popen.poll() is not None for popen in manager._popens.values())


@pytest.mark.parametrize("raises", [False, True])
def test_partial_previous_cleanup_keeps_proven_candidate_available(manager, monkeypatch, raises):
    previous = manager.start(
        cmd=server(port := free_port()), cwd=manager.root, readiness=readiness(port)
    )
    original_stop = manager.stop

    def stop(service_id):
        result = original_stop(service_id)
        if service_id == previous.service_id and result.get("stopped"):
            # The old listener is gone but a later cleanup step was inconclusive.
            if raises:
                raise OSError("late cleanup persistence failed")
            return {**result, "stopped": False}
        return result

    monkeypatch.setattr(manager, "stop", stop)
    candidate = manager.start(
        cmd=server(new_port := free_port()),
        cwd=manager.root,
        readiness=readiness(new_port),
        replace_service_id=previous.service_id,
    )
    assert candidate.payload["handoff"]["status"] == "cleanup_pending"
    assert candidate.payload["handoff"]["candidate_preserved"] is True
    assert candidate.payload["failure_category"] == "handoff_cleanup_pending"
    assert manager.status(candidate.service_id)["readiness"]["endpoint_owned"] is True
    assert manager.status(previous.service_id)["alive"] is False


def test_stale_metadata_never_kills_an_unrelated_process(manager):
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        metadata = {
            "service_id": "svc_reused",
            "ownership": "DURABLE_SERVICE",
            "root": str(manager.root),
            "pid": unrelated.pid,
            "pid_start_token": "obsolete-creation-token",
            "readiness": {"type": "process_alive"},
        }
        manager._write_metadata(metadata)
        result = manager.stop("svc_reused")
        assert result["stopped"] is False
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=3)


def test_container_name_reuse_cannot_authorize_cleanup(manager, monkeypatch):
    started = manager.start(cmd=command("import time; time.sleep(30)"), cwd=manager.root)
    metadata = manager._read_metadata(started.service_id)
    metadata.update(backend="docker", container_name="reused-name", container_id="original-id")
    manager._write_metadata(metadata)
    monkeypatch.setattr(dsm.shutil, "which", lambda _name: "docker")
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        assert args[1] == "inspect", "must not remove the unrelated container"
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "id": "different-id",
                    "service": started.service_id,
                    "running": True,
                }
            ),
        )

    monkeypatch.setattr(dsm.subprocess, "run", run)
    result = manager.stop(started.service_id)
    assert result["stopped"] is False
    assert result["failure_category"] == "service_cleanup_unverified"
    assert result["cleanup_pending"] is True
    assert manager._read_metadata(started.service_id) is not None
    assert calls
    # This test's actual process is a host sleeper, already reaped. Restore its
    # real backend so fixture cleanup does not need a nonexistent Docker engine.
    metadata["backend"] = "host"
    manager._write_metadata(metadata)


@pytest.mark.parametrize(
    "label,listener,expected",
    [("svc_test", True, True), ("svc_other", True, False), ("svc_test", False, False)],
)
def test_container_proxy_requires_launch_identity_and_application_listener(
    monkeypatch, label, listener, expected
):
    monkeypatch.setattr(dsm.shutil, "which", lambda _name: "docker")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["timeout"] <= 0.300001
        if args[1] == "inspect":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "id": "immutable-id",
                        "service": label,
                        "running": True,
                        "ports": {"8091/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8091"}]},
                        "networks": {},
                    }
                ),
            )
        assert args[2] == "immutable-id"
        return SimpleNamespace(
            returncode=0, stdout="0: 00000000:1F9B 00000000:0000 0A" if listener else ""
        )

    monkeypatch.setattr(dsm.subprocess, "run", run)
    outcome = dsm._docker_endpoint_ownership(
        {"service_id": "svc_test", "container_name": "alysis-test"},
        peer=("127.0.0.1", 8091),
        timeout_s=0.3,
    )
    assert outcome["endpoint_owned"] is expected
    assert bool(len(calls) == 2) == bool(label == "svc_test")


@pytest.mark.parametrize("owner_header,owner", [("pid", "123"), ("process:pid", "Worker Name:123")])
def test_macos_listener_table_preserves_owner_and_wildcard_family(owner_header, owner):
    table = (
        f"Proto Recv-Q Send-Q Local Address Foreign Address (state) rhiwat shiwat {owner_header} epcb\n"
        f"tcp4 0 0 127.0.0.1.8080 *.* LISTEN 1 1 {owner} 0\n"
        f"tcp6 0 0 *.8081 *.* LISTEN 1 1 {owner} 0\n"
        f"tcp46 0 0 *.8082 *.* LISTEN 1 1 {owner} 0\n"
    )
    rows = dsm._parse_macos_tcp_listeners(table)
    assert [(r.laddr.ip, r.laddr.port, r.pid) for r in rows] == [
        ("127.0.0.1", 8080, 123),
        ("::", 8081, 123),
        ("0.0.0.0", 8082, 123),
        ("::", 8082, 123),
    ]


@pytest.mark.parametrize(
    "output", ["", "tcp4 0 0 unknown", "Proto Local Address (state) pid\ntcp4 malformed"]
)
def test_macos_listener_table_rejects_missing_or_unparseable_ownership(output):
    with pytest.raises(ValueError):
        dsm._parse_macos_tcp_listeners(output)
