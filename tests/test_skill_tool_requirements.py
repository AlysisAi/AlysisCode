"""A skill that names the tools it uses is offered only by a session that has all of them.

The rule is the same for every session: each tool a skill declares in ``uses_tools`` must be
among the session's tools. A skill that declares none is offered as before.
"""

from __future__ import annotations

import io
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from alysis_code.agent.tool_definition import ToolDef
from alysis_code.agent_loop import build_tools, create_session
from alysis_code.config import AppConfig
from alysis_code.ide.protocol import ProtocolError
from alysis_code.ide.skill_invocation import validate_skill_invocation
from alysis_code.mcp.manager import McpToolBinding
from alysis_code.session_store import SessionStore
from alysis_code.skills import (
    build_skill_advertise_block,
    discover_skills,
    offer_skills_for_tools,
    validate_skill_bundle,
)
from alysis_code.tools.registry import get_builtin_tool_metadata

# Declared by these fixture skills; one is a tool no session here has.
REPORTS = "αναφορές-μηνός"  # uses the document tools
LOOKUP = "顧客検索"  # uses a connected app's tool that no session here has
WEB = "web-notes"  # uses the web reader
NOTES = "notes"  # declares nothing


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep the developer's own skill folders out of every session built here."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for key, folder in (("ALYSIS_CONFIG_DIR", "config"), ("ALYSIS_DATA_DIR", "data")):
        monkeypatch.setenv(key, str(tmp_path / folder))
    monkeypatch.setattr(
        "alysis_code.skills.discovery.canonical_user_config_dir", lambda: tmp_path / "config"
    )
    return home


def _skill(
    root: Path, folder: str, name: str, uses_tools: str | None, *, skills=".alysis_skills"
) -> Path:
    bundle = root / skills / folder
    bundle.mkdir(parents=True, exist_ok=True)
    declared = f"uses_tools: {uses_tools}\n" if uses_tools is not None else ""
    (bundle / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Μηνιαία εργασία · 毎月の作業\n{declared}---\n\n"
        "Follow the steps.\n",
        encoding="utf-8",
    )
    return bundle


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "έργο · 作業"
    root.mkdir()
    _skill(root, "reports", REPORTS, "[document_operation, artifact_check]")
    _skill(root, "lookup", LOOKUP, "[mcp__crm__customer_lookup]")
    _skill(root, "web", WEB, "[web_fetch]")
    _skill(root, "notes", NOTES, None)
    return root


def _catalog(session) -> str:
    return next(
        (
            str(message["content"])
            for message in session.messages
            if message.get("role") == "user"
            and str(message.get("content") or "").startswith("<skill_context>")
        ),
        "",
    )


def _listed(session) -> set[str]:
    return {
        line.split(" | ", 1)[0].removeprefix("- ")
        for line in _catalog(session).splitlines()
        if " | " in line
    }


def _session(root: Path, **kwargs):
    cfg = kwargs.pop("cfg", None) or AppConfig(
        model="test-model", web_search_mode="off", bundled_skills_enabled=False
    )
    return create_session(
        cfg=cfg,
        root=root,
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="test-placeholder",
        non_interactive=True,
        **kwargs,
    )


def _host_tool(name: str) -> ToolDef:
    return ToolDef(
        name=name,
        description=f"Host fixture tool {name}.",
        parameters={"type": "object", "properties": {}},
        run=lambda arguments: {"ok": True},
    )


def test_uses_tools_is_read_from_frontmatter(tmp_path: Path) -> None:
    bundle = _skill(tmp_path, "reports", REPORTS, "[document_operation, artifact_check, ok-2]")

    validation = validate_skill_bundle(bundle)
    discovered = discover_skills(focus_path=tmp_path, workspace_root=tmp_path)

    assert validation.valid, validation.errors
    assert discovered.skills[REPORTS.casefold()].uses_tools == (
        "document_operation",
        "artifact_check",
        "ok-2",
    )


