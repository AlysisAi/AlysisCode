from __future__ import annotations

import io
from importlib import resources
from pathlib import Path
from typing import Any

from rich.console import Console

from alysis_code.agent_loop import build_tools, create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import SessionStore
from alysis_code.skills import (
    SkillBundle,
    build_explicit_skill_context_message,
    build_matched_skill_context,
    build_skill_advertise_block,
    discover_skills,
    load_repo_conventions,
    match_skills,
    read_skill_bundle_file,
    resolve_skill_by_name,
    validate_skill_bundle,
)
from alysis_code.skills.prompting import (
    EXPLICIT_SKILL_CONTEXT_TOTAL_MAX_CHARS,
    EXPLICIT_SKILL_ENTRYPOINT_MAX_CHARS,
)
from alysis_code.skills.validation import (
    SKILL_DESCRIPTION_WARNING_CHARS,
    SKILL_ENTRYPOINT_WARNING_CHARS,
    SKILL_NAME_WARNING_CHARS,
)

_BUNDLED_SKILL_NAMES = {
    "alysis-docs",
    "documents",
    "spreadsheets",
    "presentations",
    "pdf",
    "research",
    "address-pr-comments",
    "code-review",
    "commit",
    "computer-use",
    "image-pdf-files",
    "office-files",
    "local-files",
    "debug",
    "fix-ci",
    "release-notes",
    "security-review",
    "skill-creator",
}


def _write_skill(
    root: Path,
    rel_root: str,
    bundle_name: str,
    *,
    name: str,
    description: str,
    body: str,
    frontmatter_extra: str = "",
) -> Path:
    bundle = root / rel_root / bundle_name
    bundle.mkdir(parents=True, exist_ok=True)
    frontmatter = f"---\nname: {name}\ndescription: {description}\n{frontmatter_extra}---\n\n"
    (bundle / "SKILL.md").write_text(frontmatter + body, encoding="utf-8")
    return bundle


def _write_invalid_utf8_skill(root: Path, rel_root: str, bundle_name: str) -> Path:
    bundle = root / rel_root / bundle_name
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "SKILL.md").write_bytes(
        b"---\nname: broken\ndescription: broken\n---\n\n\xff\xfe\xfa\n"
    )
    return bundle


def _store(root: Path, *, enabled: bool = False) -> SessionStore:
    return SessionStore(
        enabled=enabled,
        sessions_dir=root / "sessions",
        session_id="skills-test",
        cwd=str(root),
        repo_root=str(root),
    )


class _CaptureClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = on_text_delta, temperature
        self.calls.append({"messages": list(messages), "tools": tools, "stream": stream})
        return LLMResponse(content="Done.", tool_calls=[], raw={})


class _SkillReadThenDoneCaptureClient(_CaptureClient):
    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = on_text_delta, temperature
        self.calls.append({"messages": list(messages), "tools": tools, "stream": stream})
        if len(self.calls) == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="skill-read-1",
                        name="skill_read",
                        arguments={"name": "pytest"},
                    )
                ],
                raw={},
            )
        return LLMResponse(content="Done.", tool_calls=[], raw={})


class _NoToolCaptureClient(_CaptureClient):
    supports_tool_calling = False


def test_discover_skills_respects_ancestor_and_path_family_precedence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    focus = repo / "apps" / "api"
    focus.mkdir(parents=True)

    _write_skill(
        repo,
        ".alysis_skills",
        "deploy",
        name="deploy",
        description="root native deploy",
        body="Use the root native deploy workflow.",
    )
    _write_skill(
        repo,
        ".agents/skills",
        "deploy",
        name="deploy",
        description="root agents deploy",
        body="Use the root agents deploy workflow.",
    )
    _write_skill(
        focus.parent,
        ".agents/skills",
        "deploy",
        name="deploy",
        description="nearer agents deploy",
        body="Use the nearer deploy workflow.",
    )
    _write_skill(
        focus.parent,
        ".claude/skills",
        "lint",
        name="lint",
        description="claude lint",
        body="Use the claude lint workflow.",
    )
    _write_skill(
        focus.parent,
        ".github/skills",
        "lint",
        name="lint",
        description="github lint",
        body="Use the github lint workflow.",
    )
    _write_skill(
        focus.parent,
        ".alysis_skills",
        "lint",
        name="lint",
        description="native lint",
        body="Use the native lint workflow.",
    )

    discovered = discover_skills(focus_path=focus, workspace_root=repo)

    deploy = resolve_skill_by_name(discovered.skills, "deploy")
    lint = resolve_skill_by_name(discovered.skills, "lint")

    assert deploy is not None
    assert deploy.description == "nearer agents deploy"
    assert deploy.source_scope == "project"
    assert deploy.source_family == ".agents/skills"
    assert deploy.ancestor_distance == 1

    assert lint is not None
    assert lint.description == "native lint"
    assert lint.source_family == ".alysis_skills"
    assert lint.ancestor_distance == 1


