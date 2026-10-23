"""Canonical product identity and runtime locations for Alysis Code."""

from __future__ import annotations

import os
from collections.abc import Mapping
from importlib import metadata as importlib_metadata
from pathlib import Path
from urllib.parse import urlparse

from platformdirs import user_config_dir, user_data_dir

PRODUCT_NAME = "Alysis Code"
CANONICAL_APP_NAME = "alysis"
CANONICAL_SERVER_APP_NAME = "alysis-code-server"
PYTHON_PACKAGE_NAME = "alysis-code"
PROJECT_SOURCE_URL = "https://github.com/AlysisAi/AlysisCode"
SANDBOX_IMAGE_REPOSITORY = "alysis-sandbox"
ENV_PREFIX = "ALYSIS_"
PROJECT_DIR_NAME = ".alysis"
PLUGIN_MANIFEST_NAME = "alysis-plugin.toml"


def env_get(name: str, default: str | None = None) -> str | None:
    """Read an environment variable using its canonical name."""
    return env_get_from(os.environ, name, default)


def env_get_from(environ: Mapping[str, str], name: str, default: str | None = None) -> str | None:
    """Read a canonical environment variable from an injected mapping."""
    return environ.get(name, default)


def _github_owner_from_url(url: str) -> str | None:
    value = url.strip()
    if value.startswith("git@github.com:"):
        path = value.removeprefix("git@github.com:")
        parts = [part for part in path.removesuffix(".git").split("/") if part]
        return parts[0] if len(parts) >= 2 else None

    parsed = urlparse(value)
    if parsed.hostname != "github.com":
        return None
    parts = [part for part in parsed.path.removesuffix(".git").strip("/").split("/") if part]
    return parts[0] if len(parts) >= 2 else None


def _packaging_source_urls() -> tuple[str, ...]:
    urls: list[str] = [PROJECT_SOURCE_URL]
    try:
        package_metadata = importlib_metadata.metadata(PYTHON_PACKAGE_NAME)
    except importlib_metadata.PackageNotFoundError:
        return tuple(urls)

    homepage = package_metadata.get("Home-page")
    if homepage:
        urls.append(homepage)
    for project_url in package_metadata.get_all("Project-URL") or ():
        label, _, value = project_url.partition(",")
        if label.strip().lower() in {"source", "repository", "homepage"} and value.strip():
            urls.append(value.strip())
    return tuple(dict.fromkeys(urls))


def resolve_ghcr_owner() -> str | None:
    for source_url in _packaging_source_urls():
        owner = _github_owner_from_url(source_url)
        if owner:
            return owner
    return None


def default_sandbox_docker_image(variant: str = "dev") -> str:
    tag = variant.strip() or "dev"
    owner = resolve_ghcr_owner()
    if owner:
        return f"ghcr.io/{owner.lower()}/{SANDBOX_IMAGE_REPOSITORY}:{tag}"
    return f"{SANDBOX_IMAGE_REPOSITORY}:{tag}"


def canonical_user_config_dir() -> Path:
    return Path(user_config_dir(CANONICAL_APP_NAME, CANONICAL_APP_NAME))


def canonical_user_data_dir() -> Path:
    return Path(user_data_dir(CANONICAL_APP_NAME, CANONICAL_APP_NAME))


def canonical_server_data_dir() -> Path:
    return Path(user_data_dir(CANONICAL_SERVER_APP_NAME, CANONICAL_APP_NAME))


def resolve_project_dir(root: Path) -> Path:
    """Return the canonical per-repository agent directory without moving data."""
    return root / PROJECT_DIR_NAME


def plugin_manifest_candidates() -> tuple[str, ...]:
    return (PLUGIN_MANIFEST_NAME,)
