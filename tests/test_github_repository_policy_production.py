"""Raw provider responses -> production run_turn -> real tools and bridge broker.

Synthetic host ports mint references; these tests do not claim APP transport coverage.
"""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import threading
import uuid

import httpx
import pytest
from test_computer_start_resolved_review import text_body
from test_computer_start_resolved_turn import body as provider_body
from test_computer_use_skill import isolated_paths as isolated_paths

from alysis_code import token_budget
from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.mcp.github_host import active_github_plugin
from tests.test_github_discovery import BOOTSTRAP
from tests.test_github_repository_policy import CORPUS, payload
from tests.test_ide_host_actions import _lines, _request, _respond, _response_for

pytest_plugins = ["tests.test_github_mcp_plugin"]


def call_body(protocol, stream, name, args):
    return provider_body(protocol, stream, args).replace(
        b"computer_start_resolved_v1", name.encode()
    )


def sidecar(root):
    return {**payload(), "plugin_manifest_sha256": active_github_plugin(root)["manifest_sha256"]}


def run_case(
    root,
    monkeypatch,
    protocol,
    stream,
    steps,
    *,
    policy=None,
    host_reply=None,
    approval_protocol=0,
    mode="auto",
    stop_on_host=False,
    approval_allow=True,
    on_approval=None,
    host_actions=None,
    workspace_trusted=True,
    session_params=None,
    agent_mcp_manager=None,
    expected_create_error=None,
):
    requests = []
    host = []
    errors = []
    captured = {}
    monkeypatch.setenv("ALYSIS_API_KEY", "fixture")
    original_run = subprocess.run

    def inert_git(command, *a, **kw):
        if isinstance(command, (list, tuple)) and os.path.basename(str(command[0])) == "git":
            return subprocess.CompletedProcess(
                command,
                128,
                "" if kw.get("text") else b"",
                "not a git repository" if kw.get("text") else b"not a git repository",
            )
        return original_run(command, *a, **kw)

    monkeypatch.setattr(subprocess, "run", inert_git)
    monkeypatch.setattr(token_budget, "_token_encoder", lambda: None)
    # Snapshot Git storage is an inert port; policy, model, tools and broker stay real.
    monkeypatch.setattr(stdio_bridge, "_checkpointing_supported", lambda _: False)
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(
            model="test-model",
            routing_mode="code_only",
            web_search_mode="off",
            stream=stream,
            skills_auto_invoke=False,
        ),
    )

    def provider(request):
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        requests.append(json.loads(request.content))
        index = len(requests) - 1
        if index < len(steps):
            step = steps[index]() if callable(steps[index]) else steps[index]
            raw = call_body(protocol, stream, step[0], step[1])
        else:
            raw = text_body(protocol, stream)
        raw = raw.replace(b"call-1", f"call-{index + 1}".encode()).replace(
            b"fc-1", f"fc-{index + 1}".encode()
        )
        return httpx.Response(
            200,
            content=raw,
            headers={"content-type": "text/event-stream" if stream else "application/json"},
        )

    cls = OpenAIResponsesClient if protocol == "responses" else OpenAICompatClient
    client = cls(
        base_url="https://fixture.invalid/v1",
        api_key="fixture",
        model="test-model",
        transport=httpx.MockTransport(provider),
    )
    monkeypatch.setattr("alysis_code.agent.session._make_session_llm_client", lambda **_: client)
    out = io.StringIO()
    bridge = stdio_bridge.StdioBridge(stdout=out, host_action_timeout_seconds=2)
    create_session = bridge._create_session

    def capture_create(**kwargs):
        if agent_mcp_manager is not None:
            kwargs["mcp_manager"] = agent_mcp_manager
        captured["create_kwargs"] = kwargs
        return create_session(**kwargs)

    bridge._create_session = capture_create
    value = sidecar(root) if policy is None else policy
    policy_args = {} if value == "absent" else {"github_repository_policy": value}
    actions = [
        "github.repo.read",
        "github.pr.list",
        "github.pr.create",
        "github.git.fetch",
        "github.git.pull",
        "github.git.push",
        "connectors.github.repositories",
        "connectors.status",
    ]
    if any(
        (s[0] if not callable(s) else "") in {"github_discover", "github_describe", "github_call"}
        for s in steps
    ):
        actions += ["mcp.github.discover", "mcp.github.describe", "mcp.github.call"]
        catalog = {"host_mcp": BOOTSTRAP}
    else:
        catalog = {}
    if host_actions is not None:
        actions = list(host_actions)
    catalog.update(session_params or {})
    bridge.process_line(
        _request(
            "session.create",
            dict(
                workspace=str(root),
                mode=mode,
                workspace_trusted=workspace_trusted,
                model="test-model",
                max_steps=12,
                no_log=True,
                host_capabilities=dict(
                    protocol_version="1", approval_protocol=approval_protocol, actions=actions
                ),
                **policy_args,
                **catalog,
            ),
            "create",
        )
    )
    created = _response_for(out, "create")
    if expected_create_error is not None:
        bridge.close()
        assert created["error"]["code"] == expected_create_error, created
        return requests, host, out.getvalue(), captured
    assert created["ok"], created
    sid = created["result"]["session_id"]
    captured["session"] = bridge._sessions[sid]
    done = threading.Event()

    def respond():
        seen = set()
        try:
            while not done.wait(0.005):
                for event in _lines(out):
                    if (
                        event.get("type") == "prompt_for_input"
                        and event["payload"].get("kind") == "approval"
                    ):
                        approval_id = event["payload"]["approval_id"]
                        if approval_id in seen:
                            continue
                        seen.add(approval_id)
                        captured.setdefault("approvals", []).append(event)
                        if on_approval:
                            on_approval(captured)
                        bridge.process_line(
                            _request(
                                "approval.respond",
                                dict(
                                    session_id=sid,
                                    approval_id=approval_id,
                                    allow=approval_allow,
                                    allow_for_session=False,
                                ),
                                "approval-" + approval_id,
                            )
                        )
                        continue
                    if event.get("type") != "host_action_requested":
                        continue
                    key = event["payload"]["host_action_id"]
                    if key in seen:
                        continue
                    seen.add(key)
                    host.append(event)
                    if stop_on_host:
                        bridge.process_line(
                            _request("session.cancel", dict(session_id=sid), "cancel")
                        )
                        continue
                    result = (
                        host_reply(event, captured)
                        if host_reply
                        else {"content": [{"type": "text", "text": "synthetic"}], "isError": False}
                    )
                    if result is None:
                        # An inert non-responding host exercises the real broker deadline.
                        continue
                    if "_error" in result:
                        _respond(
                            bridge,
                            event,
                            request_id="host-" + str(len(host)),
                            error=result["_error"],
                        )
                    else:
                        _respond(bridge, event, request_id="host-" + str(len(host)), result=result)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=respond, daemon=True)
    thread.start()
    try:
        bridge.process_line(
            _request(
                "chat.send",
                dict(
                    session_id=sid, message="Perform the requested synthetic repository operation."
                ),
                "chat",
            )
        )
        ack = _response_for(out, "chat")
        assert ack["ok"], ack
        job = bridge._sessions[sid].active_job
        if job is not None and job.thread is not None:
            job.thread.join(20)
        assert job is None or not job.thread.is_alive(), out.getvalue()[-3000:]
        assert not errors, errors
        return requests, host, out.getvalue(), captured
    finally:
        done.set()
        thread.join(3)
        bridge.close()


