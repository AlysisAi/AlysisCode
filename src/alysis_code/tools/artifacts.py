from __future__ import annotations

import codecs
from typing import Any

from ..ide.protocol import redact_secrets
from ..session_artifacts import SessionArtifactLayout

DEFAULT_SESSION_ARTIFACT_READ_BYTES = 64 * 1024
MAX_SESSION_ARTIFACT_READ_BYTES = 1024 * 1024


class SessionArtifactReadError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "session_artifact_read_failed",
        guidance: str | None = None,
        available_handles: list[dict[str, str]] | None = None,
    ) -> None:
        self.result_payload: dict[str, Any] = {
            "error": message,
            "error_code": error_code,
            "terminal": True,
            "retryable": False,
        }
        if guidance:
            self.result_payload["guidance"] = guidance
        if available_handles is not None:
            self.result_payload["available_handles"] = available_handles
        super().__init__(message)


def _invalid_locator_error(exc: BaseException) -> SessionArtifactReadError:
    guidance = (
        "Use only a locator returned by a tool in this session. Guessing a locator will not work."
    )
    return SessionArtifactReadError(
        f"Invalid session artifact locator: {exc}. A valid locator starts with "
        "session_artifacts/ and uses relative POSIX path segments. "
        f"{guidance}",
        error_code="invalid_session_artifact_locator",
        guidance=guidance,
    )


def _bounded_max_bytes(value: Any) -> int:
    if value is None or value == "":
        return DEFAULT_SESSION_ARTIFACT_READ_BYTES
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SessionArtifactReadError("Session artifact max_bytes must be an integer.") from exc
    return max(1, min(parsed, MAX_SESSION_ARTIFACT_READ_BYTES))


def _bounded_offset(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SessionArtifactReadError("Session artifact offset must be an integer.") from exc
    if parsed < 0:
        raise SessionArtifactReadError("Session artifact offset must be non-negative.")
    return parsed


def session_artifact_read(
    *,
    artifact_layout: SessionArtifactLayout,
    locator: str = "",
    handle: str = "",
    list_handles: bool = False,
    after_handle: str = "",
    max_bytes: Any = DEFAULT_SESSION_ARTIFACT_READ_BYTES,
    offset: Any = 0,
) -> dict[str, Any]:
    """Read one bounded artifact addressed only by its current-session locator."""

    if list_handles:
        references = artifact_layout.available_handles(limit=100, after_handle=after_handle)
        return {
            "available_handles": references,
            "next_handle": references[-1]["handle"] if len(references) == 100 else None,
        }
    if handle:
        try:
            handle_locator = artifact_layout.resolve_handle(handle)
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            raise SessionArtifactReadError(
                "Artifact handle is not available in the current session.",
                error_code="session_artifact_handle_not_found",
                guidance="Select an exact available handle, or use list_handles=true to discover references.",
                available_handles=artifact_layout.available_handles(),
            ) from exc
        if locator and locator != handle_locator:
            raise SessionArtifactReadError(
                "Artifact handle and locator identify different results.",
                error_code="session_artifact_reference_mismatch",
                guidance="Use the matching reference returned by the producing tool.",
            )
        locator = handle_locator
    try:
        artifact_path = artifact_layout.resolve_locator(locator)
    except (OSError, RuntimeError, ValueError) as exc:
        error = _invalid_locator_error(exc)
        error.result_payload["available_handles"] = artifact_layout.available_handles()
        raise error from exc

    read_limit = _bounded_max_bytes(max_bytes)
    read_offset = _bounded_offset(offset)
    if not artifact_path.exists() or not artifact_path.is_file():
        guidance = (
            "Select an exact available handle, or use list_handles=true. "
            "References from other sessions are not authorized here."
        )
        raise SessionArtifactReadError(
            f"Session artifact was not found in this session. {guidance}",
            error_code="session_artifact_not_found",
            guidance=guidance,
            available_handles=artifact_layout.available_handles(),
        )
    try:
        size = artifact_path.stat().st_size
        if read_offset >= size:
            payload = b""
        else:
            with artifact_path.open("rb") as artifact_file:
                artifact_file.seek(read_offset)
                payload = artifact_file.read(read_limit)
    except OSError as exc:
        raise SessionArtifactReadError("Session artifact was not readable.") from exc

    if read_offset and payload and 0x80 <= payload[0] <= 0xBF:
        raise SessionArtifactReadError(
            "Offset splits a UTF-8 character; use the previous page's next_offset.",
            error_code="session_artifact_invalid_utf8_offset",
        )
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    content = decoder.decode(payload, final=read_offset + len(payload) >= size)
    pending, _ = decoder.getstate()
    bytes_returned = len(payload) - len(pending)
    has_more = read_offset + bytes_returned < size
    return {
        "locator": str(locator),
        **({"handle": handle} if handle else {}),
        "offset": read_offset,
        "bytes_returned": bytes_returned,
        "size": size,
        "size_bytes": size,
        "has_more": has_more,
        "next_offset": read_offset + bytes_returned if has_more else None,
        "truncated": has_more,
        "max_bytes": read_limit,
        "encoding": "utf-8-replace",
        "content": redact_secrets(content),
        **({"minimum_bytes_to_progress": 4} if payload and not bytes_returned else {}),
    }


__all__ = [
    "DEFAULT_SESSION_ARTIFACT_READ_BYTES",
    "MAX_SESSION_ARTIFACT_READ_BYTES",
    "SessionArtifactReadError",
    "session_artifact_read",
]
