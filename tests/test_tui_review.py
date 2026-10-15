from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest

from alysis_code.cancellation import CooperativeCancellationError
from alysis_code.cli_impl.tui.review import ReviewModal
from alysis_code.code_review import (
    ChatReviewerClient,
    CodeReviewEngine,
    CodeReviewResult,
    ReviewDiff,
    ReviewFinding,
    ReviewRequest,
    ReviewScope,
    ReviewSummary,
)
from alysis_code.review_service import (
    ReviewCancellationToken,
    ReviewController,
    inspect_finding,
    make_request,
)

PATCH = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return 2"
FINDING = ReviewFinding(
    "high", "Wrong value", "Wrong return", "app.py", 2, 2, "return 2", "Return 1", "high"
)


def result(findings=(FINDING,), patch=PATCH):
    return CodeReviewResult(
        ReviewScope.WORKING_TREE,
        findings,
        ReviewSummary("comment", "Reviewed.", {}, 1, 1, 0, False, ()),
        ReviewDiff(
            ReviewScope.WORKING_TREE, patch, ("app.py",), ("app.py",), (), False, (), {}, len(patch)
        ),
    )


@pytest.mark.parametrize(
    ("scope", "kwargs", "expected"),
    [
        ("working_tree", {}, ReviewRequest.working_tree()),
        (
            "branch",
            {"base": "main", "head": "topic"},
            ReviewRequest.branch(base="main", head="topic"),
        ),
        ("commit", {"revision": "HEAD~2"}, ReviewRequest.commit("HEAD~2")),
        ("range", {"base": "v1", "head": "v2"}, ReviewRequest.revision_range(base="v1", head="v2")),
    ],
)
def test_scope_request_sent_to_backend(scope, kwargs, expected):
    async def run():
        seen = []

        class Engine:
            def review(self, request, *, cancellation_token):
                seen.append(request)
                return result()

        controller = ReviewController(lambda token: Engine(), on_change=lambda: None)
        await controller.start(make_request(scope, **kwargs))
        assert seen == [expected]
        assert controller.result.findings == (FINDING,)
        assert not controller.running

    asyncio.run(run())


def test_cancel_signals_provider_discards_late_result_serializes_and_disposes():
    async def run():
        entered, release = threading.Event(), threading.Event()
        tokens, disposed, calls = [], [], []

        class Engine:
            def review(self, request, *, cancellation_token):
                calls.append(request)
                entered.set()
                assert release.wait(3)
                return result()

        def factory(token):
            tokens.append(token)
            return Engine()

        controller = ReviewController(
            factory, on_change=lambda: None, dispose_engine=disposed.append
        )
        first = controller.start(ReviewRequest.working_tree())
        assert await asyncio.to_thread(entered.wait, 2)
        controller.cancel()
        assert tokens[0].is_cancelled
        assert not controller.running
        assert controller.result is None
        assert not disposed
        second = controller.start(ReviewRequest.commit("HEAD"))
        await asyncio.sleep(0.03)
        assert len(calls) == 1
        release.set()
        await asyncio.gather(first, second)
        assert len(disposed) == 2
        assert len(calls) == 2
        assert controller.result is not None

    asyncio.run(run())


def test_cancel_without_restart_never_accepts_result():
    async def run():
        entered, release = threading.Event(), threading.Event()

        class Engine:
            def review(self, request, **kwargs):
                entered.set()
                release.wait(3)
                return result()

        controller = ReviewController(lambda token: Engine(), on_change=lambda: None)
        task = controller.start(ReviewRequest.working_tree())
        assert await asyncio.to_thread(entered.wait, 2)
        controller.cancel()
        release.set()
        await task
        await asyncio.sleep(0.03)
        assert controller.result is None
        assert "Cancellation requested" in controller.status

    asyncio.run(run())


def test_failure_and_empty_results_are_distinct():
    async def run():
        class Engine:
            def review(self, request, **kwargs):
                raise RuntimeError("secret should not be displayed")

        controller = ReviewController(lambda token: Engine(), on_change=lambda: None)
        await controller.start(ReviewRequest.working_tree())
        assert "failed (RuntimeError)" in controller.status
        assert "secret" not in controller.status
        assert controller.result is None
        Engine.review = lambda *a, **kw: result(findings=())
        await controller.start(ReviewRequest.working_tree())
        assert "0 findings" in controller.status
        assert controller.result is not None

    asyncio.run(run())


