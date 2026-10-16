from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .atomic_io import atomic_write_json
from .branding import env_get_from
from .command_text import command_text
from .git_safe import build_git_cmd


class RemoteSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteSettings:
    sync_mode: str
    remote_name: str
    create_pr: bool
    provider: str

    @property
    def enabled(self) -> bool:
        return self.sync_mode in {"warn", "strict"}

    @property
    def strict(self) -> bool:
        return self.sync_mode == "strict"


def truncate_output(text: str, *, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...(truncated)"


def safe_remote_filename(task_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in {"-", "_"} else "_" for c in task_id)
    return safe or "task"


def init_remote_record(*, task_id: str, remote: str, provider: str) -> dict[str, object]:
    return {
        "task_id": task_id,
        "remote": remote,
        "provider": provider,
        "pushed_branch": False,
        "branch_push_output": "",
        "created_pr": False,
        "pr_url": None,
        "pr_number_or_iid": None,
        "pr_output": "",
        "pushed_base": False,
        "base_push_output": "",
        "errors": [],
    }


def write_remote_record(*, execution_dir: Path, task_id: str, record: dict[str, object]) -> Path:
    remote_dir = execution_dir / "remote"
    remote_dir.mkdir(parents=True, exist_ok=True)
    out = remote_dir / f"{safe_remote_filename(task_id)}.json"
    atomic_write_json(out, record)
    return out


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
) -> tuple[bool, str]:
    try:
        cp = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            **command_text(),
            cwd=os.fspath(cwd) if cwd is not None else None,
        )
    except OSError as e:
        return False, f"failed to run command: {e}"
    out = (cp.stdout or "").strip()
    err = (cp.stderr or "").strip()
    combined = out
    if err:
        combined = f"{combined}\n{err}".strip()
    return cp.returncode == 0, combined


def load_remote_settings_from_env(env: Mapping[str, str] | None = None) -> RemoteSettings:
    source = env if env is not None else os.environ

    raw_mode = str(env_get_from(source, "ALYSIS_REMOTE_SYNC", "off") or "off").strip().lower()
    if raw_mode not in {"off", "warn", "strict"}:
        raw_mode = "off"

    remote_name = str(env_get_from(source, "ALYSIS_REMOTE_NAME", "origin") or "origin").strip()
    remote_name = remote_name or "origin"

    create_pr_raw = str(env_get_from(source, "ALYSIS_REMOTE_CREATE_PR", "0") or "0").strip().lower()
    create_pr = create_pr_raw in {"1", "true", "yes", "on"}

    provider = str(env_get_from(source, "ALYSIS_REMOTE_PROVIDER", "auto") or "auto").strip().lower()
    if provider not in {"auto", "github", "gitlab", "none"}:
        provider = "auto"

    return RemoteSettings(
        sync_mode=raw_mode,
        remote_name=remote_name,
        create_pr=create_pr,
        provider=provider,
    )


def get_remote_url(root: Path, remote: str) -> str:
    ok, output = _run(build_git_cmd(root, ["remote", "get-url", remote]))
    if not ok:
        raise RemoteSyncError(f"failed to get remote URL for {remote}: {output or 'unknown error'}")
    return output.strip()


def detect_provider_from_remote_url(remote_url: str) -> str:
    url = (remote_url or "").strip().lower()
    if not url:
        return "unknown"
    if "github.com" in url or url.startswith("git@github") or "ssh://git@github" in url:
        return "github"
    if "gitlab.com" in url or url.startswith("git@gitlab") or "ssh://git@gitlab" in url:
        return "gitlab"
    return "unknown"


def resolve_provider(*, settings_provider: str, remote_url: str) -> str:
    if settings_provider == "none":
        return "unknown"
    if settings_provider in {"github", "gitlab"}:
        return settings_provider
    return detect_provider_from_remote_url(remote_url)


def _desktop_remote_refusal() -> str | None:
    # Desktop credentials belong to Electron main. This standalone/Forge helper has no
    # negotiated host-action channel and must never fall back to gh or Git credentials.
    if "ALYSIS_DESKTOP_GITHUB_POLICY" in os.environ:
        return (
            "Desktop remote sync requires the selected-account GitHub host broker. "
            "Use the negotiated github_* chat tools; standalone remote sync is unavailable "
            "in this desktop session."
        )
    return None


def push_branch(root: Path, *, remote: str, branch: str) -> tuple[bool, str]:
    if refusal := _desktop_remote_refusal():
        return False, refusal
    return _run(build_git_cmd(root, ["push", remote, branch]))


