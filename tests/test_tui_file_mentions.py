"""Drive real prompt_toolkit key dispatch and background completion."""

from __future__ import annotations

import asyncio
import importlib
import time

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from alysis_code.cli_impl.chat_slash_completer import ChatSlashCompleter
from alysis_code.cli_impl.tui.prompt_history import PromptHistory
from alysis_code.cli_impl.tui.state import TuiState
from alysis_code.cli_impl.workspace_file_index import FileIndexSnapshot, WorkspaceFileIndex


@pytest.fixture(autouse=True)
def profile(monkeypatch, tmp_path):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))


async def eventually(predicate):
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("UI did not reach the requested state")


def escape(app, pipe):
    pipe.send_text("\x1b")
    # Flush a standalone Escape through the real key processor, without relying
    # on the terminal decoder's wall-clock ambiguity timeout in the test.
    app.key_processor.feed_multiple(pipe.read_keys())
    app.key_processor.feed_multiple(pipe.flush_keys())
    app.key_processor.process_keys()


def drive(monkeypatch, root, scenario, *, workdir_provider=None):
    module = importlib.import_module("alysis_code.cli_impl.tui.app")
    calls, errors, personas, closed = [], [], [], []
    original_application = module.Application
    original_close = WorkspaceFileIndex.close
    original_index_init = WorkspaceFileIndex.__init__

    def index_init(index):
        # Keep terminal routing deterministic under load. Real Git discovery,
        # bounds and link handling are exercised in test_file_mention_completer.
        def scan(workdir, **kwargs):
            return FileIndexSnapshot(
                tuple(
                    sorted(
                        path.relative_to(workdir).as_posix() + ("/" if path.is_dir() else "")
                        for path in workdir.rglob("*")
                    )
                )
            )

        original_index_init(index, scanner=scan)

    def close(index):
        closed.append(index)
        original_close(index)

    monkeypatch.setattr(WorkspaceFileIndex, "close", close)
    monkeypatch.setattr(WorkspaceFileIndex, "__init__", index_init)

    class Session:
        def run_turn(self, text, **kwargs):
            calls.append(text)

    with create_pipe_input() as pipe:

        class Application(original_application):
            def run(self, *, pre_run):
                async def task():
                    try:
                        await scenario(self, pipe, calls)
                    except BaseException as exc:
                        exc.add_note(
                            f"draft={self.current_buffer.text!r}; completion={self.current_buffer.complete_state!r}; keys={self.key_processor.key_buffer!r}"
                        )
                        errors.append(exc)
                    finally:
                        if not self.is_done:
                            self.exit()

                def begin():
                    pre_run()

                    def failed(loop, context):
                        errors.append(context.get("exception", AssertionError(str(context))))
                        if not self.is_done:
                            self.exit()

                    asyncio.get_running_loop().set_exception_handler(failed)
                    self.create_background_task(task())

                return super().run(pre_run=begin, set_exception_handler=False)

        monkeypatch.setattr(module, "Application", Application)
        module.run_tui(
            TuiState(model_name="test"),
            input=pipe,
            output=DummyOutput(),
            owl_color=False,
            background_turns=False,
            workspace_root=root,
            workdir_provider=workdir_provider,
            session_builder=lambda _: Session(),
            persona_cycle=lambda: personas.append("cycled"),
            completer=ChatSlashCompleter(
                mode_provider=lambda: "chat", skill_names_provider=lambda: ["demo"]
            ),
        )
    if errors:
        raise errors[0]
    assert len(closed) == 1
    assert not personas
    return calls


