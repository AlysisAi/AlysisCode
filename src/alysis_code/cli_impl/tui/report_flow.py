"""Report workflow: UI-thread state transitions, worker-thread export and I/O."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ...clipboard import copy_text_to_clipboard
from ...config import AppConfig, resolve_feedback_github_enabled
from ...feedback_delivery import FeedbackReceipt, PreparedFeedback, prepare_feedback, send_feedback
from ...feedback_report import (
    FeedbackBundleResult,
    FeedbackGithubIssueResult,
    FeedbackReportCancelled,
    create_feedback_bundle,
    create_feedback_github_issue_draft,
    open_feedback_github_issue_draft,
    sanitize_feedback_text,
)
from ...forge import RunPaths
from .setup_flow import Mode, Row, Screen


def _feedback_summary(text: str) -> str:
    excerpt = "\n".join(text.splitlines()[:4])[:240]
    return excerpt + "…" if excerpt != text else text


def _diagnostic_lines(payload: dict[str, Any]) -> list[tuple[str, str]]:
    diagnostics = payload["diagnostics"]
    lines = [
        (f"Alysis version: {payload['client_version']}", "plain"),
        (f"Operating system: {diagnostics['os']}", "plain"),
        (f"Python version: {diagnostics['python_version']}", "plain"),
        ("", "dim"),
        ("Recent conversation", "plain"),
    ]
    messages = diagnostics["messages"]
    if not messages:
        lines.append(("No conversation messages included.", "dim"))
    for message in messages:
        lines.append(("", "dim"))
        if isinstance(message, dict):
            if "omitted" in message:
                lines.append((str(message["omitted"]), "dim"))
                continue
            lines.append((str(message.get("role", "Message")).capitalize(), "plain"))
            content = message.get("content", "")
            if isinstance(content, list):
                content = "\n".join(str(part.get("text", "")) for part in content)
            lines.extend((line, "plain") for line in str(content).splitlines())
            for call in message.get("tool_calls", []) or []:
                function = call.get("function", {}) if isinstance(call, dict) else {}
                if not isinstance(function, dict):
                    function = {}
                lines.append((f"Tool: {function.get('name', 'unknown')}", "plain"))
                arguments = function.get("arguments", "")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False, indent=2)
                lines.extend((line, "plain") for line in arguments.splitlines())
        else:
            lines.extend((line, "plain") for line in str(message).splitlines())
    if diagnostics["omitted_messages"]:
        lines.extend(
            [
                ("", "dim"),
                (f"Earlier messages not included: {diagnostics['omitted_messages']}", "dim"),
            ]
        )
    return lines


class ReportFlow:
    def __init__(
        self,
        *,
        workspace_root: Path,
        cfg: AppConfig,
        active_session: Any | None = None,
        active_run_paths: RunPaths | None = None,
        feedback_text: str = "",
        exporter: Callable[..., FeedbackBundleResult] | None = None,
        issue_drafter: Callable[..., FeedbackGithubIssueResult] | None = None,
        copier: Callable[[str], None] | None = None,
        browser_opener: Callable[..., bool] | None = None,
        sender: Callable[[PreparedFeedback], FeedbackReceipt] | None = None,
    ) -> None:
        self.root = workspace_root
        self.cfg = cfg
        # A setup/subscription-blocked TUI has no live agent. Let it report
        # the problem without silently exporting an unrelated retained session.
        self.session = active_session or SimpleNamespace(root=workspace_root, messages=[])
        self.run_paths = active_run_paths
        self.feedback = feedback_text.strip()
        self.stage = "review" if self.feedback else "description"
        self.index = 0
        self.status = ""
        self.saved = False
        self.result: FeedbackBundleResult | None = None
        self.issue: FeedbackGithubIssueResult | None = None
        self.include_diagnostics = False
        self.prepared: PreparedFeedback | None = None
        self.receipt: FeedbackReceipt | None = None
        self._sender = sender or send_feedback
        self._exporter = exporter or create_feedback_bundle
        self._drafter = issue_drafter or create_feedback_github_issue_draft
        self._copier = copier or copy_text_to_clipboard
        self._browser_opener = browser_opener
        self._cancel = Event()
        self._progress: SimpleQueue[str] = SimpleQueue()
        self._busy_label = ""
        self._job = ""
        self._outcome: tuple[str, Any] | None = None

    def current_mode(self) -> Mode:
        if self.stage == "description":
            return "input"
        if self.stage == "working":
            return "busy"
        if self.stage in {"draft", "diagnostics", "feedback_details"}:
            return "confirm"
        if self.stage == "done":
            return "done"
        return "list"

    def field_key(self) -> str:
        return self.stage

    def _breadcrumb(self) -> str:
        return "report"

    def _goto(self, stage: str) -> None:
        self.stage, self.index = stage, 0

    def screen(self) -> Screen:
        scr = Screen(
            stage=self.stage, mode=self.current_mode(), title="Report a problem", status=self.status
        )
        if "failed" in self.status.lower() or "could not" in self.status.lower():
            scr.status_tone = "err"
        if self.stage == "description":
            scr.subtitle = (
                "Describe what happened and what you expected. Nothing is sent from this screen."
                " Alt+Enter adds a new line."
            )
            scr.input_label = "Description (optional)"
            scr.input_default = self.feedback
            scr.hint = "Enter review · Alt+Enter newline · Esc back"
        elif self.stage == "review":
            description = (
                sanitize_feedback_text(self.feedback, workspace_root=self.root)
                or "No description provided."
            )
            scr.subtitle = (
                f"{_feedback_summary(description)}\n\n"
                "Share a bug or suggestion privately with the Alysis team."
            )
            scr.rows = [
                Row("Send to Alysis", "review and send", "prepare_send"),
                Row(
                    f"Include diagnostics: {'On' if self.include_diagnostics else 'Off'}",
                    "toggle recent messages and system information",
                    "toggle_diagnostics",
                ),
                Row("Edit description", "", "edit"),
                Row("Save locally", "full support bundle; nothing uploaded", "save"),
                Row("Cancel", "return to chat", "close"),
            ]
        elif self.stage == "complete":
            scr.title = "Feedback sent to Alysis" if self.receipt else "Report saved locally"
            if self.receipt:
                scr.subtitle = "Your feedback was sent privately."
                if self.result:
                    scr.subtitle += "\nA local ZIP copy is saved on this computer."
            else:
                scr.subtitle = (
                    "Your ZIP is saved on this computer.\nNothing has been sent to Alysis."
                )
            scr.rows = [
                Row("Return to chat", "", "close"),
            ]
            if self.result:
                scr.rows.append(Row("Copy ZIP path", "", "copy_path"))
            if self.result and resolve_feedback_github_enabled(self.cfg):
                scr.rows.append(Row("Review public GitHub draft", "", "prepare_draft"))
            elif not resolve_feedback_github_enabled(self.cfg):
                scr.subtitle += "\nGitHub drafts are disabled in your settings."
            if self.receipt:
                scr.rows.append(Row("Copy report ID", "", "copy_id"))
                if not self.result:
                    scr.rows.append(Row("Save a local ZIP", "", "save"))
                    if resolve_feedback_github_enabled(self.cfg):
                        scr.rows.append(
                            Row(
                                "Draft a public GitHub issue",
                                "",
                                "export_draft",
                            )
                        )
            else:
                scr.rows.append(Row("Send feedback to Alysis", "", "review_send"))
        elif self.stage == "confirm_send" and self.prepared:
            payload = json.loads(self.prepared.body)
            description = payload["description"]
            diagnostics = payload["diagnostics"]
            scr.title = "Send feedback to Alysis"
            included = (
                "Diagnostics included: recent messages and system information."
                if diagnostics is not None
                else "Diagnostics off. Your feedback and app version will be shared."
            )
            scr.subtitle = (
                f"{_feedback_summary(description)}\n\n{included}\n"
                "This will be sent privately to the Alysis team."
            )
            scr.numbered = False
            scr.rows = [Row("Send feedback", "", "send")]
            if _feedback_summary(description) != description:
                scr.rows.append(Row("Read full feedback", "", "feedback_details"))
            if diagnostics is not None:
                scr.rows.append(Row("Review diagnostics", "", "diagnostics"))
            scr.rows.extend([Row("Edit feedback", "", "edit"), Row("Back", "", "back")])
        elif self.stage in {"diagnostics", "feedback_details"} and self.prepared:
            payload = json.loads(self.prepared.body)
            scr.title = "Included diagnostics" if self.stage == "diagnostics" else "Your feedback"
            scr.subtitle = "Review before sharing. Enter returns to the send screen."
            scr.lines = (
                _diagnostic_lines(payload)
                if self.stage == "diagnostics"
                else [(line, "plain") for line in payload["description"].splitlines()]
            )
        elif self.stage == "draft" and self.issue and self.issue.issue_url:
            query = parse_qs(urlsplit(self.issue.issue_url).query)
            scr.title = "Review GitHub draft"
            scr.subtitle = (
                f"Repository: github.com/{self.issue.repo}\n"
                "Opening shares this draft with GitHub. You submit it in the browser. "
                "The ZIP is not uploaded."
            )
            scr.lines = [(query.get("title", [""])[0], "plain"), ("", "dim")]
            scr.lines += [(line, "plain") for line in query.get("body", [""])[0].splitlines()]
            scr.confirm_default = True
        elif self.stage == "working":
            try:
                while True:
                    self._busy_label = self._progress.get_nowait()
            except Empty:
                pass
            scr.busy_label = (
                "Cancelling export…"
                if self._cancel.is_set() and self._job in {"save", "export_draft"}
                else self._busy_label
            )
        scr.index = self.index
        return scr

    def move(self, delta: int) -> None:
        rows = self.screen().rows
        if rows:
            self.index = (self.index + delta) % len(rows)

    def choose_index(self, index: int) -> None:
        rows = self.screen().rows
        if 0 <= index < len(rows):
            self.index = index
            self.choose_current()

    def choose_current(self) -> None:
        rows = self.screen().rows
        if rows and 0 <= self.index < len(rows):
            self.choose(str(rows[self.index].value))

    def choose(self, value: str) -> None:
        if self.current_mode() == "busy":
            return
        if value == "close":
            self._goto("done")
        elif value == "edit" and self.stage in {"review", "confirm_send"}:
            self._goto("description")
        elif value == "send" and self.stage == "confirm_send":
            self.confirm(True)
        elif value == "back" and self.stage == "confirm_send":
            self.back()
        elif value in {"diagnostics", "feedback_details"} and self.stage == "confirm_send":
            if value != "diagnostics" or json.loads(self.prepared.body)["diagnostics"] is not None:
                self._goto(value)
        elif value == "toggle_diagnostics" and self.stage == "review":
            self.include_diagnostics = not self.include_diagnostics
            self.prepared = None
            self.status = ""
        elif value == "prepare_send" and self.stage == "review":
            self._start("prepare_send", "Preparing report preview")
        elif value == "review_send" and self.stage == "complete":
            self._goto("review")
        elif value == "save" and self.stage in {"review", "complete"}:
            self._start("save", "Collecting session diagnostics")
        elif value == "export_draft" and self.stage == "complete":
            self._start("export_draft", "Saving local bundle for GitHub preview")
        elif value == "copy_id" and self.stage == "complete" and self.receipt:
            self._start("copy_id", "Copying report ID")
        elif value in {"copy_path", "prepare_draft"} and self.stage == "complete":
            self._start(
                value,
                "Copying archive path" if value == "copy_path" else "Preparing GitHub preview",
            )
        elif value == "copy_url" and self.stage == "draft":
            self._start(value, "Copying draft URL")

    def submit_input(self, text: str) -> None:
        if self.stage == "description":
            if self.feedback != text.strip():
                self.prepared = None
            self.feedback = text.strip()
            self.status = ""
            self._goto("review")

    def back(self) -> None:
        if self.stage == "working":
            self.request_cancel()
        elif self.stage == "draft":
            self._goto("complete")
        elif self.stage == "confirm_send":
            self._goto("review")
        elif self.stage in {"diagnostics", "feedback_details"}:
            self._goto("confirm_send")
        elif self.stage == "review":
            self._goto("description")
        else:
            self._goto("done")

    def request_cancel(self) -> None:
        if self.stage == "working":
            if self._job in {"save", "export_draft"}:
                self._cancel.set()
        else:
            self._goto("done")

    def confirm(self, value: bool) -> None:
        if self.stage in {"diagnostics", "feedback_details"}:
            self._goto("confirm_send")
            return
        if self.stage == "confirm_send":
            if value:
                self._start("send", "Sending your feedback to Alysis…")
            else:
                self._goto("review")
        if self.stage == "draft":
            if value:
                self._start("open_draft", "Opening GitHub draft")
            else:
                self._goto("complete")

    def _start(self, job: str, label: str) -> None:
        self._job, self._busy_label, self.status = job, label, ""
        self._progress = SimpleQueue()
        self._cancel.clear()
        self._outcome = None
        self._goto("working")

    def perform_busy(self) -> None:
        """Only the worker calls this; it publishes an outcome, not UI state."""
        try:
            if self._job == "prepare_send":
                result = self.prepared or prepare_feedback(
                    workspace_root=self.root,
                    feedback_text=self.feedback,
                    include_diagnostics=self.include_diagnostics,
                    active_session=self.session,
                )
            elif self._job == "send":
                result = self._sender(self.prepared)
            elif self._job in {"save", "export_draft"}:
                result = self._exporter(
                    workspace_root=self.root,
                    cfg=self.cfg,
                    feedback_text=self.feedback,
                    active_session=self.session,
                    active_run_paths=self.run_paths,
                    progress=self._progress.put,
                    cancelled=self._cancel.is_set,
                )
            elif self._job == "prepare_draft":
                result = self._drafter(
                    bundle_result=self.result,
                    feedback_text=self.feedback,
                    cfg=self.cfg,
                    open_browser=False,
                )
            elif self._job == "open_draft":
                result = open_feedback_github_issue_draft(
                    self.issue, browser_open=self._browser_opener
                )
            else:
                value = (
                    self.receipt.report_id
                    if self._job == "copy_id"
                    else str(self.result.zip_path)
                    if self._job == "copy_path"
                    else self.issue.issue_url
                )
                self._copier(value)
                result = None
            self._outcome = ("ok", result)
        except FeedbackReportCancelled:
            self._outcome = ("cancelled", None)
        except Exception as exc:
            self.set_busy_failure(str(exc))

    def set_busy_failure(self, message: str) -> None:
        self._outcome = ("error", sanitize_feedback_text(message, workspace_root=self.root))

    def apply_busy_outcome(self) -> None:
        kind, value = self._outcome or ("error", "The operation did not complete. Please retry.")
        if kind == "cancelled":
            self.status = "Export cancelled. No archive was published."
            self._goto("review")
        elif kind == "error":
            self.status = f"Report action failed: {value}"
            self._goto(
                "confirm_send"
                if self._job == "send"
                else "complete"
                if self.result or self.receipt
                else "review"
            )
        elif self._job == "prepare_send":
            self.prepared = value
            self._goto("confirm_send")
        elif self._job == "send":
            self.receipt = value
            self._goto("complete")
        elif self._job == "export_draft":
            self.result = value
            self.saved = True
            self._start("prepare_draft", "Preparing GitHub preview")
        elif self._job == "save":
            self.result, self.saved = value, True
            self._goto("complete")
        elif self._job in {"prepare_draft", "open_draft"}:
            self.issue = value
            if not value.issue_url:
                self.status = "GitHub drafts are disabled. Your local report is ready."
                self._goto("complete")
            elif self._job == "prepare_draft":
                self._goto("draft")
            else:
                self.status = "Draft opened in your browser. Submit it there when ready."
                if not value.opened:
                    detail = sanitize_feedback_text(
                        value.open_error or "No browser accepted the URL.", workspace_root=self.root
                    )
                    self.status = (
                        f"Browser could not open: {detail} Use Ctrl+Y to copy the draft URL."
                    )
                self._goto("draft" if not value.opened else "complete")
        else:
            self.status = (
                "Report ID copied."
                if self._job == "copy_id"
                else "ZIP path copied."
                if self._job == "copy_path"
                else "Draft URL copied."
            )
            self._goto("complete" if self._job in {"copy_path", "copy_id"} else "draft")
