from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from alysis_code.agent_loop import create_session
from alysis_code.atomic_io import atomic_write_json as _real_atomic_write_json
from alysis_code.config import AppConfig
from alysis_code.skills import (
    SkillLifecycleState,
    discover_skills,
    install_skill_bundle,
    remove_managed_skill,
    resolve_skill_catalog,
    save_global_skill_state,
    scaffold_skill_bundle,
    set_global_skill_disabled,
    set_project_skill_override,
    validate_skill_bundle,
)
from alysis_code.subagents import load_subagent_registry


def _bundled_skill_names() -> set[str]:
    """Every shipped bundle that discovery serves on this platform."""
    from alysis_code.computer_use_platform_policy import computer_use_blocked
    from alysis_code.skills.paths import bundled_skill_root

    names = {p.name for p in bundled_skill_root().iterdir() if (p / "SKILL.md").is_file()}
    if computer_use_blocked():
        names.discard("computer-use")
    return names


def _write_skill_bundle(
    bundle: Path,
    *,
    name: str,
    description: str,
    body: str = "Use the skill.\n",
) -> Path:
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}",
        encoding="utf-8",
    )
    return bundle


def _patch_user_config_dir(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setattr(
        "alysis_code.skills.paths.canonical_user_config_dir",
        lambda: path,
    )
    monkeypatch.setattr(
        "alysis_code.skills.discovery.canonical_user_config_dir",
        lambda: path,
    )


def test_scaffold_skill_bundle_selects_native_and_portable_project_roots(tmp_path: Path) -> None:
    native = scaffold_skill_bundle(
        name="Pytest Debug",
        description="Debug pytest failures.",
        workspace_root=tmp_path,
        project=True,
    )
    portable = scaffold_skill_bundle(
        name="Docs Consistency",
        description="Keep docs aligned.",
        workspace_root=tmp_path,
        project=True,
        family="agents",
    )

    assert native.bundle_path == tmp_path / ".alysis_skills" / "pytest-debug"
    assert native.managed is True
    assert portable.bundle_path == tmp_path / ".agents" / "skills" / "docs-consistency"
    assert portable.managed is False
    assert (native.bundle_path / "references").is_dir()
    assert "name: Pytest Debug" in (native.bundle_path / "SKILL.md").read_text(encoding="utf-8")


def test_scaffold_global_native_records_managed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_cfg = tmp_path / "usercfg"
    _patch_user_config_dir(monkeypatch, user_cfg)

    result = scaffold_skill_bundle(
        name="Architecture Review",
        description="Review design changes.",
        workspace_root=tmp_path,
        project=False,
    )

    state_path = user_cfg / "skills.json"
    state_text = state_path.read_text(encoding="utf-8")
    assert result.bundle_path == user_cfg / "skills" / "architecture-review"
    assert '"source_kind": "scaffold"' in state_text
    assert '"architecture review"' in state_text.casefold()


def test_scaffold_project_managed_fails_before_mutation_on_malformed_state(tmp_path: Path) -> None:
    state_path = tmp_path / ".alysis" / "skills.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{broken json", encoding="utf-8")

    with pytest.raises(Exception, match="Invalid JSON"):
        scaffold_skill_bundle(
            name="Pytest Debug",
            description="Debug pytest failures.",
            workspace_root=tmp_path,
            project=True,
        )

    assert not (tmp_path / ".alysis_skills" / "pytest-debug").exists()


def test_scaffold_global_managed_fails_before_mutation_on_malformed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_cfg = tmp_path / "usercfg"
    _patch_user_config_dir(monkeypatch, user_cfg)
    user_cfg.mkdir(parents=True, exist_ok=True)
    (user_cfg / "skills.json").write_text("{broken json", encoding="utf-8")

    with pytest.raises(Exception, match="Invalid JSON"):
        scaffold_skill_bundle(
            name="Architecture Review",
            description="Review design changes.",
            workspace_root=tmp_path,
            project=False,
            user_config_dir=user_cfg,
        )

    assert not (user_cfg / "skills" / "architecture-review").exists()


