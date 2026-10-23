from __future__ import annotations

import asyncio
import subprocess
import threading

import pytest

from alysis_code.cli_impl.tui.diff_view import DiffViewer, FileSummary, display_patch
from alysis_code.code_review import GitReviewError, ReviewLimits
from alysis_code.diff_view import ChangedFile, ChangeList, DiffReader, FilePatch


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    for name in ("both.txt", "gone.txt", "old λ.txt", "binary.bin"):
        (tmp_path / name).write_bytes(b"original\n" if name != "binary.bin" else b"\0old")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def test_states_unicode_rename_binary_and_readonly(repo):
    (repo / "both.txt").write_text("staged\n")
    git(repo, "add", "both.txt")
    (repo / "both.txt").write_text("unstaged\n")
    (repo / "gone.txt").unlink()
    git(repo, "mv", "old λ.txt", "new λ.txt")
    (repo / "binary.bin").write_bytes(b"\0new")
    (repo / "fresh.txt").write_text("new\n")
    (repo / "empty.txt").touch()
    before = (repo / ".git/index").read_bytes()
    contents = {p.name: p.read_bytes() for p in repo.iterdir() if p.is_file()}
    reader = DiffReader(repo)
    changes = reader.list_changes()
    entries = {(x.section, x.paths[-1]): x for x in changes.files}
    assert ("staged", "both.txt") in entries and ("unstaged", "both.txt") in entries
    rename = entries["staged", "new λ.txt"]
    assert rename.paths == ("old λ.txt", "new λ.txt") and rename.status.startswith("R")
    assert entries["unstaged", "gone.txt"].status == "D"
    assert "+staged" in reader.read_patch(entries["staged", "both.txt"]).text
    assert "+unstaged" in reader.read_patch(entries["unstaged", "both.txt"]).text
    assert "rename from" in reader.read_patch(rename).text
    assert (
        "Binary content is not displayed"
        in reader.read_patch(entries["unstaged", "binary.bin"]).text
    )
    assert "+new" in reader.read_patch(entries["untracked", "fresh.txt"]).text
    reader.list_changes()
    assert before == (repo / ".git/index").read_bytes()
    assert contents == {p.name: p.read_bytes() for p in repo.iterdir() if p.is_file()}


def test_bounds_controls_and_literal_pathspec(repo):
    (repo / "large").write_text("x\n" * 10000)
    (repo / "[abc]").write_text("safe\x1b[31m\x07\n")
    reader = DiffReader(repo, limits=ReviewLimits(max_files=300, max_file_bytes=512))
    files = {x.paths[-1]: x for x in reader.list_changes().files}
    patch = reader.read_patch(files["large"])
    assert patch.truncated and "remaining content omitted" in patch.text
    assert len(patch.text.encode()) < 700
    assert "\x1b" not in reader.read_patch(files["[abc]"]).text
    git(repo, "add", "[abc]")
    files = {x.paths[-1]: x for x in reader.list_changes().files}
    assert "+safe" in reader.read_patch(files["[abc]"]).text
    for i in range(5):
        (repo / f"file{i}").touch()
    capped = DiffReader(repo, limits=ReviewLimits(max_files=2)).list_changes()
    assert len(capped.files) == 2
    assert any("omitted" in n for n in capped.notices)


def test_empty_non_git_and_subdirectory(tmp_path, repo):
    empty = tmp_path / "empty"
    empty.mkdir()
    # This fixture lives within repo; explicit non-Git test is separate below.
    assert DiffReader(empty).list_changes().files == ()
    assert DiffReader(repo).list_changes().files == ()


def test_non_git(tmp_path):
    with pytest.raises(GitReviewError, match="Git working tree"):
        DiffReader(tmp_path).list_changes()


def test_unborn_and_untracked_binary(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "new").write_text("added")
    git(tmp_path, "add", "new")
    (tmp_path / "binary").write_bytes(b"\0binary")
    reader = DiffReader(tmp_path)
    entries = {x.paths[-1]: x for x in reader.list_changes().files}
    assert "+added" in reader.read_patch(entries["new"]).text
    assert "binary file" in reader.read_patch(entries["binary"]).text


