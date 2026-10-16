from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from ..command_text import command_text
from ..file_classification import BROAD_SOURCE_EXTENSIONS, CODE_SCAN_SKIP_DIR_NAMES
from ..repo_scan import scan_workspace
from ..workspace_context import resolve_workspace_context
from .symbols import SymbolSearchError, symbol_search
from .test_discovery import test_discover

_MAX_FILE_BYTES = 512 * 1024
_MAX_SCAN_FILES = 900
_GIT_TIMEOUT_S = 2.0
_IMPORT_RE = re.compile(
    r"""(?:import\s+[^'"]+\s+from\s+|export\s+[^'"]+\s+from\s+|require\()\s*['"]([^'"]+)['"]"""
)
_DYNAMIC_IMPORT_RE = re.compile(r"""import\(\s*['"]([^'"]+)['"]\s*\)""")
_DEFAULT_SKIP_DIRS = set(CODE_SCAN_SKIP_DIR_NAMES) | {
    ".git",
    ".hg",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "vendor",
}


def repo_map(
    *,
    root: Path,
    paths: list[str] | None = None,
    symbols: list[str] | None = None,
    include_tests: bool = True,
    include_imports: bool = True,
    include_references: bool = False,
    depth: int = 2,
    max_items: int = 80,
) -> dict[str, Any]:
    root_abs = root.resolve()
    safe_max = max(1, min(int(max_items), 200))
    safe_depth = max(0, min(int(depth), 4))
    normalized_paths = _normalized_input_paths(root=root_abs, paths=paths or [])
    normalized_symbols = [str(symbol).strip() for symbol in (symbols or []) if str(symbol).strip()]
    if not normalized_paths:
        normalized_paths = _git_changed_paths(root_abs)[:safe_max]

    symbol_matches = _symbol_matches(root_abs, normalized_symbols, safe_max=max(1, safe_max // 3))
    for match in symbol_matches:
        path = str(match.get("path") or "").strip()
        if path and path not in normalized_paths:
            normalized_paths.append(path)
            if len(normalized_paths) >= safe_max:
                break

    related = _RelatedCollector(limit=safe_max)
    for rel_path in normalized_paths:
        related.add(rel_path, reason="input path", confidence=1.0, source="input")
    for match in symbol_matches:
        path = str(match.get("path") or "").strip()
        if path:
            related.add(
                path, reason="matches requested symbol", confidence=0.88, source="symbol_search"
            )

    import_edges: list[dict[str, Any]] = []
    if include_imports and normalized_paths:
        import_edges = _collect_import_graph(
            root=root_abs,
            seed_paths=normalized_paths,
            related=related,
            max_depth=safe_depth,
            max_edges=safe_max,
        )

    test_result: dict[str, Any] = {}
    if include_tests:
        test_result = test_discover(
            root=root_abs,
            paths=normalized_paths,
            symbols=normalized_symbols,
            include_commands=True,
            max_results=min(safe_max, 50),
        )
        for test in test_result.get("candidate_tests") or []:
            if not isinstance(test, dict):
                continue
            path = str(test.get("path") or "").strip()
            if path:
                related.add(
                    path,
                    reason=str(test.get("reason") or "candidate test"),
                    confidence=_float(test.get("confidence"), fallback=0.7),
                    source="test_discover",
                )

    references: list[dict[str, Any]] = []
    if include_references:
        references = _collect_reference_hints(
            root=root_abs,
            paths=normalized_paths,
            symbols=normalized_symbols,
            max_items=min(safe_max, 50),
        )
        for ref in references:
            path = str(ref.get("path") or "").strip()
            if path:
                related.add(
                    path, reason="contains reference hint", confidence=0.55, source="reference"
                )

    scan = scan_workspace(context=resolve_workspace_context(root_abs)).to_dict()
    broad_commands = _dedupe(
        [
            *[str(item) for item in test_result.get("broad_commands") or [] if str(item).strip()],
            *[str(item) for item in scan.get("likely_test_commands") or [] if str(item).strip()],
        ]
    )[:10]

    return {
        "paths": normalized_paths[:safe_max],
        "symbols": normalized_symbols,
        "related_files": related.items(),
        "import_edges": import_edges[:safe_max],
        "candidate_tests": list(test_result.get("candidate_tests") or [])[:safe_max],
        "candidate_commands": list(test_result.get("candidate_commands") or [])[:safe_max],
        "broad_commands": broad_commands,
        "symbol_matches": symbol_matches[:safe_max],
        "references": references[:safe_max],
        "language_hints": list(scan.get("language_hints") or [])[:10],
        "manifests": list(scan.get("manifests") or [])[:10],
        "depth": safe_depth,
        "heuristic": True,
    }


class _RelatedCollector:
    def __init__(self, *, limit: int) -> None:
        self.limit = limit
        self._items: dict[str, dict[str, Any]] = {}

    def add(self, path: str, *, reason: str, confidence: float, source: str) -> None:
        clean_path = str(path or "").strip().replace("\\", "/")
        while clean_path.startswith("./"):
            clean_path = clean_path[2:]
        if not clean_path or clean_path == ".":
            return
        existing = self._items.get(clean_path)
        if existing is None:
            if len(self._items) >= self.limit:
                return
            self._items[clean_path] = {
                "path": clean_path,
                "confidence": round(max(0.0, min(1.0, confidence)), 2),
                "reasons": [reason],
                "sources": [source],
            }
            return
        existing["confidence"] = round(
            max(float(existing.get("confidence") or 0.0), max(0.0, min(1.0, confidence))),
            2,
        )
        reasons = existing.setdefault("reasons", [])
        if reason and reason not in reasons:
            reasons.append(reason)
        sources = existing.setdefault("sources", [])
        if source and source not in sources:
            sources.append(source)

    def items(self) -> list[dict[str, Any]]:
        return sorted(
            self._items.values(),
            key=lambda item: (-float(item.get("confidence") or 0.0), str(item.get("path") or "")),
        )


def _float(value: Any, *, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        item = str(raw or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _normalized_input_paths(*, root: Path, paths: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw in paths:
        value = str(raw or "").strip()
        if not value:
            continue
        value = value.replace("\\", "/")
        candidate = Path(value)
        try:
            resolved = (
                candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
            )
            value = resolved.relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
        if value and value != ".":
            normalized.append(value)
    return _dedupe(normalized)


def _git_changed_paths(root: Path) -> list[str]:
    try:
        cp = subprocess.run(
            ["git", "diff", "--name-only", "HEAD", "--"],
            cwd=root,
            capture_output=True,
            **command_text(),
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if cp.returncode != 0:
        return []
    return _dedupe([line.strip() for line in cp.stdout.splitlines() if line.strip()])


def _symbol_matches(root: Path, symbols: list[str], *, safe_max: int) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            result = symbol_search(root=root, query=symbol, max_results=safe_max, exact=False)
        except SymbolSearchError:
            continue
        for match in result.get("matches") or []:
            if isinstance(match, dict):
                matches.append(dict(match))
                if len(matches) >= safe_max:
                    return matches
    return matches


def _collect_import_graph(
    *,
    root: Path,
    seed_paths: list[str],
    related: _RelatedCollector,
    max_depth: int,
    max_edges: int,
) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    frontier = list(seed_paths)
    seen_paths: set[str] = set()
    depth = 0
    while frontier and depth <= max_depth and len(edges) < max_edges:
        next_frontier: list[str] = []
        for rel_path in frontier:
            if rel_path in seen_paths:
                continue
            seen_paths.add(rel_path)
            file_path = (root / rel_path).resolve()
            try:
                file_path.relative_to(root)
            except ValueError:
                continue
            if not file_path.is_file() or _too_large(file_path):
                continue
            imports = _imports_for_file(root=root, rel_path=rel_path, path=file_path)
            for target, import_name in imports:
                edge = {"from": rel_path, "to": target, "import": import_name, "depth": depth}
                if edge not in edges:
                    edges.append(edge)
                related.add(
                    target, reason=f"imported by {rel_path}", confidence=0.72, source="import"
                )
                if target not in seen_paths and target not in next_frontier:
                    next_frontier.append(target)
                if len(edges) >= max_edges:
                    break
        frontier = next_frontier
        depth += 1
    return edges


def _too_large(path: Path) -> bool:
    try:
        return path.stat().st_size > _MAX_FILE_BYTES
    except OSError:
        return True


def _imports_for_file(*, root: Path, rel_path: str, path: Path) -> list[tuple[str, str]]:
    suffix = path.suffix.lower()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if suffix == ".py":
        return _python_imports(root=root, rel_path=rel_path, text=text)
    if suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
        return _js_ts_imports(root=root, rel_path=rel_path, text=text)
    return []


def _python_imports(*, root: Path, rel_path: str, text: str) -> list[tuple[str, str]]:
    try:
        tree = ast.parse(text, filename=rel_path)
    except SyntaxError:
        return []
    out: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = _resolve_python_module(root=root, module=alias.name)
                if target:
                    out.append((target, alias.name))
            continue
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            candidates = []
            if node.level:
                candidates.extend(_resolve_relative_python_modules(rel_path, module, node.level))
            if module:
                candidates.append(module)
            for candidate in candidates:
                target = _resolve_python_module(root=root, module=candidate)
                if target:
                    out.append((target, "." * node.level + module if node.level else module))
                    break
    return _dedupe_edges(out)


def _resolve_relative_python_modules(rel_path: str, module: str, level: int) -> list[str]:
    parts = list(PurePosixPath(rel_path).parent.parts)
    keep = max(0, len(parts) - max(0, level - 1))
    base = parts[:keep]
    module_parts = [part for part in module.split(".") if part]
    if module_parts:
        return [".".join([*base, *module_parts])]
    return [".".join(base)] if base else []


def _resolve_python_module(*, root: Path, module: str) -> str | None:
    parts = [part for part in module.split(".") if part]
    if not parts:
        return None
    candidates = [
        Path(*parts).with_suffix(".py"),
        Path(*parts) / "__init__.py",
        Path("src", *parts).with_suffix(".py"),
        Path("src", *parts) / "__init__.py",
    ]
    for candidate in candidates:
        path = root / candidate
        if path.is_file():
            return candidate.as_posix()
    return None


def _js_ts_imports(*, root: Path, rel_path: str, text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for pattern in (_IMPORT_RE, _DYNAMIC_IMPORT_RE):
        for match in pattern.finditer(text):
            spec = match.group(1)
            if not spec.startswith("."):
                continue
            target = _resolve_relative_js_import(root=root, rel_path=rel_path, spec=spec)
            if target:
                out.append((target, spec))
    return _dedupe_edges(out)


def _resolve_relative_js_import(*, root: Path, rel_path: str, spec: str) -> str | None:
    base = PurePosixPath(rel_path).parent
    raw = (base / spec).as_posix()
    candidates: list[str] = []
    candidate_path = PurePosixPath(raw)
    if candidate_path.suffix:
        candidates.append(candidate_path.as_posix())
    else:
        for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            candidates.append(candidate_path.with_suffix(ext).as_posix())
        for ext in (".ts", ".tsx", ".js", ".jsx"):
            candidates.append((candidate_path / f"index{ext}").as_posix())
    for candidate in candidates:
        if (root / candidate).is_file():
            return candidate
    return None


def _dedupe_edges(edges: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for edge in edges:
        if edge in seen:
            continue
        seen.add(edge)
        out.append(edge)
    return out


def _collect_reference_hints(
    *,
    root: Path,
    paths: list[str],
    symbols: list[str],
    max_items: int,
) -> list[dict[str, Any]]:
    needles = _dedupe(
        [
            *[PurePosixPath(path).stem for path in paths if path],
            *[symbol.split(".")[-1] for symbol in symbols if symbol],
        ]
    )
    if not needles:
        return []
    out: list[dict[str, Any]] = []
    files_scanned = 0
    for path in _iter_source_files(root):
        files_scanned += 1
        if files_scanned > _MAX_SCAN_FILES:
            break
        if _too_large(path):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        for lineno, line in enumerate(lines, start=1):
            for needle in needles:
                if needle and needle in line:
                    out.append(
                        {
                            "path": rel,
                            "line": lineno,
                            "text": line.strip()[:240],
                            "needle": needle,
                        }
                    )
                    break
            if len(out) >= max_items:
                return out
    return out


def _iter_source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(dirname for dirname in dirnames if dirname not in _DEFAULT_SKIP_DIRS)
        for filename in sorted(filenames):
            path = Path(current_root) / filename
            rel = path.relative_to(root).as_posix()
            if path.suffix.lower() in BROAD_SOURCE_EXTENSIONS and not rel.startswith("."):
                files.append(path)
            if len(files) >= _MAX_SCAN_FILES:
                return files
    return files


repo_map.__test__ = False
