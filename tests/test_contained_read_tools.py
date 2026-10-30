from __future__ import annotations

import base64
import json
import subprocess
from dataclasses import asdict

import pytest

import alysis_code.contained_read_tools as contained
from alysis_code.config import ConfigError
from alysis_code.host_read_worker import execute
from alysis_code.sandbox_settings import ShellSandboxSettings


def test_worker_dispatch_is_fixed_and_root_cannot_be_supplied(tmp_path):
    (tmp_path / "sample.py").write_text("def sample_function():\n    return 7\n")
    assert "sample.py" in json.dumps(execute("fs_list", {}, tmp_path))
    assert "sample_function" in json.dumps(
        execute("search_rg", {"pattern": "sample_function"}, tmp_path)
    )
    assert "sample_function" in json.dumps(
        execute("symbol_search", {"query": "sample_function"}, tmp_path)
    )
    for name, args in [
        ("shell_run", {}),
        ("fs_list", {"root": "/"}),
        ("fs_list", {"unexpected": True}),
    ]:
        with pytest.raises(ValueError):
            execute(name, args, tmp_path)


def test_reader_uses_strict_readonly_offline_policy_and_no_host_fallback(tmp_path, monkeypatch):
    built = []
    calls = []

    class Runner:
        def run(self, **kwargs):
            calls.append(kwargs)
            return subprocess.CompletedProcess(
                [], 0, json.dumps({"ok": True, "result": {"files": []}}), ""
            )

    def build(settings, root):
        built.append((settings, root))
        return Runner()

    monkeypatch.setattr(contained, "build_shell_runner_from_settings", build)
    settings = ShellSandboxSettings(backend="windows-mxc", network="on", unavailable_fallback="ask")
    worker = contained.ContainedReadTools.for_settings(settings, tmp_path)
    assert worker is not None
    assert worker.run("fs_list", {}) == {"files": []}
    assert worker.run("fs_list", {}) == {"files": []}
    assert len(built) == 1
    policy, root = built[0]
    assert root == tmp_path and policy.mode == "strict"
    assert policy.network == "off" and policy.filesystem_access == "read-only"
    assert policy.unavailable_fallback == "refuse" and policy.clear_env
    assert policy.protect_repo_meta and not policy.safe_git_writes
    command = calls[0]["cmd"]
    assert "'-I' '-B' '-m' 'alysis_code.host_read_worker'" in command
    assert calls[0]["cwd"] == tmp_path
    with pytest.raises(ConfigError, match="cannot run"):
        worker.run("fs_write", {})
    with pytest.raises(ConfigError, match="too large"):
        worker.run("search_rg", {"pattern": "x" * 13_000})
    assert len(calls) == 2


@pytest.mark.parametrize(
    "code,stdout",
    [
        (0, "not json"),
        (1, '{"ok":false,"error":"denied"}'),
        (0, '{"ok":true,"result":[]}'),
        (0, "x" * (2 * 1024 * 1024 + 257)),
    ],
    ids=["malformed", "denied", "wrong-shape", "oversized"],
)
def test_malformed_or_failed_worker_response_never_runs_host_reader(tmp_path, code, stdout):
    worker = contained.ContainedReadTools(ShellSandboxSettings(backend="windows-mxc"), tmp_path)

    class Runner:
        def run(self, **kwargs):
            return subprocess.CompletedProcess([], code, stdout, "error")

    worker.runner = Runner()
    with pytest.raises(ConfigError):
        worker.run("fs_list", {})


