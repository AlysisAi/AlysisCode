"""Generic, deterministic facts from one fresh host observation, never a goal certificate."""

from __future__ import annotations

import unicodedata
from typing import Any

FACT_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "maxItems": 16,
    "items": {
        "type": "object",
        "properties": {
            "role": {"type": "string", "minLength": 1, "maxLength": 128},
            "name": {"type": "string", "minLength": 1, "maxLength": 512},
            "property": {
                "type": "string",
                "enum": ["value", "enabled", "disabled", "focused", "checked", "selected"],
            },
            "expected": {"anyOf": [{"type": "string", "maxLength": 2000}, {"type": "boolean"}]},
        },
        "required": ["role", "name", "property", "expected"],
        "additionalProperties": False,
    },
}


def valid_facts(facts: Any) -> bool:
    return (
        isinstance(facts, list)
        and 1 <= len(facts) <= 16
        and all(
            isinstance(f, dict)
            and set(f) == {"role", "name", "property", "expected"}
            and isinstance(f["role"], str)
            and 1 <= len(f["role"]) <= 128
            and isinstance(f["name"], str)
            and 1 <= len(f["name"]) <= 512
            and f["property"] in FACT_SCHEMA["items"]["properties"]["property"]["enum"]
            and (
                type(f["expected"]) is bool
                or isinstance(f["expected"], str)
                and len(f["expected"]) <= 2000
            )
            for f in facts
        )
    )


def observed_facts(state: dict[str, Any], facts: Any) -> dict[str, Any]:
    if not valid_facts(facts):
        raise ValueError("Invalid observation assertions")
    elements = state.get("elements", [])
    if not isinstance(elements, list) or not state.get("snapshot_id"):
        raise ValueError("Missing fresh host observation")

    def text(value: Any) -> str:
        return unicodedata.normalize("NFC", value) if isinstance(value, str) else ""

    def usable(element: dict[str, Any], field: str) -> bool:
        # Native placeholders and bounded prefixes are useful observations, but
        # cannot prove equality. Missing optional AX attributes are not empty values.
        native_names = {
            "role": "AXRole",
            "label": "AXDescription",
            "description": "AXDescription",
            "title": "AXTitle",
            "help": "AXHelp",
            "value": "AXValue",
            "enabled": "AXEnabled",
            "focused": "AXFocused",
        }
        attribute = native_names.get(field, field)
        reads = element.get("attribute_reads", {})
        truncated = element.get("truncated_attributes", [])
        return (
            isinstance(reads, dict)
            and attribute not in reads
            and isinstance(truncated, list)
            and attribute not in truncated
            and field in element
            and element[field] is not None
        )

    results = []
    for fact in facts:
        matches = [
            e
            for e in elements
            if isinstance(e, dict)
            and usable(e, "role")
            and e.get("role") == fact["role"]
            and text(fact["name"])
            in [text(e.get(k)) for k in ("label", "name", "title", "description") if usable(e, k)]
        ]
        if (
            len(matches) != 1
            or matches[0].get("secure") is True
            or matches[0].get("content_redacted") is True
            or matches[0].get("security_state", "ordinary") != "ordinary"
            or not usable(matches[0], fact["property"])
        ):
            results.append(
                {"status": "inconclusive", "reason": "missing_ambiguous_or_protected_observation"}
            )
            continue
        actual = matches[0][fact["property"]]
        if not (type(actual) is bool or isinstance(actual, str) and len(actual) <= 2000):
            results.append({"status": "inconclusive", "reason": "unsupported_observed_property"})
            continue
        expected = fact["expected"]
        equal = type(actual) is type(expected) and (
            text(actual) == text(expected) if isinstance(expected, str) else actual == expected
        )
        results.append({"status": "satisfied" if equal else "not_satisfied", "actual": actual})
    return {
        "kind": "deterministic_observed_facts",
        "snapshot_id": state["snapshot_id"],
        "observation_incomplete": bool(
            state.get("incomplete")
            or state.get("truncated")
            or isinstance(state.get("coverage"), dict)
            and state["coverage"].get("complete") is False
        ),
        "facts": results,
        "status": "satisfied"
        if all(r["status"] == "satisfied" for r in results)
        else "inconclusive"
        if any(r["status"] == "inconclusive" for r in results)
        else "not_satisfied",
        "scope": "Current rendered properties only. Expected facts are model proposals; task meaning, persistence, exhaustive coverage and overall completion require separate assessment.",
        "goal_completion": "not_certified",
    }
