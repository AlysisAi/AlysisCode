from __future__ import annotations

import io
import subprocess
import tarfile
import tomllib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest
from _release_test_helpers import PACKAGE_VERSION

from alysis_code.build_identity import BuildInfo, render_build_info_module
from scripts.release import validate_python_distributions as distribution_checks
from scripts.release.validate_python_distributions import (
    DistributionValidationError,
    validate_distributions,
)

NAME = "alysis-code"
NORMALIZED = "alysis_code"
VERSION = PACKAGE_VERSION
ROOT = Path(__file__).resolve().parents[1]
COMMIT = "abcdef1234567890abcdef1234567890abcdef12"


@pytest.mark.parametrize("archive", ["wheel", "sdist"])
@pytest.mark.parametrize("tampered", [False, True])
def test_portable_git_ca_requires_exact_public_bundle(tmp_path, archive, tampered):
    member = distribution_checks._PUBLIC_CERTIFICATE_ASSETS[0]
    content = (ROOT / "src" / member).read_bytes()
    original = content
    if tampered:
        content += b"\n-----BEGIN PRIVATE KEY-----\nnot-allowed\n"
    dist, project = _candidate(
        tmp_path,
        wheel_extra=(member, content if archive == "wheel" else original),
        sdist_extra=(
            f"{NORMALIZED}-{VERSION}/src/{member}",
            content if archive == "sdist" else original,
        ),
    )
    pinned = project.parent / "src" / member
    pinned.parent.mkdir(parents=True)
    pinned.write_bytes(original)
    if tampered:
        with pytest.raises(DistributionValidationError, match="public certificates differ"):
            validate_distributions(dist, project_file=project, smoke=False)
    else:
        validate_distributions(dist, project_file=project, smoke=False)


def _stamp(**overrides: object) -> bytes:
    fields = {
        "commit": COMMIT,
        "commit_subject": "Release fixture",
        "timestamp": "2026-10-15T12:00:00Z",
        "dirty": False,
        "source": "git",
    }
    return render_build_info_module(BuildInfo(**(fields | overrides))).encode("utf-8")


def _stamped_candidate(tmp_path: Path, wheel_stamp: bytes, sdist_stamp: bytes) -> tuple[Path, Path]:
    return _candidate(
        tmp_path,
        wheel_extra=("alysis_code/_build_info.py", wheel_stamp),
        sdist_extra=(f"{NORMALIZED}-{VERSION}/src/alysis_code/_build_info.py", sdist_stamp),
    )


def test_admits_matching_clean_generated_build_stamps(tmp_path: Path) -> None:
    dist, project = _stamped_candidate(tmp_path, _stamp(), _stamp())
    validate_distributions(dist, project_file=project, smoke=False, expected_commit=COMMIT)


@pytest.mark.parametrize("archive", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "invalid",
    [
        _stamp(commit="0" * 40),
        _stamp(dirty=True),
        _stamp(source="dev-default"),
        _stamp(timestamp="2026-02-30T00:00:00Z"),
        _stamp().replace(b"BUILD_DIRTY = False", b"BUILD_DIRTY = 0"),
        _stamp().replace(b"BUILD_INFO_SCHEMA_VERSION = 2", b"BUILD_INFO_SCHEMA_VERSION = True"),
        _stamp() + b"BUILD_DIRTY = False\n",
        _stamp() + b"import os\n",
        _stamp().replace(b"BUILD_DIRTY = False", b"BUILD_DIRTY = bool(0)"),
        b"# oversized\n" + b" " * (16 * 1024),
        b"\xff",
        b"BUILD_COMMIT = 'missing other fields'\n",
    ],
    ids=[
        "wrong-commit",
        "dirty",
        "dev-default",
        "invalid-date",
        "false-integer",
        "schema-bool",
        "duplicate-field",
        "import",
        "call",
        "oversized",
        "non-utf8",
        "missing-fields",
    ],
)
def test_rejects_invalid_build_identity_in_either_archive(
    tmp_path: Path, archive: str, invalid: bytes
) -> None:
    dist, project = _stamped_candidate(
        tmp_path,
        invalid if archive == "wheel" else _stamp(),
        invalid if archive == "sdist" else _stamp(),
    )
    with pytest.raises(DistributionValidationError, match="[Bb]uild stamp"):
        validate_distributions(dist, project_file=project, smoke=False, expected_commit=COMMIT)