def test_mid_sentence_unicode_spaces_multiple_mentions_and_history(monkeypatch, tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "δοκιμή notes.md").touch()
    (root / "other.py").touch()
    expected = 'explain @"δοκιμή notes.md" with @other.py'

    async def scenario(app, pipe, calls):
        buffer = app.current_buffer
        pipe.send_text("explain @δοκ")
        await eventually(
            lambda: buffer.complete_state is not None and bool(buffer.complete_state.completions)
        )
        pipe.send_text("\t")
        await eventually(
            lambda: buffer.text == 'explain @"δοκιμή notes.md"' and buffer.complete_state is None
        )
        pipe.send_text(" with @oth")
        await eventually(
            lambda: buffer.complete_state is not None and bool(buffer.complete_state.completions)
        )
        pipe.send_text("\t")
        await eventually(lambda: buffer.text == expected and buffer.complete_state is None)
        pipe.send_text("\r")
        await eventually(lambda: calls == [expected])
        pipe.send_text("unsent draft")
        await eventually(lambda: buffer.text == "unsent draft" and buffer.complete_state is None)
        pipe.send_text("\x01\x1b[A")
        await eventually(lambda: buffer.text == expected)
        await eventually(lambda: buffer.complete_state is None)
        pipe.send_text("\x05\x1b[B")
        await eventually(lambda: buffer.text == "unsent draft")
        pipe.send_text("\x01\x1b[A\r")
        await eventually(lambda: calls == [expected, expected])

    assert drive(monkeypatch, root, scenario) == [expected, expected]
    entries = PromptHistory(root).entries
    assert [entry.text for entry in entries] == [expected]
    assert not entries[0].attachments


