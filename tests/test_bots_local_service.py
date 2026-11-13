from __future__ import annotations

import errno
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from alysis_code.config import AppConfig, save_config


def test_profiles_report_only_implemented_bots_protocols(tmp_path, monkeypatch):
    from alysis_code.bots.local_service import profiles

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_LOCAL_TEST_KEY", "placeholder")
    cfg = AppConfig(model="test-model", base_url="https://models.example.invalid/v1")
    cfg.extra_fields["profiles"] = {
        name: {
            "protocol": protocol,
            "base_url": cfg.base_url,
            "default_model": cfg.model,
            "api_key_env": "ALYSIS_LOCAL_TEST_KEY",
        }
        for name, protocol in (
            ("supported", "openai_compat"),
            ("other-protocol", "gemini_generate_content"),
        )
    }
    cfg.extra_fields["active_profile"] = "supported"
    save_config(cfg)
    available = {item["name"]: item["available"] for item in profiles()["profiles"]}
    assert available["supported"] is True
    assert available["other-protocol"] is False


def test_service_auth_storage_restart_and_parent_lifetime(tmp_path, monkeypatch):
    config = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(config))
    cfg = AppConfig(model="test-model", base_url="https://models.example.invalid/v1")
    cfg.extra_fields["profiles"] = {
        "test": {
            "protocol": "openai_compat",
            "base_url": cfg.base_url,
            "default_model": cfg.model,
            "api_key_env": "ALYSIS_LOCAL_TEST_KEY",
        }
    }
    cfg.extra_fields["active_profile"] = "test"
    save_config(cfg)
    env = {key: value for key, value in os.environ.items() if not key.startswith("ALYSIS_")}
    env.update(ALYSIS_CONFIG_DIR=str(config), ALYSIS_LOCAL_TEST_KEY="placeholder-model-key")
    token = secrets.token_hex(32)
    bootstrap = {"directory": str(tmp_path / "data"), "token": token, "profile": "test"}

    def start():
        child = subprocess.Popen(
            [sys.executable, "-I", "-m", "alysis_code.cli", "bots", "serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        child.stdin.write(json.dumps(bootstrap).encode() + b"\n")
        child.stdin.flush()
        ready = json.loads(child.stdout.readline())
        assert ready["type"] == "ready"
        assert token not in json.dumps(ready)
        return child, ready["url"]

    child, url = start()
    try:
        with httpx.Client(base_url=url, trust_env=False) as client:
            assert client.get("/v1/bots").status_code == 401
            client.headers["Authorization"] = "Bearer " + token
            response = client.post(
                "/v1/bots", json={"name": "Καλημέρα 東京", "purpose": "Persistent local work"}
            )
            assert response.status_code == 201
            bot = response.json()
            assert client.get("/v1/status").json()["mode"] == "local"
    finally:
        child.stdin.close()
        assert child.wait(timeout=15) == 0
    with pytest.raises(httpx.TransportError):
        httpx.get(url + "/v1/bots", trust_env=False)
    child, url = start()
    try:
        response = httpx.get(
            url + "/v1/bots", headers={"Authorization": "Bearer " + token}, trust_env=False
        )
        assert response.status_code == 200
        assert [item["id"] for item in response.json()] == [bot["id"]]
        assert response.json()[0]["name"] == "Καλημέρα 東京"
    finally:
        child.stdin.close()
        assert child.wait(timeout=15) == 0


def test_vm_runner_closes_hypervisor_when_controller_pipe_closes(tmp_path):
    import psutil

    from alysis_code.bots import vm_runner

    child = subprocess.Popen(
        [sys.executable, "-I", str(Path(vm_runner.__file__))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    child.stdin.write(
        json.dumps({"argv": [sys.executable, "-I", "-c", "import time; time.sleep(120)"]}).encode()
        + b"\n"
    )
    child.stdin.flush()
    pid = json.loads(child.stdout.readline())["pid"]
    assert psutil.pid_exists(pid)
    child.stdin.close()
    assert child.wait(timeout=12) == 0
    assert not psutil.pid_exists(pid)


def test_private_desktop_runtime_exposes_code_without_base_configuration(tmp_path, monkeypatch):
    from alysis_code.bots import desktop_linux

    prefix = tmp_path / "environment"
    prefix.mkdir()
    base = tmp_path / "base"
    (base / "bin").mkdir(parents=True)
    (base / "lib").mkdir()
    (base / "private-config").write_text("placeholder")
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "base_prefix", str(base))
    monkeypatch.setattr(sys, "base_exec_prefix", str(base))
    paths = desktop_linux.runtime_paths()
    assert prefix in paths
    assert base not in paths
    assert base / "lib" in paths
    assert base / "private-config" not in paths
    monkeypatch.setattr(sys, "prefix", str(Path.home()))
    with pytest.raises(desktop_linux.DesktopError):
        desktop_linux.runtime_paths()


def test_local_vm_disk_refusal_precedes_all_mutations(tmp_path, monkeypatch):
    from alysis_code.bots import local_vm
    from alysis_code.bots.compute import ComputeCreationRefused, ComputerIdentity, ComputerSpec

    # Exercise the real preflight without booting a hypervisor or depending on
    # the test host's available disk space or operating system.
    provider = object.__new__(local_vm.LocalVmComputers)
    provider.root = tmp_path / "computers"
    provider.root.mkdir()
    provider.image = tmp_path / "disk.raw"
    provider.image.write_bytes(b"synthetic image")
    provider._lock = threading.RLock()
    from alysis_code.bots.computer import ComputerLocks

    provider._operations = ComputerLocks()
    provider._starting = set()
    provider._processes = {}
    monkeypatch.setattr(local_vm.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    monkeypatch.setattr(provider, "_boot", lambda *_: pytest.fail("A refused VM must not boot"))
    identity = ComputerIdentity("account", "bot", uuid4().hex)
    with pytest.raises(ComputeCreationRefused) as caught:
        provider.create(identity, ComputerSpec("private-image"))
    assert caught.value.code == "insufficient_local_storage"
    assert list(provider.root.iterdir()) == []
    assert provider._processes == {}


@pytest.mark.parametrize(
    "error,code",
    [
        (OSError(errno.ENOSPC, "No space left on device"), "disk_full"),
        (sqlite3.OperationalError("database or disk is full"), "disk_full"),
        (PermissionError(errno.EACCES, "Access is denied"), "storage"),
        (ValueError("Set a CPU and RAM budget for private computers."), "settings"),
        (RuntimeError("Local service did not start."), "internal"),
    ],
)
def test_a_start_failure_is_named_by_a_fixed_code(error, code):
    from alysis_code.bots.local_service import start_failure_code

    assert start_failure_code(error) == code


def test_a_service_that_cannot_start_says_why_on_its_ready_channel():
    """The app reads one stdout line: a failed start announces an error code, never its text."""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio; from alysis_code.bots.local_service import serve; asyncio.run(serve())",
        ],
        input=b"not a bootstrap\n",
        capture_output=True,
        timeout=60,
    )
    assert child.returncode != 0
    first = child.stdout.decode().splitlines()[0]
    assert json.loads(first) == {"type": "error", "protocol": 1, "code": "settings"}
