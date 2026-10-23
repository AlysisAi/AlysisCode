#!/usr/bin/env python3
"""Check public-tree contents and portable relative documentation links."""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

_CACHE_DIRS = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
}
_LOCAL_ROOTS = {
    ".alysis",
    ".sylliptor",
    "artifacts",
    "build",
    "dist",
    "runs",
    "outputs",
    ".qa-results",
    "private-attestation",
}
_BLOCKED_NAMES = {
    ".coverage",
    ".DS_Store",
    "coverage.xml",
}
_BLOCKED_SUFFIXES = {
    ".bak",
    ".db",
    ".har",
    ".jks",
    ".key",
    ".keystore",
    ".log",
    ".p12",
    ".pcap",
    ".pem",
    ".pfx",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".swp",
    ".tmp",
    ".trace",
}
_PUBLIC_EDITOR_PACKAGES = {"vscode-alysis", "jetbrains-alysis", "shared-ui"}
_PUBLIC_VERIFICATION_KEY = "extensions/vscode-alysis/resources/managed-cli-release-public.pem"
_ENV_EXAMPLES = {".env.example", ".env.sample", ".env.template"}
_MAX_TRACKED_FILE_BYTES = 5 * 1024 * 1024
_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")
_DEPLOYMENT_HOST_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+supabase\.(?:co|net|com)\b", re.IGNORECASE)
_DEPLOYMENT_KEY_RE = re.compile(r"\bsb_(?:publishable|secret)_[A-Za-z0-9_-]{12,}\b")
_JSON_TOKEN_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\b")

# Only this public client endpoint is approved for distribution. Keep the
# exception scoped to its exact declaration; other hosts and all keys still fail.
_PUBLIC_CLOUD_DEFAULT_DECLARATION = (
    'DEFAULT_CLOUD_API_URL = "https://'
    + ".".join(("vzigujbcjjmpntxhmyvr", "supabase", "co"))
    + '/functions/v1"'
)


def deployment_content_violations(path_text: str, content: bytes) -> list[str]:
    """Reject deployment hosts and project-bound keys without printing their values.

    Inspect JWT payload segments separately: Python can split a token across
    adjacent string literals. Public anon/publishable keys are excluded too.
    """
    text = content.decode("utf-8", errors="replace")
    violations: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        reasons: set[str] = set()
        approved_default = (
            path_text == "src/alysis_code/alysis_cloud.py"
            and line.strip() == _PUBLIC_CLOUD_DEFAULT_DECLARATION
        )
        if _DEPLOYMENT_HOST_RE.search(line) and not approved_default:
            reasons.add("deployment-specific backend host")
        if _DEPLOYMENT_KEY_RE.search(line):
            reasons.add("deployment-specific backend key")
        for match in _JSON_TOKEN_RE.finditer(line):
            token = match.group()
            try:
                payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(payload, dict) and (
                payload.get("iss") == "supabase"
                or (payload.get("ref") and payload.get("role") in ("anon", "service_role"))
            ):
                reasons.add("deployment-specific backend JWT")
        violations.extend(f"{path_text}:{line_number}: {reason}" for reason in sorted(reasons))
    return violations


def path_violation(path_text: str) -> str | None:
    """Return the public-tree policy violation for a tracked path, if any."""
    path = PurePosixPath(path_text)
    parts = set(path.parts)
    lower_parts = tuple(part.casefold() for part in path.parts)

    if lower_parts[:2] == ("docs", "internal"):
        return "internal documentation directory"
    if lower_parts and lower_parts[0] in {"prompts", "claude outputs", ".claude", ".codex"}:
        return "local assistant working files"
    if lower_parts and lower_parts[0] == "supabase":
        return "hosted-service implementation"
    if (
        lower_parts
        and lower_parts[0] == "extensions"
        and (len(lower_parts) < 2 or lower_parts[1] not in _PUBLIC_EDITOR_PACKAGES)
    ):
        return "unreleased editor package"
    if "power_mode" in lower_parts:
        return "unreleased Power mode package"
    if parts & _CACHE_DIRS:
        return "generated cache directory"
    if path.parts and path.parts[0] in _LOCAL_ROOTS:
        return "local runtime or build-output directory"
    if path.name in _BLOCKED_NAMES:
        return "local generated file"
    if path.name.startswith(".env") and path.name not in _ENV_EXAMPLES:
        return "environment file"
    if path.suffix.lower() in _BLOCKED_SUFFIXES and path_text != _PUBLIC_VERIFICATION_KEY:
        return "credential-bearing or generated file type"
    return None


