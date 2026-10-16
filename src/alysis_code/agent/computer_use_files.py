"""Closed file-tool schemas and transient delivery state, never file authority."""

from __future__ import annotations

import re
from typing import Any

from .computer_use_uploads import computer_use_upload_completion_status

COMPUTER_USE_FILE_TOOL_NAMES = ("computer_download", "computer_file_status")
COMPUTER_USE_FILE_PROMPT = """
The host may offer computer_download for one observed native link and
computer_file_status for an opaque artifact ID returned by the host. These tools
accept no URLs, paths, filenames, file bytes, grants or proofs. Observe before a
download. A needs_user_setup or permission_required response means the user must
complete ordinary host setup, then explicitly continue with a fresh observation;
never treat that response as a finished download. A start/correlation response is
not delivery. Read computer_file_status for retained progress; status never starts
or retries a transfer or copies a file. Do not repeat a download after an uncertain
outcome. Local byte verification, actual destination delivery, and business-record
identity are different claims. Selected-link bytes cannot prove that the source
is the requested invoice or that all requested records have been downloaded.
"""


def file_tool_parameters(name: str) -> dict[str, Any]:
    field = "element_id" if name == "computer_download" else "artifact_id"
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


def validate_file_arguments(name: str, value: Any) -> bool:
    field = "element_id" if name == "computer_download" else "artifact_id"
    if (
        name not in COMPUTER_USE_FILE_TOOL_NAMES
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


def validate_file_result(value: Any) -> bool:
    if (
        not isinstance(value, dict)
        or set(value) - {"file_result", "alysis_workflow"}
        or "file_result" not in value
    ):
        return False
    result = value["file_result"]
    if (
        not isinstance(result, dict)
        or set(result) != {"version", "operation", "status", "artifact"}
        or type(result["version"]) is not int
        or result["version"] != 1
        or result["operation"] != "download"
        or not isinstance(result["status"], str)
    ):
        return False
    if result["status"] in {"needs_user_setup", "permission_required"}:
        return result["artifact"] is None
    artifact = result["artifact"]
    fields = {
        "artifactId",
        "status",
        "browserComplete",
        "localIntegrity",
        "sourceIdentity",
        "proofScope",
        "businessRecordVerified",
        "historicalDeliveryObserved",
        "byteLength",
        "requiresReconciliation",
        "cancelRequired",
        "deliveryStatus",
        "delivered",
    }
    if result["status"] != "artifact" or not isinstance(artifact, dict) or set(artifact) != fields:
        return False
    if not isinstance(artifact["artifactId"], str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", artifact["artifactId"]
    ):
        return False
    enums = {
        "status": {
            "not_dispatched",
            "start_unknown",
            "downloading",
            "browser_completed",
            "interrupted",
            "dangerous",
            "blocked",
            "delivered",
        },
        "localIntegrity": {
            "not_verified",
            "local_hash_recorded",
            "source_checksum_matched",
            "failed",
        },
        "sourceIdentity": {"not_verified", "matched", "mismatched", "conflicted"},
        "proofScope": {"selected_link_bytes", "business_record", "legacy_unqualified"},
        "deliveryStatus": {
            "not_requested",
            "start_unknown",
            "delivered",
            "partial",
            "not_delivered",
            "uncertain",
            "blocked",
        },
    }
    if any(
        not isinstance(artifact[key], str) or artifact[key] not in choices
        for key, choices in enums.items()
    ):
        return False
    if any(
        type(artifact[key]) is not bool
        for key in (
            "browserComplete",
            "businessRecordVerified",
            "historicalDeliveryObserved",
            "requiresReconciliation",
            "cancelRequired",
            "delivered",
        )
    ):
        return False
    length = artifact["byteLength"]
    if length is not None and (type(length) is not int or not 0 <= length <= 512 * 1024 * 1024):
        return False
    integrity = artifact["localIntegrity"] in {"local_hash_recorded", "source_checksum_matched"}
    if (artifact["status"] == "delivered") != artifact["delivered"] or (
        artifact["deliveryStatus"] == "delivered"
    ) != artifact["delivered"]:
        return False
    if artifact["delivered"] and (
        not artifact["browserComplete"]
        or not integrity
        or length is None
        or artifact["requiresReconciliation"]
        or artifact["cancelRequired"]
        or artifact["proofScope"] == "legacy_unqualified"
        or artifact["proofScope"] == "business_record"
        and not artifact["businessRecordVerified"]
    ):
        return False
    if artifact["businessRecordVerified"] and (
        artifact["proofScope"] != "business_record"
        or artifact["sourceIdentity"] != "matched"
        or not integrity
    ):
        return False
    if artifact["proofScope"] != "business_record" and artifact["sourceIdentity"] not in {
        "not_verified",
        "conflicted",
    }:
        return False
    if artifact["historicalDeliveryObserved"] and artifact["proofScope"] != "legacy_unqualified":
        return False
    if artifact["proofScope"] == "legacy_unqualified" and (
        not artifact["requiresReconciliation"]
        or artifact["delivered"]
        or artifact["businessRecordVerified"]
    ):
        return False
    if (
        artifact["localIntegrity"] == "not_verified"
        and length is not None
        or integrity
        and length is None
    ):
        return False
    return True


class TrustedFileDeliveryBinding:
    def __init__(self) -> None:
        self.unknown = 0
        self.setup = False
        self.artifacts: dict[str, bool] = {}

    def starting(self) -> None:
        self.unknown += 1

    def failed_status(self, identifier: str | None) -> None:
        # A failed read cannot preserve a previous positive status. Unknown IDs
        # never grow retained state; they also cannot reconcile an unknown start.
        if identifier in self.artifacts:
            self.artifacts[identifier] = False

    def adopt(self, tool: str, result: dict[str, Any], requested_id: str | None) -> bool:
        data = result["file_result"]
        if data["status"] != "artifact":
            self.setup = True
            if tool == "computer_download":
                self.unknown = max(
                    0, self.unknown - 1
                )  # Only this authenticated prepare did not dispatch.
            return True
        artifact = data["artifact"]
        identifier = artifact["artifactId"]
        if requested_id is not None and identifier != requested_id:
            return False
        if identifier not in self.artifacts and len(self.artifacts) >= 64:
            return False
        if tool == "computer_download":
            self.unknown = max(0, self.unknown - 1)
            self.setup = False
            self.artifacts[identifier] = (
                False  # Start is correlation, never independent delivery proof.
            )
        else:
            self.artifacts[identifier] = artifact["delivered"] is True
        return True

    def status(self) -> dict[str, Any]:
        return {
            "complete": not self.unknown and not self.setup and all(self.artifacts.values()),
            "needs_user_setup": self.setup,
            "uncertain": self.unknown > 0,
            "pending_count": sum(not value for value in self.artifacts.values()),
        }


def computer_use_file_delivery_status(tools: dict[str, Any]) -> dict[str, Any]:
    status = {"complete": True, "needs_user_setup": False, "uncertain": False, "pending_count": 0}
    for tool in tools.values():
        metadata = getattr(tool, "metadata", None)
        binding = metadata.get("_computer_use_file_binding") if isinstance(metadata, dict) else None
        if isinstance(binding, TrustedFileDeliveryBinding):
            status = binding.status()
            break
    upload = computer_use_upload_completion_status(tools)
    return {
        "complete": status["complete"] and upload["complete"],
        "needs_user_setup": status["needs_user_setup"] or upload["needs_user_setup"],
        "uncertain": status["uncertain"] or upload["uncertain"],
        "pending_count": status["pending_count"] + upload["pending_count"],
    }
