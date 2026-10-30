import os
import zipfile
from pathlib import Path

import pytest

from alysis_code.skills.state import SkillLifecycleError, load_project_skill_state
from alysis_code.skills.workbench import SkillWorkbench, read_bundle


@pytest.fixture(autouse=True)
def isolated_customizations(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "isolated-config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "isolated-data"))
    monkeypatch.setattr(
        "alysis_code.skills.paths.canonical_user_config_dir", lambda: tmp_path / "isolated-config"
    )


def bundle(root: Path, name: str = "review", *, extra: str = "") -> Path:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: >\n  Review this\n  project carefully.\n{extra}---\n\nRead the project before making changes.\n",
        encoding="utf-8",
    )
    (root / "assets").mkdir()
    (root / "assets" / "διάγραμμα.bin").write_bytes(b"\0\xffsupport")
    return root


def test_multiple_unicode_skills_install_reviewed_bytes_and_scope(tmp_path):
    source = tmp_path / "repository"
    first = bundle(source / "εργαλεία" / "α", "έλεγχος")
    bundle(source / "two", "second")
    workspace = tmp_path / "project"
    workspace.mkdir()
    personal = tmp_path / "personal"
    service = SkillWorkbench(user_config_dir=personal)
    preview = service.preview(workspace=workspace, source=str(source))
    assert len(preview["candidates"]) == 2
    item = next(item for item in preview["candidates"] if item["name"] == "έλεγχος")
    assert item["description"] == "Review this project carefully."
    (first / "SKILL.md").write_text("changed after preview")
    result = service.install(workspace, preview["draft_id"], [item["id"]], project=True)
    assert result["changed"]
    target = workspace / ".alysis_skills" / "έλεγχοσ"
    assert "Read the project" in (target / "SKILL.md").read_text()
    assert (target / "assets" / "διάγραμμα.bin").read_bytes() == b"\0\xffsupport"
    assert not personal.exists()
    record = load_project_skill_state(workspace).managed_installs["έλεγχος".casefold()]
    assert record.source == str(source)
    assert record.source_digest == read_bundle(target).digest
    with pytest.raises(SkillLifecycleError, match="expired"):
        service.install(workspace, preview["draft_id"], [item["id"]], project=True)


def test_workspace_binding_and_cancel(tmp_path):
    source = bundle(tmp_path / "source")
    service = SkillWorkbench(user_config_dir=tmp_path / "personal")
    preview = service.preview(workspace=tmp_path / "one", source=str(source))
    with pytest.raises(SkillLifecycleError, match="another project"):
        service.install(tmp_path / "two", preview["draft_id"], ["."], project=True)
    service.discard(tmp_path / "one", preview["draft_id"])
    assert not (tmp_path / "personal").exists()


