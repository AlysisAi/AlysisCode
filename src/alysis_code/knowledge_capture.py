from __future__ import annotations

import json
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .forge import RunPaths
from .knowledge_base import write_decision_entry, write_fact_entry
from .surface.base import Surface
from .surface.noop_surface import NoopSurface

KNOWLEDGE_CAPTURE_SCHEMA_VERSION = 1
KNOWLEDGE_CAPTURE_FENCE = "knowledge_capture_json"
_CAPTURE_BLOCK_RE = re.compile(
    rf"```{KNOWLEDGE_CAPTURE_FENCE}\s*(\{{.*?\}})\s*```",
    re.DOTALL,
)
_ALLOWED_TOP_LEVEL_KEYS = frozenset({"schema_version", "facts", "decisions", "open_questions"})
_ALLOWED_FACT_KEYS = frozenset({"title", "summary", "paths", "tags"})
_ALLOWED_DECISION_KEYS = frozenset({"decision_key", "title", "summary", "status", "paths", "tags"})
_DECISION_STATUSES = frozenset({"active", "invalidated"})
_MAX_FACTS = 5
_MAX_DECISIONS = 4
_MAX_OPEN_QUESTIONS = 4
_MAX_TITLE_CHARS = 120
_MAX_SUMMARY_CHARS = 400
_MAX_PATHS = 8
_MAX_TAGS = 6
_MAX_TAG_CHARS = 32
_MAX_OPEN_QUESTION_CHARS = 160
_MAX_DECISION_KEY_CHARS = 64
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_DECISION_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _safe_component(value: str) -> str:
    safe = "".join(c if c.isalnum() or c in {"-", "_"} else "_" for c in str(value).strip())
    return safe or "item"


def _repo_rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _normalize_repo_rel_path(value: str) -> str | None:
    cleaned = str(value).strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = cleaned.strip().rstrip("/")
    if not cleaned:
        return None
    if cleaned.startswith("/"):
        return None
    if cleaned == ".." or cleaned.startswith("../") or "/../" in cleaned:
        return None
    if cleaned == ".alysis" or cleaned.startswith(".alysis/"):
        return None
    return cleaned


def _bounded_text(value: Any, *, field: str, max_chars: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} must be non-empty")
    if len(text) > max_chars:
        raise ValueError(f"{field} exceeds {max_chars} chars")
    return text


def _normalize_tags(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("tags must be an array")
    tags: list[str] = []
    for raw in value[:_MAX_TAGS]:
        tag = str(raw or "").strip().lower()
        if not tag:
            continue
        if len(tag) > _MAX_TAG_CHARS:
            raise ValueError(f"tag exceeds {_MAX_TAG_CHARS} chars: {tag}")
        if not _TAG_RE.match(tag):
            raise ValueError(f"invalid tag: {tag}")
        if tag not in tags:
            tags.append(tag)
    return tuple(tags)


def _normalize_paths(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("paths must be an array")
    paths: list[str] = []
    for raw in value[:_MAX_PATHS]:
        normalized = _normalize_repo_rel_path(str(raw or ""))
        if normalized is None:
            raise ValueError(f"invalid repo-relative path: {raw}")
        if normalized not in paths:
            paths.append(normalized)
    return tuple(paths)


def _normalize_open_questions(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("open_questions must be an array")
    out: list[str] = []
    for raw in value[:_MAX_OPEN_QUESTIONS]:
        text = str(raw or "").strip()
        if not text:
            continue
        if len(text) > _MAX_OPEN_QUESTION_CHARS:
            raise ValueError(f"open question exceeds {_MAX_OPEN_QUESTION_CHARS} chars: {text[:40]}")
        out.append(text)
    return tuple(out)


def _emit_surface_warning(
    surface: object,
    message: str,
    *,
    worker_id: str | None = None,
    role: str | None = None,
) -> bool:
    surface_cls = getattr(surface, "__class__", None)
    handler = getattr(surface, "emit_warning", None)
    if callable(handler):
        cls_handler = getattr(surface_cls, "emit_warning", None)
        if cls_handler is not getattr(NoopSurface, "emit_warning", None):
            handler(message, worker_id=worker_id, role=role)
            return True
    handler = getattr(surface, "on_warning", None)
    if not callable(handler):
        return False
    cls_handler = getattr(surface_cls, "on_warning", None)
    if cls_handler is getattr(NoopSurface, "on_warning", None):
        return False
    handler(message)
    return True


@dataclass(frozen=True)
class CapturedFact:
    title: str
    summary: str
    paths: tuple[str, ...]
    tags: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "paths": list(self.paths),
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class CapturedDecision:
    decision_key: str
    title: str
    summary: str
    status: str
    paths: tuple[str, ...]
    tags: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "decision_key": self.decision_key,
            "title": self.title,
            "summary": self.summary,
            "status": self.status,
            "paths": list(self.paths),
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class KnowledgeCapturePayload:
    schema_version: int
    facts: tuple[CapturedFact, ...]
    decisions: tuple[CapturedDecision, ...]
    open_questions: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "facts": [item.to_payload() for item in self.facts],
            "decisions": [item.to_payload() for item in self.decisions],
            "open_questions": list(self.open_questions),
        }


@dataclass(frozen=True)
class KnowledgeCaptureValidation:
    valid: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    payload: KnowledgeCapturePayload | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "parsed_capture": self.payload.to_payload() if self.payload is not None else None,
        }


@dataclass(frozen=True)
class PersistedKnowledgeCapture:
    artifact_dir: Path
    assistant_message_path: Path
    capture_block_path: Path | None
    parsed_capture_path: Path | None
    validation_path: Path
    promotion_path: Path
    summary_path: Path
    valid: bool


@dataclass(frozen=True)
class KnowledgeCapturePromotionResult:
    artifact_dir: Path
    promotion_path: Path
    capture_valid: bool
    promotion_attempted: bool
    promotion_succeeded: bool
    promotion_skipped_reason: str | None
    promotion_errors: tuple[str, ...]
    fact_entry_ids: tuple[str, ...]
    decision_entry_ids: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "capture_valid": self.capture_valid,
            "promotion_attempted": self.promotion_attempted,
            "promotion_succeeded": self.promotion_succeeded,
            "promotion_skipped_reason": self.promotion_skipped_reason,
            "promotion_errors": list(self.promotion_errors),
            "fact_entry_ids": list(self.fact_entry_ids),
            "decision_entry_ids": list(self.decision_entry_ids),
        }