def test_external_filters_are_not_executed(repo):
    marker = repo / "filter-ran"
    git(repo, "config", "filter.hostile.clean", f'echo executed > "{marker.as_posix()}"')
    (repo / ".gitattributes").write_text("both.txt filter=hostile\n")
    (repo / "both.txt").write_text("changed")
    changes = DiffReader(repo).list_changes()
    assert not marker.exists()
    assert any("unavailable" in n for n in changes.notices)


def test_ui_selection_refresh_close_races_and_hunks(tmp_path):
    async def run():
        started = threading.Event()
        release = threading.Event()
        entries = (ChangedFile("unstaged", "M", ("a",)), ChangedFile("staged", "M", ("b",)))

        class Reader:
            blocked = False

            def list_changes(self):
                return ChangeList(entries)

            def read_patch(self, entry):
                if self.blocked and entry == entries[0]:
                    started.set()
                    release.wait(3)
                return FilePatch(f"{entry.paths[0]}\n@@ -0,0 +1 @@\n+one\n@@ -4,0 +6 @@\n+two")

        reader = Reader()
        closed = []
        viewer = DiffViewer(
            tmp_path,
            reader=reader,
            on_close=lambda: closed.append(True),
            invalidate=lambda: None,
            collapsed=False,
        )
        await viewer.refresh()
        assert "a" in viewer.diff.text
        assert "Unstaged" in str(viewer._subtitle_text())
        viewer.scroll_hunk(1)
        assert viewer.diff.buffer.document.current_line == "1 +one"
        viewer.scroll_hunk(1)
        assert viewer.diff.buffer.document.current_line == "6 +two"
        viewer.scroll_hunk(-1)
        assert viewer.diff.buffer.document.current_line == "1 +one"
        reader.blocked = True
        first = viewer.select(0)
        await asyncio.to_thread(started.wait, 2)
        second = viewer.select(1)
        release.set()
        await asyncio.gather(first, second)
        assert viewer.diff.text.startswith("b\n")
        assert "Staged" in str(viewer._subtitle_text())
        await viewer.refresh()
        assert viewer.index == 1
        started.clear()
        release.clear()
        pending = viewer.select(-1)
        await asyncio.to_thread(started.wait, 2)
        text = viewer.diff.text
        viewer.close()
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        assert viewer.diff.text == text and closed == [True]
        assert not viewer.is_open
        viewer.close()
        assert closed == [True]

    asyncio.run(run())


def test_refresh_race_does_not_replace_newer_list(tmp_path):
    async def run():
        started = threading.Event()
        release = threading.Event()
        a = ChangedFile("unstaged", "M", ("old",))
        b = ChangedFile("unstaged", "M", ("new",))

        class Reader:
            calls = 0

            def list_changes(self):
                self.calls += 1
                if self.calls == 1:
                    started.set()
                    release.wait(3)
                    return ChangeList((a,))
                return ChangeList((b,))

            def read_patch(self, entry):
                return FilePatch(entry.paths[-1])

        viewer = DiffViewer(
            tmp_path,
            reader=Reader(),
            on_close=lambda: None,
            invalidate=lambda: None,
            collapsed=False,
        )
        first = viewer.start_refresh()
        await asyncio.to_thread(started.wait, 2)
        second = viewer.start_refresh()
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        assert viewer.changes.files == (b,)
        assert viewer.diff.text.endswith("new")
        viewer.close()

    asyncio.run(run())