def test_github_tree_link_resolves_slash_reference_and_pins_snapshot(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import alysis_code.skills.workbench as workbench

    calls = []

    def git(args, *, cwd):
        calls.append((args, cwd))
        if args[1] == "ls-remote":
            return SimpleNamespace(
                stdout="a" * 40
                + "\trefs/heads/feature\n"
                + "b" * 40
                + "\trefs/heads/feature/skills\n"
            )
        if args[1] == "rev-parse":
            return SimpleNamespace(stdout="b" * 40 + "\n")
        if args[1] == "checkout":
            bundle(cwd / "crew/review")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(workbench, "run_installer_git", git)
    result = SkillWorkbench().preview(
        workspace=tmp_path, source="https://github.com/example/repository/tree/feature/skills/crew"
    )
    assert result["source"] == "https://github.com/example/repository"
    assert result["source_ref"] == "feature/skills"
    assert result["source_commit"] == "b" * 40
    assert result["candidates"][0]["id"] == "crew/review"
    assert next(args for args, _ in calls if args[1] == "fetch")[-1] == "feature/skills"


def test_update_conflicts_and_reviewed_update(tmp_path):
    source = bundle(tmp_path / "source")
    project = tmp_path / "project"
    service = SkillWorkbench()
    preview = service.preview(workspace=project, source=str(source))
    service.install(project, preview["draft_id"], ["."], project=True)
    target = project / ".alysis_skills" / "review"
    (source / "assets" / "διάγραμμα.bin").write_bytes(b"new")
    with pytest.raises(SkillLifecycleError, match="source changed after review"):
        service.update_preview(project, "review", project=True, expected_source="stale-provenance")
    update = service.update_preview(project, "review", project=True)
    assert update["update_available"]
    (target / "SKILL.md").write_text((target / "SKILL.md").read_text() + "\nLocal edit\n")
    with pytest.raises(SkillLifecycleError, match="Local edits were preserved"):
        service.install(project, update["draft_id"], ["."], project=True)
    assert "Local edit" in (target / "SKILL.md").read_text()
    with pytest.raises(SkillLifecycleError, match="Local edits"):
        service.update_preview(project, "review", project=True)


def test_edit_revision_and_supporting_files(tmp_path):
    source = bundle(tmp_path / "source")
    service = SkillWorkbench(user_config_dir=tmp_path / "personal")
    preview = service.preview(workspace=tmp_path, source=str(source))
    service.install(tmp_path, preview["draft_id"], ["."], project=False)
    _, path = service.managed(tmp_path, "review", project=False)
    files = read_bundle(path)
    content = (path / "SKILL.md").read_text() + "\nCheck errors.\n"
    service.save(tmp_path, content, project=False, name="review", revision=files.digest)
    assert (path / "assets" / "διάγραμμα.bin").exists()
    with pytest.raises(SkillLifecycleError, match="changed"):
        service.save(tmp_path, content, project=False, name="review", revision=files.digest)


def test_metadata_is_preserved_and_unsupported_behavior_starts_disabled(tmp_path):
    source = bundle(
        tmp_path / "source", extra="disable-model-invocation: true\nmetadata:\n  author: José\n"
    )
    service = SkillWorkbench()
    preview = service.preview(workspace=tmp_path, source=str(source))
    assert preview["candidates"][0]["unsupported_metadata"] == ["disable-model-invocation"]
    with pytest.raises(SkillLifecycleError, match="unsupported metadata"):
        service.install(tmp_path, preview["draft_id"], ["."], project=True)
    service.install(tmp_path, preview["draft_id"], ["."], project=True, accept_partial=True)
    assert "review" in load_project_skill_state(tmp_path).disabled_names
    assert "author: José" in (tmp_path / ".alysis_skills/review/SKILL.md").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("project_scope", [True, False])
def test_update_with_unsupported_behavior_disables_the_skill(tmp_path, project_scope):
    from alysis_code.skills.state import load_global_skill_state

    source = bundle(tmp_path / "source")
    personal = tmp_path / "personal"
    service = SkillWorkbench(user_config_dir=personal)
    preview = service.preview(workspace=tmp_path, source=str(source))
    service.install(tmp_path, preview["draft_id"], ["."], project=project_scope)
    path = source / "SKILL.md"
    path.write_text(
        path.read_text().replace("description:", "disable-model-invocation: true\ndescription:")
    )
    update = service.update_preview(tmp_path, "review", project=project_scope)
    assert update["candidates"][0]["unsupported_metadata"] == ["disable-model-invocation"]
    result = service.install(
        tmp_path, update["draft_id"], ["."], project=project_scope, accept_partial=True
    )
    state = (
        load_project_skill_state(tmp_path)
        if project_scope
        else load_global_skill_state(user_config_dir=personal)
    )
    assert result["results"][0]["disabled"] is True
    assert "review" in state.disabled_names


def test_source_links_rejected(tmp_path, require_symlinks):
    source = bundle(tmp_path / "source")
    (source / "linked").symlink_to(tmp_path / "outside")
    service = SkillWorkbench()
    with pytest.raises(SkillLifecycleError, match="link"):
        service.preview(workspace=tmp_path, source=str(source))


def test_zip_traversal_rejected(tmp_path):
    service = SkillWorkbench()
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("../escape/SKILL.md", "bad")
    with pytest.raises(SkillLifecycleError):
        service.preview(workspace=tmp_path, source=str(archive))
    assert not (tmp_path / "escape").exists()


def test_zip_outer_folder(tmp_path):
    source = native_package(tmp_path / "package")
    archive = tmp_path / "package.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        for path in source.rglob("*"):
            if path.is_file():
                zipped.write(path, "repo-main/" + path.relative_to(source).as_posix())
    workspace = tmp_path / "project"
    workspace.mkdir()
    service = SkillWorkbench()
    preview = service.preview(workspace=workspace, source=str(archive))
    assert preview["package"]["format"] == "Alysis"
    assert preview["candidates"][0]["id"] == "repo-main/skill"


def test_project_storage_links(tmp_path, require_symlinks):
    source = bundle(tmp_path / "source")
    workspace = tmp_path / "project"
    workspace.mkdir()
    service = SkillWorkbench()
    preview = service.preview(workspace=workspace, source=str(source))
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / ".alysis_skills").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SkillLifecycleError, match="symbolic links"):
        service.install(workspace, preview["draft_id"], ["."], project=True)
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Native Windows directory junctions")
@pytest.mark.parametrize("placement", ["root", "child", "subdir", "ancestor"])
def test_source_junctions_rejected(tmp_path, placement):
    from _winapi import CreateJunction

    source = bundle(tmp_path / "source")
    outside = bundle(tmp_path / "outside", "outside")
    link = tmp_path / "linked" if placement in {"root", "ancestor"} else source / "linked"
    CreateJunction(str(outside), str(link))
    if placement == "ancestor":
        bundle(outside / "nested")
    selected = (
        link / "nested" if placement == "ancestor" else link if placement == "root" else source
    )
    with pytest.raises(SkillLifecycleError, match="link|reparse"):
        SkillWorkbench().preview(
            workspace=tmp_path,
            source=str(selected),
            subdir="linked" if placement == "subdir" else "",
        )


