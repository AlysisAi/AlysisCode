"""Hub plugins (hub.<app>): apps of the catalog Alysis's server brokers (2026-11-23).

The engine loads the signed-in account's connected apps from Alysis's server and runs their calls
with the account's own key; the desktop app never sends a hub catalog. Each tool's effect comes from
its own hints: a declared read of an app Alysis tested runs, a declared read of any other app follows
the approval mode, and everything else asks a person on every call.
"""

from __future__ import annotations

import copy
import io
import itertools
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from alysis_code import hub_apps
from alysis_code.agent_loop import AgentRuntimeError, build_tools
from alysis_code.host_actions import HostActionError
from alysis_code.ide.approval_binding import normalize_connector_approval_context
from alysis_code.ide.health import capabilities_payload
from alysis_code.mcp.connector_host import connector_tool_name, normalize_connector_catalogs
from alysis_code.mcp.plugin_hub import (
    MAX_HUB_TOOLS,
    HubCatalogError,
    bind_hub_catalog,
    check_hub_catalog,
    hub_tool_effect,
)
from alysis_code.surface import ApprovalDecision
from alysis_code.surface.noop_surface import NoopSurface
from tests.test_host_actions import _store

APP = "hub.mail"


def _tool(name: str, hints: dict[str, bool], description: str = "fixture") -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
        "hints": hints,
    }


def _catalog(*, tested: bool = False, tools=None) -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "plugin_id": APP,
        "server_id": f"{APP}/hub",
        "catalog_id": "mail",
        "name": "Αλληλογραφία",
        "tested": tested,
        "tools": tools
        or [
            _tool("MAIL_FETCH_MESSAGES", {"readOnly": True}),
            _tool("MAIL_SEND_MESSAGE", {"create": True}),
            _tool("MAIL_DELETE_MESSAGE", {"destructive": True}),
            _tool("MAIL_LIST_LABELS", {}),
        ],
    }


# --- One rule for every tool of every app --------------------------------------------------

_HINT_NAMES = ("readOnly", "create", "update", "destructive")


@pytest.mark.parametrize("tested", [True, False])
@pytest.mark.parametrize(
    "values", list(itertools.product([None, False, True], repeat=len(_HINT_NAMES)))
)
def test_only_a_tool_that_declares_it_only_reads_avoids_asking(values, tested: bool):
    hints = {
        name: value for name, value in zip(_HINT_NAMES, values, strict=True) if value is not None
    }
    reads_only = hints.get("readOnly") is True and not any(
        hints.get(name) for name in ("create", "update", "destructive")
    )
    expected = ("read" if tested else "own_change") if reads_only else "asks"
    assert hub_tool_effect(hints, tested=tested) == expected


def test_a_tool_with_no_hints_asks():
    assert hub_tool_effect(None, tested=True) == "asks"
    assert hub_tool_effect({}, tested=True) == "asks"


# --- Names the model sees ------------------------------------------------------------------


def test_the_apps_own_id_isnt_repeated_in_a_tool_name():
    assert connector_tool_name(APP, "MAIL_SEND_MESSAGE") == "mcp__hub_mail__send_message"


def test_an_action_that_doesnt_repeat_its_app_never_takes_another_actions_name():
    prefixed = connector_tool_name(APP, "MAIL_SEND_MESSAGE")
    bare = connector_tool_name(APP, "SEND_MESSAGE")
    assert prefixed != bare and bare.startswith("mcp__hub_mail__send_message_")


@pytest.mark.parametrize(
    "name", ["A" * 24 + "_" + "B" * 39, "MAIL_" + "A" * 59, "Ε" * 64, "MAIL_ΑΠΟΣΤΟΛΗ_" + "B" * 40]
)
def test_every_name_fits_64_characters_with_the_longest_app_id(name: str):
    plugin_id = "hub." + "a" * 24
    long_name = connector_tool_name(plugin_id, name)
    assert len(long_name) <= 64 and long_name.startswith("mcp__hub_" + "a" * 24 + "__")
    assert connector_tool_name(plugin_id, name + "X") != long_name


# --- Binding an app the engine loaded --------------------------------------------------------


