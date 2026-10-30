from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from .runtime_artifacts import RUNTIME_ARTIFACT_DIR_NAMES

LANGUAGE_EXTENSIONS: dict[str, frozenset[str]] = {
    "python": frozenset({".py"}),
    "javascript": frozenset({".cjs", ".js", ".jsx", ".mjs"}),
    "typescript": frozenset({".cts", ".mts", ".ts", ".tsx"}),
    "rust": frozenset({".rs"}),
    "go": frozenset({".go"}),
    "java": frozenset({".java"}),
    "kotlin": frozenset({".kt", ".kts"}),
    "c": frozenset({".c", ".h"}),
    "cpp": frozenset({".cc", ".cpp", ".cxx", ".hpp", ".hxx"}),
    "csharp": frozenset({".cs"}),
    "php": frozenset({".php"}),
    "ruby": frozenset({".rb"}),
    "shell": frozenset({".bash", ".sh"}),
    "swift": frozenset({".swift"}),
}

SOURCE_EXTENSIONS_BY_LANGUAGE: dict[str, frozenset[str]] = {
    **LANGUAGE_EXTENSIONS,
    "java": LANGUAGE_EXTENSIONS["java"] | LANGUAGE_EXTENSIONS["kotlin"],
    "node": LANGUAGE_EXTENSIONS["javascript"] | LANGUAGE_EXTENSIONS["typescript"],
}
BROAD_SOURCE_EXTENSIONS = frozenset(
    suffix for extensions in LANGUAGE_EXTENSIONS.values() for suffix in extensions
)

CODE_IMPLEMENTATION_EXTENSIONS = frozenset(
    {
        ".cjs",
        ".go",
        ".java",
        ".js",
        ".jsx",
        ".mjs",
        ".mts",
        ".py",
        ".rs",
        ".ts",
        ".tsx",
    }
)
SYMBOL_SCANNABLE_EXTENSIONS = CODE_IMPLEMENTATION_EXTENSIONS
SYMBOL_SEARCH_BACKEND_BY_EXTENSION = {
    ".py": "python_ast",
    ".cjs": "js_ts_heuristic",
    ".js": "js_ts_heuristic",
    ".jsx": "js_ts_heuristic",
    ".mjs": "js_ts_heuristic",
    ".mts": "js_ts_heuristic",
    ".ts": "js_ts_heuristic",
    ".tsx": "js_ts_heuristic",
    ".java": "java_heuristic",
}

INFERRED_FILE_EXTENSIONS = frozenset(
    {
        *(suffix.lstrip(".") for suffix in BROAD_SOURCE_EXTENSIONS),
        "cfg",
        "conf",
        "css",
        "csv",
        "env",
        "html",
        "ini",
        "json",
        "md",
        "scss",
        "sql",
        "svg",
        "toml",
        "txt",
        "xml",
        "yaml",
        "yml",
    }
)

REPOSITORY_METADATA_DIR_NAMES = frozenset({".git", ".hg", ".svn"})
CODE_SCAN_SKIP_DIR_NAMES = (
    frozenset(
        {
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".alysis",
            ".tox",
            ".venv",
            "__pycache__",
            "build",
            "coverage",
            "dist",
            "node_modules",
            "target",
            "vendor",
            "venv",
            # Vendored-dependency trees under other conventional names. Editing
            # (or symbol-scanning) these is practically never the intended change:
            # sklearn uses ``externals``, pip uses ``_vendor``, Chromium-style
            # projects use ``third_party``/``thirdparty``.
            "_vendor",
            "externals",
            "third_party",
            "thirdparty",
        }
    )
    | frozenset(RUNTIME_ARTIFACT_DIR_NAMES)
    | REPOSITORY_METADATA_DIR_NAMES
)
GENERATED_DIR_NAMES = frozenset({"gen", "generated", "out"})
TEST_DIR_NAMES = frozenset({"__tests__", "spec", "specs", "test", "tests"})
FIXTURE_DIR_NAMES = frozenset(
    {
        "__snapshots__",
        "fixture",
        "fixtures",
        "golden",
        "goldens",
        "sample",
        "samples",
        "snapshot",
        "snapshots",
        "test-data",
        "test_data",
        "testdata",
    }
)
CONTENT_SURFACE_EXTENSIONS = frozenset({".adoc", ".markdown", ".md", ".mdx", ".rst", ".txt"})
FRONTEND_SURFACE_EXTENSIONS = frozenset({".css", ".htm", ".html", ".less", ".sass", ".scss"})
CONFIG_EXTENSIONS = frozenset(
    {
        ".cfg",
        ".conf",
        ".env",
        ".gradle",
        ".ini",
        ".json",
        ".properties",
        ".toml",
        ".xml",
        ".yaml",
        ".yml",
    }
)
CONFIG_FILENAMES = frozenset(
    {
        ".env",
        "cargo.toml",
        "dockerfile",
        "go.mod",
        "go.sum",
        "makefile",
        "mix.exs",
        "package-lock.json",
        "package.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pom.xml",
        "pyproject.toml",
        "requirements-dev.txt",
        "requirements.txt",
        "setup.cfg",
        "setup.py",
        "tox.ini",
        "yarn.lock",
    }
)

