from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from alysis_code.agent_loop import AgentRuntimeError, build_tools
from alysis_code.config import ConfigError
from alysis_code.host_file_policy import HostFilePolicy, HostFilePolicyError
from alysis_code.session_store import SessionStore
from alysis_code.surface import ApprovalDecision, NoopSurface


@pytest.mark.parametrize("exists", [False, True])
def test_custom_memory_is_private_even_before_creation(tmp_path, monkeypatch, exists):
    memory = tmp_path / "private-memory"
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(memory))
    if exists:
        memory.mkdir()
        (memory / "MEMORY.md").write_text("synthetic project memory")
    policy = HostFilePolicy.capture()
    with pytest.raises(HostFilePolicyError, match="private host"):
        policy.guard_read(tmp_path, "private-memory/MEMORY.md")
    with pytest.raises(HostFilePolicyError, match="protected host"):
        policy.guard_write(tmp_path, "private-memory/MEMORY.md")
    with pytest.raises(HostFilePolicyError, match="protected host"):
        policy.guard_write(tmp_path, ".")


@pytest.fixture
def protected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # This fixture isolates the host file layer. Tests exercising a contained
    # worker or strict Full access explicitly opt back into strict mode.
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
    root = tmp_path / "workspace"
    root.mkdir()
    areas = {
        name: root / "owned" / name for name in ("runtime", "control", "configuration", "data")
    }
    for path in areas.values():
        path.mkdir(parents=True)
        (path / "value.txt").write_text("synthetic original\n", encoding="utf-8")
    ordinary = root / "project"
    ordinary.mkdir()
    (ordinary / "input.txt").write_text("ordinary original\n", encoding="utf-8")
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(areas["configuration"]))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(areas["data"]))
    monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", json.dumps([str(areas["runtime"])]))
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", json.dumps([str(areas["control"])]))

    def tools(mode="auto", surface=None, cfg=None):
        return build_tools(
            root=root,
            console=None,
            mode=mode,
            cfg=cfg,
            yes=True,
            surface=surface,
            non_interactive=surface is None,
            store=SessionStore(
                enabled=False,
                sessions_dir=tmp_path / "sessions",
                session_id="host-policy",
                cwd=str(root),
                repo_root=str(root),
            ),
        )

    return root, areas, tools


@pytest.mark.parametrize("area", ["runtime", "control", "configuration", "data"])
@pytest.mark.parametrize(
    "operation",
    [
        "fs_write",
        "fs_edit",
        "fs_delete",
        "move_source",
        "move_destination",
        "copy_destination",
        "fs_mkdir",
    ],
)
def test_protected_writes_cannot_bypass_shell_policy(protected, area, operation):
    root, areas, build = protected
    path = (areas[area] / "value.txt").relative_to(root).as_posix()
    tools = build()
    tool, args = {
        "fs_write": ("fs_write", {"path": path, "content": "changed"}),
        "fs_edit": ("fs_edit", {"path": path, "edits": [{"op": "append", "content": "changed"}]}),
        "fs_delete": ("fs_delete", {"path": path}),
        "move_source": ("fs_move", {"source_path": path, "destination_path": "project/stolen.txt"}),
        "move_destination": (
            "fs_move",
            {"source_path": "project/input.txt", "destination_path": path, "overwrite": True},
        ),
        "copy_destination": (
            "fs_copy",
            {"source_path": "project/input.txt", "destination_path": path, "overwrite": True},
        ),
        "fs_mkdir": ("fs_mkdir", {"path": str(Path(path).parent / "new-folder")}),
    }[operation]
    with pytest.raises(AgentRuntimeError, match="protected host"):
        tools[tool].run(args)
    assert (areas[area] / "value.txt").read_text() == "synthetic original\n"
    assert (root / "project/input.txt").read_text() == "ordinary original\n"
    assert not (root / "project/stolen.txt").exists()