def test_posix_worker_quotes_payload_and_off_mode_keeps_explicit_host_semantics(
    tmp_path, monkeypatch
):
    from alysis_code.sandbox_runner import SandboxBackendChoice

    monkeypatch.setattr(
        contained, "select_shell_sandbox_backend", lambda _: SandboxBackendChoice("bwrap")
    )
    assert (
        contained.ContainedReadTools.for_settings(ShellSandboxSettings(mode="off"), tmp_path)
        is None
    )
    worker = contained.ContainedReadTools(ShellSandboxSettings(backend="bwrap"), tmp_path)
    calls = []

    class Runner:
        def run(self, **kwargs):
            calls.append(kwargs)
            return subprocess.CompletedProcess([], 0, '{"ok":true,"result":{}}', "")

    worker.runner = Runner()
    pattern = "'$(echo unsafe);`x`"
    worker.run("search_rg", {"pattern": pattern})
    assert pattern not in calls[0]["cmd"]
    encoded = calls[0]["cmd"].split()[-1]
    assert json.loads(base64.b64decode(encoded))["arguments"]["pattern"] == pattern


def test_recursive_worker_supports_normal_repo_and_test_discovery(tmp_path):
    (tmp_path / "test_sample.py").write_text("def test_sample():\n    assert 1 == 1\n")
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths=["."]\n')
    for name, args in [("repo_map", {"paths": ["test_sample.py"]}), ("test_discover", {})]:
        assert isinstance(execute(name, args, tmp_path), dict)


def test_strict_reader_refuses_missing_backend_instead_of_falling_back(tmp_path, monkeypatch):
    from alysis_code.sandbox_runner import SandboxBackendChoice

    monkeypatch.setattr(
        contained, "select_shell_sandbox_backend", lambda _: SandboxBackendChoice(None)
    )
    worker = contained.ContainedReadTools.for_settings(ShellSandboxSettings(), tmp_path)
    assert worker is not None
    with pytest.raises(ConfigError, match="no host fallback"):
        worker.run("fs_list", {})


def test_contained_prepared_write_preserves_stale_file_semantics(tmp_path):
    from alysis_code.host_read_worker import execute_write
    from alysis_code.tools.fs import StaleFileError, capture_file_precondition

    path = tmp_path / "ordinary.txt"
    path.write_text("old")
    args = {
        "path": path.name,
        "content": "new",
        "precondition": asdict(capture_file_precondition(root=tmp_path, path=path.name)),
    }
    assert execute_write("fs_write_prepared", args, tmp_path)["created"] is False
    assert path.read_text() == "new"
    with pytest.raises(StaleFileError):
        execute_write("fs_write_prepared", args, tmp_path)
    assert path.read_text() == "new"


def test_git_worker_keeps_real_index_and_object_store_unchanged(tmp_path, monkeypatch):
    import tempfile

    from alysis_code.host_read_worker import execute_write
    from alysis_code.tools.fs import capture_file_precondition

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    # The OS container's inherited TEMP may be unusable. The isolated worker
    # must still patch successfully without reaching outside its write grant.
    unavailable_temp = tmp_path / "not-a-temp-directory"
    unavailable_temp.write_text("synthetic")
    monkeypatch.setattr(tempfile, "tempdir", str(unavailable_temp))
    path = tmp_path / "ordinary.txt"
    path.write_text("old\n")
    before = {
        p.relative_to(tmp_path): p.read_bytes()
        for p in (tmp_path / ".git").rglob("*")
        if p.is_file()
    }
    result = execute_write(
        "git_apply_patch",
        {
            "patch": "diff --git a/ordinary.txt b/ordinary.txt\n--- a/ordinary.txt\n+++ b/ordinary.txt\n@@ -1 +1 @@\n-old\n+new\n",
            "preconditions": [asdict(capture_file_precondition(root=tmp_path, path=path.name))],
        },
        tmp_path,
    )
    assert result == {"applied": True}
    assert path.read_text() == "new\n"
    after = {
        p.relative_to(tmp_path): p.read_bytes()
        for p in (tmp_path / ".git").rglob("*")
        if p.is_file()
    }
    assert after == before
    assert not list(tmp_path.glob("alysis-patch-index-*"))


def test_git_worker_preserves_each_files_line_endings(tmp_path):
    from alysis_code.host_read_worker import execute_write

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    patch = ""
    for name, ending in (("unix.txt", b"\n"), ("windows.txt", b"\r\n")):
        (tmp_path / name).write_bytes(b"before" + ending)
        patch += f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-before\n+after\n"
    assert execute_write("git_apply_patch", {"patch": patch}, tmp_path) == {"applied": True}
    assert (tmp_path / "unix.txt").read_bytes() == b"after\n"
    assert (tmp_path / "windows.txt").read_bytes() == b"after\r\n"