def test_git_timeout_kills_process_and_output_is_bounded(tmp_path, monkeypatch):
    import io

    class Process:
        killed = False
        stdout = io.BytesIO(b"x" * 10000)
        stderr = io.BytesIO(b"e" * 10000)
        returncode = 0

        def wait(self, timeout):
            if not self.killed:
                raise subprocess.TimeoutExpired("git", timeout)

        def kill(self):
            self.killed = True

    process = Process()
    monkeypatch.setattr("alysis_code.diff_view.subprocess.Popen", lambda *a, **kw: process)
    with pytest.raises(GitReviewError, match="timed out"):
        DiffReader(tmp_path)._git(["diff"], 256)
    assert process.killed
    process.stdout = io.BytesIO(b"x" * 10000)
    process.stderr = io.BytesIO(b"e" * 10000)
    result = DiffReader(tmp_path)._git(["diff"], 256)
    assert result.stdout == b"x" * 256 and result.truncated


def test_display_keeps_header_like_code_unicode_and_original_line_numbers():
    patch = FilePatch(
        "diff --git a/example.py b/example.py\nindex abc..def 100644\n"
        "--- a/example.py\n+++ b/example.py\n"
        "@@ -20,3 +20,3 @@ def example():\n"
        " context\n--- source text\n+++ source text λ\u2028tail\n \n"
        "@@ -90 +100 @@\n-old\n+new\n\\ No newline at end of file\n"
    )
    display = display_patch(patch)
    assert "diff --git" not in display.text and "@@" not in display.text
    assert [
        (row.number, row.sign, row.text) for row in display.lines if row.number is not None
    ] == [
        (20, " ", "context"),
        (21, "-", "-- source text"),
        (21, "+", "++ source text λ\u2028tail"),
        (22, " ", ""),
        (90, "-", "old"),
        (100, "+", "new"),
    ]
    assert display.added == display.removed == 2
    assert "No newline at end of file" in display.text and "⋯" in display.text


@pytest.mark.parametrize(
    "text",
    [
        "old mode 100644\nnew mode 100755",
        "Binary files a/a and b/a differ\nBinary content is not displayed.",
        "Content unavailable: unreadable file.",
        "[Empty untracked file; no textual content.]",
    ],
)
def test_display_preserves_non_text_change_explanations(text):
    display = display_patch(FilePatch(text))
    assert text.splitlines()[-1] in display.text
    assert not display.hunks
    assert display.added == display.removed == 0


def test_display_marks_partial_counts_and_omissions(tmp_path):
    viewer = DiffViewer(tmp_path, on_close=lambda: None, invalidate=lambda: None, collapsed=False)
    viewer.changes = ChangeList(
        (ChangedFile("unstaged", "M", ("large.py",)),),
        ("File limit reached; additional changes omitted.",),
    )
    viewer._patch = display_patch(FilePatch("@@ -1 +1 @@\n-old\n+new", truncated=True))
    viewer._loading = False
    viewer._truncated = True
    assert "partial" in str(viewer._title_text())
    assert "remaining content omitted" in viewer._patch.text
    assert "omitted" in viewer._notice_text()
    assert viewer.changes.notices[0] in viewer._help_text()


def test_navigation_during_refresh_keeps_new_file_list(tmp_path):
    async def run():
        started, release = threading.Event(), threading.Event()
        entries = tuple(ChangedFile("unstaged", "M", (name,)) for name in ("a", "b", "new"))

        class Reader:
            calls = 0

            def list_changes(self):
                self.calls += 1
                if self.calls == 1:
                    return ChangeList(entries[:2])
                started.set()
                release.wait(3)
                return ChangeList(entries)

            def read_patch(self, entry):
                return FilePatch(entry.paths[-1])

        viewer = DiffViewer(
            tmp_path,
            reader=Reader(),
            on_close=lambda: None,
            invalidate=lambda: None,
            collapsed=False,
        )
        await viewer.refresh()
        refresh = viewer.start_refresh()
        try:
            assert await asyncio.to_thread(started.wait, 2)
            selection = viewer.select(1)
            release.set()
            await asyncio.gather(refresh, selection)
            assert viewer.changes.files == entries
            assert viewer.index == 1 and viewer.diff.text == "b"
        finally:
            release.set()
            viewer.close()

    asyncio.run(run())


async def settle_viewer(viewer):
    async with asyncio.timeout(5):
        while viewer._tasks:
            await asyncio.gather(*tuple(viewer._tasks), return_exceptions=True)
            await asyncio.sleep(0)