@pytest.mark.parametrize("area", ["control", "configuration", "data"])
@pytest.mark.parametrize(
    "tool", ["fs_read", "asset_view", "fs_copy", "search_rg", "symbol_search", "fs_list"]
)
def test_private_contents_cannot_be_read_or_copied(protected, area, tool):
    root, areas, build = protected
    path = (areas[area] / "value.txt").relative_to(root).as_posix()
    args = {
        "path": path,
        "source_path": path,
        "destination_path": "project/exfiltrated.txt",
        "root_path": str(Path(path).parent),
        "pattern": "synthetic",
        "query": "synthetic",
    }
    with pytest.raises(AgentRuntimeError, match="private host state"):
        build()[tool].run(args)
    assert not (root / "project/exfiltrated.txt").exists()


@pytest.mark.parametrize(
    "tool",
    [
        "fs_list",
        "search_rg",
        "symbol_search",
        "repo_map",
        "test_discover",
        "git_diff",
        "git_status",
    ],
)
def test_recursive_readers_refuse_containing_private_trees(protected, tool):
    _root, _areas, build = protected
    with pytest.raises(AgentRuntimeError, match="narrower ordinary project"):
        build()[tool].run({"pattern": "synthetic", "query": "synthetic"})


@pytest.mark.parametrize(
    "tool,args",
    [
        ("fs_move", {"source_path": "owned", "destination_path": "other"}),
        ("fs_move", {"source_path": "project/input.txt", "destination_path": "owned"}),
        ("fs_delete", {"path": "owned"}),
        ("fs_mkdir", {"path": "owned"}),
    ],
)
def test_ancestor_replacement_is_refused_before_file_preparation(protected, tool, args):
    _root, _areas, build = protected
    with pytest.raises(AgentRuntimeError, match="protected host"):
        build()[tool].run(args)