@pytest.mark.parametrize("archive", ["wheel", "sdist"])
def test_requires_stamp_in_both_archives(tmp_path: Path, archive: str) -> None:
    dist, project = _candidate(
        tmp_path,
        wheel_extra=None if archive == "wheel" else ("alysis_code/_build_info.py", _stamp()),
        sdist_extra=None
        if archive == "sdist"
        else (f"{NORMALIZED}-{VERSION}/src/alysis_code/_build_info.py", _stamp()),
    )
    with pytest.raises(DistributionValidationError, match="build stamp is missing"):
        validate_distributions(dist, project_file=project, smoke=False, expected_commit=COMMIT)


def test_does_not_execute_stamp_before_admission(tmp_path: Path) -> None:
    canary = tmp_path / "must-not-exist"
    malicious = _stamp() + f"open({str(canary)!r}, 'w').write('executed')\n".encode()
    dist, project = _stamped_candidate(tmp_path, malicious, malicious)
    with pytest.raises(DistributionValidationError, match="Nonliteral"):
        validate_distributions(dist, project_file=project, smoke=False, expected_commit=COMMIT)
    assert not canary.exists()


def test_requires_byte_identical_wheel_and_sdist_stamp(tmp_path: Path) -> None:
    dist, project = _stamped_candidate(tmp_path, _stamp(), _stamp(commit_subject="Another build"))
    with pytest.raises(DistributionValidationError, match="stamps differ"):
        validate_distributions(dist, project_file=project, smoke=False, expected_commit=COMMIT)


@pytest.mark.parametrize("commit", ["", COMMIT[:12], COMMIT.upper(), "main"])
def test_requires_independently_selected_full_commit(tmp_path: Path, commit: str) -> None:
    dist, project = _stamped_candidate(tmp_path, _stamp(), _stamp())
    with pytest.raises(DistributionValidationError, match="exact Git SHA"):
        validate_distributions(dist, project_file=project, smoke=False, expected_commit=commit)


@pytest.mark.parametrize(
    "identity,accepted",
    [
        (f"commit: {COMMIT[:12]}, dirty: no, source: git", True),
        (f"commit: {COMMIT[:12]}, dirty: yes, source: git", False),
        ("commit: unknown, dirty: no, source: git", False),
        (f"commit: {COMMIT[:12]}, dirty: no, source: dev-default", False),
    ],
    ids=["expected", "dirty", "unknown-commit", "dev-default"],
)
def test_archive_identity_must_match_installed_version_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: str, accepted: bool
) -> None:
    dist, project = _stamped_candidate(tmp_path, _stamp(), _stamp())
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, f"alysis {VERSION} ({identity})", "")

    monkeypatch.setattr(distribution_checks.shutil, "which", lambda _name: "fixture-uv")
    monkeypatch.setattr(distribution_checks.subprocess, "run", fake_run)
    if accepted:
        validate_distributions(dist, project_file=project, expected_commit=COMMIT)
    else:
        with pytest.raises(DistributionValidationError, match="expected clean build identity"):
            validate_distributions(dist, project_file=project, expected_commit=COMMIT)
    assert calls[0][0] == "fixture-uv"
    assert any(call[-1] == "--version" for call in calls)
    if accepted:
        assert any("smoke_hosted_login.py" in str(part) for call in calls for part in call)


def test_accepts_exact_safe_wheel_and_sdist(tmp_path: Path) -> None:
    dist, project = _candidate(tmp_path)

    wheel, sdist = validate_distributions(dist, project_file=project, smoke=False)

    assert wheel.name == f"{NORMALIZED}-{VERSION}-py3-none-any.whl"
    assert sdist.name == f"{NORMALIZED}-{VERSION}.tar.gz"