_EXTENSION_TO_LANGUAGE = {
    suffix: language
    for language, extensions in LANGUAGE_EXTENSIONS.items()
    for suffix in extensions
}
_LANGUAGE_DISPLAY = {
    "cpp": "C++",
    "csharp": "C#",
    "go": "Go",
    "java": "Java",
    "javascript": "JavaScript",
    "kotlin": "Kotlin",
    "php": "PHP",
    "python": "Python",
    "ruby": "Ruby",
    "rust": "Rust",
    "shell": "shell",
    "swift": "Swift",
    "typescript": "TypeScript",
}


@dataclass(frozen=True)
class PathClassification:
    path: str
    suffix: str
    language: str | None
    kind: str
    label: str


def normalize_classification_path(path: str) -> str:
    normalized = str(path or "").strip().replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def _parts(path: str) -> tuple[str, ...]:
    normalized = normalize_classification_path(path)
    if not normalized:
        return ()
    return tuple(part.casefold() for part in PurePosixPath(normalized).parts if part)


def _suffix(path: str) -> str:
    return PurePosixPath(normalize_classification_path(path)).suffix.casefold()


def language_for_path(path: str) -> str | None:
    return _EXTENSION_TO_LANGUAGE.get(_suffix(path))


def language_display_name(language: str | None) -> str | None:
    if not language:
        return None
    return _LANGUAGE_DISPLAY.get(language, language.title())


def source_extensions_for_languages(language_hints: list[str]) -> set[str]:
    extensions: set[str] = set()
    for hint in language_hints:
        extensions.update(SOURCE_EXTENSIONS_BY_LANGUAGE.get(str(hint).casefold(), frozenset()))
    return extensions or set(BROAD_SOURCE_EXTENSIONS)


def is_generated_or_vendor_path(path: str) -> bool:
    parts = _parts(path)
    if not parts:
        return False
    return bool(set(parts) & (CODE_SCAN_SKIP_DIR_NAMES | GENERATED_DIR_NAMES))


def is_repository_metadata_path(path: str) -> bool:
    """Repository bookkeeping stays excluded from source scans, not an edited dependency."""
    return bool(set(_parts(path)) & REPOSITORY_METADATA_DIR_NAMES)


DERIVED_ARTIFACT_FILENAMES = frozenset(
    {
        "bun.lockb",
        "cargo.lock",
        "composer.lock",
        "flake.lock",
        "gemfile.lock",
        "go.sum",
        "gradle.lockfile",
        "mix.lock",
        "npm-shrinkwrap.json",
        "package-lock.json",
        "packages.lock.json",
        "pipfile.lock",
        "pnpm-lock.yaml",
        "podfile.lock",
        "poetry.lock",
        "pubspec.lock",
        "uv.lock",
        "yarn.lock",
    }
)
_MINIFIED_BUNDLE_NAME_SUFFIXES = (".min.js", ".min.mjs", ".min.cjs", ".min.css")
_SOURCE_MAP_NAME_SUFFIXES = (".js.map", ".mjs.map", ".cjs.map", ".css.map")