def test_the_engine_binds_each_tool_with_its_own_effect():
    bound = bind_hub_catalog(_catalog())
    assert [(t["name"], t["effect"], t["readOnly"]) for t in bound["tools"]] == [
        ("MAIL_FETCH_MESSAGES", "own_change", False),
        ("MAIL_SEND_MESSAGE", "asks", False),
        ("MAIL_DELETE_MESSAGE", "asks", False),
        ("MAIL_LIST_LABELS", "asks", False),
    ]
    assert bound["plugin"]["source_kind"] == "hub"
    tested = bind_hub_catalog(_catalog(tested=True))
    assert [t["name"] for t in tested["tools"] if t["readOnly"]] == ["MAIL_FETCH_MESSAGES"]


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c.update(extra=True),
        lambda c: c.update(server_id="hub.mail/other"),
        lambda c: c.update(tested="yes"),
        lambda c: c.update(protocol_version=2),
        lambda c: c.update(catalog_id="Mail Box"),
        lambda c: c.update(name=" "),
        lambda c: c["tools"][0].update(hints={"readOnly": "true"}),
        lambda c: c["tools"][0].update(hints={"safe": True}),
        lambda c: c["tools"][0].pop("hints"),
        lambda c: c["tools"].append(copy.deepcopy(c["tools"][0])),
        lambda c: c.update(tools=[]),
        lambda c: c.update(tools=[_tool(f"MAIL_T{i}", {}) for i in range(MAX_HUB_TOOLS + 1)]),
        lambda c: c.update(plugin_id="hub.Mail", server_id="hub.Mail/hub"),
        lambda c: c.update(plugin_id="hub." + "a" * 25, server_id="hub." + "a" * 25 + "/hub"),
    ],
)
def test_a_malformed_app_is_refused(change):
    catalog = _catalog()
    change(catalog)
    with pytest.raises(HubCatalogError):
        check_hub_catalog(catalog)


def test_the_desktop_app_cant_send_a_hub_app():
    with pytest.raises(HostActionError) as caught:
        normalize_connector_catalogs([_catalog()])
    assert caught.value.code == "invalid_connector_mcp"


# --- The engine's client and loader --------------------------------------------------------


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _opener(routes: dict[str, Any], seen: list[Any]):
    def open_(request, timeout=None):
        seen.append(request)
        path = request.full_url.split("/apps", 1)[1]
        status, body = routes[path.split("?", 1)[0]]
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "x", {}, io.BytesIO(raw))
        return _Response(raw)

    return open_


def test_scoped_client_keeps_identity_and_revision_on_discovery_and_dispatch():
    owl = "a" * 32
    prefix = f"/owls/{owl}/connections/ca_work"
    routes = {
        f"{prefix}/tools": (
            200,
            {"schema_version": 1, "connection_id": "ca_work", "grant_revision": 2},
        ),
        f"{prefix}/call": (200, {"schema_version": 1, "sent": True, "text": "synthetic"}),
    }
    seen = []
    client = hub_apps.HubAppsClient(
        "slk_fixture", url="https://example.test/apps", owl_id=owl, opener=_opener(routes, seen)
    )
    assert client.tools("notes", connection_id="ca_work")["grant_revision"] == 2
    assert client.call("notes", "NOTES_READ", {}, connection_id="ca_work", grant_revision=2).sent
    assert seen[0].full_url.endswith(prefix + "/tools?app=notes")
    assert json.loads(seen[-1].data)["grant_revision"] == 2
    routes[f"{prefix}/tools"][1]["connection_id"] = "ca_home"
    with pytest.raises(hub_apps.HubAppsError, match="connected"):
        client.tools("notes", connection_id="ca_work")


def test_the_client_sends_the_accounts_key_and_reads_refusal_reasons():
    seen: list[Any] = []
    routes = {
        "/connections": (200, {"schema_version": 1, "connections": [], "allowance": {}}),
        "/call": (402, {"error": "allowance_used"}),
        "/catalog": (200, {"apps": []}),
    }
    client = hub_apps.HubAppsClient(
        "slk_fixture", url="https://api.example.test/apps", opener=_opener(routes, seen)
    )
    assert client.connections()["connections"] == []
    assert seen[0].get_header("Authorization") == "Bearer slk_fixture"
    with pytest.raises(hub_apps.HubAppsError) as refused:
        client.call("mail", "MAIL_SEND_MESSAGE", {"to": "x"})
    assert refused.value.reason == "allowance_used"
    with pytest.raises(hub_apps.HubAppsError) as unknown:
        client.catalog()
    assert unknown.value.reason == "unavailable", "an answer without schema_version is unreadable"


