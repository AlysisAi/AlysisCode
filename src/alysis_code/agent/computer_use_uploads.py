"""Closed upload projections and transient completion tracking, never file authority."""

from __future__ import annotations

import re
from typing import Any

from .computer_use_upload_objectives import UploadObjectiveBinding, validate_upload_objective_result

COMPUTER_USE_UPLOAD_TOOL_NAMES = ("computer_upload", "computer_upload_status")
COMPUTER_USE_UPLOAD_PROMPT = """
The host may offer computer_upload for one observed native single-file input and
computer_upload_status for an opaque upload ID returned by the host. Select only
the observed element ID. Never provide a local path, filename, file bytes, URL,
snapshot ID, grant or approval token. The user chooses the actual source through
ordinary host setup. A needs_user_setup response pauses the task for explicit
user setup and continuation; do not repeat the upload to bypass it. Uploading
requires the exact ordinary approval because assigning files already discloses
bytes to page handlers. A page_disclosed receipt proves assignment only, not
server receipt, matching remote bytes, or the requested objective. Read retained
status without starting or retrying an upload. An uncertain outcome must not be
repeated. Report receiver acknowledgement or matching remote bytes only when the
corresponding retained host status explicitly verifies them. Remote byte identity
still does not prove the requested objective. Only when the host has admitted the
original selected-file objective can an independent computer_verify evaluate
result verify that exact upload. Never propose or supply an upload objective,
proof, or digest yourself. If the host cannot verify it, report that limit honestly.
"""


def upload_tool_parameters(name: str) -> dict[str, Any]:
    field = "element_id" if name == "computer_upload" else "upload_id"
    return {
        "type": "object",
        "properties": {
            field: {
                "type": "string",
                "minLength": 1,
                "maxLength": 256 if field == "element_id" else 128,
            }
        },
        "required": [field],
        "additionalProperties": False,
    }


def validate_upload_arguments(name: str, value: Any) -> bool:
    field = "element_id" if name == "computer_upload" else "upload_id"
    if (
        name not in COMPUTER_USE_UPLOAD_TOOL_NAMES
        or not isinstance(value, dict)
        or set(value) != {field}
    ):
        return False
    item = value[field]
    if (
        not isinstance(item, str)
        or not item
        or len(item) > (256 if field == "element_id" else 128)
        or item != item.strip()
        or any(ord(c) < 32 or ord(c) == 127 for c in item)
    ):
        return False
    return (
        field == "element_id" or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", item) is not None
    )


