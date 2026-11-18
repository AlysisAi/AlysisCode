from __future__ import annotations

from pathlib import Path

import typer

from ...config import ConfigError, load_config
from ...feedback_report import (
    FeedbackReportError,
    create_feedback_bundle,
    create_feedback_github_issue_draft,
    feedback_github_issue_status_lines,
    resolve_feedback_workspace_root,
)
from . import _patchable
from ._shared import _console

report_app = typer.Typer(add_completion=False, help="Feedback bundle commands.")


@report_app.command("create")
def report_create(
    feedback: str | None = typer.Argument(None, help="Optional feedback text."),
    path: Path = typer.Option(Path("."), "--path", help="Workspace path."),
    session_id: str | None = typer.Option(None, "--session-id", help="Retained session id."),
    latest: bool = typer.Option(
        False,
        "--latest",
        help="Use the latest retained session when no active session exists.",
    ),
    github: bool | None = typer.Option(
        None,
        "--github/--no-github",
        help="Override feedback GitHub issue draft creation.",
    ),
    local_only: bool = typer.Option(
        False,
        "--local-only",
        help="Only create the local bundle; do not create a GitHub issue draft URL.",
    ),
    open_browser: bool | None = typer.Option(
        None,
        "--open/--no-open",
        help="Open the GitHub issue draft in a browser. CLI defaults to URL-only.",
    ),
) -> None:
    console = _console()
    if local_only and github is True:
        console.print("[red]Feedback report failed:[/red] Use either --github or --local-only.")
        raise typer.Exit(code=2)
    try:
        cfg = _patchable("load_config", load_config)()
        workspace_root = _patchable(
            "resolve_feedback_workspace_root", resolve_feedback_workspace_root
        )(path)
        result = _patchable("create_feedback_bundle", create_feedback_bundle)(
            workspace_root=workspace_root,
            feedback_text=feedback,
            cfg=cfg,
            session_id=session_id,
            latest=latest,
        )
    except (ConfigError, FeedbackReportError) as e:
        console.print(f"[red]Feedback report failed:[/red] {e}")
        raise typer.Exit(code=2) from e

    console.print(f"Feedback bundle directory: {result.bundle_dir}")
    console.print(f"Feedback bundle archive: {result.zip_path}")

    try:
        issue_result = _patchable(
            "create_feedback_github_issue_draft",
            create_feedback_github_issue_draft,
        )(
            bundle_result=result,
            feedback_text=feedback,
            cfg=cfg,
            github_enabled=False if local_only else github,
            open_browser=False if open_browser is None else open_browser,
        )
    except Exception as e:  # noqa: BLE001 - GitHub issue drafting is best-effort.
        console.print(f"[yellow]GitHub issue draft skipped:[/yellow] {e}")
        return

    for line in feedback_github_issue_status_lines(issue_result):
        console.print(line)