def test_validate_skill_bundle_accepts_valid_bundle_and_rejects_invalid_utf8(
    tmp_path: Path,
) -> None:
    valid_bundle = _write_skill_bundle(
        tmp_path / "valid",
        name="verification-playbook",
        description="Recommend verification commands.",
        body="Run the narrowest verification that proves the change.\n",
    )
    invalid_bundle = tmp_path / "broken"
    invalid_bundle.mkdir(parents=True)
    (invalid_bundle / "SKILL.md").write_bytes(
        b"---\nname: broken\ndescription: broken\n---\n\n\xff\xfe\xfa"
    )

    valid = validate_skill_bundle(valid_bundle)
    broken = validate_skill_bundle(invalid_bundle)

    assert valid.valid is True
    assert valid.name == "verification-playbook"
    assert broken.valid is False
    assert any("UTF-8" in issue.message for issue in broken.issues)


def test_inaccessible_skill_bundle_is_reported_without_breaking_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    bundle = _write_skill_bundle(
        workspace / ".alysis_skills" / "blocked",
        name="blocked",
        description="A bundle whose metadata cannot be inspected.",
    )
    blocked_entry = (bundle / "SKILL.md").resolve()
    original_is_symlink = Path.is_symlink

    def _permission_denied_for_entry(path: Path) -> bool:
        if path == blocked_entry:
            raise PermissionError("access denied by test")
        return original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", _permission_denied_for_entry)

    validation = validate_skill_bundle(bundle)
    discovered = discover_skills(
        focus_path=workspace,
        workspace_root=workspace,
        user_config_dir=tmp_path / "empty-user-config",
        home_dir=tmp_path / "empty-home",
    )

    assert validation.valid is False
    assert len(validation.errors) == 1
    assert "Failed to inspect skill bundle" in validation.errors[0].message
    assert "blocked" not in discovered.skills
    assert set(discovered.skills) == _bundled_skill_names()
    assert all(skill.source_scope == "bundled" for skill in discovered.ordered)
    assert len(discovered.issues) == 1
    assert "Failed to inspect skill bundle" in discovered.issues[0].message


def test_install_skill_bundle_from_local_dir_records_managed_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_cfg = tmp_path / "usercfg"
    _patch_user_config_dir(monkeypatch, user_cfg)
    source_bundle = _write_skill_bundle(
        tmp_path / "source-skill",
        name="docs-consistency",
        description="Keep docs aligned.",
    )

    result = install_skill_bundle(
        source=str(source_bundle),
        workspace_root=tmp_path,
        project=False,
    )

    installed_skill = user_cfg / "skills" / "docs-consistency" / "SKILL.md"
    state_text = (user_cfg / "skills.json").read_text(encoding="utf-8")
    assert result.bundle_path == installed_skill.parent
    assert installed_skill.exists()
    assert '"source_kind": "dir"' in state_text


def test_install_skill_bundle_project_managed_fails_before_mutation_on_malformed_state(
    tmp_path: Path,
) -> None:
    source_bundle = _write_skill_bundle(
        tmp_path / "source-skill",
        name="docs-consistency",
        description="Keep docs aligned.",
    )
    state_path = tmp_path / ".alysis" / "skills.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{broken json", encoding="utf-8")

    with pytest.raises(Exception, match="Invalid JSON"):
        install_skill_bundle(
            source=str(source_bundle),
            workspace_root=tmp_path,
            project=True,
        )

    assert not (tmp_path / ".alysis_skills" / "docs-consistency").exists()