@pytest.mark.skipif(os.name != "nt", reason="Native Windows directory junctions")
@pytest.mark.parametrize("directory", [".alysis_skills", ".alysis"])
def test_project_storage_junctions_rejected(tmp_path, directory):
    from _winapi import CreateJunction

    source = bundle(tmp_path / "source")
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    CreateJunction(str(outside), str(workspace / directory))
    service = SkillWorkbench()
    preview = service.preview(workspace=workspace, source=str(source))
    with pytest.raises(SkillLifecycleError, match="link|reparse"):
        service.install(workspace, preview["draft_id"], ["."], project=True)
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Native Windows directory junctions")
def test_personal_storage_junction_rejected(tmp_path):
    from _winapi import CreateJunction

    source = bundle(tmp_path / "source")
    personal = tmp_path / "personal"
    personal.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    CreateJunction(str(outside), str(personal / "skills"))
    service = SkillWorkbench(user_config_dir=personal)
    preview = service.preview(workspace=tmp_path, source=str(source))
    with pytest.raises(SkillLifecycleError, match="link|reparse"):
        service.install(tmp_path, preview["draft_id"], ["."], project=False)
    assert list(outside.iterdir()) == []


def test_collision_fails_before_any_install(tmp_path):
    source = tmp_path / "source"
    bundle(source / "a", "same")
    bundle(source / "b", "same")
    service = SkillWorkbench()
    preview = service.preview(workspace=tmp_path, source=str(source))
    with pytest.raises(SkillLifecycleError, match="conflicting names"):
        service.install(tmp_path, preview["draft_id"], ["a", "b"], project=True)
    assert not (tmp_path / ".alysis_skills").exists()


def native_package(root: Path) -> Path:
    bundle(root / "skill", "native-review")
    (root / "alysis-plugin.toml").write_text("""schema_version = 1
[plugin]
id = "acme.workflows"
name = "Project workflows"
version = "1.0.0"
description = "Review workflows"
author = "Acme"
license = "MIT"
[compatibility]
alysis = ">=0.1,<1"
platforms = ["linux", "darwin", "windows"]
[[components.skill]]
path = "skill"
""")
    return root


