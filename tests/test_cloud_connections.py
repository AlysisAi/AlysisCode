from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from pydantic import ValidationError

from alysis_code.cloud_connections import (
    GITHUB_PLUGIN,
    BoundConnections,
    ConnectionStore,
    ConnectionUnavailable,
    GitHubBinding,
    GitHubReader,
    GrantInput,
    InstallationToken,
)


@pytest.fixture
def setup(tmp_path):
    store = ConnectionStore(tmp_path / "connections.sqlite3")
    binding = GitHubBinding(123, 456, "synthetic/project", "Synthetic account")
    identity = store.register_github("owner", binding)
    return store, binding, identity


def grant(store, identity, subject="bot:one", enabled=True, revision=0):
    store.set_grant(
        "owner", subject, identity, GrantInput(enabled=enabled, expected_revision=revision)
    )


def reader(handler=None, tokens=None):
    def response(request):
        assert request.method == "GET"
        assert request.url == "https://api.github.com/repos/synthetic/project/issues/42"
        assert request.headers["Authorization"] == "Bearer synthetic-token"
        return httpx.Response(
            200,
            json={
                "number": 42,
                "repository_url": "https://api.github.com/repos/synthetic/project",
                "title": "Έλεγχος 日本語",
                "body": "Synthetic issue; preserve Unicode.",
                "state": "open",
            },
        )

    return GitHubReader(
        tokens or (lambda _: InstallationToken("synthetic-token", time.time() + 600)),
        client=httpx.Client(transport=httpx.MockTransport(handler or response)),
    )


def test_independent_bot_and_main_grants_survive_restart(setup):
    store, binding, identity = setup
    assert store.register_github("owner", binding) == identity
    for subject in ("bot:one", "bot:two", "agent:main"):
        assert BoundConnections(store, "owner", subject, reader()).catalog() == []
    grant(store, identity)
    grant(store, identity, "agent:main")
    reopened = ConnectionStore(store.path)
    for subject in ("bot:one", "agent:main"):
        bound = BoundConnections(reopened, "owner", subject, reader())
        assert set(bound.tools()) == {"list_connected_apps", "github_read_issue"}
        result = bound.tools()["github_read_issue"].run(
            {"connection_id": identity, "issue_number": 42}
        )
        assert result["title"] == "Έλεγχος 日本語"
        assert result["content_is_untrusted"]
        assert "synthetic-token" not in json.dumps(result)
    for owner, subject in (("owner", "bot:two"), ("other", "bot:one")):
        bound = BoundConnections(reopened, owner, subject, reader())
        with pytest.raises(ConnectionUnavailable):
            bound.read_issue({"connection_id": identity, "issue_number": 42})
    assert "synthetic-token" not in store.path.read_bytes().decode("utf8", errors="ignore")
    assert "synthetic-token" not in repr(InstallationToken("synthetic-token", time.time() + 600))


def test_grant_conflict_and_plugin_revocation(setup):
    store, _, identity = setup
    grant(store, identity)
    bound = BoundConnections(store, "owner", "bot:one", reader())
    retained_tool = bound.tools()["github_read_issue"]
    with pytest.raises(ConnectionUnavailable, match="Refresh"):
        grant(store, identity, enabled=False)
    store.set_plugin_enabled("owner", GITHUB_PLUGIN, False)
    assert bound.catalog() == []
    with pytest.raises(ConnectionUnavailable):
        retained_tool.run({"connection_id": identity, "issue_number": 42})
    store.set_plugin_enabled("owner", GITHUB_PLUGIN, True)
    grant(store, identity, enabled=False, revision=1)
    with pytest.raises(ConnectionUnavailable):
        retained_tool.run({"connection_id": identity, "issue_number": 42})


@pytest.mark.parametrize("phase", ["token", "http"])
def test_inflight_revocation_withholds_result_even_after_reenable(setup, phase):
    store, _, identity = setup
    grant(store, identity)
    entered, release = threading.Event(), threading.Event()
    requests = []

    def block():
        entered.set()
        assert release.wait(5)

    def tokens(_):
        if phase == "token":
            block()
        return InstallationToken("synthetic-token", time.time() + 600)

    def transport(request):
        requests.append(request)
        block()
        return httpx.Response(
            200,
            json={
                "number": 42,
                "repository_url": "https://api.github.com/repos/synthetic/project",
                "title": "Private read",
                "body": "Must be withheld",
                "state": "open",
            },
        )

    bound = BoundConnections(store, "owner", "bot:one", reader(transport, tokens))
    with ThreadPoolExecutor() as pool:
        future = pool.submit(bound.read_issue, {"connection_id": identity, "issue_number": 42})
        assert entered.wait(5)
        grant(store, identity, enabled=False, revision=1)
        grant(store, identity, enabled=True, revision=2)
        release.set()
        with pytest.raises(ConnectionUnavailable, match="Access changed"):
            future.result(5)
    assert len(requests) == (0 if phase == "token" else 1)