def collect_violations(paths: Iterable[str], root: Path) -> list[str]:
    """Return sorted policy violations for tracked repository paths."""
    violations: list[str] = []
    for path_text in paths:
        reason = path_violation(path_text)
        if reason:
            violations.append(f"{path_text}: {reason}")
            continue

        local_path = root / path_text
        if path_text == _PUBLIC_VERIFICATION_KEY and local_path.is_file():
            from cryptography.hazmat.primitives.serialization import load_pem_public_key

            try:
                load_pem_public_key(local_path.read_bytes())
            except (ValueError, TypeError):
                violations.append(f"{path_text}: expected a public verification key")
        if local_path.is_file() and local_path.stat().st_size > _MAX_TRACKED_FILE_BYTES:
            violations.append(f"{path_text}: tracked file exceeds 5 MiB")
        elif local_path.is_file():
            violations.extend(deployment_content_violations(path_text, local_path.read_bytes()))
    return sorted(violations)


def _is_host_navigation_target(relative_target: str) -> bool:
    """Return whether an escaping link denotes a repository-host route, not a file."""
    path = PurePosixPath(relative_target.rstrip("/"))
    return bool(path.parts) and path.name not in {".", ".."} and not path.suffix


def _path_exists_with_exact_case(path: Path, root: Path) -> bool:
    """Check each link component even on a case-insensitive filesystem."""
    current = root
    for part in path.relative_to(root).parts:
        if part == "..":
            current = current.resolve().parent
            continue
        if not current.is_dir() or part not in {entry.name for entry in current.iterdir()}:
            return False
        current = current / part
    return current.exists()


def markdown_link_violations(paths: Iterable[str], root: Path) -> list[str]:
    """Return missing, miscapitalized, or repository-escaping relative Markdown links."""
    root = root.resolve()
    violations: list[str] = []
    for path_text in paths:
        if PurePosixPath(path_text).suffix.lower() != ".md":
            continue
        markdown_path = root / path_text
        if not markdown_path.is_file():
            continue

        in_fence = False
        for line_number, line in enumerate(
            markdown_path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            stripped = line.lstrip()
            if stripped.startswith(("```", "~~~")):
                in_fence = not in_fence
                continue
            if in_fence:
                continue

            for match in _MARKDOWN_LINK_RE.finditer(line):
                raw_target = match.group(1).strip("<>")
                parsed = urlsplit(raw_target)
                if parsed.scheme or parsed.netloc or raw_target.startswith(("#", "/")):
                    continue
                relative_target = unquote(parsed.path)
                if not relative_target:
                    continue
                target = markdown_path.parent / relative_target
                resolved = target.resolve()
                try:
                    resolved.relative_to(root)
                except ValueError:
                    # Hosted Markdown may link to extensionless repository routes such as
                    # ``../../issues``. Those are URL paths, so local filesystem checks cannot
                    # validate them. Escaping links that look like files remain violations.
                    if _is_host_navigation_target(relative_target):
                        continue
                    violations.append(
                        f"{path_text}:{line_number}: relative link escapes repository: {raw_target}"
                    )
                    continue
                if not _path_exists_with_exact_case(target, root):
                    violations.append(
                        f"{path_text}:{line_number}: broken relative link: {raw_target}"
                    )
    return sorted(violations)


def _git_lines(root: Path, *args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    repository_paths = _git_lines(
        root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
    )
    repository_paths = [
        path for path in repository_paths if (root / path).exists() or (root / path).is_symlink()
    ]
    violations = collect_violations(repository_paths, root)
    violations.extend(markdown_link_violations(repository_paths, root))

    for path_text in _git_lines(root, "ls-files", "-ci", "--exclude-standard"):
        if (root / path_text).exists() or (root / path_text).is_symlink():
            violations.append(f"{path_text}: tracked file is excluded by .gitignore")

    if violations:
        print("Public repository hygiene check failed:", file=sys.stderr)
        for violation in sorted(set(violations)):
            print(f"- {violation}", file=sys.stderr)
        return 1

    print(f"Public repository hygiene check passed ({len(repository_paths)} repository files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
