from __future__ import annotations

import json
import os
import platform
import re
import shutil
import zipfile
from collections.abc import Callable
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from . import __version__
from .config import (
    AppConfig,
    resolve_feedback_github_enabled,
    resolve_feedback_github_repo,
    resolve_feedback_open_browser,
)
from .git_ops import GitOpsError, ensure_git_repo, ensure_runtime_artifact_excludes
from .host_browser import open_url
from .logging_redaction import redact_log_text
from .serialized_paths import (
    looks_like_serialized_path_field,
    safe_serialized_path,
    safe_serialized_path_field,
    sanitize_paths_in_text,
)
from .session_metrics import score_session_events, score_session_log
from .session_store import (
    filter_sessions_to_local_owner,
    list_sessions,
    read_session_events,
    resolve_sessions_dir,
    sanitize_session_id,
)
from .web_research import (
    SessionWebResearchTracker,
    build_web_research_metrics_from_artifact_payload,
    web_research_artifact_has_activity,
)
from .workspace_context import WorkspaceContextError, resolve_workspace_context


class FeedbackReportError(RuntimeError):
    pass


class FeedbackReportCancelled(FeedbackReportError):
    """An export was cancelled before publishing its archive."""


@dataclass
class _ExportJob:
    progress: Callable[[str], None] | None = None
    cancelled: Callable[[], bool] | None = None
    bundle_dir: Path | None = None
    zip_path: Path | None = None


_EXPORT_JOB: ContextVar[_ExportJob | None] = ContextVar("feedback_export_job", default=None)


def _io_path(path: Path) -> Path:
    """Use extended Windows paths for I/O, never for display or serialization."""
    if os.name != "nt":
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return Path(absolute)
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)


def _checkpoint(label: str | None = None) -> None:
    job = _EXPORT_JOB.get()
    if job is None:
        return
    if job.cancelled is not None and job.cancelled():
        raise FeedbackReportCancelled("Report cancelled. No archive was published.")
    if label and job.progress is not None:
        job.progress(label)


_SAFE_EXPORT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_HIERARCHICAL_URL_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.-]*://\S+",
)
_ISSUE_TITLE_MAX_CHARS = 96
_ISSUE_FEEDBACK_MAX_CHARS = 1400
_ISSUE_BODY_MAX_CHARS = 3600
_ISSUE_URL_MAX_CHARS = 8000
_MAX_REDACTION_DEPTH = 32
_NESTED_EXPORT_OMITTED = "[Omitted: diagnostic data exceeds the safe redaction depth.]"
_SECRET_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bslk_[A-Za-z0-9_-]{8,}"), "[REDACTED]"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "[REDACTED]"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(Authorization\s*:\s*)(.+)", re.IGNORECASE), r"\1[REDACTED]"),
    (
        re.compile(r"((?:ALYSIS_API_KEY|OPENAI_API_KEY)\s*[:=]\s*)([^\s\"']+)", re.IGNORECASE),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r'((?:"(?:api_key|api-key|authorization)"\s*:\s*"))([^"]+)(")', re.IGNORECASE),
        r"\1[REDACTED]\3",
    ),
    (
        re.compile(r"((?:api_key|api-key)\s*[:=]\s*)([^\s,]+)", re.IGNORECASE),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"""(\b(?:[\w-]*[_-])?(?:api[_-]?key|access[_-]?key|secret[_-]?key|client[_-]?secret|auth[_-]?token|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|authorization|cookie|set-cookie)\b["']?\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;]+)""",
            re.IGNORECASE,
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----"
        ),
        "[REDACTED PRIVATE KEY]",
    ),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "[REDACTED]"),
)

# Provider continuation state is required inside a live session but must never
# leave the private session store through a support bundle. They can contain raw
# chain-of-thought, encrypted/redacted blocks, provider signatures, response
# items, and replay tokens. Drop the whole container rather than trying to
# maintain a provider-by-provider nested denylist.
_PROVIDER_CONTINUATION_CONTAINER_KEYS = frozenset(
    {
        "providermetadata",
        "alysisprovidermetadata",
    }
)
_REASONING_BLOCK_TYPES = frozenset(
    {
        "reasoning",
        "reasoningencrypted",
        "reasoningsummary",
        "reasoningtext",
        "redactedthinking",
        "thinking",
        "thinkchunk",
        "thought",
    }
)
_REASONING_FAMILIES = ("reasoning", "thinking", "thought")
_REASONING_SENSITIVE_MARKERS = (
    "block",
    "chunk",
    "content",
    "detail",
    "encrypted",
    "monologue",
    "raw",
    "redacted",
    "signature",
    "summary",
    "text",
)
_REASONING_DIAGNOSTIC_MARKERS = (
    "active",
    "adapter",
    "available",
    "availability",
    "budget",
    "capability",
    "confidence",
    "config",
    "count",
    "display",
    "effort",
    "enabled",
    "format",
    "include",
    "kind",
    "level",
    "mode",
    "policy",
    "requested",
    "source",
    "support",
    "token",
    "trace",
    "type",
)
_OPAQUE_CONTINUATION_KEYS = frozenset(
    {
        "cachedcontent",
        "continuation",
        "continuationdata",
        "continuationpayload",
        "continuationstate",
        "continuationtoken",
        "encryptedcontent",
        "encryptedreasoning",
        "encryptedthinking",
        "opaquecontinuation",
        "opaquepayload",
        "opaquestate",
        "previousresponseid",
        "redactedcontent",
        "redactedthinking",
    }
)
_INVALID_STRUCTURED_EXPORT = {
    "redacted": "Invalid structured artifact omitted from support bundle."
}


@dataclass(frozen=True)
class FeedbackBundleResult:
    bundle_dir: Path
    zip_path: Path
    output_root: Path
    workspace_root: Path
    session_id: str | None


@dataclass(frozen=True)
class FeedbackGithubIssueResult:
    repo: str
    issue_url: str | None
    opened: bool
    open_attempted: bool
    disabled_reason: str | None = None
    open_error: str | None = None


@dataclass(frozen=True)
class _ResolvedSessionSource:
    source: str
    session_id: str | None
    log_path: Path | None
    log_meta_path: Path | None
    artifact_root: Path | None
    logging_enabled: bool
    score_payload: dict[str, Any]
    snapshot_payload: dict[str, Any] | None
    web_research_payload: dict[str, Any] | None


