from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from alysis_code.agent.read_ledger import SessionReadLedger
from alysis_code.agent.tools_assembly import ToolDef, build_tools
from alysis_code.config import AppConfig, set_config_value
from alysis_code.session_store import SessionStore


def _tools(
    tmp_path: Path,
    *,
    session_id: str,
    read_ledger_enabled: bool = True,
    read_ledger_sink=None,
) -> tuple[dict[str, ToolDef], SessionStore]:
    store = SessionStore(
        enabled=False,
        sessions_dir=tmp_path / "sessions",
        session_id=session_id,
        cwd=os.fspath(tmp_path),
        repo_root=os.fspath(tmp_path),
    )
    ledgers = []
    tools = build_tools(
        root=tmp_path,
        console=None,
        store=store,
        mode="auto",
        yes=True,
        cfg=AppConfig(model="test-model", read_ledger_enabled=read_ledger_enabled),
        subagents_enabled=False,
        read_ledger_sink=ledgers.append,
    )
    if read_ledger_sink is not None:
        read_ledger_sink(ledgers[0])
    # These focused tests deliver the entire result. Runtime offload composition
    # is exercised separately against the real turn/message boundary.
    for name in ("fs_read",):
        run = tools[name].run

        def deliver(args, run=run):
            result = run(args)
            ledgers[0].record_delivery(result=result, content_for_message=json.dumps(result))
            return result

        tools[name] = replace(tools[name], run=deliver)
    return tools, store


def _write_lines(path: Path, *lines: str) -> None:
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8", newline="")


def test_attached_folder_receipts_expire_when_scope_is_removed(tmp_path):
    from alysis_code.project_folders import folder_scope

    main, extra = tmp_path / "main", tmp_path / "共有"
    main.mkdir()
    extra.mkdir()
    path = extra / "data.txt"
    path.write_text("observed content\n")
    ledger = SessionReadLedger(root=main)
    assert ledger.content_hash(str(path)) is None
    with folder_scope(main, [str(extra)]):
        content_hash = ledger.content_hash(str(path))
        assert content_hash
        result = ledger.filter_result(
            path=str(path),
            result={"path": str(path), "content": path.read_text(), "truncated": False},
            content_hash_before=content_hash,
            force=False,
        )
        assert result.get("read_receipt_id")
        ledger.record_delivery(result=result, content_for_message=json.dumps(result))
        snapshot = ledger.snapshot()
        assert str(path) in snapshot
    assert ledger.content_hash(str(path)) is None
    assert SessionReadLedger(root=main).seed_from_snapshot(snapshot) == 0


def test_exact_unchanged_reread_is_replaced_by_compact_notice(tmp_path: Path) -> None:
    _write_lines(tmp_path / "demo.txt", "one", "two", "three", "four")
    tools, store = _tools(tmp_path, session_id="exact")
    try:
        first = tools["fs_read"].run({"path": "demo.txt"})
        second = tools["fs_read"].run({"path": "demo.txt"})

        assert first["content"] == "one\ntwo\nthree\nfour\n"
        assert second["read_ledger_skipped"] is True
        assert "lines 1-4 of demo.txt were already returned" in second["content"]
        assert "one\ntwo" not in second["content"]
    finally:
        store.close()


def test_partial_overlap_returns_only_unread_delta_and_notice(tmp_path: Path) -> None:
    _write_lines(tmp_path / "demo.txt", "one", "two", "three", "four", "five", "six")
    tools, store = _tools(tmp_path, session_id="partial")
    try:
        first = tools["fs_read"].run({"path": "demo.txt", "start_line": 2, "end_line": 4})
        second = tools["fs_read"].run({"path": "demo.txt", "start_line": 3, "end_line": 6})

        assert "2: two" in first["content"]
        assert second["read_ledger_partial"] is True
        assert second["returned_ranges"] == [{"start_line": 5, "end_line": 6}]
        assert second["skipped_ranges"] == [{"start_line": 3, "end_line": 4}]
        assert "5: five" in second["content"]
        assert "6: six" in second["content"]
        assert "3: three" not in second["content"]
        assert "4: four" not in second["content"]
        assert "lines 3-4 of demo.txt were already returned" in second["content"]
    finally:
        store.close()


