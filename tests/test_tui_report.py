from __future__ import annotations

import threading
import time
import zipfile
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from alysis_code import feedback_report as reports
from alysis_code.cli_impl.tui import app as app_module
from alysis_code.cli_impl.tui.report_flow import ReportFlow
from alysis_code.cli_impl.tui.state import TuiState
from alysis_code.config import AppConfig


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("Tests must use an injected browser opener")

    monkeypatch.setattr(reports, "open_url", forbidden)
    yield
    assert attempts == []


def _session(root):
    return SimpleNamespace(
        root=root,
        mode="review",
        cfg=AppConfig(),
        messages=[{"role": "user", "content": "Original conversation"}],
        store=SimpleNamespace(enabled=False, path=None, session_id="tui_report"),
    )


def _finish(flow):
    flow.perform_busy()
    assert flow.stage == "working"  # worker never mutates renderer-visible state
    flow.apply_busy_outcome()


def test_report_flow_save_preview_browser_failure_copy_and_retry(tmp_path):
    opened, copied = [], []
    accept = False

    def browser(url, **_kwargs):
        opened.append(url)
        return accept

    flow = ReportFlow(
        workspace_root=tmp_path,
        cfg=AppConfig(),
        active_session=_session(tmp_path),
        browser_opener=browser,
        copier=copied.append,
    )
    flow.submit_input("Unexpected error with password=synthetic123")
    assert "synthetic123" not in flow.screen().subtitle
    flow.choose("save")
    _finish(flow)
    assert flow.stage == "complete" and flow.result.zip_path.exists()
    assert opened == []
    flow.choose("copy_path")
    assert flow.screen().busy_label == "Copying archive path"
    _finish(flow)
    assert copied == [str(flow.result.zip_path)]
    flow.choose("prepare_draft")
    _finish(flow)
    assert opened == [] and flow.stage == "draft"
    assert flow.screen().confirm_default is True
    flow.confirm(False)
    assert flow.stage == "complete" and opened == []
    flow.choose("prepare_draft")
    _finish(flow)
    flow.confirm(True)
    _finish(flow)
    assert flow.stage == "draft" and "Browser could not open" in flow.status
    flow.choose("copy_url")
    _finish(flow)
    assert copied[-1] == opened[0]
    assert "synthetic123" not in parse_qs(urlsplit(copied[-1]).query)["body"][0]
    accept = True
    flow.confirm(True)
    _finish(flow)
    assert flow.stage == "complete" and "opened" in flow.status