def test_rejects_extra_release_output(tmp_path: Path) -> None:
    dist, project = _candidate(tmp_path)
    (dist / "checksums.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(DistributionValidationError, match="exactly one wheel and one sdist"):
        validate_distributions(dist, project_file=project, smoke=False)


@pytest.mark.parametrize(
    "entry",
    [
        "../escape.py",
        "alysis_code/deploy.ppk",
        "alysis_code/.ssh/config",
    ],
)
def test_rejects_unsafe_or_sensitive_wheel_entries(tmp_path: Path, entry: str) -> None:
    dist, project = _candidate(tmp_path, wheel_extra=(entry, b"secret"))

    with pytest.raises(DistributionValidationError):
        validate_distributions(dist, project_file=project, smoke=False)


def test_rejects_case_insensitive_duplicate_wheel_entries(tmp_path: Path) -> None:
    dist, project = _candidate(
        tmp_path,
        wheel_extra=("ALYSIS_CODE/__init__.py", b"duplicate"),
    )

    with pytest.raises(DistributionValidationError, match="duplicate"):
        validate_distributions(dist, project_file=project, smoke=False)


def test_rejects_wheel_symlinks(tmp_path: Path) -> None:
    dist, project = _candidate(tmp_path)
    wheel = dist / f"{NORMALIZED}-{VERSION}-py3-none-any.whl"
    link = ZipInfo("alysis_code/link")
    link.create_system = 3
    link.external_attr = 0o120777 << 16
    with ZipFile(wheel, "a", compression=ZIP_DEFLATED) as archive:
        archive.writestr(link, "target")

    with pytest.raises(DistributionValidationError, match="linked"):
        validate_distributions(dist, project_file=project, smoke=False)


def test_rejects_sdist_links(tmp_path: Path) -> None:
    dist, project = _candidate(tmp_path, sdist_link=True)

    with pytest.raises(DistributionValidationError, match="linked"):
        validate_distributions(dist, project_file=project, smoke=False)


def test_rejects_any_other_sdist_pem(tmp_path: Path) -> None:
    root = f"{NORMALIZED}-{VERSION}"
    dist, project = _candidate(
        tmp_path,
        sdist_extra=(f"{root}/src/alysis_code/signing.pem", b"secret"),
    )

    with pytest.raises(DistributionValidationError, match="private key"):
        validate_distributions(dist, project_file=project, smoke=False)


def _public_certificate_candidate(tmp_path: Path, *, changed_archive: str | None = None):
    name = distribution_checks._PUBLIC_CERTIFICATE_ASSETS[0]
    certificates = (ROOT / "src" / name).read_bytes()
    root = f"{NORMALIZED}-{VERSION}"
    dist, project = _candidate(
        tmp_path,
        wheel_extra=(name, certificates + (b"# altered\n" if changed_archive == "wheel" else b"")),
        sdist_extra=(
            f"{root}/src/{name}",
            certificates + (b"# altered\n" if changed_archive == "sdist" else b""),
        ),
    )
    pinned = project.parent / "src" / name
    pinned.parent.mkdir(parents=True)
    pinned.write_bytes(certificates)
    return dist, project, pinned


def test_admits_only_exact_pinned_public_certificate_bundle(tmp_path: Path) -> None:
    dist, project, _ = _public_certificate_candidate(tmp_path)
    validate_distributions(dist, project_file=project, smoke=False)


@pytest.mark.parametrize("archive", ["wheel", "sdist"])
def test_rejects_changed_public_certificates_even_with_valid_pem(
    tmp_path: Path, archive: str
) -> None:
    dist, project, _ = _public_certificate_candidate(tmp_path, changed_archive=archive)
    with pytest.raises(DistributionValidationError, match="public certificates differ"):
        validate_distributions(dist, project_file=project, smoke=False)


@pytest.mark.parametrize(
    "extra",
    [
        b"-----BEGIN PRIVATE KEY-----\naW5lcnQ=\n-----END PRIVATE KEY-----\n",
        b"unreviewed non-certificate text\n",
        b"\x00\xff",
    ],
)
def test_repository_certificate_asset_cannot_hide_other_material(
    tmp_path: Path, extra: bytes
) -> None:
    dist, project, pinned = _public_certificate_candidate(tmp_path)
    pinned.write_bytes(pinned.read_bytes() + extra)
    with pytest.raises(DistributionValidationError, match="non-certificate material"):
        validate_distributions(dist, project_file=project, smoke=False)


def test_certificate_named_file_requires_the_independent_repository_asset(tmp_path: Path) -> None:
    dist, project, pinned = _public_certificate_candidate(tmp_path)
    pinned.unlink()
    with pytest.raises(DistributionValidationError, match="private key material"):
        validate_distributions(dist, project_file=project, smoke=False)


@pytest.mark.parametrize(
    "sdist_entry",
    [
        "workspace-output.txt",
        "docs/debug.txt",
        "CHANGELOG.md",
        "tests/fixtures/forge_host_v2/debug.json",
        "schemas/other.schema.json",
    ],
)
def test_rejects_files_outside_exact_release_inventory(tmp_path: Path, sdist_entry: str) -> None:
    dist, project = _candidate(
        tmp_path / "wheel",
        wheel_extra=("documentation/debug.txt", b"debug"),
    )

    with pytest.raises(DistributionValidationError, match="outside the release inventory"):
        validate_distributions(dist, project_file=project, smoke=False)

    dist, project = _candidate(
        tmp_path / "sdist",
        sdist_extra=(f"{NORMALIZED}-{VERSION}/{sdist_entry}", b"debug"),
    )

    with pytest.raises(DistributionValidationError, match="outside the release inventory"):
        validate_distributions(dist, project_file=project, smoke=False)


def test_hatch_sdist_uses_an_explicit_release_inventory() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["tool"]["hatch"]["build"]["targets"]["sdist"]["include"] == [
        "/.gitignore",
        "/docs/CHANGELOG.md",
        "/LICENSE",
        "/NOTICE",
        "/README.md",
        "/pyproject.toml",
        "/hatch_build.py",
        "/src/alysis_code",
    ]


def test_python_release_verifies_exact_provenance_and_cyclonedx_predicates() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert workflow.count('--predicate-type "https://slsa.dev/provenance/v1"') == 2
    assert workflow.count('--predicate-type "https://cyclonedx.org/bom"') == 2
    assert workflow.count("--expected-predicate dist/alysis-code.cdx.json") == 1
    assert workflow.count("validate_github_attestation_receipt.py") == 2
    assert "--deny-self-hosted-runners" in workflow


def test_declared_windows_bundle_cannot_be_omitted_from_distributions(tmp_path):
    dist, project = _candidate(tmp_path)
    with project.open("a", encoding="utf-8") as stream:
        stream.write('\n[tool.hatch.build.hooks.custom]\npath = "hatch_build.py"\n')
    with pytest.raises(DistributionValidationError, match="Windows sandbox bundle is missing"):
        validate_distributions(dist, project_file=project, smoke=False)


def test_rejects_wheel_metadata_identity_mismatch(tmp_path: Path) -> None:
    dist, project = _candidate(tmp_path, metadata_name="different-package")

    with pytest.raises(DistributionValidationError, match="metadata identity"):
        validate_distributions(dist, project_file=project, smoke=False)


def _candidate(
    tmp_path: Path,
    *,
    wheel_extra: tuple[str, bytes] | None = None,
    sdist_link: bool = False,
    sdist_extra: tuple[str, bytes] | None = None,
    metadata_name: str = NAME,
) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    project = tmp_path / "pyproject.toml"
    project.write_text(
        f'[project]\nname = "{NAME}"\nversion = "{VERSION}"\n',
        encoding="utf-8",
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / f"{NORMALIZED}-{VERSION}-py3-none-any.whl"
    dist_info = f"{NORMALIZED}-{VERSION}.dist-info"
    with ZipFile(wheel, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("alysis_code/__init__.py", "")
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.3\nName: {metadata_name}\nVersion: {VERSION}\n",
        )
        archive.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n")
        archive.writestr(
            f"{dist_info}/entry_points.txt",
            "[console_scripts]\nalysis = alysis_code.cli:app\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
        if wheel_extra is not None:
            archive.writestr(*wheel_extra)

    root = f"{NORMALIZED}-{VERSION}"
    sdist = dist / f"{root}.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        _tar_file(archive, f"{root}/pyproject.toml", project.read_bytes())
        _tar_file(archive, f"{root}/src/alysis_code/__init__.py", b"")
        _tar_file(archive, f"{root}/docs/CHANGELOG.md", b"# Changelog\n")
        if sdist_extra is not None:
            _tar_file(archive, *sdist_extra)
        if sdist_link:
            link = tarfile.TarInfo(f"{root}/src/alysis_code/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "__init__.py"
            archive.addfile(link)
    return dist, project


def _tar_file(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))
