from __future__ import annotations

import pytest

from alysis_code.file_classification import (
    derived_artifact_reason,
    describe_path_kinds,
    is_code_implementation_path,
    is_generated_or_vendor_path,
    is_repository_metadata_path,
    is_symbol_scannable_path,
    is_test_path,
    language_for_path,
    source_extensions_for_languages,
)


@pytest.mark.parametrize(
    "path",
    [
        "src/main/java/com/example/ConfigLoader.java",
        "src/ConfigLoader.java",
        "src/**.java",
    ],
)
def test_java_implementation_paths_are_first_class(path: str) -> None:
    assert language_for_path(path) == "java"
    assert is_code_implementation_path(path)
    assert is_symbol_scannable_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "src/test/java/com/example/ConfigLoaderTest.java",
        "src/test/java/**/*.java",
        "tests/ConfigLoaderTest.java",
    ],
)
def test_java_test_paths_are_not_implementation_paths(path: str) -> None:
    assert language_for_path(path) == "java"
    assert is_test_path(path)
    assert not is_code_implementation_path(path)
    assert is_symbol_scannable_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "build/generated/src/main/java/com/example/ConfigLoader.java",
        "vendor/src/ConfigLoader.java",
        "src/generated/java/com/example/ConfigLoader.java",
    ],
)
def test_generated_vendor_and_build_paths_are_not_normal_implementation_scope(
    path: str,
) -> None:
    assert language_for_path(path) == "java"
    assert not is_code_implementation_path(path)
    assert not is_symbol_scannable_path(path)


@pytest.mark.parametrize(
    "path, language",
    [
        ("src/app.py", "python"),
        ("src/app.ts", "typescript"),
        ("src/app.js", "javascript"),
        ("src/lib.rs", "rust"),
        ("cmd/server.go", "go"),
    ],
)
def test_existing_implementation_languages_stay_supported(path: str, language: str) -> None:
    assert language_for_path(path) == language
    assert is_code_implementation_path(path)
    assert is_symbol_scannable_path(path)


def test_java_repo_scan_language_extensions_preserve_kotlin_hinting() -> None:
    assert {".java", ".kt"} <= source_extensions_for_languages(["java"])


def test_existing_test_patterns_stay_excluded_from_implementation() -> None:
    for path in [
        "tests/test_app.py",
        "src/app.test.ts",
        "src/app.spec.js",
        "src/lib_test.go",
    ]:
        assert is_test_path(path)
        assert not is_code_implementation_path(path)


def test_path_kind_descriptions_include_known_and_unknown_kinds() -> None:
    assert describe_path_kinds(["src/test/java/com/example/ConfigLoaderTest.java"]) == (
        "Java test files only"
    )
    assert describe_path_kinds(["README.md", "settings.json"]) == "docs/config only"
    assert describe_path_kinds(["notes/data.unknownext"]) == "docs only"
    assert describe_path_kinds(["scripts/task.weird"]) == "unknown .weird files only"


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("package-lock.json", "dependency lockfile"),
        ("frontend/package-lock.json", "dependency lockfile"),
        ("uv.lock", "dependency lockfile"),
        ("Cargo.lock", "dependency lockfile"),
        ("go.sum", "dependency lockfile"),
        ("assets/app.min.js", "minified bundle"),
        ("assets/styles.min.css", "minified bundle"),
        ("assets/app.js.map", "source map"),
        ("state/session.lock", "lockfile"),
        ("dist/bundle.js", "generated or vendored path"),
        ("vendor/lib/util.py", "generated or vendored path"),
    ],
)
def test_derived_artifact_reason_classifies_by_file_class(path: str, reason: str) -> None:
    assert derived_artifact_reason(path) == reason


@pytest.mark.parametrize(
    "path",
    [
        "src/main.py",
        "README.md",
        "package.json",
        "pyproject.toml",
        "src/locker.py",
        "docs/locks.md",
        "",
    ],
)
def test_ordinary_paths_are_not_derived_artifacts(path: str) -> None:
    assert derived_artifact_reason(path) is None


@pytest.mark.parametrize(
    "path",
    [
        "sklearn/externals/joblib/parallel.py",
        "pip/_vendor/requests/api.py",
        "src/third_party/zlib/deflate.c",
        "lib/thirdparty/mod.js",
    ],
)
def test_vendored_tree_conventions_classify_as_generated_or_vendor(path: str) -> None:
    assert is_generated_or_vendor_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "sklearn/external_api.py",
        "docs/vendors.md",
        "src/thirdparty_client_names.py",
    ],
)
def test_vendored_lookalike_names_stay_first_party(path: str) -> None:
    assert is_generated_or_vendor_path(path) is False


@pytest.mark.parametrize(
    "path",
    ["clone/.git/HEAD", "src/.hg/store/data", r"repo\.svn\entries", "vendor/lib/.git/config"],
)
def test_repository_metadata_remains_excluded_from_code_scans(path):
    assert is_repository_metadata_path(path)
    assert is_generated_or_vendor_path(path)
    assert not is_symbol_scannable_path(path)


@pytest.mark.parametrize(
    "path", ["repo/.github/workflows/build.yml", "vendor/lib/source.py", "build/generated.py"]
)
def test_dependency_sources_and_similar_names_are_not_repository_metadata(path):
    assert not is_repository_metadata_path(path)