def test_report_failure_can_retry_without_losing_description(tmp_path):
    attempts = 0

    def exporter(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("cannot write password=synthetic123")
        return reports.create_feedback_bundle(**kwargs)

    flow = ReportFlow(
        workspace_root=tmp_path,
        cfg=AppConfig(feedback_github_enabled=False),
        active_session=_session(tmp_path),
        feedback_text="repro steps",
        exporter=exporter,
    )
    flow.choose("save")
    _finish(flow)
    assert flow.stage == "review" and flow.feedback == "repro steps"
    assert "synthetic123" not in flow.status
    flow.choose("save")
    _finish(flow)
    assert flow.saved
    assert "prepare_draft" not in [row.value for row in flow.screen().rows]


def test_report_without_live_session_does_not_export_retained_session(tmp_path, monkeypatch):
    monkeypatch.setattr(
        reports,
        "list_sessions",
        lambda *args: pytest.fail("Unrelated sessions must not be selected"),
    )
    flow = ReportFlow(workspace_root=tmp_path, cfg=AppConfig(), feedback_text="Cannot connect")
    flow.choose("save")
    _finish(flow)
    assert flow.saved and flow.result.session_id is None


def test_browser_receives_exact_preview_even_if_bundle_changes(tmp_path):
    opened = []
    flow = ReportFlow(
        workspace_root=tmp_path,
        cfg=AppConfig(),
        feedback_text="repro",
        browser_opener=lambda url, **kwargs: opened.append(url) or True,
    )
    flow.choose("save")
    _finish(flow)
    flow.choose("prepare_draft")
    _finish(flow)
    preview_url = flow.issue.issue_url
    (flow.result.bundle_dir / "session_score.json").write_text(
        '{"last_verification_failure_kind": "changed-after-preview"}'
    )
    flow.confirm(True)
    _finish(flow)
    assert opened == [preview_url]


def _wait(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Timed out waiting for report UI state")
        time.sleep(0.01)


def _run_ui(
    tmp_path,
    monkeypatch,
    driver,
    *,
    columns=80,
    rows=24,
    connected=True,
    capture_frames=False,
    **flow_kwargs,
):
    captured: dict[str, Any] = {"flows": [], "commands": [], "errors": []}
    session = _session(tmp_path)

    def application(*args, **kwargs):
        result = Application(*args, **kwargs)
        captured["app"] = result
        if capture_frames:
            captured["frames"] = []

            def rendered(app):
                if captured["flows"]:
                    captured["frames"].append((captured["flows"][-1].stage, _screen_text(captured)))

            result.after_render += rendered
        return result

    monkeypatch.setattr(app_module, "Application", application)

    def factory(live_session, text):
        assert live_session is (session if connected else None)
        flow = ReportFlow(
            workspace_root=tmp_path,
            cfg=AppConfig(),
            active_session=live_session,
            feedback_text=text,
            **flow_kwargs,
        )
        captured["flows"].append(flow)
        return flow

    def runner(live_session, text, width):
        captured["commands"].append(text)
        return ("exit" if text == "/exit" else "handled", "", None, None)

    class Output(DummyOutput):
        def get_size(self):
            return Size(rows=rows, columns=captured.get("columns", columns))

    with create_pipe_input() as pipe:

        def feed():
            try:
                _wait(lambda: captured.get("app") is not None and captured["app"].is_running)
                driver(pipe, captured)
            except BaseException as exc:
                captured["errors"].append(exc)
            finally:
                # Ensure a failed assertion cannot hang the test in Application.run.
                if captured["errors"] and captured.get("app") and captured["app"].loop:
                    captured["app"].loop.call_soon_threadsafe(captured["app"].exit)

        thread = threading.Thread(target=feed, daemon=True)
        thread.start()
        result, transcript = app_module.run_tui(
            TuiState(model_name="test", username="tester"),
            owl_color=False,
            input=pipe,
            output=Output(),
            session_builder=(lambda surface: session) if connected else None,
            command_runner=runner,
            report_flow_factory=factory,
        )
        thread.join(timeout=6)
    if captured["errors"]:
        raise captured["errors"][0]
    assert not thread.is_alive()
    assert session.messages == [{"role": "user", "content": "Original conversation"}]
    return result, transcript, captured


def _screen_text(captured):
    screen = captured["app"].renderer.last_rendered_screen
    if screen is None:
        return ""
    size = captured["app"].output.get_size()
    return "\n".join(
        "".join(screen.data_buffer[y][x].char for x in range(size.columns))
        for y in range(size.rows)
    )


@pytest.mark.parametrize("command", ["/report", "/feedback"])
@pytest.mark.parametrize("columns", [40, 80, 120])
def test_report_keyboard_flow_creates_real_zip_and_preserves_chat(
    tmp_path, monkeypatch, command, columns
):
    def driver(pipe, captured):
        pipe.send_text(command + "\r")
        _wait(lambda: bool(captured["flows"]))
        flow = captured["flows"][0]
        assert flow.stage == "description"
        _wait(lambda: "Description (optional)" in _screen_text(captured))
        pipe.send_text("Actual result\x1b\rExpected result\r")  # Alt+Enter newline; Enter review
        _wait(lambda: flow.stage == "review")
        assert flow.feedback == "Actual result\nExpected result"
        pipe.send_text("3")  # edit description
        _wait(lambda: flow.stage == "description")
        pipe.send_text(" corrected\r")
        _wait(lambda: flow.stage == "review")
        pipe.send_text("4")  # save locally
        _wait(lambda: flow.stage == "complete")
        with zipfile.ZipFile(flow.result.zip_path) as archive:
            assert archive.testzip() is None
            assert b"Expected result corrected" in archive.read("feedback.md")
        pipe.send_text("\r")  # close result
        _wait(lambda: flow.stage == "done")
        pipe.send_text("/exit\r")

    result, transcript, captured = _run_ui(tmp_path, monkeypatch, driver, columns=columns)
    assert result == "/exit"
    assert captured["commands"] == ["/exit"]
    assert any("Feedback bundle archive:" in content for _, content in transcript)
    assert not any("Actual result" in content for _, content in transcript)


def test_report_worker_is_off_ui_thread_and_escape_cancels_export(tmp_path, monkeypatch):
    started = threading.Event()

    def exporter(**kwargs):
        assert threading.current_thread() is not threading.main_thread()
        started.set()
        _wait(kwargs["cancelled"])
        return reports.create_feedback_bundle(**kwargs)

    def driver(pipe, captured):
        pipe.send_text("/report cancellation test\r")
        _wait(lambda: bool(captured["flows"]))
        flow = captured["flows"][0]
        pipe.send_text("4")
        assert started.wait(3)
        assert flow.stage == "working"
        pipe.send_text("\x1b")
        _wait(lambda: flow.stage == "review")
        assert "cancelled" in flow.status.lower()
        assert flow.result is None
        pipe.send_text("\x03")  # Ctrl+C closes report, not chat
        _wait(lambda: flow.stage == "done")
        pipe.send_text("/exit\r")

    result, _, captured = _run_ui(tmp_path, monkeypatch, driver, exporter=exporter)
    assert result == "/exit" and captured["commands"] == ["/exit"]


def test_report_preview_keyboard_enter_opens_and_escape_returns(tmp_path, monkeypatch):
    opened, copied = [], []

    def driver(pipe, captured):
        pipe.send_text("/report UI error\r")
        _wait(lambda: bool(captured["flows"]))
        flow = captured["flows"][0]
        pipe.send_text("4")
        _wait(lambda: flow.stage == "complete")
        pipe.send_text("3")  # preview
        _wait(lambda: flow.stage == "draft")
        assert opened == []
        pipe.send_text("\x1b[6~\x1b[5~\x1b")  # scroll; Esc returns without opening
        _wait(lambda: flow.stage == "complete")
        assert opened == []
        pipe.send_text("3")
        _wait(lambda: flow.stage == "draft")
        pipe.send_text("\r")
        _wait(lambda: "Browser could not open" in flow.status)
        assert len(opened) == 1
        pipe.send_text("\x19")  # Ctrl+Y
        _wait(lambda: "Draft URL copied" in flow.status)
        assert copied == opened
        assert "\n" not in copied[0]
        pipe.send_text("n")
        _wait(lambda: flow.stage == "complete")
        pipe.send_text("\r")
        _wait(lambda: flow.stage == "done")
        pipe.send_text("/exit\r")

    _run_ui(
        tmp_path,
        monkeypatch,
        driver,
        browser_opener=lambda url, **kw: opened.append(url) or False,
        copier=copied.append,
    )


def test_unconnected_tui_report_saves_and_small_terminal_can_resize(tmp_path, monkeypatch):
    def driver(pipe, captured):
        pipe.send_text("/report setup problem\r")
        _wait(lambda: bool(captured["flows"]))
        flow = captured["flows"][0]
        pipe.send_text("\x1b[B\x1b[B\x1b[B")  # scroll the small terminal to Save locally
        _wait(lambda: flow.index == 3)
        _wait(lambda: "Save locally" in _screen_text(captured))
        captured["columns"] = 80
        captured["app"].invalidate()
        _wait(lambda: "Save locally" in _screen_text(captured))
        pipe.send_text("4")
        _wait(lambda: flow.stage == "complete")
        assert flow.result.session_id is None
        pipe.send_text("\r")
        _wait(lambda: flow.stage == "done")
        pipe.send_text("/exit\r")

    result, _, _ = _run_ui(tmp_path, monkeypatch, driver, columns=40, rows=16, connected=False)
    assert result == "/exit"


def test_application_teardown_waits_for_cancelled_report_worker(tmp_path, monkeypatch):
    started, finished = threading.Event(), threading.Event()

    def exporter(**kwargs):
        started.set()
        try:
            _wait(kwargs["cancelled"])
            return reports.create_feedback_bundle(**kwargs)
        finally:
            finished.set()

    def driver(pipe, captured):
        pipe.send_text("/report teardown test\r")
        _wait(lambda: bool(captured["flows"]))
        pipe.send_text("4")
        assert started.wait(3)
        captured["app"].loop.call_soon_threadsafe(captured["app"].exit)

    _run_ui(tmp_path, monkeypatch, driver, exporter=exporter)
    assert finished.is_set()
    assert not any(t.name == "alysis-report-export" and t.is_alive() for t in threading.enumerate())


def test_keyboard_export_and_clipboard_errors_can_retry(tmp_path, monkeypatch):
    exports, copies = [], []

    def exporter(**kwargs):
        exports.append(kwargs["feedback_text"])
        if len(exports) == 1:
            raise OSError("Temporary write failure")
        return reports.create_feedback_bundle(**kwargs)

    def copier(value):
        copies.append(value)
        if len(copies) == 1:
            raise OSError("Clipboard unavailable")

    def driver(pipe, captured):
        pipe.send_text("/report preserve my description\r")
        _wait(lambda: bool(captured["flows"]))
        flow = captured["flows"][0]
        pipe.send_text("4")
        _wait(lambda: flow.stage == "review" and "failed" in flow.status)
        pipe.send_text("4")
        _wait(lambda: flow.stage == "complete")
        pipe.send_text("2")
        _wait(lambda: flow.stage == "complete" and "Clipboard unavailable" in flow.status)
        pipe.send_text("2")
        _wait(lambda: flow.status == "ZIP path copied.")
        pipe.send_text("\r")
        _wait(lambda: flow.stage == "done")
        pipe.send_text("/exit\r")

    _run_ui(tmp_path, monkeypatch, driver, exporter=exporter, copier=copier)
    assert exports == ["preserve my description", "preserve my description"]
    assert len(copies) == 2 and copies[0] == copies[1]


@pytest.mark.parametrize("columns,rows", [(40, 16), (80, 24), (120, 30)])
def test_result_options_visible_on_first_frame_and_after_scrolled_draft(
    tmp_path, monkeypatch, columns, rows, configured_hosted_api
):
    from alysis_code.feedback_delivery import FeedbackReceipt

    frames = []
    copied = []

    def driver(pipe, captured):
        pipe.send_text("/report " + "A long feedback paragraph. " * 30 + "\r")
        _wait(lambda: bool(captured["flows"]))
        flow = captured["flows"][0]
        pipe.send_text("1")
        _wait(lambda: flow.stage == "confirm_send")
        pipe.send_text("\r")
        _wait(lambda: flow.stage == "complete")
        _wait(lambda: any(stage == "complete" for stage, _ in captured["frames"]))
        frames.append(next(text for stage, text in captured["frames"] if stage == "complete"))
        for label in [
            "Return to chat",
            "Copy report ID",
            "Save a local ZIP",
            "Draft a public GitHub issue",
        ]:
            assert label in frames[-1]
        pipe.send_text("4")  # Export the local ZIP, then review the public draft.
        _wait(lambda: flow.stage == "draft")
        _wait(lambda: "Review GitHub draft" in _screen_text(captured))
        pipe.send_text("\x1b[6~" * 5)
        _wait(lambda: "Review GitHub draft" not in _screen_text(captured))
        frame_start = len(captured["frames"])
        pipe.send_text("\r")
        _wait(lambda: flow.stage == "complete")
        _wait(lambda: any(stage == "complete" for stage, _ in captured["frames"][frame_start:]))
        frames.append(
            next(text for stage, text in captured["frames"][frame_start:] if stage == "complete")
        )
        for label in [
            "Feedback sent to Alysis",
            "Return to chat",
            "Copy ZIP path",
            "Review public GitHub draft",
            "Copy report ID",
        ]:
            assert label in frames[-1]
        assert "Submitittherewhenready." in "".join(_screen_text(captured).split())
        assert str(flow.result.zip_path) not in frames[-1]
        assert flow.receipt.report_id not in frames[-1]
        pipe.send_text("2")
        _wait(lambda: flow.status == "ZIP path copied.")
        assert copied == [str(flow.result.zip_path)]
        pipe.send_text("4")
        _wait(lambda: flow.status == "Report ID copied.")
        assert copied[-1] == flow.receipt.report_id
        pipe.send_text("\x1b")
        _wait(lambda: flow.stage == "done")
        pipe.send_text("/exit\r")

    _run_ui(
        tmp_path,
        monkeypatch,
        driver,
        columns=columns,
        rows=rows,
        capture_frames=True,
        sender=lambda report: FeedbackReceipt(report.report_id, "2026-09-09T12:00:00Z"),
        browser_opener=lambda url, **kwargs: True,
        copier=copied.append,
    )


def test_default_report_browser_uses_quiet_host_launcher(monkeypatch):
    calls = []
    monkeypatch.setattr(
        reports, "open_url", lambda url, **kwargs: calls.append((url, kwargs)) or False
    )
    url = "https://github.com/AlysisAi/AlysisCode/issues/new?title=test&body=synthetic"
    result = reports.open_feedback_github_issue_draft(
        reports.FeedbackGithubIssueResult(
            repo="AlysisAi/AlysisCode", issue_url=url, opened=False, open_attempted=False
        )
    )
    assert calls == [(url, {"quiet": True})]
    assert result.open_attempted and not result.opened
