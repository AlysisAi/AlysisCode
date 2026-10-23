"""Content-pinned descriptors shipped with the engine, without network installation."""

from __future__ import annotations

import hashlib
import re
from importlib import resources

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
