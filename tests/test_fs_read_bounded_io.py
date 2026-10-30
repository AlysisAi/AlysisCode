from __future__ import annotations

import io
import json
import tracemalloc
from pathlib import Path

import pytest

from alysis_code.agent.read_ledger import SessionReadLedger
from alysis_code.agent.tools_assembly import build_tools
from alysis_code.config import AppConfig
from alysis_code.session_store import SessionStore
from alysis_code.tools import fs as fs_mod
from alysis_code.tools.fs import FsError, fs_read


class _ReadMeter:
    def __init__(self) -> None:
        self.requests: list[int] = []
        self.bytes_read = 0
        self.opens = 0

    def watch(self, monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
        original_open = Path.open
        meter = self

        class MeteredFile:
            def __init__(self, handle) -> None:
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.handle.__exit__(*args)

            def read(self, size=-1):
                assert size >= 0, "A bounded read must not request the whole file"
                meter.requests.append(size)
                result = self.handle.read(size)
                assert isinstance(result, bytes), "Reader must stream bytes, not whole text lines"
                meter.bytes_read += len(result)
                return result

        def metered_open(path, *args, **kwargs):
            handle = original_open(path, *args, **kwargs)
            if path == target:
                meter.opens += 1
                return MeteredFile(handle)
            return handle

        monkeypatch.setattr(Path, "open", metered_open)


@pytest.mark.parametrize(
    ("name", "options", "read_limit"),
    [
        ("large.txt", {}, 33),
        ("package-lock.json", {}, 32),
        ("large.txt", {"start_line": 1}, fs_mod._READ_CHUNK_BYTES),
    ],
)
def test_raw_derived_and_minified_windows_have_bounded_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, options: dict, read_limit: int
) -> None:
    path = tmp_path / name
    path.write_bytes(b"x" * (4 * 1024 * 1024))
    meter = _ReadMeter()
    meter.watch(monkeypatch, path)

    result = fs_read(root=tmp_path, path=name, max_bytes=32, **options)

    assert meter.opens == 1
    assert meter.bytes_read <= read_limit
    assert result["total_lines"] is None
    assert result["truncated"] is True
    assert result["line_clipped"] is True
    assert result["next_range"]["start_line"] == 1
    assert len(result["content"].encode()) <= 32


def test_focused_window_does_not_read_the_next_enormous_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "focused.txt"
    path.write_bytes(b"first\n" + b"x" * (4 * 1024 * 1024))
    meter = _ReadMeter()
    meter.watch(monkeypatch, path)

    result = fs_read(root=tmp_path, path=path.name, start_line=1, end_line=1)

    assert meter.opens == 1
    assert meter.bytes_read <= fs_mod._READ_CHUNK_BYTES
    assert result["content"] == "1: first\n"
    assert result["truncated"] is False
    assert result["total_lines"] is None
    assert "next_range" not in result


def test_skipping_an_enormous_line_keeps_memory_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "skipped.txt"
    prefix = b"x" * (4 * 1024 * 1024) + b"\r\n"
    path.write_bytes(prefix + b"wanted\n" + b"z" * (4 * 1024 * 1024))
    meter = _ReadMeter()
    meter.watch(monkeypatch, path)

    tracemalloc.start()
    try:
        result = fs_read(root=tmp_path, path=path.name, start_line=2, end_line=2)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result["content"] == "2: wanted\n"
    assert result["total_lines"] is None
    assert max(meter.requests) == fs_mod._READ_CHUNK_BYTES
    assert len(prefix) <= meter.bytes_read <= len(prefix) + 2 * fs_mod._READ_CHUNK_BYTES
    # An actual four-megabyte skipped line must not become one string/buffer.
    assert peak < 1_000_000


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 8192])
@pytest.mark.parametrize("numbered", [False, True])
def test_chunk_boundaries_preserve_utf8_and_physical_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chunk_size: int, numbered: bool
) -> None:
    text = "α\r\nβ\rγ\nδ\u2028ε\x85ζ\r\n끝"
    path = tmp_path / "unicode.txt"
    path.write_bytes(text.encode())
    monkeypatch.setattr(fs_mod, "_READ_CHUNK_BYTES", chunk_size)
    physical_lines = list(io.StringIO(text, newline=""))

    result = fs_read(
        root=tmp_path, path=path.name, start_line=2, end_line=20, include_line_numbers=numbered
    )

    expected = "".join(
        f"{number}: {line}" if numbered else line
        for number, line in enumerate(physical_lines[1:], start=2)
    )
    assert result["content"] == expected
    assert result["total_lines"] == len(physical_lines)
    assert result["truncated"] is False


@pytest.mark.parametrize("text", ["", "one", "one\r", "one\r\n", "one\n", "\r\n\r\n"])
def test_observed_eof_and_out_of_range_are_exact(tmp_path: Path, text: str) -> None:
    path = tmp_path / "eof.txt"
    path.write_bytes(text.encode())
    line_count = len(list(io.StringIO(text, newline="")))
    if line_count:
        result = fs_read(root=tmp_path, path=path.name, start_line=1, include_line_numbers=False)
        assert result["content"] == text
        assert result["total_lines"] == line_count
        assert result["truncated"] is False
    with pytest.raises(FsError, match=f"end of file \\({line_count} lines\\)"):
        fs_read(root=tmp_path, path=path.name, start_line=line_count + 1)