def test_connecting_asks_for_the_page_after_signing_in_in_the_apps_language_only():
    seen: list[Any] = []
    link = (200, {"schema_version": 1, "url": "https://connect.example/ln_1", "expires_at": None})
    client = hub_apps.HubAppsClient(
        "slk_fixture", url="https://api.example.test/apps", opener=_opener({"/connect": link}, seen)
    )
    for language in ("el", "zh-CN", "en-US", "", None):
        assert client.connect("gmail", language=language)["url"] == "https://connect.example/ln_1"
    bodies = [json.loads(request.data) for request in seen]
    assert bodies == [
        {"app": "gmail", "language": "el"},
        {"app": "gmail", "language": "zh-CN"},
        {"app": "gmail"},
        {"app": "gmail"},
        {"app": "gmail"},
    ]


class _FakeClient:
    _key = "slk_fake"
    owl_id = None

    def __init__(self, connections, listings):
        self._connections = connections
        self._listings = listings
        self.tool_reads: list[str] = []
        self.bindings = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def connections(self):
        return {
            "schema_version": 1,
            "connections": self._connections,
            "account_selection": 1,
            "allowance": {"calls_used": 3},
        }

    def tools(self, app, connection_id=None, timeout_s=None):
        self.tool_reads.append(app)
        self.bindings.append(connection_id)
        listing = self._listings[app]
        if isinstance(listing, Exception):
            raise listing
        return listing

    def call(self, app, tool, arguments, request_id=None, connection_id=None):
        self.calls.append((app, tool, arguments))
        self.last_connection = connection_id
        return hub_apps.HubCallResult(
            is_error=False, text=json.dumps({"ok": "Γεια"}, ensure_ascii=False), sent=True
        )


def _listing(app: str, *, tested=False) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": f"hub.{app}",
        "catalog_id": app,
        "name": app.title(),
        "tested": tested,
        "tools": [
            {
                "name": f"{app.upper()}_READ",
                "description": "Ανάγνωση",
                "input_schema": {"type": "object", "properties": {}},
                "hints": {"readOnly": True},
            },
            {"name": f"{app.upper()}_BAD", "description": 7, "input_schema": {}, "hints": {}},
        ],
    }


def test_only_connected_apps_load_and_one_bad_action_leaves_out_only_that_action():
    hub_apps._tools_cache.clear()
    client = _FakeClient(
        [
            {"app": "mail", "id": "ca_work", "label": "Work", "status": "active", "selected": True},
            {
                "app": "mail",
                "id": "ca_home",
                "label": "Personal",
                "status": "active",
                "selected": False,
            },
            {"app": "crm", "status": "needs_sign_in"},
            {"app": "desk", "id": "ca_desk", "status": "active", "selected": True},
        ],
        {"mail": _listing("mail", tested=True), "desk": hub_apps.HubAppsError("offline")},
    )
    catalogs, status = hub_apps.load_hub_catalogs(client)
    assert [(c["plugin_id"], [t["name"] for t in c["tools"]]) for c in catalogs] == [
        ("hub.mail", ["MAIL_READ"])
    ]
    assert catalogs[0]["tools"][0]["effect"] == "read"
    assert catalogs[0]["connection_id"] == "ca_work" and "Work" in catalogs[0]["name"]
    assert status == {"loaded": 1, "failed": ["desk"], "allowance": {"calls_used": 3}}
    hub_apps.load_hub_catalogs(client)
    assert client.tool_reads.count("mail") == 1, "an app's tools are kept for ten minutes"
    client._connections[0]["selected"] = False
    client._connections[1]["selected"] = True
    switched, _ = hub_apps.load_hub_catalogs(client)
    assert switched[0]["connection_id"] == "ca_home" and "Personal" in switched[0]["name"]
    assert "ca_home" in client.bindings


def test_a_signed_out_engine_loads_no_apps_and_says_why(monkeypatch, tmp_path):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    assert hub_apps.load_hub_catalogs() == ([], {"loaded": 0, "error": "signed_out"})


# --- Running a call ------------------------------------------------------------------------


