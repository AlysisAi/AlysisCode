from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_REPEATED_DASH_RE = re.compile(r"-+")


def normalize_extension_id(value: str) -> str:
    return value.strip().lower()


def plugin_slug_from_id(ext_id: str) -> str:
    slug = _NON_ALNUM_RE.sub("-", normalize_extension_id(ext_id))
    slug = _REPEATED_DASH_RE.sub("-", slug).strip("-")
    return slug or "ext"


class RegistryEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    description: str
    repo: str
    commit: str
    version: str | None = None
    tags: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    manifest_path: str | None = None
    sha256: str | None = None
    source_kind: Literal["git", "bundled"] = "git"
    bundled_path: str | None = None
    package_sha256: str | None = None
    upstream_commit: str | None = None
    # Display only (Marketplace): category ids, who publishes it, and its review tier.
    categories: list[str] = Field(default_factory=list)
    publisher: str | None = None
    tier: Literal["alysis", "verified"] | None = None


class RegistryFile(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int = 1
    extensions: list[RegistryEntry] = Field(default_factory=list)


class InstalledExtensionState(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    version: str | None = None
    commit: str | None = None
    source: str | None = None
    trust: str | None = None
    enabled: bool = False
    manifest_sha256: str | None = None
    installed_at: str | None = None
    source_url: str | None = None
    scope: Literal["user", "project"] | None = None
    component_ids: dict[str, list[str]] = Field(default_factory=dict)
    #: "custom": the user's own online plugin, whose descriptor the desktop app supplied.
    source_kind: Literal["git", "bundled", "custom", "snapshot"] = "git"
    package_sha256: str | None = None
    upstream_commit: str | None = None


class ExtensionState(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int = 1
    installed: dict[str, InstalledExtensionState] = Field(default_factory=dict)
    enabled: list[str] = Field(default_factory=list)


class ProjectExtensionOverrides(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int = 1
    enabled: list[str] = Field(default_factory=list)
    disabled: list[str] = Field(default_factory=list)
    allow_overrides: bool = False