def test_install_skill_bundle_global_managed_fails_before_mutation_on_malformed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_cfg = tmp_path / "usercfg"
    _patch_user_config_dir(monkeypatch, user_cfg)
    source_bundle = _write_skill_bundle(
        tmp_path / "source-skill",
        name="docs-consistency",
        description="Keep docs aligned.",
    )
    user_cfg.mkdir(parents=True, exist_ok=True)
    (user_cfg / "skills.json").write_text("{broken json", encoding="utf-8")

    with pytest.raises(Exception, match="Invalid JSON"):
        install_skill_bundle(
            source=str(source_bundle),
            workspace_root=tmp_path,
            project=False,
            user_config_dir=user_cfg,
        )

    assert not (user_cfg / "skills" / "docs-consistency").exists()


def test_install_skill_bundle_from_zip_rejects_path_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_cfg = tmp_path / "usercfg"
    _patch_user_config_dir(monkeypatch, user_cfg)
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.txt", "nope")
        zf.writestr("bundle/SKILL.md", "---\nname: bad\ndescription: bad\n---\n\nbad\n")

    with pytest.raises(Exception, match="escapes extraction root"):
        install_skill_bundle(
            source=str(archive),
            workspace_root=tmp_path,
            project=False,
        )


def test_install_skill_bundle_from_zip_succeeds_for_normal_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_cfg = tmp_path / "usercfg"
    _patch_user_config_dir(monkeypatch, user_cfg)
    archive = tmp_path / "good.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "bundle/SKILL.md",
            "---\nname: docs-consistency\ndescription: Keep docs aligned.\n---\n\nUse the skill.\n",
        )
        zf.writestr("bundle/references/checklist.md", "Keep README and docs aligned.\n")

    result = install_skill_bundle(
        source=str(archive),
        workspace_root=tmp_path,
        project=False,
    )

    assert result.source_kind == "zip"
    assert (user_cfg / "skills" / "docs-consistency" / "SKILL.md").exists()


def test_install_skill_bundle_from_zip_rejects_symlink_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_cfg = tmp_path / "usercfg"
    _patch_user_config_dir(monkeypatch, user_cfg)
    archive = tmp_path / "symlink.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("bundle/SKILL.md", "---\nname: bad\ndescription: bad\n---\n\nbad\n")
        info = zipfile.ZipInfo("bundle/scripts/link.sh")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        zf.writestr(info, "scripts/run.sh")

    with pytest.raises(Exception, match="symlink entries"):
        install_skill_bundle(
            source=str(archive),
            workspace_root=tmp_path,
            project=False,
        )


def test_install_skill_bundle_from_zip_rejects_too_many_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_cfg = tmp_path / "usercfg"
    _patch_user_config_dir(monkeypatch, user_cfg)
    archive = tmp_path / "many-files.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bundle/SKILL.md", "---\nname: crowded\ndescription: crowded\n---\n\nUse it.\n")
        for idx in range(256):
            zf.writestr(f"bundle/references/file-{idx}.txt", "x")

    with pytest.raises(Exception, match="file-count limit"):
        install_skill_bundle(
            source=str(archive),
            workspace_root=tmp_path,
            project=False,
        )


def test_install_skill_bundle_from_zip_rejects_excess_uncompressed_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_cfg = tmp_path / "usercfg"
    _patch_user_config_dir(monkeypatch, user_cfg)
    archive = tmp_path / "oversized.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "bundle/SKILL.md",
            "---\nname: heavy\ndescription: heavy\n---\n\nUse it.\n",
        )
        zf.writestr("bundle/assets/blob.txt", "a" * ((8 * 1024 * 1024) + 1))

    with pytest.raises(Exception, match="uncompressed-size limit"):
        install_skill_bundle(
            source=str(archive),
            workspace_root=tmp_path,
            project=False,
        )


