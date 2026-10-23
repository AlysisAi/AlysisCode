from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..ide.protocol import redact_secrets
from ..private_artifact_io import _atomic_private_write_text
from ..session_artifacts import SessionArtifactLayout
from ..tools.registry import summarize_tool_output_chunk


@dataclass(frozen=True)
class OffloadResult:
    content_for_message: str
    offloaded: bool
    transcript_shaped: bool
    artifact_locator: str | None
    artifact_fs_path: str | None
    artifact_readable_via_fs: bool
    artifact_location: str | None
    original_chars: int
    preview_chars: int
    message_chars: int
    error: str | None = None


def _safe_component(raw: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", raw.strip())
    return clean or "x"


def _truncated_preview(content: str, limit: int) -> tuple[str, int]:
    if limit <= 0:
        return ("", 0)
    if len(content) <= limit:
        return (content, len(content))
    return (content[:limit] + "...(truncated)", limit)


def _path_is_under_root(*, path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


class ToolOutputOffloader:
    def __init__(
        self,
        *,
        artifact_layout: SessionArtifactLayout,
        workspace_root: Path | None,
        threshold_chars: int,
        preview_chars: int,
        workspace_artifacts_enabled: bool = True,
    ) -> None:
        self._artifact_layout = artifact_layout
        self._session_artifact_root = artifact_layout.filesystem_root.resolve()
        self._workspace_root = workspace_root.resolve() if workspace_root is not None else None
        self._threshold_chars = max(1, int(threshold_chars))
        self._preview_chars = max(1, int(preview_chars))
        # Retained for constructor compatibility; artifacts always follow the
        # session store, including when that store is outside the workspace.
        _ = workspace_artifacts_enabled

    def _transcript_shape_threshold_chars(self) -> int:
        return self._threshold_chars

    def _storage_layout(self) -> SessionArtifactLayout:
        return self._artifact_layout

    @property
    def artifact_root(self) -> Path:
        """Session-scoped root containing offloaded outputs."""

        return self._storage_layout().filesystem_root.resolve()

    @staticmethod
    def _full_output_guidance(
        *,
        original_chars: int,
        preview_chars: int,
        artifact_locator: str | None,
        fs_read_path: str | None,
        artifact_readable_via_fs: bool,
        artifact_location: str | None,
    ) -> str:
        readable_path = str(fs_read_path or artifact_locator or "").strip()
        if artifact_locator:
            return (
                f"Truncated to {preview_chars} of {original_chars} chars. "
                f"Full output saved as {artifact_locator}; use session_artifact_read "
                "with that locator."
            )
        if artifact_readable_via_fs and readable_path:
            return (
                f"Truncated to {preview_chars} of {original_chars} chars. "
                f"Full output saved to {artifact_location or 'workspace_root'} at "
                f"{readable_path} - use fs_read on that path to view all of it."
            )
        return (
            f"Truncated to {preview_chars} of {original_chars} chars; the rest is not "
            "readable via fs. Re-run the command narrowed (grep/head/sed/awk) to get "
            "the specific slice you need."
        )

    def _build_transcript_stub(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        step: int,
        content_json: str,
        original_chars: int,
        preview_text: str,
        preview_chars: int,
        offloaded: bool,
        transcript_shaped: bool,
        artifact_locator: str | None = None,
        artifact_handle: str | None = None,
        artifact_saved: bool = False,
        artifact_readable_via_fs: bool = False,
        artifact_location: str | None = None,
        fs_read_path: str | None = None,
        error: str | None = None,
        continuation_metadata: dict[str, Any] | None = None,
    ) -> str:
        file_preview = None
        if tool_name in {"fs_read", "fs_read_lines"}:
            try:
                parsed = json.loads(content_json)
                if isinstance(parsed, dict) and isinstance(parsed.get("content"), str):
                    file_preview = parsed["content"][: self._preview_chars]
            except (ValueError, TypeError):
                pass
        if file_preview is not None:
            preview_text = file_preview
            preview_chars = len(file_preview)
        payload: dict[str, Any] = {
            "tool": tool_name,
            "tool_call_id": tool_call_id,
            "step": step,
            "offloaded": bool(offloaded),
            "summary": str(redact_secrets(summarize_tool_output_chunk(tool_name, content_json))),
            "preview": str(redact_secrets(preview_text)),
            "preview_chars": preview_chars,
            "original_chars": original_chars,
            "content_truncated": original_chars > preview_chars,
            "raw_saved_in_session_log": False,
        }
        if file_preview is not None:
            payload["preview_format"] = "file_content"
            payload["preview_utf8_bytes"] = len(str(payload["preview"]).encode("utf-8"))
        if transcript_shaped and not offloaded:
            payload["transcript_shaped"] = True
        if continuation_metadata:
            payload.update(continuation_metadata)
        if offloaded:
            payload["artifact_locator"] = artifact_locator
            if artifact_handle:
                payload["artifact_handle"] = artifact_handle
                payload["artifact_read"] = {"handle": artifact_handle}
            payload["artifact_saved"] = artifact_saved
            payload["artifact_readable_via_fs"] = artifact_readable_via_fs
            payload["artifact_location"] = artifact_location
            if artifact_readable_via_fs and fs_read_path:
                payload["fs_read_path"] = fs_read_path
        if original_chars > preview_chars:
            payload["full_output"] = self._full_output_guidance(
                original_chars=original_chars,
                preview_chars=preview_chars,
                artifact_locator=artifact_locator,
                fs_read_path=fs_read_path,
                artifact_readable_via_fs=artifact_readable_via_fs,
                artifact_location=artifact_location,
            )
        if error:
            payload["error"] = str(redact_secrets(error))
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))

    def _artifact_path(self, *, tool_name: str, tool_call_id: str, step: int) -> Path:
        safe_tool = _safe_component(tool_name)
        safe_call_id = _safe_component(tool_call_id)
        filename = f"step{max(0, int(step))}_{safe_tool}_{safe_call_id}.json"
        storage_layout = self._storage_layout()
        storage_root = storage_layout.filesystem_root.resolve()
        candidate = storage_layout.artifact_fs_path("tool_outputs", filename)
        candidate.parent.resolve().relative_to(storage_root)
        if candidate.is_symlink():
            raise ValueError("tool output artifact path cannot be a symlink")
        return candidate

    def maybe_offload(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        step: int,
        result: Any,
        content_json: str,
    ) -> OffloadResult:
        original_chars = len(content_json)
        continuation_metadata = None
        if isinstance(result, dict) and result.get("truncated") is True:
            continuation_metadata = {
                key: result.get(key)
                for key in ("total_lines", "returned_range", "next_range")
                if key in result
            }
        if tool_name == "git_diff" and isinstance(result, dict):
            # Preserve pagination even when a diff page is stored as an artifact,
            # including next_offset=None on the final page.
            continuation_metadata = {
                key: result[key]
                for key in ("diff_id", "offset", "next_offset", "total_chars")
                if key in result
            }
        if tool_name in {
            "embedded_browser_open",
            "embedded_browser_read",
            "embedded_browser_navigate",
            "embedded_browser_back",
            "embedded_browser_forward",
            "embedded_browser_reload",
            "embedded_browser_snapshot",
            "embedded_browser_click",
            "embedded_browser_type",
            "embedded_browser_select",
            "embedded_browser_scroll",
            "embedded_browser_key",
        } and isinstance(result, dict):
            # A stored page still needs its tab and cursor visible to the model,
            # so it can continue reading without opening the wrong tab or restarting.
            continuation_metadata = {
                key: result[key]
                for key in (
                    "tab_id",
                    "title",
                    "url",
                    "next_offset",
                    "snapshot_id",
                    "viewport",
                    "focused_ref",
                )
                if key in result
            }
        if (
            tool_name.startswith("embedded_browser_")
            and continuation_metadata is not None
            and isinstance(result, dict)
            and isinstance(result.get("elements"), list)
        ):
            # Keep observed control refs usable after page text is offloaded. Never
            # synthesize refs or shorten option values, which would change targets.
            controls = []
            for element in result["elements"][:20]:
                if not isinstance(element, dict):
                    continue
                control = {
                    key: element[key]
                    for key in (
                        "ref",
                        "role",
                        "name",
                        "bounds",
                        "editable",
                        "disabled",
                        "checked",
                        "type",
                        "input_type",
                    )
                    if key in element
                }
                if len(json.dumps([*controls, control], ensure_ascii=False)) > 3500:
                    break
                with_options = {
                    **control,
                    **{key: element[key] for key in ("options", "value") if key in element},
                }
                if len(json.dumps([*controls, with_options], ensure_ascii=False)) <= 3500:
                    control = with_options
                elif "options" in element or "value" in element:
                    control["details_omitted"] = True
                if len(json.dumps([*controls, control], ensure_ascii=False)) > 3500:
                    break
                controls.append(control)
            continuation_metadata["elements"] = controls
            continuation_metadata["elements_truncated"] = len(controls) < len(result["elements"])
            continuation_metadata["element_details"] = (
                "Use these observed refs with snapshot_id. Read the linked artifact for omitted controls or option values; never guess them."
            )
        preview_text, preview_chars = _truncated_preview(content_json, self._preview_chars)
        shape_threshold_chars = self._transcript_shape_threshold_chars()
        if original_chars < shape_threshold_chars:
            return OffloadResult(
                content_for_message=content_json,
                offloaded=False,
                transcript_shaped=False,
                artifact_locator=None,
                artifact_fs_path=None,
                artifact_readable_via_fs=False,
                artifact_location=None,
                original_chars=original_chars,
                preview_chars=preview_chars,
                message_chars=original_chars,
                error=None,
            )

        if original_chars < self._threshold_chars:
            content_for_message = self._build_transcript_stub(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                step=step,
                content_json=content_json,
                original_chars=original_chars,
                preview_text=preview_text,
                preview_chars=preview_chars,
                offloaded=False,
                transcript_shaped=True,
                continuation_metadata=continuation_metadata,
            )
            return OffloadResult(
                content_for_message=content_for_message,
                offloaded=False,
                transcript_shaped=True,
                artifact_locator=None,
                artifact_fs_path=None,
                artifact_readable_via_fs=False,
                artifact_location=None,
                original_chars=original_chars,
                preview_chars=preview_chars,
                message_chars=len(content_for_message),
                error=None,
            )

        try:
            artifact_abs = self._artifact_path(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                step=step,
            )
            artifact_abs.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": 1,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "step": step,
                "content_json": content_json,
            }
            _atomic_private_write_text(
                artifact_abs,
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                containment_root=(
                    self._workspace_root
                    if _path_is_under_root(path=artifact_abs, root=self._workspace_root)
                    else None
                ),
            )
            storage_layout = self._storage_layout()
            artifact_ref = storage_layout.model_reference_for_path(
                artifact_path=artifact_abs,
                workspace_root=self._workspace_root,
            )
            artifact_handle = storage_layout.register_artifact(artifact_abs)
            fs_read_path = None
            if artifact_ref.artifact_readable_via_fs and self._workspace_root is not None:
                fs_read_path = (
                    artifact_abs.resolve().relative_to(self._workspace_root.resolve()).as_posix()
                )
            content_for_message = self._build_transcript_stub(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                step=step,
                content_json=content_json,
                original_chars=original_chars,
                preview_text=preview_text,
                preview_chars=preview_chars,
                offloaded=True,
                transcript_shaped=True,
                artifact_locator=artifact_ref.locator,
                artifact_handle=artifact_handle,
                artifact_saved=True,
                artifact_readable_via_fs=artifact_ref.artifact_readable_via_fs,
                artifact_location=artifact_ref.artifact_location,
                fs_read_path=fs_read_path,
                continuation_metadata=continuation_metadata,
            )
            return OffloadResult(
                content_for_message=content_for_message,
                offloaded=True,
                transcript_shaped=True,
                artifact_locator=artifact_ref.locator,
                artifact_fs_path=str(artifact_abs),
                artifact_readable_via_fs=artifact_ref.artifact_readable_via_fs,
                artifact_location=artifact_ref.artifact_location,
                original_chars=original_chars,
                preview_chars=preview_chars,
                message_chars=len(content_for_message),
                error=None,
            )
        except Exception as exc:
            safe_error = str(redact_secrets(str(exc)))
            fallback_preview, fallback_preview_chars = _truncated_preview(
                content_json,
                self._preview_chars,
            )
            content_for_message = self._build_transcript_stub(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                step=step,
                content_json=content_json,
                original_chars=original_chars,
                preview_text=fallback_preview,
                preview_chars=fallback_preview_chars,
                offloaded=False,
                transcript_shaped=True,
                error=safe_error,
                continuation_metadata=continuation_metadata,
            )
            return OffloadResult(
                content_for_message=content_for_message,
                offloaded=False,
                transcript_shaped=True,
                artifact_locator=None,
                artifact_fs_path=None,
                artifact_readable_via_fs=False,
                artifact_location=None,
                original_chars=original_chars,
                preview_chars=fallback_preview_chars,
                message_chars=len(content_for_message),
                error=safe_error,
            )