@pytest.mark.parametrize(
    "value",
    [
        "document_operation",  # one name, not a list
        "[document_operation, 7]",  # not text
        '["two words"]',
        '["fs_read; rm"]',
        '[""]',
        '["9_lives"]',  # starts with a digit
        "{document_operation: true}",
        "[[document_operation]]",
    ],
)
def test_uses_tools_must_be_a_list_of_tool_names(tmp_path: Path, value: str) -> None:
    bundle = _skill(tmp_path, "broken", "broken", value)

    validation = validate_skill_bundle(bundle)
    discovered = discover_skills(focus_path=tmp_path, workspace_root=tmp_path)

    assert not validation.valid
    assert "uses_tools must be a list of tool names" in validation.errors[0].message
    assert "broken" not in discovered.skills
    assert any("uses_tools" in issue.message for issue in discovered.issues)


def test_offer_follows_each_session_tool_set(workspace: Path) -> None:
    discovered = discover_skills(
        focus_path=workspace,
        workspace_root=workspace,
        cfg=AppConfig(model="test-model", bundled_skills_enabled=False),
    )
    with_documents = {"document_operation", "artifact_check", "fs_read"}
    with_web = {"web_fetch", "browser_open"}

    assert {s.name for s in offer_skills_for_tools(discovered, with_documents).ordered} == {
        REPORTS,
        NOTES,
    }
    assert {s.name for s in offer_skills_for_tools(discovered, with_web).ordered} == {WEB, NOTES}
    assert {s.name for s in offer_skills_for_tools(discovered, set()).ordered} == {NOTES}
    every_tool = with_documents | with_web | {"mcp__crm__customer_lookup"}
    assert offer_skills_for_tools(discovered, every_tool) is discovered


@pytest.mark.parametrize("mode", ["auto", "readonly"])
def test_session_lists_reads_and_runs_only_skills_whose_tools_it_has(
    workspace: Path, mode: str
) -> None:
    session = _session(workspace, mode=mode)
    try:
        listed = _listed(session)
        missing = session.tools["skill_read"].run({"name": LOOKUP})
        offered = session.tools["skill_read"].run({"name": REPORTS})
        with pytest.raises(ProtocolError, match="unavailable in this session"):
            validate_skill_invocation(session, {"name": LOOKUP, "arguments": "顧客 A"})
        chosen = validate_skill_invocation(session, {"name": REPORTS, "arguments": "Μάρτιος"})
        registry = {skill.name for skill in session.skill_registry.values()}
        ordered = {skill.name for skill in session.skills_ordered}
    finally:
        session.close()

    # Plan mode holds the document tools back for now; the session still has them, and
    # its skill list is the same in every mode.
    assert listed == registry == ordered == {REPORTS, WEB, NOTES}
    assert missing["error"] == f"Unknown skill: {LOOKUP}"
    assert LOOKUP not in missing["available_skills"]
    assert offered["path"] == "SKILL.md"
    assert chosen["name"] == REPORTS


@pytest.mark.parametrize(
    ("host_tools", "expected"),
    [
        (("document_operation", "artifact_check", "computer_run"), {REPORTS, NOTES}),
        (("web_fetch", "computer_run"), {WEB, NOTES}),
    ],
)
def test_exclusive_host_surface_gets_skill_read_for_the_skills_it_can_follow(
    workspace: Path, host_tools: tuple[str, ...], expected: set[str]
) -> None:
    session = _session(
        workspace,
        mode="auto",
        tools_override={name: _host_tool(name) for name in host_tools},
    )
    try:
        listed = _listed(session)
        tool_names = set(session.tools)
        read = session.tools["skill_read"].run({"name": NOTES})
        hidden = {
            name: session.tools["skill_read"].run({"name": name})
            for name in {REPORTS, WEB, LOOKUP} - expected
        }
    finally:
        session.close()

    assert listed == expected
    assert tool_names == {*host_tools, "skill_read"}
    assert read["name"] == NOTES
    for name, result in hidden.items():
        assert result["error"] == f"Unknown skill: {name}"
        assert sorted(result["available_skills"]) == sorted(expected)