class _Surface(NoopSurface):
    def __init__(self, allow: bool) -> None:
        super().__init__()
        self.allow = allow
        self.requests: list[Any] = []

    def request_approval(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return ApprovalDecision(allow=self.allow)


def _tools(root: Path, catalog: dict[str, Any], *, mode: str, surface: _Surface):
    from alysis_code.config import AppConfig

    return build_tools(
        root=root,
        console=Console(file=io.StringIO()),
        surface=surface,
        store=_store(root),
        mode=mode,
        yes=False,
        cfg=AppConfig(model="test-model"),
        connector_mcp=[bind_hub_catalog(catalog)],
    )


@pytest.fixture
def client(monkeypatch, tmp_path) -> _FakeClient:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    fake = _FakeClient([], {})
    monkeypatch.setattr(
        hub_apps.HubAppsClient, "from_saved_account", classmethod(lambda cls, **_kw: fake)
    )
    return fake


def test_a_tested_apps_read_runs_without_asking(tmp_path: Path, client):
    surface = _Surface(allow=False)
    tools = _tools(tmp_path, _catalog(tested=True), mode="review", surface=surface)
    result = tools["mcp__hub_mail__fetch_messages"].run({"query": "Αθήνα"})
    assert client.calls == [("mail", "MAIL_FETCH_MESSAGES", {"query": "Αθήνα"})]
    assert surface.requests == []
    assert "MCP_UNTRUSTED_TEXT" in result["text"] and "Γεια" in result["text"]


def test_work_executes_using_the_account_bound_during_discovery(tmp_path: Path, client):
    catalog = {**_catalog(tested=True), "connection_id": "ca_work", "name": "Mail · Work"}
    tools = _tools(tmp_path, catalog, mode="review", surface=_Surface(allow=False))
    read = tools["mcp__hub_mail__fetch_messages"]
    assert "Mail · Work" in read.description
    read.run({"query": "synthetic"})
    assert client.last_connection == "ca_work"


def test_an_untested_apps_read_follows_full_access(tmp_path: Path, client):
    surface = _Surface(allow=False)
    tools = _tools(tmp_path, _catalog(), mode="fullaccess", surface=surface)
    tools["mcp__hub_mail__fetch_messages"].run({"query": "x"})
    assert surface.requests == [] and len(client.calls) == 1


@pytest.mark.parametrize("tool", ["mcp__hub_mail__send_message", "mcp__hub_mail__list_labels"])
def test_a_change_or_an_undeclared_tool_asks_the_person_even_in_full_access(
    tmp_path: Path, client, tool: str
):
    surface = _Surface(allow=False)
    tools = _tools(tmp_path, _catalog(tested=True), mode="fullaccess", surface=surface)
    with pytest.raises(AgentRuntimeError) as denied:
        tools[tool].run({"query": "x"})
    assert denied.value.result_payload["dispatch_state"] == "not_dispatched"
    assert client.calls == [], "nothing is sent without the person's yes"
    [request] = surface.requests
    assert request.metadata["always_ask_person"] is True
    assert request.metadata["connector_context"] == {
        "connector": "Hub",
        "service": "Αλληλογραφία",
        "tool": request.metadata["mcp_tool_id"],
    }
    surface.allow = True
    tools[tool].run({"query": "x"})
    assert len(client.calls) == 1


def test_read_only_mode_offers_only_a_tested_apps_reads(tmp_path: Path, client):
    surface = _Surface(allow=True)
    tools = _tools(tmp_path, _catalog(tested=True), mode="readonly", surface=surface)
    assert [name for name in tools if name.startswith("mcp__hub_")] == [
        "mcp__hub_mail__fetch_messages"
    ]


def test_a_refusal_from_the_server_says_nothing_was_sent(tmp_path: Path, monkeypatch):
    class Refusing(_FakeClient):
        def call(self, *_args, **_kwargs):
            raise hub_apps.HubAppsError("allowance_used")

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    fake = Refusing([], {})
    monkeypatch.setattr(
        hub_apps.HubAppsClient, "from_saved_account", classmethod(lambda cls, **_kw: fake)
    )
    tools = _tools(tmp_path, _catalog(tested=True), mode="review", surface=_Surface(allow=True))
    with pytest.raises(AgentRuntimeError) as refused:
        tools["mcp__hub_mail__fetch_messages"].run({"query": "x"})
    assert refused.value.result_payload["error_code"] == "hub_allowance_used"
    assert refused.value.result_payload["dispatch_state"] == "not_dispatched"


# --- What the engine says it supports ------------------------------------------------------


def test_the_engine_advertises_hub_apps():
    assert capabilities_payload()["features"]["hub_apps"] == {
        "version": 1,
        "session_param": "hub_apps",
        "result": "hub_apps",
        "command": "apps",
        "max_apps": 16,
        "max_tools": MAX_HUB_TOOLS,
    }


def test_an_approval_can_say_it_is_a_hub_app():
    assert normalize_connector_approval_context({"connector": "Hub", "service": "Mail"}) == {
        "connector": "Hub",
        "service": "Mail",
    }


# --- A chat asks for the account's apps ----------------------------------------------------


def _create(root: Path, monkeypatch, *, mode: str, **params: Any) -> dict[str, Any]:
    from alysis_code.config import AppConfig
    from alysis_code.ide import stdio_bridge
    from alysis_code.ide.stdio_bridge import StdioBridge
    from tests.test_ide_host_actions import _request, _response_for

    out = io.StringIO()
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    captured: dict[str, Any] = {}

    class FakeSession:
        store = type("S", (), {"session_artifact_root": root / "artifacts"})()

        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def close(self) -> None:
            return

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession(**kwargs))
    bridge.process_line(
        _request(
            "session.create",
            {"workspace": str(root), "mode": mode, "model": "test-model", **params},
            "create",
        )
    )
    captured["create"] = _response_for(out, "create")
    bridge.close()
    return captured


