"""Closed selected-file objective evidence from the authenticated host tool lane.

The initial binding is captured before session callbacks. These values never grant
file authority and are not inferred from model text or a verification response.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


def _closed(value: Any, keys: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == keys


def _version(value: Any) -> bool:
    return type(value) in (int, float) and value == 1


def _digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", value) is not None


def _id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value) is not None
    )


def validate_upload_objective_binding(value: Any) -> bool:
    return (
        _closed(value, {"version", "objective_digest", "task_scope_digest"})
        and _version(value["version"])
        and _digest(value["objective_digest"])
        and _digest(value["task_scope_digest"])
    )


def validate_upload_objective_proof(value: Any) -> bool:
    return (
        _closed(
            value,
            {
                "version",
                "objective_digest",
                "task_scope_digest",
                "upload_id",
                "result_id",
                "result_digest",
            },
        )
        and _version(value["version"])
        and _digest(value["objective_digest"])
        and _digest(value["task_scope_digest"])
        and _digest(value["result_digest"])
        and _id(value["upload_id"])
        and _id(value["result_id"])
    )


def validate_upload_objective_result(value: Any) -> bool:
    if not _closed(value, {"verification"}):
        return False
    v = value["verification"]
    return (
        _closed(v, {"version", "status", "reason", "evidence_scope", "upload_objective"})
        and _version(v["version"])
        and v["status"] == "verified"
        and v["reason"] is None
        and v["evidence_scope"] == "selected_file_upload_readback"
        and validate_upload_objective_proof(v["upload_objective"])
    )


@dataclass(frozen=True, slots=True)
class UploadObjectiveBinding:
    objective_digest: str
    task_scope_digest: str

    def to_protocol(self) -> dict[str, Any]:
        return {
            "version": 1,
            "objective_digest": self.objective_digest,
            "task_scope_digest": self.task_scope_digest,
        }


def capture_upload_objective(value: Any) -> UploadObjectiveBinding | None:
    if value is None:
        return None
    if type(value) is UploadObjectiveBinding:
        value = value.to_protocol()
    if not validate_upload_objective_binding(value):
        raise ValueError("Unsupported upload objective binding")
    return UploadObjectiveBinding(value["objective_digest"], value["task_scope_digest"])
