"""Read recorded live evidence without model calls or semantic keyword scoring."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def jsonl(path: Path, *, partial: bool = False) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    records = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if partial and index == len(lines) - 1:
                break
            raise
    return records


def root_sessions(directory: Path, *, partial: bool = False):
    roots = []
    for path in sorted((directory / "data" / "sessions").glob("*.jsonl")):
        records = jsonl(path, partial=partial)
        start = next((r for r in records if r.get("type") == "session_start"), None)
        if start and start.get("payload", {}).get("subagent_depth") == 0:
            roots.append((path.stem, records))
    return roots


def completed_turns(directory: Path) -> list[dict[str, Any]]:
    # The wrapper observes real run_turn returns/exceptions, including provider
    # failures that the interactive CLI catches without a turn_finished event.
    observed = directory / "turn-boundaries.jsonl"
    if observed.exists():
        return jsonl(observed, partial=True)
    roots = root_sessions(directory, partial=True)
    if not roots:
        return []
    if len(roots) != 1:
        raise ValueError("A live chat attempt must retain exactly one root session")
    root_id = roots[0][0]
    return [
        record
        for record in jsonl(directory / "diagnostics.jsonl", partial=True)
        if record.get("session_id") == root_id and record.get("event_type") == "turn_finished"
    ]


def numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def normalized_usage(usage: dict[str, Any]) -> dict[str, Any]:
    values = dict(usage)
    if numeric(values.get("input_tokens_uncached")):
        values["uncached_prompt_tokens"] = values["input_tokens_uncached"]
    prompt, cached = values.get("prompt_tokens"), values.get("cached_prompt_tokens")
    if (
        not numeric(values.get("uncached_prompt_tokens"))
        and numeric(prompt)
        and numeric(cached)
        and cached <= prompt
    ):
        values["uncached_prompt_tokens"] = prompt - cached
    return values


def provider_observed_usage(record: dict[str, Any]) -> dict[str, Any]:
    """Count each actual HTTP attempt once when that evidence is available."""
    if numeric(record.get("attempt_count")) and record["attempt_count"] > 0:
        totals = dict(record.get("attempt_usage_totals") or {})
        # Aggregated prompt/cache totals may describe different subsets of
        # attempts. Only the independently observed uncached sum is usable.
        totals["uncached_prompt_tokens"] = totals.get("input_tokens_uncached")
        return totals
    return normalized_usage(record.get("usage") or {})


def provider_missing_counts(record: dict[str, Any], fields: tuple[str, ...]) -> dict[str, int]:
    observed = provider_observed_usage(record)
    if numeric(record.get("attempt_count")) and record["attempt_count"] > 0:
        count = record["attempt_count"]
        missing = record.get("attempt_usage_missing_counts") or {}
        result = {}
        for field in fields:
            key = "input_tokens_uncached" if field == "uncached_prompt_tokens" else field
            value = missing.get(key)
            result[field] = value if isinstance(value, int) and 0 <= value <= count else count
            if not numeric(observed.get(field)):
                result[field] = count
        return result
    return {field: int(not numeric(observed.get(field))) for field in fields}


def selected(payload: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {key: payload[key] for key in fields if key in payload}


def fingerprint(record: dict[str, Any], *, provider: bool = False) -> tuple:
    usage = (record.get("usage") or {}) if provider else record
    return (
        record.get("model") if provider else record.get("requested_model"),
        (record.get("request_plan") or {}).get("request_messages_signature"),
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
        usage.get("cached_prompt_tokens"),
    )


def summarize_evidence(directory: Path) -> dict[str, Any]:
    roots = root_sessions(directory)
    events = roots[0][1] if len(roots) == 1 else []
    usage = [normalized_usage(r.get("payload", {})) for r in events if r.get("type") == "llm_usage"]
    provider = [
        record
        for path in sorted(
            (directory / "data" / "sessions").glob("*/diagnostics/provider_telemetry.jsonl")
        )
        for record in jsonl(path)
    ]
    known_provider = [
        p
        for p in provider
        if numeric((p.get("usage") or {}).get("prompt_tokens"))
        and numeric((p.get("usage") or {}).get("completion_tokens"))
    ]
    matches = Counter(fingerprint(r) for r in usage) == Counter(
        fingerprint(r, provider=True) for r in known_provider
    )
    fields = (
        "prompt_tokens",
        "cached_prompt_tokens",
        "uncached_prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
    )
    observed = [provider_observed_usage(p) for p in provider] if provider else usage
    missing_records = [provider_missing_counts(p, fields) for p in provider]
    totals = {}
    logical_totals = {}
    missing = {}
    for field in fields:
        values = [u.get(field) for u in observed]
        missing[field] = (
            sum(row[field] for row in missing_records)
            if provider
            else sum(not numeric(value) for value in values)
        )
        totals[field] = sum(value for value in values if numeric(value))
        logical_totals[field] = sum(u[field] for u in usage if numeric(u.get(field)))
    diagnostics = jsonl(directory / "diagnostics.jsonl")
    failures = [
        {
            "event_type": d.get("event_type"),
            "session_id": d.get("session_id"),
            "payload": selected(
                d.get("payload", {}), ("failure_category", "operation", "step", "trigger")
            ),
        }
        for d in diagnostics
        if d.get("payload", {}).get("failure_category")
    ]
    finals = [
        selected(r.get("payload", {}), ("content", "controller_interventions_total"))
        for r in events
        if r.get("type") == "final"
    ]
    tool_counts = Counter(
        r.get("payload", {}).get("name") for r in events if r.get("type") == "tool_call"
    )
    unknown = (
        len(roots) != 1
        or not usage
        or len(known_provider) != len(provider)
        or not matches
        or any(
            missing[field]
            for field in (
                "prompt_tokens",
                "cached_prompt_tokens",
                "uncached_prompt_tokens",
                "completion_tokens",
            )
        )
        or any(
            numeric(u.get("cached_prompt_tokens"))
            and numeric(u.get("prompt_tokens"))
            and u["cached_prompt_tokens"] > u["prompt_tokens"]
            for u in observed
        )
    )
    return {
        "root_session_ids": [root_id for root_id, _ in roots],
        "usage_basis": "Provider records count parent and original child calls once, replacing final-response usage with all-attempt observed totals when available. Root replayed llm_usage is used only for logical-response reconciliation, never added again. Missing counters remain unknown; totals are observed lower bounds.",
        "known_usage": totals,
        "logical_response_usage": logical_totals,
        "additional_observed_attempt_usage": {
            field: (
                totals[field] - logical_totals[field]
                if matches and not missing[field] and totals[field] >= logical_totals[field]
                else None
            )
            for field in fields
        },
        "missing_usage_fields": missing,
        "provider_records_without_usage": len(provider) - len(known_provider),
        "known_usage_reconciles_with_provider": matches,
        "unknown_usage": unknown,
        "subscription_dollar_cost": None,
        "usage_calls": len(usage),
        "turns_finished": [
            {
                "event_type": d.get("event_type"),
                "session_id": d.get("session_id"),
                "payload": selected(
                    d.get("payload", {}),
                    (
                        "exit_code",
                        "reason",
                        "steps_attempted",
                        "exception_type",
                        "failure_category",
                        "turn",
                    ),
                ),
            }
            for d in completed_turns(directory)
        ],
        "finals": finals,
        "failure_events": failures,
        "tool_counts": dict(tool_counts),
        "verification": [
            selected(
                r.get("payload", {}),
                (
                    "commands",
                    "all_passed",
                    "status",
                    "executed_count",
                    "skipped_count",
                    "verification_evidence_allowed",
                    "verification_evidence_category",
                    "verification_contract_type",
                    "verification_authoritative",
                ),
            )
            for r in events
            if r.get("type") == "verify_run"
        ],
        "quality": "Unscored: complete the frozen manual rubric against source, final text and observed evidence.",
    }
