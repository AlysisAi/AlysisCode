"""Canonical JSON and cryptographic bindings for Forge Host Protocol v2.

This module deliberately implements the same JSON profile as the desktop App's
``forgeContractV2.ts``: NFC strings and keys, Unicode-code-point key ordering,
compact UTF-8, JSON primitives only, and JavaScript-safe integers.  Fingerprints
are lowercase SHA-256 hex over an explicit semantic projection; callers never
trust a fingerprint supplied on the wire without recomputing it.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

JS_SAFE_INTEGER_MAX = 9_007_199_254_740_991
_COMPUTED_FINGERPRINT_KEYS = frozenset(
    {
        "fingerprint",
        "contractFingerprint",
        "contract_fingerprint",
        "full_content_fingerprint",
    }
)


class CanonicalJSONError(ValueError):
    """A value is outside the cross-language canonical JSON profile."""


def _normalize_string(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    # Python can retain lone surrogate code points even though UTF-8 cannot.
    if any(0xD800 <= ord(character) <= 0xDFFF for character in normalized):
        raise CanonicalJSONError("canonical JSON rejects unpaired surrogates")
    return normalized


def _normalize_value(
    value: Any,
    *,
    exclude_computed_fingerprints: bool,
) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True, exclude_none=False)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _normalize_string(value)
    if isinstance(value, int):
        if abs(value) > JS_SAFE_INTEGER_MAX:
            raise CanonicalJSONError("canonical JSON requires JavaScript-safe integers")
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer() or abs(value) > JS_SAFE_INTEGER_MAX:
            raise CanonicalJSONError(
                "canonical JSON accepts only finite JavaScript-safe integral numbers"
            )
        return int(value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise CanonicalJSONError("canonical JSON object keys must be strings")
            if exclude_computed_fingerprints and raw_key in _COMPUTED_FINGERPRINT_KEYS:
                continue
            key = _normalize_string(raw_key)
            if key in normalized:
                raise CanonicalJSONError(
                    "canonical JSON contains duplicate keys after NFC normalization"
                )
            normalized[key] = _normalize_value(
                item,
                exclude_computed_fingerprints=exclude_computed_fingerprints,
            )
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [
            _normalize_value(
                item,
                exclude_computed_fingerprints=exclude_computed_fingerprints,
            )
            for item in value
        ]
    raise CanonicalJSONError("canonical JSON accepts only JSON data values")


def canonical_json_bytes(
    value: Any,
    *,
    exclude_computed_fingerprints: bool = True,
) -> bytes:
    """Encode a value using the Forge NFC canonical JSON profile."""

    normalized = _normalize_value(
        value,
        exclude_computed_fingerprints=exclude_computed_fingerprints,
    )
    try:
        return json.dumps(
            normalized,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=False,
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as exc:
        raise CanonicalJSONError("value cannot be encoded as canonical JSON") from exc


def canonical_json_text(
    value: Any,
    *,
    exclude_computed_fingerprints: bool = True,
) -> str:
    return canonical_json_bytes(
        value,
        exclude_computed_fingerprints=exclude_computed_fingerprints,
    ).decode("utf-8")


def canonical_fingerprint(
    value: Any,
    *,
    exclude_computed_fingerprints: bool = True,
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            value,
            exclude_computed_fingerprints=exclude_computed_fingerprints,
        )
    ).hexdigest()


def plan_semantic_projection(value: Mapping[str, Any] | BaseModel) -> dict[str, Any]:
    """Return the complete Host-v2 authored view bound by ``hostPlanFingerprint``.

    This is an explicitly versioned Host projection. It is not the legacy App/
    pinned-CLI approval projection and therefore cannot reuse stored approvals.
    """

    wire = (
        value.model_dump(mode="json", by_alias=True, exclude_none=False)
        if isinstance(value, BaseModel)
        else dict(value)
    )
    plan = wire.get("planDefinition", wire.get("plan_definition", wire))
    if not isinstance(plan, Mapping):
        raise CanonicalJSONError("host plan definition must be an object")
    return dict(plan)


def plan_fingerprint(value: Mapping[str, Any] | BaseModel) -> str:
    return canonical_fingerprint(plan_semantic_projection(value))


def verification_contract_projection(
    value: Mapping[str, Any] | BaseModel,
) -> dict[str, Any]:
    """Return the verification contract without its computed fingerprint."""

    wire = (
        value.model_dump(mode="json", by_alias=True, exclude_none=False)
        if isinstance(value, BaseModel)
        else dict(value)
    )
    projection = dict(wire)
    projection.pop("contractFingerprint", None)
    projection.pop("contract_fingerprint", None)
    return projection


def verification_contract_fingerprint(value: Mapping[str, Any] | BaseModel) -> str:
    return canonical_fingerprint(verification_contract_projection(value))


def execution_admission_projection(
    value: Mapping[str, Any] | BaseModel,
) -> dict[str, Any]:
    """Return the immutable engine-owned execution admission authority.

    The admission is distinct from the authored plan and verification contract:
    it binds the selected workspace/provider plus the exact strategy and bounded
    runtime budget that the engine may execute after approval.
    """

    wire = (
        value.model_dump(mode="json", by_alias=True, exclude_none=False)
        if isinstance(value, BaseModel)
        else dict(value)
    )
    projection = dict(wire)
    projection.pop("fingerprint", None)
    return projection


def execution_admission_fingerprint(value: Mapping[str, Any] | BaseModel) -> str:
    return canonical_fingerprint(execution_admission_projection(value))


def approval_policy_projection(value: Mapping[str, Any] | BaseModel) -> dict[str, Any]:
    wire = (
        value.model_dump(mode="json", by_alias=True, exclude_none=False)
        if isinstance(value, BaseModel)
        else dict(value)
    )
    projection = dict(wire)
    projection.pop("fingerprint", None)
    return projection


def approval_policy_fingerprint(value: Mapping[str, Any] | BaseModel) -> str:
    return canonical_fingerprint(approval_policy_projection(value))


def approval_scope_projection(value: Mapping[str, Any] | BaseModel) -> dict[str, Any]:
    wire = (
        value.model_dump(mode="json", by_alias=True, exclude_none=False)
        if isinstance(value, BaseModel)
        else dict(value)
    )
    projection = dict(wire)
    projection.pop("fingerprint", None)
    return projection


def approval_scope_fingerprint(value: Mapping[str, Any] | BaseModel) -> str:
    return canonical_fingerprint(approval_scope_projection(value))


def approval_action_projection(
    *,
    operation_id: str,
    session_id: str,
    generation_id: str,
    run_id: str | None,
    revision: int,
    approval_id: str,
    action: Mapping[str, Any],
    scope_fingerprint: str,
    expires_at: str,
    risk: str,
    summary: str,
) -> dict[str, Any]:
    """Return the exact authority tuple bound by ``actionFingerprint``."""

    return {
        "action": dict(action),
        "approvalId": approval_id,
        "authority": {
            "generationId": generation_id,
            "revision": revision,
            "runId": run_id,
            "sessionId": session_id,
        },
        "expiresAt": expires_at,
        "operationId": operation_id,
        "risk": risk,
        "schema": "alysis.forge-host.approval-action/v2",
        "scopeFingerprint": scope_fingerprint,
        "summary": summary,
    }


def approval_action_fingerprint(
    *,
    operation_id: str,
    session_id: str,
    generation_id: str,
    run_id: str | None,
    revision: int,
    approval_id: str,
    action: Mapping[str, Any],
    scope_fingerprint: str,
    expires_at: str,
    risk: str,
    summary: str,
) -> str:
    return canonical_fingerprint(
        approval_action_projection(
            operation_id=operation_id,
            session_id=session_id,
            generation_id=generation_id,
            run_id=run_id,
            revision=revision,
            approval_id=approval_id,
            action=action,
            scope_fingerprint=scope_fingerprint,
            expires_at=expires_at,
            risk=risk,
            summary=summary,
        )
    )
