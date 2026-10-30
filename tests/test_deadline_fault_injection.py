"""Real loopback failures, with no paid provider or benchmark workload."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from alysis_code.budget_policy import BudgetCancellationToken, BudgetWatchdog
from alysis_code.cancellation import (
    CombinedCancellationToken,
    CooperativeCancellationError,
    InteractiveCancellationToken,
)
from alysis_code.llm.http_cancellation import cancellable_httpx_request
from alysis_code.llm.provider_limits import ProviderRetrySettings, run_provider_limited_call


@contextmanager
def _fault_server(
    phase: str, *, stall_seconds: float = 5.0
) -> Iterator[tuple[str, threading.Event]]:
    stop = threading.Event()
    connected = threading.Event()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    listener.settimeout(0.05)
    port = listener.getsockname()[1]

    def serve() -> None:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                connection.settimeout(0.1)
                connected.set()
                if phase in {"tls_handshake", "headers"}:
                    stop.wait(stall_seconds)
                    continue
                try:
                    request = b""
                    while b"\r\n\r\n" not in request and not stop.is_set():
                        request += connection.recv(4096)
                    if phase == "transient":
                        connection.sendall(b"HTTP/1.1 503 Unavailable\r\ncontent-length: 0\r\n\r\n")
                        continue
                    connection.sendall(b"HTTP/1.1 200 OK\r\ncontent-length: 100000\r\n\r\n")
                    if phase == "body":
                        stop.wait(stall_seconds)
                    else:
                        while not stop.wait(0.02):
                            connection.sendall(b"x")
                except OSError:
                    continue

    worker = threading.Thread(target=serve, daemon=True, name=f"fault-server-{phase}")
    worker.start()
    try:
        scheme = "https" if phase == "tls_handshake" else "http"
        yield f"{scheme}://127.0.0.1:{port}/fault", connected
    finally:
        stop.set()
        listener.close()
        worker.join(2)
        assert not worker.is_alive(), "owned loopback server did not retire"


@pytest.mark.parametrize("phase", ["tls_handshake", "headers", "body", "trickle"])
@pytest.mark.parametrize("stream", [True, False])
def test_hard_deadline_interrupts_real_network_fault(phase: str, stream: bool) -> None:
    watchdog = BudgetWatchdog(budget_seconds=0.4, grace_seconds=0)
    token = BudgetCancellationToken(watchdog.event, error_class=CooperativeCancellationError)
    errors: list[BaseException] = []

    with _fault_server(phase) as (url, connected):

        def request() -> None:
            try:
                with httpx.Client(timeout=10, verify=False, trust_env=False) as client:
                    with cancellable_httpx_request(
                        client=client,
                        cancellation_token=token,
                        method="GET",
                        url=url,
                        stream=stream,
                    ) as response:
                        if stream:
                            list(response.iter_raw())
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=request, daemon=True, name="deadline-fault-request")
        started = time.monotonic()
        with watchdog:
            worker.start()
            assert connected.wait(1)
            worker.join(2)
        elapsed = time.monotonic() - started
        # This is the supported local regression threshold, not a universal SLA.
        assert not worker.is_alive(), f"{phase} outlived hard deadline"
        assert elapsed < 2
    assert len(errors) == 1
    assert isinstance(errors[0], CooperativeCancellationError)
    assert errors[0].reason == "run_budget_exhausted"


def test_repeated_transient_responses_and_backoff_share_the_deadline() -> None:
    watchdog = BudgetWatchdog(budget_seconds=0.3, grace_seconds=0)
    token = BudgetCancellationToken(watchdog.event, error_class=CooperativeCancellationError)
    attempts: list[float] = []
    # Construct the client before the deadline: this test measures requests and
    # retry backoff, not cold HTTP transport imports on a loaded runner.
    with (
        _fault_server("transient") as (url, _),
        httpx.Client(timeout=10, verify=False, trust_env=False) as client,
    ):

        def call() -> None:
            attempts.append(time.monotonic())
            with cancellable_httpx_request(
                client=client, cancellation_token=token, method="GET", url=url, stream=False
            ) as response:
                response.raise_for_status()

        started = time.monotonic()
        with watchdog, pytest.raises(CooperativeCancellationError, match="run_budget_exhausted"):
            run_provider_limited_call(
                call=call,
                provider_key=None,
                operation="qualification",
                cancellation_token=token,
                retry_settings=ProviderRetrySettings(
                    max_retries=100, base_delay_seconds=0.04, max_delay_seconds=0.04
                ),
                random_fn=lambda: 0.5,
            )
        assert time.monotonic() - started < 2
    assert len(attempts) >= 2


@pytest.mark.parametrize("cause", ["caller", "deadline"])
def test_combined_token_preserves_cancellation_authority(cause: str) -> None:
    caller = InteractiveCancellationToken()
    expired = threading.Event()
    budget = BudgetCancellationToken(expired, error_class=CooperativeCancellationError)
    token = CombinedCancellationToken(caller, budget)
    assert not token.wait(0)
    if cause == "caller":
        caller.cancel()
        expected: type[BaseException] = KeyboardInterrupt
        reason = "cancelled_by_user"
    else:
        expired.set()
        expected = CooperativeCancellationError
        reason = "run_budget_exhausted"
    assert token.wait(0.1)
    with pytest.raises(expected, match=reason):
        token.throw_if_cancelled()


def test_run_agent_retains_hard_deadline_with_external_caller_token(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from alysis_code import agent_loop
    from alysis_code.config import AppConfig

    caller = InteractiveCancellationToken()
    observed: list[Any] = []

    class Session:
        store = type("Store", (), {"append": lambda *args: None})()
        crash_diagnostics = None

        def run_turn(self, instruction: str, *, cancellation_token: Any, **_: Any) -> int:
            assert instruction == "bounded work"
            assert cancellation_token is not caller
            assert cancellation_token.wait(2)
            with pytest.raises(CooperativeCancellationError, match="run_budget_exhausted"):
                cancellation_token.throw_if_cancelled()
            observed.append(cancellation_token)
            return 0

        def close(self) -> None:
            pass

    monkeypatch.setattr(agent_loop, "create_session", lambda **_: Session())
    monkeypatch.setenv("ALYSIS_BUDGET_GRACE_SECONDS", "60")
    started = time.monotonic()
    assert (
        agent_loop.run_agent(
            cfg=AppConfig(model="test-model"),
            root=tmp_path,
            instruction="bounded work",
            mode="fullaccess",
            yes=True,
            max_steps=1,
            no_log=True,
            cancellation_token=caller,
            run_deadline_seconds=0.2,
        )
        == 0
    )
    assert time.monotonic() - started < 2
    assert observed and not caller.is_cancelled


def test_blocked_tool_timeout_reaps_stubborn_child_and_retains_artifact(tmp_path: Path) -> None:
    import psutil

    from alysis_code.process_reaping import ProcessGroupRegistry, reap_tracked_groups
    from alysis_code.sandbox_runner import HostShellRunner

    worker_path = tmp_path / "worker.py"
    worker_path.write_text(
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "Path('child.pid').write_text(str(os.getpid()))\n"
        "Path('retained.txt').write_text('finished portion')\n"
        "time.sleep(60)\n"
    )
    parent_path = tmp_path / "parent.py"
    parent_path.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, 'worker.py'])\n"
        "time.sleep(60)\n"
    )
    import shlex

    argv = [sys.executable, str(parent_path)]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    registry = ProcessGroupRegistry()
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            HostShellRunner(process_group_registry=registry).run(
                root=tmp_path,
                cwd=tmp_path,
                cmd=command,
                timeout_s=1,
            )
    finally:
        reap_tracked_groups(registry, grace_seconds=0.2)
        pid_path = tmp_path / "child.pid"
        pid = int(pid_path.read_text()) if pid_path.exists() else 0
        if pid:
            try:
                process = psutil.Process(pid)
                alive = process.is_running() and process.status() != psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                alive = False
            if alive:
                process.kill()
                process.wait(3)
            assert not alive, "owned stubborn subprocess survived deadline cleanup"
    assert pid > 0, "test must actually start the stubborn child"
    assert time.monotonic() - started < 5
    assert (tmp_path / "retained.txt").read_text() == "finished portion"