def test_native_package_uses_snapshot_and_existing_lifecycle(tmp_path):
    from alysis_code.extensions.paths import installed_plugin_root
    from alysis_code.extensions.state import load_global_state

    source = native_package(tmp_path / "source")
    workspace = tmp_path / "project"
    workspace.mkdir()
    service = SkillWorkbench()
    preview = service.preview(workspace=workspace, source=str(source))
    assert preview["package"]["valid"]
    assert preview["package"]["components"] == ["skill"]
    (source / "skill/SKILL.md").write_text("Changed after review")
    result = service.install_package(workspace, preview["draft_id"])
    assert result == {"changed": True, "plugin_id": "acme.workflows"}
    installed = load_global_state().installed["acme.workflows"]
    assert installed.source_kind == "snapshot"
    path = installed_plugin_root("acme.workflows", project=False, repo_root=workspace)
    assert "Read the project" in (path / "skill/SKILL.md").read_text()
    assert installed.package_sha256 == read_bundle(path).digest


def test_native_install_failure_rolls_back_components_and_package(tmp_path, monkeypatch):
    from alysis_code.extensions import install
    from alysis_code.extensions.paths import installed_plugin_root
    from alysis_code.extensions.state import load_global_state
    from alysis_code.skills.paths import global_managed_skill_root

    source = native_package(tmp_path / "source")
    service = SkillWorkbench()
    preview = service.preview(workspace=tmp_path, source=str(source))

    def fail(**kwargs):
        raise OSError("injected install failure")

    monkeypatch.setattr(install, "_install_tools", fail)
    with pytest.raises(SkillLifecycleError, match="injected install failure"):
        service.install_package(tmp_path, preview["draft_id"])
    assert "acme.workflows" not in load_global_state().installed
    assert not (global_managed_skill_root() / "native-review").exists()
    assert not installed_plugin_root("acme.workflows", project=False, repo_root=tmp_path).exists()


def test_management_protocol_creation_edit_and_hook_validation(tmp_path):
    from alysis_code.ide.management_protocol import handle_management_method
    from alysis_code.ide.protocol import ProtocolError

    source = bundle(tmp_path / "source")
    workspace = tmp_path / "project"
    workspace.mkdir()

    def rpc(method, **params):
        return handle_management_method(
            method,
            {"workspace": str(workspace), "workspace_trusted": True, **params},
            request_id="test",
        )

    assert (
        rpc("skill.import.preview", source=str(source))["action"]["kind"] == "requires_confirmation"
    )
    preview = rpc("skill.import.preview", source=str(source), yes=True)
    assert rpc("skill.import.describe", draft_id=preview["draft_id"])["source"] == str(source)
    assert rpc(
        "skill.import.install", draft_id=preview["draft_id"], selected=["."], project=True, yes=True
    )["changed"]
    document = rpc("skill.document.read", name="review")
    assert document["editable"]
    assert rpc(
        "skill.document.save",
        name="review",
        content=document["content"] + "\nMore instructions.\n",
        revision=document["revision"],
        project=True,
        yes=True,
    )["changed"]
    hooks = rpc("hooks.document.read", layer="project")
    with pytest.raises(ProtocolError, match="Invalid hooks"):
        rpc(
            "hooks.document.save",
            layer="project",
            revision=hooks["revision"],
            content='{"hooks":{"InvalidEvent":[]}}',
            yes=True,
        )
    assert not (workspace / ".alysis/hooks.json").exists()
    assert rpc(
        "hooks.document.save",
        layer="project",
        revision=hooks["revision"],
        content='{"schema_version":1,"hooks":{}}',
        yes=True,
    )["changed"]
    with pytest.raises(ProtocolError, match="changed after review"):
        rpc("hooks.trust", target="project_config", expected_hash="old")
