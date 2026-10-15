from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from alysis_code.agent.session import create_session
from alysis_code.config import AppConfig
from alysis_code.extensions.models import ExtensionState, InstalledExtensionState
from alysis_code.extensions.paths import project_extensions_path
from alysis_code.extensions.state import save_global_state
from alysis_code.extensions.workspace_trust import grant_workspace_trust
from alysis_code.ide import management_protocol
from alysis_code.ide.protocol import ProtocolError
from alysis_code.ide.skill_invocation import validate_skill_invocation
from alysis_code.skills.state import SkillLifecycleState, save_project_skill_state


@pytest.mark.parametrize(
    "skills_enabled,bundled_enabled", [(True, True), (False, True), (True, False)]
)
def test_alysis_docs_available_in_app_catalog_and_cli_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skills_enabled: bool, bundled_enabled: bool
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    cfg = AppConfig(
        model="test-model",
        web_search_mode="off",
        routing_mode="code_only",
        skills_enabled=skills_enabled,
        bundled_skills_enabled=bundled_enabled,
    )
    monkeypatch.setattr(management_protocol, "load_config", lambda: cfg)
    catalog = management_protocol.handle_management_method(
        "skill.list", {"workspace": str(tmp_path)}, request_id="list"
    )
    available = {entry["name"] for entry in catalog["skills"] if entry["available"]}
    expected = skills_enabled and bundled_enabled
    assert ("alysis-docs" in available) is expected
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="readonly",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="test-placeholder",
        non_interactive=True,
    )
    try:
        if expected:
            selection = {"name": "alysis-docs", "arguments": "How do profiles work?"}
            assert validate_skill_invocation(session, selection) == selection
            contents = "\n".join(str(message.get("content") or "") for message in session.messages)
            assert "alysis-docs" in contents
            # Advertise only; the actual documentation workflow loads on demand.
            assert "https://alysiscode.com/llms.txt" not in contents
            tool = session.tools["skill_read"]
            result = tool.run({"name": "alysis-docs"})
            assert "https://alysiscode.com/llms.txt" in result["content"]
            session.store.append("tool_call", {"name": "skill_read", "arguments": selection})
            session.store.append("tool_result", {"name": "skill_read", "result": result})
            classification, url = session.store.resolve_web_fetch_url(
                "https://alysiscode.com/llms.txt"
            )
            assert classification is not None
            assert url == "https://alysiscode.com/llms.txt"
        else:
            with pytest.raises(ProtocolError):
                validate_skill_invocation(session, {"name": "alysis-docs", "arguments": "Help"})
    finally:
        session.close()


@pytest.mark.parametrize(
    "plugin_enabled,override,trusted,skill_enabled,skills_enabled,expected",
    [
        (False, None, False, True, True, False),
        (True, None, False, True, True, True),
        (False, "enabled", False, True, True, False),
        (False, "enabled", True, True, True, True),
        (True, "disabled", True, True, True, False),
        (True, None, False, False, True, False),
        (True, None, False, True, False, False),
    ],
)
def test_catalog_availability_matches_real_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plugin_enabled: bool,
    override: str | None,
    trusted: bool,
    skill_enabled: bool,
    skills_enabled: bool,
    expected: bool,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    workspace = tmp_path / "workspace"
    # Component ownership can refer to the bundle alias rather than its declared name.
    for bundle_name, skill_name in (("plugin-bundle", "δοκιμή"), ("ordinary", "ordinary")):
        bundle = workspace / ".alysis_skills" / bundle_name
        bundle.mkdir(parents=True)
        (bundle / "SKILL.md").write_text(
            f"---\nname: {skill_name}\ndescription: Review changes\n---\nReview $ARGUMENTS.\n",
            encoding="utf-8",
        )
    save_global_state(
        ExtensionState(
            installed={
                "example.review": InstalledExtensionState(
                    id="example.review",
                    enabled=plugin_enabled,
                    component_ids={"skill": ["plugin-bundle"]},
                )
            }
        )
    )
    if override:
        path = project_extensions_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps({override: ["example.review"]}).encode()
        path.write_bytes(raw)
        if trusted:
            grant_workspace_trust(
                repo_root=workspace, overrides_sha256=hashlib.sha256(raw).hexdigest()
            )
    if not skill_enabled:
        save_project_skill_state(workspace, SkillLifecycleState(disabled_names=("δοκιμή",)))
    cfg = AppConfig(
        model="test-model",
        web_search_mode="off",
        routing_mode="code_only",
        skills_enabled=skills_enabled,
        bundled_skills_enabled=False,
    )
    monkeypatch.setattr(management_protocol, "load_config", lambda: cfg)
    params = {"workspace": str(workspace)}
    catalog = management_protocol.handle_management_method("skill.list", params, request_id="list")
    entries = {entry["name"]: entry for entry in catalog["skills"]}
    assert entries["δοκιμή"]["enabled"] is skill_enabled
    assert entries["δοκιμή"]["available"] is expected
    assert entries["ordinary"]["available"] is skills_enabled
    info = management_protocol.handle_management_method(
        "skill.info", {**params, "name": "plugin-bundle"}, request_id="info"
    )
    assert info["skill"]["available"] is expected
    session = create_session(
        cfg=cfg,
        root=workspace,
        mode="readonly",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="test-placeholder",
        non_interactive=True,
    )
    try:
        selection = {"name": "plugin-bundle", "arguments": "Ελληνικά and 日本語"}
        if expected:
            assert validate_skill_invocation(session, selection)["name"] == "δοκιμή"
        else:
            with pytest.raises(ProtocolError):
                validate_skill_invocation(session, selection)
    finally:
        session.close()