def test_install_skill_bundle_from_https_git_with_fake_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Preserve remote subdir/commit/state coverage without launching real git.
    from alysis_code.skills import install as install_mod

    user_cfg = tmp_path / "usercfg"
    _patch_user_config_dir(monkeypatch, user_cfg)
    monkeypatch.setattr(install_mod.shutil, "which", lambda _: "/fixture/git")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        assert kwargs["env"]["GIT_ALLOW_PROTOCOL"] == "https"
        if args[1] == "clone":
            assert args[2:6] == ["--depth", "1", "--", "https://example.invalid/skill.git"]
            _write_skill_bundle(
                Path(args[-1]) / "packs" / "architecture-review",
                name="architecture-review",
                description="Review architecture changes.",
            )
        return subprocess.CompletedProcess(args, 0, stdout="a" * 40 + "\n", stderr="")

    monkeypatch.setattr(install_mod.subprocess, "run", fake_run)
    result = install_skill_bundle(
        source="https://example.invalid/skill.git",
        workspace_root=tmp_path,
        subdir="packs/architecture-review",
        project=False,
    )
    assert len(calls) == 2
    assert result.source_kind == "git"
    assert result.source_commit == "a" * 40
    assert (user_cfg / "skills" / "architecture-review" / "SKILL.md").exists()


def test_resolve_skill_catalog_applies_global_and_project_enable_disable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_cfg = tmp_path / "usercfg"
    _patch_user_config_dir(monkeypatch, user_cfg)
    _write_skill_bundle(
        user_cfg / "skills" / "verification-playbook",
        name="verification-playbook",
        description="Choose good verify commands.",
    )

    discovered = discover_skills(
        focus_path=tmp_path,
        workspace_root=tmp_path,
        user_config_dir=user_cfg,
        home_dir=tmp_path / "home",
    )
    catalog = resolve_skill_catalog(
        discovered=discovered,
        workspace_root=tmp_path,
        user_config_dir=user_cfg,
    )
    assert catalog.effective.ordered

    global_state = set_global_skill_disabled(
        catalog.global_state, name="verification-playbook", disabled=True
    )
    from alysis_code.skills import save_global_skill_state, save_project_skill_state

    save_global_skill_state(global_state, user_config_dir=user_cfg)
    disabled_catalog = resolve_skill_catalog(
        discovered=discovered,
        workspace_root=tmp_path,
        user_config_dir=user_cfg,
    )
    assert "verification-playbook" not in disabled_catalog.effective.skills
    assert len(disabled_catalog.effective.ordered) == len(discovered.ordered) - 1
    assert sorted(s.name for s in disabled_catalog.effective.ordered) == sorted(
        _bundled_skill_names()
    )
    assert all(skill.source_scope == "bundled" for skill in disabled_catalog.effective.ordered)

    project_state = set_project_skill_override(
        disabled_catalog.project_state,
        name="verification-playbook",
        enabled=True,
    )
    save_project_skill_state(tmp_path, project_state)
    enabled_catalog = resolve_skill_catalog(
        discovered=discovered,
        workspace_root=tmp_path,
        user_config_dir=user_cfg,
    )
    assert "verification-playbook" in enabled_catalog.effective.skills
    assert len(enabled_catalog.effective.ordered) == len(discovered.ordered)
    assert sorted(s.name for s in enabled_catalog.effective.ordered) == sorted(
        _bundled_skill_names() | {"verification-playbook"}
    )


def test_remove_managed_skill_deletes_bundle_and_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_cfg = tmp_path / "usercfg"
    _patch_user_config_dir(monkeypatch, user_cfg)
    source_bundle = _write_skill_bundle(
        tmp_path / "source-skill",
        name="pytest-debug",
        description="Debug pytest failures.",
    )
    install_skill_bundle(source=str(source_bundle), workspace_root=tmp_path, project=False)

    result = remove_managed_skill(name="pytest-debug", workspace_root=tmp_path, project=False)

    assert result.removed_name == "pytest-debug"
    assert not (user_cfg / "skills" / "pytest-debug").exists()
    assert "pytest-debug" not in (user_cfg / "skills.json").read_text(encoding="utf-8").casefold()


