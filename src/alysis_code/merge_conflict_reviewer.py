from __future__ import annotations

import json
import re
import subprocess
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .command_text import command_text
from .config import (
    AppConfig,
    ConfigError,
    get_api_key,
    resolve_llm_timeout_s,
    resolve_model_access_api_key,
    resolve_prompt_cache_key,
    resolve_prompt_cache_retention,
    resolve_role_temperature,
)
from .execution_shared import safe_task_file_component
from .failure_category import classify_failure_category
from .forge import RunPaths, ensure_execution_dirs, now_iso
from .git_safe import build_git_cmd
from .llm.base import ChatClient
from .llm.factory import _resolve_base_url, make_llm_client
from .llm.openai_compat import OpenAICompatClient as _OpenAICompatClient
from .llm.types import LLMError
from .model_metadata_policy import (
    ActiveModelRef,
    ModelMetadataPolicyError,
    evaluate_active_model_metadata_policy,
)
from .model_registry import ModelRegistry
from .model_router import ROLE_CONFLICT_REVIEW, resolve_model_for_role
from .profiles import get_active_profile

_ALLOWED_RESOLUTION = {"take_ours", "take_theirs", "manual_merge"}
_ALLOWED_CONFIDENCE = {"high", "medium", "low"}
_ALLOWED_RISK = {"low", "medium", "high"}
_CONFLICT_REVIEW_TRANSIENT_REQUEST_MAX_ATTEMPTS = 2
_RETRYABLE_LLM_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
_RETRYABLE_LLM_REQUEST_ERROR_MARKERS = (
    "connection aborted",
    "connection reset",
    "connect timeout",
    "connecttimeout",
    "eof",
    "pool timeout",
    "read error",
    "read timeout",
    "readtimeout",
    "remote protocol error",
    "temporarily unavailable",
    "timed out",
)
_REQUEST_RETRY_STATE_NONE = "none"
_REQUEST_RETRY_STATE_RECOVERED = "recovered"
_REQUEST_RETRY_STATE_EXHAUSTED = "exhausted"
_REQUEST_RETRY_STATE_FINAL_FAILURE_AFTER_RETRY = "final_failure_after_retry"

# Compatibility surface for tests and downstream monkeypatches that predate the LLM factory.
OpenAICompatClient = _OpenAICompatClient

MERGE_CONFLICT_REVIEWER_SYSTEM_PROMPT = """You are a strict merge-conflict reviewer.

Goal
- Analyze the merge conflict context (base/ours/theirs/working excerpts) and recommend the safest resolution strategy.
- Be conservative: when uncertain, recommend manual_merge and explain why.

Guidelines
- Prefer minimal, low-risk merges that preserve intended behavior.
- Consider task acceptance criteria and write scope constraints if provided.
- Call out risks (build breaks, semantic changes, missing tests, formatting traps).

Output
- Return valid JSON only, strictly matching the schema requested by the user prompt.
"""


@dataclass(frozen=True)
class ConflictReviewOutcome:
    review_json: dict[str, Any] | None
    review_markdown: str
    skipped_reason: str | None
    request_retry_count: int = 0
    request_retry_state: str = _REQUEST_RETRY_STATE_NONE
    failure_category: str | None = None


@dataclass(frozen=True)
class ConflictArtifactPaths:
    conflict_dir: Path
    context_json_path: Path
    review_json_path: Path | None
    review_md_path: Path
    cleanup_log_path: Path


def _run_git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        build_git_cmd(root, args),
        check=False,
        capture_output=True,
        **command_text(),
    )


def _truncate_head_tail(text: str, *, max_chars: int = 8000) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    omitted = len(text) - max_chars
    return text[:half] + f"\n\n... [truncated {omitted} chars] ...\n\n" + text[-half:]


def _read_text_excerpt(path: Path, *, max_chars: int = 8000) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return _truncate_head_tail(content, max_chars=max_chars)


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        sliced = text[start : end + 1]
        parsed = json.loads(sliced)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("model response is not a valid JSON object")


def _normalize_per_file(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("per_file must be a list")
    out: list[dict[str, str]] = []
    for idx, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"per_file[{idx}] must be an object")
        path = str(item.get("path") or "").strip()
        recommended_resolution = str(item.get("recommended_resolution") or "").strip().lower()
        notes = str(item.get("notes") or "").strip()
        risk = str(item.get("risk") or "").strip().lower()
        if recommended_resolution not in _ALLOWED_RESOLUTION:
            raise ValueError(
                f"per_file[{idx}].recommended_resolution must be one of "
                "take_ours|take_theirs|manual_merge"
            )
        if risk not in _ALLOWED_RISK:
            raise ValueError(f"per_file[{idx}].risk must be one of low|medium|high")
        out.append(
            {
                "path": path or "(unknown)",
                "recommended_resolution": recommended_resolution,
                "notes": notes or "(no notes)",
                "risk": risk,
            }
        )
    return out


