from __future__ import annotations

import asyncio
import importlib
import subprocess
import threading

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from alysis_code.cli_impl.chat_slash_completer import DIFF_COMMAND_SPEC, ChatSlashCompleter
from alysis_code.cli_impl.tui.diff_view import DiffViewer
from alysis_code.cli_impl.tui.state import TuiState
from alysis_code.diff_view import ChangedFile, ChangeList, FilePatch


@pytest.fixture(autouse=True)
def profile(monkeypatch, tmp_path):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))


async def eventually(predicate):
    for _ in range(1500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("UI did not reach requested state")


def escape(app, pipe):
    pipe.send_text("\x1b")
    app.key_processor.feed_multiple(pipe.flush_keys())
    app.key_processor.process_keys()


class Reader:
    def list_changes(self):
        return ChangeList(
            (
                ChangedFile("unstaged", "M", ("a.py",)),
                ChangedFile("staged", "R100", ("b.py", "c.py")),
            )
        )

    def read_patch(self, entry):
        return FilePatch("@@ -1 +1 @@\n-old\n+new\n@@ -9,0 +10 @@\n+more")


def drive(
    monkeypatch, root, scenario, *, reader=None, active=None, running=False, screen_size=None
):
    module = importlib.import_module("alysis_code.cli_impl.tui.app")
    original = module.Application
    viewers, errors, calls, routed, tokens, modes = [], [], [], [], [], []
    release = threading.Event()

    class Output(DummyOutput):
        def get_size(self):
            if screen_size:
                return Size(rows=screen_size[1], columns=screen_size[0])
            return super().get_size()

    class Session:
        def run_turn(self, text, cancellation_token=None, **kwargs):
            calls.append(text)
            tokens.append(cancellation_token)
            if running:
                release.wait(10)

    def runner(session, text, width):
        routed.append(text)
        return "run", "", text, {}

    def factory(workspace, **kwargs):
        viewer = DiffViewer(workspace, reader=reader, **kwargs)
        viewers.append(viewer)
        return viewer

    with create_pipe_input() as pipe:

        class Application(original):
            def run(self, *, pre_run):
                async def task():
                    try:
                        await scenario(self, pipe, viewers, calls, tokens)
                    except BaseException as exc:
                        exc.add_note(
                            f"focus={self.layout.current_control!r}; open={[v.is_open for v in viewers]}"
                        )
                        errors.append(exc)
                    finally:
                        release.set()
                        if not self.is_done:
                            self.exit()

                def begin():
                    pre_run()

                    def failed(loop, context):
                        errors.append(context.get("exception", AssertionError(str(context))))
                        release.set()
                        if not self.is_done:
                            self.exit()

                    asyncio.get_running_loop().set_exception_handler(failed)
                    self.create_background_task(task())

                return super().run(pre_run=begin, set_exception_handler=False)

        monkeypatch.setattr(module, "Application", Application)
        module.run_tui(
            TuiState(model_name="test"),
            input=pipe,
            output=Output(),
            owl_color=False,
            background_turns=running,
            workspace_root=root,
            workdir_provider=(lambda: active[0]) if active else None,
            session_builder=lambda _: Session(),
            command_runner=runner,
            mode_cycle=lambda: modes.append("mode"),
            diff_viewer_factory=factory,
            completer=ChatSlashCompleter(
                mode_provider=lambda: "chat", extra_specs=(DIFF_COMMAND_SPEC,)
            ),
        )
    if errors:
        raise errors[0]
    assert not modes
    assert all(not viewer.is_open for viewer in viewers)
    assert not any(text.startswith("/diff") for text in routed + calls)
    return calls


def test_real_git_command_navigation_refresh_no_mutations(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    (root / "staged.txt").write_text("staged\n")
    subprocess.run(["git", "-C", str(root), "add", "staged.txt"], check=True)
    (root / "fresh.txt").write_text("fresh\n")
    before = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }

    async def scenario(app, pipe, viewers, calls, tokens):
        composer = app.current_buffer
        pipe.send_text("/diff\r")
        await eventually(lambda: viewers and not viewers[-1]._tasks)
        viewer = viewers[-1]
        assert {entry.section for entry in viewer.changes.files} == {"staged", "untracked"}
        assert app.layout.has_focus(viewer.summary_control)
        assert viewer.collapsed and not viewer.diff.text
        app._redraw()
        assert viewer.summary_window in app.renderer._last_screen.visible_windows
        assert viewer.diff.window not in app.renderer._last_screen.visible_windows
        summary = "".join(text for _, text in viewer._summary_text())
        assert "staged.txt" in summary and "fresh.txt" in summary and "+1 -0" in summary
        assert not viewer.files_open
        pipe.send_text("\x1b[B\r")
        await eventually(lambda: viewer.index == 1 and not viewer.collapsed and not viewer._tasks)
        assert app.layout.has_focus(viewer.diff)
        assert "Untracked" in str(viewer._subtitle_text())
        assert "+fresh" in viewer.diff.text and "diff --git" not in viewer.diff.text
        generation = viewer._generation
        pipe.send_text("r")
        await eventually(lambda: viewer._generation > generation and not viewer._tasks)
        escape(app, pipe)
        await eventually(lambda: not viewer.is_open)
        assert app.current_buffer is composer
        assert composer.text == "/diff" and composer.cursor_position == 5
        assert calls == []

    drive(monkeypatch, root, scenario)
    assert before == {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def test_diff_shortcut_preserves_exact_draft_cursor_attachments_and_opaque_paste(
    monkeypatch, tmp_path
):
    file = tmp_path / "notes.txt"
    file.write_text("notes")
    payload = "[File #1]\n" + "\n".join(f"opaque {i}" for i in range(20))

    async def scenario(app, pipe, viewers, calls, tokens):
        composer = app.current_buffer
        pipe.send_text(f"\x1b[200~{file}\x1b[201~\x1b[200~{payload}\x1b[201~ draft")
        await eventually(lambda: composer.text.endswith(" draft"))
        composer.cursor_position -= 2
        saved = composer.document
        pipe.send_text("\x10")
        await eventually(lambda: viewers and not viewers[-1]._tasks)
        viewer = viewers[-1]
        await eventually(lambda: viewer.container in app.layout._child_to_parent)
        pipe.send_text("\r")
        await eventually(lambda: not viewer.collapsed and not viewer._tasks)
        assert app.layout.has_focus(viewer.diff)
        pipe.send_text("n")
        await eventually(lambda: viewer.diff.buffer.document.current_line == "10 +more")
        pipe.send_text("p")
        await eventually(lambda: viewer.diff.buffer.document.current_line.strip() == "1 -old")
        pipe.send_text("\x1b[Z")
        await eventually(lambda: app.layout.has_focus(viewer.list_control))
        assert viewer.files_open
        pipe.send_text("\r")
        await eventually(lambda: not viewer.files_open and app.layout.has_focus(viewer.diff))
        pipe.send_text("?")
        await eventually(lambda: viewer.help_open)
        pipe.send_text("?")
        await eventually(lambda: not viewer.help_open)
        pipe.send_text("c")
        await eventually(lambda: viewer.collapsed and app.layout.has_focus(viewer.summary_control))
        app._redraw()
        assert viewer.diff.window not in app.renderer._last_screen.visible_windows
        assert viewer.summary_window in app.renderer._last_screen.visible_windows
        pipe.send_text("\r")
        await eventually(lambda: not viewer.collapsed and app.layout.has_focus(viewer.diff))
        escape(app, pipe)
        await eventually(lambda: not viewer.is_open)
        assert composer.document == saved
        pipe.send_text("\r")
        await eventually(lambda: len(calls) == 1)
        assert calls == [f"{file.resolve()} {payload} draft"]

    drive(monkeypatch, tmp_path, scenario, reader=Reader())


def test_active_turn_escape_does_not_interrupt_and_workspace_changes(monkeypatch, tmp_path):
    active = [tmp_path]
    other = tmp_path / "other"
    other.mkdir()

    async def scenario(app, pipe, viewers, calls, tokens):
        composer = app.current_buffer
        pipe.send_text("work\r")
        await eventually(lambda: bool(tokens))
        pipe.send_text("/diff\r")
        await eventually(lambda: viewers and not viewers[-1]._tasks)
        escape(app, pipe)
        await eventually(lambda: not viewers[-1].is_open)
        assert not tokens[0].is_cancelled
        assert composer.text == "/diff"
        active[0] = other
        pipe.send_text("\r")
        await eventually(lambda: len(viewers) == 2)
        assert viewers[-1].workspace == other
        pipe.send_text("\x03")
        await eventually(lambda: not viewers[-1].is_open)
        assert not tokens[0].is_cancelled
        assert calls == ["work"]

    drive(monkeypatch, tmp_path, scenario, reader=Reader(), active=active, running=True)


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("draft_kind", ["text", "paste", "completion"])
def test_ctrl_p_opens_collapsed_diff_and_preserves_turn_and_draft(
    monkeypatch, tmp_path, running, draft_kind
):
    payload = "\n".join(f"opaque line {i}" for i in range(20))

    async def scenario(app, pipe, viewers, calls, tokens):
        composer = app.current_buffer
        if running:
            pipe.send_text("work\r")
            await eventually(lambda: bool(tokens))
        if draft_kind == "paste":
            pipe.send_text(f"\x1b[200~{payload}\x1b[201~")
            await eventually(lambda: "[pasted" in composer.text)
            submitted = payload
        elif draft_kind == "completion":
            composer.document = Document("/di", 3)
            composer.start_completion(select_first=False)
            await eventually(lambda: composer.complete_state is not None)
        else:
            composer.document = Document("keep this draft", 4)
            submitted = composer.text
        saved = composer.document
        pipe.send_text("\x10")
        await eventually(lambda: viewers and not viewers[-1]._tasks)
        viewer = viewers[-1]
        assert viewer.collapsed
        assert app.layout.has_focus(viewer.summary_control)
        assert composer.document == saved and composer.complete_state is None
        # A second press inside the viewer must not open another modal.
        pipe.send_text("\x10")
        escape(app, pipe)
        await eventually(lambda: not viewer.is_open)
        assert len(viewers) == 1
        assert app.current_buffer is composer and composer.document == saved
        assert calls == (["work"] if running else [])
        if running:
            assert not tokens[0].is_cancelled
        elif draft_kind != "completion":
            pipe.send_text("\r")
            await eventually(lambda: calls == [submitted])

    drive(monkeypatch, tmp_path, scenario, reader=Reader(), running=running)


@pytest.mark.parametrize("git_repository", [False, True])
def test_empty_and_non_git_states(monkeypatch, tmp_path, git_repository):
    root = tmp_path / "workspace"
    root.mkdir()
    if git_repository:
        subprocess.run(["git", "init", "--quiet", str(root)], check=True)

    async def scenario(app, pipe, viewers, calls, tokens):
        pipe.send_text("/diff\r")
        await eventually(lambda: viewers and not viewers[-1]._tasks)
        assert not viewers[-1].changes.files
        assert ("No working-tree changes" if git_repository else "not an available Git") in viewers[
            -1
        ].message

    drive(monkeypatch, root, scenario)


def test_diff_discovery_is_tui_only():
    from prompt_toolkit.document import Document

    classic = ChatSlashCompleter(mode_provider=lambda: "chat")
    tui = ChatSlashCompleter(mode_provider=lambda: "chat", extra_specs=(DIFF_COMMAND_SPEC,))
    assert list(classic.get_completions(Document("/di"), None)) == []
    assert [c.text for c in tui.get_completions(Document("/di"), None)] == ["/diff"]


def test_closing_loading_viewer_discards_stale_output(monkeypatch, tmp_path):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    class SlowReader(Reader):
        def list_changes(self):
            started.set()
            release.wait(5)
            finished.set()
            return super().list_changes()

    async def scenario(app, pipe, viewers, calls, tokens):
        composer = app.current_buffer
        pipe.send_text("/diff\r")
        try:
            await eventually(lambda: viewers and started.is_set())
            viewer = viewers[-1]
            await eventually(lambda: viewer.container in app.layout._child_to_parent)
            assert "Loading" in viewer.message
            escape(app, pipe)
            await eventually(lambda: not viewer.is_open and not viewer._tasks)
            assert app.current_buffer is composer
            release.set()
            await eventually(finished.is_set)
            assert viewer.changes.files == () and viewer.diff.text == ""
        finally:
            release.set()

    drive(monkeypatch, tmp_path, scenario, reader=SlowReader())


def test_unsupported_diff_arguments_never_fall_through_to_model(monkeypatch, tmp_path):
    async def scenario(app, pipe, viewers, calls, tokens):
        pipe.send_text("/diff unsupported\r")
        await eventually(lambda: app.current_buffer.text == "/diff unsupported")
        # The next local command flushes the previous command's key dispatch.
        app.current_buffer.reset()
        pipe.send_text("/diff\r")
        await eventually(lambda: bool(viewers))
        assert calls == []

    drive(monkeypatch, tmp_path, scenario, reader=Reader())


@pytest.mark.parametrize("screen_size", [(80, 24), (50, 18)])
def test_arrows_and_paging_reach_long_wrapped_code(monkeypatch, tmp_path, screen_size):
    class LongReader(Reader):
        def read_patch(self, entry):
            line = "".join(f"segment{i:03d} λ界 " for i in range(150)) + " THE_END"
            return FilePatch("@@ -0,0 +1 @@\n+" + line)

    async def scenario(app, pipe, viewers, calls, tokens):
        pipe.send_text("/diff\r")
        await eventually(lambda: viewers and not viewers[-1]._tasks)
        viewer = viewers[-1]
        pipe.send_text("\r")
        await eventually(lambda: not viewer.collapsed and not viewer._tasks)
        app._redraw()
        pipe.send_text("\x1b[B")
        await eventually(lambda: viewer.diff.buffer.document.cursor_position_col > 0)
        assert viewer.diff.buffer.document.cursor_position_row == 0
        pipe.send_text("\x1b[6~" * 30)
        await eventually(
            lambda: viewer.diff.buffer.cursor_position > len(viewer.diff.text) - screen_size[0]
        )
        app._redraw()
        assert viewer.diff.window.vertical_scroll_2 > 0
        screen = app.renderer._last_screen
        rendered = "\n".join(
            "".join(screen.data_buffer[y][x].char for x in range(screen_size[0]))
            for y in range(screen_size[1])
        )
        assert "THE_END" in rendered, (
            f"cursor={viewer.diff.buffer.cursor_position}; last={viewer._visual_rows[-1]}; "
            f"scroll={viewer.diff.window.vertical_scroll_2}\n{rendered}"
        )
        last = viewer.diff.buffer.cursor_position
        pipe.send_text("\x1b[5~")
        await eventually(lambda: viewer.diff.buffer.cursor_position < last)
        previous = viewer.diff.buffer.cursor_position
        pipe.send_text("\x1b[A")
        await eventually(lambda: viewer.diff.buffer.cursor_position < previous)
        assert calls == []

    drive(monkeypatch, tmp_path, scenario, reader=LongReader(), screen_size=screen_size)
