"""Byte-identical paired section T corpus against production closed admission."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from alysis_code import computer_use_attribution as a
from alysis_code.ide.protocol import parse_request_line

PATH = Path(__file__).parent / "fixtures/computer_use_attribution_v1.json"
V = json.loads(PATH.read_text())
B = V["bases"]


def patch(value, operations):
    for operation in operations:
        parts = operation["path"].lstrip("/").split("/")
        owner = value
        for part in parts[:-1]:
            owner = owner[int(part)] if isinstance(owner, list) else owner[part]
        key = int(parts[-1]) if isinstance(owner, list) else parts[-1]
        if operation["op"] == "remove":
            del owner[key]
        else:
            owner[key] = copy.deepcopy(operation["value"])
    return value


def variants(rows):
    for row in rows:
        if "eachReplace" not in row:
            yield row
            continue
        for path, values in row["eachReplace"].items():
            for value in values:
                yield {
                    **row,
                    "patch": [
                        *row.get("patch", []),
                        {"op": "replace", "path": path, "value": value},
                    ],
                }


def default_context(base):
    if base.startswith("protected"):
        return B["protectedStartupContext" if "Startup" in base else "protectedTurnContext"]
    if base.startswith("management"):
        return B["managementContext"]
    return B[
        "startupContext"
        if base.startswith(("config", "prompt", "encoder", "startup", "nestedFallback"))
        else "turnContext"
    ]


def prior_for(base):
    return {
        "configEnd": ["configBegin"],
        "configFailed": ["configBegin"],
        "modelEnd": ["modelBegin"],
        "protectedModelEnd": ["modelBegin"],
        "encoderBegin": ["promptBegin"],
        "encoderFallback": ["promptBegin", "encoderBegin"],
        "refusal": ["modelBegin", "modelEnd"],
        "turnSummary": ["modelBegin", "modelEnd", "refusal"],
        "protectedTurnSummary": ["modelBegin", "protectedModelEnd"],
        "startupSummary": ["configBegin", "configEnd"],
        "protectedStartupSummary": ["configBegin", "configEnd"],
        "managementSummary": ["configBegin", "configEnd"],
        "cancelSummary": ["modelBegin"],
        "nestedFallbackSummary": ["promptBegin", "encoderBegin", "encoderFallback", "promptEnd"],
    }.get(base, [])


def run_case(case):
    if "records" in case:
        context = copy.deepcopy(B[case.get("context", "turnContext")])
        admission = a.Admission(context)
        results = [admission.accept(copy.deepcopy(B[name])) for name in case["records"]]
        return results
    base = case["base"]
    original = copy.deepcopy(B[base])
    value = patch(copy.deepcopy(original), case.get("patch", []))
    value.update(copy.deepcopy(case.get("add", {})))
    if base == "feature":
        return a.valid_feature(value)
    if base.endswith("Context"):
        return a.valid_context(value)
    context = copy.deepcopy(B[case["context"]] if "context" in case else default_context(base))
    context = patch(context, case.get("contextPatch", []))
    if base.endswith("Reply") or base.endswith("Event"):
        context = copy.deepcopy(
            original.get("payload", original.get("computer_use_attribution"))["context"]
        )
        binding = {
            "context": context,
            "requestId": original.get("id", "q2"),
            "method": "chat.send" if context["contextKind"] == "turn" else "session.create",
            "newJob": True,
            "sessionId": "s",
            "jobId": "j",
            "runId": "r",
            "state": "active",
            "lastSequence": 0,
            "summaryAccepted": False,
            "bridgeIsCurrent": True,
        }
        binding.update(copy.deepcopy(case.get("binding", {})))
        if isinstance(binding["context"], str):
            binding["context"] = copy.deepcopy(B[binding["context"]])
        binding.update(case.get("bindingPatch", {}))
        if base.endswith("Reply"):
            return a.valid_sidecar(value, binding)
        admission = a.Admission(binding["context"])
        for name in binding.get("prior", []):
            assert admission.accept(copy.deepcopy(B[name]))
        before = copy.deepcopy(binding)
        result = a.accept_event(value, binding, admission)
        if not result:
            assert binding == before, (
                "malformed telemetry must not advance authenticated binding/sequence"
            )
        return result
    admission = a.Admission(context)
    prior = case.get("prior", prior_for(base))
    if isinstance(prior, str):
        prior = [prior]
    for name in prior:
        # Context mutation can make an otherwise valid prefix inapplicable.
        admission.accept(copy.deepcopy(B[name]))
    return admission.accept(value)


def test_fixture_identity():
    assert (
        hashlib.sha256(PATH.read_bytes()).hexdigest()
        == "f3a072f56254ff27eec84424aeba5016b4b2b82e87c0f065084503da9a025107"
    )


@pytest.mark.parametrize("case", list(variants(V["positive"])), ids=lambda x: x["id"])
def test_positive_contract(case):
    result = run_case(case)
    assert all(result) if isinstance(result, list) else result


@pytest.mark.parametrize("case", list(variants(V["negative"])), ids=lambda x: x["id"])
def test_negative_contract(case):
    result = run_case(case)
    if isinstance(result, list):
        assert result == [expect.startswith("accept") for expect in case["expect"]]
    else:
        assert result is False


@pytest.mark.parametrize("row", V["classification"])
def test_classification(row):
    if row["names"] is None:
        # Before-response outcomes are produced by the model wrapper's exception path.
        assert row["responseClass"] == "no_response" and row["category"] == "other"
    else:
        assert a.classify(
            row["names"], {"computer_discover", "computer_start", "computer_state"}
        ) == (row["category"], row["responseClass"])


def test_duplicate_attribution_never_changes_business_parse():
    wire = V["lexical"][0]["wire"]
    request = parse_request_line(wire)
    assert request.id == "q1" and request.params == {} and request.computer_use_attribution is None
    # Duplicate business keys retain the pre-existing parser behavior.
    request = parse_request_line(
        '{"protocol_version":"1","id":"a","method":"health","params":{"x":1,"x":2}}'
    )
    assert request.params == {"x": 2}
