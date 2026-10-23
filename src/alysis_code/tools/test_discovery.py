from __future__ import annotations

import json
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from ..command_text import command_text
from ..repo_scan import scan_workspace
from ..verification_command_analysis import analyze_verification_command
from ..workspace_context import resolve_workspace_context
from ..workspace_provisioning import detect_declared_test_runner


class TestDiscoveryError(RuntimeError):
    pass


_PY_EXTENSIONS = {".py"}
_JS_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
_SOURCE_PREFIXES = ("src/", "lib/", "app/", "packages/")
_MAX_TEST_FILE_SCAN = 600
_GIT_TIMEOUT_S = 2.0


def test_discover(
    *,
    root: Path,
    paths: list[str] | None = None,
    symbols: list[str] | None = None,
    changed_only: bool = False,
    include_commands: bool = True,
    max_results: int = 20,
    failure_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root_abs = root.resolve()
    safe_max = max(1, min(int(max_results), 100))
    normalized_paths = _normalized_input_paths(root=root_abs, paths=paths or [])
    if changed_only and not normalized_paths:
        normalized_paths = _git_changed_paths(root_abs)

    summary_paths, summary_tests = _paths_from_failure_summary(
        root=root_abs,
        failure_summary=failure_summary,
    )
    normalized_paths = _dedupe([*normalized_paths, *summary_paths])

    candidate_tests: list[dict[str, Any]] = []
    candidate_commands: list[dict[str, Any]] = []
    broad_commands = _broad_commands(root_abs)
    frameworks = _frameworks_from_commands(broad_commands, root=root_abs)
    declared_runner = detect_declared_test_runner(root_abs)
    if declared_runner is not None:
        frameworks.add(declared_runner.package)
    summary_framework = (
        str(failure_summary.get("framework") or "").strip().casefold()
        if isinstance(failure_summary, dict)
        else ""
    )
    if summary_framework in {"pytest", "unittest"}:
        frameworks.add(summary_framework)

    for test in summary_tests:
        candidate_tests.append(test)
        path = str(test.get("path") or "")
        if path.endswith(".py") and summary_framework == "pytest":
            if include_commands:
                node_id = str(test.get("id") or path)
                candidate_commands.append(
                    _command(
                        f"python -m pytest {node_id} -q",
                        scope="targeted",
                        confidence=0.92,
                        reason="failing pytest nodeid reported by verification output",
                    )
                )

    test_files = _iter_test_files(root_abs)
    for rel_path in normalized_paths:
        suffix = PurePosixPath(rel_path).suffix.lower()
        if suffix in _PY_EXTENSIONS:
            candidate_tests.extend(_python_test_candidates(root_abs, rel_path, test_files))
        elif suffix in _JS_EXTENSIONS:
            frameworks.update(_node_frameworks(root_abs))
            candidate_tests.extend(_node_test_candidates(root_abs, rel_path, test_files))
        elif suffix == ".go":
            frameworks.add("go test")
            if include_commands:
                candidate_commands.append(_go_test_command(rel_path))
        elif suffix == ".rs":
            frameworks.add("cargo test")
            if include_commands:
                candidate_commands.append(
                    _command(
                        "cargo test", scope="broad", confidence=0.55, reason="Rust source changed"
                    )
                )
        elif suffix == ".java":
            frameworks.add("junit")
            candidate_tests.extend(_java_test_candidates(rel_path, test_files))

    if symbols:
        candidate_tests.extend(
            _symbol_named_test_candidates(symbols=symbols, test_files=test_files)
        )

    candidate_tests = _dedupe_test_candidates(candidate_tests)[:safe_max]
    if include_commands:
        candidate_commands.extend(
            _commands_for_tests(root_abs, candidate_tests, frameworks=frameworks)
        )
        if not candidate_commands:
            candidate_commands.extend(broad_commands)
        candidate_commands = _dedupe_commands(candidate_commands)[:safe_max]

    return {
        "paths": normalized_paths,
        "symbols": [str(symbol) for symbol in (symbols or []) if str(symbol).strip()],
        "frameworks": sorted(frameworks),
        "candidate_tests": candidate_tests,
        "candidate_commands": candidate_commands if include_commands else [],
        "broad_commands": [item["command"] for item in broad_commands] if include_commands else [],
        "changed_only": bool(changed_only),
        "heuristic": True,
        "discovery_note": (
            "Repository/test hints only. Runner availability is unverified; "
            "this tool does not execute tests."
        ),
    }


def _normalized_input_paths(*, root: Path, paths: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw in paths:
        value = _normalize_repo_relative_path(root=root, raw_path=str(raw or ""))
        if value:
            normalized.append(value)
    return _dedupe(normalized)


def _normalize_repo_relative_path(*, root: Path, raw_path: str) -> str:
    value = str(raw_path or "").strip().strip("'\"")
    if not value:
        return ""
    value = value.replace("\\", "/")
    candidate = Path(value)
    try:
        resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        relative = resolved.relative_to(root).as_posix()
    except (OSError, ValueError):
        return ""
    return "" if relative == "." else relative


def _normalize_pytest_nodeid(*, root: Path, raw_nodeid: str) -> str:
    nodeid = str(raw_nodeid or "").strip()
    if not nodeid:
        return ""
    path_part, sep, suffix = nodeid.partition("::")
    if not path_part.endswith(".py"):
        normalized_prefix = path_part.replace("\\", "/")
        path_parts = {part for part in normalized_prefix.split("/") if part}
        if sep and ("/" in normalized_prefix or ".." in path_parts):
            return ""
        return nodeid
    normalized_path = _normalize_repo_relative_path(root=root, raw_path=path_part)
    if not normalized_path:
        return ""
    return f"{normalized_path}{sep}{suffix}" if sep else normalized_path


def _path_from_normalized_pytest_nodeid(nodeid: str) -> str:
    path_part, sep, _suffix = str(nodeid or "").partition("::")
    if sep and path_part.endswith(".py"):
        return path_part
    return ""


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


def _paths_from_failure_summary(
    *,
    root: Path,
    failure_summary: dict[str, Any] | None,
) -> tuple[list[str], list[dict[str, Any]]]:
    if not isinstance(failure_summary, dict):
        return ([], [])
    paths: list[str] = []
    tests: list[dict[str, Any]] = []
    raw_tests = failure_summary.get("failing_tests")
    if isinstance(raw_tests, list):
        for raw in raw_tests:
            if not isinstance(raw, dict):
                continue
            path = _normalize_repo_relative_path(root=root, raw_path=str(raw.get("path") or ""))
            test_id = _normalize_pytest_nodeid(root=root, raw_nodeid=str(raw.get("id") or ""))
            if not path:
                path = _path_from_normalized_pytest_nodeid(test_id)
            if path:
                paths.append(path)
            if not path and not test_id:
                continue
            item: dict[str, Any] = {
                "path": path,
                "confidence": 0.95,
                "reason": "failing test reported by verification output",
            }
            if test_id:
                item["id"] = test_id
            if raw.get("line") is not None:
                item["line"] = raw.get("line")
            message = str(raw.get("message") or "").strip()
            if message:
                item["message"] = message[:240]
            if path or test_id:
                tests.append(item)
    for key in ("likely_next_files", "stack_frames"):
        raw_items = failure_summary.get(key)
        if not isinstance(raw_items, list):
            continue
        for raw in raw_items:
            if isinstance(raw, dict):
                path = _normalize_repo_relative_path(root=root, raw_path=str(raw.get("path") or ""))
            else:
                path = _normalize_repo_relative_path(root=root, raw_path=str(raw or ""))
            if path:
                paths.append(path)
    return (_dedupe(paths), tests)


def _iter_test_files(root: Path) -> list[str]:
    files: list[str] = []
    for path in root.rglob("*"):
        if len(files) >= _MAX_TEST_FILE_SCAN:
            break
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if _is_ignored(rel):
            continue
        name = path.name.lower()
        suffix = path.suffix.lower()
        if (
            rel.startswith(("tests/", "test/"))
            or "__tests__/" in rel
            or name.startswith("test_")
            or name.endswith("_test.py")
            or ".test." in name
            or ".spec." in name
            or name.endswith("test.go")
            or name.endswith("_test.rs")
            or name.endswith("test.java")
        ) and suffix in _PY_EXTENSIONS | _JS_EXTENSIONS | {".go", ".rs", ".java"}:
            files.append(rel)
    return sorted(files)


def _python_test_candidates(
    root: Path, rel_path: str, test_files: list[str]
) -> list[dict[str, Any]]:
    pure = PurePosixPath(rel_path)
    stem = pure.stem
    module = _python_module_name(rel_path)
    candidates: list[dict[str, Any]] = []
    for test_file in test_files:
        name = PurePosixPath(test_file).name
        score = 0.0
        reasons: list[str] = []
        if test_file == rel_path:
            score = 0.98
            reasons.append("input path is already a Python test file")
        if name in {f"test_{stem}.py", f"{stem}_test.py"}:
            score = max(score, 0.86)
            reasons.append("test filename mirrors source filename")
        if f"/test_{stem}.py" in test_file or f"/{stem}_test.py" in test_file:
            score = max(score, 0.78)
            reasons.append("test path mirrors source filename")
        if module and _file_contains(root / test_file, module):
            score = max(score, 0.9)
            reasons.append(f"test imports or references {module}")
        if score:
            candidates.append(
                {
                    "path": test_file,
                    "confidence": round(score, 2),
                    "reason": "; ".join(reasons),
                }
            )
    return candidates


def _node_test_candidates(root: Path, rel_path: str, test_files: list[str]) -> list[dict[str, Any]]:
    stem = PurePosixPath(rel_path).stem
    candidates: list[dict[str, Any]] = []
    for test_file in test_files:
        name = PurePosixPath(test_file).name.lower()
        score = 0.0
        reasons: list[str] = []
        if test_file == rel_path:
            score = 0.98
            reasons.append("input path is already a JS/TS test file")
        if name.startswith(stem.lower() + ".") and (".test." in name or ".spec." in name):
            score = max(score, 0.84)
            reasons.append("test filename mirrors source filename")
        if _file_contains(root / test_file, PurePosixPath(rel_path).stem):
            score = max(score, 0.68)
            reasons.append("test references source basename")
        if score:
            candidates.append(
                {
                    "path": test_file,
                    "confidence": round(score, 2),
                    "reason": "; ".join(reasons),
                }
            )
    return candidates


def _java_test_candidates(rel_path: str, test_files: list[str]) -> list[dict[str, Any]]:
    stem = PurePosixPath(rel_path).stem
    return [
        {
            "path": test_file,
            "confidence": 0.72,
            "reason": "Java test class mirrors source class name",
        }
        for test_file in test_files
        if PurePosixPath(test_file).name == f"{stem}Test.java"
    ]


def _symbol_named_test_candidates(
    *,
    symbols: list[str],
    test_files: list[str],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    lowered_symbols = [str(symbol).strip().casefold() for symbol in symbols if str(symbol).strip()]
    for test_file in test_files:
        test_name = PurePosixPath(test_file).stem.casefold()
        for symbol in lowered_symbols:
            token = symbol.split(".")[-1]
            if token and token in test_name:
                candidates.append(
                    {
                        "path": test_file,
                        "confidence": 0.62,
                        "reason": f"test filename mentions symbol {symbol}",
                    }
                )
    return candidates


def _commands_for_tests(
    root: Path,
    candidate_tests: list[dict[str, Any]],
    *,
    frameworks: set[str],
) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    for candidate in candidate_tests:
        path = str(candidate.get("path") or "").strip()
        if not path:
            continue
        suffix = PurePosixPath(path).suffix.lower()
        confidence = float(candidate.get("confidence") or 0.5)
        if suffix == ".py" and "pytest" in frameworks:
            commands.append(
                _command(
                    f"python -m pytest {path} -q",
                    scope="targeted",
                    confidence=min(0.9, max(confidence, 0.55)),
                    reason=f"pytest candidate for {path}",
                )
            )
        elif suffix in _JS_EXTENSIONS:
            package_command = _node_test_command(root, path)
            if package_command:
                commands.append(package_command)
        elif suffix == ".java":
            commands.extend(_java_commands(root, path))
    return commands


def _go_test_command(rel_path: str) -> dict[str, Any]:
    parent = PurePosixPath(rel_path).parent.as_posix()
    package = "./" + parent if parent not in {"", "."} else "."
    return _command(
        f"go test {package}",
        scope="targeted",
        confidence=0.78,
        reason="Go tests run at package directory granularity",
    )


def _node_test_command(root: Path, path: str) -> dict[str, Any] | None:
    script = _package_test_script(root)
    if not script:
        return None
    manager = _node_package_manager(root)
    if manager == "npm":
        command = f"npm test -- {path}"
    elif manager == "yarn":
        command = f"yarn test {path}"
    elif manager == "pnpm":
        command = f"pnpm test -- {path}"
    else:
        command = f"bun test {path}"
    return _command(
        command,
        scope="targeted",
        confidence=0.58,
        reason=f"{manager} test script exists; path filtering is framework-dependent",
    )


def _java_commands(root: Path, path: str) -> list[dict[str, Any]]:
    stem = PurePosixPath(path).stem
    if (root / "gradlew").exists():
        return [
            _command(
                f"./gradlew test --tests '*{stem}'",
                scope="targeted",
                confidence=0.56,
                reason="Gradle wrapper exists and Java test class was matched",
            )
        ]
    if (root / "pom.xml").exists():
        return [
            _command(
                f"mvn -Dtest={stem} test",
                scope="targeted",
                confidence=0.56,
                reason="Maven project and Java test class was matched",
            )
        ]
    return []


def _broad_commands(root: Path) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    try:
        scan = scan_workspace(context=resolve_workspace_context(root))
    except Exception:
        scan = None
    if scan is not None:
        for command in scan.likely_test_commands[:6]:
            commands.append(
                _command(
                    command,
                    scope="broad",
                    confidence=0.74,
                    reason="repo scan discovered a likely native test command",
                )
            )
    if commands:
        return _dedupe_commands(commands)
    if (root / "package.json").exists():
        manager = _node_package_manager(root)
        commands.append(
            _command(
                f"{manager} test",
                scope="broad",
                confidence=0.62,
                reason="package.json test surface detected",
            )
        )
    if (root / "go.mod").exists():
        commands.append(
            _command("go test ./...", scope="broad", confidence=0.76, reason="go.mod detected")
        )
    if (root / "Cargo.toml").exists():
        commands.append(
            _command("cargo test", scope="broad", confidence=0.72, reason="Cargo.toml detected")
        )
    if (root / "gradlew").exists():
        commands.append(
            _command(
                "./gradlew test", scope="broad", confidence=0.62, reason="Gradle wrapper detected"
            )
        )
    elif (root / "pom.xml").exists():
        commands.append(
            _command("mvn test", scope="broad", confidence=0.62, reason="pom.xml detected")
        )
    return _dedupe_commands(commands)


def _frameworks_from_commands(commands: list[dict[str, Any]], *, root: Path) -> set[str]:
    frameworks: set[str] = set()
    for item in commands:
        raw_command = str(item.get("command") or "")
        command = raw_command.casefold()
        analysis = analyze_verification_command(raw_command, trusted=True, workspace_root=root)
        if analysis.command_family in {"pytest", "unittest"}:
            frameworks.add(analysis.command_family)
        if "npm" in command or "yarn" in command or "pnpm" in command or "bun" in command:
            frameworks.add("node")
        if "go test" in command:
            frameworks.add("go test")
        if "cargo test" in command:
            frameworks.add("cargo test")
        if "gradle" in command or "mvn" in command:
            frameworks.add("junit")
    return frameworks


def _python_module_name(rel_path: str) -> str:
    pure = PurePosixPath(rel_path)
    if pure.suffix != ".py":
        return ""
    parts = list(pure.with_suffix("").parts)
    if parts and parts[0] in {"src", "lib"}:
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(part for part in parts if part.isidentifier())


def _node_frameworks(root: Path) -> set[str]:
    script = _package_test_script(root).casefold()
    frameworks = {"node"}
    if "vitest" in script:
        frameworks.add("vitest")
    if "jest" in script:
        frameworks.add("jest")
    return frameworks


def _node_package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    if (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        return "bun"
    return "npm"


def _package_test_script(root: Path) -> str:
    package_json = root / "package.json"
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return ""
    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        return ""
    value = scripts.get("test")
    return str(value or "") if isinstance(value, str) else ""


def _file_contains(path: Path, needle: str) -> bool:
    if not needle:
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return needle in text


def _is_ignored(rel_path: str) -> bool:
    parts = set(PurePosixPath(rel_path).parts)
    return bool(parts & {".git", ".venv", "venv", "node_modules", "__pycache__", ".ruff_cache"})


def _command(command: str, *, scope: str, confidence: float, reason: str) -> dict[str, Any]:
    return {
        "command": command,
        "scope": scope,
        "confidence": round(float(confidence), 2),
        "reason": reason,
    }


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        clean = str(value or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        deduped.append(clean)
    return deduped


def _dedupe_test_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_path: dict[str, dict[str, Any]] = {}
    pathless: list[dict[str, Any]] = []
    for candidate in candidates:
        path = str(candidate.get("path") or "").strip()
        if not path:
            pathless.append(candidate)
            continue
        existing = by_path.get(path)
        if existing is None or _test_candidate_rank(candidate) > _test_candidate_rank(existing):
            by_path[path] = candidate
    return sorted(
        [*by_path.values(), *pathless],
        key=_test_candidate_rank,
        reverse=True,
    )


def _test_candidate_rank(candidate: dict[str, Any]) -> tuple[int, float]:
    has_node_id = 1 if str(candidate.get("id") or "").strip() else 0
    return (has_node_id, float(candidate.get("confidence") or 0))


def _dedupe_commands(commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_command: dict[str, dict[str, Any]] = {}
    for item in commands:
        command = str(item.get("command") or "").strip()
        if not command:
            continue
        existing = by_command.get(command)
        if existing is None or float(item.get("confidence") or 0) > float(
            existing.get("confidence") or 0
        ):
            by_command[command] = item
    return sorted(
        by_command.values(),
        key=lambda item: (item.get("scope") != "targeted", -float(item.get("confidence") or 0)),
    )


test_discover.__test__ = False
