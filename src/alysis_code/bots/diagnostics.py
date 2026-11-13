"""Credential-free diagnostics for local Owls (AL, 2026-11-07).

The local service runs with logging off, because exception text and logs can carry prompts, paths
or keys. When owls fail on someone's computer, support still needs to know where. Each entry keeps
what ``runtime_failure`` task events keep (the exception type and the Alysis code location), a
fixed event name, a few machine facts, and the person-facing words of typed refusals, which are
written to be shown. Never exception text, locals, request data or raw tracebacks.

The desktop app's Copy diagnostics reads this file directly, so it also helps when the service
could not start. The file is capped; old entries go first.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .compute import ComputeCreationRefused, ComputeStopped

FILE = "diagnostics.jsonl"
MAX_BYTES = 128 * 1024
KEEP_LINES = 200
_lock = threading.Lock()
_path: Path | None = None


def configure(directory: Path) -> None:
    """Write entries into ``directory`` (the local owls folder). Unconfigured, nothing is written."""
    global _path
    _path = Path(directory) / FILE


def _location(error: BaseException) -> str | None:
    location, trace = None, error.__traceback__
    while trace is not None:
        module = trace.tb_frame.f_globals.get("__name__", "")
        if module.startswith("alysis_code."):
            location = f"{module}:{trace.tb_frame.f_code.co_name}:{trace.tb_lineno}"
        trace = trace.tb_next
    return location


def _fact(value) -> bool:
    if isinstance(value, bool | int | float):
        return True
    return isinstance(value, str) and len(value) <= 120


def record(event: str, error: BaseException | None = None, **facts) -> None:
    """Append one entry. Never raises: a diagnostic must not replace the failure it describes."""
    path = _path
    if path is None:
        return
    entry = {"at": int(time.time()), "event": event}
    if error is not None:
        entry["error"] = type(error).__name__
        code = getattr(error, "code", None)
        if isinstance(code, str):
            entry["code"] = code
        location = _location(error)
        if location:
            entry["where"] = location
        if isinstance(error, ComputeCreationRefused | ComputeStopped):
            entry["message"] = str(error)[:300]
    entry.update({key: value for key, value in facts.items() if _fact(value)})
    try:
        with _lock:
            if path.exists() and path.stat().st_size > MAX_BYTES:
                kept = path.read_text(encoding="utf-8").splitlines()[-KEEP_LINES:]
                path.write_text("\n".join(kept) + "\n", encoding="utf-8")
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
