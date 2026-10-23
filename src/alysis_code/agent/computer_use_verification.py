"""Optional host-only predicate proposals; page observations never admit proofs."""

from __future__ import annotations

from typing import Any

COMPUTER_USE_VERIFICATION_TOOL_NAMES = ("computer_verify",)
COMPUTER_USE_VERIFICATION_PROMPT = """
When computer_verify is available, propose a supported host verification predicate using only
observed field/table IDs, exact column header labels and account/filter/period witness IDs.
The desktop derives expected values and scope from the original request. A proposal does not
change that request, authorize another action, or prove success. For a record search, read all
document chunks of the first history page and propose its mapping before moving to the next
website page. The desktop needs that starting page to collect and reconcile the full history.
After each website page or filter changes, use computer_state and read all its document chunks;
the action receipt alone is not an independent read. Use operation evaluate at the observed end
of the traversal; the desktop performs another independent read before evaluating. A verified
records result is also available to the user in the extension's verified-results view; preserve
its scope, count and totals in your response. A verified evaluation ends the task's browser
authority: answer from that result without requesting more page reads or actions. A host result marked
inconclusive is a disclosed verification limit, not permission to retry mutations. Rendered
fields and observed site-history pages are as-of evidence, never an atomic backend snapshot.
For a native form change, propose {"kind":"form"} from the initial independent observation
before editing. The host binds every requested field and its expected value from the original
request; do not supply a subset, expected values, or proof flags. After making the requested
changes and saving once, evaluate. The host requires an independent read and may revisit the
same form with a fresh GET to check saved values. Evaluate with exactly {"operation":"evaluate"};
omit predicate and every other field. It does not resubmit Save. A success toast
or an edited field alone does not establish saved state. Preserve the host's observed-form
evidence scope and report an inconclusive result honestly without repeating the mutation.
After computer_verify, call computer_state before another action because observation authority
was reset. Never invent missing witness IDs or ask the verifier to ignore requested constraints.
"""

_ID = {"type": "string", "minLength": 1, "maxLength": 128}
_NULLABLE_ID = {"anyOf": [_ID, {"type": "null"}]}
_COLUMN = {"anyOf": [{"type": "string", "minLength": 1, "maxLength": 255}, {"type": "null"}]}
_COLUMNS = (
    "record_id",
    "amount",
    "outstanding_amount",
    "charged_amount",
    "status",
    "date",
    "merchant",
)
_WITNESSES = (
    "account_element_id",
    "status_element_id",
    "merchant_element_id",
    "period_element_id",
    "period_from_element_id",
    "period_through_element_id",
)
COMPUTER_USE_VERIFICATION_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operation": {"type": "string", "enum": ["propose", "evaluate"]},
        "predicate": {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {"kind": {"const": "form"}},
                    "required": ["kind"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {"kind": {"const": "field"}, "element_id": _ID},
                    "required": ["kind", "element_id"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "records"},
                        "table_id": _ID,
                        "columns": {
                            "type": "object",
                            "properties": {
                                name: (
                                    {"type": "string", "minLength": 1, "maxLength": 255}
                                    if name == "record_id"
                                    else _COLUMN
                                )
                                for name in _COLUMNS
                            },
                            "required": list(_COLUMNS),
                            "additionalProperties": False,
                        },
                        "witnesses": {
                            "type": "object",
                            "properties": {
                                name: (_ID if name == "account_element_id" else _NULLABLE_ID)
                                for name in _WITNESSES
                            },
                            "required": list(_WITNESSES),
                            "additionalProperties": False,
                        },
                    },
                    "required": ["kind", "table_id", "columns", "witnesses"],
                    "additionalProperties": False,
                },
            ],
        },
    },
    "required": ["operation"],
    # Explicit alternatives survive provider schema lowering, which does not
    # preserve if/then. Evaluation accepts no predicate, including null.
    "oneOf": [
        {"properties": {"operation": {"enum": ["propose"]}}, "required": ["predicate"]},
        {
            "type": "object",
            "properties": {"operation": {"enum": ["evaluate"]}},
            "required": ["operation"],
            "additionalProperties": False,
        },
    ],
    "additionalProperties": False,
}


def validate_computer_use_verification_arguments(value: dict[str, Any]) -> bool:
    def text(item: Any, maximum: int, nullable: bool = False) -> bool:
        return (nullable and item is None) or (
            isinstance(item, str) and 0 < len(item) <= maximum and "\x00" not in item
        )

    if not isinstance(value, dict):
        return False
    if value.get("operation") == "evaluate":
        return set(value) == {"operation"}
    if value.get("operation") != "propose" or set(value) != {"operation", "predicate"}:
        return False
    predicate = value.get("predicate")
    if not isinstance(predicate, dict):
        return False
    if predicate.get("kind") == "form":
        return set(predicate) == {"kind"}
    if predicate.get("kind") == "field":
        return set(predicate) == {"kind", "element_id"} and text(predicate["element_id"], 128)
    if (
        predicate.get("kind") != "records"
        or set(predicate) != {"kind", "table_id", "columns", "witnesses"}
        or not text(predicate["table_id"], 128)
    ):
        return False
    columns, witnesses = predicate["columns"], predicate["witnesses"]
    return (
        isinstance(columns, dict)
        and set(columns) == set(_COLUMNS)
        and all(text(columns[name], 255, name != "record_id") for name in _COLUMNS)
        and isinstance(witnesses, dict)
        and set(witnesses) == set(_WITNESSES)
        and all(text(witnesses[name], 128, name != "account_element_id") for name in _WITNESSES)
    )