def test_discover_skills_project_beats_user_and_user_precedence_is_deterministic(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    user_cfg = tmp_path / "usercfg"
    home_dir = tmp_path / "home"
    user_cfg.mkdir()
    home_dir.mkdir()

    _write_skill(
        repo,
        ".github/skills",
        "shared",
        name="shared",
        description="project shared",
        body="Project shared instructions.",
    )
    _write_skill(
        user_cfg,
        "skills",
        "shared",
        name="shared",
        description="user native shared",
        body="User shared instructions.",
    )
    _write_skill(
        user_cfg,
        "skills",
        "useronly",
        name="useronly",
        description="user native",
        body="User native instructions.",
    )
    _write_skill(
        home_dir,
        ".config/agents/skills",
        "useronly",
        name="useronly",
        description="agents interop",
        body="Agents interop instructions.",
    )
    _write_skill(
        home_dir,
        ".claude/skills",
        "research",
        name="research",
        description="claude global",
        body="Claude global instructions.",
    )
    _write_skill(
        home_dir,
        ".copilot/skills",
        "research",
        name="research",
        description="copilot global",
        body="Copilot global instructions.",
    )

    discovered = discover_skills(
        focus_path=repo,
        workspace_root=repo,
        user_config_dir=user_cfg,
        home_dir=home_dir,
    )

    shared = resolve_skill_by_name(discovered.skills, "shared")
    useronly = resolve_skill_by_name(discovered.skills, "useronly")
    research = resolve_skill_by_name(discovered.skills, "research")

    assert shared is not None
    assert shared.description == "project shared"
    assert shared.source_scope == "project"

    assert useronly is not None
    assert useronly.description == "user native"
    assert useronly.source_scope == "user"
    assert useronly.source_family == "skills"

    assert research is not None
    assert research.description == "claude global"
    assert research.source_family == ".claude/skills"


def test_discover_skills_includes_bundled_pack_by_default(
    tmp_path: Path, mac_computer_use_protocol
) -> None:
    discovered = discover_skills(
        focus_path=tmp_path,
        workspace_root=tmp_path,
        user_config_dir=tmp_path / "empty-user-config",
        home_dir=tmp_path / "empty-home",
    )

    bundled = {skill.name: skill for skill in discovered.ordered if skill.source_scope == "bundled"}
    assert set(bundled) == _BUNDLED_SKILL_NAMES
    assert all(skill.source_kind == "bundled" for skill in bundled.values())


def test_skill_catalog_advertises_every_bundled_skill_beside_project_skills(
    tmp_path: Path, mac_computer_use_protocol
) -> None:
    for number in range(8):
        folder = tmp_path / ".alysis_skills" / f"project-{number}"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: project-{number}\ndescription: Project workflow {number}\n---\nInstructions.\n",
            encoding="utf-8",
        )
    discovered = discover_skills(
        focus_path=tmp_path,
        workspace_root=tmp_path,
        user_config_dir=tmp_path / "empty-user-config",
        home_dir=tmp_path / "empty-home",
    )

    catalog = build_skill_advertise_block(skills=discovered.ordered, skills_auto_invoke=True)

    assert catalog is not None
    assert "...(truncated)" not in catalog
    advertised = {
        line.split(" | ", 1)[0].removeprefix("- ") for line in catalog.splitlines() if " | " in line
    }
    assert advertised == {skill.name for skill in discovered.ordered}
    assert _BUNDLED_SKILL_NAMES | {f"project-{number}" for number in range(8)} <= advertised


def test_discover_skills_bundled_kill_switch_removes_exactly_the_pack(
    tmp_path: Path, mac_computer_use_protocol
) -> None:
    enabled = discover_skills(
        focus_path=tmp_path,
        workspace_root=tmp_path,
        user_config_dir=tmp_path / "empty-user-config",
        home_dir=tmp_path / "empty-home",
        cfg=AppConfig(model="test-model"),
    )
    disabled = discover_skills(
        focus_path=tmp_path,
        workspace_root=tmp_path,
        user_config_dir=tmp_path / "empty-user-config",
        home_dir=tmp_path / "empty-home",
        cfg=AppConfig(model="test-model", bundled_skills_enabled=False),
    )

    assert set(enabled.skills) - set(disabled.skills) == _BUNDLED_SKILL_NAMES
    assert not any(skill.source_scope == "bundled" for skill in disabled.ordered)


def test_project_skill_shadows_same_named_bundled_skill(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        ".alysis_skills",
        "code-review",
        name="code-review",
        description="Project-specific review workflow",
        body="Use the project review workflow.",
    )

    discovered = discover_skills(
        focus_path=tmp_path,
        workspace_root=tmp_path,
        user_config_dir=tmp_path / "empty-user-config",
        home_dir=tmp_path / "empty-home",
    )
    selected = resolve_skill_by_name(discovered.skills, "code-review")

    assert selected is not None
    assert selected.source_scope == "project"
    assert selected.description == "Project-specific review workflow"
    assert sum(skill.name == "code-review" for skill in discovered.ordered) == 1


def test_bundled_skill_entrypoints_are_packaged_resources() -> None:
    bundled_root = resources.files("alysis_code.skills").joinpath("bundled")

    packaged_names = {
        name for name in _BUNDLED_SKILL_NAMES if bundled_root.joinpath(name, "SKILL.md").is_file()
    }

    assert packaged_names == _BUNDLED_SKILL_NAMES