@pytest.mark.parametrize(
    "payload",
    [
        {"issue_number": True},
        {"issue_number": -1},
        {"issue_number": 42, "repo": "other/private"},
        {"issue_number": 42, "action": "delete"},
    ],
)
def test_model_cannot_change_binding_or_operation(setup, payload):
    store, _, identity = setup
    grant(store, identity)
    bound = BoundConnections(
        store, "owner", "bot:one", reader(lambda _: pytest.fail("No HTTP permitted"))
    )
    with pytest.raises(ValidationError):
        bound.read_issue({"connection_id": identity, **payload})


@pytest.mark.parametrize("status", [301, 302, 401, 403, 404, 500])
def test_http_failures_never_follow_redirects_or_echo_response(setup, status):
    store, _, identity = setup
    grant(store, identity)
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://untrusted.invalid"},
            text="private-provider-diagnostics",
        )

    with pytest.raises(ConnectionUnavailable) as failure:
        BoundConnections(store, "owner", "bot:one", reader(transport)).read_issue(
            {"connection_id": identity, "issue_number": 42}
        )
    assert len(calls) == 1
    assert "private-provider-diagnostics" not in str(failure.value)


def test_unavailable_expired_and_wrong_repository_fail_closed(setup):
    store, _, identity = setup
    grant(store, identity)
    bound = BoundConnections(store, "owner", "bot:one", None)
    assert bound.catalog()[0]["availability"] == "unavailable"
    assert "github_read_issue" not in bound.tools()
    with pytest.raises(ConnectionUnavailable):
        bound.read_issue({"connection_id": identity, "issue_number": 42})
    expired = reader(
        lambda _: pytest.fail("Expired token cannot be sent"),
        lambda _: InstallationToken("synthetic-token", 0),
    )
    with pytest.raises(ConnectionUnavailable, match="refreshed"):
        BoundConnections(store, "owner", "bot:one", expired).read_issue(
            {"connection_id": identity, "issue_number": 42}
        )
    wrong = reader(
        lambda _: httpx.Response(
            200,
            json={
                "number": 42,
                "repository_url": "https://api.github.com/repos/other/project",
                "title": "Wrong",
                "body": "",
                "state": "open",
            },
        )
    )
    with pytest.raises(ConnectionUnavailable, match="metadata"):
        BoundConnections(store, "owner", "bot:one", wrong).read_issue(
            {"connection_id": identity, "issue_number": 42}
        )


def test_bots_model_dispatch_uses_shared_grant(setup, tmp_path):
    from alysis_code.bots.models import BotInput, TaskInput
    from alysis_code.bots.runtime import BotRuntime
    from alysis_code.bots.store import Store
    from alysis_code.cancellation import EventCancellationToken
    from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall

    store, _, identity = setup
    bots = Store(tmp_path / "bots.sqlite3")
    bot = bots.save_bot("owner", BotInput(name="Test", purpose="Read assigned issues"))
    chat = bots.create_chat("owner", bot["id"], "Conversation")
    grant(store, identity, "bot:" + bot["id"])
    task = bots.enqueue(
        "owner", chat["id"], TaskInput(message="Read issue 42", request_id="synthetic-read-42")
    )

    class Model:
        model = "fixture"
        supports_tool_calling = True
        calls = 0

        def chat(self, **kwargs):
            self.calls += 1
            assert "github_read_issue" in [t["function"]["name"] for t in kwargs["tools"]]
            if self.calls == 1:
                return LLMResponse(
                    "",
                    [
                        ToolCall(
                            "read",
                            "github_read_issue",
                            {"connection_id": identity, "issue_number": 42},
                        )
                    ],
                    {},
                    usage=LLMUsage(1, 1, 2),
                )
            receipts = [
                message
                for message in kwargs["messages"]
                if message.get("role") == "tool" and message.get("tool_call_id") == "read"
            ]
            assert len(receipts) == 1 and "Έλεγχος" in receipts[0]["content"]
            return LLMResponse(
                "Fixture saw the actual adapter result.", [], {}, usage=LLMUsage(1, 1, 2)
            )

    runtime = BotRuntime(
        bots,
        "owner",
        lambda _: Model(),
        connections=lambda bot_id: BoundConnections(store, "owner", "bot:" + bot_id, reader()),
    )
    runtime.run(bots.claim("owner"), EventCancellationToken(threading.Event()))
    result = bots.task("owner", task["id"])
    assert result["status"] == "completed", result["error"]
    assert any(
        e["kind"] == "tool_completed" and e["data"]["name"] == "github_read_issue"
        for e in result["events"]
    )