def _normalize_review_json(raw: dict[str, Any], *, task_id: str) -> dict[str, Any]:
    confidence = str(raw.get("confidence") or "").strip().lower()
    summary = str(raw.get("summary") or "").strip()
    root_cause = str(raw.get("root_cause") or "").strip()
    strategy = str(raw.get("recommended_strategy") or "").strip()

    if confidence not in _ALLOWED_CONFIDENCE:
        raise ValueError("confidence must be high|medium|low")
    if not summary:
        raise ValueError("summary is required")
    if not root_cause:
        raise ValueError("root_cause is required")
    if not strategy:
        raise ValueError("recommended_strategy is required")

    next_steps_raw = raw.get("next_steps")
    if not isinstance(next_steps_raw, list):
        raise ValueError("next_steps must be a list")
    next_steps = [str(item).strip() for item in next_steps_raw if str(item).strip()]

    return {
        "task_id": task_id,
        "confidence": confidence,
        "summary": summary,
        "root_cause": root_cause,
        "recommended_strategy": strategy,
        "per_file": _normalize_per_file(raw.get("per_file")),
        "next_steps": next_steps,
        "generated_at": now_iso(),
    }


def _llm_error_status_code(err: LLMError) -> int | None:
    match = re.match(r"LLM error (\d{3}):", str(err or "").strip())
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _is_retryable_conflict_review_request_error(err: LLMError) -> bool:
    status_code = _llm_error_status_code(err)
    if status_code is not None:
        return status_code in _RETRYABLE_LLM_HTTP_STATUSES

    message = str(err or "").casefold().strip()
    if not message.startswith("llm request failed:"):
        return False
    return any(marker in message for marker in _RETRYABLE_LLM_REQUEST_ERROR_MARKERS)


def _attach_conflict_review_retry_details(
    err: LLMError,
    *,
    retry_count: int,
    exhausted: bool,
) -> LLMError:
    err.conflict_review_request_retry_count = int(retry_count)  # type: ignore[attr-defined]
    err.conflict_review_request_retry_exhausted = bool(exhausted)  # type: ignore[attr-defined]
    return err


def _request_retry_state_for_terminal_outcome(
    *,
    request_retry_count: int,
    request_retry_exhausted: bool = False,
    skipped_reason: str | None,
) -> str:
    if request_retry_count <= 0:
        return _REQUEST_RETRY_STATE_NONE
    if not skipped_reason:
        return _REQUEST_RETRY_STATE_RECOVERED
    if request_retry_exhausted:
        return _REQUEST_RETRY_STATE_EXHAUSTED
    return _REQUEST_RETRY_STATE_FINAL_FAILURE_AFTER_RETRY


def _request_retry_line(*, retry_count: int, retry_state: str) -> str | None:
    if retry_count <= 0:
        return None
    retry_word = "retry" if retry_count == 1 else "retries"
    if retry_state == _REQUEST_RETRY_STATE_RECOVERED:
        return f"- Request Retries: {retry_count} transient {retry_word} before successful review."
    if retry_state == _REQUEST_RETRY_STATE_EXHAUSTED:
        return (
            f"- Request Retries: {retry_count} transient {retry_word} exhausted before review "
            "was skipped."
        )
    if retry_state == _REQUEST_RETRY_STATE_FINAL_FAILURE_AFTER_RETRY:
        return (
            f"- Request Retries: {retry_count} transient {retry_word} before final review failure."
        )
    return None


def _request_conflict_review_response(
    *,
    client: ChatClient,
    messages: list[dict[str, str]],
) -> tuple[Any, int]:
    retry_count = 0
    for attempt in range(1, _CONFLICT_REVIEW_TRANSIENT_REQUEST_MAX_ATTEMPTS + 1):
        try:
            return (
                client.chat(
                    messages=messages,
                    stream=False,
                ),
                retry_count,
            )
        except LLMError as e:
            retryable = _is_retryable_conflict_review_request_error(e)
            if not retryable or attempt >= _CONFLICT_REVIEW_TRANSIENT_REQUEST_MAX_ATTEMPTS:
                raise _attach_conflict_review_retry_details(
                    e,
                    retry_count=retry_count,
                    exhausted=retryable
                    and attempt >= _CONFLICT_REVIEW_TRANSIENT_REQUEST_MAX_ATTEMPTS,
                ) from e
            retry_count += 1
    raise AssertionError("unreachable")


