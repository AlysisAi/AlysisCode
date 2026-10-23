from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

_ARTIFACT_SCAN_IGNORED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".qa-results",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "qa_reports",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (_repo_root() / path).read_text(encoding="utf-8")


def _checkout_paths(repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return [path for path in result.stdout.split("\0") if path and (repo_root / path).is_file()]

    # Release artifacts and clean source mirrors intentionally omit .git. In that
    # environment, validate the exported tree while excluding generated caches.
    return sorted(
        path.relative_to(repo_root).as_posix()
        for path in repo_root.rglob("*")
        if path.is_file()
        and not any(
            part in _ARTIFACT_SCAN_IGNORED_DIRS for part in path.relative_to(repo_root).parts
        )
    )


def test_tracked_paths_are_case_unique_for_cross_platform_checkout() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    tracked_paths = _checkout_paths(repo_root)
    by_casefold: dict[str, str] = {}
    collisions: list[str] = []
    for path in tracked_paths:
        folded = path.casefold()
        existing = by_casefold.setdefault(folded, path)
        if existing != path:
            collisions.append(f"{existing} <> {path}")

    assert not collisions, "Tracked paths differ only by case: " + ", ".join(collisions)


def test_python_runtime_baseline_is_documented_and_ci_aligned() -> None:
    repo_root = _repo_root()
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    readme = _read("README.md")
    server_doc = _read("docs/server.md")
    contributing = _read(".github/CONTRIBUTING.md")
    ci = _read(".github/workflows/ci.yml")

    assert pyproject["project"]["requires-python"] == ">=3.11"
    assert pyproject["tool"]["ruff"]["target-version"] == "py311"
    assert '"python-version":"3.11"' in ci
    assert '"python-version":"3.12"' in ci
    assert "Python 3.11+" in readme
    assert "Python 3.11 or newer" in contributing
    assert "Python 3.11 or newer" in server_doc


def test_documented_install_command_points_at_the_installer_script() -> None:
    # Users fetch the installer from this exact path on main, so the documented
    # command and the script's location must move together.
    command = (
        "curl -LsSf https://raw.githubusercontent.com/AlysisAi/AlysisCode/main/scripts/install.sh"
        " | sh"
    )
    assert command in _read("README.md")
    assert command in _read("docs/quickstart.md")
    # `curl … | sh` runs it under dash on Ubuntu, so it must stay POSIX sh.
    assert _read("scripts/install.sh").startswith("#!/bin/sh\n")
    windows_command = (
        "irm https://raw.githubusercontent.com/AlysisAi/AlysisCode/main/scripts/install.ps1 | iex"
    )
    assert windows_command in _read("README.md")
    assert windows_command in _read("docs/quickstart.md")
    assert windows_command in _read("scripts/install.ps1")


def test_readme_keeps_public_launch_surface() -> None:
    readme = _read("README.md")

    assert "Alysis Code" in readme
    assert 'src="docs/assets/banner.svg"' in readme
    assert (_repo_root() / "docs/assets/banner.svg").is_file()
    # The product site. Derived from alysis_cloud so a future move updates this
    # assertion with the constant rather than leaving a stale literal behind.
    from alysis_code.alysis_cloud import site_url

    assert f"{site_url()}/" in readme
    assert f'href="{site_url()}/docs">Documentation</a>' in readme
    assert 'href="docs/CHANGELOG.md">Changelog</a>' in readme
    assert (_repo_root() / "docs/CHANGELOG.md").is_file()
    assert 'href="https://github.com/sponsors/AlysisAi"' in readme
    assert "pipx install alysis-code" in readme
    assert "Apache-2.0" in readme


def test_governance_and_packaging_metadata_are_public_ready() -> None:
    from alysis_code.alysis_cloud import site_url

    repo_root = _repo_root()
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = set(pyproject["project"]["dependencies"])

    assert pyproject["project"]["license"]["text"] == "Apache-2.0"
    assert (
        "License :: OSI Approved :: Apache Software License" in pyproject["project"]["classifiers"]
    )
    assert "click>=8.0.0" in dependencies
    assert "packaging>=23.0" in dependencies
    assert (repo_root / "LICENSE").read_text(encoding="utf-8").startswith("Apache License")
    assert "Copyright 2026 AlysisAi" in _read("NOTICE")
    assert "alysis-contributor-pack" not in _read(".github/CONTRIBUTING.md")
    assert "products@alysisai.com" in _read(".github/SECURITY.md")
    assert pyproject["project"]["urls"]["Documentation"] == f"{site_url()}/docs"


def test_vscode_changelog_uses_current_product_identity() -> None:
    introduction = _read("extensions/vscode-alysis/CHANGELOG.md").split("## [Unreleased]", 1)[0]

    assert "Alysis Code VS Code extension" in introduction


def test_docs_index_links_only_existing_public_docs() -> None:
    repo_root = _repo_root()
    docs_index = _read("docs/README.md")
    linked_paths = re.findall(r"\]\(([^)]+\.md)\)", docs_index)

    assert "reference.md" in docs_index
    assert "architecture.md" in docs_index
    assert "quickstart.md" in docs_index
    assert "security_model.md" in docs_index
    assert "subagents.md" in docs_index
    for raw_path in linked_paths:
        target = (repo_root / "docs" / raw_path).resolve()
        if raw_path.startswith("../"):
            target = (repo_root / "docs" / raw_path).resolve()
        assert target.exists(), raw_path


def test_public_docs_cover_core_user_and_contributor_topics() -> None:
    reference = _read("docs/reference.md")
    architecture = _read("docs/architecture.md")
    subagents = _read("docs/subagents.md")
    mcp = _read("docs/mcp.md")
    skills = _read("docs/skills.md")
    forge = _read("docs/forge.md")
    security = _read("docs/security_model.md")

    assert "readonly" in reference
    assert "review" in reference
    assert "fullaccess" in reference
    assert "runtime_kind" in reference
    assert "High-level flow" in architecture
    assert "workspace_root" in architecture
    assert "active_workdir" in architecture
    assert "Subagents" in subagents
    for name in (
        "explorer",
        "general",
        "frontend-engineer",
        "debugger",
        "verifier",
        "code-reviewer",
        "visual-designer",
    ):
        assert name in subagents
    assert "routing_visibility" in subagents
    assert "parallel_batch_eligible" in subagents
    assert "parallel_safe" not in subagents
    assert "image_generation.enabled" in subagents
    assert "Visual QA" in subagents
    assert "Streamable HTTP" in mcp
    assert "OAuth" in mcp
    assert "Skills" in skills
    assert "skill_read" in skills
    assert "Forge" in forge
    assert "forge exec" in forge
    assert "verification" in forge.lower()
    assert "safe HTTP" in security
    assert "MCP" in security


def test_internal_cleanup_artifacts_stay_absent() -> None:
    repo_root = _repo_root()
    absent_paths = [
        "RE" + "FACTOR_NOTES.md",
        "RE" + "FACTOR_PLAN.md",
        "qa_" + "reports",
        "scripts/skills_" + "do" + "gfood_campaign.py",
        "scripts/skills_" + "do" + "gfood_validation_round.py",
        "docs/SANDBOX.md",
        "docs/FORGE.md",
        "docs/skills_" + "evals.md",
        "docs/internal",
        "docs/subagents",
        "docs/tooling",
        "docs/forge_plan_phase.md",
        "docs/forge_execution_phase.md",
        "docs/forge_swarm.md",
    ]

    source_paths = set(_checkout_paths(repo_root))
    for path in absent_paths:
        path_prefix = f"{path.rstrip('/')}/"
        assert not any(
            source_path == path or source_path.startswith(path_prefix)
            for source_path in source_paths
        ), path

    terminals = _read("docs/terminals.md")
    assert "shell_background" in terminals
    assert "shell_output" in terminals
    assert "shell_kill" in terminals
    assert "shell_list" in terminals


def test_source_urls_and_container_docs_point_to_alysisai() -> None:
    branding = _read("src/alysis_code/branding.py")
    sandbox = _read("docs/shell_sandbox.md")

    assert 'PROJECT_SOURCE_URL = "https://github.com/AlysisAi/AlysisCode"' in branding
    assert "ghcr.io/alysisai/alysis-sandbox" in sandbox
    assert "ap" + "fivos/alysis" not in branding
    assert "ghcr.io/ap" + "fivos" not in sandbox


def test_current_copy_uses_the_correct_article_for_alysis() -> None:
    repo_root = _repo_root()
    bad_phrase = "a " + "Alysis"
    bad_command_phrase = "a " + "alysis executable"
    text_suffixes = {".md", ".py", ".ts", ".json"}

    for raw_path in _checkout_paths(repo_root):
        candidate = repo_root / raw_path
        # The Git index can still include files deleted in the current checkout.
        if candidate.suffix not in text_suffixes or not candidate.is_file():
            continue
        contents = candidate.read_text(encoding="utf-8")
        assert bad_phrase not in contents, raw_path
        assert bad_command_phrase not in contents, raw_path