def test_continuation_is_a_bounded_suggestion_until_eof(tmp_path: Path) -> None:
    (tmp_path / "lines.txt").write_bytes(b"one\ntwo\nthree\n")
    first = fs_read(root=tmp_path, path="lines.txt", max_bytes=4)
    assert first["total_lines"] is None
    assert first["next_range"] == {"start_line": 2, "end_line": 201}
    second = fs_read(root=tmp_path, path="lines.txt", **first["next_range"])
    assert second["content"] == "2: two\n3: three\n"
    assert second["total_lines"] == 3
    assert second["truncated"] is False


def test_crlf_cut_repeats_the_incomplete_line(tmp_path: Path) -> None:
    (tmp_path / "lines.txt").write_bytes(b"one\r\ntwo\rthree\n")
    first = fs_read(root=tmp_path, path="lines.txt", max_bytes=4)
    assert first["content"] == "one\r"
    assert first["line_clipped"] is True
    assert first["returned_range"] == {"start_line": 1, "end_line": 1}
    assert first["next_range"]["start_line"] == 1
    second = fs_read(root=tmp_path, path="lines.txt", **first["next_range"])
    assert second["content"] == "1: one\r\n2: two\r3: three\n"


def test_empty_utf8_prefix_still_continues_at_first_line(tmp_path: Path) -> None:
    (tmp_path / "unicode.txt").write_text("你好\n", encoding="utf-8")
    result = fs_read(root=tmp_path, path="unicode.txt", max_bytes=1)
    assert result["content"] == ""
    assert result["total_lines"] is None
    assert result["next_range"]["start_line"] == 1


@pytest.fixture
def in_process_file_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    # Use direct I/O: the meter cannot observe descriptor-based or child reads.
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")


def _tools(root: Path):
    store = SessionStore(
        enabled=False,
        sessions_dir=root / "sessions",
        session_id="bounded-read",
        cwd=str(root),
        repo_root=str(root),
    )
    ledgers = []
    tools = build_tools(
        root=root,
        console=None,
        store=store,
        mode="fullaccess",
        yes=True,
        # Measure this process's reads; contained worker transport is covered
        # separately and cannot expose its file handles to the meter.
        cfg=AppConfig(model="offline", extra_fields={"shell_sandbox": {"mode": "off"}}),
        subagents_enabled=False,
        read_ledger_sink=ledgers.append,
    )
    return tools, store, ledgers[0]


@pytest.mark.parametrize("options", [{}, {"start_line": 1, "end_line": 1}])
@pytest.mark.usefixtures("in_process_file_reads")
def test_actual_tool_large_file_does_not_add_full_ledger_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, options: dict
) -> None:
    path = tmp_path / "large.txt"
    path.write_bytes(b"first\n" + b"x" * (4 * 1024 * 1024))
    tools, store, _ledger = _tools(tmp_path)
    meter = _ReadMeter()
    meter.watch(monkeypatch, path)
    try:
        result = tools["fs_read"].run({"path": path.name, "max_bytes": 32, **options})
        repeated = tools["fs_read"].run({"path": path.name, "max_bytes": 32, **options})
    finally:
        store.close()
    assert meter.opens == 2
    assert meter.bytes_read <= 2 * fs_mod._READ_CHUNK_BYTES
    assert result == repeated
    assert "read_ledger_skipped" not in repeated


@pytest.mark.usefixtures("in_process_file_reads")
def test_small_file_tool_keeps_bounded_hashing_and_partial_ledger_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "small.txt"
    path.write_bytes(b"one\nsecond\nthird\n")
    tools, store, ledger = _tools(tmp_path)
    meter = _ReadMeter()
    meter.watch(monkeypatch, path)
    try:
        first = tools["fs_read"].run({"path": path.name, "max_bytes": 6})
        ledger.record_delivery(result=first, content_for_message=json.dumps(first))
        second = tools["fs_read"].run({"path": path.name})
    finally:
        store.close()
    assert first["line_clipped"] is True
    assert second["read_ledger_partial"] is True
    assert second["content"].startswith("second\nthird\n")
    assert second["returned_ranges"] == [{"start_line": 2, "end_line": 3}]
    assert second["skipped_ranges"] == [{"start_line": 1, "end_line": 1}]
    assert meter.opens == 7  # Reader + two hashes per call, plus delivery freshness.
    assert max(meter.requests) <= fs_mod._DEFAULT_FS_READ_MAX_BYTES + 1
    assert meter.bytes_read <= 6 * (fs_mod._DEFAULT_FS_READ_MAX_BYTES + 1)


def test_ledger_declines_large_files_even_when_the_read_budget_is_larger(tmp_path: Path) -> None:
    (tmp_path / "large.txt").write_bytes(b"x" * 20_000)
    tools, store, _ledger = _tools(tmp_path)
    try:
        first = tools["fs_read"].run({"path": "large.txt", "max_bytes": 40_000})
        second = tools["fs_read"].run({"path": "large.txt", "max_bytes": 40_000})
    finally:
        store.close()
    assert first == second
    assert second["truncated"] is False
    assert "read_ledger_skipped" not in second


def test_ledger_growth_during_hash_remains_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "growing.txt"
    path.write_bytes(b"tiny")
    original_stat = Path.stat
    small_stat = path.stat()
    path.write_bytes(b"x" * 100_000)
    monkeypatch.setattr(
        Path, "stat", lambda p, *a, **kw: small_stat if p == path else original_stat(p, *a, **kw)
    )
    meter = _ReadMeter()
    meter.watch(monkeypatch, path)

    assert SessionReadLedger(root=tmp_path).content_hash(path.name) is None
    assert meter.bytes_read == fs_mod._DEFAULT_FS_READ_MAX_BYTES + 1