def _make_conflict_review_llm_client(
    *,
    cfg: AppConfig,
    api_key: str,
    model_name: str,
) -> ChatClient:
    temperature = resolve_role_temperature(cfg, role="conflict_review")
    if OpenAICompatClient is _OpenAICompatClient:
        return make_llm_client(
            cfg=cfg,
            api_key=api_key,
            model=model_name,
            timeout_s=resolve_llm_timeout_s(cfg),
            temperature=temperature,
            prompt_cache_key=resolve_prompt_cache_key(cfg),
            prompt_cache_retention=resolve_prompt_cache_retention(cfg),
        )

    profile = get_active_profile(cfg)
    return OpenAICompatClient(
        base_url=_resolve_base_url(cfg=cfg, profile=profile),
        api_key=api_key,
        model=model_name,
        timeout_s=resolve_llm_timeout_s(cfg),
        temperature=temperature,
        prompt_cache_key=resolve_prompt_cache_key(cfg),
        prompt_cache_retention=resolve_prompt_cache_retention(cfg),
        extra_headers=profile.extra_headers,
    )


def _review_markdown(
    *,
    task: dict[str, Any],
    context: dict[str, Any],
    review: dict[str, Any] | None,
    skipped_reason: str | None,
    request_retry_count: int = 0,
    request_retry_state: str = _REQUEST_RETRY_STATE_NONE,
) -> str:
    task_id = str(task.get("id") or "")
    title = str(task.get("title") or "")
    lines: list[str] = [
        f"# Merge Conflict Review: {task_id}",
        "",
        f"- Task Title: {title}",
        f"- Generated At: {now_iso()}",
    ]
    retry_line = _request_retry_line(
        retry_count=request_retry_count,
        retry_state=request_retry_state,
    )
    if retry_line is not None:
        lines.append(retry_line)
    lines.append("")

    unmerged_files = context.get("unmerged_files")
    if isinstance(unmerged_files, list) and unmerged_files:
        lines.append("## Unmerged Files")
        lines.append("")
        for item in unmerged_files:
            lines.append(f"- `{item}`")
        lines.append("")

    if skipped_reason:
        lines.extend(
            [
                "## Review Status",
                "",
                f"LLM conflict review skipped: {skipped_reason}",
                "",
                "## Suggested Next Steps",
                "",
                "- Resolve conflicts manually in listed files.",
                "- Commit the resolution on the task branch.",
                "- Retry merge after resolution.",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"

    assert review is not None
    lines.extend(
        [
            "## Summary",
            "",
            review["summary"],
            "",
            "## Root Cause",
            "",
            review["root_cause"],
            "",
            "## Recommended Strategy",
            "",
            review["recommended_strategy"],
            "",
            f"- Confidence: {review['confidence']}",
            "",
            "## Per-file Resolution",
            "",
        ]
    )
    if review["per_file"]:
        for entry in review["per_file"]:
            lines.append(
                f"- `{entry['path']}` -> {entry['recommended_resolution']} "
                f"(risk={entry['risk']}): {entry['notes']}"
            )
    else:
        lines.append("- (none)")

    lines.extend(["", "## Next Steps", ""])
    if review["next_steps"]:
        for step in review["next_steps"]:
            lines.append(f"- {step}")
    else:
        lines.append("- Resolve conflicts manually and retry merge.")

    return "\n".join(lines).rstrip() + "\n"


def list_unmerged_files(root: Path) -> list[str]:
    cp = _run_git(root, ["diff", "--name-only", "--diff-filter=U"])
    if cp.returncode != 0:
        return []
    return [line.strip() for line in cp.stdout.splitlines() if line.strip()]


def read_stage_blob(root: Path, stage: int, path: str) -> str:
    if stage not in {1, 2, 3}:
        raise ValueError("stage must be one of: 1, 2, 3")
    cp = _run_git(root, ["show", f":{stage}:{path}"])
    if cp.returncode != 0:
        return ""
    return cp.stdout


def capture_merge_conflict_context(
    root: Path,
    *,
    base_branch: str,
    task_branch: str,
    merge_error: str,
) -> dict[str, Any]:
    status_cp = _run_git(root, ["status", "--porcelain=v1"])
    git_status = status_cp.stdout if status_cp.returncode == 0 else (status_cp.stderr or "")

    files = list_unmerged_files(root)
    per_file: list[dict[str, str]] = []
    for path in files:
        working_excerpt = _read_text_excerpt(root / path)
        per_file.append(
            {
                "path": path,
                "base_version_excerpt": _truncate_head_tail(read_stage_blob(root, 1, path)),
                "ours_version_excerpt": _truncate_head_tail(read_stage_blob(root, 2, path)),
                "theirs_version_excerpt": _truncate_head_tail(read_stage_blob(root, 3, path)),
                "working_excerpt": working_excerpt,
            }
        )

    return {
        "base_branch": base_branch,
        "task_branch": task_branch,
        "merge_error": merge_error,
        "git_status_porcelain": _truncate_head_tail(git_status, max_chars=12000),
        "unmerged_files": files,
        "files": per_file,
        "captured_at": now_iso(),
    }


def try_abort_merge(root: Path, *, base_branch: str) -> tuple[bool, str]:
    logs: list[str] = []

    def _record(cp: subprocess.CompletedProcess[str], cmd: str) -> None:
        out = (cp.stdout or "").strip()
        err = (cp.stderr or "").strip()
        logs.append(f"$ {cmd}")
        logs.append(f"returncode={cp.returncode}")
        if out:
            logs.append("stdout:")
            logs.append(out)
        if err:
            logs.append("stderr:")
            logs.append(err)
        logs.append("")

    abort_cp = _run_git(root, ["merge", "--abort"])
    _record(abort_cp, f"git -C {root} merge --abort")
    abort_ok = abort_cp.returncode == 0

    reset_ok = False
    if not abort_ok:
        reset_cp = _run_git(root, ["reset", "--hard"])
        _record(reset_cp, f"git -C {root} reset --hard")
        reset_ok = reset_cp.returncode == 0

    checkout_cp = _run_git(root, ["checkout", base_branch])
    _record(checkout_cp, f"git -C {root} checkout {base_branch}")
    checkout_ok = checkout_cp.returncode == 0

    ok = (abort_ok or reset_ok) and checkout_ok
    return ok, "\n".join(logs).rstrip() + "\n"


def review_merge_conflict(
    *,
    paths: RunPaths,
    task: dict[str, Any],
    cfg: AppConfig,
    api_key_override: str | None,
    context: dict[str, Any],
    plan: dict[str, Any] | None = None,
) -> ConflictReviewOutcome:
    task_id = str(task.get("id") or "").strip() or "(unknown)"

    try:
        api_key = resolve_model_access_api_key(
            cfg,
            override=api_key_override,
            legacy_resolver=get_api_key,
        )
    except ConfigError as exc:
        reason = str(exc)
        review_md = _review_markdown(
            task=task,
            context=context,
            review=None,
            skipped_reason=reason,
            request_retry_count=0,
            request_retry_state=_REQUEST_RETRY_STATE_NONE,
        )
        return ConflictReviewOutcome(
            review_json=None,
            review_markdown=review_md,
            skipped_reason=reason,
            request_retry_count=0,
            request_retry_state=_REQUEST_RETRY_STATE_NONE,
        )

    prompt_payload = {
        "task": {
            "id": task.get("id", ""),
            "title": task.get("title", ""),
            "description": task.get("description", ""),
            "acceptance_criteria": task.get("acceptance_criteria", []),
            "write_scope": task.get("write_scope", []),
            "estimated_files": task.get("estimated_files", []),
            "branch": task.get("branch", ""),
        },
        "conflict_context": context,
    }
    schema_hint = {
        "task_id": task_id,
        "confidence": "high|medium|low",
        "summary": "string",
        "root_cause": "string",
        "recommended_strategy": "string",
        "per_file": [
            {
                "path": "src/x.py",
                "recommended_resolution": "take_ours|take_theirs|manual_merge",
                "notes": "string",
                "risk": "low|medium|high",
            }
        ],
        "next_steps": ["string"],
    }

    prompt = (
        "Analyze this git merge conflict and return STRICT JSON only.\n\n"
        "Rubric:\n"
        "- root cause of conflict\n"
        "- safest resolution strategy per file\n"
        "- risks of taking ours/theirs\n"
        "- concrete next steps\n\n"
        f"Input:\n{json.dumps(prompt_payload, ensure_ascii=True, indent=2)}\n\n"
        f"Required schema:\n{json.dumps(schema_hint, ensure_ascii=True, indent=2)}\n"
    )
    model_name = resolve_model_for_role(
        cfg=cfg,
        role=ROLE_CONFLICT_REVIEW,
        plan=plan,
        prefer_context="forge",
    )
    registry = ModelRegistry(cfg=cfg, api_key=api_key)
    try:
        metadata_policy_result = evaluate_active_model_metadata_policy(
            cfg=cfg,
            registry=registry,
            active_models=[ActiveModelRef(role=ROLE_CONFLICT_REVIEW, model_name=model_name)],
        )
    except ModelMetadataPolicyError as e:
        raise ConfigError(str(e)) from e
    for warning_message in metadata_policy_result.warning_messages:
        warnings.warn(warning_message, stacklevel=2)

    client = _make_conflict_review_llm_client(
        cfg=cfg,
        api_key=api_key,
        model_name=model_name,
    )
    request_retry_count = 0
    try:
        response, request_retry_count = _request_conflict_review_response(
            client=client,
            messages=[
                {
                    "role": "system",
                    "content": MERGE_CONFLICT_REVIEWER_SYSTEM_PROMPT,
                },
                {"role": "user", "content": prompt},
            ],
        )
        raw = _extract_json_object(response.content)
        review_json = _normalize_review_json(raw, task_id=task_id)
        review_md = _review_markdown(
            task=task,
            context=context,
            review=review_json,
            skipped_reason=None,
            request_retry_count=request_retry_count,
            request_retry_state=_request_retry_state_for_terminal_outcome(
                request_retry_count=request_retry_count,
                skipped_reason=None,
            ),
        )
        return ConflictReviewOutcome(
            review_json=review_json,
            review_markdown=review_md,
            skipped_reason=None,
            request_retry_count=request_retry_count,
            request_retry_state=_request_retry_state_for_terminal_outcome(
                request_retry_count=request_retry_count,
                skipped_reason=None,
            ),
        )
    except LLMError as e:
        request_retry_count = int(getattr(e, "conflict_review_request_retry_count", 0) or 0)
        request_retry_exhausted = bool(
            getattr(e, "conflict_review_request_retry_exhausted", False) or False
        )
        retry_word = "retry" if request_retry_count == 1 else "retries"
        reason = f"review failed: {e}"
        if request_retry_count > 0:
            reason = f"review failed after {request_retry_count} transient {retry_word}: {e}"
        retry_state = _request_retry_state_for_terminal_outcome(
            request_retry_count=request_retry_count,
            request_retry_exhausted=request_retry_exhausted,
            skipped_reason=reason,
        )
        review_md = _review_markdown(
            task=task,
            context=context,
            review=None,
            skipped_reason=reason,
            request_retry_count=request_retry_count,
            request_retry_state=retry_state,
        )
        return ConflictReviewOutcome(
            review_json=None,
            review_markdown=review_md,
            skipped_reason=reason,
            request_retry_count=request_retry_count,
            request_retry_state=retry_state,
            failure_category=classify_failure_category(e).value,
        )
    except (ValueError, json.JSONDecodeError) as e:
        reason = f"review failed: {e}"
        retry_state = _request_retry_state_for_terminal_outcome(
            request_retry_count=request_retry_count,
            skipped_reason=reason,
        )
        review_md = _review_markdown(
            task=task,
            context=context,
            review=None,
            skipped_reason=reason,
            request_retry_count=request_retry_count,
            request_retry_state=retry_state,
        )
        return ConflictReviewOutcome(
            review_json=None,
            review_markdown=review_md,
            skipped_reason=reason,
            request_retry_count=request_retry_count,
            request_retry_state=retry_state,
        )


def write_conflict_artifacts(
    *,
    paths: RunPaths,
    task_id: str,
    context: dict[str, Any],
    review_json: dict[str, Any] | None,
    review_md: str,
    cleanup_log: str,
) -> ConflictArtifactPaths:
    ensure_execution_dirs(paths)
    conflict_root = paths.execution_dir / "conflicts"
    conflict_root.mkdir(parents=True, exist_ok=True)

    safe = safe_task_file_component(task_id)
    out_dir = conflict_root / safe
    out_dir.mkdir(parents=True, exist_ok=True)

    context_json_path = out_dir / "conflict_context.json"
    review_json_path = out_dir / "conflict_review.json" if review_json is not None else None
    review_md_path = out_dir / "conflict_review.md"
    cleanup_log_path = out_dir / "merge_cleanup.log"

    context_json_path.write_text(
        json.dumps(context, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if review_json_path is not None:
        review_json_path.write_text(
            json.dumps(review_json, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    review_md_path.write_text(review_md.rstrip() + "\n", encoding="utf-8")
    cleanup_log_path.write_text(cleanup_log.rstrip() + "\n", encoding="utf-8")

    return ConflictArtifactPaths(
        conflict_dir=out_dir,
        context_json_path=context_json_path,
        review_json_path=review_json_path,
        review_md_path=review_md_path,
        cleanup_log_path=cleanup_log_path,
    )
