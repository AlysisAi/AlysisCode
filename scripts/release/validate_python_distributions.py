from __future__ import annotations

import argparse
import email
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile


class DistributionValidationError(ValueError):
    """A Python release distribution is incomplete or unsafe."""


_FORBIDDEN_NAMES = {
    ".env",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
}
_FORBIDDEN_SUFFIXES = {".jks", ".key", ".keystore", ".p12", ".pfx", ".pem", ".ppk"}
_FORBIDDEN_DIRECTORIES = {".aws", ".docker", ".gnupg", ".kube", ".ssh"}
_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
_MAX_MEMBER_BYTES = 64 * 1024 * 1024
_SDIST_FILES = {
    ".gitignore",
    "docs/CHANGELOG.md",
    "LICENSE",
    "NOTICE",
    "PKG-INFO",
    "README.md",
    "pyproject.toml",
}


def validate_distributions(
    dist_dir: Path,
    *,
    project_file: Path,
    smoke: bool = True,
) -> tuple[Path, Path]:
    project = tomllib.loads(project_file.read_text(encoding="utf-8"))["project"]
    name = str(project["name"])
    version = str(project["version"])
    normalized = re.sub(r"[-_.]+", "_", name).lower()
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1 or len(list(dist_dir.iterdir())) != 2:
        raise DistributionValidationError(
            "Release output must contain exactly one wheel and one sdist."
        )
    wheel, sdist = wheels[0], sdists[0]
    expected_wheel = f"{normalized}-{version}-py3-none-any.whl"
    expected_sdist = f"{normalized}-{version}.tar.gz"
    if wheel.name != expected_wheel or sdist.name != expected_sdist:
        raise DistributionValidationError(
            "Distribution filenames do not match the package identity."
        )
    for path in (wheel, sdist):
        if path.stat().st_size <= 0 or path.stat().st_size > _MAX_ARCHIVE_BYTES:
            raise DistributionValidationError("Distribution archive size is invalid.")
    _validate_wheel(wheel, name=name, version=version, normalized=normalized)
    _validate_sdist(
        sdist,
        normalized=normalized,
        version=version,
    )
    if smoke:
        _smoke_wheel(wheel, name=name, version=version)
    return wheel, sdist


def _validate_wheel(wheel: Path, *, name: str, version: str, normalized: str) -> None:
    dist_info = f"{normalized}-{version}.dist-info"
    try:
        with ZipFile(wheel) as archive:
            names = _validate_member_names(
                (
                    (
                        item.filename,
                        item.file_size,
                        bool(item.flag_bits & 0x1)
                        or (item.external_attr >> 16) & 0o170000 == 0o120000,
                    )
                    for item in archive.infolist()
                ),
                archive_kind="wheel",
            )
            if archive.testzip() is not None:
                raise DistributionValidationError("Wheel contains a corrupt member.")
            metadata_name = f"{dist_info}/METADATA"
            entry_points_name = f"{dist_info}/entry_points.txt"
            required = {
                metadata_name,
                entry_points_name,
                f"{dist_info}/RECORD",
                f"{dist_info}/WHEEL",
                "alysis_code/__init__.py",
            }
            missing = required - names
            if missing:
                raise DistributionValidationError(
                    f"Wheel is missing required entries: {sorted(missing)}"
                )
            unexpected = sorted(
                name
                for name in names
                if not name.startswith("alysis_code/") and not name.startswith(f"{dist_info}/")
            )
            if unexpected:
                raise DistributionValidationError(
                    f"Wheel contains entries outside the release inventory: {unexpected}"
                )
            metadata = email.message_from_bytes(archive.read(metadata_name))
            if metadata.get("Name") != name or metadata.get("Version") != version:
                raise DistributionValidationError(
                    "Wheel metadata identity does not match pyproject.toml."
                )
            entry_points = archive.read(entry_points_name).decode("utf-8")
            if "alysis = alysis_code.cli:app" not in entry_points:
                raise DistributionValidationError("Wheel console entry point is missing.")
    except (BadZipFile, KeyError, UnicodeDecodeError) as exc:
        raise DistributionValidationError("Wheel archive is invalid.") from exc