def test_summary_does_not_infer_binary_or_unavailable_from_filenames():
    patch = FilePatch(
        "diff --git a/binary.py b/unavailable.py\nsimilarity index 100%\n"
        "rename from binary.py\nrename to unavailable.py\n"
    )
    summary = FileSummary.from_patch(patch, display_patch(patch))
    assert summary == FileSummary(0, 0)


def test_collapsed_summary_counts_are_honest_and_code_is_hidden(tmp_path):
    async def run():
        entries = tuple(
            ChangedFile("unstaged", "M", (name,))
            for name in ("normal.py", "large.py", "binary.bin", "unreadable.txt", "permissions.sh")
        )
        patches = {
            "normal.py": FilePatch("@@ -1 +1 @@\n-old\n+new"),
            "large.py": FilePatch("@@ -0,0 +1,2 @@\n+one\n+two", truncated=True),
            "binary.bin": FilePatch("Binary content is not displayed."),
            "permissions.sh": FilePatch("old mode 100644\nnew mode 100755"),
        }

        class Reader:
            def list_changes(self):
                return ChangeList(entries)

            def read_patch(self, entry):
                return patches[entry.paths[-1]]

        viewer = DiffViewer(
            tmp_path, reader=Reader(), on_close=lambda: None, invalidate=lambda: None
        )
        await viewer.refresh()
        await settle_viewer(viewer)
        assert viewer.collapsed and viewer.diff.text == ""
        rows = "".join(text for _, text in viewer._summary_text()).splitlines()
        assert "+1 -1" in rows[0]
        assert "+2 -0 partial" in rows[1]
        assert "binary" in rows[2] and "+0" not in rows[2]
        assert "unavailable" in rows[3] and "+0" not in rows[3]
        assert "+0 -0" in rows[4]
        viewer.close()

    asyncio.run(run())


def test_summary_reads_visible_files_lazily_and_refresh_replaces_counts(tmp_path):
    async def run():
        entries = tuple(ChangedFile("unstaged", "M", (f"file{i}.py",)) for i in range(80))

        class Reader:
            lines = 1
            read = []

            def list_changes(self):
                return ChangeList(entries)

            def read_patch(self, entry):
                self.read.append(entry)
                return FilePatch(f"@@ -0,0 +1,{self.lines} @@\n" + "+line\n" * self.lines)

        reader = Reader()
        viewer = DiffViewer(tmp_path, reader=reader, on_close=lambda: None, invalidate=lambda: None)
        await viewer.refresh()
        await settle_viewer(viewer)
        assert 0 < len(reader.read) < len(entries)
        assert entries[60] not in reader.read
        viewer.select(60)
        await settle_viewer(viewer)
        assert viewer._summaries[entries[60]].added == 1
        assert len(reader.read) < len(entries)
        reader.lines = 2
        await viewer.refresh()
        await settle_viewer(viewer)
        assert viewer.index == 60
        assert viewer._summaries[entries[60]].added == 2
        assert entries[0] not in viewer._summaries
        viewer.close()

    asyncio.run(run())


def test_closing_while_summary_loads_discards_result(tmp_path):
    async def run():
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        entry = ChangedFile("unstaged", "M", ("slow.py",))

        class Reader:
            def list_changes(self):
                return ChangeList((entry,))

            def read_patch(self, _):
                started.set()
                release.wait(3)
                finished.set()
                return FilePatch("@@ -0,0 +1 @@\n+late")

        viewer = DiffViewer(
            tmp_path, reader=Reader(), on_close=lambda: None, invalidate=lambda: None
        )
        await viewer.refresh()
        try:
            assert await asyncio.to_thread(started.wait, 2)
            viewer.close()
            release.set()
            await settle_viewer(viewer)
            assert await asyncio.to_thread(finished.wait, 2)
            assert not viewer._summaries and not viewer.diff.text
        finally:
            release.set()

    asyncio.run(run())