def test_policy_is_immutable_after_tools_are_built(protected, monkeypatch):
    root, areas, build = protected
    tools = build()
    monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", "[]")
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", "[]")
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(root / "unrelated-config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(root / "unrelated-data"))
    for target in areas.values():
        with pytest.raises(AgentRuntimeError, match="protected host"):
            tools["fs_write"].run(
                {"path": str((target / "value.txt").relative_to(root)), "content": "changed"}
            )


def test_normal_files_and_runtime_reads_remain_usable(protected):
    root, _areas, build = protected
    tools = build()
    assert (
        "synthetic original" in tools["fs_read"].run({"path": "owned/runtime/value.txt"})["content"]
    )
    tools["fs_write"].run({"path": "project/new.txt", "content": "ordinary text\n"})
    tools["fs_edit"].run(
        {"path": "project/new.txt", "edits": [{"op": "append", "content": "extra\n"}]}
    )
    tools["fs_copy"].run({"source_path": "project/new.txt", "destination_path": "project/copy.txt"})
    tools["fs_move"].run(
        {"source_path": "project/copy.txt", "destination_path": "project/moved.txt"}
    )
    tools["fs_delete"].run({"path": "project/moved.txt"})
    tools["fs_mkdir"].run({"path": "project/generated"})
    assert (root / "project/new.txt").read_text() == "ordinary text\nextra\n"
    assert "ordinary" in str(
        tools["search_rg"].run({"root_path": "project", "pattern": "ordinary"})
    )


def test_explicit_fullaccess_retains_current_filesystem_semantics(protected, monkeypatch):
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
    root, areas, build = protected
    tools = build("fullaccess")
    for path in areas.values():
        target = str((path / "value.txt").relative_to(root))
        tools["fs_write"].run({"path": target, "content": "full access"})
        assert tools["fs_read"].run({"path": target})["content"] == "full access"


@pytest.mark.parametrize("tool", ["fs_read", "fs_write", "fs_copy", "search_rg"])
def test_hardlink_names_cannot_disclose_private_content(protected, tool):
    root, areas, build = protected
    os.link(areas["configuration"] / "value.txt", root / "project/alias.txt")
    args = {
        "path": "project/alias.txt",
        "source_path": "project/alias.txt",
        "destination_path": "project/copy.txt",
        "content": "changed",
        "root_path": "project",
        "pattern": "synthetic",
    }
    with pytest.raises(AgentRuntimeError, match="hard-linked"):
        build()[tool].run(args)
    assert (areas["configuration"] / "value.txt").read_text() == "synthetic original\n"
    assert not (root / "project/copy.txt").exists()


def test_missing_protected_paths_are_reserved(protected):
    root, _areas, _build = protected
    reserved = root / "future-runtime"
    policy = HostFilePolicy((), (reserved,))
    for path in ["future-runtime/next.py", "future-runtime", "."]:
        with pytest.raises(HostFilePolicyError, match="protected host"):
            policy.guard_write(root, path)
    assert not reserved.exists()


@pytest.mark.parametrize("raw", ["{", '"bad"', '["relative"]', "[null]"])
def test_invalid_launcher_private_policy_fails_closed(protected, monkeypatch, raw):
    _root, _areas, build = protected
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", raw)
    with pytest.raises(ConfigError, match="protected host file policy"):
        build()


def test_approval_time_parent_alias_cannot_redirect_a_write(protected, require_symlinks):
    root, areas, build = protected
    ordinary = root / "project"

    class RedirectingSurface(NoopSurface):
        def request_approval(self, request):
            ordinary.rename(root / "retained-project")
            ordinary.symlink_to(areas["configuration"], target_is_directory=True)
            return ApprovalDecision(allow=True)

    # Native Windows pins stop the attempted rename itself; POSIX detects the
    # changed path after the approval callback returns.
    with pytest.raises((AgentRuntimeError, PermissionError)):
        build("review", RedirectingSurface())["fs_write"].run(
            {"path": "project/new.txt", "content": "changed"}
        )
    assert not (areas["configuration"] / "new.txt").exists()


def test_mixed_delete_is_refused_before_modifying_any_file(protected):
    root, _areas, build = protected
    with pytest.raises(AgentRuntimeError, match="protected host"):
        build()["fs_delete"].run({"paths": ["project/input.txt", "owned/runtime/value.txt"]})
    assert (root / "project/input.txt").read_text() == "ordinary original\n"


def test_patch_paths_cannot_modify_runtime_before_git_execution(protected):
    _root, areas, build = protected
    patch = (
        "diff --git a/owned/runtime/value.txt b/owned/runtime/value.txt\n"
        "--- a/owned/runtime/value.txt\n+++ b/owned/runtime/value.txt\n"
        "@@ -1 +1 @@\n-synthetic original\n+changed\n"
    )
    with pytest.raises(AgentRuntimeError, match="protected host"):
        build()["git_apply_patch"].run({"patch": patch})
    assert (areas["runtime"] / "value.txt").read_text() == "synthetic original\n"


def test_running_python_installations_are_protected_without_desktop_policy(protected, monkeypatch):
    root, _areas, build = protected
    runtime = root / "python-installation"
    runtime.mkdir()
    monkeypatch.setattr(sys, "prefix", str(runtime))
    monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", "[]")
    with pytest.raises(AgentRuntimeError, match="protected host"):
        build()["fs_write"].run({"path": "python-installation/new.py", "content": "changed"})
    assert not (runtime / "new.py").exists()


def test_oversized_private_policy_is_rejected_without_changing_windows_environment(
    protected, monkeypatch
):
    from alysis_code import host_file_policy

    original = host_file_policy.env_get
    monkeypatch.setattr(
        host_file_policy,
        "env_get",
        lambda key: " " * 131_073 if key == "ALYSIS_DESKTOP_CONTROL_PATHS" else original(key),
    )
    with pytest.raises(ConfigError, match="protected host file policy"):
        HostFilePolicy.capture()


@pytest.mark.skipif(os.name != "nt", reason="Windows alternate streams")
def test_windows_stream_alias_is_refused(protected):
    _root, _areas, build = protected
    with pytest.raises(AgentRuntimeError, match="alternate data stream"):
        build()["fs_write"].run({"path": "project/input.txt:stream", "content": "changed"})


def test_fullaccess_with_strict_sandbox_keeps_host_policy(protected, monkeypatch):
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "strict")
    root, areas, build = protected
    tools = build("fullaccess")
    for area in areas.values():
        with pytest.raises(AgentRuntimeError, match="protected host"):
            tools["fs_write"].run(
                {"path": str((area / "value.txt").relative_to(root)), "content": "bad"}
            )
    with pytest.raises(AgentRuntimeError, match="private host"):
        tools["fs_read"].run({"path": "owned/configuration/value.txt"})