def test_large_write_packet_is_digest_bound_and_removed_after_result(tmp_path, monkeypatch):
    import hashlib
    import shlex

    from alysis_code.sandbox_runner import SandboxBackendChoice

    monkeypatch.setattr(
        contained, "select_shell_sandbox_backend", lambda _: SandboxBackendChoice("bwrap")
    )
    worker = contained.ContainedReadTools(ShellSandboxSettings(backend="bwrap"), tmp_path)
    seen = []

    class Runner:
        def run(self, **kwargs):
            argv = shlex.split(kwargs["cmd"])
            packet = tmp_path / argv[-2]
            import os

            from alysis_code.host_file_handles import open_windows_packet_read

            with (
                open_windows_packet_read(packet) if os.name == "nt" else packet.open("rb")
            ) as stream:
                raw = stream.read()
            assert hashlib.sha256(raw).hexdigest() == argv[-1]
            assert len(json.loads(raw)["arguments"]["content"]) == 100_000
            seen.append(packet)
            return subprocess.CompletedProcess([], 0, '{"ok":true,"result":{"created":true}}', "")

    worker.write_runner = Runner()
    assert worker.run_write("fs_write_prepared", {"content": "x" * 100_000}) == {"created": True}
    assert seen and not seen[0].exists()
    readonly = contained.ContainedReadTools(
        ShellSandboxSettings(filesystem_access="read-only"), tmp_path
    )
    with pytest.raises(ConfigError, match="read-only"):
        readonly.run_write("fs_mkdir", {"path": "forbidden"})


def test_changed_packet_is_rejected_before_mutation(tmp_path, monkeypatch, capsys):
    from alysis_code.host_read_worker import main

    packet = tmp_path / "input.json"
    packet.write_text(json.dumps({"tool": "fs_mkdir", "arguments": {"path": "forbidden"}}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["worker", "--request-file", str(packet), "0" * 64])
    assert main() == 1
    assert "packet changed" in capsys.readouterr().out
    assert not (tmp_path / "forbidden").exists()


def test_windows_packet_creation_cannot_follow_metadata_only_root_junction(tmp_path, monkeypatch):
    import importlib.util
    import os
    from pathlib import Path

    from alysis_code.host_file_policy import HostFilePolicy
    from alysis_code.host_tool_packet import stage_packet

    if os.name != "nt":
        pytest.skip("native NT directory-relative creation")
    spec = importlib.util.spec_from_file_location(
        "reparse_probe", Path(__file__).parent / "fixtures/windows_mxc_reparse.py"
    )
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    root, private = tmp_path / "ordinary", tmp_path / "private"
    root.mkdir()
    private.mkdir()
    original = HostFilePolicy.guard_write
    injected = []

    def checked_then_replaced(self, base, name):
        original(self, base, name)
        if base == root and not injected:
            injected.append(probe.set_junction(root, private))
            assert injected[0]["error"] == 0

    monkeypatch.setattr(HostFilePolicy, "guard_write", checked_then_replaced)
    with pytest.raises(OSError):
        with stage_packet(root, b"synthetic"):
            pytest.fail("metadata-mutated packet parent was accepted")
    assert injected
    assert list(private.iterdir()) == []


def test_windows_packet_denies_other_writers_and_cleans_its_own_object(tmp_path):
    import os

    from alysis_code.host_file_handles import open_windows_packet_read
    from alysis_code.host_tool_packet import stage_packet

    if os.name != "nt":
        pytest.skip("Windows sharing contract")
    with stage_packet(tmp_path, b"synthetic") as packet:
        with open_windows_packet_read(packet) as stream:
            assert stream.read() == b"synthetic"
        with pytest.raises(PermissionError):
            packet.write_bytes(b"changed")
        with pytest.raises(PermissionError):
            packet.unlink()
    assert not packet.exists()