def test_exact_snapshot_location_and_unavailable_ambiguous_lines():
    location = inspect_finding(result(), FINDING)
    assert location.text.splitlines()[location.row] == "+    return 2"
    assert "snapshot" in location.notice
    assert inspect_finding(result(), replace(FINDING, line_start=200)).row is None
    assert "unavailable" in inspect_finding(result(), replace(FINDING, line_start=200)).notice
    assert "Ambiguous" in inspect_finding(result(patch=PATCH + "\n" + PATCH), FINDING).notice
    assert "unavailable" in inspect_finding(result(), replace(FINDING, path="missing.py")).notice
    assert "No line" in inspect_finding(result(), replace(FINDING, line_start=None)).notice


def test_deleted_file_and_renamed_path_location():
    patch = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-def value():\n-    return 1"
    location = inspect_finding(result(patch=patch), FINDING)
    assert location.text.splitlines()[location.row] == "-    return 1"
    renamed = PATCH.replace("a/app.py", "a/old.py")
    assert inspect_finding(result(patch=renamed), FINDING).row is not None


def test_modal_scope_findings_errors_close(tmp_path):
    closed = []
    modal = ReviewModal(
        tmp_path,
        engine_factory=lambda token: None,
        on_close=lambda: closed.append(True),
        invalidate=lambda: None,
    )
    modal.scope.current_value = "branch"
    modal.start()
    assert "Invalid scope" in modal.controller.status
    modal.base.text = "main"
    assert modal.request() == ReviewRequest.branch(base="main")
    assert "main" in modal._scope_text()
    modal.controller.result = result()
    modal._changed()
    assert "app.py:2-2" in modal.details.text
    assert "Wrong return" in modal.details.text
    assert "diff --git" not in modal.details.text
    modal.detail_mode = "evidence"
    modal.inspect_selected()
    assert "+    return 2" in modal.details.text
    modal.controller.result = result(findings=())
    modal._changed()
    assert "No findings" in modal._finding_text()[0][1]
    assert "reviewed 1/1" in modal._coverage_status()
    modal.close()
    modal.close()
    assert closed == [True]


def test_provider_receives_token_and_does_not_retry_after_cancellation():
    token = ReviewCancellationToken()
    seen = []

    class Client:
        def chat(self, **kwargs):
            seen.append(kwargs["cancellation_token"])
            token.cancel_nonblocking()
            from alysis_code.llm.types import LLMError

            raise LLMError("response_format unsupported")

    with pytest.raises(CooperativeCancellationError):
        ChatReviewerClient(Client(), cancellation_token=token).review(
            system_prompt="", user_prompt="", response_schema={}
        )
    assert seen == [token]


def test_engine_pre_cancel_does_not_collect():
    token = ReviewCancellationToken()
    token.cancel_nonblocking()
    engine = object.__new__(CodeReviewEngine)
    with pytest.raises(CooperativeCancellationError):
        engine.review(ReviewRequest.working_tree(), cancellation_token=token)


def test_real_engine_all_scopes_preserve_files_and_index(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True
        ).stdout

    git("init", "-q")
    git("config", "user.name", "Review Test")
    git("config", "user.email", "review@example.test")
    target = repo / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    git("add", "app.py")
    git("commit", "-qm", "seed")
    base = git("rev-parse", "HEAD").decode().strip()
    target.write_text("value = 2\n", encoding="utf-8")
    git("commit", "-qam", "change")
    target.write_text("value = 3\n", encoding="utf-8")
    git("add", "app.py")
    target.write_text("value = 4\n", encoding="utf-8")
    (repo / "new.py").write_text("new = True\n", encoding="utf-8")
    before = {p: p.read_bytes() for p in (target, repo / "new.py", repo / ".git" / "index")}

    class Reviewer:
        def review(self, **kwargs):
            return {"verdict": "approve", "overview": "No defects.", "findings": []}

    async def run():
        controller = ReviewController(
            lambda token: CodeReviewEngine(repo, Reviewer()), on_change=lambda: None
        )
        for request in (
            ReviewRequest.working_tree(),
            ReviewRequest.branch(base=base),
            ReviewRequest.commit("HEAD"),
            ReviewRequest.revision_range(base=base, head="HEAD"),
        ):
            await controller.start(request)
            assert controller.result is not None, controller.status
            assert controller.result.scope == request.scope
            assert {p: p.read_bytes() for p in before} == before

    asyncio.run(run())


