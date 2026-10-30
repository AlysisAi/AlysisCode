from __future__ import annotations

import os
from pathlib import Path

import pytest

from alysis_code.agent.turn.snapshot import _run_with_command_mutation_detection
from alysis_code.sandbox_bookkeeping import record_instruction_creation


def _create_receipt(root: Path, name: str = "AGENTS.md") -> None:
    with (root / name).open("xb") as leaf:
        record_instruction_creation(root / name, os.fstat(leaf.fileno()))


def test_only_exclusive_creation_receipt_establishes_instruction_baseline(tmp_path: Path):
    def operation():
        _create_receipt(tmp_path)
        (tmp_path / "CLAUDE.md").touch()  # A real user/tool creation without a receipt.
        return {"exit_code": 0}

    result, touched = _run_with_command_mutation_detection(
        root=tmp_path, enabled=True, operation=operation
    )
    assert result == {"exit_code": 0}
    assert touched == ["CLAUDE.md"]


@pytest.mark.parametrize("edit", ["write", "replace", "delete"])
def test_edits_after_placeholder_creation_are_material(tmp_path: Path, edit: str):
    def operation():
        _create_receipt(tmp_path)
        path = tmp_path / "AGENTS.md"
        if edit == "write":
            path.write_text("real instruction edit")
        elif edit == "replace":
            # Same empty content and mtime are not enough: identity must match.
            before = path.stat()
            replacement = tmp_path / "replacement"
            replacement.touch()
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            replacement.replace(path)
        else:
            path.unlink()
        return {"exit_code": 1}  # A failed command never suppresses its real edits.

    _, touched = _run_with_command_mutation_detection(
        root=tmp_path, enabled=True, operation=operation
    )
    assert touched == ["AGENTS.md"]


def test_existing_zero_byte_instructions_are_not_excluded(tmp_path: Path):
    path = tmp_path / "AGENTS.md"
    path.touch()
    _, unchanged = _run_with_command_mutation_detection(
        root=tmp_path, enabled=True, operation=lambda: None
    )
    assert unchanged == []
    _, edited = _run_with_command_mutation_detection(
        root=tmp_path, enabled=True, operation=lambda: path.write_text("user instruction")
    )
    assert edited == ["AGENTS.md"]


def test_preexisting_path_is_never_rebased_and_exception_drops_receipts(tmp_path: Path):
    path = tmp_path / "AGENTS.md"
    path.write_text("original instruction")

    def replace_with_bookkeeping():
        path.unlink()
        _create_receipt(tmp_path)

    _, touched = _run_with_command_mutation_detection(
        root=tmp_path, enabled=True, operation=replace_with_bookkeeping
    )
    assert touched == ["AGENTS.md"]

    def failed_launch():
        _create_receipt(tmp_path, "CLAUDE.md")
        raise RuntimeError("launch failed")

    with pytest.raises(RuntimeError, match="launch failed"):
        _run_with_command_mutation_detection(root=tmp_path, enabled=True, operation=failed_launch)
    _, touched = _run_with_command_mutation_detection(
        root=tmp_path,
        enabled=True,
        operation=lambda: (tmp_path / "CLAUDE.md").write_text("later user edit"),
    )
    assert touched == ["CLAUDE.md"]