def test_readonly_sandbox_blocks_host_writes_even_with_fullaccess(protected, monkeypatch):
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "strict")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_FILESYSTEM_ACCESS", "read-only")
    root, _areas, build = protected
    tools = build("fullaccess")
    assert "ordinary" in tools["fs_read"].run({"path": "project/input.txt"})["content"]
    with pytest.raises(AgentRuntimeError, match="protected host"):
        tools["fs_write"].run({"path": "project/input.txt", "content": "bad"})
    assert (root / "project/input.txt").read_text() == "ordinary original\n"


@pytest.mark.skipif(os.name != "nt", reason="Windows directory handle denial")
@pytest.mark.parametrize(
    "tool", ["fs_read", "asset_view", "fs_write", "fs_edit", "fs_copy", "fs_move", "fs_delete"]
)
def test_existing_parent_cannot_be_replaced_during_direct_action(protected, monkeypatch, tool):
    from alysis_code import host_file_policy

    root, areas, build = protected
    original = host_file_policy.HostFilePolicy.guard_read
    attempts = []

    def attack(policy, workspace, path, **kwargs):
        original(policy, workspace, path, **kwargs)
        if str(path).replace("\\", "/") == "project/input.txt" and not attempts:
            try:
                (root / "project").rename(root / "displaced-project")
            except OSError as exc:
                attempts.append(exc.winerror)
            else:
                raise AssertionError("Ancestor rename succeeded while action was admitted")

    monkeypatch.setattr(host_file_policy.HostFilePolicy, "guard_read", attack)
    args = {
        "path": "project/input.txt",
        "content": "new",
        "edits": [{"op": "append", "content": "new"}],
        "source_path": "project/input.txt",
        "destination_path": "project/output.txt",
    }
    build()[tool].run(args)
    assert attempts == [32]
    assert (areas["configuration"] / "value.txt").read_text() == "synthetic original\n"
    # Handles are scoped to the action, including decoder failures and approvals.
    (root / "project").rename(root / "renamed-after-action")


@pytest.mark.skipif(os.name != "nt", reason="Windows native junction injection")
@pytest.mark.parametrize("tool", ["fs_read", "fs_write", "fs_edit", "fs_copy"])
def test_junction_leaf_injected_at_open_cannot_reveal_private_state(protected, monkeypatch, tool):
    import _winapi

    from alysis_code import host_file_handles

    root, areas, build = protected
    original = host_file_handles._windows_open
    attacked = []

    def attack(path, *, directory):
        if path == root / "project/input.txt" and not directory and not attacked:
            path.unlink()
            _winapi.CreateJunction(str(areas["configuration"]), str(path))
            attacked.append(True)
        return original(path, directory=directory)

    monkeypatch.setattr(host_file_handles, "_windows_open", attack)
    args = {
        "path": "project/input.txt",
        "content": "bad",
        "edits": [{"op": "append", "content": "bad"}],
        "source_path": "project/input.txt",
        "destination_path": "project/output.txt",
    }
    with pytest.raises((AgentRuntimeError, PermissionError), match="linked|denied"):
        build()[tool].run(args)
    assert attacked
    assert (areas["configuration"] / "value.txt").read_text() == "synthetic original\n"
    assert not (root / "project/output.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows native hardlink injection")
def test_mutation_snapshot_rejects_hardlink_injected_at_open(protected, monkeypatch):
    from alysis_code import host_file_handles

    root, areas, build = protected
    original = host_file_handles._windows_open
    attacked = []

    def attack(path, *, directory):
        if path == root / "project/input.txt" and not directory and not attacked:
            path.unlink()
            os.link(areas["configuration"] / "value.txt", path)
            attacked.append(True)
        return original(path, directory=directory)

    monkeypatch.setattr(host_file_handles, "_windows_open", attack)
    with pytest.raises(AgentRuntimeError, match="hard-linked"):
        build()["fs_write"].run({"path": "project/input.txt", "content": "bad"})
    assert attacked
    assert (areas["configuration"] / "value.txt").read_text() == "synthetic original\n"