@pytest.mark.parametrize("protocol", ["responses", "compat"])
@pytest.mark.parametrize("stream", [False, True])
def test_raw_duplicate_page_never_reaches_host(installed, monkeypatch, protocol, stream):
    requests, host, raw, _ = run_case(
        installed, monkeypatch, protocol, stream, [("github_repositories", '{"page":1,"page":2}')]
    )
    assert len(requests) >= 2
    assert host == []
    assert "_invalid_github_arguments" not in json.dumps(requests[0])
    assert "account_link_revision" not in json.dumps(requests)
    assert "checkpoint_unavailable" not in raw
    assert any(
        e.get("type") == "tool_call_completed" and e["payload"]["success"] is False
        for e in map(json.loads, raw.splitlines())
    )


@pytest.mark.parametrize("protocol", ["responses", "compat"])
@pytest.mark.parametrize("stream", [False, True])
def test_real_discover_describe_call_normalizes_before_host(
    installed, monkeypatch, protocol, stream
):
    refs = {}
    catalog = "cat_" + uuid.uuid4().hex
    target = "target_" + uuid.uuid4().hex
    row = next(r for r in CORPUS["schemas"] if r["name"] == "github_api_read")
    descriptor = dict(
        name="github_api_read",
        description="Read root or branches.",
        scope="repository",
        readOnly=True,
        schema_hash="b" * 64,
    )

    def reply(event, captured):
        action = event["payload"]["action"]
        if action == "mcp.github.discover":
            refs["catalog"] = catalog
            return dict(catalog_id=catalog, tools=[descriptor])
        if action == "mcp.github.describe":
            refs["target"] = target
            return dict(
                catalog_id=catalog,
                target_id=target,
                tools=[{**descriptor, "inputSchema": row["effectiveInputSchema"]}],
            )
        return {"content": [{"type": "text", "text": "synthetic result"}], "isError": False}

    steps = [
        ("github_discover", "{}"),
        lambda: (
            "github_describe",
            json.dumps(
                dict(
                    catalog_id=refs["catalog"],
                    names=["github_api_read"],
                    target=dict(scope="workspace"),
                )
            ),
        ),
        lambda: (
            "github_call",
            json.dumps(
                dict(
                    catalog_id=refs["catalog"],
                    target_id=refs["target"],
                    schema_hash="b" * 64,
                    name="github_api_read",
                    arguments=dict(path="branches"),
                )
            ),
        ),
    ]
    requests, host, raw, _ = run_case(
        installed, monkeypatch, protocol, stream, steps, host_reply=reply
    )
    assert len(host) == 3, raw[-5000:]
    assert host[-1]["payload"]["arguments"]["arguments"] == dict(
        path="branches", query=dict(page="1", per_page="100")
    )
    model = json.dumps(requests)
    assert "account_link_revision" not in model and "policy_revision" not in model
    assert row["effectiveInputSchema"]["properties"]["path"]["enum"] == ["", "branches"]
    assert "Other routes, search, GraphQL" in json.dumps(requests[0])