@pytest.mark.parametrize("pre_read_raw", [False, True])
def test_clipped_line_is_not_recorded_as_fully_returned(tmp_path: Path, pre_read_raw: bool) -> None:
    _write_lines(tmp_path / "demo.txt", "abcdefghijklmnopqrstuvwxyz")
    tools, store = _tools(tmp_path, session_id="clipped-line")
    try:
        if pre_read_raw:
            tools["fs_read"].run({"path": "demo.txt"})
        clipped = tools["fs_read"].run(
            {
                "path": "demo.txt",
                "start_line": 1,
                "end_line": 1,
                "max_bytes": 10,
            }
        )
        complete = tools["fs_read"].run(
            {
                "path": "demo.txt",
                "start_line": 1,
                "end_line": 1,
                "max_bytes": 1_000,
            }
        )

        assert clipped["line_clipped"] is True
        assert "read_ledger_skipped" not in complete
        assert "1: abcdefghijklmnopqrstuvwxyz" in complete["content"]
    finally:
        store.close()


def test_truncated_full_read_records_only_complete_lines(tmp_path: Path) -> None:
    _write_lines(tmp_path / "demo.txt", "one", "abcdefghijklmnopqrstuvwxyz")
    tools, store = _tools(tmp_path, session_id="truncated-full-read")
    try:
        truncated = tools["fs_read"].run({"path": "demo.txt", "max_bytes": 10})
        complete = tools["fs_read"].run({"path": "demo.txt", "max_bytes": 1_000})

        assert truncated["line_clipped"] is True
        assert complete["read_ledger_partial"] is True
        assert complete["returned_ranges"] == [{"start_line": 2, "end_line": 2}]
        assert "abcdefghijklmnopqrstuvwxyz" in complete["content"]
        assert "one\n" not in complete["content"]
    finally:
        store.close()


def test_observed_file_modification_invalidates_prior_ranges(tmp_path: Path) -> None:
    target = tmp_path / "demo.txt"
    _write_lines(target, "old-one", "old-two")
    tools, store = _tools(tmp_path, session_id="external-change")
    try:
        tools["fs_read"].run({"path": "demo.txt", "start_line": 1, "end_line": 2})
        _write_lines(target, "new-one", "new-two")
        result = tools["fs_read"].run({"path": "demo.txt", "start_line": 1, "end_line": 2})

        assert "new-one" in result["content"]
        assert "read_ledger_skipped" not in result
        assert "read_ledger_partial" not in result
    finally:
        store.close()


def test_session_write_invalidates_touched_file(tmp_path: Path) -> None:
    _write_lines(tmp_path / "demo.txt", "before")
    tools, store = _tools(tmp_path, session_id="session-write")
    try:
        tools["fs_read"].run({"path": "demo.txt"})
        tools["fs_write"].run({"path": "demo.txt", "content": "after\n"})
        result = tools["fs_read"].run({"path": "demo.txt"})

        assert result["content"] == "after\n"
        assert "read_ledger_skipped" not in result
    finally:
        store.close()


def test_force_bypasses_unchanged_range_dedupe(tmp_path: Path) -> None:
    _write_lines(tmp_path / "demo.txt", "one", "two")
    tools, store = _tools(tmp_path, session_id="force")
    try:
        tools["fs_read"].run({"path": "demo.txt", "start_line": 1, "end_line": 2})
        result = tools["fs_read"].run(
            {"path": "demo.txt", "start_line": 1, "end_line": 2, "force": True}
        )

        assert result["read_ledger_forced"] is True
        assert "1: one" in result["content"]
        assert "2: two" in result["content"]
    finally:
        store.close()


def test_parent_and_child_sessions_have_independent_ledgers(tmp_path: Path) -> None:
    _write_lines(tmp_path / "demo.txt", "one", "two")
    parent_tools, parent_store = _tools(tmp_path, session_id="parent")
    child_tools, child_store = _tools(tmp_path, session_id="child")
    try:
        parent_tools["fs_read"].run({"path": "demo.txt"})
        child_result = child_tools["fs_read"].run({"path": "demo.txt"})

        assert child_result["content"] == "one\ntwo\n"
        assert "read_ledger_skipped" not in child_result
    finally:
        child_store.close()
        parent_store.close()


def test_disabled_read_ledger_restores_repeated_content(tmp_path: Path) -> None:
    _write_lines(tmp_path / "demo.txt", "one", "two")
    tools, store = _tools(tmp_path, session_id="disabled", read_ledger_enabled=False)
    try:
        first = tools["fs_read"].run({"path": "demo.txt"})
        second = tools["fs_read"].run({"path": "demo.txt"})

        assert first == second
        assert "read_ledger_skipped" not in second
    finally:
        store.close()