def push_base(root: Path, *, remote: str, base_branch: str) -> tuple[bool, str]:
    if refusal := _desktop_remote_refusal():
        return False, refusal
    return _run(build_git_cmd(root, ["push", remote, base_branch]))


def _extract_url(text: str) -> str | None:
    pattern = re.compile(r"https?://\S+")
    match = pattern.search(text)
    return match.group(0) if match else None


def _extract_number_from_url(url: str | None) -> str | None:
    if not url:
        return None
    cleaned = url.rstrip("/").split("/")[-1]
    return cleaned if cleaned.isdigit() else None


def _parse_json_list(raw: str) -> list[dict[str, Any]]:
    text = raw.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


def find_existing_pr_or_mr(
    root: Path,
    *,
    provider: str,
    base_branch: str,
    head_branch: str,
) -> tuple[bool, str | None, str | None, str]:
    if refusal := _desktop_remote_refusal():
        return False, None, None, refusal
    if provider == "github":
        if shutil.which("gh") is None:
            return False, None, None, "gh CLI not available"
        ok, output = _run(
            [
                "gh",
                "pr",
                "list",
                "--base",
                base_branch,
                "--head",
                head_branch,
                "--json",
                "number,url",
                "--limit",
                "1",
            ],
            cwd=root,
        )
        if not ok:
            return False, None, None, output
        items = _parse_json_list(output)
        if items:
            first = items[0]
            number = first.get("number")
            url = str(first.get("url") or "").strip() or _extract_url(output)
            pr_id = str(number).strip() if number is not None else _extract_number_from_url(url)
            return bool(url), url or None, pr_id, output
        return False, None, None, output

    if provider == "gitlab":
        if shutil.which("glab") is None:
            return False, None, None, "glab CLI not available"
        ok, output = _run(
            [
                "glab",
                "mr",
                "list",
                "--source-branch",
                head_branch,
                "--target-branch",
                base_branch,
                "--output",
                "json",
            ],
            cwd=root,
        )
        if not ok:
            return False, None, None, output
        items = _parse_json_list(output)
        if items:
            first = items[0]
            iid = first.get("iid")
            url = (
                str(first.get("web_url") or "").strip()
                or str(first.get("url") or "").strip()
                or _extract_url(output)
            )
            pr_id = str(iid).strip() if iid is not None else _extract_number_from_url(url)
            return bool(url), url or None, pr_id, output
        return False, None, None, output

    return False, None, None, "provider unsupported for PR/MR lookup"


def create_pr_or_mr(
    root: Path,
    *,
    provider: str,
    base_branch: str,
    head_branch: str,
    title: str,
    body: str,
) -> tuple[bool, str | None, str]:
    if refusal := _desktop_remote_refusal():
        return False, None, refusal
    if provider == "github":
        if shutil.which("gh") is None:
            return False, None, "gh CLI not available"
        ok, output = _run(
            [
                "gh",
                "pr",
                "create",
                "--base",
                base_branch,
                "--head",
                head_branch,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=root,
        )
        return ok, _extract_url(output), output

    if provider == "gitlab":
        if shutil.which("glab") is None:
            return False, None, "glab CLI not available"
        ok, output = _run(
            [
                "glab",
                "mr",
                "create",
                "--source-branch",
                head_branch,
                "--target-branch",
                base_branch,
                "--title",
                title,
                "--description",
                body,
            ],
            cwd=root,
        )
        return ok, _extract_url(output), output

    return False, None, "provider unsupported for PR/MR creation"


def ensure_pr_or_mr(
    root: Path,
    *,
    provider: str,
    base_branch: str,
    head_branch: str,
    title: str,
    body: str,
) -> tuple[bool, str | None, str | None, str]:
    found, existing_url, existing_id, existing_output = find_existing_pr_or_mr(
        root,
        provider=provider,
        base_branch=base_branch,
        head_branch=head_branch,
    )
    if found:
        return True, existing_url, existing_id, existing_output

    created, created_url, created_output = create_pr_or_mr(
        root,
        provider=provider,
        base_branch=base_branch,
        head_branch=head_branch,
        title=title,
        body=body,
    )
    if created:
        created_id = _extract_number_from_url(created_url)
        return True, created_url, created_id, created_output

    found_after, existing_url_after, existing_id_after, existing_output_after = (
        find_existing_pr_or_mr(
            root,
            provider=provider,
            base_branch=base_branch,
            head_branch=head_branch,
        )
    )
    if found_after:
        combined = f"{created_output}\n\n(reused existing PR/MR)\n{existing_output_after}".strip()
        return True, existing_url_after, existing_id_after, combined

    return False, None, None, created_output