@pytest.mark.parametrize(
    "argument", [dict(scope="account"), dict(scope="repository", owner="acme", repo="forbidden")]
)
def test_forbidden_describe_has_no_host_verification(installed, monkeypatch, argument):
    catalog = "cat_" + uuid.uuid4().hex
    descriptor = dict(
        name="github_api_read",
        description="Read",
        scope="repository",
        readOnly=True,
        schema_hash="b" * 64,
    )

    def reply(event, _):
        return dict(catalog_id=catalog, tools=[descriptor])

    steps = [
        ("github_discover", "{}"),
        (
            "github_describe",
            json.dumps(dict(catalog_id=catalog, names=["github_api_read"], target=argument)),
        ),
    ]
    _, host, _, _ = run_case(installed, monkeypatch, "responses", False, steps, host_reply=reply)
    assert [x["payload"]["action"] for x in host] == ["mcp.github.discover"]


# These corpus rows exercise engine-owned admission/argument behavior. Transport,
# credentials, human IPC and main-only mutations are deliberately not simulated as
# engine authority. Their paired consumer coverage belongs to APP.
ENGINE_CASES = [
    c
    for c in CORPUS["cases"]
    if c["entry"] == "plugin"
    and c["change_at"] is None
    and c["source_kind"] in {"engine_native", "local_rest"}
    and c["expected"]["reason"]
    in {
        "allowed",
        "repository_denied",
        "invalid_arguments",
        "policy_unavailable",
        "unsupported_scope",
        "unsupported_or_denied_target",
    }
    and c["action"] not in {"mcp.github.discover", "mcp.github.describe"}
]