@pytest.mark.parametrize("mode", ["readonly", "auto"])
def test_tools_the_permission_mode_holds_back_are_reported(tmp_path: Path, mode: str) -> None:
    connected = McpToolBinding(
        server_id="crm",
        tool_name="customer_lookup",
        tool_alias="mcp__crm__customer_lookup",
        description="Look up a customer",
        parameters={"type": "object", "properties": {}, "required": []},
        client=SimpleNamespace(),
    )
    reported: list[frozenset[str]] = []
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=SessionStore(
            enabled=False,
            sessions_dir=tmp_path / "sessions",
            session_id="held-back",
            cwd=str(tmp_path),
            repo_root=str(tmp_path),
        ),
        mode=mode,
        yes=True,
        cfg=AppConfig(model="test-model", web_search_mode="off"),
        non_interactive=True,
        mcp_manager=SimpleNamespace(tool_bindings=(connected,)),  # type: ignore[arg-type]
        mode_withheld_tool_names_sink=reported.append,
    )

    (held_back,) = reported
    assert not held_back & set(tools)
    if mode == "readonly":
        assert {"document_operation", "artifact_check", "fs_copy", "shell_run"} <= held_back
        assert "mcp__crm__customer_lookup" in held_back
    else:
        assert held_back == frozenset()
        assert {"document_operation", "mcp__crm__customer_lookup"} <= set(tools)


def test_exclusive_host_surface_stays_exact_with_skills_off(workspace: Path) -> None:
    host_tools = {name: _host_tool(name) for name in ("document_operation", "artifact_check")}
    session = _session(
        workspace,
        mode="auto",
        cfg=AppConfig(model="test-model", web_search_mode="off", skills_enabled=False),
        tools_override=host_tools,
    )
    try:
        assert set(session.tools) == set(host_tools)
        assert _catalog(session) == ""
    finally:
        session.close()


def test_user_skill_folders_can_be_left_out(workspace: Path, isolated_home: Path) -> None:
    _skill(isolated_home, "mine", "my-own", None, skills=".config/agents/skills")

    def names(cfg: AppConfig) -> set[str]:
        return set(discover_skills(focus_path=workspace, workspace_root=workspace, cfg=cfg).skills)

    assert "my-own" in names(AppConfig(model="test-model"))
    assert "my-own" not in names(AppConfig(model="test-model", user_skills_enabled=False))
    assert NOTES in names(AppConfig(model="test-model", user_skills_enabled=False))


# The packaged skills: what each declares is a real tool, and Work offers every one.

_BUNDLED = resources.files("alysis_code.skills").joinpath("bundled")
# Tools a host adds to an ordinary chat when it can hand work to the person's own apps.
_HOST_START_TOOLS = {"computer_discover", "computer_start"}


def test_bundled_skills_declare_only_tools_the_engine_has() -> None:
    declared = {}
    for entry in _BUNDLED.iterdir():
        if entry.joinpath("SKILL.md").is_file():
            validation = validate_skill_bundle(Path(str(entry)))
            assert validation.valid, (entry.name, validation.errors)
            declared[validation.name] = validation.metadata.get("uses_tools") or []

    for name, tools in declared.items():
        for tool in tools:
            assert tool in _HOST_START_TOOLS or get_builtin_tool_metadata(tool), (name, tool)
    # The skills built on the desktop app's document and file tools say so.
    for name in ("documents", "spreadsheets", "presentations", "pdf"):
        assert {"document_operation", "artifact_check"} <= set(declared[name]), name
    assert set(declared["computer-use"]) == _HOST_START_TOOLS


@pytest.mark.parametrize("mode", ["auto", "review", "readonly"])
def test_work_session_offers_every_bundled_skill_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    # Computer-use needs a host that hands work to the person's apps; that host's own tests
    # cover it. Here the platform must not decide which skills a test sees.
    monkeypatch.setattr(
        "alysis_code.computer_use_platform_policy.computer_use_blocked", lambda: True
    )
    root = tmp_path / "work"
    root.mkdir()
    cfg = AppConfig(model="test-model")
    discovered = discover_skills(focus_path=root, workspace_root=root, cfg=cfg)
    session = _session(root, mode=mode, cfg=cfg)
    try:
        catalog = _catalog(session)
        offered = [skill.name for skill in session.skills_ordered]
    finally:
        session.close()

    assert offered == [skill.name for skill in discovered.ordered]
    assert {"documents", "spreadsheets", "presentations", "pdf", "local-files"} <= set(offered)
    assert catalog == build_skill_advertise_block(
        skills=discovered.ordered, skills_auto_invoke=True
    )