def derived_artifact_reason(path: str) -> str | None:
    """Classify machine-generated, low-information-density artifacts.

    These files are legitimate to read when they are the subject of the task,
    but reading them wholesale by default mostly burns context: lockfiles,
    minified bundles, source maps, and files under generated/vendored trees.
    Classification is by file class (name shape and location) only — never by
    task wording — so callers can offer an explicit opt-in instead of a block.
    Returns a short human-readable reason, or None for ordinary files.
    """

    normalized = normalize_classification_path(path)
    if not normalized:
        return None
    name = PurePosixPath(normalized).name.casefold()
    if name in DERIVED_ARTIFACT_FILENAMES:
        return "dependency lockfile"
    if name.endswith(_MINIFIED_BUNDLE_NAME_SUFFIXES):
        return "minified bundle"
    if name.endswith(_SOURCE_MAP_NAME_SUFFIXES):
        return "source map"
    if name.endswith(".lock"):
        return "lockfile"
    if is_generated_or_vendor_path(normalized):
        return "generated or vendored path"
    return None


def is_fixture_path(path: str) -> bool:
    parts = _parts(path)
    return bool(parts and set(parts) & FIXTURE_DIR_NAMES)


def is_test_path(path: str) -> bool:
    normalized = normalize_classification_path(path)
    if not normalized:
        return False
    pure = PurePosixPath(normalized)
    parts = {part.casefold() for part in pure.parts if part}
    if parts & TEST_DIR_NAMES:
        return True

    name = pure.name
    lowered_name = name.casefold()
    stem = pure.stem
    lowered_stem = stem.casefold()
    suffix = pure.suffix.casefold()
    if (
        lowered_name.startswith(("test_", "test-"))
        or lowered_stem.endswith(("_test", "-test", "_spec", "-spec"))
        or ".test." in lowered_name
        or ".spec." in lowered_name
    ):
        return True
    if suffix == ".py" and lowered_name.endswith("_test.py"):
        return True
    if suffix in {".java", ".kt", ".kts"}:
        return (stem.startswith("Test") and len(stem) > len("Test")) or any(
            stem.endswith(suffix_marker) and len(stem) > len(suffix_marker)
            for suffix_marker in ("Test", "Tests", "Spec", "IT")
        )
    if suffix == ".go" and lowered_name.endswith("_test.go"):
        return True
    if suffix == ".rs" and (
        lowered_name.endswith("_test.rs") or lowered_stem.endswith(("_tests", "-tests"))
    ):
        return True
    return False


def is_symbol_scannable_path(path: str) -> bool:
    return _suffix(path) in SYMBOL_SCANNABLE_EXTENSIONS and not is_generated_or_vendor_path(path)


def symbol_search_backend_for_path(path: str) -> str | None:
    if is_generated_or_vendor_path(path):
        return None
    return SYMBOL_SEARCH_BACKEND_BY_EXTENSION.get(_suffix(path))


def is_code_implementation_path(path: str) -> bool:
    return (
        _suffix(path) in CODE_IMPLEMENTATION_EXTENSIONS
        and not is_test_path(path)
        and not is_fixture_path(path)
        and not is_generated_or_vendor_path(path)
    )


def is_docs_path(path: str) -> bool:
    normalized = normalize_classification_path(path).casefold()
    if normalized in {"readme", "readme.md", "todo", "todo.md"}:
        return True
    if normalized.startswith(("docs/", "doc/", "notes/", "note/")):
        return True
    return _suffix(normalized) in CONTENT_SURFACE_EXTENSIONS


def is_frontend_surface_path(path: str) -> bool:
    return _suffix(path) in FRONTEND_SURFACE_EXTENSIONS


def is_config_path(path: str) -> bool:
    normalized = normalize_classification_path(path).casefold()
    if PurePosixPath(normalized).name in CONFIG_FILENAMES:
        return True
    return _suffix(normalized) in CONFIG_EXTENSIONS