@pytest.mark.parametrize("case", ENGINE_CASES, ids=lambda c: c["id"])
def test_pinned_corpus_through_provider_tool_and_broker(installed, monkeypatch, case):
    policy = {
        **sidecar(installed),
        **(case["policy"] or {}),
        "workspace_repository": case["binding"]["workspace_repository"],
    }
    missing = case["policy"] is None or not case["binding"]["capability_supported"]
    if missing:
        policy = "absent"
    action = case["action"]
    refs = {}
    name = case["tool_name"]
    steps = []
    if action == "mcp.github.call":
        row = next(r for r in CORPUS["schemas"] if r["name"] == name)
        descriptor = dict(
            name=name,
            description="Local restricted REST definition",
            scope="repository",
            readOnly=name == "github_api_read",
            schema_hash="b" * 64,
        )
        catalog = "cat_" + uuid.uuid4().hex
        target = "target_" + uuid.uuid4().hex
        steps.append(("github_discover", "{}"))
        steps.append(
            (
                "github_describe",
                json.dumps(dict(catalog_id=catalog, names=[name], target=case["target"])),
            )
        )
        args = case.get("raw_arguments_json", json.dumps(case["arguments"]))
        envelope = dict(catalog_id=catalog, target_id=target, schema_hash="b" * 64, name=name)
        raw = json.dumps(envelope)[:-1] + ',"arguments":' + args + "}"
        steps.append(("github_call", raw))

        def reply(event, _):
            a = event["payload"]["action"]
            if a == "mcp.github.discover":
                refs["catalog"] = catalog
                return dict(catalog_id=catalog, tools=[descriptor])
            if a == "mcp.github.describe":
                refs["target"] = target
                return dict(
                    catalog_id=catalog,
                    target_id=target,
                    tools=[{**descriptor, "inputSchema": row["effectiveInputSchema"]}],
                )
            return dict(content=[dict(type="text", text="synthetic")], isError=False)
    else:
        steps = [(name, case.get("raw_arguments_json", json.dumps(case["arguments"])))]

        def reply(event, _):
            # Inspect the real emitted request; a closed inert host refusal avoids
            # inventing a provider result for native rows. No repository port runs.
            return {
                "_error": dict(code="synthetic_host_refusal", message="Inert host", retryable=False)
            }

    requests, events, raw, _ = run_case(
        installed, monkeypatch, "responses", False, steps, policy=policy, host_reply=reply
    )
    emitted = [e for e in events if e["payload"]["action"] == action]
    if case["expected"]["decision"] == "allow":
        assert len(emitted) == 1, raw[-2500:]
        sent = emitted[0]["payload"]["arguments"]
        if action == "mcp.github.call":
            sent = sent["arguments"]
        expected = copy.deepcopy(case["arguments"])
        if action == "connectors.github.repositories":
            expected.setdefault("page", 1)
        if action == "github.pr.create":
            expected.setdefault("draft", True)
        if name == "github_api_write":
            expected["body"].setdefault("draft", True)
        if name == "github_api_read" and expected["path"] == "branches":
            query = expected.setdefault("query", {})
            query.setdefault("page", "1")
            query.setdefault("per_page", "100")
        assert sent == expected
    else:
        assert emitted == [], (case["id"], emitted)
    assert "policy_revision" not in json.dumps(requests)


@pytest.mark.parametrize("allow", [False, True])
def test_restricted_write_asks_person_and_binds_normalized_draft(installed, monkeypatch, allow):
    def reply(event, _):
        phase = event["payload"]["phase"]
        if phase == "prepare":
            assert event["payload"]["arguments"]["draft"] is True
            return dict(
                preparation_id="pa_" + "b" * 32,
                required=True,
                write_effect=True,
                kind="mcp",
                reason="Fixture",
                preview="Create PR",
                target="@fixture acme/allowed",
            )
        if phase == "discard":
            return {"discarded": True}
        assert event["payload"]["arguments"]["draft"] is True
        return {
            "_error": dict(
                code="synthetic_host_refusal", message="Inert dispatched host", retryable=False
            )
        }

    _, events, _, captured = run_case(
        installed,
        monkeypatch,
        "responses",
        False,
        [
            (
                "github_pr_create",
                json.dumps(dict(base="main", head="feature", title=" t ", body="\n")),
            )
        ],
        host_reply=reply,
        approval_protocol=1,
        mode="fullaccess",
        approval_allow=allow,
    )
    assert len(captured["approvals"]) == 1
    approval = captured["approvals"][0]["payload"]
    assert approval["metadata"]["always_ask_person"] is True
    assert approval["allow_for_session_supported"] is False
    assert [e["payload"]["phase"] for e in events] == ["prepare", "execute" if allow else "discard"]


