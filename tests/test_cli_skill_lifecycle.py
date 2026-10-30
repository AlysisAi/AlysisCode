from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.skills import discover_skills, resolve_skill_catalog
from alysis_code.workspace_context import WorkspaceContext


@pytest.fixture(autouse=True)
def _isolate_cli_workspace_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep CLI fixtures scoped even when pytest's base temp lives in this Git repo."""

    def resolve(path: Path) -> WorkspaceContext:
        root = Path(path).expanduser().resolve()
        return WorkspaceContext(
            input_path=root,
            focus_path=root,
            workspace_root=root,
            git_root=None,
            focus_relpath=".",
            workspace_kind="plain_dir",
            has_head_commit=False,
            current_branch=None,
        )

    monkeypatch.setattr(
        cli_mod,
        "resolve_workspace_context",
        resolve,
    )


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


def test_cli_skill_init_and_create_support_project_native_and_portable_roots(
    tmp_path: Path,
) -> None:
    runner = CliRunner()

    native = runner.invoke(
        cli_mod.app,
        ["skill", "init", "Pytest Debug", "--path", str(tmp_path)],
    )
    portable = runner.invoke(
        cli_mod.app,
        ["skill", "create", "Docs Consistency", "--path", str(tmp_path), "--portable"],
    )

    assert native.exit_code == 0
    assert portable.exit_code == 0
    assert (tmp_path / ".alysis_skills" / "pytest-debug" / "SKILL.md").exists()
    assert (tmp_path / ".agents" / "skills" / "docs-consistency" / "SKILL.md").exists()
    assert "Created skill scaffold:" in native.output


def test_cli_skill_validate_supports_bundle_path_and_all(tmp_path: Path) -> None:
    runner = CliRunner()
    valid = _write_skill_bundle(
        tmp_path / ".alysis_skills" / "verification-playbook",
        name="verification-playbook",
        description="Choose good verify commands.",
    )
    broken = tmp_path / ".alysis_skills" / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_bytes(b"---\nname: broken\ndescription: broken\n---\n\n\xff")

    single = runner.invoke(
        cli_mod.app,
        ["skill", "validate", str(valid)],
    )
    validate_all = runner.invoke(
        cli_mod.app,
        ["skill", "validate", "--all", "--path", str(tmp_path)],
    )

    assert single.exit_code == 0
    assert "valid: yes" in single.output
    assert validate_all.exit_code == 1
    assert "UTF-8" in validate_all.output


def test_cli_skill_install_enable_disable_remove_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    user_cfg = tmp_path / "usercfg"
    _patch_user_config_dir(monkeypatch, user_cfg)
    source_bundle = _write_skill_bundle(
        tmp_path / "source-skill",
        name="docs-consistency",
        description="Keep docs aligned.",
    )

    install = runner.invoke(
        cli_mod.app,
        ["skill", "install", str(source_bundle)],
    )
    disable = runner.invoke(
        cli_mod.app,
        ["skill", "disable", "docs-consistency"],
    )
    info_disabled = runner.invoke(
        cli_mod.app,
        ["skill", "info", "docs-consistency", "--path", str(tmp_path)],
    )
    enable = runner.invoke(
        cli_mod.app,
        ["skill", "enable", "docs-consistency"],
    )
    remove = runner.invoke(
        cli_mod.app,
        ["skill", "remove", "docs-consistency"],
    )

    assert install.exit_code == 0
    assert disable.exit_code == 0
    assert info_disabled.exit_code == 0
    assert "enabled: no" in info_disabled.output
    assert enable.exit_code == 0
    assert remove.exit_code == 0
    assert not (user_cfg / "skills" / "docs-consistency").exists()


def test_cli_disable_enable_bundled_skill_persists_in_global_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    user_cfg = tmp_path / "usercfg"
    _patch_user_config_dir(monkeypatch, user_cfg)

    disable = runner.invoke(
        cli_mod.app,
        ["skill", "disable", "code-review", "--path", str(tmp_path)],
    )
    after_disable = resolve_skill_catalog(
        discovered=discover_skills(
            focus_path=tmp_path,
            workspace_root=tmp_path,
            user_config_dir=user_cfg,
            home_dir=tmp_path / "empty-home",
        ),
        workspace_root=tmp_path,
        user_config_dir=user_cfg,
    )
    disabled_state_text = (user_cfg / "skills.json").read_text(encoding="utf-8")
    enable = runner.invoke(
        cli_mod.app,
        ["skill", "enable", "code-review", "--path", str(tmp_path)],
    )
    after_enable = resolve_skill_catalog(
        discovered=discover_skills(
            focus_path=tmp_path,
            workspace_root=tmp_path,
            user_config_dir=user_cfg,
            home_dir=tmp_path / "empty-home",
        ),
        workspace_root=tmp_path,
        user_config_dir=user_cfg,
    )
    bundled_path = after_enable.effective.skills["code-review"].source_path
    remove = runner.invoke(
        cli_mod.app,
        ["skill", "remove", "code-review", "--path", str(tmp_path)],
    )

    assert disable.exit_code == 0
    assert "code-review" not in after_disable.effective.skills
    assert '"code-review"' in disabled_state_text
    assert enable.exit_code == 0
    assert "code-review" in after_enable.effective.skills
    assert remove.exit_code == 1
    assert "Managed user skill not found" in remove.output
    assert (bundled_path / "SKILL.md").is_file()


def test_cli_skill_install_rejects_zip_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    user_cfg = tmp_path / "usercfg"
    _patch_user_config_dir(monkeypatch, user_cfg)
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.txt", "nope")
        zf.writestr("bundle/SKILL.md", "---\nname: bad\ndescription: bad\n---\n\nbad\n")

    result = runner.invoke(cli_mod.app, ["skill", "install", str(archive)])

    assert result.exit_code == 1
    assert "escapes extraction root" in result.output


def test_cli_skill_list_warns_on_invalid_project_state(tmp_path: Path) -> None:
    runner = CliRunner()
    _write_skill_bundle(
        tmp_path / ".alysis_skills" / "docs-consistency",
        name="docs-consistency",
        description="Keep docs aligned.",
    )
    state_path = tmp_path / ".alysis" / "skills.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{bad json", encoding="utf-8")

    result = runner.invoke(cli_mod.app, ["skill", "list", "--path", str(tmp_path)])

    assert result.exit_code == 0
    discovered = discover_skills(focus_path=tmp_path, workspace_root=tmp_path)
    assert f"Skills ({len(discovered.ordered)})" in result.output
    assert "Lifecycle state warning:" in result.output


def test_cli_skill_list_and_info_warn_on_invalid_global_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    user_cfg = tmp_path / "usercfg"
    _patch_user_config_dir(monkeypatch, user_cfg)
    _write_skill_bundle(
        user_cfg / "skills" / "verification-playbook",
        name="verification-playbook",
        description="Choose good verify commands.",
    )
    user_cfg.mkdir(parents=True, exist_ok=True)
    (user_cfg / "skills.json").write_text("{bad json", encoding="utf-8")

    list_result = runner.invoke(
        cli_mod.app,
        ["skill", "list", "--path", str(tmp_path)],
    )
    info_result = runner.invoke(
        cli_mod.app,
        ["skill", "info", "verification-playbook", "--path", str(tmp_path)],
    )

    assert list_result.exit_code == 0
    assert "verification-playbook" in list_result.output
    assert "Lifecycle state warning:" in list_result.output
    assert info_result.exit_code == 0
    assert "name: verification-playbook" in info_result.output
    assert "Lifecycle state warning:" in info_result.output
