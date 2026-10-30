"""Content-pinned descriptors shipped with the engine, without network installation."""

from __future__ import annotations

import hashlib
import re
import tomllib
from importlib import resources

from pydantic import ValidationError

from .manifest import PluginManifest
from .models import RegistryEntry


def bundled_manifest(entry: RegistryEntry) -> bytes:
    name = str(entry.bundled_path or "")
    if entry.source_kind != "bundled" or not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
        raise RuntimeError("Invalid bundled plugin descriptor.")
    content = (
        resources.files("alysis_code.extensions")
        .joinpath("bundled", name, "alysis-plugin.toml")
        .read_bytes()
    )
    digest = hashlib.sha256(content).hexdigest()
    if entry.package_sha256 != digest or entry.commit != f"sha256:{digest}":
        raise RuntimeError("Bundled plugin descriptor does not match its catalog content pin.")
    return content


def parse_bundled_manifest(content: bytes) -> PluginManifest | None:
    try:
        return PluginManifest.model_validate(tomllib.loads(content.decode("utf-8")))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValidationError):
        return None


def same_bundled_access(installed: PluginManifest, bundled_content: bytes) -> bool:
    """Whether an installed descriptor grants exactly what `bundled_content` grants."""
    bundled = parse_bundled_manifest(bundled_content)
    return (
        bundled is not None
        and installed.plugin.id == bundled.plugin.id
        and installed.components.model_dump(mode="json")
        == bundled.components.model_dump(mode="json")
    )
