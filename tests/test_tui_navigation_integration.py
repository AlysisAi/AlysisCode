from __future__ import annotations

import asyncio
import importlib
import json
import threading
import time

import pytest
from prompt_toolkit.application import Application as BaseApplication
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from alysis_code.cli_impl.tui.navigation_ui import SessionSearchModal, TranscriptSearchModal
from alysis_code.cli_impl.tui.session_navigation import SessionPickerProvider, SessionPickerResult
from alysis_code.cli_impl.tui.state import TuiState


async def eventually(predicate):
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Navigation did not reach expected state")


def escape(app, pipe):
    pipe.send_text("\x1b")
    app.key_processor.feed_multiple(pipe.flush_keys())
    app.key_processor.process_keys()


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("alysis_code.session_store.local_session_owner", lambda: "local")


def log(sessions, workspace, name, text="Find Ελληνικά Straße", *, legacy=False, owner="local"):
    sessions.mkdir(exist_ok=True)
    path = sessions / f"{name}.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "user_message",
                "owner": owner,
                "git_root" if legacy else "workspace_root": str(workspace),
                "payload": {"content": text},
            }
        )
        + "\n{malformed\n[]\n",
        encoding="utf-8",
    )
    return path


def drive(monkeypatch, root, scenario, **kwargs):
    module = importlib.import_module("alysis_code.cli_impl.tui.app")
    modals, errors, calls = [], [], []
    surface_box = []

    class Session:
        def run_turn(self, text, **kw):
            calls.append(text)

    def builder(surface):
        surface_box.append(surface)
        return Session()

    def session_modal(*args, **kw):
        value = SessionSearchModal(*args, **kw)
        modals.append(value)
        return value

    def transcript_modal(*args, **kw):
        value = TranscriptSearchModal(*args, **kw)
        modals.append(value)
        return value

    with create_pipe_input() as pipe:

        class Application(BaseApplication):
            def run(self, *, pre_run):
                async def task():
                    try:
                        await scenario(self, pipe, modals, calls, surface_box)
                    except BaseException as exc:
                        exc.add_note(
                            f"modals={[m.status for m in modals]}; focus={self.layout.current_control!r}"
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
        monkeypatch.setattr(module, "SessionSearchModal", session_modal)
        monkeypatch.setattr(module, "TranscriptSearchModal", transcript_modal)
        _, entries = module.run_tui(
            TuiState(model_name="test"),
            input=pipe,
            output=DummyOutput(),
            owl_color=False,
            workspace_root=root,
            session_builder=kwargs.pop("session_builder", builder),
            background_turns=kwargs.pop("background_turns", False),
            **kwargs,
        )
    if errors:
        raise errors[0]
    assert all(not m.is_open for m in modals)
    return entries


def test_session_search_rename_resume_preserves_command_and_resolves_payloads(
    monkeypatch, tmp_path
):
    sessions, root = tmp_path / "sessions", tmp_path / "root"
    root.mkdir()
    new_root = root / "nested"
    new_root.mkdir()
    old_file = root / "keep.txt"
    old_file.write_text("old")
    (new_root / "new.txt").write_text("new")
    path = log(sessions, root, "target", legacy=True)
    log(sessions, tmp_path / "elsewhere", "other")
    log(sessions, root, "foreign", owner="other")
    provider = SessionPickerProvider(sessions, root, root)
    active = [root]
    selected = []
    clipboard_roots = []

    def paste_image(*, root, output_path):
        clipboard_roots.append(root)
        return root / "new.txt"

    monkeypatch.setattr("alysis_code.cli_impl.tui.app.paste_clipboard_image", paste_image)

    def resume(value):
        selected.append(value)
        if len(selected) == 1:
            return False
        active[0] = new_root
        return True

    payload = "[File #1]\n" + "\n".join(f"opaque {i}" for i in range(20))

    async def scenario(app, pipe, modals, calls, surface):
        composer = app.current_buffer
        composer.text = "/resume"
        invocation = composer.document
        pipe.send_text("\r")
        await eventually(lambda: modals and modals[-1].rows)
        modal = modals[-1]
        assert [r.session_id for r in modal.rows] == ["target"]
        pipe.send_text("missing")
        await eventually(lambda: modal.status.startswith("No matching"))
        pipe.send_text("\x15Ελληνικά")
        await eventually(lambda: modal.rows and modal.query.text == "Ελληνικά")
        pipe.send_text("\x1bOQ")
        await eventually(lambda: modal.renaming)
        pipe.send_text("\x15Σχέδιο updated\r")
        await eventually(
            lambda: not modal.renaming and modal.rows and modal.rows[0].title == "Σχέδιο updated"
        )
        assert (
            json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))["custom_name"]
            == "Σχέδιο updated"
        )
        pipe.send_text("\r")
        await eventually(lambda: modal.status.startswith("Resume failed"))
        assert composer.document == invocation and active[0] == root
        pipe.send_text("\r")
        await eventually(lambda: not modal.is_open)
        assert composer.document == invocation and active[0] == new_root
        composer.text = ""
        pipe.send_text(f"\x1b[200~{old_file}\x1b[201~")
        await eventually(lambda: "[File #1]" in composer.text)
        pipe.send_text(f"\x1b[200~{payload}\x1b[201~ draft")
        await eventually(lambda: "draft" in composer.text)
        composer.cursor_position = len(composer.text)
        pipe.send_text("\x16")
        await eventually(lambda: "[File #2]" in composer.text)
        pipe.send_text("\r")
        await eventually(lambda: bool(calls))
        assert str(old_file.resolve()) in calls[0]
        assert str((new_root / "new.txt").resolve()) in calls[0]
        assert payload in calls[0]
        assert clipboard_roots == [new_root]
        pipe.send_text("/image new.txt\r")
        await eventually(lambda: "[File #3]" in composer.text)
        pipe.send_text("\r")
        await eventually(lambda: len(calls) == 2)
        assert str((new_root / "new.txt").resolve()) in calls[1]

    drive(
        monkeypatch,
        root,
        scenario,
        session_search_provider=lambda: provider,
        resume_session=resume,
        workdir_provider=lambda: active[0],
    )
    assert selected == ["target", "target"]


