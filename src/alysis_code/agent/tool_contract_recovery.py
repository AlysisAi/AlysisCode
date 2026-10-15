"""Turn-local admission for automatic provider-contract recovery."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterable
from typing import Any

from .tool_arguments import conform_tool_arguments
from .tools_assembly import ToolDef
from .turn.read_cache import _same_batch_read_cache_should_invalidate


def recovery_repeat_safe(tool: ToolDef | None, name: str) -> bool:
    # Only host-bound capability metadata and the existing built-in read contract
    # grant this exemption. Model prose, arguments and MCP annotations do not.
    metadata = getattr(tool, "metadata", None) or {}
    return metadata.get("read_only") is True or (
        metadata.get("tool_type") == "builtin"
        and not _same_batch_read_cache_should_invalidate(name, tool)
    )


def recovery_blocked_result(*, whole_batch: bool = False) -> dict[str, Any]:
    return {
        "error": "recovery_operation_repeat",
        "status": "blocked",
        "retryable": False,
        "message": (
            "Automatic recovery refused a repetition of a prior dispatched operation. "
            + ("No calls from this batch ran. " if whole_batch else "This operation did not run. ")
            + "Inspect the prior "
            "outcome with a read; do not resubmit the operation."
        ),
    }


def _operation_key(tool: ToolDef | None, name: str, arguments: dict[str, Any]) -> bytes | None:
    try:
        # Non-string keys are controller-only cancellation/run bindings, never
        # provider arguments. Built-in defaults are normalized at both boundaries.
        values = {key: value for key, value in arguments.items() if isinstance(key, str)}
        if (getattr(tool, "metadata", None) or {}).get("tool_type") == "builtin":
            schema = getattr(tool, "parameters", None) or {}
            values, _ = conform_tool_arguments(schema, values)
            for field, spec in (schema.get("properties") or {}).items():
                if field not in values and isinstance(spec, dict) and "default" in spec:
                    values[field] = spec["default"]
        payload = json.dumps([name, values], sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, AttributeError):
        return None
    # No arguments, IDs, results or digests are persisted by this ledger.
    return hashlib.sha256(payload.encode("utf-8")).digest()


class ToolContractRecoveryAdmission:
    def __init__(self) -> None:
        self._dispatched: set[bytes] = set()
        self._lock = threading.Lock()

    def blocks_batch(self, calls: Iterable[Any], tools: dict[str, ToolDef]) -> bool:
        with self._lock:
            previous = set(self._dispatched)
        for call in calls:
            tool = tools.get(call.name)
            if recovery_repeat_safe(tool, call.name):
                continue
            key = _operation_key(tool, call.name, call.arguments)
            if key is None or key in previous:
                return True
            previous.add(key)
        return False

    def admit_dispatch(
        self, tool: ToolDef, name: str, arguments: dict[str, Any], *, recovering: bool
    ) -> bool:
        if recovery_repeat_safe(tool, name):
            return True
        key = _operation_key(tool, name, arguments)
        with self._lock:
            if recovering and (key is None or key in self._dispatched):
                return False
            if key is not None:
                # Reserve before calling the handler. An exception or uncertain
                # result cannot prove absence of a side effect, so never remove it.
                self._dispatched.add(key)
        return True
