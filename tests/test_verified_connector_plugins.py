"""Verified connector plugins (``alysis.notion``): a vendor's official online tool server.

Alysis reviews the tool list and its "only reads" marks in a content-pinned bundled descriptor.
The desktop app keeps the login and calls the server through the custom plugin channel
(``mcp.connector.call``). The engine binds only the reviewed tools of the installed, pinned
descriptor; every call to a tool not marked "only reads" asks a person, in every mode.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tomllib
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from alysis_code.config import AppConfig
from alysis_code.extensions.bundled import bundled_manifest
from alysis_code.extensions.install import (
    PluginInstallError,
    _check_verified_connector_source,
    _resolve_source,
    disable_plugin,
    install_plugin,
)
from alysis_code.extensions.manifest import (
    MAX_VERIFIED_TOOLS,
    PluginManifest,
    load_manifest,
    resolve_manifest_path,
)
from alysis_code.extensions.paths import installed_plugin_root
from alysis_code.extensions.registry import find_by_id, load_registry
from alysis_code.extensions.state import load_global_state
from alysis_code.host_actions import HostActionError, normalize_host_action_arguments
from alysis_code.ide import stdio_bridge
from alysis_code.ide.health import capabilities_payload
from alysis_code.ide.management_protocol import handle_management_method
from alysis_code.ide.stdio_bridge import StdioBridge
from alysis_code.mcp import connector_host, github_host
from alysis_code.mcp.config import user_mcp_config_path
from alysis_code.mcp.connector_host import (
    CONNECTOR_MCP_ACTION,
    VERIFIED_CONNECTOR_PLUGIN_IDS,
    bind_connector_catalogs,
    check_connector_call,
    connector_tool_name,
    connector_tool_prefix,
    normalize_connector_catalogs,
)

NOTION = "alysis.notion"
NOTION_READS = (
    "notion-search notion-fetch notion-get-self notion-get-users notion-get-teams "
    "notion-get-groups notion-get-comments notion-list-private-pages notion-list-shared-pages "
    "notion-list-favorite-pages notion-list-recent-pages notion-list-page-backlinks "
    "notion-query-data-sources notion-query-multiple-data-sources notion-query-meeting-notes "
    "notion-memory-search notion-get-async-task notion-get-tool-access "
    "notion-calendar-list-calendars notion-calendar-list-events "
    "notion-calendar-list-calendar-resources"
).split()
NOTION_WRITES = (
    "notion-create-pages notion-update-page notion-move-pages notion-duplicate-page "
    "notion-delete-pages notion-restore-pages notion-create-database notion-update-data-source "
    "notion-create-view notion-update-view notion-create-folder notion-update-folder "
    "notion-create-comment notion-update-comment notion-delete-comment "
    "notion-set-discussion-resolved notion-calendar-create-events notion-calendar-update-events "
    "notion-calendar-cancel-events"
).split()
NOTION_LEFT_OUT = (
    "notion-spawn-session notion-send-message-to-session notion-stop-session "
    "notion-upload-skill notion-update-enabled-skills notion-convert-page-to-skill "
    "notion-download-skill notion-get-enabled-skills notion-connector-search "
    "notion-create-file-upload notion-create-attachment notion-download-attachment "
    "notion-get-file-download-urls notion-delete-databases notion-calendar-list-coworkers-events "
    "notion-calendar-list-contacts notion-ai-search"
).split()


def _verified(
    *,
    plugin_id: str = "alysis.demo",
    connector: str = "",
    tools: tuple[tuple[str, bool], ...] = (("search", True), ("publish", False)),
    extra: str = "",
    tables: str = "",
) -> str:
    lines = connector or (
        'url = "https://mcp.example.com/mcp"\n'
        'oauth_issuer = "https://mcp.example.com"\n'
        'oauth_client = "dynamic"\n'
        'oauth_scopes = ["default"]\n'
    )
    tool_tables = "".join(
        f'\n[[components.verified_connector.tools]]\nname = "{name}"\n'
        f"reads_only = {str(read).lower()}\n"
        for name, read in tools
    )
    return (
        "schema_version = 1\n\n[plugin]\n"
        f'id = "{plugin_id}"\nname = "Demo"\nversion = "1.0.0"\n'
        'description = "A vendor server."\nauthor = "Alysis AI"\nlicense = "MIT"\n\n'
        '[compatibility]\nalysis = ">=0.14.1"\n\n'
        f'[[components.verified_connector]]\nid = "demo"\n{lines}{extra}{tool_tables}{tables}'
    )


def _parse(text: str) -> PluginManifest:
    return PluginManifest.model_validate(tomllib.loads(text))


def _refused(text: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _parse(text)


# --- The descriptor: Alysis's review, pinned in the engine --------------------------------


def test_a_verified_descriptor_parses_with_its_defaults():
    connector = _parse(
        _verified(
            connector='url = "https://mcp.example.com/v2/mcp?tools=all"\n'
            'oauth_issuer = "https://auth.example.com/tenant"\n'
            'oauth_sign_in_origins = ["https://auth.example.com"]\n'
            'oauth_client = "dynamic"\noauth_scopes = ["read:me", "offline_access"]\n',
            tables='\n[[components.verified_connector.host_arguments]]\nname = "cloudId"\n'
            'source = "atlassian_site"\n',
        )
    ).components.verified_connector[0]
    # The resource is the url without its query, unless the descriptor names one.
    assert connector.effective_resource == "https://mcp.example.com/v2/mcp"
    assert connector.max_argument_bytes == 8192
    assert [(a.name, a.source) for a in connector.host_arguments] == [("cloudId", "atlassian_site")]
    # Kept exactly as reviewed: the app compares these with its own pins.
    assert connector.oauth_issuer == "https://auth.example.com/tenant"


@pytest.mark.parametrize(
    "plugin_id", ["acme.notion", "custom.notion", "alysis.notion-x", "alysis.n" + "a" * 20]
)
def test_only_alysis_plugins_carry_a_verified_connector(plugin_id: str):
    with pytest.raises(ValidationError):
        _parse(_verified(plugin_id=plugin_id))


@pytest.mark.parametrize(
    "extra",
    [
        '\n[[components.mcp_server]]\nid = "x"\ntransport = "http"\n'
        'url = "https://x.example.com/mcp"\nhost_managed = true\nscopes = ["x"]\n',
        '\n[[components.custom_connector]]\nid = "x"\nurl = "https://x.example.com/mcp"\n'
        'auth = "none"\n[[components.custom_connector.tools]]\nname = "x"\n',
        '\n[[components.tool]]\npath = "t.py"\ndescription = "x"\nnetwork = false\n'
        'filesystem = "none"\n',
        '\n[[components.hook]]\nevent = "SessionStart"\npath = "h.sh"\n',
        '\n[[components.skill]]\npath = "skills/x"\n',
    ],
    ids=["mcp_server", "custom_connector", "tool", "hook", "skill"],
)
def test_only_plugin_skills_may_accompany_a_verified_connector(extra: str):
    with pytest.raises(ValidationError):
        _parse(_verified(tables=extra))


def test_plugin_skills_may_accompany_a_verified_connector():
    manifest = _parse(
        _verified(
            tables='\n[[components.plugin_skill]]\nname = "find-notes"\ntitle = "Find notes"\n'
            'description = "Search the workspace first."\ntext = "Search before you fetch."\n'
        )
    )
    assert [skill.name for skill in manifest.components.plugin_skill] == ["find-notes"]


def test_a_plugin_has_at_most_one_verified_connector():
    text = _verified()
    second = text[text.index("[[components.verified_connector]]") :]
    with pytest.raises(ValidationError):
        _parse(text + "\n" + second.replace('id = "demo"', 'id = "other"', 1))


_BASE = (
    'url = "https://mcp.example.com/mcp"\noauth_issuer = "https://mcp.example.com"\n'
    'oauth_client = "dynamic"\noauth_scopes = ["default"]\n'
)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("https://mcp.example.com/mcp", "http://mcp.example.com/mcp", "url must be an https"),
        (
            "https://mcp.example.com/mcp",
            "https://me:pw@mcp.example.com/mcp",
            "url must be an https",
        ),
        ("https://mcp.example.com/mcp", "https://mcp.example.com/mcp#x", "url must be an https"),
        ("https://mcp.example.com/mcp", "https://127.0.0.1/mcp", "public server name"),
        ("https://mcp.example.com/mcp", "https://localhost/mcp", "public server name"),
        ("https://mcp.example.com/mcp", "https://mcp.example.com/mcp?token=1", "key in its query"),
        ('"https://mcp.example.com"\n', '"https://mcp.example.com?x=1"\n', "oauth_issuer can't"),
        ('"https://mcp.example.com"\n', '"http://mcp.example.com"\n', "oauth_issuer must be"),
        ('"dynamic"', '"own"', "oauth_client"),
        ('["default"]', "[]", "oauth_scopes"),
        ('["default"]', '["default", "default"]', "oauth_scopes must be unique"),
        ('["default"]', '["read write"]', "oauth_scopes"),
        ('["default"]', '["it\'s"]', "oauth_scopes"),
    ],
)
def test_bad_verified_connectors_are_refused(old: str, new: str, message: str):
    _refused(_verified(connector=_BASE.replace(old, new, 1)), message)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ('resource = "https://mcp.example.com/mcp?x=1"\n', "resource can't have a query"),
        ('resource = "http://mcp.example.com/mcp"\n', "resource must be an https"),
        ('oauth_sign_in_origins = ["https://auth.example.com/path"]\n', "bare https origins"),
        ('oauth_sign_in_origins = ["https://auth.example.com/"]\n', "bare https origins"),
        (
            'oauth_sign_in_origins = ["https://auth.example.com", "https://auth.example.com"]\n',
            "must be unique",
        ),
        ("max_argument_bytes = 8191\n", "max_argument_bytes"),
        ("max_argument_bytes = 32769\n", "max_argument_bytes"),
    ],
)
def test_bad_verified_connector_fields_are_refused(extra: str, message: str):
    _refused(_verified(connector=_BASE + extra), message)


def test_the_tool_list_is_bounded_unique_and_distinct_for_the_model():
    _refused(_verified(tools=()), "tools")
    many = tuple((f"tool-{i}", True) for i in range(MAX_VERIFIED_TOOLS + 1))
    _refused(_verified(tools=many), "tools")
    assert (
        len(
            _parse(_verified(tools=many[:MAX_VERIFIED_TOOLS]))
            .components.verified_connector[0]
            .tools
        )
        == MAX_VERIFIED_TOOLS
    )
    _refused(_verified(tools=(("search", True), ("search", False))), "unique")
    _refused(_verified(tools=(("a-b", True), ("a_b", True))), "distinct")
    # Marks are required: the review states every tool's effect.
    _refused(
        _verified(tools=()) + '\n[[components.verified_connector.tools]]\nname = "x"\n',
        "reads_only",
    )


@pytest.mark.parametrize(
    ("tables", "message"),
    [
        (
            '\n[[components.verified_connector.host_arguments]]\nname = "cloudId"\nsource = "other"\n',
            "source",
        ),
        (
            "".join(
                f'\n[[components.verified_connector.host_arguments]]\nname = "a{i}"\n'
                'source = "atlassian_site"\n'
                for i in range(5)
            ),
            "host_arguments",
        ),
        (
            '\n[[components.verified_connector.host_arguments]]\nname = "cloudId"\n'
            'source = "atlassian_site"\n' * 2,
            "host_arguments names must be unique",
        ),
        (
            '\n[[components.verified_connector.host_arguments]]\nname = "cloud-id"\nsource = "atlassian_site"\n',
            "name",
        ),
    ],
)
def test_bad_host_arguments_are_refused(tables: str, message: str):
    _refused(_verified(tables=tables), message)


def test_a_verified_connector_counts_as_a_component(tmp_path: Path, recwarn):
    (tmp_path / "alysis-plugin.toml").write_text(_verified(), encoding="utf-8")
    manifest = load_manifest(tmp_path)
    assert manifest.components.verified_connector
    assert not [w for w in recwarn if "declares no components" in str(w.message)]


# --- The bundled Notion plugin: pinned, installed only from its catalog entry --------------


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        "alysis_code.extensions.install._run_git",
        lambda *_a, **_k: pytest.fail("No network for verified plugins"),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def _install(root: Path) -> list[Any]:
    requests: list[Any] = []
    install_plugin(source=NOTION, repo_root=root, trust_prompt=lambda r: requests.append(r) or True)
    return requests


def _notion_bytes() -> bytes:
    return bundled_manifest(find_by_id(load_registry(), NOTION))


# --- The descriptor: Alysis's review, pinned in the engine --------------------------------


def test_the_bundled_notion_descriptor_carries_the_reviewed_tools_and_marks():
    manifest = _parse(_notion_bytes().decode("utf-8"))
    assert manifest.plugin.id == NOTION
    [connector] = manifest.components.verified_connector
    assert connector.id == "notion"
    assert connector.url == "https://mcp.notion.com/mcp"
    assert connector.effective_resource == "https://mcp.notion.com/mcp"
    assert connector.oauth_issuer == "https://mcp.notion.com"
    assert connector.oauth_sign_in_origins == []
    assert connector.oauth_client == "dynamic"
    assert connector.oauth_scopes == ["default"]
    assert connector.max_argument_bytes == 32768
    assert connector.host_arguments == []
    marks = {tool.name: tool.reads_only for tool in connector.tools}
    assert sorted(name for name, read in marks.items() if read) == sorted(NOTION_READS)
    assert sorted(name for name, read in marks.items() if not read) == sorted(NOTION_WRITES)
    assert not set(NOTION_LEFT_OUT) & set(marks)
    # Ships no skills now, and nothing that runs on the computer.
    components = manifest.components
    assert not (components.plugin_skill or components.mcp_server or components.tool)


def test_the_catalog_entry_pins_the_notion_descriptor():
    entry = find_by_id(load_registry(), NOTION)
    assert entry is not None and entry.source_kind == "bundled"
    assert entry.bundled_path == "notion"
    assert entry.tier == "alysis" and entry.publisher == "Alysis"
    assert entry.categories == ["notes-wiki"]
    digest = hashlib.sha256(_notion_bytes()).hexdigest()
    assert entry.package_sha256 == digest and entry.commit == f"sha256:{digest}"


def test_a_descriptor_that_fails_its_pin_is_never_read():
    entry = find_by_id(load_registry(), NOTION).model_copy(update={"package_sha256": "0" * 64})
    with pytest.raises(RuntimeError, match="content pin"):
        bundled_manifest(entry)


def test_notion_installs_from_its_catalog_entry_for_the_user(root: Path):
    [request] = _install(root)
    assert request.plugin_id == NOTION and request.source_kind == "bundled"
    # The trust review counts the online server as network access.
    assert request.permissions_summary.network is True
    assert request.permissions_summary.filesystem_write is False
    info = handle_management_method(
        "ext.info", {"workspace": str(root), "plugin_id": NOTION}, request_id=1
    )
    assert info["enabled_effective"] is True
    assert info["installed"]["source_kind"] == "bundled"
    assert info["installed"]["manifest_sha256"] == hashlib.sha256(_notion_bytes()).hexdigest()
    # Never written to the user's own tool server list.
    assert not user_mcp_config_path().exists()


def test_a_verified_connector_isnt_installed_for_a_project(root: Path):
    with pytest.raises(PluginInstallError, match="for the user"):
        install_plugin(source=NOTION, repo_root=root, project=True, trust_prompt=lambda _r: True)


def test_a_verified_connector_comes_only_from_its_pinned_catalog_entry(root: Path, monkeypatch):
    manifest = _parse(_notion_bytes().decode("utf-8"))
    source = _resolve_source(NOTION)
    _check_verified_connector_source(manifest, source, project=False)
    # Content that isn't the pinned bundled copy, or any other source kind, is refused.
    forged = [
        source.__class__(**{**source.__dict__, "bundled_content": _notion_bytes() + b"\n"}),
        source.__class__(**{**source.__dict__, "registry_id": None}),
        source.__class__(**{**source.__dict__, "git_url": "https://example.com/notion.git"}),
        source.__class__(**{**source.__dict__, "custom_id": "custom.notion"}),
        source.__class__(**{**source.__dict__, "package_sha256": "0" * 64}),
    ]
    for forged_source in forged:
        with pytest.raises(PluginInstallError, match="built-in catalog entry"):
            _check_verified_connector_source(manifest, forged_source, project=False)


def test_a_git_source_cant_carry_a_verified_connector(root: Path, monkeypatch):
    def clone(*, git_url: str, commit: str, destination: Path) -> None:
        (destination / "alysis-plugin.toml").write_bytes(_notion_bytes())

    monkeypatch.setattr("alysis_code.extensions.install._clone_pinned_git_repo", clone)
    with pytest.raises(PluginInstallError):
        install_plugin(
            source="git+https://example.com/notion.git@" + "a" * 40,
            repo_root=root,
            trust_prompt=lambda _r: True,
        )
    assert NOTION not in load_global_state().installed


def test_a_custom_save_cant_carry_a_verified_connector(root: Path):
    from alysis_code.ide.protocol import ProtocolError

    text = _verified(plugin_id="custom.notion")
    with pytest.raises(ProtocolError):
        handle_management_method(
            "ext.custom.save",
            {
                "workspace": str(root),
                "workspace_trusted": True,
                "plugin_id": "custom.notion",
                "descriptor": text,
            },
            request_id=1,
        )


def _expected_block() -> dict[str, Any]:
    connector = _parse(_notion_bytes().decode("utf-8")).components.verified_connector[0]
    return {
        "id": "notion",
        "url": "https://mcp.notion.com/mcp",
        "resource": "https://mcp.notion.com/mcp",
        "oauth_issuer": "https://mcp.notion.com",
        "oauth_sign_in_origins": [],
        "oauth_scopes": ["default"],
        "max_argument_bytes": 32768,
        "host_arguments": [],
        "tools": [{"name": t.name, "reads_only": t.reads_only} for t in connector.tools],
    }


def test_the_plugin_page_carries_the_verified_connector_from_the_pinned_descriptor(root: Path):
    info = handle_management_method(
        "ext.info", {"workspace": str(root), "plugin_id": NOTION}, request_id=1
    )
    extension = info["extension"]
    assert extension["verified_connector"] == _expected_block()
    assert extension["connects_to"] == ["https://mcp.notion.com/mcp"]
    assert extension["categories"] == ["notes-wiki"]
    assert extension["tier"] == "alysis" and extension["catalog_source"] == "built-in"
    assert extension["skills"] == []
    found = handle_management_method("ext.search", {"query": "notion"}, request_id=2)
    [entry] = found["extensions"]
    assert entry["verified_connector"] == _expected_block()
    # Plugins without the component carry no block.
    github = handle_management_method("ext.search", {"query": "alysis.github"}, request_id=3)
    assert all("verified_connector" not in e for e in github["extensions"])


def test_a_notion_descriptor_that_fails_its_pin_shows_no_connector(root: Path, monkeypatch):
    registry = load_registry()
    find_by_id(registry, NOTION).package_sha256 = "0" * 64
    monkeypatch.setattr("alysis_code.ide.management_protocol.load_registry", lambda: registry)
    info = handle_management_method(
        "ext.info", {"workspace": str(root), "plugin_id": NOTION}, request_id=1
    )
    assert info["extension"]["connects_to"] == []
    assert "verified_connector" not in info["extension"]


# --- A chat: only the reviewed tools of the pinned install; writes always ask a person ------


def _pin() -> str:
    return hashlib.sha256(_notion_bytes()).hexdigest()


def _catalog(
    tools: tuple[str, ...] = ("notion-search", "notion-create-pages"),
    *,
    plugin_id: str = NOTION,
    server_id: str = "alysis.notion/notion",
    sha: str | None = None,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "plugin_id": plugin_id,
        "server_id": server_id,
        "descriptor_sha256": sha or _pin(),
        "tools": [
            {
                "name": name,
                "description": f"{name} fixture",
                "inputSchema": schema
                or {"type": "object", "properties": {"query": {"type": "string"}}},
            }
            for name in tools
        ],
    }


def _bridge(
    root: Path,
    monkeypatch,
    catalog: dict[str, Any],
    *,
    mode: str = "auto",
    tool: str = "notion-create-pages",
    arguments: dict[str, Any] | None = None,
    drop: bool = True,
):
    from tests.test_ide_host_actions import _request, _response_for

    out = io.StringIO()
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    captured: dict[str, Any] = {}
    results: list[Any] = []
    errors: list[HostActionError] = []

    class FakeStore:
        session_artifact_root = root / "artifacts"

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            self.surface = kwargs["surface"]

        def run_turn(self, _message: str) -> int:
            try:
                results.append(
                    captured["host_action_handler"](
                        CONNECTOR_MCP_ACTION,
                        {
                            "plugin_id": catalog["plugin_id"],
                            "name": tool,
                            "arguments": arguments if arguments is not None else {"query": "x"},
                        },
                    )
                )
            except HostActionError as exc:
                errors.append(exc)
            self.surface.emit_message_end("done")
            return 0

        def close(self) -> None:
            return

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession(**kwargs))
    bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": os.fspath(root),
                "mode": mode,
                "model": "test-model",
                "workspace_trusted": True,
                "host_capabilities": {
                    "protocol_version": "1",
                    "approval_protocol": 1,
                    "actions": [CONNECTOR_MCP_ACTION],
                },
                "connector_mcp": [catalog],
                **({"drop_inactive_host_plugins": True} if drop else {}),
            },
            "create",
        )
    )
    captured["create"] = _response_for(out, "create")
    return out, bridge, captured, results, errors


def _create(root: Path, monkeypatch, catalog: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    _out, bridge, captured, _results, _errors = _bridge(root, monkeypatch, catalog, **kwargs)
    bridge.close()
    return captured


def _dropped(captured: dict[str, Any]) -> list[str]:
    return [d["plugin_id"] for d in captured["create"]["result"]["host_plugins_dropped"]]


def test_the_engine_advertises_the_verified_plugins_it_can_bind():
    feature = capabilities_payload()["features"]["custom_connector_mcp"]
    assert feature == {
        "version": 1,
        "host_action": CONNECTOR_MCP_ACTION,
        "session_param": "connector_mcp",
        "credential_authority": "host",
        "management_method": "ext.custom.save",
        "max_plugins": 16,
        "verified_plugins": list(VERIFIED_CONNECTOR_PLUGIN_IDS),
    }
    assert VERIFIED_CONNECTOR_PLUGIN_IDS[0] == "alysis.notion"
    registry = load_registry()
    for plugin_id in VERIFIED_CONNECTOR_PLUGIN_IDS:
        entry = find_by_id(registry, plugin_id)
        manifest = _parse(bundled_manifest(entry).decode("utf-8"))
        assert entry.source_kind == "bundled" and len(manifest.components.verified_connector) == 1


def test_the_longest_verified_name_fits(monkeypatch):
    longest = "alysis." + "a" * 20
    monkeypatch.setattr(connector_host, "VERIFIED_CONNECTOR_PLUGIN_IDS", (longest,))
    assert len(connector_tool_name(longest, "x" * 64)) == 64


def test_model_facing_names():
    assert connector_tool_name(NOTION, "notion-search") == "mcp__notion__notion_search"
    assert (
        connector_tool_name(NOTION, "notion-calendar-list-calendar-resources")
        == "mcp__notion__notion_calendar_list_calendar_resources"
    )
    names = [connector_tool_name(NOTION, name) for name in NOTION_READS + NOTION_WRITES]
    assert len(set(names)) == len(names) and max(map(len, names)) <= 64
    # Custom plugins keep their names.
    assert connector_tool_name("custom.tools", "search") == "mcp__custom_tools__search"
    assert connector_tool_prefix(NOTION) == "mcp__notion__"
    assert connector_tool_prefix("custom.notion") == "mcp__custom_notion__"


def test_a_chat_binds_the_reviewed_tools_with_alysiss_marks(root: Path, monkeypatch):
    _install(root)
    captured = _create(root, monkeypatch, _catalog())
    created = captured["create"]
    assert created["ok"], created
    [payload] = created["result"]["connector_mcp"]
    assert payload == {
        "protocol_version": 1,
        "plugin_id": NOTION,
        "server_id": "alysis.notion/notion",
        "manifest_sha256": _pin(),
        "package_sha256": _pin(),
        "source_kind": "bundled",
        "scope": "user",
        "tool_names": ["notion-search", "notion-create-pages"],
    }
    assert created["result"]["host_plugins_dropped"] == []
    [bound] = captured["connector_mcp"]
    assert [(t["name"], t["readOnly"]) for t in bound["tools"]] == [
        ("notion-search", True),
        ("notion-create-pages", False),
    ]
    assert bound["max_argument_bytes"] == 32768 and bound["host_arguments"] == []


def test_a_catalog_naming_a_tool_alysis_didnt_review_fails(root: Path, monkeypatch):
    _install(root)
    captured = _create(root, monkeypatch, _catalog(("notion-search", "notion-spawn-session")))
    assert captured["create"]["ok"] is False
    assert captured["create"]["error"]["code"] == "invalid_connector_mcp"


@pytest.mark.parametrize("plugin_id", ["alysis.unknown", "alysis.github", "alysis.google_drive"])
def test_a_plugin_this_engine_cant_bind_fails(root: Path, monkeypatch, plugin_id: str):
    _install(root)
    catalog = _catalog(plugin_id=plugin_id, server_id=f"{plugin_id}/x")
    captured = _create(root, monkeypatch, catalog)
    assert captured["create"]["ok"] is False
    assert captured["create"]["error"]["code"] == "invalid_connector_mcp"


def test_an_off_removed_changed_or_unpinned_plugin_is_dropped(root: Path, monkeypatch):
    # Not installed.
    assert _dropped(_create(root, monkeypatch, _catalog())) == [NOTION]
    _install(root)
    # The app holds a different descriptor than the installed one.
    assert _dropped(_create(root, monkeypatch, _catalog(sha="0" * 64))) == [NOTION]
    # The installed bytes changed on disk.
    path = resolve_manifest_path(installed_plugin_root(NOTION, project=False, repo_root=root))
    original = path.read_bytes()
    path.write_bytes(original + b"\n# changed\n")
    assert _dropped(_create(root, monkeypatch, _catalog())) == [NOTION]
    path.write_bytes(original)
    assert _dropped(_create(root, monkeypatch, _catalog())) == []
    # Turned off.
    disable_plugin(plugin_id=NOTION, repo_root=root)
    captured = _create(root, monkeypatch, _catalog())
    assert _dropped(captured) == [NOTION]
    assert "connector_mcp" not in captured["create"]["result"]


def test_without_the_opt_in_an_off_plugin_fails_the_session(root: Path, monkeypatch):
    _install(root)
    disable_plugin(plugin_id=NOTION, repo_root=root)
    captured = _create(root, monkeypatch, _catalog(), drop=False)
    assert captured["create"]["ok"] is False


def _ship_new_notion_descriptor(monkeypatch, *, old: bytes, new: bytes) -> None:
    """Simulate a newer Alysis that bundles an edited Notion descriptor."""
    content = _notion_bytes().replace(old, new, 1)
    assert content != _notion_bytes()
    digest = hashlib.sha256(content).hexdigest()
    registry = load_registry()
    entry = find_by_id(registry, NOTION)
    entry.package_sha256 = digest
    entry.commit = f"sha256:{digest}"
    for module in (github_host, connector_host):
        monkeypatch.setattr(module, "load_registry", lambda: registry)
        monkeypatch.setattr(module, "bundled_manifest", lambda _entry: content)


def test_an_older_install_with_the_same_access_still_binds(root: Path, monkeypatch):
    _install(root)
    _ship_new_notion_descriptor(monkeypatch, old=b'version = "1.0.0"', new=b'version = "1.0.1"')
    captured = _create(root, monkeypatch, _catalog())
    assert captured["create"]["ok"], captured["create"]
    assert _dropped(captured) == []
    [payload] = captured["create"]["result"]["connector_mcp"]
    assert payload["manifest_sha256"] == _pin()


def test_an_older_install_with_different_access_is_dropped_until_reviewed(root: Path, monkeypatch):
    _install(root)
    _ship_new_notion_descriptor(
        monkeypatch,
        old=b'name = "notion-create-pages"\nreads_only = false',
        new=b'name = "notion-create-pages"\nreads_only = true',
    )
    assert _dropped(_create(root, monkeypatch, _catalog())) == [NOTION]


def test_read_only_mode_keeps_only_the_reviewed_reads(root: Path, monkeypatch):
    _install(root)
    captured = _create(root, monkeypatch, _catalog(), mode="readonly")
    [bound] = captured["connector_mcp"]
    assert [t["name"] for t in bound["tools"]] == ["notion-search"]


def test_a_catalog_may_carry_up_to_96_tools_for_a_verified_plugin():
    def tools(count: int) -> list[dict[str, Any]]:
        return [
            {"name": f"t{i}", "description": "", "inputSchema": {"type": "object"}}
            for i in range(count)
        ]

    normalize_connector_catalogs([{**_catalog(), "tools": tools(96)}])
    with pytest.raises(HostActionError):
        normalize_connector_catalogs([{**_catalog(), "tools": tools(97)}])
    custom = {**_catalog(plugin_id="custom.tools", server_id="custom.tools/server")}
    normalize_connector_catalogs([{**custom, "tools": tools(64)}])
    with pytest.raises(HostActionError):
        normalize_connector_catalogs([{**custom, "tools": tools(65)}])


# A verified plugin with an argument the app fills in (as Jira + Confluence will have).
_SITE = _verified(
    plugin_id="alysis.sites",
    tools=(("search", True), ("publish", False)),
    tables='\n[[components.verified_connector.host_arguments]]\nname = "cloudId"\n'
    'source = "atlassian_site"\n',
)


@pytest.fixture
def site_plugin(root: Path, monkeypatch) -> dict[str, Any]:
    manifest = _parse(_SITE)
    plugin = {
        "plugin_id": "alysis.sites",
        "server_id": "alysis.sites/demo",
        "manifest_sha256": hashlib.sha256(_SITE.encode()).hexdigest(),
        "package_sha256": hashlib.sha256(_SITE.encode()).hexdigest(),
        "source_kind": "bundled",
        "scope": "user",
    }
    monkeypatch.setattr(
        connector_host,
        "VERIFIED_CONNECTOR_PLUGIN_IDS",
        (*VERIFIED_CONNECTOR_PLUGIN_IDS, "alysis.sites"),
    )
    monkeypatch.setattr(
        connector_host,
        "connector_plugin",
        lambda _root, plugin_id: (plugin, manifest) if plugin_id == "alysis.sites" else None,
    )
    return plugin


def _site_catalog(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required is not None:
        schema["required"] = required
    return _catalog(
        ("search", "publish"),
        plugin_id="alysis.sites",
        server_id="alysis.sites/demo",
        sha=hashlib.sha256(_SITE.encode()).hexdigest(),
        schema=schema,
    )


def test_a_catalog_still_exposing_a_host_argument_fails(root: Path, site_plugin):
    with pytest.raises(HostActionError, match="fills in"):
        bind_connector_catalogs(
            root,
            normalize_connector_catalogs(
                [_site_catalog({"cloudId": {"type": "string"}, "jql": {"type": "string"}})]
            ),
        )
    [bound] = bind_connector_catalogs(
        root, normalize_connector_catalogs([_site_catalog({"jql": {"type": "string"}})])
    )
    assert bound["host_arguments"] == ["cloudId"] and bound["max_argument_bytes"] == 8192


def test_a_call_setting_a_host_argument_is_refused_before_it_is_sent(root: Path, site_plugin):
    from alysis_code.agent.errors import AgentRuntimeError

    [bound] = bind_connector_catalogs(
        root, normalize_connector_catalogs([_site_catalog({"jql": {"type": "string"}})])
    )
    call = {"plugin_id": "alysis.sites", "name": "search", "arguments": {"cloudId": "other"}}
    with pytest.raises(HostActionError) as refused:
        check_connector_call(bound, normalize_host_action_arguments(CONNECTOR_MCP_ACTION, call))
    assert refused.value.code == "invalid_host_action_arguments"
    calls: list[Any] = []
    tools = _production_tools(root, [bound], lambda action, args: calls.append(args) or _RESULT)
    with pytest.raises(AgentRuntimeError):
        tools[connector_tool_name("alysis.sites", "search")].run({"cloudId": "other"})
    assert calls == []
    tools[connector_tool_name("alysis.sites", "search")].run({"jql": "project = X"})
    assert calls == [
        {"plugin_id": "alysis.sites", "name": "search", "arguments": {"jql": "project = X"}}
    ]


def test_each_plugin_keeps_its_own_argument_limit(root: Path, site_plugin):
    def call(plugin_id: str, size: int) -> dict[str, Any]:
        return {"plugin_id": plugin_id, "name": "search", "arguments": {"q": "x" * size}}

    # A long Notion page fits Notion's own limit.
    normalize_host_action_arguments(CONNECTOR_MCP_ACTION, call(NOTION, 20_000))
    with pytest.raises(HostActionError):
        normalize_host_action_arguments(CONNECTOR_MCP_ACTION, call(NOTION, 33_000))
    # Custom plugins keep 8 KiB.
    with pytest.raises(HostActionError):
        normalize_host_action_arguments(CONNECTOR_MCP_ACTION, call("custom.tools", 9_000))
    normalize_host_action_arguments(CONNECTOR_MCP_ACTION, call("custom.tools", 7_000))
    # A verified plugin without a raised limit keeps the default.
    [bound] = bind_connector_catalogs(
        root, normalize_connector_catalogs([_site_catalog({"jql": {"type": "string"}})])
    )
    with pytest.raises(HostActionError) as refused:
        check_connector_call(
            bound,
            normalize_host_action_arguments(CONNECTOR_MCP_ACTION, call("alysis.sites", 9_000)),
        )
    assert refused.value.code == "host_action_arguments_too_large"


def _start(out, bridge, captured) -> None:
    from tests.test_ide_host_actions import _request

    session_id = captured["create"]["result"]["session_id"]
    bridge.process_line(_request("chat.send", {"session_id": session_id, "message": "run"}, "chat"))


def _prepared(bridge, out, *, write: bool, letter: str, tool: str, label: str = "Verified") -> None:
    from tests.test_ide_host_actions import _respond, _wait_for

    prepared = _wait_for(out, lambda line: line.get("type") == "host_action_requested")
    assert prepared["payload"]["phase"] == "prepare"
    _respond(
        bridge,
        prepared,
        request_id="prepared",
        result={
            "preparation_id": "pa_" + letter * 32,
            "required": write,
            "write_effect": write,
            "kind": "mcp",
            "reason": "Fixture",
            "preview": f"{tool} fixture",
            "target": NOTION,
            **(
                {"context": {"connector": label, "service": "Notion", "tool": tool}}
                if write
                else {}
            ),
        },
    )


@pytest.mark.parametrize("mode", ["review", "auto", "fullaccess"])
def test_every_notion_write_asks_a_person_even_in_full_access(root: Path, monkeypatch, mode: str):
    from tests.test_custom_plugins import _next, _phase
    from tests.test_ide_host_actions import _request, _respond, _wait_for

    _install(root)
    out, bridge, captured, results, errors = _bridge(root, monkeypatch, _catalog(), mode=mode)
    try:
        _start(out, bridge, captured)
        _prepared(bridge, out, write=True, letter="c", tool="notion-create-pages")
        prompt = _next(out)
        assert prompt["type"] == "prompt_for_input", prompt
        metadata = prompt["payload"]["metadata"]
        assert metadata["always_ask_person"] is True
        assert metadata["mandatory_explicit_approval"] is True
        assert metadata["host_action"] == CONNECTOR_MCP_ACTION and metadata["access"] == "write"
        assert prompt["payload"]["allow_for_session_supported"] is False
        # The MCP approval contract (#84) names the verified server and tool and shows the
        # exact arguments; the label says Verified, never Custom.
        assert metadata["tool_server_rule"] == "ask_every_call"
        assert metadata["connector_context"]["connector"] == "Verified"
        assert prompt["payload"]["action"]["tool_identity"] == {
            "mcp_server_id": "alysis.notion/notion",
            "mcp_tool_id": "notion-create-pages",
        }
        preview = prompt["payload"]["preview"]
        assert preview.startswith("Run MCP tool\nserver: alysis.notion/notion\n")
        assert json.loads(preview.split("\nargs:\n", 1)[1]) == {"query": "x"}
        bridge.process_line(
            _request(
                "approval.respond",
                {
                    "session_id": captured["create"]["result"]["session_id"],
                    "approval_id": prompt["payload"]["approval_id"],
                    "allow": False,
                    "allow_for_session": False,
                },
                "deny",
            )
        )
        discard = _phase(out)
        assert discard["payload"]["phase"] == "discard"
        _respond(bridge, discard, request_id="discarded", result={"discarded": True})
        _wait_for(out, lambda line: line.get("type") == "message_end")
        assert not results and errors and errors[0].dispatch_state == "not_dispatched"
    finally:
        bridge.close()


def test_a_reviewed_read_runs_without_asking(root: Path, monkeypatch):
    from tests.test_custom_plugins import _next

    _install(root)
    # A long page body fits Notion's own argument limit.
    out, bridge, captured, _results, _errors = _bridge(
        root,
        monkeypatch,
        _catalog(),
        mode="fullaccess",
        tool="notion-search",
        arguments={"query": "x" * 20_000},
    )
    try:
        _start(out, bridge, captured)
        _prepared(bridge, out, write=False, letter="e", tool="notion-search")
        execute = _next(out)
        assert execute["payload"]["phase"] == "execute", execute
    finally:
        bridge.close()


def test_the_app_cant_turn_a_notion_write_into_a_read(root: Path, monkeypatch):
    from tests.test_custom_plugins import _next
    from tests.test_ide_host_actions import _respond, _wait_for

    _install(root)
    out, bridge, captured, results, errors = _bridge(
        root, monkeypatch, _catalog(), mode="fullaccess"
    )
    try:
        _start(out, bridge, captured)
        _prepared(bridge, out, write=False, letter="d", tool="notion-create-pages")
        discard = _next(out)
        assert discard["payload"]["phase"] == "discard"
        _respond(bridge, discard, request_id="discarded", result={"discarded": True})
        _wait_for(out, lambda line: line.get("type") == "message_end")
        assert not results and errors[0].code == "invalid_host_preparation"
    finally:
        bridge.close()


def test_read_only_mode_never_calls_a_notion_write(root: Path, monkeypatch):
    from tests.test_ide_host_actions import _wait_for

    _install(root)
    out, bridge, captured, results, errors = _bridge(root, monkeypatch, _catalog(), mode="readonly")
    try:
        _start(out, bridge, captured)
        _wait_for(out, lambda line: line.get("type") == "message_end")
        assert not results and errors and errors[0].code == "custom_tool_unavailable"
    finally:
        bridge.close()


def _production_tools(
    root: Path, bound: list[dict[str, Any]], handler: Any, *, mcp_manager: Any = None
) -> dict[str, Any]:
    from types import SimpleNamespace

    from alysis_code.agent.tools_assembly import build_tools
    from tests.test_host_actions import _store

    return build_tools(
        root=root,
        console=None,
        store=_store(root),
        mode="auto",
        yes=True,
        cfg=AppConfig(model="test-model"),
        host_action_capabilities=(CONNECTOR_MCP_ACTION,),
        host_action_handler=handler,
        connector_mcp=bound,
        mcp_manager=mcp_manager if mcp_manager is not None else SimpleNamespace(tool_bindings=[]),
    )


_RESULT = {"content": [{"type": "text", "text": "fixture result"}]}


def test_a_reviewed_read_runs_through_the_production_tool(root: Path):
    from types import SimpleNamespace

    _install(root)
    bound = bind_connector_catalogs(root, normalize_connector_catalogs([_catalog()]))
    calls: list[Any] = []
    # A user-configured server named like the plugin can't take its tools.
    impostor = SimpleNamespace(tool_alias="mcp__notion__notion_search")
    tools = _production_tools(
        root,
        bound,
        lambda action, args: calls.append((action, args)) or _RESULT,
        mcp_manager=SimpleNamespace(tool_bindings=[impostor]),
    )
    assert {"mcp__notion__notion_search", "mcp__notion__notion_create_pages"} <= set(tools)
    result = tools["mcp__notion__notion_search"].run({"query": "plans"})
    assert calls == [
        (
            CONNECTOR_MCP_ACTION,
            {"plugin_id": NOTION, "name": "notion-search", "arguments": {"query": "plans"}},
        )
    ]
    assert "MCP_UNTRUSTED_TEXT" in result["text"] and "fixture result" in result["text"]


def test_notion_turned_off_during_a_call_loses_its_result(root: Path):
    from alysis_code.agent.errors import AgentRuntimeError

    _install(root)
    bound = bind_connector_catalogs(root, normalize_connector_catalogs([_catalog()]))

    def handler(_action: str, _args: dict[str, Any]) -> dict[str, Any]:
        disable_plugin(plugin_id=NOTION, repo_root=root)
        return _RESULT

    tools = _production_tools(root, bound, handler)
    with pytest.raises(AgentRuntimeError):
        tools["mcp__notion__notion_search"].run({"query": "plans"})


def test_a_custom_plugin_cant_be_labelled_verified_in_its_approval(root: Path, monkeypatch):
    from tests import test_custom_plugins as custom
    from tests.test_ide_host_actions import _respond, _wait_for

    text = custom._descriptor()
    custom._save(root, text)
    out, bridge, captured, results, errors = custom._bridge(
        root, monkeypatch, custom._catalog(text), mode="fullaccess"
    )
    try:
        custom._start(out, bridge, captured)
        prepared = _wait_for(out, lambda line: line.get("type") == "host_action_requested")
        _respond(
            bridge,
            prepared,
            request_id="prepared",
            result={
                "preparation_id": "pa_" + "f" * 32,
                "required": True,
                "write_effect": True,
                "kind": "mcp",
                "reason": "Fixture",
                "preview": "publish fixture",
                "target": "custom.tools",
                "context": {"connector": "Verified", "service": "My tools", "tool": "publish"},
            },
        )
        _wait_for(out, lambda line: line.get("type") == "message_end")
        assert not results and errors[0].code == "invalid_host_preparation"
    finally:
        bridge.close()


# --- Plugin skills: a verified plugin's guidance is labelled like other built-in plugins -----

_SKILLED = _verified(
    plugin_id=NOTION,
    tables='\n[[components.plugin_skill]]\nname = "find-notes"\ntitle = "Find notes"\n'
    'description = "Search the workspace first."\ntext = "Search before you fetch."\n',
)


def test_skill_projection_accepts_the_verified_plugins_it_can_bind(root: Path):
    from alysis_code.extensions import plugin_skills
    from alysis_code.ide import plugin_skill_projection as projection

    assert projection.plugin_id(NOTION)
    assert not projection.plugin_id("alysis.unknown")
    assert projection.binding(NOTION) == (NOTION, plugin_skills._VERIFIED, "")
    _install(root)
    manifest, ref = projection.verified(root, NOTION, _pin())
    assert manifest.plugin.id == NOTION and ref[1] == plugin_skills._VERIFIED
    with pytest.raises(projection.PluginSkillUnavailable) as refused:
        projection.verified(root, NOTION, "0" * 64)
    assert refused.value.reason == "pin_mismatch"
    # Notion ships no skills now.
    assert plugin_skills.active_plugin_skills(root, connector_mcp=[{"plugin_id": NOTION}]) == ()


def test_a_verified_plugins_skills_come_from_its_verified_descriptor(root: Path, monkeypatch):
    from alysis_code.extensions import plugin_skills
    from alysis_code.ide import plugin_skill_projection as projection

    _install(root)
    manifest = _parse(_SKILLED)
    plugin = {"plugin_id": NOTION, "manifest_sha256": _pin()}
    monkeypatch.setattr(
        plugin_skills, "verified_connector_plugin", lambda _r, _p: (plugin, manifest)
    )
    monkeypatch.setattr(projection, "verified_connector_plugin", lambda _r, _p: (plugin, manifest))
    [skill] = plugin_skills.active_plugin_skills(root, connector_mcp=[{"plugin_id": NOTION}])
    assert skill.name == "notion:find-notes"
    assert (skill.publisher, skill.tier) == ("Alysis", "alysis")
    assert plugin_skills.plugin_skill_still_active(root, skill)
    rows, dropped, _guidance = projection.build_rows(
        root,
        {
            "epoch": "00000000-0000-4000-8000-000000000001",
            "bound_plugins": [
                {"plugin_id": NOTION, "descriptor_sha256": _pin(), "lifecycle_revision": 1}
            ],
        },
    )
    assert dropped == []
    [row] = rows
    assert row["qualified_name"] == "alysis.notion:find-notes"
    assert (row["source_kind"], row["publisher"], row["publisher_tier"]) == (
        "bundled",
        "Alysis",
        "alysis",
    )
    # Turned off: no skill.
    monkeypatch.setattr(plugin_skills, "verified_connector_plugin", lambda _r, _p: None)
    assert not plugin_skills.plugin_skill_still_active(root, skill)


def test_a_reviewed_package_import_cant_carry_a_verified_connector(root: Path, tmp_path: Path):
    # The customization workbench (#84) installs reviewed native packages as snapshots.
    package = tmp_path / "package"
    package.mkdir()
    (package / "alysis-plugin.toml").write_bytes(_notion_bytes())
    with pytest.raises(PluginInstallError):
        install_plugin(
            source="https://example.com/notion.git",
            repo_root=root,
            trust_prompt=lambda _r: True,
            reviewed_snapshot=package,
            snapshot_digest="0" * 64,
        )
    assert NOTION not in load_global_state().installed


def test_long_verified_names_keep_distinct_shortened_aliases(monkeypatch):
    longest = "alysis." + "a" * 20
    monkeypatch.setattr(connector_host, "VERIFIED_CONNECTOR_PLUGIN_IDS", (longest,))
    names = [f"a-shared-tool-prefix-that-needs-shortening-{index}" for index in range(96)]
    aliases = [connector_tool_name(longest, name) for name in names]
    assert len(set(aliases)) == len(names) and all(len(alias) <= 64 for alias in aliases)