def test_picker_rechecks_owner_and_cancel_without_model(monkeypatch, tmp_path):
    path = log(tmp_path / "sessions", tmp_path, "target")
    provider = SessionPickerProvider(tmp_path / "sessions", tmp_path)
    selected = []

    async def scenario(app, pipe, modals, calls, surface):
        composer = app.current_buffer
        pipe.send_text("/resume\r")
        await eventually(lambda: modals and modals[-1].rows)
        saved = composer.document
        path.write_text(
            path.read_text(encoding="utf-8").replace("local", "foreign"), encoding="utf-8"
        )
        pipe.send_text("\r")
        await eventually(lambda: modals[-1].status.startswith("Conversation unavailable"))
        escape(app, pipe)
        await eventually(lambda: not modals[-1].is_open)
        assert composer.document == saved
        assert not calls and not selected

    drive(
        monkeypatch,
        tmp_path,
        scenario,
        session_search_provider=lambda: provider,
        resume_session=lambda id: selected.append(id),
    )


def test_transcript_find_unicode_multiline_repeated_copy_and_draft(monkeypatch, tmp_path):
    module = importlib.import_module("alysis_code.cli_impl.tui.app")
    copied = []
    monkeypatch.setattr(module, "copy_text_to_clipboard", copied.append)

    async def scenario(app, pipe, modals, calls, surface):
        surface[0].replace_history(
            [
                {"role": "user", "content": "question"},
                {"role": "assistant", "content": "**Straße**\nΕλληνικά\nStraße\nΕλληνικά"},
            ]
        )
        composer = app.current_buffer
        pipe.send_text("/find")
        await eventually(lambda: composer.text == "/find")
        composer.cursor_position = 5
        saved = composer.document
        pipe.send_text("\r")
        await eventually(lambda: bool(modals))
        modal = modals[-1]
        pipe.send_text("STRASSE")
        await eventually(lambda: len(modal.navigator.matches) == 2)
        assert "Match 1/2" in modal.status
        pipe.send_text("\r")
        await eventually(lambda: "Match 2/2" in modal.status)
        assert modal.details.buffer.document.selection_range() == (20, 26)
        pipe.send_text("\x10")
        await eventually(lambda: "Match 1/2" in modal.status)
        pipe.send_text("\x1bOR")
        await eventually(lambda: "Match 2/2" in modal.status)
        # Repeated multiline queries use source offsets, including Unicode expansion.
        pipe.send_text("\x15Straße\nΕλληνικά")
        await eventually(lambda: modal.query.text == "Straße\nΕλληνικά")
        assert len(modal.navigator.matches) == 1
        escape(app, pipe)
        await eventually(lambda: not modal.is_open)
        assert composer.document == saved
        composer.text = "/copy-last"
        composer.cursor_position = 4
        saved = composer.document
        pipe.send_text("\r")
        await eventually(lambda: bool(copied))
        assert copied == ["**Straße**\nΕλληνικά\nStraße\nΕλληνικά"]
        assert composer.document == saved
        assert not calls

    drive(monkeypatch, tmp_path, scenario)