def resolve_feedback_workspace_root(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    if not candidate.exists():
        raise FeedbackReportError(f"Workspace path does not exist: {candidate}")
    if not candidate.is_dir():
        raise FeedbackReportError(f"Workspace path is not a directory: {candidate}")
    try:
        return resolve_workspace_context(candidate).workspace_root
    except WorkspaceContextError:
        return candidate


def create_feedback_bundle(
    *,
    workspace_root: Path,
    feedback_text: str | None = None,
    cfg: AppConfig | None = None,
    active_session: Any | None = None,
    pending_images: list[str] | None = None,
    session_id: str | None = None,
    latest: bool = False,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> FeedbackBundleResult:
    job = _ExportJob(progress=progress, cancelled=cancelled)
    token = _EXPORT_JOB.set(job)
    try:
        _checkpoint("Collecting session diagnostics")
        return _create_feedback_bundle(
            workspace_root=workspace_root,
            feedback_text=feedback_text,
            cfg=cfg,
            active_session=active_session,
            pending_images=pending_images,
            session_id=session_id,
            latest=latest,
        )
    except BaseException as exc:
        # These paths are registered only after this invocation creates them.
        # Never remove a pre-existing bundle or the output directory itself.
        if job.bundle_dir is not None:
            shutil.rmtree(_io_path(job.bundle_dir), ignore_errors=True)
        if job.zip_path is not None:
            with suppress(OSError):
                _io_path(job.zip_path.with_suffix(".zip.partial")).unlink(missing_ok=True)
        if isinstance(exc, OSError):
            raise FeedbackReportError(
                "Could not write the report. Check free space and folder permissions. "
                + _sanitize_exported_freeform_text(str(exc), workspace_root=workspace_root)
            ) from exc
        raise
    finally:
        _EXPORT_JOB.reset(token)


def _create_feedback_bundle(
    *,
    workspace_root: Path,
    feedback_text: str | None,
    cfg: AppConfig | None,
    active_session: Any | None,
    pending_images: list[str] | None,
    session_id: str | None,
    latest: bool,
) -> FeedbackBundleResult:
    if session_id and latest:
        raise FeedbackReportError("Use either session_id or latest, not both.")

    root = resolve_feedback_workspace_root(workspace_root)
    output_root = root / "alysis-feedback"
    _path_within_dir(
        base_dir=root, candidate=output_root, label="report output", require_exists=False
    )
    _io_path(output_root).mkdir(parents=True, exist_ok=True)
    _ensure_feedback_dir_is_git_ignored(root)

    session_source = _resolve_session_source(
        cfg=cfg or AppConfig(),
        active_session=active_session,
        pending_images=pending_images,
        session_id=session_id,
        latest=latest,
    )
    if not _session_source_has_exportable_content(session_source):
        raise FeedbackReportError("No retained session logs found.")

    created_at = _now_utc()
    bundle_name = _make_bundle_name(
        created_at=created_at,
        session_id=session_source.session_id,
    )
    while True:
        bundle_dir = _allocate_bundle_dir(output_root=output_root, base_name=bundle_name)
        try:
            _io_path(bundle_dir).mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            # Another process may export the same session in the same second.
            _checkpoint()
    zip_path = bundle_dir.with_suffix(".zip")
    job = _EXPORT_JOB.get()
    if job is not None:
        job.bundle_dir, job.zip_path = bundle_dir, zip_path

    copied_session_log_path: Path | None = None
    copied_session_artifacts_path: Path | None = None
    snapshot_path: Path | None = None
    copied_web_research_path: Path | None = None

    session_bundle_dir = bundle_dir / "session"
    if session_source.log_path is not None:
        copied_session_log_path = session_bundle_dir / "log.jsonl"
        _copy_file(session_source.log_path, copied_session_log_path, workspace_root=root)
        if session_source.log_meta_path is not None:
            _copy_file(
                session_source.log_meta_path,
                session_bundle_dir / "log.meta.json",
                workspace_root=root,
            )
    if session_source.artifact_root is not None:
        copied_session_artifacts_path = session_bundle_dir / "artifacts"
        _copy_tree(
            session_source.artifact_root,
            copied_session_artifacts_path,
            workspace_root=root,
        )
    if session_source.snapshot_payload is not None:
        snapshot_path = session_bundle_dir / "session_snapshot.json"
        _write_json(snapshot_path, session_source.snapshot_payload, workspace_root=root)
    if session_source.web_research_payload is not None:
        copied_web_research_path = session_bundle_dir / "web_research_sources.json"
        _write_json(
            copied_web_research_path,
            session_source.web_research_payload,
            workspace_root=root,
        )

    session_score_path = bundle_dir / "session_score.json"
    _write_json(session_score_path, session_source.score_payload, workspace_root=root)

    feedback_path = bundle_dir / "feedback.md"
    _io_path(feedback_path).write_text(
        _render_feedback_markdown(
            created_at=created_at,
            feedback_text=feedback_text,
            session_id=session_source.session_id,
            workspace_root=root,
        ),
        encoding="utf-8",
    )

    summary_path = bundle_dir / "summary.md"
    _io_path(summary_path).write_text(
        _render_summary_markdown(
            workspace_root=root,
            bundle_dir=bundle_dir,
            output_root=output_root,
            session_source=session_source,
            copied_session_log_path=copied_session_log_path,
            copied_session_artifacts_path=copied_session_artifacts_path,
            snapshot_path=snapshot_path,
            copied_web_research_path=copied_web_research_path,
        ),
        encoding="utf-8",
    )

    manifest_path = bundle_dir / "manifest.json"
    _write_json(
        manifest_path,
        {
            "schema_version": 1,
            "tool": "alysis",
            "tool_version": __version__,
            "created_at": _now_iso(created_at),
            "workspace_root": safe_serialized_path_field(
                "workspace_root",
                root,
                workspace_root=root,
            ),
            "bundle_dir": safe_serialized_path_field(
                "bundle_dir",
                bundle_dir,
                workspace_root=root,
            ),
            "zip_path": safe_serialized_path_field(
                "zip_path",
                zip_path,
                workspace_root=root,
            ),
            "session": {
                "source": session_source.source,
                "session_id": session_source.session_id,
                "logging_enabled": session_source.logging_enabled,
                "log_path": safe_serialized_path_field(
                    "log_path",
                    session_source.log_path,
                    workspace_root=root,
                ),
                "artifact_root": (
                    safe_serialized_path_field(
                        "artifact_root",
                        session_source.artifact_root,
                        workspace_root=root,
                    )
                    if session_source.artifact_root
                    else None
                ),
                "included_log": copied_session_log_path is not None,
                "included_artifacts": copied_session_artifacts_path is not None,
                "included_snapshot": snapshot_path is not None,
                "included_web_research_artifact": copied_web_research_path is not None,
            },
            "platform": {
                "python_version": platform.python_version(),
                "platform": platform.platform(),
                "system": platform.system(),
                "release": platform.release(),
            },
            "bundle_policy": {
                "local_only": True,
                "excluded": [
                    "api_keys",
                    "environment_dumps",
                    "full_workspace_snapshots",
                ],
            },
        },
        workspace_root=root,
    )

    _write_deterministic_zip(bundle_dir=bundle_dir, zip_path=zip_path)

    return FeedbackBundleResult(
        bundle_dir=bundle_dir,
        zip_path=zip_path,
        output_root=output_root,
        workspace_root=root,
        session_id=session_source.session_id,
    )


def create_feedback_github_issue_draft(
    *,
    bundle_result: FeedbackBundleResult,
    feedback_text: str | None = None,
    cfg: AppConfig | None = None,
    github_enabled: bool | None = None,
    open_browser: bool | None = None,
    browser_open: Callable[..., bool] | None = None,
) -> FeedbackGithubIssueResult:
    enabled = (
        resolve_feedback_github_enabled(cfg) if github_enabled is None else bool(github_enabled)
    )
    if not enabled:
        return FeedbackGithubIssueResult(
            repo="",
            issue_url=None,
            opened=False,
            open_attempted=False,
            disabled_reason="disabled",
        )

    repo = resolve_feedback_github_repo(cfg)
    title, body = build_feedback_github_issue_payload(
        bundle_result=bundle_result,
        feedback_text=feedback_text,
    )
    issue_url = _github_new_issue_url(
        repo=repo,
        title=title,
        body=body,
        bundle_result=bundle_result,
    )
    should_open = resolve_feedback_open_browser(cfg) if open_browser is None else bool(open_browser)
    if not should_open:
        return FeedbackGithubIssueResult(
            repo=repo,
            issue_url=issue_url,
            opened=False,
            open_attempted=False,
            disabled_reason="browser_open_disabled",
        )

    return open_feedback_github_issue_draft(
        FeedbackGithubIssueResult(
            repo=repo, issue_url=issue_url, opened=False, open_attempted=False
        ),
        browser_open=browser_open,
    )


def open_feedback_github_issue_draft(
    draft: FeedbackGithubIssueResult,
    *,
    browser_open: Callable[..., bool] | None = None,
) -> FeedbackGithubIssueResult:
    """Open the exact URL that was previewed; never rebuild from mutable files."""
    if draft.issue_url is None:
        return draft
    try:
        opened = bool(
            browser_open(draft.issue_url, new=2)
            if browser_open is not None
            else open_url(draft.issue_url, quiet=True)
        )
    except Exception as exc:  # noqa: BLE001 - platform/browser integration is best-effort.
        return replace(
            draft,
            opened=False,
            open_attempted=True,
            open_error=_redact_bundle_text(str(exc)),
            disabled_reason=None,
        )
    return replace(
        draft,
        opened=opened,
        open_attempted=True,
        open_error=None if opened else "browser did not accept the URL",
        disabled_reason=None,
    )


def build_feedback_github_issue_payload(
    *,
    bundle_result: FeedbackBundleResult,
    feedback_text: str | None = None,
) -> tuple[str, str]:
    feedback = _sanitize_exported_freeform_text(
        (feedback_text or "").strip(),
        workspace_root=bundle_result.workspace_root,
    )
    title = _feedback_issue_title(feedback)
    manifest = _read_optional_json(bundle_result.bundle_dir / "manifest.json")
    score = _read_optional_json(bundle_result.bundle_dir / "session_score.json")
    body = _feedback_issue_body(
        bundle_result=bundle_result,
        feedback_text=feedback,
        manifest=manifest,
        score=score,
    )
    return title, body


def feedback_github_issue_status_lines(result: FeedbackGithubIssueResult) -> list[str]:
    if result.issue_url is None:
        return []
    lines: list[str] = []
    if result.opened:
        lines.append(f"GitHub issue draft opened: {result.issue_url}")
    else:
        lines.append(f"GitHub issue draft URL: {result.issue_url}")
        if result.open_attempted and result.open_error:
            lines.append(f"Browser open failed: {result.open_error}")
    lines.append("Review the issue in GitHub before submitting; the archive was not uploaded.")
    return lines


def _github_new_issue_url(
    *,
    repo: str,
    title: str,
    body: str,
    bundle_result: FeedbackBundleResult,
) -> str:
    url = _raw_github_new_issue_url(repo=repo, title=title, body=body)
    if len(url) <= _ISSUE_URL_MAX_CHARS:
        return url

    compact_body = _compact_feedback_issue_body(bundle_result=bundle_result)
    url = _raw_github_new_issue_url(repo=repo, title=title, body=compact_body)
    while len(url) > _ISSUE_URL_MAX_CHARS and len(compact_body) > 240:
        compact_body = _truncate_issue_text(
            compact_body,
            max_chars=max(240, len(compact_body) // 2),
        )
        url = _raw_github_new_issue_url(repo=repo, title=title, body=compact_body)
    return url


def _raw_github_new_issue_url(*, repo: str, title: str, body: str) -> str:
    query = urlencode({"title": title, "body": body})
    return f"https://github.com/{repo}/issues/new?{query}"


def _feedback_issue_title(feedback_text: str) -> str:
    first_line = next(
        (line.strip() for line in feedback_text.splitlines() if line.strip()),
        "",
    )
    if not first_line:
        return "Alysis Code feedback report"
    normalized = re.sub(r"\s+", " ", first_line)
    prefix = "Alysis Code feedback: "
    available = _ISSUE_TITLE_MAX_CHARS - len(prefix)
    if len(normalized) > available:
        normalized = normalized[: max(0, available - 1)].rstrip() + "..."
    return f"{prefix}{normalized}"


def _feedback_issue_body(
    *,
    bundle_result: FeedbackBundleResult,
    feedback_text: str,
    manifest: dict[str, Any],
    score: dict[str, Any],
) -> str:
    bundle_dir = safe_serialized_path(
        bundle_result.bundle_dir,
        workspace_root=bundle_result.workspace_root,
    )
    zip_path = safe_serialized_path(
        bundle_result.zip_path,
        workspace_root=bundle_result.workspace_root,
    )
    platform_payload = (
        manifest.get("platform") if isinstance(manifest.get("platform"), dict) else {}
    )
    session_payload = manifest.get("session") if isinstance(manifest.get("session"), dict) else {}
    feedback_block = _truncate_issue_text(
        feedback_text or "_No feedback text was provided._",
        max_chars=_ISSUE_FEEDBACK_MAX_CHARS,
    )
    lines = [
        "### Feedback",
        "",
        feedback_block,
        "",
        "### Local Support Bundle",
        "",
        f"- Directory: `{bundle_dir or '(unknown)'}`",
        f"- Archive: `{zip_path or '(unknown)'}`",
        "",
        (
            "Please review the archive before attaching it. It is generated locally, applies "
            "best-effort secret/path redaction, and is not uploaded automatically."
        ),
        "",
        "### Session",
        "",
        f"- Session ID: `{bundle_result.session_id or '(none)'}`",
        f"- Session source: `{session_payload.get('source') or '(unknown)'}`",
        f"- Verification authoritative: `{_yes_no(score.get('verification_authoritative'))}`",
        f"- Last verification failure: `{score.get('last_verification_failure_kind') or '(none recorded)'}`",
        "",
        "### Environment",
        "",
        f"- Alysis Code version: `{__version__}`",
        f"- Python: `{platform_payload.get('python_version') or platform.python_version()}`",
        f"- Platform: `{platform_payload.get('platform') or platform.platform()}`",
        "",
        "### Bundle Policy",
        "",
        "- No report data or archive was submitted automatically.",
        "- The issue body intentionally excludes raw logs, tool outputs, environment dumps, and API keys.",
        "- Attach the archive only if you are comfortable sharing the local support bundle.",
        "",
    ]
    body = "\n".join(lines)
    return _truncate_issue_text(body, max_chars=_ISSUE_BODY_MAX_CHARS)


def _compact_feedback_issue_body(*, bundle_result: FeedbackBundleResult) -> str:
    bundle_dir = safe_serialized_path(
        bundle_result.bundle_dir,
        workspace_root=bundle_result.workspace_root,
    )
    zip_path = safe_serialized_path(
        bundle_result.zip_path,
        workspace_root=bundle_result.workspace_root,
    )
    lines = [
        "### Feedback",
        "",
        "The original feedback text was too large for a stable GitHub prefill URL.",
        "",
        "### Local Support Bundle",
        "",
        f"- Directory: `{bundle_dir or '(unknown)'}`",
        f"- Archive: `{zip_path or '(unknown)'}`",
        "",
        "Please review the archive before attaching it. It was not uploaded automatically.",
        "",
        "### Bundle Policy",
        "",
        "- No report data or archive was submitted automatically.",
        "- The issue body intentionally excludes raw logs, tool outputs, environment dumps, and API keys.",
    ]
    return "\n".join(lines)


def _read_optional_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_io_path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _truncate_issue_text(text: str, *, max_chars: int) -> str:
    clean = str(text or "")
    if len(clean) <= max_chars:
        return clean
    suffix = "\n\n[Truncated for GitHub issue prefill. See local feedback bundle for details.]"
    return clean[: max(0, max_chars - len(suffix))].rstrip() + suffix


def _yes_no(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return "(unknown)"


def _resolve_session_source(
    *,
    cfg: AppConfig,
    active_session: Any | None,
    pending_images: list[str] | None,
    session_id: str | None,
    latest: bool,
) -> _ResolvedSessionSource:
    if active_session is not None:
        return _resolve_active_session_source(
            active_session=active_session,
            pending_images=pending_images,
        )

    sessions_dir = resolve_sessions_dir(cfg)
    target_session_id = _validate_export_id("session id", session_id) if session_id else ""
    if not target_session_id:
        infos = filter_sessions_to_local_owner(list_sessions(sessions_dir))
        if infos:
            target_session_id = infos[0].session_id
        elif latest:
            raise FeedbackReportError(f"No retained sessions found in {sessions_dir}")

    if not target_session_id:
        return _ResolvedSessionSource(
            source="none",
            session_id=None,
            log_path=None,
            log_meta_path=None,
            artifact_root=None,
            logging_enabled=False,
            score_payload=_fallback_session_score(
                session_id=None,
                reason="no retained session selected",
            ),
            snapshot_payload=None,
            web_research_payload=None,
        )

    log_path = _path_within_dir(
        base_dir=sessions_dir,
        candidate=sessions_dir / f"{target_session_id}.jsonl",
        label="session log",
        require_exists=True,
    )
    if not log_path.exists():
        raise FeedbackReportError(f"Session log not found: {log_path}")
    artifact_root_candidate = sessions_dir / sanitize_session_id(target_session_id)
    artifact_root = (
        _path_within_dir(
            base_dir=sessions_dir,
            candidate=artifact_root_candidate,
            label="session artifact root",
            require_exists=False,
        )
        if artifact_root_candidate.exists()
        else artifact_root_candidate
    )
    meta_path = log_path.with_suffix(".meta.json")
    events = list(read_session_events(log_path))
    score_payload = score_session_events(events)
    score_payload["session_id"] = str(score_payload.get("session_id") or target_session_id)
    score_payload["path"] = str(log_path)
    web_research_payload = _merge_web_research_payloads(
        events=events,
        persisted_payload=_read_web_research_payload(
            artifact_root if artifact_root.exists() else None
        ),
    )
    _overlay_web_research_metrics(score_payload, web_research_payload)
    return _ResolvedSessionSource(
        source="explicit_session_id" if session_id else "latest_retained",
        session_id=target_session_id,
        log_path=log_path,
        log_meta_path=meta_path if meta_path.exists() else None,
        artifact_root=artifact_root if artifact_root.exists() else None,
        logging_enabled=True,
        score_payload=score_payload,
        snapshot_payload=None,
        web_research_payload=web_research_payload,
    )


def _resolve_active_session_source(
    *,
    active_session: Any,
    pending_images: list[str] | None,
) -> _ResolvedSessionSource:
    store = getattr(active_session, "store", None)
    session_id = str(getattr(store, "session_id", "") or "").strip() or None
    logging_enabled = bool(getattr(store, "enabled", False))
    log_path_obj = getattr(store, "path", None)
    log_path = Path(log_path_obj) if log_path_obj is not None else None
    if log_path is not None and not _io_path(log_path).exists():
        log_path = None
    artifact_root_obj = getattr(store, "session_artifact_root", None)
    artifact_root = artifact_root_obj if isinstance(artifact_root_obj, Path) else None
    if artifact_root is not None and not _io_path(artifact_root).exists():
        artifact_root = None
    meta_path = log_path.with_suffix(".meta.json") if log_path is not None else None
    event_snapshot = (
        events_fn() if callable(events_fn := getattr(store, "events_snapshot", None)) else None
    )
    web_research_payload = (
        artifact_fn()
        if callable(artifact_fn := getattr(store, "web_research_artifact_payload", None))
        else None
    )
    snapshot_payload = None
    if not logging_enabled or log_path is None:
        snapshot_payload = _build_in_memory_session_snapshot(
            active_session=active_session,
            pending_images=pending_images,
        )
    if isinstance(event_snapshot, list) and event_snapshot:
        score_payload = score_session_events(event_snapshot)
        score_payload["session_id"] = str(score_payload.get("session_id") or session_id or "")
        score_payload["path"] = str(log_path) if log_path is not None else None
        score_payload["available"] = True
        score_payload["reason"] = "derived from active in-memory session events"
    elif log_path is not None:
        score_payload = score_session_log(log_path)
    else:
        score_payload = _fallback_session_score(
            session_id=session_id,
            reason="session logging disabled or log not retained",
        )
    _overlay_web_research_metrics(score_payload, web_research_payload)
    return _ResolvedSessionSource(
        source="active_session",
        session_id=session_id,
        log_path=log_path,
        log_meta_path=meta_path if meta_path is not None and meta_path.exists() else None,
        artifact_root=artifact_root,
        logging_enabled=logging_enabled,
        score_payload=score_payload,
        snapshot_payload=snapshot_payload,
        web_research_payload=(
            web_research_payload
            if web_research_artifact_has_activity(web_research_payload)
            else None
        ),
    )


def _read_web_research_payload(artifact_root: Path | None) -> dict[str, Any] | None:
    if artifact_root is None:
        return None
    artifact_path = artifact_root / "web_research_sources.json"
    if not artifact_path.exists():
        return None
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if web_research_artifact_has_activity(payload) else None


def _merge_web_research_payloads(
    *,
    events: list[dict[str, Any]] | None = None,
    persisted_payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    tracker = SessionWebResearchTracker()
    if isinstance(events, list):
        for event in events:
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            tracker.observe_event(
                event_type=str(event.get("type") or "").strip(),
                payload=payload,
                ts=str(event.get("ts") or "").strip() or None,
            )
    if isinstance(persisted_payload, dict):
        tracker.merge_from_artifact_payload(persisted_payload)
    tracker.clear_pending()
    payload = tracker.artifact_payload()
    return payload if web_research_artifact_has_activity(payload) else None


def _overlay_web_research_metrics(
    score_payload: dict[str, Any],
    web_research_payload: dict[str, Any] | None,
) -> None:
    if not web_research_artifact_has_activity(web_research_payload):
        return
    score_payload.update(build_web_research_metrics_from_artifact_payload(web_research_payload))


def _build_in_memory_session_snapshot(
    *,
    active_session: Any,
    pending_images: list[str] | None,
) -> dict[str, Any]:
    session_root_obj = getattr(active_session, "root", None)
    session_root = session_root_obj if isinstance(session_root_obj, Path) else None
    usage_summary = getattr(active_session, "usage_summary", None)
    totals_fn = getattr(usage_summary, "totals", None)
    usage_totals = totals_fn() if callable(totals_fn) else {}
    messages_obj = getattr(active_session, "messages", [])
    messages = messages_obj if isinstance(messages_obj, list) else []
    non_system_messages = [
        _to_jsonable(message)
        for message in messages
        if isinstance(message, dict) and str(message.get("role") or "").strip().lower() != "system"
    ]
    store = getattr(active_session, "store", None)
    web_research_sources = None
    has_web_research_activity = getattr(store, "has_web_research_activity", None)
    web_research_artifact_payload = getattr(store, "web_research_artifact_payload", None)
    if callable(has_web_research_activity) and callable(web_research_artifact_payload):
        if has_web_research_activity():
            web_research_sources = _to_jsonable(web_research_artifact_payload())
    return {
        "snapshot_type": "in_memory_session",
        "session_id": str(getattr(store, "session_id", "") or "").strip() or None,
        "workspace_root": safe_serialized_path_field(
            "workspace_root",
            session_root,
            workspace_root=session_root,
        ),
        "mode": str(getattr(active_session, "mode", "") or ""),
        "model": str(getattr(getattr(active_session, "client", None), "model", "") or ""),
        "stream": bool(getattr(active_session, "stream", False)),
        "no_log": bool(getattr(active_session, "no_log", False)),
        "message_count": len(messages),
        "messages": non_system_messages,
        "pending_images": [
            safe_serialized_path(str(item), workspace_root=session_root) or str(item)
            for item in list(pending_images or [])
            if str(item).strip()
        ],
        "usage_totals": _to_jsonable(usage_totals),
        "effective_verification_commands": _to_jsonable(
            getattr(active_session, "effective_verification_commands", [])
        ),
        "authoritative_verification_commands": _to_jsonable(
            getattr(active_session, "authoritative_verification_commands", None)
        ),
        "verification_selection_source": str(
            getattr(active_session, "verification_selection_source", "") or ""
        ),
        "verification_selection_reason": str(
            getattr(active_session, "verification_selection_reason", "") or ""
        ),
        "verification_contract_type": str(
            getattr(active_session, "verification_contract_type", "") or ""
        ),
        "verification_authoritative": bool(
            getattr(active_session, "verification_authoritative", False)
        ),
        "web_research_sources": web_research_sources,
    }


def _fallback_session_score(*, session_id: str | None, reason: str) -> dict[str, Any]:
    payload = score_session_events(
        [
            {
                "type": "session_start",
                "session_id": str(session_id or ""),
                "payload": {},
            }
        ]
    )
    payload["session_id"] = str(session_id or "")
    payload["available"] = False
    payload["reason"] = reason
    payload["path"] = None
    return payload


def _ensure_feedback_dir_is_git_ignored(root: Path) -> None:
    try:
        ensure_git_repo(root)
    except GitOpsError:
        return
    ensure_runtime_artifact_excludes(root)


def _session_source_has_exportable_content(source: _ResolvedSessionSource) -> bool:
    return (
        source.log_path is not None
        or source.artifact_root is not None
        or source.snapshot_payload is not None
        or source.web_research_payload is not None
    )


def _copy_tree(src: Path, dest: Path, *, workspace_root: Path) -> None:
    src, dest = _io_path(src), _io_path(dest)
    _checkpoint("Sanitizing diagnostic artifacts")
    if not src.exists():
        return
    if src.is_dir():
        dest.mkdir(parents=True, exist_ok=True)
    for path in sorted(src.rglob("*")):
        _checkpoint()
        if path.is_symlink():
            continue
        rel = path.relative_to(src)
        target = dest / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not path.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_file(path, target, workspace_root=workspace_root)


def _copy_file(src: Path, dest: Path, *, workspace_root: Path) -> None:
    src, dest = _io_path(src), _io_path(dest)
    _checkpoint()
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = _read_text_if_utf8(src)
    suffix = src.suffix.lower()
    if text is None:
        if suffix in {".json", ".jsonl"}:
            _write_invalid_structured_export(dest, jsonl=suffix == ".jsonl")
            shutil.copystat(src, dest)
            return
        # Binary/non-UTF-8 data cannot be inspected by the export scrubber.
        # In particular UTF-16 tool output must not bypass credential redaction.
        dest.write_text(
            "[Omitted: artifact could not be safely sanitized as UTF-8 text.]\n", encoding="utf-8"
        )
        return
    if suffix == ".json":
        sanitized_json = _sanitize_json_text(text, workspace_root=workspace_root)
        if sanitized_json is not None:
            dest.write_text(sanitized_json, encoding="utf-8")
            shutil.copystat(src, dest)
            return
        _write_invalid_structured_export(dest, jsonl=False)
        shutil.copystat(src, dest)
        return
    if suffix == ".jsonl":
        sanitized_jsonl = _sanitize_jsonl_text(text, workspace_root=workspace_root)
        if sanitized_jsonl is not None:
            dest.write_text(sanitized_jsonl, encoding="utf-8")
            shutil.copystat(src, dest)
            return
        _write_invalid_structured_export(dest, jsonl=True)
        shutil.copystat(src, dest)
        return
    dest.write_text(
        _sanitize_exported_freeform_text(text, workspace_root=workspace_root),
        encoding="utf-8",
    )
    shutil.copystat(src, dest)


def _write_invalid_structured_export(path: Path, *, jsonl: bool) -> None:
    """Replace an unreadable/malformed structured artifact instead of copying
    bytes that could bypass the recursive reasoning/continuation scrubber."""

    if jsonl:
        serialized = (
            json.dumps(_INVALID_STRUCTURED_EXPORT, sort_keys=True, ensure_ascii=True) + "\n"
        )
    else:
        serialized = (
            json.dumps(
                _INVALID_STRUCTURED_EXPORT,
                indent=2,
                sort_keys=True,
                ensure_ascii=True,
            )
            + "\n"
        )
    path.write_text(serialized, encoding="utf-8")


def _write_json(
    path: Path,
    payload: dict[str, Any],
    *,
    workspace_root: Path | None = None,
) -> None:
    path = _io_path(path)
    _checkpoint()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            _redact_jsonable(payload, workspace_root=workspace_root),
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_deterministic_zip(*, bundle_dir: Path, zip_path: Path) -> None:
    _checkpoint("Compressing support bundle")
    bundle_dir, zip_path = _io_path(bundle_dir), _io_path(zip_path)
    partial = zip_path.with_suffix(".zip.partial")
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(bundle_dir.rglob("*")):
            _checkpoint()
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(bundle_dir).as_posix()
            info = zipfile.ZipInfo(rel)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.date_time = (1980, 1, 1, 0, 0, 0)
            # Chunked writes keep cancellation responsive and avoid reading
            # each large tool output into memory again during compression.
            with path.open("rb") as source, zf.open(info, "w", force_zip64=True) as target:
                while chunk := source.read(1024 * 1024):
                    _checkpoint()
                    target.write(chunk)
    _checkpoint()
    partial.replace(zip_path)


def _render_feedback_markdown(
    *,
    created_at: datetime,
    feedback_text: str | None,
    session_id: str | None,
    workspace_root: Path,
) -> str:
    lines = [
        "# Feedback",
        "",
        f"- Created At: {_now_iso(created_at)}",
        f"- Session ID: {session_id or '(none)'}",
        "",
        "## User Feedback",
        "",
    ]
    text = _sanitize_exported_freeform_text(
        (feedback_text or "").strip(),
        workspace_root=workspace_root,
    )
    lines.append(text if text else "_No feedback text was provided._")
    lines.append("")
    return "\n".join(lines)


def _render_summary_markdown(
    *,
    workspace_root: Path,
    bundle_dir: Path,
    output_root: Path,
    session_source: _ResolvedSessionSource,
    copied_session_log_path: Path | None,
    copied_session_artifacts_path: Path | None,
    snapshot_path: Path | None,
    copied_web_research_path: Path | None,
) -> str:
    score_payload = (
        session_source.score_payload if isinstance(session_source.score_payload, dict) else {}
    )
    verification_selection_source = str(
        score_payload.get("verification_selection_source") or ""
    ).strip()
    verification_selection_reason = str(
        score_payload.get("verification_selection_reason") or ""
    ).strip()
    verification_contract_type = str(score_payload.get("verification_contract_type") or "").strip()
    verification_authoritative = bool(score_payload.get("verification_authoritative", False))
    last_verification_failure_kind = str(
        score_payload.get("last_verification_failure_kind") or ""
    ).strip()
    lines = [
        "# Support Bundle Summary",
        "",
        f"- Output Root: `{safe_serialized_path(output_root, workspace_root=workspace_root)}`",
        f"- Session Source: `{session_source.source}`",
        f"- Session ID: `{session_source.session_id or '(none)'}`",
        f"- Session Logging Enabled: {'yes' if session_source.logging_enabled else 'no'}",
        (
            f"- Included Session Log: `"
            f"{safe_serialized_path(copied_session_log_path, bundle_root=bundle_dir, prefer_bundle_relative=True)}`"
            if copied_session_log_path is not None
            else "- Included Session Log: (none)"
        ),
        (
            f"- Included Session Artifacts: "
            f"`{safe_serialized_path(copied_session_artifacts_path, bundle_root=bundle_dir, prefer_bundle_relative=True)}`"
            if copied_session_artifacts_path is not None
            else "- Included Session Artifacts: (none)"
        ),
        (
            f"- Included In-Memory Snapshot: `"
            f"{safe_serialized_path(snapshot_path, bundle_root=bundle_dir, prefer_bundle_relative=True)}`"
            if snapshot_path is not None
            else "- Included In-Memory Snapshot: (none)"
        ),
        (
            f"- Included Web Research Artifact: `"
            f"{safe_serialized_path(copied_web_research_path, bundle_root=bundle_dir, prefer_bundle_relative=True)}`"
            if copied_web_research_path is not None
            else "- Included Web Research Artifact: (none)"
        ),
        f"- Verification Selection Source: `{verification_selection_source or '(unknown)'}`",
        f"- Verification Contract Type: `{verification_contract_type or '(unknown)'}`",
        f"- Verification Authoritative: {'yes' if verification_authoritative else 'no'}",
        (
            f"- Verification Failure Kind: `{last_verification_failure_kind}`"
            if last_verification_failure_kind
            else "- Verification Failure Kind: (none recorded)"
        ),
        (
            f"- Verification Selection Reason: {verification_selection_reason}"
            if verification_selection_reason
            else "- Verification Selection Reason: (none recorded)"
        ),
        "- Excluded By Policy: API keys, environment dumps, full workspace snapshots",
        "",
    ]
    return "\n".join(lines)


def _allocate_bundle_dir(*, output_root: Path, base_name: str) -> Path:
    candidate = output_root / base_name
    if not _io_path(candidate).exists() and not _io_path(candidate.with_suffix(".zip")).exists():
        return candidate
    index = 2
    while True:
        candidate = output_root / f"{base_name}_{index:02d}"
        if (
            not _io_path(candidate).exists()
            and not _io_path(candidate.with_suffix(".zip")).exists()
        ):
            return candidate
        index += 1


def _make_bundle_name(
    *,
    created_at: datetime,
    session_id: str | None,
) -> str:
    parts = [f"report_{created_at.strftime('%Y%m%dT%H%M%SZ')}"]
    if session_id:
        parts.append(f"session_{sanitize_session_id(session_id)[:24]}")
    return "__".join(parts)


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    return str(value)


def _validate_export_id(kind: str, value: str | None) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise FeedbackReportError(f"Missing {kind}.")
    if not _SAFE_EXPORT_ID_RE.fullmatch(normalized):
        raise FeedbackReportError(
            f"Invalid {kind}: {normalized!r}. Only letters, numbers, '_' and '-' are allowed."
        )
    return normalized


def _path_within_dir(
    *,
    base_dir: Path,
    candidate: Path,
    label: str,
    require_exists: bool,
) -> Path:
    base_resolved = base_dir.resolve()
    try:
        candidate_resolved = candidate.resolve(strict=require_exists)
    except OSError as e:
        raise FeedbackReportError(f"Unable to resolve {label}: {candidate}") from e
    try:
        candidate_resolved.relative_to(base_resolved)
    except ValueError as e:
        raise FeedbackReportError(f"{label.capitalize()} escapes its expected directory.") from e
    return candidate_resolved


def _redact_bundle_text(text: str) -> str:
    clean = str(text or "")
    for pattern, replacement in _SECRET_REPLACEMENTS:
        clean = pattern.sub(replacement, clean)
    return redact_log_text(clean)


def sanitize_feedback_text(text: str, *, workspace_root: Path) -> str:
    """The same sanitized feedback for the local preview, archive and issue."""
    return _sanitize_exported_freeform_text(text, workspace_root=workspace_root)


def _is_secret_export_key(key: str | None) -> bool:
    normalized = _normalize_export_key(key)
    return normalized in {
        "token",
        "secret",
        "cookie",
        "setcookie",
        "credentials",
    } or normalized.endswith(
        (
            "apikey",
            "accesskey",
            "secretkey",
            "privatekey",
            "clientsecret",
            "password",
            "passwd",
            "authorization",
            "accesstoken",
            "refreshtoken",
            "authtoken",
            "sessiontoken",
        )
    )


def _sanitize_url_match(match: re.Match[str], *, strip_path: bool) -> str:
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ".,;:!?)]":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    try:
        parsed = urlsplit(raw)
        hostname = str(parsed.hostname or "").rstrip(".")
        safe_host = hostname.encode("idna").decode("ascii").casefold()
        if not safe_host or any(char.isspace() or char in "/@?#" for char in safe_host):
            raise ValueError("URL has no safe host")
        if ":" in safe_host and not safe_host.startswith("["):
            safe_host = f"[{safe_host}]"
        try:
            port = parsed.port
        except ValueError:
            port = None
        netloc = f"{safe_host}:{port}" if port is not None else safe_host
        sanitized = urlunsplit(
            (
                parsed.scheme.casefold(),
                netloc,
                "" if strip_path else parsed.path,
                "",
                "",
            )
        )
    except (UnicodeError, ValueError):
        sanitized = "[redacted URL]"
    return sanitized + trailing


def _sanitize_urls_in_text(text: str, *, strip_path: bool = False) -> str:
    return _HIERARCHICAL_URL_RE.sub(
        lambda match: _sanitize_url_match(match, strip_path=strip_path),
        str(text or ""),
    )


def _is_endpoint_url_field(key: object) -> bool:
    normalized = _normalize_export_key(key)
    return normalized == "baseurl" or normalized.endswith("baseurl")


def _sanitize_exported_freeform_text(
    text: str | Path | None,
    *,
    workspace_root: Path | None,
    _depth: int = 0,
) -> str:
    if _depth >= _MAX_REDACTION_DEPTH:
        return _NESTED_EXPORT_OMITTED
    raw = str(text or "")
    # Tool arguments and file contents can each be JSON encoded inside another
    # JSON string. Inspect decoded fields before applying text patterns: escaped
    # quotes (and escaped key names) otherwise hide credential assignments.
    if raw.lstrip().startswith(("{", "[", '"')):
        try:
            decoded = json.loads(raw)
        except RecursionError:
            return _NESTED_EXPORT_OMITTED
        except ValueError:
            pass  # Ordinary prose still uses the free-form scrubber below.
        else:
            return json.dumps(
                _redact_jsonable(decoded, workspace_root=workspace_root, _depth=_depth + 1),
                ensure_ascii=True,
            )
    # Free-form exported strings can still contain absolute host paths even when
    # the surrounding JSON field is not path-shaped (for example payload.content
    # or user-authored feedback text), so bundle serialization must sanitize
    # text values separately from explicit path fields.
    without_host_paths = sanitize_paths_in_text(raw, workspace_root=workspace_root)
    # Support bundles are shared outside the live session boundary. Keep only
    # scheme + sanitized host (+ optional port) for hierarchical URLs: signed
    # path segments can be just as sensitive as userinfo and query values.
    return _redact_bundle_text(_sanitize_urls_in_text(without_host_paths, strip_path=True))


def _normalize_export_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _is_provider_reasoning_block(value: Any) -> bool:
    """Return whether a mapping is a provider-owned reasoning/thought block.

    The check intentionally uses protocol-level markers shared by OpenAI,
    Anthropic, Gemini, OpenRouter, and Mistral rather than provider names, so an
    unknown compatible provider fails closed too.
    """

    if not isinstance(value, dict):
        return False
    block_type = _normalize_export_key(value.get("type"))
    if block_type in _REASONING_BLOCK_TYPES:
        return True
    if block_type == "signaturedelta":
        return True
    if any(family in block_type for family in _REASONING_FAMILIES) and (
        any(marker in block_type for marker in _REASONING_SENSITIVE_MARKERS)
        or block_type.endswith(("block", "chunk", "delta", "item", "part"))
    ):
        return True
    return value.get("thought") is True


def _is_reasoning_payload_key(key: object) -> bool:
    normalized = _normalize_export_key(key)
    if not normalized:
        return False
    if normalized in _PROVIDER_CONTINUATION_CONTAINER_KEYS:
        return True
    if normalized in _OPAQUE_CONTINUATION_KEYS:
        return True
    if "chainofthought" in normalized or "internalmonologue" in normalized:
        return True
    if not any(family in normalized for family in _REASONING_FAMILIES):
        return False
    # Content-bearing markers always win over diagnostic markers. For example,
    # `reasoning_summary_tokens` still identifies summary content and is removed,
    # while `reasoning_tokens` and `reasoning_effort` remain useful diagnostics.
    if any(marker in normalized for marker in _REASONING_SENSITIVE_MARKERS):
        return True
    if normalized in {"reasoning", "thinking", "thought", "thoughts"}:
        return True
    if any(marker in normalized for marker in _REASONING_DIAGNOSTIC_MARKERS):
        return False
    # Unknown content-like fields fail closed; an explicitly diagnostic field
    # must carry one of the allowlisted markers above.
    return True


def _redact_jsonable(
    value: Any,
    *,
    current_key: str | None = None,
    workspace_root: Path | None = None,
    _depth: int = 0,
) -> Any:
    # Match the field before descending: after JSON parsing, a naked value
    # no longer contains the key/assignment syntax used by the text scanner.
    if _is_secret_export_key(current_key) and value is not None:
        return "[REDACTED]"
    if _depth >= _MAX_REDACTION_DEPTH:
        return _NESTED_EXPORT_OMITTED
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        if _is_endpoint_url_field(current_key):
            return _redact_bundle_text(_sanitize_urls_in_text(value, strip_path=True))
        if looks_like_serialized_path_field(current_key):
            return _redact_bundle_text(
                safe_serialized_path_field(
                    current_key or "",
                    value,
                    workspace_root=workspace_root,
                )
                or ""
            )
        # Keep path-shaped fields on the precise structured serializer, then use
        # best-effort text sanitization for generic strings so exported JSON and
        # JSONL artifacts do not leak absolute host paths via free-form text.
        return _sanitize_exported_freeform_text(value, workspace_root=workspace_root, _depth=_depth)
    if isinstance(value, Path):
        if looks_like_serialized_path_field(current_key):
            return _redact_bundle_text(
                safe_serialized_path_field(
                    current_key or "",
                    value,
                    workspace_root=workspace_root,
                )
                or ""
            )
        return _sanitize_exported_freeform_text(
            str(value), workspace_root=workspace_root, _depth=_depth
        )
    if isinstance(value, dict):
        if _is_provider_reasoning_block(value):
            return {}
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_reasoning_payload_key(key_text) or _is_provider_reasoning_block(item):
                continue
            sanitized[key_text] = _redact_jsonable(
                item,
                current_key=key_text,
                workspace_root=workspace_root,
                _depth=_depth + 1,
            )
        return sanitized
    if isinstance(value, (list, tuple)):
        return [
            _redact_jsonable(
                item,
                current_key=current_key,
                workspace_root=workspace_root,
                _depth=_depth + 1,
            )
            for item in value
            if not _is_provider_reasoning_block(item)
        ]
    return _sanitize_exported_freeform_text(
        str(value), workspace_root=workspace_root, _depth=_depth
    )


def _sanitize_json_text(text: str, *, workspace_root: Path) -> str | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return (
        json.dumps(
            _redact_jsonable(payload, workspace_root=workspace_root),
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
        )
        + "\n"
    )


def _sanitize_jsonl_text(text: str, *, workspace_root: Path) -> str | None:
    lines: list[str] = []
    for raw_line in text.splitlines():
        _checkpoint()
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            return None
        # JSONL bundle exports must sanitize both explicit path fields and
        # generic free-form strings because session events often nest paths
        # inside payload.content/message-style fields.
        lines.append(
            json.dumps(
                _redact_jsonable(payload, workspace_root=workspace_root),
                sort_keys=True,
                ensure_ascii=True,
            )
        )
    return ("\n".join(lines) + "\n") if lines else ""


def _read_text_if_utf8(path: Path) -> str | None:
    try:
        chunks: list[str] = []
        with _io_path(path).open(encoding="utf-8") as stream:
            while chunk := stream.read(1024 * 1024):
                _checkpoint()
                chunks.append(chunk)
        return "".join(chunks)
    except (UnicodeDecodeError, OSError):
        return None


def _now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _now_iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