def test_scaffold_project_force_preserves_existing_bundle_when_state_is_malformed(
    tmp_path: Path,
) -> None:
    initial = scaffold_skill_bundle(
        name="Pytest Debug",
        description="Original description.",
        workspace_root=tmp_path,
        project=True,
    )
    original_text = (initial.bundle_path / "SKILL.md").read_text(encoding="utf-8")
    state_path = tmp_path / ".alysis" / "skills.json"
    state_path.write_text("{broken json", encoding="utf-8")

    with pytest.raises(Exception, match="Invalid JSON"):
        scaffold_skill_bundle(
            name="Pytest Debug",
            description="Replacement description.",
            workspace_root=tmp_path,
            project=True,
            force=True,
        )

    assert (initial.bundle_path / "SKILL.md").read_text(encoding="utf-8") == original_text


def test_install_skill_bundle_force_restores_existing_global_bundle_on_state_save_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_cfg = tmp_path / "usercfg"
    _patch_user_config_dir(monkeypatch, user_cfg)
    old_bundle = _write_skill_bundle(
        tmp_path / "old-skill",
        name="docs-consistency",
        description="Old description.",
        body="Old instructions.\n",
    )
    new_bundle = _write_skill_bundle(
        tmp_path / "new-skill",
        name="docs-consistency",
        description="New description.",
        body="New instructions.\n",
    )
    install_skill_bundle(
        source=str(old_bundle),
        workspace_root=tmp_path,
        project=False,
        user_config_dir=user_cfg,
    )
    installed_entry = user_cfg / "skills" / "docs-consistency" / "SKILL.md"
    original_text = installed_entry.read_text(encoding="utf-8")

    def _fail_save(*args: object, **kwargs: object) -> None:
        raise RuntimeError("state save failed")

    monkeypatch.setattr(
        "alysis_code.skills.install.save_global_skill_state",
        _fail_save,
    )

    with pytest.raises(RuntimeError, match="state save failed"):
        install_skill_bundle(
            source=str(new_bundle),
            workspace_root=tmp_path,
            project=False,
            user_config_dir=user_cfg,
            force=True,
        )

    assert installed_entry.read_text(encoding="utf-8") == original_text


def test_remove_managed_skill_restores_bundle_on_state_save_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_cfg = tmp_path / "usercfg"
    _patch_user_config_dir(monkeypatch, user_cfg)
    source_bundle = _write_skill_bundle(
        tmp_path / "source-skill",
        name="pytest-debug",
        description="Debug pytest failures.",
    )
    install_skill_bundle(
        source=str(source_bundle),
        workspace_root=tmp_path,
        project=False,
        user_config_dir=user_cfg,
    )
    bundle_dir = user_cfg / "skills" / "pytest-debug"
    original_state = (user_cfg / "skills.json").read_text(encoding="utf-8")

    def _fail_save(*args: object, **kwargs: object) -> None:
        raise RuntimeError("state save failed")

    monkeypatch.setattr(
        "alysis_code.skills.install.save_global_skill_state",
        _fail_save,
    )

    with pytest.raises(RuntimeError, match="state save failed"):
        remove_managed_skill(
            name="pytest-debug",
            workspace_root=tmp_path,
            project=False,
            user_config_dir=user_cfg,
        )

    assert bundle_dir.exists()
    assert (user_cfg / "skills.json").read_text(encoding="utf-8") == original_state