def test_background_search_coalesces_queries_and_discards_closed_result(monkeypatch, tmp_path):
    started, release = threading.Event(), threading.Event()
    queries = []

    class Provider:
        def search(self, query):
            queries.append(query)
            started.set()
            release.wait(10)
            return SessionPickerResult((), True)

    async def scenario(app, pipe, modals, calls, surface):
        pipe.send_text("/resume\r")
        await eventually(started.is_set)
        pipe.send_text("abcdef")
        await eventually(lambda: modals[-1].query.text == "abcdef")
        assert queries == [""]
        escape(app, pipe)
        await eventually(lambda: not modals[-1].is_open)
        release.set()
        await asyncio.sleep(0.2)
        assert queries == [""]

    try:
        drive(
            monkeypatch,
            tmp_path,
            scenario,
            session_search_provider=Provider,
            resume_session=lambda id: True,
        )
    finally:
        release.set()


def test_review_rejects_open_during_idle_gap_before_queue_drain(monkeypatch, tmp_path):
    module = importlib.import_module("alysis_code.cli_impl.tui.app")
    from alysis_code.cli_impl.tui.review import ReviewModal

    release = threading.Event()
    calls, modals, held = [], [], []

    class Session:
        def run_turn(self, text, **kwargs):
            calls.append(text)
            if text == "first":
                release.wait(10)

    def modal(*args, **kwargs):
        value = ReviewModal(*args, **kwargs)
        modals.append(value)
        return value

    monkeypatch.setattr(module, "ReviewModal", modal)

    async def scenario(app, pipe, nav, c, surface):
        loop = asyncio.get_running_loop()
        original = loop.call_soon_threadsafe

        intercepted = False

        def closes_over_completion(callback, depth=0):
            if getattr(callback, "__name__", "") == "finish_on_ui":
                return True
            if depth >= 3:
                return False
            return any(
                closes_over_completion(cell.cell_contents, depth + 1)
                for cell in getattr(callback, "__closure__", ()) or ()
                if callable(cell.cell_contents)
            )

        def schedule(callback, *args, **kwargs):
            nonlocal intercepted
            # Completion is posted through the captured-owner/context wrapper.
            # Hold the first publication, not the actual worker's cleanup.
            if not intercepted and closes_over_completion(callback):
                intercepted = True
                held.append(lambda: callback(*args))
                return None
            return original(callback, *args, **kwargs)

        monkeypatch.setattr(loop, "call_soon_threadsafe", schedule)
        composer = app.current_buffer
        pipe.send_text("first\r")
        await eventually(lambda: calls == ["first"])
        pipe.send_text("second")
        await eventually(lambda: composer.text == "second")
        pipe.send_text("\x11")
        await eventually(lambda: composer.text == "")
        release.set()
        await eventually(lambda: bool(held))
        pipe.send_text("/review\r")
        await eventually(lambda: composer.text == "/review")
        # The worker has returned, but busy remains true until publication
        # clears it and drains queued work atomically on the UI thread.
        pipe.send_text(" ")
        await eventually(lambda: composer.text == "/review ")
        assert not modals
        assert calls == ["first"], "queued work started before UI completion"
        held.pop(0)()
        await eventually(lambda: calls == ["first", "second"])

    try:
        entries = drive(
            monkeypatch,
            tmp_path,
            scenario,
            session_builder=lambda _: Session(),
            background_turns=True,
            review_engine_factory=lambda root: lambda token: None,
        )
    finally:
        release.set()
    assert any("active turn" in text for _, text in entries)


