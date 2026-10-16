from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import to_formatted_text

from alysis_code.cli_impl.chat_slash_completer import ChatSlashCompleter
from alysis_code.cli_impl.file_mention_completer import (
    ChatComposerCompleter,
    FileMentionCompleter,
    mention_at_cursor,
    quote_reference,
)
from alysis_code.cli_impl.workspace_file_index import (
    FileIndexSnapshot,
    WorkspaceFileIndex,
    scan_workspace,
)


class StaticIndex:
    def __init__(self, *paths, truncated=False):
        self.snapshot = FileIndexSnapshot(paths, truncated=truncated)

    def cached(self, root):
        return self.snapshot

    async def ready(self, root):
        return self.snapshot


def completions(text, *paths, cursor=None):
    completer = FileMentionCompleter(workdir_provider=lambda: Path.cwd(), index=StaticIndex(*paths))
    document = Document(text, cursor_position=cursor)
    return document, list(completer.get_completions(document, None))


@pytest.mark.parametrize("text", ["@src", "explain @src", "compare @docs/a.md with @src"])
def test_start_middle_multiple_mentions(text):
    document, items = completions(text, "src/main.py", "docs/a.md")
    assert [c.text for c in items] == ["@src/main.py"]
    buffer = Buffer(document=document)
    buffer.apply_completion(items[0])
    assert buffer.text == text[:-4] + "@src/main.py"


@pytest.mark.parametrize(
    "text", ["hi@example.com", r"explain \@src", "@src prose", "@../secret", "@/etc"]
)
def test_prose_emails_escaped_and_external_paths_are_not_mentions(text):
    assert mention_at_cursor(text, len(text)) is None


@pytest.mark.parametrize(
    "path", ["docs/hello world.md", "δοκιμή/σχέδιο.md", 'docs/a"b.md', "docs/a\\b.md"]
)
def test_quoting_unicode_spaces_and_escapes(path):
    document, items = completions("explain @", path)
    buffer = Buffer(document=document)
    buffer.apply_completion(items[0])
    reference = buffer.text.removeprefix("explain @")
    assert (json.loads(reference) if reference.startswith('"') else reference) == path


def test_incomplete_quoted_path():
    _, items = completions('explain @"docs/hello w', "docs/hello world.md")
    assert items[0].text == '@"docs/hello world.md"'


@pytest.mark.parametrize(
    "draft,cursor,path,expected",
    [
        ("explain @src then @docs", 12, "src/main.py", "explain @src/main.py then @docs"),
        ("explain @sr/main.py then @docs", 11, "src/main.py", "explain @src/main.py then @docs"),
        (
            'read @"docs/he world.md" then @src',
            14,
            "docs/hello world.md",
            'read @"docs/hello world.md" then @src',
        ),
    ],
)
def test_edit_earlier_mention_preserves_every_character_after_cursor(draft, cursor, path, expected):
    document, items = completions(draft, path, cursor=cursor)
    assert items
    buffer = Buffer(document=document)
    buffer.apply_completion(items[0])
    assert buffer.text == expected
    assert buffer.text.endswith(draft[cursor:])


def test_incompatible_suffix_is_not_duplicated_or_removed():
    _, items = completions("@src/wrong.py afterwards", "src/main.py", cursor=4)
    assert items == []


def test_visible_result_limits():
    completer = FileMentionCompleter(
        workdir_provider=Path.cwd, index=StaticIndex("a", "b", "c"), max_results=2
    )
    items = list(completer.get_completions(Document("@"), None))
    assert len(items) == 2
    assert "results limited" in "".join(t for _, t in to_formatted_text(items[0].display_meta))


def test_composite_preserves_slash_and_dollar_skill_completion():
    delegate = ChatSlashCompleter(
        mode_provider=lambda: "chat", skill_names_provider=lambda: ["demo"]
    )
    composite = ChatComposerCompleter(
        delegate, workdir_provider=Path.cwd, index=StaticIndex("src/a.py")
    )
    for text in ["/co", "$de", "/skill de"]:
        document = Document(text)
        assert list(composite.get_completions(document, None)) == list(
            delegate.get_completions(document, None)
        )
    assert list(composite.get_completions(Document("ask @src"), None))[0].text == "@src/a.py"


