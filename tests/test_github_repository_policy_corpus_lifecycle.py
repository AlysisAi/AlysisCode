"""Remaining corpus projections: production boundaries and labelled synthetic guards.

The manifest partitions every remaining ID. Fixture references are translated to
references minted through real discover/describe; flags never become authority.
"""

from __future__ import annotations

import copy
import json
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from test_computer_use_skill import isolated_paths as isolated_paths

from tests.test_github_repository_policy import CORPUS
from tests.test_github_repository_policy_production import ENGINE_CASES, run_case, sidecar

pytest_plugins = ["tests.test_github_mcp_plugin"]

COVERAGE = json.loads(
    (Path(__file__).parent / "fixtures/github_repository_policy/engine-coverage.json").read_text()
)
MAPPING = {row["id"]: row for row in COVERAGE["remaining"]}
CASES = [
    c for c in CORPUS["cases"] if c["id"] in MAPPING and MAPPING[c["id"]]["consumer"] != "app_only"
]


def test_complete_literal_coverage_partition():
    old = {c["id"] for c in ENGINE_CASES}
    new = {c["id"] for c in CASES}
    app = {r["id"] for r in COVERAGE["remaining"] if r["consumer"] == "app_only"}
    assert len(old) == 108 and len(new) == 47 and len(app) == 38
    assert sum(r.get("evidence") == "synthetic_owner_guard" for r in MAPPING.values()) == 20
    assert sum(r.get("evidence") == "production_engine_boundary" for r in MAPPING.values()) == 27
    assert sum(r.get("evidence") == "app_only" for r in MAPPING.values()) == 38
    assert len(MAPPING) == len(COVERAGE["remaining"]) == 85
    assert not (old & new or old & app or new & app)
    assert old | new | app == {c["id"] for c in CORPUS["cases"]}
    assert all(row["boundary"] for row in COVERAGE["remaining"])


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_remaining_literal_case_through_real_producer(installed, monkeypatch, case):
    group = MAPPING[case["id"]]["consumer"]
    action, name = case["action"], case["tool_name"]
    change = case["change_at"] or {}
    policy = {
        **sidecar(installed),
        **case["policy"],
        "workspace_repository": case["binding"]["workspace_repository"],
    }
    refs = {}
    desc_count = 0
    late_marker = "must-not-publish-retired-repository-result"
    mcp = action.startswith("mcp.")
    schema = next(
        (r.get("effectiveInputSchema") for r in CORPUS["schemas"] if r["name"] == name), None
    )
    descriptor = dict(
        name=name,
        description="Synthetic local definition",
        scope="repository",
        readOnly=name != "github_api_write",
        schema_hash="b" * 64,
    )

    def retire(captured):
        # Except descriptor bytes, this is synthetic private-owner injection,
        # not a production policy-update route. See the coverage evidence field.
        session = captured["session"]
        field = change.get("field", "policy_revision")
        if field == "plugin_manifest_sha256":
            from alysis_code.extensions.install import installed_plugin_root
            from alysis_code.extensions.manifest import resolve_manifest_path

            path = resolve_manifest_path(
                installed_plugin_root("alysis.github", project=False, repo_root=installed)
            )
            path.write_bytes(path.read_bytes() + b"\n# changed installed descriptor\n")
        else:
            value = (
                ("acme", "second")
                if field == "workspace_repository"
                else "00000000-0000-4000-8000-000000000099"
            )
            session.github_repository_policy = replace(
                session.github_repository_policy, **{field: value}
            )

    def reply(event, captured):
        nonlocal desc_count
        p = event["payload"]
        phase = p.get("phase", "execute")
        target_action = p["action"] == action
        if phase == "discard":
            return {"discarded": True}
        if phase == "prepare":
            if target_action and group == "interrupt_prepare":
                # Stop uses actual session.cancel in run_case; timeout leaves the
                # actual broker's pending preparation unanswered.
                return None
            if target_action and group in {"prepare_retirement", "binding_retirement"}:
                retire(captured)
            needs_person = target_action and case["person_approval"] in {"current", "denied"}
            if action == "mcp.github.describe" and desc_count:
                needs_person = False
            return dict(
                preparation_id="pa_" + uuid.uuid4().hex,
                required=needs_person,
                write_effect=needs_person and action != "mcp.github.describe",
                kind="mcp",
                reason="Fixture",
                preview="Synthetic action",
                target="fixture",
            )
        if p["action"] == "mcp.github.discover":
            # A real second catalog response invalidates the previously loaded
            # refs in GitHubDiscoveryState.record; no current=False injection.
            refs["catalog"] = "cat_" + uuid.uuid4().hex
            return dict(catalog_id=refs["catalog"], tools=[descriptor])
        if p["action"] == "mcp.github.describe":
            desc_count += 1
            refs.setdefault("target", "target_" + uuid.uuid4().hex)
            result_schema = copy.deepcopy(schema)
            if group == "effective_schema":
                result_schema["properties"]["path"]["enum"].append("search")
            if group == "post_dispatch" and target_action:
                retire(captured)
            return dict(
                catalog_id=refs["catalog"],
                target_id=refs["target"],
                tools=[{**descriptor, "inputSchema": result_schema}],
            )
        if group == "post_dispatch":
            retire(captured)
        if mcp:
            return dict(content=[dict(type="text", text=late_marker)], isError=False)
        # Native positives are not these rows: an unexpected dispatch is still
        # observed, but a fixed inert refusal avoids inventing provider results.
        return {
            "_error": dict(code="synthetic_host_refusal", message="Inert host", retryable=False)
        }

    if group == "native_schema":
        from alysis_code.tools import registry

        original = registry.copied_tool_parameters
        assert case["source_schema_sha256"] == "f" * 64
        monkeypatch.setattr(
            registry,
            "copied_tool_parameters",
            lambda tool: {**original(tool), "unreviewed": True} if tool == name else original(tool),
        )

    def describe():
        args = copy.deepcopy(
            case["arguments"]
            if action == "mcp.github.describe"
            else dict(names=[name], target=case["target"])
        )
        args["catalog_id"] = refs["catalog"]
        return "github_describe", json.dumps(args)

    def call():
        return "github_call", json.dumps(
            dict(
                catalog_id=refs["call_catalog"],
                target_id=refs.get("target", "unissued"),
                schema_hash="c" * 64
                if group == "runtime_refs" and not case["runtime_refs"]["schema_match"]
                else "b" * 64,
                name=name,
                arguments=case["arguments"],
            )
        )

    def save_call_refs():
        refs["call_catalog"] = refs["catalog"]
        return call()

    if not mcp:
        steps = [(name, json.dumps(case["arguments"]))]
    else:
        steps = [
            (
                "github_discover",
                json.dumps(case["arguments"]) if action == "mcp.github.discover" else "{}",
            )
        ]
        if action != "mcp.github.discover":
            steps.append(describe)
        if case["id"] == "G-rest-root-describe-repeated":
            steps.append(describe)
        if action == "mcp.github.call":
            if group == "runtime_refs" and not case["runtime_refs"]["current"]:

                def rediscover():
                    refs["call_catalog"] = refs["catalog"]
                    return "github_discover", "{}"

                steps += [rediscover, call]
            else:
                steps.append(save_call_refs)

    requests, events, raw, captured = run_case(
        installed,
        monkeypatch,
        "responses",
        False,
        steps,
        policy=policy,
        host_reply=reply,
        approval_protocol=1,
        approval_allow=group != "person_denial",
        on_approval=retire if group == "approval_retirement" else None,
        stop_on_host=group == "interrupt_prepare" and change["field"] == "cancelled",
    )
    emitted = [e["payload"] for e in events if e["payload"]["action"] == action]
    executed = [e for e in emitted if e.get("phase") == "execute"]
    approvals = captured.get("approvals", [])
    started = [
        e["payload"]
        for e in map(json.loads, raw.splitlines())
        if e.get("type") == "tool_call_started"
    ]
    if action == "mcp.github.call":
        assert any(e["name"] == "github_call" for e in started), started
    if group in {"runtime_refs", "effective_schema"}:
        assert any(
            e["payload"]["action"] == "mcp.github.describe"
            and e["payload"].get("phase") == "execute"
            for e in events
        )
    assert requests, "Must use a real provider/model request"
    assert "policy_revision" not in json.dumps(requests)
    if group == "discovery_control" and case["expected"]["decision"] == "allow":
        assert len(executed) == (2 if case["id"].endswith("repeated") else 1), raw[-2500:]
        assert len(approvals) == case["expected"]["person_approval_count"]
        assert "github_repository_policy_denied" not in raw
    elif group == "post_dispatch":
        assert len(executed) == 1, raw[-2500:]
        assert len(approvals) == case["expected"]["person_approval_count"]
        assert "github_repository_policy_denied" in raw
        assert late_marker not in json.dumps(requests)
        if action == "mcp.github.describe":
            assert refs["target"] not in json.dumps(requests)
            assert refs["target"] not in captured["session"].github_discovery.targets
        assert "unknown" in raw or "dispatched" in raw
    else:
        assert executed == [], (case["id"], executed, raw[-2000:])
        if group in {"person_denial", "approval_retirement"}:
            assert len(approvals) == 1
            assert any(e["phase"] == "prepare" for e in emitted)
            assert "not_dispatched" in raw
        elif group in {"prepare_retirement", "binding_retirement", "interrupt_prepare"}:
            assert len(emitted) == 1 and emitted[0]["phase"] == "prepare", raw[-2000:]
            assert approvals == []
        if group in {"native_schema", "prepare_retirement", "approval_retirement"}:
            assert "github_repository_policy_denied" in raw
        if group == "interrupt_prepare":
            assert ("cancelled" if change["field"] == "cancelled" else "host_action_timeout") in raw
        if group == "remote_name":
            assert not any(e["payload"]["action"] != "mcp.github.discover" for e in events)