def test_review_start_rechecks_live_eligibility(monkeypatch, tmp_path):
    module = importlib.import_module("alysis_code.cli_impl.tui.app")
    from alysis_code.cli_impl.tui.review import ReviewModal

    modals = []

    def modal(*args, **kwargs):
        value = ReviewModal(*args, **kwargs)
        modals.append(value)
        return value

    monkeypatch.setattr(module, "ReviewModal", modal)
    made = []

    async def scenario(app, pipe, nav, calls, surface):
        pipe.send_text("/review\r")
        await eventually(lambda: bool(modals))
        modal = modals[-1]
        # Simulate work arriving after scope selection and before explicit Start.
        # This is the live callback supplied by run_tui, not a fake eligibility test.
        cells = dict(
            zip(
                modal.eligible.__code__.co_freevars,
                (c.cell_contents for c in modal.eligible.__closure__),
                strict=True,
            )
        )
        cells["pending_turns"].append(module._PendingMessage(text="queued", display_text="queued"))
        # Exercise the real queued-message renderer before the eligibility check.
        app._redraw()
        app.layout.focus(modal.start_button)
        pipe.send_text("\r")
        await eventually(lambda: "queued work" in modal.controller.status)
        assert not made and not modal.controller.running
        cells["pending_turns"].clear()

    drive(
        monkeypatch,
        tmp_path,
        scenario,
        review_engine_factory=lambda root: lambda token: made.append(token),
    )


def test_search_older_content_beyond_first_page_and_visible_bounds(monkeypatch, tmp_path):
    import os

    sessions = tmp_path / "sessions"
    oldest = log(sessions, tmp_path, "oldest", "unique-content Ελληνικά")
    os.utime(oldest, (1, 1))
    for i in range(51):
        log(sessions, tmp_path, str(i))
    provider = SessionPickerProvider(sessions, tmp_path)

    async def scenario(app, pipe, modals, calls, surface):
        pipe.send_text("/resume\r")
        await eventually(lambda: modals and len(modals[-1].rows) == 50)
        modal = modals[-1]
        assert "limited" in modal.status
        assert "oldest" not in [r.session_id for r in modal.rows]
        pipe.send_text("unique-content")
        await eventually(lambda: len(modal.rows) == 1 and modal.rows[0].session_id == "oldest")
        assert "unique-content" in modal.rows[0].title
        escape(app, pipe)

    drive(
        monkeypatch,
        tmp_path,
        scenario,
        session_search_provider=lambda: provider,
        resume_session=lambda id: True,
    )


def test_direct_resume_and_copy_failure_are_local_and_preserve_composer(monkeypatch, tmp_path):
    module = importlib.import_module("alysis_code.cli_impl.tui.app")
    from alysis_code.clipboard import ClipboardError

    routed = []

    def runner(session, text, width):
        routed.append(text)
        return "run", "", "must not reach model", {}

    def unavailable(text):
        raise ClipboardError("unavailable")

    monkeypatch.setattr(module, "copy_text_to_clipboard", unavailable)

    async def scenario(app, pipe, modals, calls, surface):
        surface[0].replace_history([{"role": "assistant", "content": "answer"}])
        composer = app.current_buffer
        pipe.send_text("/resume missing\r")
        await eventually(lambda: bool(routed))
        assert composer.text == "/resume missing"
        assert not calls
        composer.text = "/copy-last"
        composer.cursor_position = 4
        saved = composer.document
        pipe.send_text("\r")
        # Another key confirms dispatch completed and composer focus was restored.
        pipe.send_text("!")
        await eventually(lambda: composer.text == "/cop!y-last")
        assert composer.cursor_position == saved.cursor_position + 1
        assert not calls

    entries = drive(
        monkeypatch,
        tmp_path,
        scenario,
        session_search_provider=lambda: None,
        resume_session=lambda id: False,
        command_runner=runner,
    )
    assert routed == ["/resume missing"]
    assert any("Clipboard unavailable" in text for _, text in entries)


def test_reserved_direct_resume_reports_worker_failure_and_retains_draft(monkeypatch, tmp_path):
    from alysis_code.cli_impl.tui.local_action_lifecycle import TuiLocalActionLifecycle

    lifecycle = TuiLocalActionLifecycle()
    monkeypatch.setattr(lifecycle, "identity_for", lambda session: ("current", str(tmp_path)))

    class Provider:
        def resolve(self, target):
            raise ValueError("Conversation unavailable")

    async def scenario(app, pipe, modals, calls, surface):
        composer = app.current_buffer
        pipe.send_text("/resume missing\r")
        await eventually(lambda: any("Resume failed" in text for _, text in surface[0]._t.entries))
        assert composer.text == "/resume missing"
        assert not calls
        assert not lifecycle.mutating

    drive(
        monkeypatch,
        tmp_path,
        scenario,
        session_search_provider=Provider,
        resume_session=lambda target: True,
        local_lifecycle=lifecycle,
    )
