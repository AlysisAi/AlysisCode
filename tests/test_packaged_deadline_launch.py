"""Adapter argument propagation through a real CLI process and loopback provider.

The host fixture maps the Unix container paths to a Windows/POSIX temporary
workspace. Container installation remains separately qualified by setup tests.
This file can be copied beside an installed wheel's qualification tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_deadline_fault_injection import _fault_server

import alysis_code
from alysis_code.managed_host_deadline import MANAGED_HOST_DEADLINE_UNIX_ENV
from scripts.benchmarks.terminal_bench import box_harbor_agent as adapter


@pytest.mark.parametrize("delayed_start", [False, True], ids=["stalled_provider", "startup_expiry"])
def test_real_adapter_launch_installed_cli_stops_stalled_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delayed_start: bool,
) -> None:
    monkeypatch.setattr(
        adapter,
        "evaluate_adapter_provenance",
        lambda **_: SimpleNamespace(
            payload=lambda: {"allowed": True},
            failed=False,
        ),
    )
    records: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    # A cold installed CLI must finish importing before the stalled-provider
    # case can exercise network cancellation. Keep startup expiry independently
    # bounded by the inherited one-second deadline below.
    host_budget_seconds = 15
    # Keep the injected stall alive beyond the host budget. A five-second
    # disconnect can finish first on a fast startup and tests provider failure,
    # not cancellation of a stalled request.
    with _fault_server("headers", stall_seconds=host_budget_seconds + 1) as (url, connected):
        agent = adapter.AlysisAgent(
            logs_dir=tmp_path / "logs",
            extra_env={
                "ALYSIS_MODEL": "local-qualification",
                "ALYSIS_API_KEY": "loopback-only",
                "ALYSIS_BASE_URL": url.rsplit("/", 1)[0] + "/v1",
                "ALYSIS_MAX_STEPS": "2",
                "ALYSIS_REQUIRE_CLEAN_BUILD": "0",
                "ALYSIS_RUN_PROFILE": "fullaccess",
                # The launcher passes --strict-provenance, which refuses a run
                # without an explicit thinking setting, so the test declares one.
                "ALYSIS_LLM_ENABLE_THINKING": "false",
                **({MANAGED_HOST_DEADLINE_UNIX_ENV: str(time.time() + 1)} if delayed_start else {}),
            },
        )

        async def exec_as_agent(_environment: Any, **kwargs: Any) -> Any:
            command = kwargs["command"]
            if "alysis run" not in command:
                return SimpleNamespace(stdout="")
            arguments = shlex.split(command.split("alysis run", 1)[1].split("</dev/null", 1)[0])
            env = dict(os.environ)
            env.update(kwargs["env"])
            guard_dir = tmp_path / "network_guard"
            guard_dir.mkdir(exist_ok=True)
            (guard_dir / "sitecustomize.py").write_text(
                f"import time\ntime.sleep({1.1 if delayed_start else 0})\n"
                "import socket\n"
                "_resolve = socket.getaddrinfo\n"
                "def _local(host, *args, **kwargs):\n"
                "    if host not in ('127.0.0.1', 'localhost', '::1', None):\n"
                "        raise OSError('qualification forbids non-loopback network')\n"
                "    return _resolve(host, *args, **kwargs)\n"
                "socket.getaddrinfo = _local\n"
            )
            env["PYTHONPATH"] = os.pathsep.join(
                [
                    str(guard_dir),
                    str(Path(alysis_code.__file__).resolve().parents[1]),
                ]
            )
            env["ALYSIS_CONFIG_DIR"] = str(tmp_path / "config")
            env["ALYSIS_DATA_DIR"] = str(tmp_path / "data")
            env["ALYSIS_WEB_TOOLS"] = "off"
            env["ALYSIS_TERMINAL_OWNERSHIP_DIR"] = str(tmp_path / "ownership")
            outcome_path = tmp_path / Path(env["ALYSIS_TASK_OUTCOME_PATH"]).name
            env["ALYSIS_TASK_OUTCOME_PATH"] = str(outcome_path)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "alysis_code",
                "run",
                *arguments,
                cwd=tmp_path,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await process.communicate()
            except BaseException:
                process.kill()
                await process.wait()
                raise
            assert outcome_path.is_file(), stderr.decode(errors="replace")
            record = json.loads(outcome_path.read_text())
            records.append(record)
            diagnostics.append(stderr.decode(errors="replace"))
            marker = re.search(r"ALYSIS_TASK_OUTCOME_[a-f0-9]{32}=", command)[0]
            return SimpleNamespace(
                stdout=stdout.decode(errors="replace") + "\n" + marker + json.dumps(record)
            )

        agent.exec_as_agent = exec_as_agent
        context = SimpleNamespace(
            metadata={
                "managed_host_deadline": {
                    "remaining_seconds": host_budget_seconds,
                    "shutdown_reserve_seconds": 1,
                }
            }
        )
        started = time.monotonic()
        asyncio.run(agent.run("Inspect the files in this workspace.", object(), context))
        elapsed = time.monotonic() - started
        if delayed_start:
            assert not connected.is_set(), (
                "startup consumed the inherited expiry; no request may start"
            )
        else:
            assert connected.is_set(), (
                "the real CLI must reach the loopback provider",
                records,
                diagnostics,
            )
    assert elapsed < (7 if delayed_start else host_budget_seconds + 1)
    assert records[-1]["terminal"] is True
    assert records[-1]["outcome"] == "deadline_exceeded", (records, diagnostics)
    assert records[-1]["verified_success"] is False
    assert context.metadata["alysis_task_outcome"] == records[-1]
    session_events = [
        json.loads(line)
        for path in (tmp_path / "data" / "sessions").glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    anchors = [
        event["payload"]
        for event in session_events
        if event["type"] == "managed_host_deadline_anchor"
    ]
    assert len(anchors) == 1
    assert anchors[0]["narrowed_by_seconds"] > 0
    if delayed_start:
        assert anchors[0]["status"] == "exhausted"
        assert not any(event["type"] == "provider_failure_salvage" for event in session_events)