def test_a_chat_that_asks_gets_the_accounts_apps_with_no_host_action(tmp_path: Path, monkeypatch):
    loaded = [bind_hub_catalog(_catalog(tested=True))]
    monkeypatch.setattr(hub_apps, "load_hub_catalogs", lambda: (loaded, {"loaded": 1}))
    captured = _create(tmp_path, monkeypatch, mode="auto", hub_apps=True)
    assert captured["create"]["ok"], captured["create"]
    assert captured["create"]["result"]["hub_apps"] == {"loaded": 1}
    assert [c["plugin_id"] for c in captured["connector_mcp"]] == [APP]
    read_only = _create(tmp_path, monkeypatch, mode="readonly", hub_apps=True)
    assert [t["name"] for t in read_only["connector_mcp"][0]["tools"]] == ["MAIL_FETCH_MESSAGES"]


def test_a_chat_that_doesnt_ask_loads_nothing(tmp_path: Path, monkeypatch):
    def refuse():
        raise AssertionError("not asked for")

    monkeypatch.setattr(hub_apps, "load_hub_catalogs", refuse)
    captured = _create(tmp_path, monkeypatch, mode="auto")
    assert captured["create"]["ok"] and "hub_apps" not in captured["create"]["result"]
    assert not captured.get("connector_mcp")


def test_the_apps_command_adds_an_app_that_needs_no_sign_in_without_opening_a_browser(monkeypatch):
    from typer.testing import CliRunner

    from alysis_code import host_browser
    from alysis_code.cli import app as alysis_app

    class _Client:
        def connect(self, app, *, language=None):
            assert (app, language) == ("hackernews", None)
            return {"schema_version": 1, "added": True}

    opened: list[str] = []
    monkeypatch.setattr(
        hub_apps.HubAppsClient, "from_saved_account", classmethod(lambda cls: _Client())
    )
    monkeypatch.setattr(host_browser, "open_url", opened.append)
    human = CliRunner().invoke(alysis_app, ["apps", "connect", "hackernews"])
    assert human.exit_code == 0, human.output
    assert "Added" in human.output
    assert opened == [], "nothing to sign in to"
    as_json = CliRunner().invoke(alysis_app, ["apps", "connect", "hackernews", "--json"])
    assert json.loads(as_json.output) == {"ok": True, "added": True}


def test_the_apps_command_passes_the_language_on_and_opens_the_sign_in_page(monkeypatch):
    from typer.testing import CliRunner

    from alysis_code import host_browser
    from alysis_code.cli import app as alysis_app

    asked: list[Any] = []

    class _Client:
        def connect(self, app, *, language=None):
            asked.append((app, language))
            return {"schema_version": 1, "url": "https://connect.example/ln_1", "expires_at": None}

    opened: list[str] = []
    monkeypatch.setattr(
        hub_apps.HubAppsClient, "from_saved_account", classmethod(lambda cls: _Client())
    )
    monkeypatch.setattr(host_browser, "open_url", opened.append)
    result = CliRunner().invoke(alysis_app, ["apps", "connect", "gmail", "--language=el", "--json"])
    assert json.loads(result.output) == {
        "ok": True,
        "url": "https://connect.example/ln_1",
        "expires_at": None,
    }
    human = CliRunner().invoke(alysis_app, ["apps", "connect", "gmail"])
    assert human.exit_code == 0, human.output
    assert asked == [("gmail", "el"), ("gmail", None)]
    assert opened == ["https://connect.example/ln_1"], "only the person's own run opens a browser"