def test_read_ledger_config_and_force_schemas() -> None:
    cfg = AppConfig()
    assert cfg.read_ledger_enabled is True
    assert set_config_value(cfg, "read_ledger_enabled", "false").read_ledger_enabled is False

    from alysis_code.tools.registry import require_builtin_tool_metadata

    force = require_builtin_tool_metadata("fs_read").parameters["properties"]["force"]
    assert force["type"] == "boolean"
    assert force["default"] is False


@pytest.mark.parametrize("raw_range", [False, True])
@pytest.mark.parametrize("numbered_first", [False, True])
def test_each_requested_presentation_is_delivered_once(
    tmp_path: Path, raw_range: bool, numbered_first: bool
) -> None:
    _write_lines(tmp_path / "demo.txt", "  Ελληνικά", "second")
    tools, store = _tools(tmp_path, session_id="presentations")
    raw_args = {"path": "demo.txt"}
    if raw_range:
        raw_args.update(start_line=1, end_line=2, include_line_numbers=False)
    numbered_args = {"path": "demo.txt", "start_line": 1, "end_line": 2}
    calls = [("fs_read", raw_args), ("fs_read", numbered_args)]
    if numbered_first:
        calls.reverse()
    try:
        for name, args in calls:
            result = tools[name].run(args)
            numbered = args is numbered_args
            assert result["content"] == (
                "1:   Ελληνικά\n2: second\n" if numbered else "  Ελληνικά\nsecond\n"
            )
            assert "read_ledger_skipped" not in result
        for name, args in calls:
            assert tools[name].run(args)["read_ledger_skipped"] is True
    finally:
        store.close()


def test_partial_numbered_coverage_does_not_inherit_raw_ranges(tmp_path: Path) -> None:
    _write_lines(tmp_path / "demo.txt", "one", "two", "three", "four")
    tools, store = _tools(tmp_path, session_id="partial-presentation")
    try:
        tools["fs_read"].run({"path": "demo.txt"})
        tools["fs_read"].run({"path": "demo.txt", "start_line": 2, "end_line": 3})
        result = tools["fs_read"].run({"path": "demo.txt", "start_line": 1, "end_line": 4})
        assert result["returned_ranges"] == [
            {"start_line": 1, "end_line": 1},
            {"start_line": 4, "end_line": 4},
        ]
        assert result["skipped_ranges"] == [{"start_line": 2, "end_line": 3}]
        assert result["content"].startswith("1: one\n4: four\n")
    finally:
        store.close()


@pytest.mark.parametrize("numbered", [False, True])
@pytest.mark.parametrize("newline", ["\n", "\r", "\r\n"])
def test_partial_overlap_uses_physical_lines_not_unicode_separators(
    tmp_path: Path, numbered: bool, newline: str
) -> None:
    (tmp_path / "demo.txt").write_bytes(
        newline.join(["first", "α\u2028β\vγ", "third", "fourth", ""]).encode("utf-8")
    )
    tools, store = _tools(tmp_path, session_id="physical-lines")
    args = {"path": "demo.txt", "include_line_numbers": numbered}
    try:
        tools["fs_read"].run({**args, "start_line": 1, "end_line": 2})
        result = tools["fs_read"].run({**args, "start_line": 2, "end_line": 4})
        assert result["returned_ranges"] == [{"start_line": 3, "end_line": 4}]
        expected = (
            f"3: third{newline}4: fourth{newline}" if numbered else f"third{newline}fourth{newline}"
        )
        assert result["content"].startswith(expected)
        assert "β" not in result["content"]
    finally:
        store.close()


@pytest.mark.parametrize(
    "line_endings", [("\n",) * 3, ("\r",) * 3, ("\r\n",) * 3, ("\r\n", "\r", "\n")]
)
def test_full_raw_read_covers_physical_lines_with_any_line_endings(
    tmp_path: Path, line_endings: tuple[str, str, str]
) -> None:
    content = "".join(
        line + ending
        for line, ending in zip(("α\u2028β", "two", "three"), line_endings, strict=True)
    )
    (tmp_path / "demo.txt").write_bytes(content.encode("utf-8"))
    tools, store = _tools(tmp_path, session_id="raw-physical-lines")
    try:
        assert tools["fs_read"].run({"path": "demo.txt"})["content"] == content
        result = tools["fs_read"].run(
            {"path": "demo.txt", "start_line": 1, "end_line": 3, "include_line_numbers": False}
        )
        assert result["read_ledger_skipped"] is True
        assert result["skipped_ranges"] == [{"start_line": 1, "end_line": 3}]
    finally:
        store.close()


