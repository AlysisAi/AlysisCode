"""Reviewed skill imports and edits. Drafts are ephemeral and bound to one workspace.

The desktop owns source selection and consent. Nothing in this module executes a
bundle's scripts. Installation always uses the bytes shown in the preview.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import uuid4

from ..installer_git import run_installer_git
from .install import _extract_skill_zip
from .paths import global_managed_skill_root, project_managed_skill_root, skill_bundle_dir_name
from .state import (
    ManagedSkillRecord,
    SkillLifecycleError,
    load_global_skill_state,
    load_project_skill_state,
    resolve_managed_bundle_path,
    save_global_skill_state,
    save_project_skill_state,
    set_global_skill_disabled,
    set_project_skill_override,
    with_managed_install,
)
from .transactions import commit_managed_bundle_update
from .validation import validate_skill_bundle

MAX_FILES = 4096
MAX_BYTES = 32 * 1024 * 1024
MAX_DRAFTS = 4
DRAFT_LIFETIME = 30 * 60
# These are descriptive Agent Skills fields. Other metadata is retained but needs review.
DESCRIPTIVE_FIELDS = {"name", "description", "aliases", "license", "compatibility", "metadata"}


def _is_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _reject_source_links(path: Path) -> None:
    # Windows junctions can appear anywhere in a selected path. Do not resolve
    # them away before checking; POSIX system aliases such as /var remain valid.
    inspected = (path, *path.absolute().parents) if os.name == "nt" else (path,)
    if any(_is_link(item) for item in inspected):
        raise SkillLifecycleError("Source path contains a symbolic link or reparse point.")


def provenance_revision(record: ManagedSkillRecord) -> str:
    return hashlib.sha256(json.dumps(asdict(record), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class BundleFiles:
    files: dict[str, tuple[bytes, int]]

    @property
    def digest(self) -> str:
        digest = hashlib.sha256()
        for path, (data, mode) in sorted(self.files.items()):
            digest.update(json.dumps([path, mode, len(data)], ensure_ascii=False).encode())
            digest.update(b"\0" + data)
        return digest.hexdigest()

    def write(self, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=False)
        for relative, (data, mode) in self.files.items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(mode)


def read_bundle(root: Path) -> BundleFiles:
    """Bounded regular files only; no directory links, devices, or linked files."""
    _reject_source_links(root)
    if not root.is_dir():
        raise SkillLifecycleError("Choose a regular folder, not a symbolic link.")
    files: dict[str, tuple[bytes, int]] = {}
    size = 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name != ".git")
        base = Path(directory)
        for name in dirs:
            if _is_link(base / name):
                raise SkillLifecycleError("Source contains a symbolic link or reparse point.")
        for name in sorted(names):
            path = base / name
            if name == ".git":
                continue
            info = path.lstat()
            if _is_link(path) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SkillLifecycleError("Source contains a link or non-regular file.")
            size += info.st_size
            if len(files) >= MAX_FILES or size > MAX_BYTES:
                raise SkillLifecycleError(
                    "Source exceeds 4096 files or 32 MiB. Choose a subfolder."
                )
            # Recheck the opened file so a symlink swap cannot silently read outside the source.
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if (opened.st_ino, opened.st_dev) != (info.st_ino, info.st_dev):
                    raise SkillLifecycleError("Source changed while reading. Preview again.")
                data = handle.read(MAX_BYTES + 1)
            if len(data) != info.st_size:
                raise SkillLifecycleError("Source changed while reading. Preview again.")
            files[path.relative_to(root).as_posix()] = (
                data,
                0o755 if info.st_mode & 0o111 else 0o644,
            )
    return BundleFiles(files)


def _child(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative)
    if parts.is_absolute() or ".." in parts.parts or "\\" in relative:
        raise SkillLifecycleError("Subfolder must stay inside the source.")
    candidate = root.joinpath(*parts.parts)
    current = root
    for part in parts.parts:
        current /= part
        if _is_link(current):
            raise SkillLifecycleError("Source path contains a symbolic link.")
    return candidate


def _git_source(source: str, ref: str, subdir: str, target: Path) -> tuple[str, str, str, str]:
    parsed = urlsplit(source)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
    ):
        raise SkillLifecycleError(
            "Use an HTTPS repository URL without credentials or query parameters."
        )
    ref = ref or parsed.fragment
    source = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    if parsed.hostname.casefold() == "github.com":
        parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
        if len(parts) >= 4 and parts[2] == "tree":
            source = f"https://github.com/{parts[0]}/{parts[1]}"
            tail = "/".join(parts[3:])
            refs = run_installer_git(["git", "ls-remote", "--heads", "--tags", source], cwd=None)
            names = [
                line.split("\t", 1)[1].removeprefix("refs/heads/").removeprefix("refs/tags/")
                for line in refs.stdout.splitlines()
                if "\t" in line and not line.endswith("^{}")
            ]
            matches = [name for name in names if tail == name or tail.startswith(name + "/")]
            # Commit links have a full immutable SHA; branch names are resolved, never guessed.
            branch = (
                max(matches, key=len)
                if matches
                else parts[3]
                if len(parts[3]) == 40 and all(c in "0123456789abcdef" for c in parts[3].lower())
                else ""
            )
            if not branch:
                raise SkillLifecycleError(
                    "Could not resolve this GitHub link. Enter the repository, ref and subfolder separately."
                )
            if ref and ref != branch:
                raise SkillLifecycleError("The reference differs from the GitHub link.")
            ref = branch
            linked_dir = tail[len(branch) :].lstrip("/")
            subdir = "/".join(value for value in (linked_dir, subdir) if value)
    if ref.startswith("-") or any(c.isspace() for c in ref):
        raise SkillLifecycleError("Invalid Git reference.")
    target.mkdir()
    run_installer_git(["git", "init", "--quiet", str(target)], cwd=None)
    run_installer_git(["git", "fetch", "--depth=1", "--no-tags", source, ref or "HEAD"], cwd=target)
    commit = run_installer_git(["git", "rev-parse", "FETCH_HEAD"], cwd=target).stdout.strip()
    run_installer_git(["git", "checkout", "--quiet", "--detach", commit], cwd=target)
    return source, ref, subdir, commit


@dataclass
class ImportDraft:
    workspace: Path
    created: float
    source: str
    kind: str
    ref: str
    commit: str | None
    bundles: dict[str, BundleFiles]
    candidates: list[dict[str, Any]]
    package: dict[str, Any]
    package_files: BundleFiles
    update: tuple[ManagedSkillRecord, str] | None = None


class SkillWorkbench:
    def __init__(self, *, user_config_dir: Path | None = None):
        self.user_config_dir = user_config_dir
        self.drafts: dict[str, ImportDraft] = {}

    def _prune(self) -> None:
        for key, draft in list(self.drafts.items()):
            if time.monotonic() - draft.created > DRAFT_LIFETIME:
                del self.drafts[key]

    def preview(
        self, *, workspace: Path, source: str, subdir: str = "", ref: str = ""
    ) -> dict[str, Any]:
        self._prune()
        if len(self.drafts) >= MAX_DRAFTS:
            del self.drafts[next(iter(self.drafts))]
        try:
            with tempfile.TemporaryDirectory(prefix="alysis-customize-") as tmp:
                temp = Path(tmp)
                local = Path(source).expanduser()
                commit = None
                if urlsplit(source).scheme == "https":
                    source, ref, subdir, commit = _git_source(source, ref, subdir, temp / "git")
                    root, kind = temp / "git", "git"
                elif local.is_dir():
                    root, kind, source = local, "dir", str(local.absolute())
                elif local.is_file() and local.suffix.lower() == ".zip":
                    _reject_source_links(local)
                    root, kind, source = temp / "zip", "zip", str(local.absolute())
                    root.mkdir()
                    _extract_skill_zip(local, root)
                else:
                    raise SkillLifecycleError("Choose a folder, ZIP file, or HTTPS Git repository.")
                if kind == "zip" and not subdir:
                    outer = list(root.iterdir())
                    if len(outer) == 1 and outer[0].is_dir():
                        subdir = outer[0].name
                selected = _child(root, subdir)
                source_files = read_bundle(selected)
                candidates: list[dict[str, Any]] = []
                bundles: dict[str, BundleFiles] = {}
                roots = {
                    str(PurePosixPath(entry).parent)
                    for entry in source_files.files
                    if PurePosixPath(entry).name == "SKILL.md"
                }
                for entry in sorted(source_files.files):
                    if PurePosixPath(entry).name != "SKILL.md":
                        continue
                    relative = str(PurePosixPath(entry).parent)
                    if any(str(parent) in roots for parent in PurePosixPath(relative).parents):
                        continue
                    prefix = "" if relative == "." else relative + "/"
                    files = BundleFiles(
                        {
                            name[len(prefix) :]: value
                            for name, value in source_files.files.items()
                            if name.startswith(prefix)
                        }
                    )
                    candidate_id = (
                        "/".join(part for part in (subdir, relative) if part and part != ".") or "."
                    )
                    snapshot_path = temp / f"validate-{len(candidates)}"
                    files.write(snapshot_path)
                    validation = validate_skill_bundle(snapshot_path)
                    unsupported = sorted(set(validation.metadata) - DESCRIPTIVE_FIELDS)
                    bundles[candidate_id] = files
                    candidates.append(
                        {
                            "id": candidate_id,
                            "name": validation.name,
                            "description": validation.description,
                            "valid": validation.valid,
                            "issues": [issue.message for issue in validation.issues],
                            "unsupported_metadata": unsupported,
                            "files": sorted(files.files),
                            "digest": files.digest,
                            "content": files.files["SKILL.md"][0].decode("utf-8", errors="replace"),
                        }
                    )
                package = _package_summary(source_files)
                if not candidates and not package:
                    raise SkillLifecycleError(
                        "No SKILL.md or supported package manifest found in this source."
                    )
                token = uuid4().hex
                draft = ImportDraft(
                    workspace.resolve(),
                    time.monotonic(),
                    source,
                    kind,
                    ref,
                    commit,
                    bundles,
                    candidates,
                    package,
                    source_files,
                )
                self.drafts[token] = draft
                return self._payload(token, draft)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SkillLifecycleError(
                "Could not read the source. Check the path, repository access and Git reference."
            ) from exc

    def _payload(self, token: str, draft: ImportDraft) -> dict[str, Any]:
        return {
            "draft_id": token,
            "source": draft.source,
            "source_kind": draft.kind,
            "source_commit": draft.commit,
            "source_ref": draft.ref,
            "candidates": draft.candidates,
            "package": draft.package,
            "expires_in_seconds": DRAFT_LIFETIME,
            "update": draft.update is not None,
        }

    def _draft(self, workspace: Path, token: str) -> ImportDraft:
        self._prune()
        draft = self.drafts.get(token)
        if draft is None or draft.workspace != workspace.resolve():
            raise SkillLifecycleError(
                "This import preview expired or belongs to another project. Preview the source again."
            )
        return draft

    def discard(self, workspace: Path, token: str) -> dict[str, Any]:
        self._draft(workspace, token)
        del self.drafts[token]
        return {"changed": False}

    def install_package(self, workspace: Path, token: str) -> dict[str, Any]:
        from ..extensions.install import PluginInstallError, install_plugin
        from ..extensions.manifest import load_manifest
        from ..extensions.paths import installed_plugin_root
        from ..extensions.state import load_global_state

        draft = self._draft(workspace, token)
        if draft.package.get("format") != "Alysis" or not draft.package.get("valid"):
            raise SkillLifecycleError(
                "Only validated native Alysis packages can be installed as a whole."
            )
        with tempfile.TemporaryDirectory(prefix="alysis-package-review-") as temp:
            root = Path(temp) / "package"
            draft.package_files.write(root)
            manifest = load_manifest(root)
            existing = load_global_state().installed.get(manifest.plugin.id)
            if existing:
                installed = installed_plugin_root(
                    manifest.plugin.id, project=False, repo_root=workspace
                )
                if (
                    not existing.package_sha256
                    or read_bundle(installed).digest != existing.package_sha256
                ):
                    raise SkillLifecycleError(
                        "The installed package changed or has no snapshot baseline. Local edits were preserved."
                    )
                # Components can be edited outside the installed package tree. Until those
                # have a joint baseline, never overwrite an existing package through import.
                raise SkillLifecycleError(
                    "This plugin is already installed. Remove it before importing a replacement; export any local component edits first."
                )
            try:
                result = install_plugin(
                    source=draft.source + (f"#{draft.commit}" if draft.commit else ""),
                    repo_root=workspace,
                    trust_prompt=lambda request: True,
                    reviewed_snapshot=root,
                    snapshot_digest=draft.package_files.digest,
                )
            except PluginInstallError as exc:
                raise SkillLifecycleError(str(exc)) from exc
        del self.drafts[token]
        return {"changed": True, "plugin_id": result.plugin_id}

    def install(
        self,
        workspace: Path,
        token: str,
        selected: list[str],
        *,
        project: bool,
        accept_partial: bool = False,
    ) -> dict[str, Any]:
        draft = self._draft(workspace, token)
        candidates = {item["id"]: item for item in draft.candidates}
        if (
            not selected
            or len(set(selected)) != len(selected)
            or any(key not in candidates for key in selected)
        ):
            raise SkillLifecycleError("Select skills from this preview.")
        if any(not candidates[key]["valid"] for key in selected):
            raise SkillLifecycleError("Fix invalid skills before installation.")
        if (
            draft.package or any(candidates[key]["unsupported_metadata"] for key in selected)
        ) and not accept_partial:
            raise SkillLifecycleError(
                "Review package components and unsupported metadata before importing skills separately."
            )
        targets = [skill_bundle_dir_name(candidates[key]["name"]) for key in selected]
        if len(set(targets)) != len(targets):
            raise SkillLifecycleError("Selected skills have conflicting names. Import one of them.")
        if draft.update:
            record, expected = draft.update
            if len(selected) != 1 or project != (record.scope == "project"):
                raise SkillLifecycleError("An update must keep the installed skill and scope.")
            current, target = self.managed(workspace, record.name, project=project)
            if current != record or read_bundle(target).digest != expected:
                raise SkillLifecycleError(
                    "The installed skill changed since preview. Local edits were preserved."
                )
        else:
            for target in targets:
                if (self._root(workspace, project) / target).exists():
                    raise SkillLifecycleError(
                        "A selected skill is already installed. Use Check for updates or choose another name."
                    )
        results = []
        for key in selected:
            candidate = candidates[key]
            old_record = draft.update[0] if draft.update else None
            if old_record and candidate["name"] != old_record.name:
                raise SkillLifecycleError("The upstream skill was renamed. Import it separately.")
            record = ManagedSkillRecord(
                name=candidate["name"],
                bundle_dir=skill_bundle_dir_name(candidate["name"]),
                scope="project" if project else "user",
                source_kind=draft.kind,
                source=draft.source,
                source_subdir=key,
                source_commit=draft.commit,
                source_ref=draft.ref or None,
                source_digest=draft.bundles[key].digest,
                installed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            try:
                self._commit(
                    workspace,
                    record,
                    draft.bundles[key],
                    force=old_record is not None,
                    disable=bool(candidate["unsupported_metadata"]),
                )
                results.append(
                    {
                        "name": record.name,
                        "installed": True,
                        "disabled": bool(candidate["unsupported_metadata"]),
                    }
                )
            except (OSError, SkillLifecycleError) as exc:
                results.append({"name": record.name, "installed": False, "error": str(exc)})
                break
        del self.drafts[token]
        return {"changed": any(item["installed"] for item in results), "results": results}

    def _root(self, workspace: Path, project: bool) -> Path:
        root = (
            project_managed_skill_root(workspace)
            if project
            else global_managed_skill_root(user_config_dir=self.user_config_dir)
        )
        if project:
            paths = (
                workspace / ".alysis_skills",
                workspace / ".alysis",
                workspace / ".alysis/skills.json",
            )
        else:
            paths = (root, root.parent / "skills.json")
        for path in paths:
            if _is_link(path):
                raise SkillLifecycleError(
                    "Skill storage must not use symbolic links or reparse points."
                )
        return root

    def managed(
        self, workspace: Path, name: str, *, project: bool
    ) -> tuple[ManagedSkillRecord, Path]:
        self._root(workspace, project)
        state = (
            load_project_skill_state(workspace)
            if project
            else load_global_skill_state(user_config_dir=self.user_config_dir)
        )
        record = state.managed_installs.get(name.casefold())
        if not record:
            raise SkillLifecycleError(
                "Only managed skills can be changed here. Copy this skill to customize it."
            )
        path = resolve_managed_bundle_path(
            managed_root=self._root(workspace, project), bundle_dir=record.bundle_dir
        )
        return record, path

    def _commit(
        self,
        workspace: Path,
        record: ManagedSkillRecord,
        files: BundleFiles,
        *,
        force: bool,
        disable: bool = False,
    ) -> None:
        from ..extensions.component_activation import build_plugin_activation_index

        owner = build_plugin_activation_index(workspace).skill_lookup_to_plugin_id.get(
            record.name.casefold()
        )
        if owner:
            raise SkillLifecycleError(
                f"This name belongs to plugin {owner}. Copy it with a different name to customize it."
            )
        project = record.scope == "project"
        self._root(workspace, project)
        state = (
            load_project_skill_state(workspace)
            if project
            else load_global_skill_state(user_config_dir=self.user_config_dir)
        )
        if not force and record.name.casefold() in state.managed_installs:
            raise SkillLifecycleError(
                "A skill with this name is already managed. Use its update or edit action."
            )
        state = with_managed_install(state, record)
        if disable:
            state = (
                set_project_skill_override(state, name=record.name, enabled=False)
                if project
                else set_global_skill_disabled(state, name=record.name, disabled=True)
            )
        target = resolve_managed_bundle_path(
            managed_root=self._root(workspace, project), bundle_dir=record.bundle_dir
        )
        commit_managed_bundle_update(
            target_path=target,
            force=force,
            stage_bundle=files.write,
            persist_state=lambda: (
                save_project_skill_state(workspace, state)
                if project
                else save_global_skill_state(state, user_config_dir=self.user_config_dir)
            ),
        )

    def save(
        self,
        workspace: Path,
        content: str,
        *,
        project: bool,
        name: str = "",
        revision: str = "",
        template: BundleFiles | None = None,
    ) -> dict[str, Any]:
        if len(content.encode()) > 1024 * 1024:
            raise SkillLifecycleError("SKILL.md exceeds 1 MiB.")
        old = None
        files = template or BundleFiles({})
        if name:
            old, path = self.managed(workspace, name, project=project)
            files = read_bundle(path)
            if not revision or files.digest != revision:
                raise SkillLifecycleError(
                    "The skill changed since it was opened. Reload it before saving."
                )
        files = BundleFiles({**files.files, "SKILL.md": (content.encode(), 0o644)})
        with tempfile.TemporaryDirectory(prefix="alysis-skill-edit-") as tmp:
            bundle = Path(tmp) / "bundle"
            files.write(bundle)
            validation = validate_skill_bundle(bundle)
            if not validation.valid:
                raise SkillLifecycleError("\n".join(issue.message for issue in validation.errors))
        if old and old.name != validation.name:
            raise SkillLifecycleError(
                "Keep the installed name when editing; use Create skill for a renamed copy."
            )
        record = old or ManagedSkillRecord(
            name=validation.name,
            bundle_dir=skill_bundle_dir_name(validation.name),
            scope="project" if project else "user",
            source_kind="scaffold",
        )
        self._commit(
            workspace,
            record,
            files,
            force=old is not None,
            disable=bool(set(validation.metadata) - DESCRIPTIVE_FIELDS),
        )
        return {"changed": True, "name": record.name, "revision": files.digest}

    def update_preview(
        self, workspace: Path, name: str, *, project: bool, expected_source: str | None = None
    ) -> dict[str, Any]:
        record, path = self.managed(workspace, name, project=project)
        if expected_source is not None and expected_source != provenance_revision(record):
            raise SkillLifecycleError(
                "The update source changed after review. Open the skill again before checking it."
            )
        current = read_bundle(path).digest
        if not record.source_digest or current != record.source_digest:
            raise SkillLifecycleError(
                "Local edits or a missing import baseline prevent automatic replacement. Import a separate copy to compare."
            )
        if not record.source or record.source_kind == "scaffold":
            raise SkillLifecycleError("This skill has no external update source.")
        result = self.preview(
            workspace=workspace,
            source=record.source,
            subdir=record.source_subdir or "",
            ref=record.source_ref or "",
        )
        draft = self.drafts[result["draft_id"]]
        draft.update = (record, current)
        result["update"] = True
        result["update_available"] = (
            len(draft.bundles) == 1 and next(iter(draft.bundles.values())).digest != current
        )
        return result


def _package_summary(files: BundleFiles) -> dict[str, Any]:
    for filename, family in (
        ("alysis-plugin.toml", "Alysis"),
        ("plugin.json", "Agent Plugins"),
        (".codex-plugin/plugin.json", "Codex"),
        (".cursor-plugin/plugin.json", "Cursor"),
    ):
        if filename not in files.files:
            continue
        try:
            if filename.endswith(".toml"):
                import tomllib

                manifest = tomllib.loads(files.files[filename][0].decode())
            else:
                manifest = json.loads(files.files[filename][0])
            if not isinstance(manifest, dict):
                raise ValueError("Manifest must be an object.")
            components = manifest.get("components", manifest)
            if not isinstance(components, dict):
                raise ValueError("Package components must be an object.")
            declared = sorted(
                key
                for key in (
                    "skills",
                    "mcpServers",
                    "mcp_servers",
                    "hooks",
                    "tools",
                    "rules",
                    "agents",
                    "commands",
                )
                if key in components
            )
            if family == "Alysis":
                from ..extensions.manifest import PluginManifestError, load_manifest

                with tempfile.TemporaryDirectory(prefix="alysis-package-validate-") as tmp:
                    root = Path(tmp) / "package"
                    files.write(root)
                    try:
                        native = load_manifest(root)
                    except PluginManifestError as exc:
                        return {
                            "format": family,
                            "manifest": filename,
                            "valid": False,
                            "notice": str(exc),
                        }
                return {
                    "format": family,
                    "manifest": filename,
                    "valid": True,
                    "name": native.plugin.name,
                    "components": [
                        key for key, value in native.components.model_dump().items() if value
                    ],
                    "review": native.model_dump(mode="json"),
                    "notice": "Review this package's components and permissions before installing. Native package imports are personal.",
                }
            mcp = None
            declared_mcp = manifest.get("mcpServers")
            if isinstance(declared_mcp, dict):
                mcp = {"mcpServers": declared_mcp}
            else:
                config_paths = (
                    [declared_mcp] if isinstance(declared_mcp, str) else ["mcp.json", ".mcp.json"]
                )
                for config_path in config_paths:
                    if config_path.removeprefix("./") in files.files:
                        mcp = json.loads(files.files[config_path.removeprefix("./")][0])
                        break
            return {
                "format": family,
                "manifest": filename,
                "name": str(manifest.get("name") or manifest.get("id") or ""),
                "components": declared,
                "mcp": mcp,
                "notice": "Skills can be imported separately. Other components are not activated by a skill import; connect MCP servers and configure hooks in Customize.",
            }
        except (ValueError, UnicodeDecodeError):
            return {
                "format": family,
                "manifest": filename,
                "notice": "The package manifest is invalid. Review individual skills before importing.",
            }
    return {}
