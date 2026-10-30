from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path

import pytest
from rich.console import Console

from alysis_code.config import ConfigError
from alysis_code.sandbox_bookkeeping import (
    PROTECTED_INSTRUCTION_FILES,
    capture_instruction_creations,
    command_file_signature,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows handle-relative preparation")


def _prepare(root: Path) -> None:
    from alysis_code._windows_sandbox_bookkeeping import prepare_instruction_files

    prepare_instruction_files(root)


def test_fixed_empty_files_have_creation_handle_receipts_and_keep_existing_files(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    existing = root / "CLAUDE.md"
    existing.write_text("preserve user instructions")
    original = existing.stat()
    with capture_instruction_creations(root) as receipts:
        _prepare(root)
    assert [entry.relative_path for entry in receipts] == ["AGENTS.md", "CONVENTIONS.md"]
    assert existing.read_text() == "preserve user instructions"
    assert command_file_signature(existing.stat()) == command_file_signature(original)
    for entry in receipts:
        assert (root / entry.relative_path).read_bytes() == b""
        assert entry.signature == command_file_signature((root / entry.relative_path).stat())
    with capture_instruction_creations(root) as repeated:
        _prepare(root)
    assert repeated == []


def test_workspace_ancestor_junction_is_rejected_without_creating_files(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "alias"
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0
    try:
        with pytest.raises(ConfigError, match="reparse"):
            _prepare(link)
        assert list(target.iterdir()) == []
    finally:
        link.rmdir()


def test_existing_hardlinked_instruction_fails_closed(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("do not touch")
    os.link(outside, root / "AGENTS.md")
    with pytest.raises(ConfigError, match="hard-linked"):
        _prepare(root)
    assert outside.read_text() == "do not touch"
    assert not (root / "CLAUDE.md").exists()


def test_invalid_or_excessive_roots_do_not_create_instruction_files(tmp_path: Path, monkeypatch):
    for root in (Path("relative"), Path(r"\\server\share\workspace"), Path(tmp_path.anchor)):
        with pytest.raises(ConfigError):
            _prepare(root)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with pytest.raises(ConfigError, match="profile"):
        _prepare(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_ancestor_handles_block_rename_during_creation(tmp_path: Path, monkeypatch):
    from alysis_code._windows_managed_session import _Api

    root = tmp_path / "workspace"
    root.mkdir()
    original = _Api.open_relative
    checked = []

    def open_relative(self, parent, name, **kwargs):
        if kwargs.get("create_new"):
            with pytest.raises(PermissionError):
                root.rename(tmp_path / "moved")
            checked.append(name)
        handle = original(self, parent, name, **kwargs)
        if kwargs.get("create_new"):
            with pytest.raises(PermissionError):
                (root / name).write_text("racing host write")
        return handle

    monkeypatch.setattr(_Api, "open_relative", open_relative)
    monkeypatch.setattr(_Api, "admit_private_file", lambda *a, **kw: pytest.fail("DACL changed"))
    _prepare(root)
    assert checked == list(PROTECTED_INSTRUCTION_FILES)


def test_native_runner_preparation_does_not_precede_command_approval(tmp_path: Path, monkeypatch):
    from alysis_code.agent_loop import AgentRuntimeError, build_tools
    from alysis_code.sandbox_settings import ShellSandboxSettings
    from alysis_code.session_store import SessionStore
    from alysis_code.surface import ApprovalDecision
    from alysis_code.surface.noop_surface import NoopSurface
    from alysis_code.windows_sandbox import WindowsNativeRunner

    class Decline(NoopSurface):
        host_managed_approvals = True

        def request_approval(self, request):
            assert not any((tmp_path / name).exists() for name in PROTECTED_INSTRUCTION_FILES)
            return ApprovalDecision(allow=False)

    monkeypatch.setattr(
        WindowsNativeRunner, "_prepare", lambda *a, **kw: pytest.fail("ran before approval")
    )
    runner = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native"))
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        surface=Decline(),
        store=SessionStore(
            enabled=False,
            sessions_dir=tmp_path / "sessions",
            session_id="approval",
            cwd=str(tmp_path),
            repo_root=str(tmp_path),
        ),
        mode="review",
        yes=False,
        non_interactive=True,
        shell_runner=runner,
    )
    with pytest.raises(AgentRuntimeError, match="User declined"):
        tools["shell_run"].run({"cmd": "Set-Content -Path result.txt -Value changed"})
    assert not any((tmp_path / name).exists() for name in PROTECTED_INSTRUCTION_FILES)
