"""Temporary, explicitly enabled local diagnostics for the bounded desktop trial.

No request bodies, credentials, tool arguments, or successful model output are
recorded. This observer cannot alter transport, retries, or tool dispatch.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from ..error_text import sanitize_error_summary


def diagnostic_root() -> Path:
    return (
        Path.home()
        / "Library/Application Support/Alysis Code/logs/runtime/focused-input-diagnostic"
    )


def record_stream_diagnostic(
    response: Any, event: dict[str, Any] | None = None, *, event_count: int = 0
) -> None:
    """Record bounded error fields before the normal closed diagnostic projection."""
    try:
        root = diagnostic_root()
        until = float((root / "enabled-until.txt").read_text())
        now = time.time()
        if not now < until <= now + 7200:
            return
        row: dict[str, Any] = {
            "at": now,
            "phase": "stream_error" if event else "stream_open",
            "http_status": response.status_code,
            "event_count": event_count,
        }
        for key in ("x-request-id", "request-id", "openai-request-id", "cf-ray"):
            value = response.headers.get(key, "")
            if re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value):
                row[key] = value
        if event is not None:
            for key in ("request_id", "response_id", "event_id"):
                value = event.get(key)
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value):
                    row[key] = value
            container = event.get("response")
            container = container if isinstance(container, dict) else event
            detail = container.get("error")
            if isinstance(detail, str):
                row["error_message"] = sanitize_error_summary(detail, max_chars=1600)
            detail = detail if isinstance(detail, dict) else container
            for key in ("type", "code", "param", "status", "status_code", "message"):
                value = detail.get(key)
                if isinstance(value, (str, int)) and not isinstance(value, bool):
                    row["error_" + key] = sanitize_error_summary(str(value), max_chars=1600)
            row["event_type"] = sanitize_error_summary(str(event.get("type", "")), max_chars=120)
        else:
            # Shape only, to distinguish continuation/history/schema failures.
            request = json.loads(response.request.content)
            row.update(
                {
                    "model": request.get("model"),
                    "previous_response_id_used": bool(request.get("previous_response_id")),
                    "input_count": len(request.get("input", [])),
                    "tool_count": len(request.get("tools", [])),
                    "parallel_tool_calls": request.get("parallel_tool_calls"),
                    "store": request.get("store"),
                }
            )
        path = root / "provider.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            if os.fstat(fd).st_size < 1_048_576:
                os.write(fd, (json.dumps(row, ensure_ascii=True) + "\n").encode())
        finally:
            os.close(fd)
    except Exception:  # Diagnostics must never affect the provider operation.
        pass
