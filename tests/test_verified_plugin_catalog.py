"""The built-in verified plugins, each exactly as Alysis reviewed it (DESIGN.md §2).

Every shipped descriptor must carry the reviewed server, sign-in and tool list with its read
marks, be pinned by its catalog entry, and bind through the connector host. A tool left out on
purpose must never appear.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from alysis_code.extensions.bundled import bundled_manifest
from alysis_code.extensions.install import install_plugin
from alysis_code.extensions.manifest import PluginManifest
from alysis_code.extensions.registry import find_by_id, load_registry
from alysis_code.ide.health import capabilities_payload
from alysis_code.ide.management_protocol import handle_management_method
from alysis_code.mcp.connector_host import (
    VERIFIED_CONNECTOR_PLUGIN_IDS,
    bind_connector_catalogs,
    connector_tool_name,
    normalize_connector_catalogs,
)
from tests.test_verified_connector_plugins import NOTION_LEFT_OUT, NOTION_READS, NOTION_WRITES

#: Built-in plugins: GitHub, eight Google services, and the verified plugins below.
BUNDLED_PLUGINS = 9 + 10

REVIEWED: dict[str, dict[str, Any]] = {
    "alysis.notion": {
        "name": "Notion",
        "category": "notes-wiki",
        "connector": {
            "id": "notion",
            "url": "https://mcp.notion.com/mcp",
            "resource": "https://mcp.notion.com/mcp",
            "oauth_issuer": "https://mcp.notion.com",
            "oauth_sign_in_origins": [],
            "oauth_scopes": ["default"],
            "max_argument_bytes": 32768,
            "host_arguments": [],
        },
        "reads": NOTION_READS,
        "writes": NOTION_WRITES,
        "left_out": NOTION_LEFT_OUT,
    },
    "alysis.linear": {
        "name": "Linear",
        "category": "tasks-projects",
        "connector": {
            "id": "linear",
            "url": "https://mcp.linear.app/mcp",
            "resource": "https://mcp.linear.app/mcp",
            "oauth_issuer": "https://mcp.linear.app",
            "oauth_sign_in_origins": [],
            "oauth_scopes": ["read", "write"],
            "max_argument_bytes": 32768,
            "host_arguments": [],
        },
        "reads": (
            "list_issues get_issue list_issue_statuses get_issue_status list_issue_labels "
            "list_comments list_cycles list_documents get_document list_projects get_project "
            "list_project_labels list_milestones get_milestone list_teams get_team list_users "
            "get_user get_attachment search_documentation list_customer_needs"
        ).split(),
        "writes": (
            "save_issue save_comment delete_comment save_document create_document "
            "update_document create_issue_label save_project save_milestone"
        ).split(),
        "left_out": (
            "create_attachment delete_attachment extract_images save_customer_need search fetch "
            "create_issue update_issue"
        ).split(),
    },
    "alysis.sentry": {
        "name": "Sentry",
        "category": "code",
        "connector": {
            "id": "sentry",
            "url": "https://mcp.sentry.dev/mcp",
            "resource": "https://mcp.sentry.dev/mcp",
            "oauth_issuer": "https://mcp.sentry.dev",
            "oauth_sign_in_origins": [],
            "oauth_scopes": [
                "org:read",
                "project:write",
                "team:write",
                "event:write",
                "alerts:write",
            ],
            "max_argument_bytes": 8192,
            "host_arguments": [],
        },
        "reads": (
            "find_organizations find_projects search_issues search_errors search_logs "
            "search_traces search_metrics search_profiles search_replays find_dropped_events "
            "get_sentry_resource"
        ).split(),
        "writes": ("update_issue").split(),
        "left_out": ("analyze_issue_with_seer search_sentry_tools execute_sentry_tool").split(),
    },
    "alysis.atlassian": {
        "name": "Jira + Confluence",
        "category": "tasks-projects",
        "connector": {
            "id": "atlassian",
            "url": "https://mcp.atlassian.com/v2/mcp?tools=all",
            "resource": "https://mcp.atlassian.com/v2/mcp",
            "oauth_issuer": "https://auth.atlassian.com/VCeDsk8ZHncYF1g234fKtc4lNipbBhu3",
            "oauth_sign_in_origins": ["https://auth.atlassian.com"],
            "oauth_scopes": [
                "read:me",
                "offline_access",
                "read:jira:agent-interface",
                "write:jira:agent-interface",
                "search:jira:agent-interface",
                "read:confluence:agent-interface",
                "write:confluence:agent-interface",
                "search:confluence:agent-interface",
            ],
            "max_argument_bytes": 32768,
            "host_arguments": [{"name": "cloudId", "source": "atlassian_site"}],
        },
        "reads": (
            "atlassianUserInfo getContentFormatGuide getJiraIssue listJiraIssueComments "
            "searchJiraIssuesUsingJql listJiraProjects listJiraProjectIssueTypesMetadata "
            "getJiraIssueTypeMetaWithFields listJiraIssueTransitions listJiraIssueLinkTypes "
            "listJiraIssueRemoteIssueLinks listJiraIssueWorklogs lookupJiraAccountId "
            "listJiraIssueAssignableUsers listJiraIssueChangelogs getJiraCurrentUser getJiraUser "
            "listJiraStatuses listJiraProjectComponents getJiraProjectVersions "
            "getJiraProjectVersionRelatedWork listJiraBoards getJiraBoardConfig "
            "getJiraBoardIssueData getJiraBoardSprintData listJiraBoardSprints listJiraFilters "
            "listJiraDashboards getJiraEntityProperty getConfluenceContent searchConfluence "
            "listConfluenceContent listConfluenceSpaces getConfluenceSpace "
            "getConfluenceSpaceInstructions getConfluencePersonalSpace listConfluenceComments "
            "getConfluenceComment listConfluenceContentVersions getConfluenceContentVersion "
            "diffConfluenceContentVersions listConfluenceAttachments getConfluenceAttachment "
            "exportConfluenceContent getConfluenceContentPermissions "
            "getConfluenceContentRestrictionState getConfluencePublicLinkStatus "
            "getConfluenceReactions getConfluenceTask listConfluenceTasks "
            "listConfluenceTemplates getConfluenceTemplate resolveConfluenceContentMacros "
            "getConfluenceContentSuggestions"
        ).split(),
        "writes": (
            "createJiraIssue editJiraIssue transitionJiraIssue addOrEditJiraIssueComment "
            "addOrEditJiraIssueWorklog createJiraIssueLink watchJiraIssue "
            "createConfluenceContent updateConfluenceContent createConfluenceComment "
            "updateConfluenceComment updateConfluenceCommentResolution "
            "addLabelsToConfluenceContent completeConfluenceTask reopenConfluenceTask "
            "moveConfluenceContent archiveConfluenceContent unarchiveConfluenceContent"
        ).split(),
        "left_out": (
            "getAccessibleAtlassianResources discover executeRead executeWrite "
            "executeDestructive downloadJiraIssueAttachment downloadConfluenceAttachment "
            "getConfluenceMauiApp deleteJiraIssue deleteJiraComment deleteJiraIssueAttachment "
            "createJiraProject updateJiraProject updateJiraScreen getJiraWorkflows "
            "manageJiraSprint manageJiraProjectVersion createJiraBoard "
            "uploadAttachmentToJiraIssue createConfluenceSpace setConfluenceSpaceInstructions "
            "createConfluenceAttachment addConfluenceContentPermissions "
            "enableConfluencePublicLink createConfluenceInfographicForPage "
            "createConfluenceMauiApp getTeamworkGraphContext search"
        ).split(),
    },
    "alysis.supabase": {
        "name": "Supabase",
        "category": "code",
        "connector": {
            "id": "supabase",
            "url": "https://mcp.supabase.com/mcp",
            "resource": "https://mcp.supabase.com/mcp",
            "oauth_issuer": "https://api.supabase.com",
            "oauth_sign_in_origins": ["https://api.supabase.com"],
            "oauth_scopes": [
                "organizations:read",
                "projects:read",
                "database:read",
                "database:write",
                "analytics:read",
                "edge_functions:read",
                "edge_functions:write",
                "storage:read",
                "storage:write",
            ],
            "max_argument_bytes": 32768,
            "host_arguments": [],
        },
        "reads": (
            "list_tables list_extensions list_migrations query_logs get_advisors "
            "get_project_url generate_typescript_types list_edge_functions "
            "get_edge_function list_projects get_project list_organizations get_organization "
            "search_docs list_storage_buckets get_storage_config"
        ).split(),
        "writes": "execute_sql apply_migration deploy_edge_function update_storage_config".split(),
        "left_out": (
            "create_project get_cost confirm_cost create_branch pause_project restore_project "
            "delete_branch reset_branch create_edge_function_secret "
            # Their documented permission isn't one Alysis asks for (review SUPABASE-SOL-2).
            "list_branches merge_branch rebase_branch get_publishable_keys"
        ).split(),
    },
    "alysis.gitlab": {
        "name": "GitLab",
        "category": "code",
        "connector": {
            "id": "gitlab",
            "url": "https://gitlab.com/api/v4/mcp",
            "resource": "https://gitlab.com/api/v4/mcp",
            "oauth_issuer": "https://gitlab.com",
            "oauth_sign_in_origins": [],
            "oauth_scopes": ["mcp"],
            "max_argument_bytes": 32768,
            "host_arguments": [],
        },
        "reads": (
            "get_mcp_server_version get_project get_user list_groups list_projects "
            "list_project_members list_todos search search_labels get_merge_request "
            "list_merge_requests get_work_item list_work_items get_work_item_types "
            "get_saved_view_work_items get_repository_file list_repository_tree "
            "get_commit list_commits list_branches list_tags list_releases "
            "get_pipeline list_pipelines get_job get_artifact_file list_wiki_pages"
        ).split(),
        "writes": (
            "save_note save_merge_request accept_merge_request save_work_item "
            "link_work_items add_branch fork_repository add_commit save_pipeline"
        ).split(),
        "left_out": (
            "semantic_search get_pages_content save_merge_request_review "
            "list_duo_agents_and_flows list_duo_sessions get_duo_session "
            "start_duo_session send_duo_session_input list_vulnerabilities "
            "get_vulnerability save_vulnerability attach_scan_profile manage_pipeline "
            "create_issue get_merge_request_diffs"
        ).split(),
    },
    "alysis.huggingface": {
        "name": "Hugging Face",
        "category": "code",
        "connector": {
            "id": "huggingface",
            "url": "https://huggingface.co/mcp",
            "resource": "https://huggingface.co/mcp",
            "oauth_issuer": "https://huggingface.co",
            "oauth_sign_in_origins": [],
            "oauth_scopes": ["openid", "profile", "read-mcp", "read-repos"],
            "max_argument_bytes": 8192,
            "host_arguments": [],
        },
        "reads": "hf_whoami hf_fs hub_repo_search hub_repo_details".split(),
        "writes": [],
        "left_out": (
            "create_repo hf_fs_write hf_jobs hf_sandbox hf_sandbox_exec hf_sandbox_fs dynamic_space"
        ).split(),
    },
    "alysis.clickup": {
        "name": "ClickUp",
        "category": "tasks-projects",
        "connector": {
            "id": "clickup",
            "url": "https://mcp.clickup.com/mcp",
            "resource": "https://mcp.clickup.com/mcp",
            "oauth_issuer": "https://mcp.clickup.com",
            "oauth_sign_in_origins": [],
            "oauth_scopes": ["read", "write"],
            "max_argument_bytes": 32768,
            "host_arguments": [],
        },
        "reads": (
            "clickup_get_task clickup_filter_tasks clickup_get_custom_fields "
            "clickup_get_current_time_entry clickup_get_time_entries "
            "clickup_get_task_time_in_status clickup_get_bulk_tasks_time_in_status "
            "clickup_download_task_attachment clickup_get_task_comments "
            "clickup_get_threaded_comments clickup_list_document_pages "
            "clickup_get_document_pages clickup_list_document_page_attachments "
            "clickup_download_document_page_attachment clickup_get_chat_channels "
            "clickup_get_chat_channel_messages clickup_get_chat_message_replies "
            "clickup_get_list clickup_get_folder clickup_search_reminders "
            "clickup_get_workspace_hierarchy clickup_get_workspace_members "
            "clickup_find_member_by_name clickup_resolve_assignees clickup_get_user "
            "clickup_search"
        ).split(),
        "writes": (
            "clickup_create_task clickup_update_task clickup_delete_task "
            "clickup_move_task clickup_add_task_to_list clickup_remove_task_from_list "
            "clickup_add_task_dependency clickup_remove_task_dependency "
            "clickup_add_task_link clickup_remove_task_link clickup_create_space_tag "
            "clickup_add_tag_to_task clickup_remove_tag_from_task "
            "clickup_start_time_tracking clickup_stop_time_tracking "
            "clickup_add_time_entry clickup_create_comment "
            "clickup_create_task_comment clickup_update_comment "
            "clickup_create_document clickup_create_document_page "
            "clickup_update_document_page clickup_send_chat_message "
            "clickup_update_chat_message clickup_react_to_chat_message "
            "clickup_create_list clickup_create_list_in_folder clickup_update_list "
            "clickup_create_folder clickup_update_folder clickup_create_reminder "
            "clickup_update_reminder"
        ).split(),
        "left_out": (
            "clickup_merge_tasks clickup_delete_comment clickup_execute_operator "
            "clickup_get_operators clickup_get_schema clickup_attach_task_file "
            "clickup_request_attachment_upload"
        ).split(),
    },
    "alysis.todoist": {
        "name": "Todoist",
        "category": "tasks-projects",
        "connector": {
            "id": "todoist",
            "url": "https://ai.todoist.net/mcp",
            "resource": "https://ai.todoist.net/mcp",
            "oauth_issuer": "https://todoist.com",
            "oauth_sign_in_origins": ["https://todoist.com"],
            "oauth_scopes": ["data:read_write"],
            "max_argument_bytes": 32768,
            "host_arguments": [],
        },
        "reads": (
            "find-tasks find-tasks-by-date find-completed-tasks find-projects "
            "find-sections find-comments find-reminders view-attachment find-labels "
            "find-filters find-activity get-productivity-stats get-project-health "
            "get-project-activity-stats get-workspace-insights get-overview "
            "fetch-object user-info find-project-collaborators list-workspaces"
        ).split(),
        "writes": (
            "add-tasks complete-tasks uncomplete-tasks update-tasks reschedule-tasks "
            "add-projects update-projects project-management project-move "
            "add-sections update-sections add-comments update-comments add-reminders "
            "update-reminders add-labels update-labels add-filters update-filters "
            "reorder-objects manage-assignments"
        ).split(),
        "left_out": (
            "delete-object export-project-template import-project-template "
            "analyze-project-health search fetch"
        ).split(),
    },
    "alysis.airtable": {
        "name": "Airtable",
        "category": "docs-files",
        "connector": {
            "id": "airtable",
            "url": "https://mcp.airtable.com/mcp",
            "resource": "https://mcp.airtable.com",
            "oauth_issuer": "https://airtable.com/oauth2/v1",
            "oauth_sign_in_origins": ["https://airtable.com"],
            "oauth_scopes": [
                "data.records:read",
                "data.records:write",
                "schema.bases:read",
                "schema.bases:write",
                "workspacesAndBases:read",
            ],
            "max_argument_bytes": 32768,
            "host_arguments": [],
        },
        "reads": (
            "fetch_automation_input_data get_automation "
            "get_create_automation_instructions list_automations list_bases "
            "search_bases get_form_schema list_records_for_page get_record_for_page "
            "describe_page_element describe_page_type list_pages_for_base "
            "display_records_for_table list_records_for_table "
            "search_candidate_linked_records search_records get_table_schema "
            "list_views_for_table list_tables_for_base list_workspaces "
            "list_external_accounts"
        ).split(),
        "writes": (
            "create_automation update_automation create_base create_field "
            "update_field submit_form create_page create_interface "
            "create_records_for_table update_records_for_table revert_action "
            "create_table update_table"
        ).split(),
        "left_out": (
            "delete_records_for_table delete_table delete_interface delete_page "
            "delete_automation test_automation_webhook_trigger"
        ).split(),
    },
}


def _content(plugin_id: str) -> bytes:
    return bundled_manifest(find_by_id(load_registry(), plugin_id))


def _manifest(plugin_id: str) -> PluginManifest:
    return PluginManifest.model_validate(tomllib.loads(_content(plugin_id).decode("utf-8")))


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


def test_the_engine_binds_exactly_the_reviewed_plugins():
    assert list(VERIFIED_CONNECTOR_PLUGIN_IDS) == list(REVIEWED)
    feature = capabilities_payload()["features"]["custom_connector_mcp"]
    assert feature["verified_plugins"] == list(REVIEWED)


def test_every_built_in_plugin_is_in_the_catalog():
    found = handle_management_method("ext.search", {}, request_id=1)
    assert found["count"] == BUNDLED_PLUGINS
    assert set(REVIEWED) <= {entry["id"] for entry in found["extensions"]}


@pytest.mark.parametrize("plugin_id", REVIEWED)
def test_the_descriptor_is_exactly_the_reviewed_one(plugin_id: str):
    review = REVIEWED[plugin_id]
    manifest = _manifest(plugin_id)
    assert manifest.plugin.id == plugin_id and manifest.plugin.name == review["name"]
    components = manifest.components
    assert not (components.mcp_server or components.tool or components.hook or components.skill)
    [connector] = components.verified_connector
    expected = review["connector"]
    assert connector.id == expected["id"]
    assert connector.url == expected["url"]
    assert connector.effective_resource == expected["resource"]
    assert connector.oauth_issuer == expected["oauth_issuer"]
    assert connector.oauth_sign_in_origins == expected["oauth_sign_in_origins"]
    assert connector.oauth_client == "dynamic"
    assert connector.oauth_scopes == expected["oauth_scopes"]
    assert connector.max_argument_bytes == expected["max_argument_bytes"]
    assert [a.model_dump() for a in connector.host_arguments] == expected["host_arguments"]
    # The reviewed lists, in order, with their marks; nothing left out sneaks in.
    assert [t.name for t in connector.tools if t.reads_only] == review["reads"]
    assert [t.name for t in connector.tools if not t.reads_only] == review["writes"]
    assert len(connector.tools) == len(review["reads"]) + len(review["writes"])
    assert not set(review["left_out"]) & {t.name for t in connector.tools}
    assert manifest.security is not None and manifest.security.contact.startswith("https://")


@pytest.mark.parametrize("plugin_id", REVIEWED)
def test_the_catalog_entry_pins_it(plugin_id: str):
    entry = find_by_id(load_registry(), plugin_id)
    digest = hashlib.sha256(_content(plugin_id)).hexdigest()
    assert entry.source_kind == "bundled"
    assert entry.package_sha256 == digest and entry.commit == f"sha256:{digest}"
    assert entry.tier == "alysis" and entry.publisher == "Alysis"
    assert entry.categories == [REVIEWED[plugin_id]["category"]]
    assert entry.name == REVIEWED[plugin_id]["name"]


@pytest.mark.parametrize("plugin_id", REVIEWED)
def test_the_plugin_page_shows_the_reviewed_connector(plugin_id: str):
    review = REVIEWED[plugin_id]
    found = handle_management_method("ext.search", {"query": plugin_id}, request_id=1)
    [entry] = [e for e in found["extensions"] if e["id"] == plugin_id]
    block = entry["verified_connector"]
    assert {k: v for k, v in block.items() if k != "tools"} == review["connector"]
    assert block["tools"] == [{"name": n, "reads_only": True} for n in review["reads"]] + [
        {"name": n, "reads_only": False} for n in review["writes"]
    ]
    connector = review["connector"]
    assert entry["connects_to"] == [connector["url"], *connector["oauth_sign_in_origins"]]
    assert entry["categories"] == [review["category"]]


@pytest.mark.parametrize("plugin_id", REVIEWED)
def test_model_facing_names_are_distinct_and_fit(plugin_id: str):
    review = REVIEWED[plugin_id]
    names = [connector_tool_name(plugin_id, n) for n in review["reads"] + review["writes"]]
    short = plugin_id.split(".", 1)[1]
    assert len(set(names)) == len(names)
    assert all(len(n) <= 64 and n.startswith(f"mcp__{short}__") for n in names)


@pytest.mark.parametrize("plugin_id", REVIEWED)
def test_it_installs_for_the_user_and_binds_with_the_reviewed_marks(root: Path, plugin_id: str):
    review = REVIEWED[plugin_id]
    requests: list[Any] = []
    install_plugin(
        source=plugin_id, repo_root=root, trust_prompt=lambda r: requests.append(r) or True
    )
    assert requests[0].permissions_summary.network is True
    # Hugging Face is read only: its catalog is one read.
    read, *writes = review["reads"][0], *review["writes"][:1]
    catalog = {
        "protocol_version": 1,
        "plugin_id": plugin_id,
        "server_id": f"{plugin_id}/{review['connector']['id']}",
        "descriptor_sha256": hashlib.sha256(_content(plugin_id)).hexdigest(),
        "tools": [
            {"name": name, "description": "", "inputSchema": {"type": "object"}}
            for name in (read, *writes)
        ],
    }
    [bound] = bind_connector_catalogs(root, normalize_connector_catalogs([catalog]))
    assert [(t["name"], t["readOnly"]) for t in bound["tools"]] == [
        (read, True),
        *((write, False) for write in writes),
    ]
    assert bound["plugin"]["source_kind"] == "bundled" and bound["plugin"]["scope"] == "user"
    assert bound["max_argument_bytes"] == review["connector"]["max_argument_bytes"]
    assert bound["host_arguments"] == [a["name"] for a in review["connector"]["host_arguments"]]


# --- Jira + Confluence: the site argument is the app's, end to end -------------------------

ATLASSIAN = "alysis.atlassian"


def _site_catalog(*, cloud_id_in_schema: bool = False) -> dict[str, Any]:
    properties: dict[str, Any] = {"issueIdOrKey": {"type": "string"}}
    if cloud_id_in_schema:
        properties["cloudId"] = {"type": "string"}
    return {
        "protocol_version": 1,
        "plugin_id": ATLASSIAN,
        "server_id": f"{ATLASSIAN}/atlassian",
        "descriptor_sha256": hashlib.sha256(_content(ATLASSIAN)).hexdigest(),
        "tools": [
            {
                "name": name,
                "description": f"{name} fixture",
                "inputSchema": {"type": "object", "properties": properties},
            }
            for name in ("getJiraIssue", "createJiraIssue")
        ],
    }


def _install_atlassian(root: Path) -> None:
    install_plugin(source=ATLASSIAN, repo_root=root, trust_prompt=lambda _r: True)


def test_a_site_catalog_still_showing_cloud_id_fails_the_session(root: Path, monkeypatch):
    from tests.test_verified_connector_plugins import _create

    _install_atlassian(root)
    captured = _create(root, monkeypatch, _site_catalog(cloud_id_in_schema=True))
    assert captured["create"]["ok"] is False
    assert captured["create"]["error"]["code"] == "invalid_connector_mcp"
    captured = _create(root, monkeypatch, _site_catalog())
    assert captured["create"]["ok"], captured["create"]
    [bound] = captured["connector_mcp"]
    assert bound["host_arguments"] == ["cloudId"] and bound["max_argument_bytes"] == 32768


def test_a_call_setting_cloud_id_never_reaches_the_app(root: Path, monkeypatch):
    from tests.test_ide_host_actions import _wait_for
    from tests.test_verified_connector_plugins import _bridge, _start

    _install_atlassian(root)
    out, bridge, captured, results, errors = _bridge(
        root,
        monkeypatch,
        _site_catalog(),
        mode="fullaccess",
        tool="getJiraIssue",
        arguments={"issueIdOrKey": "OPS-1", "cloudId": "another-site"},
    )
    try:
        _start(out, bridge, captured)
        _wait_for(out, lambda line: line.get("type") == "message_end")
        assert not results and errors[0].code == "invalid_host_action_arguments"
        lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        assert not [line for line in lines if line.get("type") == "host_action_requested"]
    finally:
        bridge.close()


def test_a_call_without_cloud_id_reaches_the_app_and_writes_still_ask(root: Path, monkeypatch):
    from tests.test_custom_plugins import _next
    from tests.test_verified_connector_plugins import _bridge, _prepared, _start

    _install_atlassian(root)
    out, bridge, captured, _results, _errors = _bridge(
        root,
        monkeypatch,
        _site_catalog(),
        mode="fullaccess",
        tool="getJiraIssue",
        arguments={"issueIdOrKey": "OPS-1"},
    )
    try:
        _start(out, bridge, captured)
        _prepared(bridge, out, write=False, letter="a", tool="getJiraIssue")
        assert _next(out)["payload"]["phase"] == "execute"
    finally:
        bridge.close()
    out, bridge, captured, _results, _errors = _bridge(
        root,
        monkeypatch,
        _site_catalog(),
        mode="fullaccess",
        tool="createJiraIssue",
        arguments={"issueIdOrKey": "OPS-1"},
    )
    try:
        _start(out, bridge, captured)
        _prepared(bridge, out, write=True, letter="b", tool="createJiraIssue")
        prompt = _next(out)
        assert prompt["type"] == "prompt_for_input", prompt
        assert prompt["payload"]["metadata"]["always_ask_person"] is True
    finally:
        bridge.close()


def test_the_production_tool_refuses_cloud_id_before_calling_the_app(root: Path):
    from alysis_code.agent.errors import AgentRuntimeError
    from tests.test_verified_connector_plugins import _RESULT, _production_tools

    _install_atlassian(root)
    bound = bind_connector_catalogs(root, normalize_connector_catalogs([_site_catalog()]))
    calls: list[Any] = []
    tools = _production_tools(root, bound, lambda _action, args: calls.append(args) or _RESULT)
    name = connector_tool_name(ATLASSIAN, "getJiraIssue")
    assert name == "mcp__atlassian__getjiraissue"
    with pytest.raises(AgentRuntimeError):
        tools[name].run({"issueIdOrKey": "OPS-1", "cloudId": "another-site"})
    assert calls == []
    tools[name].run({"issueIdOrKey": "OPS-1"})
    assert calls == [
        {"plugin_id": ATLASSIAN, "name": "getJiraIssue", "arguments": {"issueIdOrKey": "OPS-1"}}
    ]


# --- Supabase: reads run, every change (running SQL too) asks, unreviewed tools never bind ---

SUPABASE = "alysis.supabase"


def _supabase_catalog(*names: str) -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "plugin_id": SUPABASE,
        "server_id": f"{SUPABASE}/supabase",
        "descriptor_sha256": hashlib.sha256(_content(SUPABASE)).hexdigest(),
        "tools": [
            {
                "name": name,
                "description": f"{name} fixture",
                "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
            }
            for name in names or ("list_tables", "execute_sql")
        ],
    }


@pytest.mark.parametrize(
    "tool",
    "create_project confirm_cost pause_project delete_branch create_edge_function_secret "
    "list_branches merge_branch rebase_branch get_publishable_keys".split(),
)
def test_a_supabase_tool_alysis_left_out_fails_the_session(root: Path, monkeypatch, tool: str):
    from tests.test_verified_connector_plugins import _create

    install_plugin(source=SUPABASE, repo_root=root, trust_prompt=lambda _r: True)
    captured = _create(root, monkeypatch, _supabase_catalog("list_tables", tool))
    assert captured["create"]["ok"] is False
    assert captured["create"]["error"]["code"] == "invalid_connector_mcp"
    captured = _create(root, monkeypatch, _supabase_catalog())
    assert captured["create"]["ok"], captured["create"]
    [receipt] = captured["create"]["result"]["connector_mcp"]
    assert receipt["tool_names"] == ["list_tables", "execute_sql"]
    [bound] = captured["connector_mcp"]
    assert [(t["name"], t["readOnly"]) for t in bound["tools"]] == [
        ("list_tables", True),
        ("execute_sql", False),
    ]
    assert bound["max_argument_bytes"] == 32768 and bound["host_arguments"] == []


def test_a_supabase_read_runs_and_sql_always_asks_even_in_full_access(root: Path, monkeypatch):
    from tests.test_custom_plugins import _next
    from tests.test_verified_connector_plugins import _bridge, _prepared, _start

    install_plugin(source=SUPABASE, repo_root=root, trust_prompt=lambda _r: True)
    out, bridge, captured, _results, _errors = _bridge(
        root,
        monkeypatch,
        _supabase_catalog(),
        mode="fullaccess",
        tool="list_tables",
        arguments={"query": "public"},
    )
    try:
        _start(out, bridge, captured)
        _prepared(bridge, out, write=False, letter="a", tool="list_tables")
        assert _next(out)["payload"]["phase"] == "execute"
    finally:
        bridge.close()
    # A long migration fits Supabase's own limit; running it still asks a person.
    out, bridge, captured, _results, _errors = _bridge(
        root,
        monkeypatch,
        _supabase_catalog(),
        mode="fullaccess",
        tool="execute_sql",
        arguments={"query": "select 1;" + " " * 20_000},
    )
    try:
        _start(out, bridge, captured)
        _prepared(bridge, out, write=True, letter="b", tool="execute_sql")
        prompt = _next(out)
        assert prompt["type"] == "prompt_for_input", prompt
        metadata = prompt["payload"]["metadata"]
        assert metadata["always_ask_person"] is True and metadata["access"] == "write"
        assert metadata["tool_server_rule"] == "ask_every_call"
        assert prompt["payload"]["allow_for_session_supported"] is False
    finally:
        bridge.close()


# The permission each Supabase tool needs, from Supabase's Management API reference (the endpoints
# its MCP server calls): a reviewed tool must be covered by the scopes Alysis asks for, and the
# scopes are never widened to bring an unscoped tool in (review SUPABASE-SOL-2). None: no API call.
SUPABASE_TOOL_SCOPES = {
    "list_tables": "database:read",
    "list_extensions": "database:read",
    "list_migrations": "database:read",
    "generate_typescript_types": "database:read",
    "get_advisors": "database:read",
    "execute_sql": "database:write",
    "apply_migration": "database:write",
    "query_logs": "analytics:read",
    "get_project_url": "projects:read",
    "list_projects": "projects:read",
    "get_project": "projects:read",
    "list_organizations": "organizations:read",
    "get_organization": "organizations:read",
    "list_edge_functions": "edge_functions:read",
    "get_edge_function": "edge_functions:read",
    "deploy_edge_function": "edge_functions:write",
    "list_storage_buckets": "storage:read",
    "get_storage_config": "storage:read",
    "update_storage_config": "storage:write",
    "search_docs": None,
    # Left out: not covered by the scopes below.
    "list_branches": "environment:read",
    "merge_branch": "environment:write",
    "rebase_branch": "environment:write",
    "get_publishable_keys": "secrets:read",
}


def test_every_supabase_tool_is_covered_by_the_scopes_alysis_asks_for():
    [connector] = _manifest(SUPABASE).components.verified_connector
    scopes = set(connector.oauth_scopes)
    assert scopes == set(REVIEWED[SUPABASE]["connector"]["oauth_scopes"])
    assert not scopes & {"environment:read", "environment:write", "secrets:read", "projects:write"}
    for tool in connector.tools:
        assert tool.name in SUPABASE_TOOL_SCOPES, f"{tool.name} has no recorded permission"
        needed = SUPABASE_TOOL_SCOPES[tool.name]
        assert needed is None or needed in scopes, f"{tool.name} needs {needed}"
    unscoped = {
        name for name, needed in SUPABASE_TOOL_SCOPES.items() if needed and needed not in scopes
    }
    assert unscoped == {"list_branches", "merge_branch", "rebase_branch", "get_publishable_keys"}
    assert not unscoped & {tool.name for tool in connector.tools}


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("execute_sql", {"query": "DELETE FROM public.fixture;"}),
        ("apply_migration", {"query": "DROP TABLE public.fixture;"}),
        (
            "deploy_edge_function",
            {"query": "Deno.serve(() => new Response(Deno.env.get('FIXTURE_SECRET')));"},
        ),
    ],
)
def test_supabase_sql_migrations_and_deploys_ask_a_person_every_time(
    root: Path, monkeypatch, tool, arguments
):
    """Any SQL and any function code reach Supabase only after a person approves that one call,
    in every mode; there is no keyword filter and no session-wide approval."""
    from tests.test_custom_plugins import _next
    from tests.test_verified_connector_plugins import _bridge, _prepared, _start

    install_plugin(source=SUPABASE, repo_root=root, trust_prompt=lambda _r: True)
    for letter, mode in zip("cde", ("review", "auto", "fullaccess"), strict=True):
        out, bridge, captured, _results, _errors = _bridge(
            root,
            monkeypatch,
            _supabase_catalog("list_tables", tool),
            mode=mode,
            tool=tool,
            arguments=arguments,
        )
        try:
            _start(out, bridge, captured)
            _prepared(bridge, out, write=True, letter=letter, tool=tool)
            prompt = _next(out)
            assert prompt["type"] == "prompt_for_input", (mode, prompt)
            metadata = prompt["payload"]["metadata"]
            assert metadata["always_ask_person"] is True and metadata["access"] == "write"
            assert metadata["tool_server_rule"] == "ask_every_call"
            assert prompt["payload"]["allow_for_session_supported"] is False
        finally:
            bridge.close()


# --- Batch 3: GitLab, Hugging Face, ClickUp, Todoist, Airtable ------------------------------

BATCH3 = (
    "alysis.gitlab",
    "alysis.huggingface",
    "alysis.clickup",
    "alysis.todoist",
    "alysis.airtable",
)


def _catalog(plugin_id: str, *names: str) -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "plugin_id": plugin_id,
        "server_id": f"{plugin_id}/{REVIEWED[plugin_id]['connector']['id']}",
        "descriptor_sha256": hashlib.sha256(_content(plugin_id)).hexdigest(),
        "tools": [
            {
                "name": name,
                "description": f"{name} fixture",
                "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
            }
            for name in names
        ],
    }


@pytest.mark.parametrize(
    ("plugin_id", "tool"),
    [(plugin_id, tool) for plugin_id in BATCH3 for tool in REVIEWED[plugin_id]["left_out"]],
)
def test_a_batch3_tool_alysis_left_out_fails_the_session(
    root: Path, monkeypatch, plugin_id: str, tool: str
):
    from tests.test_verified_connector_plugins import _create

    install_plugin(source=plugin_id, repo_root=root, trust_prompt=lambda _r: True)
    read = REVIEWED[plugin_id]["reads"][0]
    captured = _create(root, monkeypatch, _catalog(plugin_id, read, tool))
    assert captured["create"]["ok"] is False
    assert captured["create"]["error"]["code"] == "invalid_connector_mcp"
    captured = _create(root, monkeypatch, _catalog(plugin_id, read))
    assert captured["create"]["ok"], captured["create"]


@pytest.mark.parametrize("plugin_id", BATCH3)
def test_a_batch3_read_runs_without_asking(root: Path, monkeypatch, plugin_id: str):
    from tests.test_custom_plugins import _next
    from tests.test_verified_connector_plugins import _bridge, _prepared, _start

    read = REVIEWED[plugin_id]["reads"][0]
    install_plugin(source=plugin_id, repo_root=root, trust_prompt=lambda _r: True)
    out, bridge, captured, _results, _errors = _bridge(
        root,
        monkeypatch,
        _catalog(plugin_id, read),
        mode="fullaccess",
        tool=read,
        arguments={"query": "x"},
    )
    try:
        _start(out, bridge, captured)
        _prepared(bridge, out, write=False, letter="a", tool=read)
        assert _next(out)["payload"]["phase"] == "execute"
    finally:
        bridge.close()


# The changes each plugin's text names; every one asks a person, in every mode.
BATCH3_RISKY = {
    "alysis.gitlab": ("accept_merge_request", "add_commit", "save_pipeline"),
    "alysis.clickup": (
        "clickup_send_chat_message",
        "clickup_delete_task",
        "clickup_update_document_page",
    ),
    "alysis.todoist": ("manage-assignments", "project-move", "add-comments"),
    "alysis.airtable": ("update_records_for_table", "create_automation", "create_base"),
}


@pytest.mark.parametrize(
    ("plugin_id", "tool"),
    [(plugin_id, tool) for plugin_id, tools in BATCH3_RISKY.items() for tool in tools],
)
def test_a_batch3_change_asks_a_person_every_time(
    root: Path, monkeypatch, plugin_id: str, tool: str
):
    from tests.test_custom_plugins import _next
    from tests.test_verified_connector_plugins import _bridge, _prepared, _start

    assert tool in REVIEWED[plugin_id]["writes"]
    install_plugin(source=plugin_id, repo_root=root, trust_prompt=lambda _r: True)
    for letter, mode in zip("cde", ("review", "auto", "fullaccess"), strict=True):
        out, bridge, captured, _results, _errors = _bridge(
            root,
            monkeypatch,
            _catalog(plugin_id, REVIEWED[plugin_id]["reads"][0], tool),
            mode=mode,
            tool=tool,
            arguments={"query": "x"},
        )
        try:
            _start(out, bridge, captured)
            _prepared(bridge, out, write=True, letter=letter, tool=tool)
            prompt = _next(out)
            assert prompt["type"] == "prompt_for_input", (mode, prompt)
            metadata = prompt["payload"]["metadata"]
            assert metadata["always_ask_person"] is True and metadata["access"] == "write"
            assert metadata["tool_server_rule"] == "ask_every_call"
            assert prompt["payload"]["allow_for_session_supported"] is False
        finally:
            bridge.close()


def test_hugging_face_is_read_only_and_asks_for_no_write_scope():
    [connector] = _manifest("alysis.huggingface").components.verified_connector
    assert all(tool.reads_only for tool in connector.tools)
    assert not set(connector.oauth_scopes) & {
        "contribute-repos",
        "write-repos",
        "jobs",
        "inference-api",
    }


def test_airtable_signs_in_for_the_site_its_metadata_names():
    [connector] = _manifest("alysis.airtable").components.verified_connector
    assert connector.url == "https://mcp.airtable.com/mcp"
    assert connector.effective_resource == "https://mcp.airtable.com"
    assert not {"data.recordComments:read", "data.recordComments:write"} & set(
        connector.oauth_scopes
    )


def test_todoist_never_asks_for_delete_scopes():
    [connector] = _manifest("alysis.todoist").components.verified_connector
    assert connector.oauth_scopes == ["data:read_write"]