@pytest.mark.parametrize("pre_read_raw", [False, True])
def test_snapshot_resume_preserves_only_observed_presentations(
    tmp_path: Path, pre_read_raw: bool
) -> None:
    _write_lines(tmp_path / "demo.txt", "one", "two", "three")
    ledgers: list[SessionReadLedger] = []
    tools, store = _tools(tmp_path, session_id="snapshot", read_ledger_sink=ledgers.append)
    resumed_ledgers: list[SessionReadLedger] = []
    resumed_tools, resumed_store = _tools(
        tmp_path, session_id="resumed", read_ledger_sink=resumed_ledgers.append
    )
    try:
        if pre_read_raw:
            tools["fs_read"].run({"path": "demo.txt"})
        tools["fs_read"].run({"path": "demo.txt", "start_line": 2, "end_line": 2})
        snapshot = ledgers[0].snapshot()
        assert resumed_ledgers[0].seed_from_snapshot(snapshot) == 1
        raw = resumed_tools["fs_read"].run({"path": "demo.txt"})
        assert (raw.get("read_ledger_skipped") is True) is pre_read_raw
        numbered = resumed_tools["fs_read"].run(
            {"path": "demo.txt", "start_line": 1, "end_line": 3}
        )
        assert numbered["returned_ranges"] == [
            {"start_line": 1, "end_line": 1},
            {"start_line": 3, "end_line": 3},
        ]
        assert snapshot["demo.txt"]["numbered_ranges"] == [(2, 2)]
        assert resumed_ledgers[0].reset() == 1
        assert resumed_tools["fs_read"].run({"path": "demo.txt"})["content"].startswith("one\n")
        assert (
            resumed_tools["fs_read"]
            .run({"path": "demo.txt", "start_line": 1, "end_line": 3})["content"]
            .startswith("1: one\n")
        )
    finally:
        resumed_store.close()
        store.close()


def test_legacy_snapshot_does_not_claim_numbered_delivery(tmp_path: Path) -> None:
    _write_lines(tmp_path / "demo.txt", "one", "two")
    ledgers: list[SessionReadLedger] = []
    tools, store = _tools(tmp_path, session_id="legacy", read_ledger_sink=ledgers.append)
    try:
        ledger = ledgers[0]
        snapshot = {
            "demo.txt": {"content_sha256": ledger.content_hash("demo.txt"), "ranges": [(1, 2)]}
        }
        assert ledger.seed_from_snapshot(snapshot) == 1
        assert tools["fs_read"].run({"path": "demo.txt"})["read_ledger_skipped"] is True
        numbered = tools["fs_read"].run({"path": "demo.txt", "start_line": 1, "end_line": 2})
        assert numbered["content"] == "1: one\n2: two\n"
        assert "read_ledger_skipped" not in numbered
    finally:
        store.close()


def test_changed_file_invalidates_both_presentations_and_snapshot(tmp_path: Path) -> None:
    target = tmp_path / "demo.txt"
    _write_lines(target, "before")
    ledgers: list[SessionReadLedger] = []
    tools, store = _tools(tmp_path, session_id="both-invalidated", read_ledger_sink=ledgers.append)
    try:
        tools["fs_read"].run({"path": "demo.txt"})
        tools["fs_read"].run({"path": "demo.txt", "start_line": 1, "end_line": 1})
        snapshot = ledgers[0].snapshot()
        _write_lines(target, "after")
        assert ledgers[0].seed_from_snapshot(snapshot) == 0
        assert tools["fs_read"].run({"path": "demo.txt"})["content"] == "after\n"
        assert (
            tools["fs_read"].run({"path": "demo.txt", "start_line": 1, "end_line": 1})["content"]
            == "1: after\n"
        )
    finally:
        store.close()


@pytest.fixture(autouse=True)
def file_semantics_without_os_backend(monkeypatch):
    # These unit tests isolate approval, stale-file and ledger behavior.
    # Native/WSL containment is exercised by the installed-runtime qualifier.
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