def test_first_async_request_awaits_scan_and_synchronous_requests_never_scan_inline(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []
    main_thread = threading.get_ident()

    def scanner(root, **kwargs):
        calls.append(threading.get_ident())
        started.set()
        assert release.wait(5)
        return FileIndexSnapshot(("src/a.py",))

    index = WorkspaceFileIndex(scanner=scanner)
    try:
        for _ in range(100):
            assert index.cached(tmp_path).paths == ()
        assert started.wait(5)
        assert calls == [calls[0]] and calls[0] != main_thread
        release.set()
        completer = FileMentionCompleter(workdir_provider=lambda: tmp_path, index=index)

        async def collect():
            return [c.text async for c in completer.get_completions_async(Document("@src"), None)]

        assert asyncio.run(collect()) == ["@src/a.py"]
        assert len(calls) == 1
    finally:
        release.set()
        index.close()


def test_root_switch_and_invalidation_cannot_reuse_previous_paths(tmp_path):
    calls = []

    def scanner(root, **kwargs):
        calls.append(root)
        return FileIndexSnapshot((root.name + ".txt",))

    index = WorkspaceFileIndex(scanner=scanner)
    try:
        assert asyncio.run(index.ready(tmp_path / "one")).paths == ("one.txt",)
        assert asyncio.run(index.ready(tmp_path / "two")).paths == ("two.txt",)
        index.invalidate()
        asyncio.run(index.ready(tmp_path / "two"))
        assert len(calls) == 3
    finally:
        index.close()


def test_expiration_refreshes_deleted_and_new_files(tmp_path, monkeypatch):
    import alysis_code.cli_impl.workspace_file_index as module

    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    contents = ["old.py"]
    index = WorkspaceFileIndex(scanner=lambda root, **kwargs: FileIndexSnapshot(tuple(contents)))
    try:
        assert asyncio.run(index.ready(tmp_path)).paths == ("old.py",)
        contents[:] = ["new.py"]
        clock[0] += 6
        assert asyncio.run(index.ready(tmp_path)).paths == ("new.py",)
    finally:
        index.close()


def test_workspace_switch_discards_in_flight_old_scan(tmp_path):
    started, release = threading.Event(), threading.Event()

    def scanner(root, **kwargs):
        if root.name == "old":
            started.set()
            assert release.wait(5)
        return FileIndexSnapshot((root.name + ".py",))

    index = WorkspaceFileIndex(scanner=scanner)
    try:
        assert index.cached(tmp_path / "old").paths == ()
        assert started.wait(5)
        assert index.cached(tmp_path / "new").paths == ()
        release.set()
        assert asyncio.run(index.ready(tmp_path / "new")).paths == ("new.py",)
    finally:
        release.set()
        index.close()


@pytest.mark.skipif(shutil.which("git") is None, reason="Git ignore matcher unavailable")
@pytest.mark.parametrize("repository", [False, True])
def test_scan_honors_nested_ignore_negation_and_generated_trees(tmp_path, repository):
    if repository:
        subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("*.secret\nignored/\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / ".gitignore").write_text("*.txt\n!keep.txt\n", encoding="utf-8")
    for name in ["src/drop.txt", "src/keep.txt", "src/code.py", "password.secret"]:
        (tmp_path / name).write_text("test", encoding="utf-8")
    for name in ["ignored", "node_modules", ".venv"]:
        (tmp_path / name).mkdir()
        (tmp_path / name / "secret.py").write_text("test", encoding="utf-8")
    snapshot = scan_workspace(tmp_path)
    assert not snapshot.unavailable
    assert "src/keep.txt" in snapshot.paths
    assert "src/code.py" in snapshot.paths
    assert "src/drop.txt" not in snapshot.paths
    assert "password.secret" not in snapshot.paths
    assert not any("secret.py" in p for p in snapshot.paths)


def test_scan_has_deterministic_entry_budget(tmp_path):
    for number in range(100):
        (tmp_path / f"file-{number}.txt").touch()
    snapshot = scan_workspace(tmp_path, max_entries=17)
    assert snapshot.visited == 17
    assert len(snapshot.paths) <= 17
    assert snapshot.truncated


def test_scan_excludes_directory_and_file_symlinks(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").touch()
    try:
        (root / "linked-dir").symlink_to(outside, target_is_directory=True)
        (root / "linked-file").symlink_to(outside / "secret")
    except OSError:
        pytest.skip("Creating symlinks requires platform privileges")
    assert scan_workspace(root).paths == ()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_windows_junction_never_traverses_outside_workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.py").touch()
    junction = root / "linked"
    # All fixture paths are generated by pytest under the unique OS temp root.
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "New-Item -ItemType Junction -Path $env:MENTION_LINK -Target $env:MENTION_TARGET | Out-Null",
        ],
        check=True,
        capture_output=True,
        env={**os.environ, "MENTION_LINK": str(junction), "MENTION_TARGET": str(outside)},
    )
    try:
        assert scan_workspace(root).paths == ()
    finally:
        os.rmdir(junction)


def test_quoting_simple_reference():
    assert quote_reference("src/main.py") == "@src/main.py"