def test_remove_managed_skill_rejects_global_bundle_dir_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_cfg = tmp_path / "usercfg"
    _patch_user_config_dir(monkeypatch, user_cfg)
    victim = tmp_path / "victim-global"
    victim.mkdir(parents=True)
    (victim / "keep.txt").write_text("still here\n", encoding="utf-8")
    user_cfg.mkdir(parents=True, exist_ok=True)
    (user_cfg / "skills.json").write_text(
        json.dumps(
            {
                "version": 1,
                "managed_installs": {
                    "danger": {
                        "name": "danger",
                        "bundle_dir": "../../victim-global",
                        "scope": "user",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="invalid bundle_dir"):
        remove_managed_skill(
            name="danger", workspace_root=tmp_path, project=False, user_config_dir=user_cfg
        )

    assert (victim / "keep.txt").exists()


def test_remove_managed_skill_rejects_project_bundle_dir_escape(tmp_path: Path) -> None:
    victim = tmp_path / "victim-project"
    victim.mkdir(parents=True)
    (victim / "keep.txt").write_text("still here\n", encoding="utf-8")
    state_path = tmp_path / ".alysis" / "skills.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "managed_installs": {
                    "danger": {
                        "name": "danger",
                        "bundle_dir": "../../victim-project",
                        "scope": "project",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="invalid bundle_dir"):
        remove_managed_skill(name="danger", workspace_root=tmp_path, project=True)

    assert (victim / "keep.txt").exists()


def test_save_global_skill_state_uses_atomic_write_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_cfg = tmp_path / "usercfg"
    calls: list[Path] = []

    def _tracked_atomic_write_json(path: Path, payload: object, **kwargs: object) -> None:
        calls.append(path)
        _real_atomic_write_json(path, payload, **kwargs)

    monkeypatch.setattr(
        "alysis_code.skills.state.atomic_write_json",
        _tracked_atomic_write_json,
    )

    save_global_skill_state(
        SkillLifecycleState(disabled_names=("docs-consistency",)),
        user_config_dir=user_cfg,
    )

    state_path = user_cfg / "skills.json"
    assert calls == [state_path]
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["disabled_names"] == ["docs-consistency"]


def test_shared_frontmatter_helpers_keep_skill_and_subagent_parsing_working(tmp_path: Path) -> None:
    _write_skill_bundle(
        tmp_path / ".alysis_skills" / "docs-consistency",
        name="docs-consistency",
        description="Keep docs aligned.",
    )
    agent_dir = tmp_path / ".alysis_agents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "reviewer.md").write_text(
        (
            "---\n"
            "name: reviewer\n"
            "description: Review changes.\n"
            "mode: readonly\n"
            "---\n\n"
            "Review the diff and report risks.\n"
        ),
        encoding="utf-8",
    )

    skills = discover_skills(focus_path=tmp_path, workspace_root=tmp_path)
    subagents = load_subagent_registry(root=tmp_path)

    assert "docs-consistency" in skills.skills
    assert "reviewer" in subagents


def test_create_session_uses_effective_enabled_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_cfg = tmp_path / "usercfg"
    _patch_user_config_dir(monkeypatch, user_cfg)
    _write_skill_bundle(
        user_cfg / "skills" / "verification-playbook",
        name="verification-playbook",
        description="Choose good verify commands.",
    )
    from alysis_code.skills import save_global_skill_state

    discovered = discover_skills(
        focus_path=tmp_path,
        workspace_root=tmp_path,
        user_config_dir=user_cfg,
        home_dir=tmp_path / "home",
    )
    catalog = resolve_skill_catalog(
        discovered=discovered,
        workspace_root=tmp_path,
        user_config_dir=user_cfg,
    )
    save_global_skill_state(
        set_global_skill_disabled(
            catalog.global_state, name="verification-playbook", disabled=True
        ),
        user_config_dir=user_cfg,
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
        assert "verification-playbook" not in session.skill_registry
        assert sorted(s.name for s in session.skills_ordered) == sorted(_bundled_skill_names())
        assert all(skill.source_scope == "bundled" for skill in session.skills_ordered)
    finally:
        session.close()


def test_create_session_tolerates_invalid_project_skill_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_cfg = tmp_path / "usercfg"
    _patch_user_config_dir(monkeypatch, user_cfg)
    _write_skill_bundle(
        tmp_path / ".alysis_skills" / "docs-consistency",
        name="docs-consistency",
        description="Keep docs aligned.",
    )
    state_path = tmp_path / ".alysis" / "skills.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{not valid json", encoding="utf-8")

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
        assert session.skills_ordered[0].name == "docs-consistency"
        assert sorted(s.name for s in session.skills_ordered) == sorted(
            _bundled_skill_names() | {"docs-consistency"}
        )
        assert all(skill.source_scope == "bundled" for skill in session.skills_ordered[1:])
    finally:
        session.close()