@pytest.mark.parametrize("phase", ["prepare", "approval", "execute"])
def test_policy_retirement_during_real_wait_never_retries(installed, monkeypatch, phase):
    """Synthetic owner replacement tests guards; APP retirement delivery is separate."""
    from dataclasses import replace

    def retire(captured):
        session = captured["session"]
        session.github_repository_policy = replace(
            session.github_repository_policy, policy_revision="00000000-0000-4000-8000-000000000003"
        )

    def reply(event, captured):
        current = event["payload"]["phase"]
        if current == phase:
            retire(captured)
        if current == "prepare":
            return dict(
                preparation_id="pa_" + "c" * 32,
                required=True,
                write_effect=True,
                kind="mcp",
                reason="Fixture",
                preview="Create PR",
                target="fixture",
            )
        # Valid bounded result so the post-await engine guard, not an inert host
        # error, must retire the response after dispatch.
        return dict(
            repository="acme/allowed",
            url="https://github.com/acme/allowed/pull/1",
            number=1,
            account_login="fixture",
        )

    _, events, raw, captured = run_case(
        installed,
        monkeypatch,
        "responses",
        False,
        [("github_pr_create", json.dumps(dict(base="main", head="feature", title="t", body="")))],
        host_reply=reply,
        approval_protocol=1,
        on_approval=retire if phase == "approval" else None,
    )
    phases = [e["payload"]["phase"] for e in events]
    assert phases == (["prepare", "execute"] if phase == "execute" else ["prepare"]), raw[-2500:]
    assert len(captured.get("approvals", [])) == (0 if phase == "prepare" else 1)
    assert "github_repository_policy_denied" in raw, raw[-2500:]
    if phase == "execute":
        assert "unknown" in raw