def classify_path(path: str) -> PathClassification:
    normalized = normalize_classification_path(path)
    suffix = _suffix(normalized)
    language = language_for_path(normalized)
    language_name = language_display_name(language)
    if is_code_implementation_path(normalized):
        label = f"{language_name} implementation files" if language_name else "implementation files"
        return PathClassification(normalized, suffix, language, "implementation", label)
    if is_generated_or_vendor_path(normalized):
        label = (
            f"generated/vendor {language_name} files" if language_name else "generated/vendor paths"
        )
        return PathClassification(normalized, suffix, language, "generated_or_vendor", label)
    if is_test_path(normalized):
        label = f"{language_name} test files" if language_name else "test files"
        return PathClassification(normalized, suffix, language, "test", label)
    if is_fixture_path(normalized):
        label = f"{language_name} fixture files" if language_name else "fixture files"
        return PathClassification(normalized, suffix, language, "fixture", label)
    if is_docs_path(normalized):
        return PathClassification(normalized, suffix, language, "docs", "docs")
    if is_frontend_surface_path(normalized):
        return PathClassification(
            normalized,
            suffix,
            language,
            "frontend_surface",
            "frontend surface files",
        )
    if is_config_path(normalized):
        return PathClassification(normalized, suffix, language, "config", "config")
    if language_name:
        return PathClassification(
            normalized,
            suffix,
            language,
            "source",
            f"{language_name} source files",
        )
    if suffix:
        return PathClassification(
            normalized, suffix, language, "unknown", f"unknown {suffix} files"
        )
    return PathClassification(
        normalized,
        suffix,
        language,
        "unknown",
        "unknown extensionless paths",
    )


def describe_path_kinds(paths: list[str]) -> str:
    labels = [classify_path(path).label for path in paths if normalize_classification_path(path)]
    if not labels:
        return "unknown paths"
    unique = list(dict.fromkeys(labels))
    if set(unique) <= {"docs", "config"}:
        if set(unique) == {"docs"}:
            return "docs only"
        if set(unique) == {"config"}:
            return "config only"
        return "docs/config only"
    if len(unique) == 1:
        return f"{unique[0]} only"
    return ", ".join(unique)


def symbol_definition_regex(symbol: str, path_or_suffix: str) -> re.Pattern[str]:
    escaped = re.escape(symbol)
    suffix = path_or_suffix if path_or_suffix.startswith(".") else _suffix(path_or_suffix)
    suffix = suffix.casefold()
    if suffix == ".py":
        return re.compile(rf"(?m)^\s*(?:async\s+def|def|class)\s+{escaped}\b")
    if suffix in {".cjs", ".js", ".jsx", ".mjs", ".mts", ".ts", ".tsx"}:
        return re.compile(
            rf"(?m)^\s*(?:export\s+)?(?:async\s+)?function\s+{escaped}\b"
            rf"|^\s*(?:export\s+)?(?:const|let|var)\s+{escaped}\s*="
            rf"|^\s*(?:export\s+)?class\s+{escaped}\b"
        )
    if suffix == ".go":
        return re.compile(rf"(?m)^\s*func\s+(?:\([^)]*\)\s*)?{escaped}\b")
    if suffix == ".rs":
        return re.compile(
            rf"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?"
            rf"(?:fn|struct|enum|trait)\s+{escaped}\b"
        )
    if suffix == ".java":
        annotation = r"(?:@[A-Za-z_$][\w$.]*(?:\([^)]*\))?\s+)*"
        modifier = (
            r"(?:(?:public|protected|private|abstract|static|final|sealed|non-sealed|"
            r"strictfp|synchronized|native|default)\s+)*"
        )
        generic_prefix = r"(?:<[^;{}()]+>\s+)?"
        return_type = (
            r"(?:(?:[A-Za-z_$][\w$.]*|void|boolean|byte|short|int|long|float|double|char)"
            r"(?:\s*<[^;{}()]+>)?(?:\s*\[\])*\s+)+"
        )
        prefix = annotation + modifier + generic_prefix
        return re.compile(
            rf"(?m)^\s*{prefix}(?:class|interface|enum|record)\s+{escaped}\b"
            rf"|^\s*{prefix}(?:{return_type})?{escaped}\s*\("
        )
    return re.compile(rf"\b{escaped}\b")