def _load_json_payload(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"expected JSON object in {path}")
    return raw


def _promotion_result_from_payload(
    *,
    artifact_dir: Path,
    promotion_path: Path,
    payload: dict[str, Any],
) -> KnowledgeCapturePromotionResult:
    return KnowledgeCapturePromotionResult(
        artifact_dir=artifact_dir,
        promotion_path=promotion_path,
        capture_valid=bool(payload.get("capture_valid")),
        promotion_attempted=bool(payload.get("promotion_attempted")),
        promotion_succeeded=bool(payload.get("promotion_succeeded")),
        promotion_skipped_reason=str(payload.get("promotion_skipped_reason") or "").strip() or None,
        promotion_errors=tuple(
            str(item).strip() for item in payload.get("promotion_errors") or [] if str(item).strip()
        ),
        fact_entry_ids=tuple(
            str(item).strip() for item in payload.get("fact_entry_ids") or [] if str(item).strip()
        ),
        decision_entry_ids=tuple(
            str(item).strip()
            for item in payload.get("decision_entry_ids") or []
            if str(item).strip()
        ),
    )


def _write_promotion_result(
    *,
    artifact_dir: Path,
    promotion_path: Path,
    capture_valid: bool,
    promotion_attempted: bool,
    promotion_succeeded: bool,
    promotion_skipped_reason: str | None,
    promotion_errors: list[str] | tuple[str, ...],
    fact_entry_ids: list[str] | tuple[str, ...],
    decision_entry_ids: list[str] | tuple[str, ...],
) -> KnowledgeCapturePromotionResult:
    result = KnowledgeCapturePromotionResult(
        artifact_dir=artifact_dir,
        promotion_path=promotion_path,
        capture_valid=capture_valid,
        promotion_attempted=promotion_attempted,
        promotion_succeeded=promotion_succeeded,
        promotion_skipped_reason=str(promotion_skipped_reason or "").strip() or None,
        promotion_errors=tuple(str(item).strip() for item in promotion_errors if str(item).strip()),
        fact_entry_ids=tuple(str(item).strip() for item in fact_entry_ids if str(item).strip()),
        decision_entry_ids=tuple(
            str(item).strip() for item in decision_entry_ids if str(item).strip()
        ),
    )
    _write_json(promotion_path, result.to_payload())
    return result


def _load_promotion_result(artifact_dir: Path) -> KnowledgeCapturePromotionResult | None:
    promotion_path = artifact_dir / "promotion.json"
    if not promotion_path.exists():
        return None
    try:
        payload = _load_json_payload(promotion_path)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    return _promotion_result_from_payload(
        artifact_dir=artifact_dir,
        promotion_path=promotion_path,
        payload=payload,
    )