def _validate_sdist(
    sdist: Path,
    *,
    normalized: str,
    version: str,
) -> None:
    prefix = f"{normalized}-{version}/"
    try:
        with tarfile.open(sdist, "r:gz") as archive:
            members = archive.getmembers()
            names = _validate_member_names(
                (
                    (
                        item.name,
                        item.size,
                        item.issym() or item.islnk() or not (item.isfile() or item.isdir()),
                    )
                    for item in members
                ),
                archive_kind="sdist",
            )
            if any(not name.startswith(prefix) for name in names):
                raise DistributionValidationError(
                    "Sdist members do not share the expected root directory."
                )
            unexpected = sorted(
                name
                for name in names
                if not _is_allowed_sdist_member(
                    name.removeprefix(prefix),
                )
            )
            if unexpected:
                raise DistributionValidationError(
                    f"Sdist contains entries outside the release inventory: {unexpected}"
                )
            required = {
                f"{prefix}pyproject.toml",
                f"{prefix}src/alysis_code/__init__.py",
            }
            missing = required - names
            if missing:
                raise DistributionValidationError(
                    f"Sdist is missing required entries: {sorted(missing)}"
                )
    except (tarfile.TarError, OSError) as exc:
        raise DistributionValidationError("Sdist archive is invalid.") from exc


def _is_allowed_sdist_member(relative_name: str) -> bool:
    return (
        relative_name in _SDIST_FILES
        or relative_name.startswith("src/alysis_code/")
        or relative_name == "schemas/forge-host-v2/forge-host-v2.schema.json"
    )


def _validate_member_names(
    members: Iterable[tuple[str, int, bool]],
    *,
    archive_kind: str,
    allowed_private_key_entries: set[str] | None = None,
) -> set[str]:
    names: set[str] = set()
    identities: set[str] = set()
    expanded = 0
    for raw_name, size, is_unsafe_type in members:
        name = str(raw_name).replace("\\", "/")
        pure = PurePosixPath(name)
        if (
            not name
            or "\x00" in name
            or str(raw_name) != name
            or pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise DistributionValidationError(f"{archive_kind} contains an unsafe member path.")
        identity = name.casefold()
        if identity in identities:
            raise DistributionValidationError(f"{archive_kind} contains duplicate member paths.")
        if is_unsafe_type:
            raise DistributionValidationError(
                f"{archive_kind} contains an encrypted, linked, or special member."
            )
        member_size = int(size)
        if member_size < 0 or member_size > _MAX_MEMBER_BYTES:
            raise DistributionValidationError(f"{archive_kind} contains an oversized member.")
        expanded += member_size
        if expanded > _MAX_ARCHIVE_BYTES:
            raise DistributionValidationError(
                f"{archive_kind} expanded size exceeds the release limit."
            )
        lowered_parts = tuple(part.casefold() for part in pure.parts)
        lower_name = lowered_parts[-1]
        suffix = Path(lower_name).suffix
        if any(part in _FORBIDDEN_DIRECTORIES for part in lowered_parts[:-1]):
            raise DistributionValidationError(f"{archive_kind} contains a credential directory.")
        if lower_name in _FORBIDDEN_NAMES or (
            lower_name.startswith(".env")
            and lower_name
            not in {
                ".env.defaults",
                ".env.dist",
                ".env.example",
                ".env.sample",
                ".env.template",
            }
        ):
            raise DistributionValidationError(f"{archive_kind} contains a credential file.")
        if suffix in _FORBIDDEN_SUFFIXES and name not in (allowed_private_key_entries or set()):
            raise DistributionValidationError(f"{archive_kind} contains private key material.")
        names.add(name)
        identities.add(identity)
    return names


def _smoke_wheel(wheel: Path, *, name: str, version: str) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise DistributionValidationError("uv is required for the isolated wheel smoke install.")
    with tempfile.TemporaryDirectory(prefix="alysis-wheel-smoke-") as directory:
        target = Path(directory) / "site"
        subprocess.run(
            [
                uv,
                "--cache-dir",
                os.fspath(Path(directory) / "uv-cache"),
                "pip",
                "install",
                "--no-deps",
                "--target",
                os.fspath(target),
                os.fspath(wheel),
            ],
            check=True,
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            [os.fspath(target), environment.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        for argument in ("--help", "--version"):
            completed = subprocess.run(
                [sys.executable, "-m", "alysis_code", argument],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
                timeout=30,
            )
            output = f"{completed.stdout}\n{completed.stderr}"
            if argument == "--version" and version not in output:
                raise DistributionValidationError(
                    f"Installed {name} wheel reported the wrong version."
                )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate and smoke exact Python release archives."
    )
    parser.add_argument("dist_dir", type=Path)
    parser.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--no-smoke", action="store_true")
    args = parser.parse_args()
    validate_distributions(args.dist_dir, project_file=args.project, smoke=not args.no_smoke)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