@pytest.mark.skipif(os.name != "nt", reason="Windows native junction insertion")
def test_new_parent_is_admitted_before_creating_descendants(protected, monkeypatch):
    import _winapi

    root, areas, build = protected
    original = Path.mkdir
    attacked = []

    def attack(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == root / "project/new-parent" and not attacked:
            path.rmdir()
            _winapi.CreateJunction(str(areas["configuration"]), str(path))
            attacked.append(True)
        return result

    monkeypatch.setattr(Path, "mkdir", attack)
    with pytest.raises(AgentRuntimeError, match="linked"):
        build()["fs_write"].run({"path": "project/new-parent/escape.txt", "content": "bad"})
    assert attacked
    assert not (areas["configuration"] / "escape.txt").exists()


def test_prepared_resolved_destination_cannot_stage_inside_runtime(protected, monkeypatch):
    from dataclasses import replace

    from alysis_code.agent import tools_assembly

    root, areas, build = protected
    original = tools_assembly.prepare_fs_write

    def redirected(**kwargs):
        return replace(original(**kwargs), path_obj=areas["runtime"] / "value.txt")

    monkeypatch.setattr(tools_assembly, "prepare_fs_write", redirected)
    before = set(areas["runtime"].iterdir())
    with pytest.raises(AgentRuntimeError, match="protected host"):
        build()["fs_write"].run({"path": "project/input.txt", "content": "bad"})
    assert set(areas["runtime"].iterdir()) == before
    assert (areas["runtime"] / "value.txt").read_text() == "synthetic original\n"
    assert (root / "project/input.txt").read_text() == "ordinary original\n"


@pytest.mark.skipif(os.name != "nt", reason="NTFS short file names")
def test_native_short_names_use_identity_admission(protected):
    import ctypes
    from ctypes import wintypes

    from alysis_code.host_file_handles import host_file_action, open_host_file_read

    root, _areas, _build = protected
    path = root / "project/input.txt"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetShortPathNameW.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetShortPathNameW(str(path), buffer, len(buffer))
    assert 0 < length < len(buffer)
    short = Path(buffer.value)
    if str(short).casefold() == str(path).casefold():
        pytest.skip("This volume does not create short names")
    with host_file_action(HostFilePolicy.capture(), readonly=True):
        with open_host_file_read(short) as stream:
            assert stream.read().replace(b"\r\n", b"\n") == b"ordinary original\n"
    path.parent.rename(root / "released-after-short-name-read")


def test_recursive_worker_failure_has_no_host_fallback(protected, monkeypatch):
    from alysis_code.agent import tools_assembly

    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "strict")
    _root, _areas, build = protected
    calls = []

    class Worker:
        def run(self, name, args, **kwargs):
            calls.append((name, args))
            raise ConfigError("synthetic contained failure")

    monkeypatch.setattr(tools_assembly.ContainedReadTools, "for_settings", lambda *args: Worker())
    tools = build()
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
    with pytest.raises(ConfigError, match="synthetic contained failure"):
        tools["search_rg"].run({"root_path": "project", "pattern": "ordinary"})
    assert calls == [
        (
            "search_rg",
            {"root_path": "project", "path_base": "workspace_root", "pattern": "ordinary"},
        )
    ]


@pytest.mark.parametrize("tool", ["search_rg", "fs_write"])
def test_stop_reaches_running_file_worker_without_entering_payload(protected, monkeypatch, tool):
    from alysis_code.agent import tools_assembly

    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "strict")
    root, _areas, build = protected

    class Token:
        is_cancelled = False

    token = Token()
    calls = []

    class Worker:
        def run(self, name, args, *, cancel_check, timeout_s):
            # Serialization must succeed even though the dispatch holds a private
            # object key. A Stop arriving after dispatch must reach this wait.
            json.dumps(args)
            assert cancel_check() is False
            token.is_cancelled = True
            assert cancel_check() is True
            assert 0 < timeout_s <= 30
            calls.append(name)
            raise ConfigError("synthetic worker cancelled")

        run_write = run

    monkeypatch.setattr(tools_assembly.ContainedReadTools, "for_settings", lambda *args: Worker())
    assert tool in tools_assembly._SHELL_CANCELLABLE_WAIT_TOOL_NAMES
    args = (
        {"root_path": "project", "pattern": "ordinary"}
        if tool == "search_rg"
        else {"path": "project/input.txt", "content": "changed"}
    )
    args[tools_assembly._SHELL_CANCELLATION_TOKEN_ARG] = token
    with pytest.raises(ConfigError, match="cancelled"):
        build()[tool].run(args)
    assert calls
    assert (root / "project/input.txt").read_text() == "ordinary original\n"


