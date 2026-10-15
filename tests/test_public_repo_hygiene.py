import base64
import json
import subprocess
from pathlib import Path

import pytest

from scripts.qa import check_public_repo
from scripts.qa.check_public_repo import (
    collect_violations,
    markdown_link_violations,
    path_violation,
)


def test_checks_working_tree_after_removing_tracked_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "ci.log").write_text("synthetic CI output\n", encoding="utf-8")
    subprocess.run(["git", "add", "ci.log"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    monkeypatch.setattr(
        check_public_repo, "__file__", str(tmp_path / "scripts/qa/check_public_repo.py")
    )

    assert check_public_repo.main() == 1
    assert "ci.log" in capsys.readouterr().err

    (tmp_path / "ci.log").rename(tmp_path / "ci-output.txt")

    assert check_public_repo.main() == 0
    assert "passed" in capsys.readouterr().out

    (tmp_path / "new.log").write_text("synthetic untracked output\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("", encoding="utf-8")

    assert check_public_repo.main() == 1
    assert "new.log" in capsys.readouterr().err


def test_rejects_local_and_credential_bearing_paths() -> None:
    assert path_violation("src/pkg/__pycache__/module.pyc") == "generated cache directory"
    assert path_violation(".alysis/runs/session.json") == "local runtime or build-output directory"
    assert path_violation("config/.env") == "environment file"
    assert path_violation("certificates/release.pem") == "credential-bearing or generated file type"


def test_allows_source_documentation_and_environment_examples() -> None:
    assert path_violation("src/alysis_code/cli.py") is None
    assert path_violation("docs/security_model.md") is None
    assert path_violation(".env.example") is None
    assert path_violation("src/alysis_code/extensions/runtime.py") is None
    assert path_violation("src/alysis_code/assets/prompts.py") is None


@pytest.mark.parametrize(
    "path",
    ["prompts/release.md", "Claude outputs/review.md", ".codex/notes.md", ".claude/settings.json"],
)
def test_rejects_local_assistant_working_files(path: str) -> None:
    assert path_violation(path) == "local assistant working files"


def test_only_reviewed_editor_packages_are_public() -> None:
    assert path_violation("extensions/vscode-alysis/package.json") is None
    assert path_violation("extensions/jetbrains-alysis/README.md") is None
    assert path_violation("extensions/shared-ui/README.md") is None
    assert path_violation("extensions/unreleased/package.json") == "unreleased editor package"
    assert path_violation("supabase/migrations/setup.sql") == "hosted-service implementation"


def test_pinned_verification_key_must_be_public(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key_path = check_public_repo._PUBLIC_VERIFICATION_KEY
    target = tmp_path / key_path
    target.parent.mkdir(parents=True)
    key = ed25519.Ed25519PrivateKey.generate()
    target.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    assert collect_violations([key_path], tmp_path) == []
    target.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    assert collect_violations([key_path], tmp_path) == [
        f"{key_path}: expected a public verification key"
    ]


def test_rejects_power_mode_package() -> None:
    assert path_violation("src/alysis_code/power_mode/worker.py") == "unreleased Power mode package"


@pytest.mark.parametrize(
    "path",
    ["docs/internal/notes.md", "docs/internal/reviews/catalog.md", "Docs/Internal/notes.md"],
)
def test_rejects_internal_documentation(path: str) -> None:
    assert path_violation(path) == "internal documentation directory"
    assert path_violation("docs/internals.md") is None
    assert path_violation("src/pkg/internal/parser.py") is None


def test_rejects_large_tracked_files(tmp_path: Path) -> None:
    large_file = tmp_path / "large.bin"
    large_file.write_bytes(b"x" * (5 * 1024 * 1024 + 1))

    assert collect_violations(["large.bin"], tmp_path) == ["large.bin: tracked file exceeds 5 MiB"]


def test_reports_broken_relative_markdown_links(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text(
        "[existing](../README.md) [missing](missing.md) "
        "[web](https://example.com) [host route](../../../issues)\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Project\n", encoding="utf-8")

    assert markdown_link_violations(["README.md", "docs/guide.md"], tmp_path) == [
        "docs/guide.md:1: broken relative link: missing.md"
    ]


def test_relative_links_require_exact_file_and_directory_case(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (docs / "οδηγός.md").write_text("# Οδηγός\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "[correct](docs/guide.md#setup)\n"
        "[wrong filename](docs/GUIDE.md)\n"
        "[wrong directory](Docs/guide.md)\n"
        "[directory](docs/)\n"
        "[wrong directory link](Docs/)\n"
        "[Unicode](docs/οδηγός.md)\n"
        "[encoded filename](docs/%CE%BF%CE%B4%CE%B7%CE%B3%CF%8C%CF%82.md)\n"
        "[wrong Unicode case](docs/ΟΔΗΓΌΣ.md)\n",
        encoding="utf-8",
    )

    assert markdown_link_violations(["README.md"], tmp_path) == [
        "README.md:2: broken relative link: docs/GUIDE.md",
        "README.md:3: broken relative link: Docs/guide.md",
        "README.md:5: broken relative link: Docs/",
        "README.md:8: broken relative link: docs/ΟΔΗΓΌΣ.md",
    ]


@pytest.mark.parametrize(
    "path", ["outputs/release-notes.md", ".qa-results/scan.json", "private-attestation/signoff.md"]
)
def test_rejects_local_release_evidence(path: str) -> None:
    assert path_violation(path) == "local runtime or build-output directory"


@pytest.mark.parametrize("scheme", ["https", "postgresql"])
def test_rejects_backend_deployment_hosts_without_echoing_them(tmp_path: Path, scheme: str) -> None:
    host = ".".join(("synthetic-project", "supabase", "co"))
    (tmp_path / "config.py").write_text(f'ENDPOINT = "{scheme}://{host}/api"\n')
    assert collect_violations(["config.py"], tmp_path) == [
        "config.py:1: deployment-specific backend host"
    ]


@pytest.mark.parametrize("role", ["anon", "service_role"])
def test_rejects_backend_jwt_even_when_split_into_string_literals(
    tmp_path: Path, role: str
) -> None:
    payload = (
        base64.urlsafe_b64encode(
            json.dumps({"iss": "supabase", "ref": "synthetic-project", "role": role}).encode()
        )
        .decode()
        .rstrip("=")
    )
    (tmp_path / "client.py").write_text(
        f'TOKEN = (\n    "header."\n    "{payload}."\n    "signature"\n)\n'
    )
    assert collect_violations(["client.py"], tmp_path) == [
        "client.py:3: deployment-specific backend JWT"
    ]


@pytest.mark.parametrize("kind", ["publishable", "secret"])
def test_rejects_backend_project_keys(tmp_path: Path, kind: str) -> None:
    key = "_".join(("sb", kind, "synthetic-test-value"))
    (tmp_path / "client.py").write_text(f'KEY = "{key}"\n')
    assert collect_violations(["client.py"], tmp_path) == [
        "client.py:1: deployment-specific backend key"
    ]


def test_allows_generic_backend_support_and_reserved_example_urls(tmp_path: Path) -> None:
    (tmp_path / "client.py").write_text(
        'ENV_NAME = "ALYSIS_SUPABASE_URL"\n'
        'BASE_URL = "https://cloud.example.invalid/api"\n'
        'VENDOR_DOMAIN = "supabase.co"\n'
    )
    assert collect_violations(["client.py"], tmp_path) == []
