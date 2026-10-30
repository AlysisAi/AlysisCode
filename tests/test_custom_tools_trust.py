from __future__ import annotations

import concurrent.futures
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import alysis_code.custom_tools.trust as trust_mod
from alysis_code.custom_tools.discovery import discover_custom_tools
from alysis_code.custom_tools.session import build_custom_tool_session_state
from alysis_code.custom_tools.trust import (
    ProjectToolTrustKey,
    ProjectToolTrustState,
    is_project_tool_trusted,
    load_trust_state,
    project_tool_trust_key,
    save_trust_state,
    trust_project_tool,
)
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.tools.registry import iter_builtin_tool_metadata


def _built_in_tool_names() -> set[str]:
    return {spec.name.casefold() for spec in iter_builtin_tool_metadata()}


def _write_tool(root: Path, rel_path: str, *, name: str) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "TOOL = {",
                f'    "name": "{name}",',
                '    "description": "Custom tool",',
                '    "input_schema": {"type": "object", "properties": {}, "required": []},',
                "}",
                "",
                "def run(args):",
                "    return {'ok': True}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_global_tools_are_implicitly_trusted(tmp_path: Path, monkeypatch) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_tool(cfg_dir, "tools/global_echo.py", name="global_echo")

    state = build_custom_tool_session_state(
        workspace_root=tmp_path / "workspace",
        custom_tools_enabled=True,
        mode="review",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        built_in_tool_names=_built_in_tool_names(),
    )

    [entry] = [entry for entry in state.catalog_entries if entry.name == "global_echo"]
    assert entry.trust == "global"
    assert entry.status == "available"


def test_project_tools_are_untrusted_by_default(tmp_path: Path, monkeypatch) -> None:
    cfg_dir = tmp_path / "config"
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_tool(workspace, ".alysis/tools/project_echo.py", name="project_echo")

    state = build_custom_tool_session_state(
        workspace_root=workspace,
        custom_tools_enabled=True,
        mode="review",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        built_in_tool_names=_built_in_tool_names(),
    )

    [entry] = [entry for entry in state.catalog_entries if entry.name == "project_echo"]
    assert entry.trust == "untrusted"
    assert entry.status == "untrusted"


def test_persistent_trust_is_keyed_by_workspace_relative_path_and_hash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg_dir = tmp_path / "config"
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_tool(workspace, ".alysis/tools/project_echo.py", name="project_echo")

    discovered = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )
    [spec] = discovered.project_tools
    trust_project_tool(spec)
    trust_state = load_trust_state()

    assert is_project_tool_trusted(spec, state=trust_state) is True
    assert any(
        key.relative_tool_path == ".alysis/tools/project_echo.py"
        and key.workspace_root == os.fspath(workspace.resolve())
        and key.file_hash == spec.file_hash
        for key in trust_state.trusted_tools
    )


def test_hash_changes_invalidate_persistent_trust(tmp_path: Path, monkeypatch) -> None:
    cfg_dir = tmp_path / "config"
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    path = _write_tool(workspace, ".alysis/tools/project_echo.py", name="project_echo")

    discovered = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )
    [spec] = discovered.project_tools
    trust_project_tool(spec)
    path.write_text(path.read_text(encoding="utf-8").replace("ok", "changed"), encoding="utf-8")
    refreshed = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )
    [updated_spec] = refreshed.project_tools

    assert updated_spec.file_hash != spec.file_hash
    assert is_project_tool_trusted(updated_spec, state=load_trust_state()) is False


def test_save_trust_state_uses_atomic_json_writer(tmp_path: Path, monkeypatch) -> None:
    cfg_dir = tmp_path / "config"
    captured: dict[str, object] = {}

    def fake_atomic_write_json(path: Path, payload: object, **kwargs: object) -> None:
        captured["path"] = path
        captured["payload"] = payload
        captured["kwargs"] = kwargs

    monkeypatch.setattr(
        "alysis_code.custom_tools.trust.atomic_write_json",
        fake_atomic_write_json,
    )

    state = ProjectToolTrustState(
        trusted_tools=(
            ProjectToolTrustKey(
                workspace_root="/workspace",
                relative_tool_path=".alysis/tools/project_echo.py",
                file_hash="abc123",
            ),
        )
    )
    save_trust_state(state, user_config_dir=cfg_dir)

    assert captured["path"] == cfg_dir.resolve() / "custom_tools_trust.json"
    assert captured["kwargs"] == {"ensure_ascii": True}
    assert captured["payload"] == {
        "schema_version": 1,
        "trusted_tools": [
            {
                "workspace_root": "/workspace",
                "relative_tool_path": ".alysis/tools/project_echo.py",
                "file_hash": "abc123",
            }
        ],
    }