@pytest.mark.parametrize("protocol", ["responses", "compat"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("case_id", ["G-pr-create-duplicate-key", "G-rest-root-duplicate-key"])
def test_other_raw_corpus_duplicates_use_actual_provider_boundary(
    installed, monkeypatch, protocol, stream, case_id
):
    case = next(c for c in CORPUS["cases"] if c["id"] == case_id)
    if case["action"] == "mcp.github.call":
        name = "github_call"
        raw = (
            '{"catalog_id":"unissued","target_id":"unissued","schema_hash":"'
            + ("b" * 64)
            + '","name":"github_api_read","arguments":'
            + case["raw_arguments_json"]
            + "}"
        )
    else:
        name = case["tool_name"]
        raw = case["raw_arguments_json"]
    requests, events, output, _ = run_case(installed, monkeypatch, protocol, stream, [(name, raw)])
    assert events == []
    starts = [
        e for e in map(json.loads, output.splitlines()) if e.get("type") == "tool_call_started"
    ]
    assert starts and "_invalid_github_arguments" in starts[0]["payload"]["arguments_preview"], (
        starts
    )


@pytest.mark.parametrize(
    "mutation", ["unknown_name", "account_scope", "effective_schema", "native_source"]
)
def test_unreviewed_definitions_refused_in_real_lifecycle(installed, monkeypatch, mutation):
    row = next(r for r in CORPUS["schemas"] if r["name"] == "github_api_read")
    catalog = "cat_" + uuid.uuid4().hex
    desc = dict(
        name="github_api_read",
        description="Read",
        scope="repository",
        readOnly=True,
        schema_hash="b" * 64,
    )
    if mutation == "unknown_name":
        desc["name"] = "search_repositories"
    if mutation == "account_scope":
        desc["scope"] = "account"
    schema = copy.deepcopy(row["effectiveInputSchema"])
    if mutation == "effective_schema":
        schema["properties"]["path"]["enum"].append("search")

    def reply(event, _):
        if event["payload"]["action"] == "mcp.github.discover":
            return dict(catalog_id=catalog, tools=[desc])
        return dict(
            catalog_id=catalog,
            target_id="target_" + uuid.uuid4().hex,
            tools=[{**desc, "inputSchema": schema}],
        )

    if mutation == "native_source":
        from alysis_code.tools import registry

        original = registry.copied_tool_parameters
        monkeypatch.setattr(
            registry,
            "copied_tool_parameters",
            lambda name: (
                {**original(name), "unexpected": True}
                if name == "github_repo_read"
                else original(name)
            ),
        )
        steps = [("github_repo_read", "{}")]
    else:
        steps = [
            ("github_discover", "{}"),
            (
                "github_describe",
                json.dumps(
                    dict(
                        catalog_id=catalog,
                        names=["github_api_read"],
                        target=dict(scope="workspace"),
                    )
                ),
            ),
        ]
    _, events, output, _ = run_case(
        installed, monkeypatch, "responses", False, steps, host_reply=reply
    )
    assert len(events) == (
        0 if mutation == "native_source" else 2 if mutation == "effective_schema" else 1
    )
    assert "github_repository_policy_denied" in output


def test_stop_while_host_pending_has_no_second_request(installed, monkeypatch):
    _, events, raw, _ = run_case(
        installed, monkeypatch, "responses", False, [("github_repo_read", "{}")], stop_on_host=True
    )
    assert len(events) == 1
    assert "cancelled" in raw


def test_readonly_host_write_definition_is_not_published(installed, monkeypatch):
    desc = dict(
        name="github_api_write",
        description="Create PR",
        scope="repository",
        readOnly=False,
        schema_hash="b" * 64,
    )

    def reply(event, _):
        return dict(catalog_id="catalog", tools=[desc])

    requests, events, raw, _ = run_case(
        installed,
        monkeypatch,
        "responses",
        False,
        [("github_discover", "{}")],
        host_reply=reply,
        mode="readonly",
    )
    assert len(events) == 1
    assert "advertised a write capability in readonly scope" in raw
    assert "inputSchema" not in json.dumps(requests[1:])


def test_missing_policy_withdraws_tools_on_first_and_later_requests(installed, monkeypatch):
    requests, events, _, _ = run_case(
        installed, monkeypatch, "responses", False, [("github_repositories", "{}")], policy="absent"
    )
    assert len(requests) >= 2 and events == []
    for request in requests:
        assert "Do not bypass the GitHub plugin policy" in json.dumps(request)
        names = {tool.get("name") for tool in request.get("tools", [])}
        assert "github_repositories" not in names and "github_git_push" not in names
        assert "connection_status" in names


def test_explicit_all_keeps_native_route_and_description(installed, monkeypatch):
    value = {**sidecar(installed), "mode": "all", "repositories": []}

    def reply(event, _):
        return {
            "_error": dict(code="synthetic_host_refusal", message="Inert host", retryable=False)
        }

    requests, events, _, _ = run_case(
        installed,
        monkeypatch,
        "responses",
        False,
        [("github_repo_read", "{}")],
        policy=value,
        host_reply=reply,
    )
    assert len(events) == 1 and events[0]["payload"]["action"] == "github.repo.read"
    definition = next(t for t in requests[0]["tools"] if t.get("name") == "github_repositories")
    assert "allow" not in definition["description"].lower()


@pytest.mark.parametrize("protocol", ["responses", "compat"])
def test_object_arguments_cannot_bypass_string_ingress(installed, monkeypatch, protocol):
    requests, events, output, _ = run_case(
        installed, monkeypatch, protocol, False, [("github_repositories", {"page": 1})]
    )
    assert len(requests) >= 2 and events == []
    starts = [
        e for e in map(json.loads, output.splitlines()) if e.get("type") == "tool_call_started"
    ]
    assert "_invalid_github_arguments" in starts[0]["payload"]["arguments_preview"]