class RecordingSurface:
    def __init__(self, delegate: Surface) -> None:
        self._delegate = delegate
        self.final_assistant_message = ""

    @property
    def canonical_message_tool_events(self) -> bool:
        """Preserve the delegate's single-delivery event contract."""
        return bool(getattr(self._delegate, "canonical_message_tool_events", False))

    def on_status_update(self, status) -> None:  # type: ignore[no-untyped-def]
        self._delegate.on_status_update(status)

    def emit_status_update(
        self,
        *,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cached_tokens: int | None = None,
        cost_usd: float | None = None,
        mode: str | None = None,
        model: str | None = None,
        step: int | None = None,
        step_budget: int | None = None,
    ) -> None:
        handler = getattr(self._delegate, "emit_status_update", None)
        if callable(handler):
            handler(
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cached_tokens=cached_tokens,
                cost_usd=cost_usd,
                mode=mode,
                model=model,
                step=step,
                step_budget=step_budget,
            )

    def on_user_message(self, text: str) -> None:
        self._delegate.on_user_message(text)

    def on_progress_update(self, message: str) -> None:
        self._delegate.on_progress_update(message)

    def on_assistant_token(self, delta: str) -> None:
        self._delegate.on_assistant_token(delta)

    def on_assistant_message_done(self, text: str) -> None:
        self.final_assistant_message = str(text or "")
        self._delegate.on_assistant_message_done(text)

    def emit_message_delta(
        self,
        text: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        handler = getattr(self._delegate, "emit_message_delta", None)
        if callable(handler):
            handler(text, worker_id=worker_id, role=role)

    def emit_message_end(
        self,
        text: str = "",
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self.final_assistant_message = str(text or "")
        handler = getattr(self._delegate, "emit_message_end", None)
        if callable(handler):
            handler(text, worker_id=worker_id, role=role)

    def on_subagent_start(self, event) -> None:  # type: ignore[no-untyped-def]
        handler = getattr(self._delegate, "on_subagent_start", None)
        if callable(handler):
            handler(event)

    def on_subagent_end(self, event) -> None:  # type: ignore[no-untyped-def]
        handler = getattr(self._delegate, "on_subagent_end", None)
        if callable(handler):
            handler(event)

    def on_tool_start(self, event) -> None:  # type: ignore[no-untyped-def]
        self._delegate.on_tool_start(event)

    def on_tool_output(self, event) -> None:  # type: ignore[no-untyped-def]
        self._delegate.on_tool_output(event)

    def on_tool_end(self, event) -> None:  # type: ignore[no-untyped-def]
        self._delegate.on_tool_end(event)

    def emit_tool_call_started(
        self,
        call_id: str,
        name: str,
        arguments_preview: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        handler = getattr(self._delegate, "emit_tool_call_started", None)
        if callable(handler):
            handler(
                call_id,
                name,
                arguments_preview,
                worker_id=worker_id,
                role=role,
            )

    def emit_tool_call_progress(
        self,
        call_id: str,
        text: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        handler = getattr(self._delegate, "emit_tool_call_progress", None)
        if callable(handler):
            handler(call_id, text, worker_id=worker_id, role=role)

    def emit_tool_call_completed(
        self,
        call_id: str,
        success: bool,
        result_preview: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        handler = getattr(self._delegate, "emit_tool_call_completed", None)
        if callable(handler):
            handler(
                call_id,
                success,
                result_preview,
                worker_id=worker_id,
                role=role,
            )

    def on_patch_generated(self, event) -> None:  # type: ignore[no-untyped-def]
        self._delegate.on_patch_generated(event)

    def on_warning(self, warning: str) -> None:
        self.emit_warning(warning)

    def emit_warning(
        self,
        message: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        if _emit_surface_warning(self._delegate, message, worker_id=worker_id, role=role):
            return
        warnings.warn(message, stacklevel=2)

    def on_error(self, err: str) -> None:
        self.emit_error("execution_error", err, True)

    def emit_error(
        self,
        code: str,
        message: str,
        recoverable: bool,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        delegate_cls = getattr(self._delegate, "__class__", None)
        handler = getattr(self._delegate, "emit_error", None)
        if callable(handler):
            cls_handler = getattr(delegate_cls, "emit_error", None)
            if cls_handler is not getattr(NoopSurface, "emit_error", None):
                handler(code, message, recoverable, worker_id=worker_id, role=role)
                return
        fallback = getattr(self._delegate, "on_error", None)
        if callable(fallback):
            fallback(message)

    def emit_info(
        self,
        message: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        delegate_cls = getattr(self._delegate, "__class__", None)
        handler = getattr(self._delegate, "emit_info", None)
        if callable(handler):
            cls_handler = getattr(delegate_cls, "emit_info", None)
            if cls_handler is not getattr(NoopSurface, "emit_info", None):
                handler(message, worker_id=worker_id, role=role)
                return
        fallback = getattr(self._delegate, "on_progress_update", None)
        if callable(fallback):
            fallback(message)

    def emit_approval_reviewed(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        handler = getattr(self._delegate, "emit_approval_reviewed", None)
        if callable(handler):
            handler(*args, **kwargs)

    def emit_mode_changed(self, mode: str) -> None:
        handler = getattr(self._delegate, "emit_mode_changed", None)
        if callable(handler):
            handler(mode)

    def emit_persona_changed(self, persona: str, effective_mode: str, source: str = "user") -> None:
        handler = getattr(self._delegate, "emit_persona_changed", None)
        if callable(handler):
            handler(persona, effective_mode, source)

    def request_approval(self, request):  # type: ignore[no-untyped-def]
        return self._delegate.request_approval(request)

    def standing_approval(self, request):  # type: ignore[no-untyped-def]
        standing = getattr(self._delegate, "standing_approval", None)
        return standing(request) if callable(standing) else None


def render_execution_knowledge_capture_rules() -> list[str]:
    return [
        "- Keep the normal human-readable final response.",
        f"- If you learned reusable repo knowledge, append one fenced `{KNOWLEDGE_CAPTURE_FENCE}` block after the summary.",
        "- Use JSON with `schema_version`, `facts`, `decisions`, and optional `open_questions`; omit the block if there is nothing durable to capture.",
        "- Facts are immutable observations with `title`, `summary`, optional repo-relative `paths`, and optional `tags`.",
        "- Decisions use `decision_key`, `title`, `summary`, `status` (`active` or `invalidated`), and optional repo-relative `paths`/`tags`.",
        f"- Keep it bounded: at most {_MAX_FACTS} facts, {_MAX_DECISIONS} decisions, and short strings.",
    ]


def extract_knowledge_capture_block(text: str) -> str | None:
    matches = list(_CAPTURE_BLOCK_RE.finditer(str(text or "")))
    if not matches:
        return None
    return matches[-1].group(1).strip()


def validate_knowledge_capture(*, final_text: str) -> KnowledgeCaptureValidation:
    clean = str(final_text or "").strip()
    if not clean:
        return KnowledgeCaptureValidation(
            valid=False,
            errors=("final assistant message was not captured",),
            warnings=(),
            payload=None,
        )

    block = extract_knowledge_capture_block(clean)
    if block is None:
        return KnowledgeCaptureValidation(
            valid=False,
            errors=(f"missing fenced `{KNOWLEDGE_CAPTURE_FENCE}` block",),
            warnings=(),
            payload=None,
        )

    try:
        raw = json.loads(block)
    except json.JSONDecodeError as exc:
        return KnowledgeCaptureValidation(
            valid=False,
            errors=(f"invalid structured knowledge JSON: {exc}",),
            warnings=(),
            payload=None,
        )

    if not isinstance(raw, dict):
        return KnowledgeCaptureValidation(
            valid=False,
            errors=("structured knowledge capture must be a JSON object",),
            warnings=(),
            payload=None,
        )

    unknown_top_level = sorted(set(raw) - _ALLOWED_TOP_LEVEL_KEYS)
    if unknown_top_level:
        return KnowledgeCaptureValidation(
            valid=False,
            errors=(f"unknown structured knowledge keys: {', '.join(unknown_top_level)}",),
            warnings=(),
            payload=None,
        )

    schema_version = raw.get("schema_version")
    if schema_version != KNOWLEDGE_CAPTURE_SCHEMA_VERSION:
        return KnowledgeCaptureValidation(
            valid=False,
            errors=(f"schema_version must be {KNOWLEDGE_CAPTURE_SCHEMA_VERSION}",),
            warnings=(),
            payload=None,
        )

    facts_raw = raw.get("facts", [])
    if not isinstance(facts_raw, list):
        return KnowledgeCaptureValidation(
            valid=False,
            errors=("facts must be an array",),
            warnings=(),
            payload=None,
        )
    if len(facts_raw) > _MAX_FACTS:
        return KnowledgeCaptureValidation(
            valid=False,
            errors=(f"facts exceeds max count {_MAX_FACTS}",),
            warnings=(),
            payload=None,
        )

    facts: list[CapturedFact] = []
    for idx, item in enumerate(facts_raw, start=1):
        if not isinstance(item, dict):
            return KnowledgeCaptureValidation(
                valid=False,
                errors=(f"fact #{idx} must be an object",),
                warnings=(),
                payload=None,
            )
        unknown_keys = sorted(set(item) - _ALLOWED_FACT_KEYS)
        if unknown_keys:
            return KnowledgeCaptureValidation(
                valid=False,
                errors=(f"fact #{idx} has unknown keys: {', '.join(unknown_keys)}",),
                warnings=(),
                payload=None,
            )
        try:
            facts.append(
                CapturedFact(
                    title=_bounded_text(
                        item.get("title"), field=f"fact #{idx} title", max_chars=_MAX_TITLE_CHARS
                    ),
                    summary=_bounded_text(
                        item.get("summary"),
                        field=f"fact #{idx} summary",
                        max_chars=_MAX_SUMMARY_CHARS,
                    ),
                    paths=_normalize_paths(item.get("paths")),
                    tags=_normalize_tags(item.get("tags")),
                )
            )
        except ValueError as exc:
            return KnowledgeCaptureValidation(
                valid=False,
                errors=(str(exc),),
                warnings=(),
                payload=None,
            )

    decisions_raw = raw.get("decisions", [])
    if not isinstance(decisions_raw, list):
        return KnowledgeCaptureValidation(
            valid=False,
            errors=("decisions must be an array",),
            warnings=(),
            payload=None,
        )
    if len(decisions_raw) > _MAX_DECISIONS:
        return KnowledgeCaptureValidation(
            valid=False,
            errors=(f"decisions exceeds max count {_MAX_DECISIONS}",),
            warnings=(),
            payload=None,
        )

    decisions: list[CapturedDecision] = []
    seen_decision_keys: set[str] = set()
    for idx, item in enumerate(decisions_raw, start=1):
        if not isinstance(item, dict):
            return KnowledgeCaptureValidation(
                valid=False,
                errors=(f"decision #{idx} must be an object",),
                warnings=(),
                payload=None,
            )
        unknown_keys = sorted(set(item) - _ALLOWED_DECISION_KEYS)
        if unknown_keys:
            return KnowledgeCaptureValidation(
                valid=False,
                errors=(f"decision #{idx} has unknown keys: {', '.join(unknown_keys)}",),
                warnings=(),
                payload=None,
            )
        try:
            decision_key = _bounded_text(
                item.get("decision_key"),
                field=f"decision #{idx} decision_key",
                max_chars=_MAX_DECISION_KEY_CHARS,
            ).lower()
        except ValueError as exc:
            return KnowledgeCaptureValidation(
                valid=False,
                errors=(str(exc),),
                warnings=(),
                payload=None,
            )
        if not _DECISION_KEY_RE.match(decision_key):
            return KnowledgeCaptureValidation(
                valid=False,
                errors=(f"invalid decision_key: {decision_key}",),
                warnings=(),
                payload=None,
            )
        if decision_key in seen_decision_keys:
            return KnowledgeCaptureValidation(
                valid=False,
                errors=(f"duplicate decision_key: {decision_key}",),
                warnings=(),
                payload=None,
            )
        seen_decision_keys.add(decision_key)
        status = str(item.get("status") or "").strip().lower()
        if status not in _DECISION_STATUSES:
            return KnowledgeCaptureValidation(
                valid=False,
                errors=(
                    f"decision #{idx} status must be one of: {', '.join(sorted(_DECISION_STATUSES))}",
                ),
                warnings=(),
                payload=None,
            )
        try:
            decisions.append(
                CapturedDecision(
                    decision_key=decision_key,
                    title=_bounded_text(
                        item.get("title"),
                        field=f"decision #{idx} title",
                        max_chars=_MAX_TITLE_CHARS,
                    ),
                    summary=_bounded_text(
                        item.get("summary"),
                        field=f"decision #{idx} summary",
                        max_chars=_MAX_SUMMARY_CHARS,
                    ),
                    status=status,
                    paths=_normalize_paths(item.get("paths")),
                    tags=_normalize_tags(item.get("tags")),
                )
            )
        except ValueError as exc:
            return KnowledgeCaptureValidation(
                valid=False,
                errors=(str(exc),),
                warnings=(),
                payload=None,
            )

    try:
        open_questions = _normalize_open_questions(raw.get("open_questions"))
    except ValueError as exc:
        return KnowledgeCaptureValidation(
            valid=False,
            errors=(str(exc),),
            warnings=(),
            payload=None,
        )

    return KnowledgeCaptureValidation(
        valid=True,
        errors=(),
        warnings=(),
        payload=KnowledgeCapturePayload(
            schema_version=KNOWLEDGE_CAPTURE_SCHEMA_VERSION,
            facts=tuple(facts),
            decisions=tuple(decisions),
            open_questions=open_questions,
        ),
    )


def _write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _repo_rel_optional(root: Path, path: Path | None) -> str | None:
    if path is None:
        return None
    return _repo_rel(root, path)


def _rooted_repo_path(root: Path, raw: Any) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    candidate = Path(text)
    if candidate.is_absolute():
        return candidate
    return root / candidate


def _build_capture_payload(raw: Any) -> KnowledgeCapturePayload:
    if not isinstance(raw, dict):
        raise ValueError("parsed structured capture must be a JSON object")
    facts: list[CapturedFact] = []
    for idx, item in enumerate(raw.get("facts") or [], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"fact #{idx} must be an object")
        facts.append(
            CapturedFact(
                title=_bounded_text(
                    item.get("title"),
                    field=f"fact #{idx} title",
                    max_chars=_MAX_TITLE_CHARS,
                ),
                summary=_bounded_text(
                    item.get("summary"),
                    field=f"fact #{idx} summary",
                    max_chars=_MAX_SUMMARY_CHARS,
                ),
                paths=_normalize_paths(item.get("paths")),
                tags=_normalize_tags(item.get("tags")),
            )
        )
    decisions: list[CapturedDecision] = []
    for idx, item in enumerate(raw.get("decisions") or [], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"decision #{idx} must be an object")
        status = str(item.get("status") or "").strip().lower()
        if status not in _DECISION_STATUSES:
            raise ValueError(
                f"decision #{idx} status must be one of: {', '.join(sorted(_DECISION_STATUSES))}"
            )
        decisions.append(
            CapturedDecision(
                decision_key=_bounded_text(
                    item.get("decision_key"),
                    field=f"decision #{idx} decision_key",
                    max_chars=_MAX_DECISION_KEY_CHARS,
                ).lower(),
                title=_bounded_text(
                    item.get("title"),
                    field=f"decision #{idx} title",
                    max_chars=_MAX_TITLE_CHARS,
                ),
                summary=_bounded_text(
                    item.get("summary"),
                    field=f"decision #{idx} summary",
                    max_chars=_MAX_SUMMARY_CHARS,
                ),
                status=status,
                paths=_normalize_paths(item.get("paths")),
                tags=_normalize_tags(item.get("tags")),
            )
        )
    return KnowledgeCapturePayload(
        schema_version=int(raw.get("schema_version") or KNOWLEDGE_CAPTURE_SCHEMA_VERSION),
        facts=tuple(facts),
        decisions=tuple(decisions),
        open_questions=_normalize_open_questions(raw.get("open_questions")),
    )


def persist_execution_knowledge_capture(
    *,
    paths: RunPaths,
    task: dict[str, Any],
    source: str,
    assistant_message: str | None,
    artifact_dir: Path,
    report_path: Path | None,
    patch_path: Path | None,
    verify_artifact_path: Path | None,
    budget_artifact_path: Path | None,
    session_artifact_dir: Path | None,
) -> PersistedKnowledgeCapture:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    assistant_message_path = _write_text(
        artifact_dir / "assistant_message.md",
        (str(assistant_message or "").rstrip() + "\n")
        if str(assistant_message or "").strip()
        else "(none captured)\n",
    )
    capture_block = extract_knowledge_capture_block(str(assistant_message or ""))
    capture_block_path = (
        _write_text(artifact_dir / "capture_block.txt", capture_block.rstrip() + "\n")
        if capture_block is not None
        else None
    )

    validation = validate_knowledge_capture(final_text=str(assistant_message or ""))
    parsed_capture_path = (
        _write_json(artifact_dir / "parsed_capture.json", validation.payload.to_payload())
        if validation.payload is not None
        else None
    )
    capture_summary_path = artifact_dir / "summary.md"
    validation_path = _write_json(
        artifact_dir / "validation.json",
        {
            "schema_version": KNOWLEDGE_CAPTURE_SCHEMA_VERSION,
            "task_id": str(task.get("id") or "").strip() or None,
            "source": source,
            "capture_present": capture_block is not None,
            "assistant_message_path": _repo_rel(paths.root, assistant_message_path),
            "capture_block_path": _repo_rel_optional(paths.root, capture_block_path),
            "parsed_capture_path": _repo_rel_optional(paths.root, parsed_capture_path),
            "report_path": _repo_rel_optional(paths.root, report_path),
            "patch_path": _repo_rel_optional(paths.root, patch_path),
            "verify_artifact_path": _repo_rel_optional(paths.root, verify_artifact_path),
            "budget_artifact_path": _repo_rel_optional(paths.root, budget_artifact_path),
            "session_artifact_dir": _repo_rel_optional(paths.root, session_artifact_dir),
            "promotable_fact_count": len(validation.payload.facts) if validation.payload else 0,
            "promotable_decision_count": len(validation.payload.decisions)
            if validation.payload
            else 0,
            **validation.to_payload(),
        },
    )
    promotion_path = artifact_dir / "promotion.json"
    _write_promotion_result(
        artifact_dir=artifact_dir,
        promotion_path=promotion_path,
        capture_valid=validation.valid,
        promotion_attempted=False,
        promotion_succeeded=False,
        promotion_skipped_reason=None,
        promotion_errors=(),
        fact_entry_ids=(),
        decision_entry_ids=(),
    )

    summary_lines = [
        "# Structured Knowledge Capture",
        "",
        f"- Source: `{source}`",
        f"- Valid: `{'yes' if validation.valid else 'no'}`",
        f"- Promotable Facts: `{len(validation.payload.facts) if validation.payload else 0}`",
        f"- Promotable Decisions: `{len(validation.payload.decisions) if validation.payload else 0}`",
        f"- Assistant Message: `{_repo_rel(paths.root, assistant_message_path)}`",
        (
            f"- Capture Block: `{_repo_rel(paths.root, capture_block_path)}`"
            if capture_block_path is not None
            else "- Capture Block: (none)"
        ),
        (
            f"- Parsed Capture: `{_repo_rel(paths.root, parsed_capture_path)}`"
            if parsed_capture_path is not None
            else "- Parsed Capture: (none)"
        ),
        f"- Validation: `{_repo_rel(paths.root, validation_path)}`",
        f"- Promotion State: `{_repo_rel(paths.root, promotion_path)}`",
    ]
    if validation.errors:
        summary_lines.extend(["", "## Errors", ""])
        summary_lines.extend(f"- {item}" for item in validation.errors)
    if validation.warnings:
        summary_lines.extend(["", "## Warnings", ""])
        summary_lines.extend(f"- {item}" for item in validation.warnings)
    if validation.payload is not None and validation.payload.open_questions:
        summary_lines.extend(["", "## Open Questions", ""])
        summary_lines.extend(f"- {item}" for item in validation.payload.open_questions)
    summary_path = _write_text(capture_summary_path, "\n".join(summary_lines).rstrip() + "\n")

    return PersistedKnowledgeCapture(
        artifact_dir=artifact_dir,
        assistant_message_path=assistant_message_path,
        capture_block_path=capture_block_path,
        parsed_capture_path=parsed_capture_path,
        validation_path=validation_path,
        promotion_path=promotion_path,
        summary_path=summary_path,
        valid=validation.valid,
    )


def promote_validated_knowledge_capture(
    *,
    paths: RunPaths,
    task: dict[str, Any],
    artifact_dir: Path,
) -> KnowledgeCapturePromotionResult:
    existing = _load_promotion_result(artifact_dir)
    if existing is not None and (
        existing.promotion_succeeded
        or existing.promotion_attempted
        or existing.promotion_skipped_reason is not None
    ):
        return existing

    promotion_path = artifact_dir / "promotion.json"
    validation_path = artifact_dir / "validation.json"
    if not validation_path.exists():
        return _write_promotion_result(
            artifact_dir=artifact_dir,
            promotion_path=promotion_path,
            capture_valid=False,
            promotion_attempted=False,
            promotion_succeeded=False,
            promotion_skipped_reason="validation artifact missing",
            promotion_errors=(),
            fact_entry_ids=(),
            decision_entry_ids=(),
        )
    try:
        validation_payload = _load_json_payload(validation_path)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        return _write_promotion_result(
            artifact_dir=artifact_dir,
            promotion_path=promotion_path,
            capture_valid=False,
            promotion_attempted=False,
            promotion_succeeded=False,
            promotion_skipped_reason=f"validation artifact unreadable: {exc}",
            promotion_errors=(),
            fact_entry_ids=(),
            decision_entry_ids=(),
        )

    capture_valid = bool(validation_payload.get("valid"))
    parsed_raw = validation_payload.get("parsed_capture")
    if not capture_valid or parsed_raw is None:
        return _write_promotion_result(
            artifact_dir=artifact_dir,
            promotion_path=promotion_path,
            capture_valid=capture_valid,
            promotion_attempted=False,
            promotion_succeeded=False,
            promotion_skipped_reason="structured capture is not valid for canonical promotion",
            promotion_errors=(),
            fact_entry_ids=(),
            decision_entry_ids=(),
        )
    try:
        payload = _build_capture_payload(parsed_raw)
    except ValueError as exc:
        return _write_promotion_result(
            artifact_dir=artifact_dir,
            promotion_path=promotion_path,
            capture_valid=False,
            promotion_attempted=False,
            promotion_succeeded=False,
            promotion_skipped_reason=f"parsed capture artifact invalid: {exc}",
            promotion_errors=(),
            fact_entry_ids=(),
            decision_entry_ids=(),
        )

    if not payload.facts and not payload.decisions:
        return _write_promotion_result(
            artifact_dir=artifact_dir,
            promotion_path=promotion_path,
            capture_valid=True,
            promotion_attempted=False,
            promotion_succeeded=False,
            promotion_skipped_reason="capture contained no promotable facts or decisions",
            promotion_errors=(),
            fact_entry_ids=(),
            decision_entry_ids=(),
        )

    report_path = _rooted_repo_path(paths.root, validation_payload.get("report_path"))
    patch_path = _rooted_repo_path(paths.root, validation_payload.get("patch_path"))
    verify_artifact_path = _rooted_repo_path(
        paths.root, validation_payload.get("verify_artifact_path")
    )
    budget_artifact_path = _rooted_repo_path(
        paths.root, validation_payload.get("budget_artifact_path")
    )
    session_artifact_dir = _rooted_repo_path(
        paths.root, validation_payload.get("session_artifact_dir")
    )
    source = str(validation_payload.get("source") or "").strip() or "execution"
    capture_artifact_path = artifact_dir / "summary.md"

    fact_entry_ids: list[str] = []
    decision_entry_ids: list[str] = []
    promotion_errors: list[str] = []
    for item in payload.facts:
        try:
            entry = write_fact_entry(
                paths=paths,
                task=task,
                source=source,
                title=item.title,
                summary=item.summary,
                paths_in_scope=item.paths,
                report_path=report_path,
                patch_path=patch_path,
                verify_artifact_path=verify_artifact_path,
                budget_artifact_path=budget_artifact_path,
                session_artifact_dir=session_artifact_dir,
                capture_artifact_path=capture_artifact_path,
                tags=["structured_capture", *item.tags],
            )
        except Exception as exc:  # noqa: BLE001
            promotion_errors.append(f"failed to publish fact '{item.title}': {exc}")
            continue
        fact_entry_ids.append(entry.id)
    for item in payload.decisions:
        try:
            entry = write_decision_entry(
                paths=paths,
                task=task,
                source=source,
                decision_key=item.decision_key,
                title=item.title,
                summary=item.summary,
                status=item.status,
                paths_in_scope=item.paths,
                report_path=report_path,
                patch_path=patch_path,
                verify_artifact_path=verify_artifact_path,
                budget_artifact_path=budget_artifact_path,
                session_artifact_dir=session_artifact_dir,
                capture_artifact_path=capture_artifact_path,
                tags=["structured_capture", *item.tags],
            )
        except Exception as exc:  # noqa: BLE001
            promotion_errors.append(f"failed to publish decision '{item.decision_key}': {exc}")
            continue
        decision_entry_ids.append(entry.id)

    return _write_promotion_result(
        artifact_dir=artifact_dir,
        promotion_path=promotion_path,
        capture_valid=True,
        promotion_attempted=True,
        promotion_succeeded=not promotion_errors,
        promotion_skipped_reason=None,
        promotion_errors=promotion_errors,
        fact_entry_ids=fact_entry_ids,
        decision_entry_ids=decision_entry_ids,
    )


def mark_knowledge_capture_promotion_skipped(
    *,
    artifact_dir: Path,
    reason: str,
) -> KnowledgeCapturePromotionResult | None:
    if not artifact_dir.exists():
        return None
    existing = _load_promotion_result(artifact_dir)
    if existing is not None and (
        existing.promotion_succeeded
        or existing.promotion_attempted
        or existing.promotion_skipped_reason is not None
    ):
        return existing
    validation_path = artifact_dir / "validation.json"
    capture_valid = False
    if validation_path.exists():
        try:
            payload = _load_json_payload(validation_path)
            capture_valid = bool(payload.get("valid"))
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            capture_valid = False
    return _write_promotion_result(
        artifact_dir=artifact_dir,
        promotion_path=artifact_dir / "promotion.json",
        capture_valid=capture_valid,
        promotion_attempted=False,
        promotion_succeeded=False,
        promotion_skipped_reason=reason,
        promotion_errors=(),
        fact_entry_ids=(),
        decision_entry_ids=(),
    )