def validate_upload_result(value: Any) -> bool:
    """Exact parity with shared/computerUseUploadTools.ts, without host envelopes."""
    if not isinstance(value, dict) or set(value) != {"upload_result"}:
        return False
    result = value["upload_result"]
    if (
        not isinstance(result, dict)
        or set(result) != {"version", "status", "upload"}
        or type(result["version"]) not in (int, float)
        or result["version"] != 1
    ):
        return False
    if result["status"] == "needs_user_setup":
        return result["upload"] is None
    upload = result["upload"]
    if (
        result["status"] != "upload"
        or not isinstance(upload, dict)
        or set(upload)
        != {
            "uploadId",
            "phase",
            "exportedBytes",
            "byteLength",
            "pageDisclosed",
            "requiresReconciliation",
            "receiverAcknowledged",
            "remoteBytesVerified",
            "objectiveVerified",
        }
    ):
        return False
    if (
        not isinstance(upload["uploadId"], str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", upload["uploadId"]) is None
    ):
        return False
    phase = upload["phase"]
    if not isinstance(phase, str) or phase not in {
        "granted",
        "staging",
        "staged",
        "release_prepared",
        "release_unknown",
        "page_disclosed",
        "not_disclosed",
        "uncertain",
    }:
        return False
    if (
        not _safe_integer(upload["byteLength"])
        or not 0 <= upload["byteLength"] <= 8 * 1024 * 1024
        or not _safe_integer(upload["exportedBytes"])
        or not 0 <= upload["exportedBytes"] <= upload["byteLength"]
    ):
        return False
    if (
        type(upload["pageDisclosed"]) is not bool
        or type(upload["requiresReconciliation"]) is not bool
        or type(upload["receiverAcknowledged"]) is not bool
        or type(upload["remoteBytesVerified"]) is not bool
        or upload["objectiveVerified"] is not False
    ):
        return False
    if upload["pageDisclosed"] != (phase == "page_disclosed"):
        return False
    if (
        upload["receiverAcknowledged"]
        and not upload["pageDisclosed"]
        or upload["remoteBytesVerified"]
        and not upload["receiverAcknowledged"]
    ):
        return False
    if not (phase == "page_disclosed" and upload["remoteBytesVerified"]) and upload[
        "requiresReconciliation"
    ] != (
        phase in {"staged", "release_prepared", "release_unknown", "uncertain", "page_disclosed"}
    ):
        return False
    if (
        phase in {"staged", "release_prepared", "release_unknown", "page_disclosed"}
        and upload["exportedBytes"] != upload["byteLength"]
    ):
        return False
    return phase != "granted" or upload["exportedBytes"] == 0


def _safe_integer(value: Any) -> bool:
    # JSON has one numeric type: JavaScript parses 12 and 12.0 identically.
    # Match Number.isSafeInteger without admitting bool, NaN or Infinity.
    return (
        type(value) in (int, float)
        and -9007199254740991 <= value <= 9007199254740991
        and int(value) == value
    )


def upload_result_projection(value: Any) -> dict[str, Any] | None:
    # The existing trusted host envelope may add a separately checked workflow
    # binding. It does not change the closed upload projection itself.
    if (
        not isinstance(value, dict)
        or "upload_result" not in value
        or set(value) - {"upload_result", "alysis_workflow"}
    ):
        return None
    projection = {"upload_result": value["upload_result"]}
    return projection if validate_upload_result(projection) else None


class TrustedUploadCompletionBinding:
    def __init__(self, objective: UploadObjectiveBinding | None = None) -> None:
        self.objective = objective
        self.initiated: set[str] = set()
        self.results: set[str] = set()
        self.unknown = 0
        self.setup = False
        self.uploads: dict[str, bool] = {}

    def starting(self) -> None:
        self.unknown += 1

    def failed_status(self, identifier: str | None) -> None:
        if identifier in self.uploads:
            self.uploads[identifier] = False

    def adopt(self, tool: str, result: dict[str, Any], requested_id: str | None) -> bool:
        data = result["upload_result"]
        if data["status"] == "needs_user_setup":
            self.setup = True
            if tool == "computer_upload":
                self.unknown = max(0, self.unknown - 1)
            return True
        upload = data["upload"]
        identifier = upload["uploadId"]
        if requested_id is not None and identifier != requested_id:
            return False
        if identifier not in self.uploads and len(self.uploads) >= 64:
            return False
        if tool == "computer_upload":
            self.unknown = max(0, self.unknown - 1)
            self.setup = False
            self.initiated.add(identifier)
        # Byte identity and exact-effect admission are narrower than the original
        # objective. No remote status may clear this aggregate completion gate.
        self.uploads[identifier] = False
        return True

    def adopt_objective(self, result: Any) -> bool:
        # Called only after a successful authenticated evaluate tool response.
        # Status-only IDs and unknown starts cannot be correlated by this proof.
        if self.objective is None or not validate_upload_objective_result(result):
            return False
        proof = result["verification"]["upload_objective"]
        identifier, result_id = proof["upload_id"], proof["result_id"]
        if (
            proof["objective_digest"] != self.objective.objective_digest
            or proof["task_scope_digest"] != self.objective.task_scope_digest
            or identifier not in self.initiated
            or identifier not in self.uploads
            or result_id in self.results
            or len(self.results) >= 64
        ):
            return False
        self.results.add(result_id)
        self.uploads[identifier] = True
        return True

    def status(self) -> dict[str, Any]:
        return {
            # An explicitly admitted upload objective cannot be completed by
            # skipping upload tools or by a generic verifier's unrelated result.
            "complete": (
                not self.unknown
                and not self.setup
                and all(self.uploads.values())
                and (self.objective is None or bool(self.results))
            ),
            "needs_user_setup": self.setup,
            "uncertain": self.unknown > 0,
            "pending_count": sum(not complete for complete in self.uploads.values()),
        }


def computer_use_upload_completion_status(tools: dict[str, Any]) -> dict[str, Any]:
    for tool in tools.values():
        metadata = getattr(tool, "metadata", None)
        binding = (
            metadata.get("_computer_use_upload_binding") if isinstance(metadata, dict) else None
        )
        if isinstance(binding, TrustedUploadCompletionBinding):
            return binding.status()
    return {"complete": True, "needs_user_setup": False, "uncertain": False, "pending_count": 0}