def test_escape_restores_draft_and_earlier_mention_keeps_suffix(monkeypatch, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").touch()

    async def scenario(app, pipe, calls):
        buffer = app.current_buffer
        pipe.send_text("explain @src/m")
        await eventually(
            lambda: buffer.complete_state is not None and bool(buffer.complete_state.completions)
        )
        pipe.send_text("\x1b[B")
        await eventually(lambda: buffer.text == "explain @src/main.py")
        escape(app, pipe)
        await eventually(lambda: buffer.complete_state is None)
        assert buffer.text == "explain @src/m"
        pipe.send_text(" then @other")
        await eventually(lambda: buffer.text.endswith(" then @other"))
        # Move into the earlier token; Tab explicitly opens completion here.
        buffer.cursor_position = len("explain @src/m")
        pipe.send_text("\t")
        await eventually(
            lambda: buffer.complete_state is not None and bool(buffer.complete_state.completions)
        )
        pipe.send_text("\t")
        await eventually(lambda: buffer.complete_state is None)
        assert buffer.text == "explain @src/main.py then @other"

    drive(monkeypatch, tmp_path, scenario)


def test_active_workdir_changes_and_slash_skill_completion(monkeypatch, tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    (first / "old.py").touch()
    (second / "new.py").touch()
    active = [first]

    async def scenario(app, pipe, calls):
        buffer = app.current_buffer
        for query, expected in [("/sta", "/status"), ("$de", "$demo")]:
            pipe.send_text(query)
            await eventually(
                lambda: (
                    buffer.complete_state is not None and bool(buffer.complete_state.completions)
                )
            )
            assert buffer.complete_state.completions[0].text == expected
            pipe.send_text("\t")
            await eventually(lambda expected=expected: buffer.text == expected)
            escape(app, pipe)
            await eventually(lambda: buffer.complete_state is None)
            buffer.reset()
        pipe.send_text("see @old")
        await eventually(
            lambda: buffer.complete_state is not None and bool(buffer.complete_state.completions)
        )
        assert buffer.complete_state.completions[0].text == "@old.py"
        escape(app, pipe)
        await eventually(lambda: buffer.complete_state is None)
        buffer.reset()
        active[0] = second
        pipe.send_text("see @")
        await eventually(
            lambda: buffer.complete_state is not None and bool(buffer.complete_state.completions)
        )
        assert [c.text for c in buffer.complete_state.completions] == ["@new.py"]

    drive(monkeypatch, first, scenario, workdir_provider=lambda: active[0])


def test_completion_worker_closed_when_application_run_fails(monkeypatch, tmp_path):
    module = importlib.import_module("alysis_code.cli_impl.tui.app")
    closed = []
    original_close = WorkspaceFileIndex.close

    def close(index):
        closed.append(index)
        original_close(index)

    def fail(*args, **kwargs):
        raise RuntimeError("terminal failed")

    monkeypatch.setattr(WorkspaceFileIndex, "close", close)
    monkeypatch.setattr(module.Application, "run", fail)
    with create_pipe_input() as pipe, pytest.raises(RuntimeError, match="terminal failed"):
        module.run_tui(
            TuiState(model_name="test"), input=pipe, output=DummyOutput(), workspace_root=tmp_path
        )
    assert len(closed) == 1


@pytest.mark.parametrize("invalidate", [None, "edit", "escape"])
def test_tab_retires_obsolete_completion_without_replaying_stale_draft(
    monkeypatch, tmp_path, invalidate
):
    from alysis_code.cli_impl.file_mention_completer import ChatComposerCompleter

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").touch()
    entered, cancelled, cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests = []
    original = ChatComposerCompleter.get_completions_async

    async def complete(self, document, event):
        requests.append((document.text, document.cursor_position, event.completion_requested))
        if document.text.endswith(" then @other") and document.is_cursor_at_the_end:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
                await cleanup.wait()
        async for item in original(self, document, event):
            yield item

    monkeypatch.setattr(ChatComposerCompleter, "get_completions_async", complete)

    async def scenario(app, pipe, calls):
        buffer = app.current_buffer
        pipe.send_text("explain @src/m")
        await eventually(lambda: buffer.complete_state and buffer.complete_state.completions)
        pipe.send_text("\x1b[B")
        await eventually(lambda: buffer.text == "explain @src/main.py")
        escape(app, pipe)
        assert buffer.text == "explain @src/m"
        pipe.send_text(" then @other")
        await eventually(entered.is_set)
        buffer.cursor_position = len("explain @src/m")
        pipe.send_text("\t")
        await eventually(cancelled.is_set)
        if invalidate == "edit":
            buffer.insert_text("issing")
        elif invalidate == "escape":
            escape(app, pipe)
        cleanup.set()
        if invalidate is None:
            await eventually(lambda: buffer.complete_state and buffer.complete_state.completions)
            pipe.send_text("\t")
            await eventually(lambda: buffer.complete_state is None)
            assert buffer.text == "explain @src/main.py then @other"
        else:
            # Drain the cancelled task and one UI callback deterministically.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not any(explicit for _, _, explicit in requests)
            assert buffer.text == (
                "explain @src/missing then @other"
                if invalidate == "edit"
                else "explain @src/m then @other"
            )
        assert calls == []

    try:
        drive(monkeypatch, tmp_path, scenario)
    finally:
        cleanup.set()


def test_tab_before_automatic_job_starts_preserves_opaque_paste(monkeypatch, tmp_path):
    from prompt_toolkit.key_binding import KeyPress
    from prompt_toolkit.keys import Keys

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").touch()
    payload = "opaque @not-a-completion\n" * 40

    async def scenario(app, pipe, calls):
        buffer = app.current_buffer
        pipe.send_text("\x1b[200~" + payload + "\x1b[201~")
        await eventually(lambda: bool(buffer.text) and payload not in buffer.text)
        prefix = buffer.text
        # No event-loop yield: automatic completion is scheduled but has not
        # entered its tracked coroutine when explicit Tab arrives.
        buffer.insert_text(" explain @src/m then @other")
        buffer.cursor_position = len(prefix + " explain @src/m")
        app.key_processor.feed_multiple([KeyPress(Keys.ControlI, "\t")])
        app.key_processor.process_keys()
        await eventually(lambda: buffer.complete_state and buffer.complete_state.completions)
        pipe.send_text("\t")
        await eventually(lambda: buffer.complete_state is None)
        assert buffer.text == prefix + " explain @src/main.py then @other"
        pipe.send_text("\r")
        await eventually(lambda: bool(calls))
        assert payload in calls[0]
        assert calls[0].endswith(" explain @src/main.py then @other")

    drive(monkeypatch, tmp_path, scenario)
