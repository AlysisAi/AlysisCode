from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from alysis_code.agent_loop import build_tools
from alysis_code.config import AppConfig
from alysis_code.session_store import SessionStore
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.tools.fs import FsError, fs_edit


def _store(root: Path) -> SessionStore:
    return SessionStore(
        enabled=False,
        sessions_dir=root / "sessions",
        session_id="fs-edit-test",
        cwd=str(root),
        repo_root=str(root),
    )


def test_fs_edit_single_replacement(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("alpha\nbeta\n", encoding="utf-8", newline="\n")

    result = fs_edit(
        root=tmp_path,
        path="demo.txt",
        edits=[{"op": "replace_exact", "target": "beta", "replacement": "gamma"}],
    )

    assert result == {
        "path": "demo.txt",
        "applied_edits": 1,
        "changed": True,
        "bytes": len(b"alpha\ngamma\n"),
    }
    assert path.read_text(encoding="utf-8") == "alpha\ngamma\n"


def test_fs_edit_replace_alias_maps_to_replace_exact(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("alpha\nbeta\n", encoding="utf-8")

    result = fs_edit(
        root=tmp_path,
        path="demo.txt",
        edits=[{"op": "replace", "target": "beta", "replacement": "gamma"}],
    )

    assert result["changed"] is True
    assert path.read_text(encoding="utf-8") == "alpha\ngamma\n"


def test_fs_edit_applies_multiple_edits_in_order(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("mid\nend\n", encoding="utf-8", newline="\n")

    result = fs_edit(
        root=tmp_path,
        path="demo.txt",
        edits=[
            {"op": "prepend", "content": "start\n"},
            {"op": "insert_after_exact", "target": "mid\n", "content": "after-mid\n"},
            {"op": "replace_exact", "target": "end", "replacement": "finish"},
        ],
    )

    assert result["applied_edits"] == 3
    assert path.read_text(encoding="utf-8") == "start\nmid\nafter-mid\nfinish\n"


def test_fs_edit_insert_before_and_after_exact(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("A\nB\n", encoding="utf-8")

    fs_edit(
        root=tmp_path,
        path="demo.txt",
        edits=[
            {"op": "insert_before_exact", "target": "B", "content": "before-"},
            {"op": "insert_after_exact", "target": "B", "content": "-after"},
        ],
    )

    assert path.read_text(encoding="utf-8") == "A\nbefore-B-after\n"


def test_fs_edit_replace_lines_with_expected_old(tmp_path: Path) -> None:
    path = tmp_path / "demo.py"
    path.write_text("def f():\n    return 1\n\nprint(f())\n", encoding="utf-8")

    result = fs_edit(
        root=tmp_path,
        path="demo.py",
        edits=[
            {
                "op": "replace_lines",
                "start_line": 1,
                "end_line": 2,
                "expected_old": "def f():\n    return 1\n",
                "replacement": "def f():\n    return 2\n",
            }
        ],
    )

    assert result["changed"] is True
    assert path.read_text(encoding="utf-8") == "def f():\n    return 2\n\nprint(f())\n"


def test_fs_edit_replace_lines_can_delete_range(tmp_path: Path) -> None:
    path = tmp_path / "demo.py"
    path.write_text("keep\nremove-a\nremove-b\nkeep-too\n", encoding="utf-8")

    fs_edit(
        root=tmp_path,
        path="demo.py",
        edits=[{"op": "replace_lines", "start_line": 2, "end_line": 3, "replacement": ""}],
    )

    assert path.read_text(encoding="utf-8") == "keep\nkeep-too\n"


def test_fs_edit_line_inserts_preserve_existing_newlines(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_bytes(b"A\r\nB\r\n")

    fs_edit(
        root=tmp_path,
        path="demo.txt",
        edits=[
            {"op": "insert_before_line", "line": 2, "content": "before\r\n"},
            {"op": "insert_after_line", "line": 3, "content": "after\r\n"},
        ],
    )

    assert path.read_bytes() == b"A\r\nbefore\r\nB\r\nafter\r\n"


def test_fs_edit_replace_lines_rejects_stale_expected_old(tmp_path: Path) -> None:
    path = tmp_path / "demo.py"
    original = "alpha\nbeta\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(FsError, match="selected line text did not match expected_old"):
        fs_edit(
            root=tmp_path,
            path="demo.py",
            edits=[
                {
                    "op": "replace_lines",
                    "start_line": 2,
                    "end_line": 2,
                    "expected_old": "gamma\n",
                    "replacement": "delta\n",
                }
            ],
        )

    assert path.read_text(encoding="utf-8") == original


def test_fs_edit_line_operations_reject_invalid_ranges(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("one\n", encoding="utf-8")

    with pytest.raises(FsError, match="end_line 2 is beyond end of file"):
        fs_edit(
            root=tmp_path,
            path="demo.txt",
            edits=[{"op": "replace_lines", "start_line": 1, "end_line": 2, "replacement": ""}],
        )

    with pytest.raises(FsError, match="line 2 is beyond end of file"):
        fs_edit(
            root=tmp_path,
            path="demo.txt",
            edits=[{"op": "insert_after_line", "line": 2, "content": "two\n"}],
        )


def test_fs_edit_append_and_prepend(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("body", encoding="utf-8")

    fs_edit(
        root=tmp_path,
        path="demo.txt",
        edits=[
            {"op": "prepend", "content": "head\n"},
            {"op": "append", "content": "\nfoot"},
        ],
    )

    assert path.read_text(encoding="utf-8") == "head\nbody\nfoot"


def test_fs_edit_preserves_existing_crlf_newlines(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_bytes(b"alpha\r\nbeta\r\n")

    fs_edit(
        root=tmp_path,
        path="demo.txt",
        edits=[{"op": "replace_exact", "target": "beta", "replacement": "gamma"}],
    )

    assert path.read_bytes() == b"alpha\r\ngamma\r\n"


def test_fs_edit_is_all_or_nothing_on_failure(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    original = "alpha\nbeta\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(FsError, match="target matched 0 times"):
        fs_edit(
            root=tmp_path,
            path="demo.txt",
            edits=[
                {"op": "replace_exact", "target": "alpha", "replacement": "ALPHA"},
                {"op": "replace_exact", "target": "missing", "replacement": "x"},
            ],
        )

    assert path.read_text(encoding="utf-8") == original


def test_fs_edit_insert_before_requires_content_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    original = "alpha\nbeta\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(
        FsError,
        match="Edit 1 \\(insert_before_exact\\) requires string field: content",
    ):
        fs_edit(
            root=tmp_path,
            path="demo.txt",
            edits=[{"op": "insert_before_exact", "target": "beta"}],
        )

    assert path.read_text(encoding="utf-8") == original


def test_fs_edit_zero_match_failure(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("alpha\n", encoding="utf-8")

    with pytest.raises(FsError, match="target matched 0 times; expected exactly 1"):
        fs_edit(
            root=tmp_path,
            path="demo.txt",
            edits=[{"op": "replace_exact", "target": "beta", "replacement": "gamma"}],
        )


def test_fs_edit_ambiguous_match_failure(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("repeat\nrepeat\n", encoding="utf-8")

    with pytest.raises(FsError, match="target matched 2 times; expected exactly 1"):
        fs_edit(
            root=tmp_path,
            path="demo.txt",
            edits=[{"op": "replace_exact", "target": "repeat", "replacement": "done"}],
        )


def test_fs_edit_expected_match_count_allows_multiple_matches(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("repeat\nrepeat\n", encoding="utf-8")

    fs_edit(
        root=tmp_path,
        path="demo.txt",
        edits=[
            {
                "op": "replace_exact",
                "target": "repeat",
                "replacement": "done",
                "expected_match_count": 2,
            }
        ],
    )

    assert path.read_text(encoding="utf-8") == "done\ndone\n"


def test_build_tools_registers_fs_edit_and_emits_diff_preview(tmp_path: Path) -> None:
    class RecordingSurface(NoopSurface):
        def __init__(self) -> None:
            self.patch_events = []

        def on_patch_generated(self, event):  # type: ignore[no-untyped-def]
            self.patch_events.append(event)

    path = tmp_path / "demo.txt"
    path.write_text("before\n", encoding="utf-8")
    surface = RecordingSurface()
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO(), force_terminal=False),
        surface=surface,
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        cfg=AppConfig(model="test-model"),
        non_interactive=True,
    )

    assert "fs_edit" in tools
    schema = tools["fs_edit"].as_openai_tool()["function"]["parameters"]
    assert schema["required"] == ["path", "edits"]
    op_enums = [
        variant["properties"]["op"]["enum"]
        for variant in schema["properties"]["edits"]["items"]["anyOf"]
    ]
    assert ["replace", "replace_exact"] in op_enums
    assert ["replace_lines"] in op_enums
    assert ["insert_before_line", "insert_after_line"] in op_enums

    result = tools["fs_edit"].run(
        {
            "path": "demo.txt",
            "edits": [{"op": "replace_exact", "target": "before", "replacement": "after"}],
        }
    )

    assert result["changed"] is True
    assert len(surface.patch_events) == 1
    event = surface.patch_events[0]
    assert event.files == ["demo.txt"]
    assert event.summary == "1 file changed via fs_edit (demo.txt)"
    assert "-before" in event.diff
    assert "+after" in event.diff


@pytest.fixture(autouse=True)
def file_semantics_without_os_backend(monkeypatch):
    # These unit tests isolate approval, stale-file and ledger behavior.
    # Native/WSL containment is exercised by the installed-runtime qualifier.
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