def test_main_session_registers_same_bound_tools(setup, tmp_path):
    from alysis_code.agent.session import create_session
    from alysis_code.config import AppConfig
    from alysis_code.mcp.manager import McpManager
    from alysis_code.mcp.models import ResolvedMcpConfig
    from alysis_code.runtime_kind import RuntimeKind

    store, _, identity = setup
    grant(store, identity, "agent:main")
    manager = McpManager(
        resolved_config=ResolvedMcpConfig(
            workspace_root=tmp_path,
            user_config_path=tmp_path / "no-user-mcp",
            project_config_path=tmp_path / "no-project-mcp",
            user_config_present=False,
            project_config_present=False,
        ),
        workspace_root=tmp_path,
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )
    session = create_session(
        cfg=AppConfig(
            model="fixture",
            skills_enabled=False,
            custom_tools_enabled=False,
            web_tools_enabled=False,
        ),
        root=tmp_path,
        mode="readonly",
        yes=True,
        max_steps=2,
        no_log=True,
        api_key_override="fixture-key",
        hooks_enabled=False,
        verification_enabled=False,
        subagents_enabled=False,
        non_interactive=True,
        mcp_manager=manager,
        session_log_dir_override=tmp_path / "sessions",
        cloud_connections=BoundConnections(store, "owner", "agent:main", reader()),
    )
    try:
        result = session.tools["github_read_issue"].run(
            {"connection_id": identity, "issue_number": 42}
        )
        assert result["title"] == "Έλεγχος 日本語"
        grant(store, identity, "agent:main", False, 1)
        with pytest.raises(ConnectionUnavailable):
            session.tools["github_read_issue"].run({"connection_id": identity, "issue_number": 42})
    finally:
        session.close()


def test_api_grants_require_owner_and_never_register_accounts(tmp_path):
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    settings = Settings(
        tmp_path / "bots.db",
        "t" * 40,
        "fixture-key",
        "https://example.invalid/v1",
        "fixture",
        max_calls=0,
    )
    app = create_app(settings)
    identity = app.state.connections.register_github(
        settings.owner, GitHubBinding(1, 2, "synthetic/project", "Synthetic")
    )
    foreign = app.state.connections.register_github(
        "other", GitHubBinding(3, 4, "other/private", "Other")
    )
    with TestClient(app, headers={"Authorization": "Bearer " + settings.token}) as client:
        bot = client.post("/v1/bots", json={"name": "Test", "purpose": "Synthetic"}).json()
        base = f"/v1/bots/{bot['id']}/apps"
        listed = client.get(base).json()
        assert len(listed) == 1 and listed[0]["availability"] == "unavailable"
        assert (
            client.patch(
                base + "/" + identity, json={"enabled": True, "expected_revision": 0}
            ).status_code
            == 200
        )
        assert (
            client.patch(
                base + "/" + identity, json={"enabled": False, "expected_revision": 0}
            ).status_code
            == 409
        )
        assert (
            client.patch(
                base + "/" + foreign, json={"enabled": True, "expected_revision": 0}
            ).status_code
            == 409
        )
        assert client.post(base, json={"token": "never-store-this"}).status_code == 405
        assert client.get("/v1/bots/unknown/apps").status_code == 404


def test_binding_rejects_path_escape_and_nonfinite_token_expiry(setup):
    for repository in ("../project", "synthetic/..", "synthetic/%2fprivate"):
        with pytest.raises(ValueError):
            GitHubBinding(1, 2, repository, "Synthetic")
    store, _, identity = setup
    grant(store, identity)
    adapter = reader(
        lambda _: pytest.fail("No HTTP allowed"),
        lambda _: InstallationToken("synthetic-token", float("nan")),
    )
    with pytest.raises(ConnectionUnavailable):
        BoundConnections(store, "owner", "bot:one", adapter).read_issue(
            {"connection_id": identity, "issue_number": 42}
        )


def test_oversize_and_transport_diagnostics_are_not_returned(setup):
    store, _, identity = setup
    grant(store, identity)

    def failure(_):
        raise RuntimeError("synthetic-sensitive-diagnostic")

    for handler in (lambda _: httpx.Response(200, content=b"a" * 256001), failure):
        with pytest.raises(ConnectionUnavailable) as raised:
            BoundConnections(store, "owner", "bot:one", reader(handler)).read_issue(
                {"connection_id": identity, "issue_number": 42}
            )
        assert "synthetic-sensitive-diagnostic" not in str(raised.value)