def test_concurrent_same_process_trust_updates_do_not_lose_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dir = tmp_path / "config"
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_tool(workspace, ".alysis/tools/alpha.py", name="alpha")
    _write_tool(workspace, ".alysis/tools/bravo.py", name="bravo")
    discovered = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )
    specs = {tool.name: tool for tool in discovered.project_tools}
    expected_keys = {
        project_tool_trust_key(specs["alpha"]),
        project_tool_trust_key(specs["bravo"]),
    }

    original_load = trust_mod._load_trust_state_from_path
    first_load_started = threading.Event()
    allow_first_load_finish = threading.Event()
    second_load_started = threading.Event()
    first_call_lock = threading.Lock()
    first_call_pending = True

    def blocking_load(path: Path) -> ProjectToolTrustState:
        nonlocal first_call_pending
        state = original_load(path)
        with first_call_lock:
            is_first = first_call_pending
            if first_call_pending:
                first_call_pending = False
        if is_first:
            first_load_started.set()
            assert allow_first_load_finish.wait(timeout=5)
        else:
            second_load_started.set()
        return state

    monkeypatch.setattr(trust_mod, "_load_trust_state_from_path", blocking_load)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(trust_project_tool, specs["alpha"], user_config_dir=cfg_dir)
        assert first_load_started.wait(timeout=5)
        second_future = pool.submit(trust_project_tool, specs["bravo"], user_config_dir=cfg_dir)
        assert not second_load_started.wait(timeout=0.5)
        allow_first_load_finish.set()
        first_future.result(timeout=5)
        second_future.result(timeout=5)

    final_state = load_trust_state(user_config_dir=cfg_dir)
    assert expected_keys.issubset(set(final_state.trusted_tools))


def test_trust_discovery_does_not_import_provider_configuration(tmp_path: Path) -> None:
    code = """
import importlib.abc
import sys

class RejectProviderImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'alysis_code.config', 'httpx', 'httpcore', 'pydantic'}:
            raise AssertionError(f'trust discovery imported unrelated backend: {fullname}')

guard = RejectProviderImports()
sys.meta_path.insert(0, guard)
from alysis_code.custom_tools import discovery, trust
from alysis_code.tools.registry import iter_builtin_tool_metadata
from alysis_code.runtime_kind import ConfigError, normalize_runtime_kind
assert tuple(iter_builtin_tool_metadata())
try:
    normalize_runtime_kind('invalid-runtime')
except ConfigError as exc:
    assert 'Invalid runtime kind' in str(exc)
else:
    raise AssertionError('invalid runtime accepted')
sys.meta_path.remove(guard)

# Existing import paths and exception handlers must retain the same class.
from alysis_code.config import ConfigError as PublicConfigError
import pickle
assert PublicConfigError is ConfigError
assert type(pickle.loads(pickle.dumps(ConfigError('synthetic')))) is PublicConfigError
"""
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        env={
            **os.environ,
            "PYTHONPATH": os.fspath(repo_root / "src"),
            "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "config"),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_concurrent_cross_process_trust_updates_do_not_lose_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dir = tmp_path / "config"
    workspace = tmp_path / "workspace"
    coord_dir = tmp_path / "coord"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    coord_dir.mkdir(parents=True, exist_ok=True)
    _write_tool(workspace, ".alysis/tools/alpha.py", name="alpha")
    _write_tool(workspace, ".alysis/tools/bravo.py", name="bravo")
    discovered = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )
    specs = {tool.name: tool for tool in discovered.project_tools}
    expected_keys = {
        project_tool_trust_key(specs["alpha"]),
        project_tool_trust_key(specs["bravo"]),
    }
    repo_root = Path(__file__).resolve().parents[1]
    worker = """
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ["PYTHONPATH"])

import alysis_code.custom_tools.trust as trust_mod
from alysis_code.custom_tools.discovery import discover_custom_tools
from alysis_code.tools.registry import iter_builtin_tool_metadata

workspace = Path(sys.argv[1])
tool_name = sys.argv[2]
role = os.environ["TRUST_TEST_ROLE"]
coord_dir = Path(os.environ["TRUST_TEST_COORD_DIR"])
loaded_marker = coord_dir / f"{role}.loaded"
release_marker = coord_dir / "release"
original_load = trust_mod._load_trust_state_from_path

def patched_load(path):
    state = original_load(path)
    loaded_marker.write_text("loaded", encoding="utf-8")
    if role == "first":
        deadline = time.monotonic() + 10
        while not release_marker.exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("timed out waiting for release")
            time.sleep(0.01)
    return state

trust_mod._load_trust_state_from_path = patched_load
built_in_tool_names = {spec.name.casefold() for spec in iter_builtin_tool_metadata()}
result = discover_custom_tools(
    workspace_root=workspace,
    built_in_tool_names=built_in_tool_names,
)
spec = next(tool for tool in result.project_tools if tool.name == tool_name)
trust_mod.trust_project_tool(spec)
"""
    base_env = {
        **os.environ,
        "PYTHONPATH": os.fspath(repo_root / "src"),
        "ALYSIS_CONFIG_DIR": os.fspath(cfg_dir),
        "TRUST_TEST_COORD_DIR": os.fspath(coord_dir),
    }

    first_proc = subprocess.Popen(
        [sys.executable, "-c", worker, os.fspath(workspace), "alpha"],
        cwd=repo_root,
        env={**base_env, "TRUST_TEST_ROLE": "first"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert _wait_for_path(coord_dir / "first.loaded")
    second_proc = subprocess.Popen(
        [sys.executable, "-c", worker, os.fspath(workspace), "bravo"],
        cwd=repo_root,
        env={**base_env, "TRUST_TEST_ROLE": "second"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert not _wait_for_path(coord_dir / "second.loaded", timeout=0.5)
    (coord_dir / "release").write_text("release\n", encoding="utf-8")

    first_stdout, first_stderr = first_proc.communicate(timeout=10)
    second_stdout, second_stderr = second_proc.communicate(timeout=10)
    assert first_proc.returncode == 0, first_stdout + first_stderr
    assert second_proc.returncode == 0, second_stdout + second_stderr

    final_state = load_trust_state(user_config_dir=cfg_dir)
    assert expected_keys.issubset(set(final_state.trusted_tools))


def _wait_for_path(path: Path, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.01)
    return path.exists()