def test_git_mutation_worker_receives_checked_preconditions_without_host_fallback(
    protected, monkeypatch
):
    from alysis_code.agent import tools_assembly

    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "strict")
    root, _areas, build = protected
    calls = []

    class Worker:
        def run_write(self, name, args, **kwargs):
            calls.append((name, args))
            raise ConfigError("synthetic contained failure")

    monkeypatch.setattr(tools_assembly.ContainedReadTools, "for_settings", lambda *args: Worker())
    patch = "--- a/project/input.txt\n+++ b/project/input.txt\n@@ -1 +1 @@\n-ordinary original\n+changed\n"
    with pytest.raises(ConfigError, match="synthetic contained failure"):
        build()["git_apply_patch"].run({"patch": patch})
    assert calls[0][0] == "git_apply_patch"
    assert calls[0][1]["patch"] == patch
    precondition = calls[0][1]["preconditions"][0]
    assert precondition["path"] == "project/input.txt"
    assert precondition["exists"] and precondition["identity_sha256"]
    assert (root / "project/input.txt").read_text() == "ordinary original\n"


@pytest.mark.parametrize(
    "tool,worker_name",
    [
        ("fs_write", "fs_write_prepared"),
        ("fs_edit", "fs_edit_prepared"),
        ("fs_copy", "fs_copy"),
        ("fs_move", "fs_move"),
        ("fs_delete", "fs_delete"),
        ("fs_mkdir", "fs_mkdir"),
    ],
)
def test_posix_final_mutation_routes_checked_payload_to_worker(
    protected, monkeypatch, tool, worker_name
):
    from alysis_code.agent import tools_assembly

    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "strict")
    root, _areas, build = protected
    calls = []

    class Worker:
        def run_write(self, name, args, **kwargs):
            calls.append((name, args))
            return {"synthetic": True}

    class Posix:
        name = "posix"

        def __getattr__(self, name):
            return getattr(os, name)

    monkeypatch.setattr(tools_assembly.ContainedReadTools, "for_settings", lambda *args: Worker())
    monkeypatch.setattr(tools_assembly, "os", Posix())
    tools = build()
    result = tools[tool].run(
        {
            "path": "project/input.txt",
            "content": "changed",
            "edits": [{"op": "append", "content": "changed"}],
            "source_path": "project/input.txt",
            "destination_path": "project/output.txt",
        }
    )
    assert result == {"synthetic": True}
    assert calls[0][0] == worker_name
    assert "root_obj" not in calls[0][1] and "path_obj" not in calls[0][1]
    assert "workspace" not in json.dumps(calls[0][1])
    assert (root / "project/input.txt").read_text() == "ordinary original\n"
    assert not (root / "project/output.txt").exists()


def test_posix_image_publication_sends_only_bytes_and_relative_target(protected, monkeypatch):
    import base64

    from alysis_code.agent import tools_assembly
    from alysis_code.config import AppConfig

    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "strict")
    root, _areas, build = protected
    calls = []

    class Worker:
        def run_write(self, name, args, **kwargs):
            calls.append((name, args))
            return {"created": True}

    class Posix:
        name = "posix"

        def __getattr__(self, name):
            return getattr(os, name)

    def generate(**kwargs):
        kwargs["publish"](root / "project/image.png", b"synthetic normalized image bytes")
        return {"created": True}

    monkeypatch.setattr(tools_assembly.ContainedReadTools, "for_settings", lambda *args: Worker())
    monkeypatch.setattr(tools_assembly, "os", Posix())
    monkeypatch.setattr(tools_assembly, "generate_images", generate)
    cfg = AppConfig(image_generation={"enabled": True, "model": "image-test"})
    result = build(cfg=cfg)["image_generate"].run(
        {"prompt": "synthetic", "output_path": "project/image.png"}
    )
    assert result == {"created": True}
    assert calls == [
        (
            "image_create_bytes",
            {
                "path": "project/image.png",
                "payload": base64.b64encode(b"synthetic normalized image bytes").decode("ascii"),
            },
        )
    ]
    assert not (root / "project/image.png").exists()
