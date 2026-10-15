from __future__ import annotations

import io
import tarfile
import tomllib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest
from _release_test_helpers import PACKAGE_VERSION

from scripts.release.validate_python_distributions import (
    DistributionValidationError,
    validate_distributions,
)

NAME = "alysis-code"
NORMALIZED = "alysis_code"
VERSION = PACKAGE_VERSION
ROOT = Path(__file__).resolve().parents[1]


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


@pytest.mark.parametrize(
    "sdist_entry",
    [
        "workspace-output.txt",
        "docs/debug.txt",
        "CHANGELOG.md",
        "schemas/forge-host-v2/fixtures/debug.json",
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
        "/src/alysis_code",
        "/schemas/forge-host-v2/forge-host-v2.schema.json",
    ]


def test_python_release_verifies_exact_provenance_and_cyclonedx_predicates() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert workflow.count('--predicate-type "https://slsa.dev/provenance/v1"') == 2
    assert workflow.count('--predicate-type "https://cyclonedx.org/bom"') == 2
    assert workflow.count("--expected-predicate dist/alysis-code.cdx.json") == 1
    assert workflow.count("validate_github_attestation_receipt.py") == 2
    assert "--deny-self-hosted-runners" in workflow


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
