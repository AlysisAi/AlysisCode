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


def test_source_urls_and_container_docs_point_to_alysisai() -> None:
    branding = _read("src/alysis_code/branding.py")
    sandbox = _read("docs/shell_sandbox.md")

    assert 'PROJECT_SOURCE_URL = "https://github.com/AlysisAi/AlysisCode"' in branding
    assert "ghcr.io/alysisai/alysis-sandbox" in sandbox
    assert "ap" + "fivos/alysis" not in branding
    assert "ghcr.io/ap" + "fivos" not in sandbox