def test_bundled_skill_bodies_follow_pack_contract() -> None:
    bundled_root = resources.files("alysis_code.skills").joinpath("bundled")

    for name in sorted(_BUNDLED_SKILL_NAMES):
        entrypoint = bundled_root.joinpath(name, "SKILL.md")
        text = entrypoint.read_text(encoding="utf-8")
        validation = validate_skill_bundle(Path(str(entrypoint.parent)))

        assert validation.valid, (name, validation.errors)
        assert text.isascii(), name
        assert len(text.splitlines()) < 80, name
        assert "\nUse when:" in text, name
        assert "\nDo not use when:" in text, name
        assert len(validation.description) <= SKILL_DESCRIPTION_WARNING_CHARS, name

    debug = validate_skill_bundle(Path(str(bundled_root.joinpath("debug"))))
    assert debug.description == (
        "Diagnose, reproduce, fix, and verify a concrete runtime/test failure as a fallback when "
        "no narrower discovered workflow governs the request."
    )

    code_review = validate_skill_bundle(Path(str(bundled_root.joinpath("code-review"))))
    assert "no narrower review workflow is requested" in code_review.description

    address_pr_comments_text = bundled_root.joinpath("address-pr-comments", "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "created during the current task" in address_pr_comments_text
    assert "Preserve pre-existing and user-provided files" in address_pr_comments_text
    assert address_pr_comments_text.index(
        "Remove temporary reproduction artifacts"
    ) < address_pr_comments_text.index("final focused verification")

    fix_ci_entrypoint = bundled_root.joinpath("fix-ci", "SKILL.md")
    fix_ci_text = fix_ci_entrypoint.read_text(encoding="utf-8")
    fix_ci = validate_skill_bundle(Path(str(fix_ci_entrypoint.parent)))
    assert fix_ci.description.startswith("Use for a failing automated build or repository check")
    assert "run output/logs" in fix_ci.description
    assert "created during the current task" in fix_ci_text
    assert "Preserve pre-existing and user-provided files" in fix_ci_text
    assert fix_ci_text.index("Remove temporary reproduction artifacts") < fix_ci_text.index(
        "final authoritative rerun"
    )

    release_notes = validate_skill_bundle(Path(str(bundled_root.joinpath("release-notes"))))
    assert "not for conceptual explanations" in release_notes.description

    skill_creator_text = bundled_root.joinpath("skill-creator", "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "created during the current task" in skill_creator_text
    assert "preserve pre-existing and user-provided files" in skill_creator_text
    assert "clean those throwaways before final verification" in skill_creator_text


def test_discover_skills_skips_malformed_bundles_without_crashing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_skill(
        repo,
        ".alysis_skills",
        "good",
        name="good",
        description="valid skill",
        body="Valid instructions.",
    )
    broken = repo / ".alysis_skills" / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("name: broken\n", encoding="utf-8")

    discovered = discover_skills(focus_path=repo, workspace_root=repo)

    assert resolve_skill_by_name(discovered.skills, "good") is not None
    assert resolve_skill_by_name(discovered.skills, "broken") is None
    assert discovered.issues
    assert any("missing YAML-style frontmatter" in issue.message for issue in discovered.issues)


def test_discover_skills_skips_invalid_utf8_project_local_bundle_without_crashing(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_skill(
        repo,
        ".alysis_skills",
        "good",
        name="good",
        description="valid skill",
        body="Valid instructions.",
    )
    broken = _write_invalid_utf8_skill(repo, ".alysis_skills", "broken")

    discovered = discover_skills(focus_path=repo, workspace_root=repo)

    assert resolve_skill_by_name(discovered.skills, "good") is not None
    assert resolve_skill_by_name(discovered.skills, "broken") is None
    assert any(issue.source_path == broken for issue in discovered.issues)
    assert any("UTF-8" in issue.message for issue in discovered.issues)


def test_discover_skills_skips_invalid_utf8_user_global_bundle_without_crashing(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    user_cfg = tmp_path / "usercfg"
    home_dir = tmp_path / "home"
    user_cfg.mkdir()
    home_dir.mkdir()
    _write_skill(
        user_cfg,
        "skills",
        "good",
        name="good",
        description="valid user skill",
        body="Valid user instructions.",
    )
    broken = _write_invalid_utf8_skill(user_cfg, "skills", "broken")

    discovered = discover_skills(
        focus_path=repo,
        workspace_root=repo,
        user_config_dir=user_cfg,
        home_dir=home_dir,
    )

    assert resolve_skill_by_name(discovered.skills, "good") is not None
    assert resolve_skill_by_name(discovered.skills, "broken") is None
    assert any(issue.source_path == broken for issue in discovered.issues)
    assert any("UTF-8" in issue.message for issue in discovered.issues)


def test_conventions_loader_stays_separate_from_skills(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    nested = repo / "pkg" / "subpkg"
    nested.mkdir(parents=True)
    (repo / "AGENTS.md").write_text("Follow agent conventions.\n", encoding="utf-8")
    (repo / "CONVENTIONS.md").write_text("Follow repo conventions.\n", encoding="utf-8")
    _write_skill(
        repo,
        ".agents/skills",
        "python",
        name="python",
        description="python skill",
        body="Use Python skill instructions.",
    )

    conventions = load_repo_conventions(focus_path=nested, workspace_root=repo)
    discovered = discover_skills(focus_path=nested, workspace_root=repo)

    assert [doc.name for doc in conventions] == ["AGENTS.md", "CONVENTIONS.md"]
    assert resolve_skill_by_name(discovered.skills, "python") is not None
    assert all(doc.name.casefold() not in discovered.skills for doc in conventions)


def test_conventions_loader_skips_symlinks_outside_workspace(
    tmp_path: Path, require_symlinks
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside-agents.md"
    outside.write_text("Host-only instructions.\n", encoding="utf-8")
    (repo / "AGENTS.md").symlink_to(outside)
    (repo / "CONVENTIONS.md").write_text("Repository instructions.\n", encoding="utf-8")

    conventions = load_repo_conventions(focus_path=repo, workspace_root=repo)

    assert [doc.name for doc in conventions] == ["CONVENTIONS.md"]
    assert all("Host-only instructions." not in doc.content for doc in conventions)


def test_conventions_loader_allows_symlinks_within_workspace(
    tmp_path: Path, require_symlinks
) -> None:
    repo = tmp_path / "repo"
    rules_dir = repo / "docs"
    rules_dir.mkdir(parents=True)
    rules = rules_dir / "agent-rules.md"
    rules.write_text("Shared repository instructions.\n", encoding="utf-8")
    link = repo / "AGENTS.md"
    link.symlink_to(rules)

    conventions = load_repo_conventions(focus_path=repo, workspace_root=repo)

    assert len(conventions) == 1
    assert conventions[0].name == "AGENTS.md"
    assert conventions[0].path == link
    assert conventions[0].content == "Shared repository instructions."


def test_match_skills_returns_obvious_positive_and_negative_cases(tmp_path: Path) -> None:
    python_skill = SkillBundle(
        name="python-debug",
        description="Debug Python stack traces and failing tests",
        instructions="Read stack traces and fix tests.",
        bundle_name="python-debug",
        bundle_path=tmp_path / "python-debug",
        entry_path=tmp_path / "python-debug" / "SKILL.md",
        source_scope="project",
        source_kind="native",
        source_family=".alysis_skills",
        source_path=tmp_path / "python-debug",
        trust_level="untrusted",
    )
    docker_skill = SkillBundle(
        name="docker-compose",
        description="Work with Docker Compose services",
        instructions="Inspect docker services.",
        bundle_name="docker-compose",
        bundle_path=tmp_path / "docker-compose",
        entry_path=tmp_path / "docker-compose" / "SKILL.md",
        source_scope="project",
        source_kind="native",
        source_family=".alysis_skills",
        source_path=tmp_path / "docker-compose",
        trust_level="untrusted",
    )

    matches = match_skills(
        "Please debug this Python test failure and stack trace.",
        skills=[python_skill, docker_skill],
    )
    negative = match_skills("Tell me a joke about Athens.", skills=[python_skill, docker_skill])

    assert matches
    assert matches[0].skill.name == "python-debug"
    assert not negative


def test_discovered_skill_source_path_refers_to_bundle_directory(tmp_path: Path) -> None:
    bundle = _write_skill(
        tmp_path,
        ".alysis_skills",
        "python",
        name="python",
        description="python skill",
        body="Use focused Python instructions.",
    )

    discovered = discover_skills(focus_path=tmp_path, workspace_root=tmp_path)
    skill = resolve_skill_by_name(discovered.skills, "python")

    assert skill is not None
    assert skill.source_path == bundle
    assert skill.entry_path == bundle / "SKILL.md"


def test_skill_read_entrypoint_nested_path_and_path_traversal_protection(tmp_path: Path) -> None:
    bundle = _write_skill(
        tmp_path,
        ".alysis_skills",
        "python",
        name="python",
        description="python skill",
        body="Use focused Python instructions.",
        frontmatter_extra="owner: platform\n",
    )
    (bundle / "references").mkdir()
    (bundle / "references" / "guide.txt").write_text("Guide text\n", encoding="utf-8")
    discovered = discover_skills(focus_path=tmp_path, workspace_root=tmp_path)
    skill = resolve_skill_by_name(discovered.skills, "python")

    assert skill is not None
    entry = read_skill_bundle_file(skill)
    guide = read_skill_bundle_file(skill, "references/guide.txt")

    assert entry["path"] == "SKILL.md"
    assert "Use focused Python instructions." in entry["content"]
    assert guide["path"] == "references/guide.txt"
    assert guide["content"] == "Guide text\n"

    try:
        read_skill_bundle_file(skill, "../secrets.txt")
    except Exception as exc:  # noqa: BLE001
        assert "escapes the skill bundle root" in str(exc)
    else:
        raise AssertionError("expected traversal read to fail")


def test_skill_advertise_and_match_blocks_stay_bounded(tmp_path: Path) -> None:
    skills = [
        SkillBundle(
            name=f"skill-{idx}",
            description=("Useful " * 50) + str(idx),
            instructions="Do the thing.",
            bundle_name=f"skill-{idx}",
            bundle_path=tmp_path / f"skill-{idx}",
            entry_path=tmp_path / f"skill-{idx}" / "SKILL.md",
            source_scope="project",
            source_kind="native",
            source_family=".alysis_skills",
            source_path=tmp_path / f"skill-{idx}",
            trust_level="untrusted",
        )
        for idx in range(8)
    ]
    advertise = build_skill_advertise_block(
        skills=skills,
        skills_auto_invoke=True,
        max_chars=260,
        max_items=4,
    )
    matches = build_matched_skill_context(
        matches=match_skills("Need skill-1 and skill-2", skills=skills),
        max_chars=220,
        max_items=2,
    )

    assert advertise is not None
    assert matches is not None
    assert len(advertise) <= 260
    assert len(matches) <= 220
    assert "...(truncated)" in advertise


def test_skill_advertise_block_distinguishes_auto_activation_from_manual_access(
    tmp_path: Path,
) -> None:
    skill = SkillBundle(
        name="pytest",
        description="Debug pytest failures and stack traces",
        instructions="Use pytest-focused debugging instructions.",
        bundle_name="pytest",
        bundle_path=tmp_path / "pytest",
        entry_path=tmp_path / "pytest" / "SKILL.md",
        source_scope="project",
        source_kind="native",
        source_family=".alysis_skills",
        source_path=tmp_path / "pytest",
        trust_level="untrusted",
    )

    automatic = build_skill_advertise_block(skills=[skill], skills_auto_invoke=True)
    manual = build_skill_advertise_block(skills=[skill], skills_auto_invoke=False)

    assert automatic is not None
    assert manual is not None
    assert "Compare all descriptions" in automatic
    assert "requested outcome and workflow" in automatic
    assert "not shared steps or concept mentions" in automatic
    assert "honor exclusions" in automatic
    assert "narrowest fit" in automatic
    assert "broad skills are fallbacks" in automatic
    assert "Read a chosen workflow with skill_read(name)" in automatic
    assert "Otherwise continue directly" in automatic
    assert "skill_read only loads instructions; it never executes them" in automatic
    assert "Use skill_read(name, path)" in automatic
    assert "optional attachable context" not in automatic
    assert "Skills are optional attachable context" in manual
    assert "Compare all descriptions" not in manual


def test_build_explicit_skill_context_substitutes_argument_placeholders() -> None:
    skill = SkillBundle(
        name="interop",
        description="Interop skill",
        instructions='Use "$ARGUMENTS" with $1 and $2. Missing: "$4".',
        bundle_name="interop",
        bundle_path=Path("/tmp/interop"),
        entry_path=Path("/tmp/interop/SKILL.md"),
        source_scope="project",
        source_kind="interop",
        source_family=".claude/skills",
        source_path=Path("/tmp/interop"),
        trust_level="untrusted",
    )

    wrapped = build_explicit_skill_context_message(
        skill=skill,
        task_text='fix parser "quoted arg"',
    )

    assert "<explicit_skill_context>" in wrapped
    assert (
        "turn_requirement: Apply this selected skill before taking other actions on the next user task."
        in wrapped
    )
    assert (
        "task_binding: Treat this wrapper and the next user message as one bound instruction set."
        in wrapped
    )
    assert 'Use "fix parser "quoted arg"" with fix and parser.' in wrapped
    assert 'Missing: "".' in wrapped
    assert '- $ARGUMENTS = "fix parser \\"quoted arg\\""' in wrapped
    assert '- $1 = "fix"' in wrapped
    assert '- $2 = "parser"' in wrapped
    assert '- $4 = ""' in wrapped
    assert '- $3 = "quoted arg"' not in wrapped


def test_build_explicit_skill_context_empty_arguments_are_deterministic() -> None:
    skill = SkillBundle(
        name="interop",
        description="Interop skill",
        instructions='ARGS="$ARGUMENTS" FIRST="$1" SECOND="$2"',
        bundle_name="interop",
        bundle_path=Path("/tmp/interop"),
        entry_path=Path("/tmp/interop/SKILL.md"),
        source_scope="project",
        source_kind="interop",
        source_family=".claude/skills",
        source_path=Path("/tmp/interop"),
        trust_level="untrusted",
    )

    wrapped = build_explicit_skill_context_message(skill=skill)

    assert 'ARGS="" FIRST="" SECOND=""' in wrapped
    assert '- $ARGUMENTS = ""' in wrapped
    assert '- $1 = ""' in wrapped
    assert '- $2 = ""' in wrapped


def test_build_explicit_skill_context_falls_back_when_shlex_parsing_fails() -> None:
    skill = SkillBundle(
        name="interop",
        description="Interop skill",
        instructions='RAW="$ARGUMENTS" FIRST="$1" SECOND="$2"',
        bundle_name="interop",
        bundle_path=Path("/tmp/interop"),
        entry_path=Path("/tmp/interop/SKILL.md"),
        source_scope="project",
        source_kind="interop",
        source_family=".claude/skills",
        source_path=Path("/tmp/interop"),
        trust_level="untrusted",
    )

    wrapped = build_explicit_skill_context_message(
        skill=skill,
        task_text='broken "quoted value',
    )

    assert "- parser = whitespace_split_fallback" in wrapped
    assert 'RAW="broken "quoted value" FIRST="broken" SECOND=""quoted"' in wrapped


def test_build_explicit_skill_context_adds_truncation_notice_for_large_entrypoint() -> None:
    skill = SkillBundle(
        name="oversized",
        description="Large skill",
        instructions=("line\n" * 3_500) + "tail-marker",
        bundle_name="oversized",
        bundle_path=Path("/tmp/oversized"),
        entry_path=Path("/tmp/oversized/SKILL.md"),
        source_scope="project",
        source_kind="interop",
        source_family=".claude/skills",
        source_path=Path("/tmp/oversized"),
        trust_level="untrusted",
    )

    wrapped = build_explicit_skill_context_message(skill=skill, task_text="run audit")

    assert "entrypoint_notice:" in wrapped
    assert "Attached entrypoint preview was truncated" in wrapped
    assert "The direct user task remains available in the next user message." in wrapped
    assert "Use skill_read(name[, path])" in wrapped
    assert "...(truncated)" in wrapped
    assert "tail-marker" not in wrapped
    assert "argument_substitution:" not in wrapped


def test_build_explicit_skill_context_total_budget_bounds_large_placeholder_task() -> None:
    skill = SkillBundle(
        name="interop",
        description="Interop skill",
        instructions=("Task copy: $ARGUMENTS\n" * 32) + "First: $1\nSecond: $2\n",
        bundle_name="interop",
        bundle_path=Path("/tmp/interop"),
        entry_path=Path("/tmp/interop/SKILL.md"),
        source_scope="project",
        source_kind="interop",
        source_family=".claude/skills",
        source_path=Path("/tmp/interop"),
        trust_level="untrusted",
    )

    wrapped = build_explicit_skill_context_message(
        skill=skill,
        task_text="δοκιμή " * 4_000,
    )

    assert len(wrapped) <= EXPLICIT_SKILL_CONTEXT_TOTAL_MAX_CHARS
    assert "entrypoint_notice:" in wrapped
    assert "argument_substitution was reduced" in wrapped
    assert "The direct user task remains available in the next user message." in wrapped
    assert "Use skill_read(name[, path])" in wrapped
    assert "argument_substitution:" in wrapped


def test_build_explicit_skill_context_omits_argument_block_without_placeholders_even_for_large_task() -> (
    None
):
    skill = SkillBundle(
        name="interop",
        description="Interop skill",
        instructions="Review the reusable workflow and proceed cautiously.",
        bundle_name="interop",
        bundle_path=Path("/tmp/interop"),
        entry_path=Path("/tmp/interop/SKILL.md"),
        source_scope="project",
        source_kind="interop",
        source_family=".claude/skills",
        source_path=Path("/tmp/interop"),
        trust_level="untrusted",
    )

    wrapped = build_explicit_skill_context_message(
        skill=skill,
        task_text="large task " * 5_000,
    )

    assert len(wrapped) <= EXPLICIT_SKILL_CONTEXT_TOTAL_MAX_CHARS
    assert "argument_substitution:" not in wrapped
    assert "entrypoint_notice:" not in wrapped
    assert "Review the reusable workflow and proceed cautiously." in wrapped


def test_build_explicit_skill_context_keeps_non_ascii_arguments_readable() -> None:
    skill = SkillBundle(
        name="interop",
        description="Interop skill",
        instructions='ARGS="$ARGUMENTS" FIRST="$1"',
        bundle_name="interop",
        bundle_path=Path("/tmp/interop"),
        entry_path=Path("/tmp/interop/SKILL.md"),
        source_scope="project",
        source_kind="interop",
        source_family=".claude/skills",
        source_path=Path("/tmp/interop"),
        trust_level="untrusted",
    )

    wrapped = build_explicit_skill_context_message(
        skill=skill,
        task_text="δοκιμή εργαλείου",
    )

    assert '"δοκιμή εργαλείου"' in wrapped
    assert 'FIRST="δοκιμή"' in wrapped
    assert "\\u03b4" not in wrapped


def test_build_explicit_skill_context_truncates_oversized_metadata_but_keeps_structure() -> None:
    skill = SkillBundle(
        name="n" * 2_000,
        description="d" * 3_000,
        instructions='Inspect "$ARGUMENTS" and continue.',
        bundle_name="interop",
        bundle_path=Path("/tmp/interop"),
        entry_path=Path("/tmp/interop/SKILL.md"),
        source_scope="project",
        source_kind="interop",
        source_family=".claude/skills",
        source_path=Path("/tmp/" + ("deep/" * 100) + "interop"),
        trust_level="untrusted",
    )

    wrapped = build_explicit_skill_context_message(
        skill=skill,
        task_text="run audit",
    )

    assert len(wrapped) <= EXPLICIT_SKILL_CONTEXT_TOTAL_MAX_CHARS
    assert wrapped.startswith("<explicit_skill_context>\n")
    assert wrapped.endswith("</explicit_skill_context>\n")
    assert "<skill_instructions>\n" in wrapped
    assert "\n</skill_instructions>\n</explicit_skill_context>\n" in wrapped
    assert "name: " in wrapped and "...(truncated)" in wrapped
    assert "description: " in wrapped
    assert 'Inspect "run audit" and continue.' in wrapped


def test_validate_skill_bundle_warns_on_oversized_metadata_fields(tmp_path: Path) -> None:
    bundle = _write_skill(
        tmp_path,
        ".alysis_skills",
        "metadata-heavy",
        name="n" * (SKILL_NAME_WARNING_CHARS + 10),
        description="d" * (SKILL_DESCRIPTION_WARNING_CHARS + 10),
        body="Use the metadata-heavy skill carefully.\n",
    )

    result = validate_skill_bundle(bundle)

    assert result.valid is True
    assert any(
        issue.severity == "warning" and "Skill name is unusually large" in issue.message
        for issue in result.issues
    )
    assert any(
        issue.severity == "warning" and "Skill description is unusually large" in issue.message
        for issue in result.issues
    )


def test_create_session_adds_skill_advertise_and_separate_repo_conventions(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("Agent conventions live here.\n", encoding="utf-8")
    _write_skill(
        tmp_path,
        ".alysis_skills",
        "python",
        name="python",
        description="Work on Python code",
        body="Deep Python instructions that should not be fully advertised by default.",
    )
    session = create_session(
        cfg=AppConfig(model="test-model", web_search_mode="off"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    try:
        contents = [
            str(message.get("content") or "")
            for message in session.messages
            if str(message.get("role") or "") == "user"
        ]
    finally:
        session.close()

    skill_blocks = [content for content in contents if "<skill_context>" in content]
    convention_blocks = [content for content in contents if "<repo_conventions>" in content]

    assert skill_blocks
    assert convention_blocks
    assert "Work on Python code" in skill_blocks[0]
    assert "Compare all descriptions" in skill_blocks[0]
    assert "requested outcome and workflow" in skill_blocks[0]
    assert "broad skills are fallbacks" in skill_blocks[0]
    assert "Read a chosen workflow with skill_read(name)" in skill_blocks[0]
    assert "optional attachable context" not in skill_blocks[0]
    assert "Deep Python instructions" not in skill_blocks[0]
    assert "AGENTS.md" in convention_blocks[0]


def test_create_session_omits_skill_context_and_skill_read_when_skills_disabled(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        ".alysis_skills",
        "python",
        name="python",
        description="Work on Python code",
        body="Python instructions.",
    )
    session = create_session(
        cfg=AppConfig(model="test-model", web_search_mode="off", skills_enabled=False),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    try:
        contents = [
            str(message.get("content") or "")
            for message in session.messages
            if str(message.get("role") or "") == "user"
        ]
        tool_names = set(session.tools)
    finally:
        session.close()

    assert not any("<skill_context>" in content for content in contents)
    assert "skill_read" not in tool_names


def test_skill_read_tool_reads_skill_bundle_and_rejects_path_escape(tmp_path: Path) -> None:
    bundle = _write_skill(
        tmp_path,
        ".alysis_skills",
        "python",
        name="python",
        description="Work on Python code",
        body="Python instructions.",
    )
    (bundle / "references").mkdir()
    (bundle / "references" / "notes.txt").write_text("note\n", encoding="utf-8")
    discovered = discover_skills(focus_path=tmp_path, workspace_root=tmp_path)
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO(), force_terminal=False),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        cfg=AppConfig(model="test-model"),
        non_interactive=True,
        skills_enabled=True,
        skill_registry=discovered.skills,
    )

    entry = tools["skill_read"].run({"name": "python"})
    nested = tools["skill_read"].run({"name": "python", "path": "references/notes.txt"})
    escaped = tools["skill_read"].run({"name": "python", "path": "../secrets.txt"})

    assert entry["path"] == "SKILL.md"
    assert nested["path"] == "references/notes.txt"
    assert escaped["error"] == "skill_read path escapes the skill bundle root"


def test_run_turn_explicit_skill_context_is_request_only(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        ".alysis_skills",
        "python",
        name="python",
        description="Work on Python code",
        body="Python instructions.",
    )
    discovered = discover_skills(focus_path=tmp_path, workspace_root=tmp_path)
    skill = resolve_skill_by_name(discovered.skills, "python")
    assert skill is not None

    session = create_session(
        cfg=AppConfig(model="test-model", web_search_mode="off", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    client = _CaptureClient()
    session.client = client  # type: ignore[assignment]
    try:
        exit_code = session.run_turn(
            "Explain the Python workflow.",
            ephemeral_user_messages=[
                build_explicit_skill_context_message(
                    skill=skill,
                    task_text='explain "python workflow"',
                )
            ],
        )
        persisted_messages = list(session.messages)
    finally:
        session.close()

    assert exit_code == 0
    request_messages = client.calls[0]["messages"]
    explicit_indexes = [
        idx
        for idx, message in enumerate(request_messages)
        if "<explicit_skill_context>" in str(message.get("content") or "")
    ]
    assert explicit_indexes
    assert not any(
        "<explicit_skill_context>" in str(message.get("content") or "")
        for message in persisted_messages
    )
    explicit_message = str(request_messages[explicit_indexes[0]].get("content") or "")
    assert "argument_substitution:" not in explicit_message
    assert any(
        str(message.get("role") or "") == "user"
        and str(message.get("content") or "") == "Explain the Python workflow."
        for message in request_messages
    )
    assert not any(
        "Compare skills to the requested workflow" in str(message.get("content") or "")
        for message in request_messages
    )


def test_run_turn_does_not_auto_attach_matched_skill_context_when_auto_invoke_disabled(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        ".alysis_skills",
        "pytest",
        name="pytest",
        description="Debug pytest failures and stack traces",
        body="Use pytest-focused debugging instructions.",
    )
    session = create_session(
        cfg=AppConfig(
            model="test-model",
            web_search_mode="off",
            routing_mode="code_only",
            skills_enabled=True,
            skills_auto_invoke=False,
        ),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    client = _CaptureClient()
    session.client = client  # type: ignore[assignment]
    try:
        exit_code = session.run_turn("Debug the pytest failure in parser.py.")
    finally:
        session.close()

    assert exit_code == 0
    assert not any(
        "<matched_skill_context>" in str(message.get("content") or "")
        for message in client.calls[0]["messages"]
    )
    assert not any(
        "Compare skills to the requested workflow" in str(message.get("content") or "")
        for message in client.calls[0]["messages"]
    )


def test_run_turn_uses_default_auto_invoke_without_host_matched_context(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        ".alysis_skills",
        "pytest",
        name="pytest",
        description="Debug pytest failures and stack traces",
        body="Use pytest-focused debugging instructions.",
    )
    session = create_session(
        cfg=AppConfig(
            model="test-model",
            web_search_mode="off",
            routing_mode="code_only",
            skills_enabled=True,
        ),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=True,
        api_key_override="override-key",
    )
    client = _SkillReadThenDoneCaptureClient()
    session.client = client  # type: ignore[assignment]
    try:
        exit_code = session.run_turn("Debug the pytest failure in parser.py.")
    finally:
        session.close()

    assert exit_code == 0
    assert session.skills_auto_invoke is True
    assert len(client.calls) == 2
    first_request = client.calls[0]["messages"]
    second_request = client.calls[1]["messages"]
    skill_context_index = next(
        index
        for index, message in enumerate(first_request)
        if str(message.get("content") or "").lstrip().startswith("<skill_context>")
    )
    task_index = next(
        index
        for index, message in enumerate(first_request)
        if str(message.get("content") or "") == "Debug the pytest failure in parser.py."
    )
    assert skill_context_index < task_index
    catalog = str(first_request[skill_context_index].get("content") or "")
    assert "Compare all descriptions with the requested outcome and workflow" in catalog
    assert "Honor explicit user skill requests" in catalog
    assert "Read a chosen workflow with skill_read(name)" in catalog
    assert "unless its instructions are already in context" in catalog
    assert "Otherwise continue directly" in catalog
    assert first_request[skill_context_index] == second_request[skill_context_index]
    # Skill selection lives in the stable catalog, with no extra turn reminder.
    assert not any(
        "Compare skills to the requested workflow" in str(message.get("content") or "")
        for message in first_request
    )
    assert not any(
        "<matched_skill_context>" in str(message.get("content") or "") for message in first_request
    )
    skill_read_schema = next(
        tool
        for tool in client.calls[0]["tools"] or []
        if str(tool.get("function", {}).get("name") or "") == "skill_read"
    )
    skill_read_description = str(skill_read_schema["function"].get("description") or "")
    assert "With automatic skill selection" in skill_read_description
    assert "requested actions" in skill_read_description
    assert "concept mentions" in skill_read_description
    assert "most specific" in skill_read_description
    assert "before relying on" in skill_read_description
    assert "already in context" in skill_read_description
    assert "before any other task tool" not in skill_read_description


def test_run_turn_does_not_auto_attach_matched_skill_context_when_skills_disabled(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        ".alysis_skills",
        "pytest",
        name="pytest",
        description="Debug pytest failures and stack traces",
        body="Use pytest-focused debugging instructions.",
    )
    session = create_session(
        cfg=AppConfig(
            model="test-model",
            web_search_mode="off",
            routing_mode="code_only",
            skills_enabled=False,
            skills_auto_invoke=True,
        ),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    client = _CaptureClient()
    session.client = client  # type: ignore[assignment]
    try:
        exit_code = session.run_turn("Debug the pytest failure in parser.py.")
    finally:
        session.close()

    assert exit_code == 0
    assert not any(
        "<matched_skill_context>" in str(message.get("content") or "")
        for message in client.calls[0]["messages"]
    )
    assert not any(
        "Compare skills to the requested workflow" in str(message.get("content") or "")
        for message in client.calls[0]["messages"]
    )


def test_run_turn_omits_auto_skill_reminder_when_provider_cannot_call_tools(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        ".alysis_skills",
        "pytest",
        name="pytest",
        description="Debug pytest failures and stack traces",
        body="Use pytest-focused debugging instructions.",
    )
    session = create_session(
        cfg=AppConfig(
            model="test-model",
            web_search_mode="off",
            routing_mode="code_only",
            skills_enabled=True,
            skills_auto_invoke=True,
        ),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    client = _NoToolCaptureClient()
    session.client = client  # type: ignore[assignment]
    try:
        exit_code = session.run_turn("Debug the pytest failure in parser.py.")
    finally:
        session.close()

    assert exit_code == 0
    assert client.calls[0]["tools"] is None
    assert not any(
        "Compare skills to the requested workflow" in str(message.get("content") or "")
        for message in client.calls[0]["messages"]
    )


def test_create_session_skips_invalid_utf8_skills_without_failing_startup(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        ".alysis_skills",
        "good",
        name="good",
        description="valid skill",
        body="Valid instructions.",
    )
    broken = _write_invalid_utf8_skill(tmp_path, ".alysis_skills", "broken")

    session = create_session(
        cfg=AppConfig(model="test-model", web_search_mode="off"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    try:
        issue_paths = {issue.source_path for issue in session.skill_discovery_issues}
        assert resolve_skill_by_name(session.skill_registry, "good") is not None
        assert resolve_skill_by_name(session.skill_registry, "broken") is None
        assert broken in issue_paths
    finally:
        session.close()


def test_validate_skill_bundle_warns_on_oversized_entrypoint(tmp_path: Path) -> None:
    bundle = _write_skill(
        tmp_path,
        ".alysis_skills",
        "oversized",
        name="oversized",
        description="large skill",
        body=("line\n" * 5_500),
    )

    result = validate_skill_bundle(bundle)

    assert result.valid is True
    assert SKILL_ENTRYPOINT_WARNING_CHARS == EXPLICIT_SKILL_ENTRYPOINT_MAX_CHARS
    assert any(
        issue.severity == "warning"
        and "SKILL.md entrypoint is unusually large" in issue.message
        and "10,000-char wrapper" in issue.message
        for issue in result.issues
    )
