"""Literal paired corpus against engine ownership, with real raw parser provenance."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_computer_start_resolved_parser import parse, response

from alysis_code.agent.browser_start import BrowserStartHandoff
from alysis_code.agent.computer_start_resolved import Entry, Refusal, valid_descriptor
from alysis_code.llm import resolved_entry
from alysis_code.llm.openai_compat import OpenAICompatClient

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")

CORPUS = json.loads(
    (Path(__file__).parent / "fixtures/computer_use_start_resolved_v1.json").read_text()
)


def patched(name, ops):
    result = copy.deepcopy(CORPUS["bases"][name])
    for op in ops:
        keys = op["path"].split("/")[1:]
        parent = result
        for key in keys[:-1]:
            parent = parent[int(key)] if isinstance(parent, list) else parent[key]
        key = keys[-1]
        if isinstance(parent, list):
            if key == "-":
                parent.append(copy.deepcopy(op["value"]))
            elif op["op"] == "remove":
                parent.pop(int(key))
            elif op["op"] == "add":
                parent.insert(int(key), copy.deepcopy(op["value"]))
            else:
                parent[int(key)] = copy.deepcopy(op["value"])
        elif op["op"] == "remove":
            del parent[key]
        else:
            parent[key] = copy.deepcopy(op["value"])
    return result


def entry(descriptor=None, host=None):
    cfg = SimpleNamespace(model="fixture")
    client = OpenAICompatClient(
        base_url="https://fixture.invalid",
        api_key="fixture",
        model="fixture",
        transport=httpx.MockTransport(lambda _: pytest.fail("unexpected HTTP")),
    )
    calls = []

    def request(p, s, c):
        calls.append((p, s, c))
        return host(p, s, c) if host else BrowserStartHandoff("fixture")

    e = Entry(
        descriptor or copy.deepcopy(CORPUS["bases"]["descriptor"]), request, client, False, cfg
    )
    return e, calls, cfg


def invoke(e, cfg, plan):
    with e.model_request(e.client, False, cfg):
        call = parse("compat", response("compat", json.dumps(plan, ensure_ascii=True))).tool_calls[
            0
        ]
    return e.dispatch(call, call.arguments, e.client, False, cfg)


CASES = [
    c for g in ["positive", "negative"] for c in CORPUS[g] if c["stage"] in ["plan", "descriptor"]
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_corpus_producer_plan_and_descriptor(case):
    value = patched(case["base"], case["ops"])
    expected = case["expected"]
    if case["stage"] == "descriptor":
        assert valid_descriptor(value) is expected["valid"]
        return
    binding = patched(case["bindingBase"], case.get("bindingOps", []))
    e, calls, cfg = entry(binding["descriptor"])
    result = invoke(e, cfg, value)
    assert bool(calls) is expected["valid"]
    if expected["valid"]:
        assert calls[0][1] == expected["selection"]
    else:
        assert result["code"] == expected["code"]


def test_duplicate_labels_complete_projection_and_no_first_row():
    d = copy.deepcopy(CORPUS["bases"]["descriptor"])
    last = copy.deepcopy(d["discovery"]["targets"][0])
    last["target_id"] = "10000000-0000-4000-8000-000000000004"
    d["discovery"]["targets"].append(last)
    e, calls, cfg = entry(d)
    result = invoke(e, cfg, CORPUS["bases"]["plan"])
    assert result["code"] == "computer_start_ambiguous_selection" and not calls
    assert len(result["available_targets"]) == 4
    assert (
        result["available_targets"][0]["selection"]
        == result["available_targets"][-1]["selection"]
        == "ambiguous"
    )
    assert e.valid_correction(result)
    result["available_targets"].pop()
    assert not e.valid_correction(result)


@pytest.mark.parametrize("coverage", ["incomplete", "unavailable"])
def test_incomplete_no_host_and_retired(coverage):
    d = copy.deepcopy(CORPUS["bases"]["descriptor"])
    d["discovery"].update(coverage=coverage, targets=[])
    e, calls, cfg = entry(d)
    result = invoke(e, cfg, CORPUS["bases"]["plan"])
    assert not calls and e.retired and result["fresh_turn_required"]


def test_w3_latches_identity_not_action_or_order():
    d = copy.deepcopy(CORPUS["bases"]["descriptor"])
    target = d["discovery"]["targets"][0]["target_id"]
    e, calls, cfg = entry(
        d, lambda *_: {"error": "computer_use_target_unverified", "unverified_target_ids": [target]}
    )
    p = copy.deepcopy(CORPUS["bases"]["plan"])
    assert invoke(e, cfg, p)["code"] == "computer_use_target_unverified"
    p["operates_apps"] = True
    p["app_steps"][0]["actions"] = ["press"]
    assert invoke(e, cfg, p)["code"] == "computer_use_target_unverified"
    assert len(calls) == 1 and not e.retired


def test_missing_foreign_mutated_proof_and_browser_lock():
    for scenario in ["missing", "foreign", "mutated", "browser"]:
        e, calls, cfg = entry()
        with e.model_request(e.client, False, cfg):
            call = parse(
                "compat", response("compat", json.dumps(CORPUS["bases"]["plan"]))
            ).tool_calls[0]
        if scenario == "missing":
            object.__setattr__(call, "_resolved_entry_proof", None)
        if scenario == "foreign":
            e.owner = object()
        if scenario == "mutated":
            call.arguments["goal"] = "changed"
        if scenario == "browser":
            assert e.choose_browser()
        assert type(e.dispatch(call, call.arguments, e.client, False, cfg)) is Refusal
        assert not calls


@pytest.mark.parametrize("case", CORPUS["boundaries"], ids=lambda c: c["id"])
def test_literal_boundary_recipes(case, monkeypatch):
    from alysis_code.agent.computer_start_resolved import label, text

    kind = case["kind"]
    expected = case["expected"]
    p = copy.deepcopy(CORPUS["bases"]["plan"])
    d = copy.deepcopy(CORPUS["bases"]["descriptor"])
    if kind == "label":
        assert bool(label(case["text"])) is expected["valid"]
        return
    if kind == "unicode_forbidden":
        for point in case["codepoints"]:
            assert not label("A" + chr(point) + "B")
        return
    if kind == "actual_frame_limit":
        value = CORPUS["bases"]
        for key in case["base"].split("."):
            value = value[key]
        raw = resolved_entry.canonical(value)
        raw += b" " * (case["actual_received_bytes"] - len(raw))
        limits = {
            "descriptor": 131072,
            "discovery": 65536,
            "plan": 131072,
            "request": 262144,
            "response": 8192,
            "ack": 256,
            "correction": 65536,
            "refusal": 1024,
        }
        if expected["sizeValid"]:
            resolved_entry.strict_loads(raw, limit=limits[case["stage"]])
        else:
            with pytest.raises(ValueError):
                resolved_entry.strict_loads(raw, limit=limits[case["stage"]])
        return
    if kind == "identifier_units":
        assert text(case["repeat"]["text"] * case["repeat"]["count"], 256) is expected["valid"]
        return
    if kind in ("capture_loss", "capture_domain"):
        # Native coverage is APP-owned. Exercise ENGINE's resulting incomplete receipt.
        d["discovery"].update(coverage="incomplete", targets=[])
        e, calls, cfg = entry(d)
        assert invoke(e, cfg, p)["code"] == "computer_start_inventory_incomplete"
        assert not calls
        return
    if kind == "goal":
        p["goal"] = case["repeat"]["text"] * case["repeat"]["count"]
    if kind == "constraints":
        p["constraints"] = [case["repeat"]["text"] * case["repeat"]["count"]] * case["items"]
    if kind == "plan_steps":
        p["app_steps"] = [{"app": "Alpha", "actions": case["actions"]}] * case["steps"]
    if kind == "action_entries":
        p["app_steps"][0]["actions"] = [case["repeat"]["text"]] * case["repeat"]["count"]
        p["operates_apps"] = case["operates_apps"]
    if kind == "scope_precedence":
        families = []
        monkeypatch.setattr("alysis_code.agent.browser_start.scope_refusal", families.append)
        p.update(operates_apps=case["operates_apps"], app_steps=case["steps"])
        e, calls, cfg = entry(d)
        assert invoke(e, cfg, p)["code"] == "computer_start_scope_inconsistent"
        assert families == [expected["family"]]
        assert not calls
        return
    if kind in ("inventory_rows", "distinct_plan_apps", "inventory_maximal_labels"):
        recipe = (
            next(x for x in CORPUS["boundaries"] if x["id"] == case["reuseRecipe"])
            if "reuseRecipe" in case
            else case
        )
        n = recipe.get("rows", recipe.get("apps"))
        d["discovery"]["targets"] = [
            {
                "target_id": f"10000000-0000-4000-8000-{i:012x}",
                "name": (
                    "界" * 128
                    if kind == "inventory_maximal_labels"
                    else case.get("rowNameOverrides", {}).get(str(i), "App " + str(i))
                ),
                "route": "native",
            }
            for i in range(1, n + 1)
        ]
        if kind == "inventory_maximal_labels":
            assert len(resolved_entry.canonical(d["discovery"])) == expected["canonicalBytes"]
            if case.get("wireEscapeAllNonAscii"):
                raw = json.dumps(d["discovery"], sort_keys=True, separators=(",", ":")).encode()
                assert len(raw) == expected["actualBytes"]
                with pytest.raises(ValueError):
                    resolved_entry.strict_loads(raw, limit=65536)
                return
            p["app_steps"] = [{"app": "界" * 128, "actions": []}]
        elif kind == "distinct_plan_apps":
            p["app_steps"] = [
                {
                    "app": "App " + str(i),
                    "actions": case["firstActions"] if i == 1 else case["laterActions"],
                }
                for i in range(1, n + 1)
            ]
        else:
            p["app_steps"] = [{"app": case["selectedLabel"], "actions": []}]
        if not expected.get("captureValid", True):
            assert not valid_descriptor(d)
            return
    e, calls, cfg = entry(d)
    result = invoke(e, cfg, p)
    valid = expected.get("valid", expected.get("hostRequests", 0) > 0)
    assert bool(calls) is valid
    if "code" in expected:
        assert result["code"] == expected["code"]
    if "selectedTargetId" in expected:
        assert calls[0][1]["target_ids"] == [expected["selectedTargetId"]]


@pytest.mark.parametrize("case", CORPUS["classification"], ids=lambda c: c["id"])
def test_frozen_T_classification(case):
    from alysis_code.computer_use_attribution import classify

    assert classify(case["calls"], set(case["registered"])) == (
        case["expected"]["category"],
        case["expected"]["responseClass"],
    )


@pytest.mark.parametrize("code", ["computer_start_capability_lost", "computer_start_owner_invalid"])
@pytest.mark.parametrize("in_flight", [False, True])
def test_loss_dispatch_basis_preserves_precommit_denial(code, in_flight):
    result = Refusal(code, "unknown") if in_flight else {"error": code}
    e, _, cfg = entry(host=lambda *_: result)
    refusal = invoke(e, cfg, CORPUS["bases"]["plan"])
    assert refusal == Refusal(code, "unknown" if in_flight else "not_dispatched")
    assert refusal["fresh_turn_required"] is True
    assert e.retired == code