def test_modal_keyboard_scope_and_close(tmp_path):
    from prompt_toolkit.application import Application
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.output import DummyOutput

    async def run():
        with create_pipe_input() as pipe:
            modal = ReviewModal(
                tmp_path,
                engine_factory=lambda token: None,
                on_close=lambda: app.exit(),
                invalidate=lambda: app.invalidate(),
            )
            app = Application(
                layout=Layout(modal.container, focused_element=modal.focus_target),
                input=pipe,
                output=DummyOutput(),
            )
            task = asyncio.create_task(app.run_async())
            await asyncio.sleep(0.05)
            pipe.send_text("\x1b[B ")
            await asyncio.sleep(0.05)
            assert modal.scope.current_value == "branch"
            pipe.send_text("\x1b")
            await asyncio.wait_for(task, 2)
            assert not modal.is_open

    asyncio.run(run())


@pytest.mark.parametrize("columns,rows", [(80, 24), (50, 18)])
def test_review_compact_render_and_keyboard(tmp_path, columns, rows):

    from prompt_toolkit.application import Application
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.widgets import Frame

    class Output(DummyOutput):
        def get_size(self):
            return Size(rows=rows, columns=columns)

    async def run():
        with create_pipe_input() as pipe:
            modal = ReviewModal(
                tmp_path,
                engine_factory=lambda token: None,
                on_close=lambda: app.exit(),
                invalidate=lambda: app.invalidate(),
            )
            app = Application(
                layout=Layout(Frame(modal.container, title="Review"), modal.focus_target),
                input=pipe,
                output=Output(),
                full_screen=True,
            )
            task = asyncio.create_task(app.run_async())

            async def key(text):
                pipe.send_text(text)
                await asyncio.sleep(0.06)

            def capture(name):
                app._redraw()
                screen = app.renderer._last_screen
                text = "\n".join(
                    "".join(screen.data_buffer[y][x].char for x in range(columns)).rstrip()
                    for y in range(rows)
                )
                folder = tmp_path / "review-captures"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"{name}-{columns}x{rows}.txt").write_text(text, encoding="utf-8")
                assert "Window too small" not in text
                return text

            await asyncio.sleep(0.06)
            setup = capture("setup")
            assert "< Start review >" in setup
            assert "Esc close" in setup
            assert modal.container.children[0].render_info.window_width == columns - 2
            assert modal.container.children[-1].render_info.window_width == columns - 2
            assert all(len(line) == columns for line in setup.splitlines() if line)
            assert modal.controller.result is None
            await key("\x1b[B ")
            app.layout.focus(modal.base)
            await key("feature?branch")
            assert modal.base.text == "feature?branch" and not modal.show_help
            snapshot = result(findings=(replace(FINDING, explanation="Explanation " * 60),))
            modal.controller.result = replace(
                snapshot,
                summary=replace(
                    snapshot.summary,
                    changed_file_count=3,
                    omitted_file_count=2,
                    warnings=("Input was capped.",),
                ),
            )
            modal._changed()
            await asyncio.sleep(0.06)
            view = capture("finding")
            assert "INCOMPLETE" in view and "warning(s)" in view
            assert "Wrong value" in view and "Start review" not in view
            assert "diff --git" not in view and "Evidence:" not in view
            await key("\r")
            assert modal.detail_mode == "evidence" and app.layout.has_focus(modal.details)
            await key("\x1b[F")
            assert modal.details.buffer.cursor_position == len(modal.details.text)
            assert "+    return 2" in capture("evidence-end")
            await key("c")
            assert "Input was capped." in modal.details.text
            assert "INCOMPLETE" in capture("coverage")
            await key("b?")
            assert modal.detail_mode == "summary" and modal.show_help
            capture("help")
            await key("?n")
            assert modal.setup and app.layout.has_focus(modal.scope)
            assert modal.controller.result is not None
            app.exit()
            await task

    asyncio.run(run())
