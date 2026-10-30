from __future__ import annotations

import argparse
import ast
import email
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

from cryptography import x509


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
_GIT_CA_SHA256 = "76055a37e6fbc802e2051bcb620210a915d112f4284a5e93fb2e68c7106398ba"
_PUBLIC_CERTIFICATE_ASSETS = ("alysis_code/assets/windows-git/ucrt64/etc/ssl/cert.pem",)
_SDIST_FILES = {
    ".gitignore",
    "docs/CHANGELOG.md",
    "LICENSE",
    "NOTICE",
    "PKG-INFO",
    "README.md",
    "pyproject.toml",
    "hatch_build.py",
}


def validate_distributions(
    dist_dir: Path,
    *,
    project_file: Path,
    smoke: bool = True,
    expected_commit: str | None = None,
) -> tuple[Path, Path]:
    if expected_commit is not None and re.fullmatch(r"[a-f0-9]{40}", expected_commit) is None:
        raise DistributionValidationError("Expected source commit must be an exact Git SHA.")
    project_document = tomllib.loads(project_file.read_text(encoding="utf-8"))
    project = project_document["project"]
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
    certificates = _pinned_public_certificates(project_file.parent)
    wheel_stamp = _validate_wheel(
        wheel,
        name=name,
        version=version,
        normalized=normalized,
        expected_commit=expected_commit,
        public_certificates=certificates,
    )
    sdist_stamp = _validate_sdist(
        sdist,
        normalized=normalized,
        version=version,
        expected_commit=expected_commit,
        public_certificates=certificates,
    )
    if expected_commit is not None and wheel_stamp != sdist_stamp:
        raise DistributionValidationError("Wheel and sdist build stamps differ.")
    hook = (
        project_document.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("hooks", {})
        .get("custom", {})
    )
    if hook.get("path") == "hatch_build.py":
        from alysis_code.windows_mxc_bundle import BUNDLE_FILES

        with ZipFile(wheel) as wheel_archive, tarfile.open(sdist, "r:gz") as source_archive:
            if "alysis_code/_windows_mxc_local.json" in wheel_archive.namelist():
                raise DistributionValidationError(
                    "Local Windows sandbox candidates cannot be released."
                )
            for filename, expected in BUNDLE_FILES.items():
                member = f"alysis_code/assets/windows-mxc/{filename}"
                try:
                    source_file = source_archive.extractfile(f"{normalized}-{version}/src/{member}")
                    if source_file is None:
                        raise KeyError(member)
                    for payload in (wheel_archive.read(member), source_file.read()):
                        if hashlib.sha256(payload).hexdigest() != expected:
                            raise DistributionValidationError(
                                f"Windows sandbox bundle changed: {filename}"
                            )
                except KeyError as exc:
                    raise DistributionValidationError(
                        f"Windows sandbox bundle is missing: {filename}"
                    ) from exc
    if smoke:
        _smoke_wheel(wheel, name=name, version=version, expected_commit=expected_commit)
    return wheel, sdist


def _validate_build_stamp(source: bytes, *, expected_commit: str) -> bytes:
    """Read literal metadata only; never execute code from an archive being admitted."""
    if len(source) > 16 * 1024:
        raise DistributionValidationError("Build stamp exceeds its size limit.")
    required = {
        "BUILD_COMMIT",
        "BUILD_COMMIT_SUBJECT",
        "BUILD_TIMESTAMP",
        "BUILD_DIRTY",
        "BUILD_SOURCE",
        "BUILD_RESOLVED_ORIGIN_MAIN",
        "BUILD_INFO_SCHEMA_VERSION",
    }
    values = {}
    try:
        for index, node in enumerate(ast.parse(source.decode("utf-8")).body):
            if (
                index == 0
                and isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                continue
            if (
                not isinstance(node, ast.Assign)
                or len(node.targets) != 1
                or not isinstance(node.targets[0], ast.Name)
            ):
                raise ValueError("Nonliteral build stamp statement")
            key = node.targets[0].id
            if key not in required or key in values:
                raise ValueError("Unexpected or repeated build stamp field")
            values[key] = ast.literal_eval(node.value)
        if set(values) != required:
            raise ValueError("Missing build stamp fields")
        if (
            values["BUILD_COMMIT"] != expected_commit
            or values["BUILD_DIRTY"] is not False
            or values["BUILD_SOURCE"] != "git"
        ):
            raise ValueError("Build stamp does not identify the clean expected commit")
        if (
            type(values["BUILD_INFO_SCHEMA_VERSION"]) is not int
            or values["BUILD_INFO_SCHEMA_VERSION"] != 2
        ):
            raise ValueError("Unsupported build stamp schema")
        if (
            not isinstance(values["BUILD_COMMIT_SUBJECT"], str)
            or len(values["BUILD_COMMIT_SUBJECT"]) > 200
        ):
            raise ValueError("Invalid build commit subject")
        timestamp = values["BUILD_TIMESTAMP"]
        if not isinstance(timestamp, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", timestamp
        ):
            raise ValueError("Invalid build timestamp")
        datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
        remote = values["BUILD_RESOLVED_ORIGIN_MAIN"]
        if not isinstance(remote, str) or (
            remote != "unavailable" and re.fullmatch(r"[a-f0-9]{40}", remote) is None
        ):
            raise ValueError("Invalid remote provenance")
    except (SyntaxError, UnicodeDecodeError, ValueError, TypeError, RecursionError) as exc:
        raise DistributionValidationError(f"Invalid build stamp: {exc}") from exc
    return source


def _pinned_public_certificates(root: Path) -> dict[str, bytes]:
    """Only exact reviewed runtime assets, never arbitrary PEM archive entries."""
    assets = {}
    block = re.compile(
        rb"-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----"
    )
    for name in _PUBLIC_CERTIFICATE_ASSETS:
        path = root / "src" / name
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 1024 * 1024:
            raise DistributionValidationError("Pinned public certificate asset is unsafe.")
        data = path.read_bytes()
        remainder = block.sub(b"", data)
        try:
            remainder.decode("utf-8")
            certificates = x509.load_pem_x509_certificates(data)
            if len(certificates) != len(block.findall(data)) or any(
                line.strip() and not line.lstrip().startswith(b"#")
                for line in remainder.splitlines()
            ):
                raise ValueError("Non-certificate material")
        except (ValueError, UnicodeDecodeError) as exc:
            raise DistributionValidationError(
                "Pinned public certificate bundle contains non-certificate material."
            ) from exc
        if hashlib.sha256(data).hexdigest() != _GIT_CA_SHA256:
            raise DistributionValidationError("Portable Git public CA bundle differs from its pin.")
        assets[name] = data
    return assets


def _validate_wheel(
    wheel: Path,
    *,
    name: str,
    version: str,
    normalized: str,
    expected_commit: str | None = None,
    public_certificates: dict[str, bytes] | None = None,
) -> bytes | None:
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
                allowed_private_key_entries=set(public_certificates or {}),
            )
            for entry, expected in (public_certificates or {}).items():
                if entry not in names or archive.read(entry) != expected:
                    raise DistributionValidationError(
                        "Wheel public certificates differ from the pinned repository asset."
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
            if expected_commit is not None:
                stamp = "alysis_code/_build_info.py"
                if stamp not in names or archive.getinfo(stamp).file_size > 16 * 1024:
                    raise DistributionValidationError("Wheel build stamp is missing or oversized.")
                return _validate_build_stamp(archive.read(stamp), expected_commit=expected_commit)
    except (BadZipFile, KeyError, UnicodeDecodeError) as exc:
        raise DistributionValidationError("Wheel archive is invalid.") from exc
    return None


def _validate_sdist(
    sdist: Path,
    *,
    normalized: str,
    version: str,
    expected_commit: str | None = None,
    public_certificates: dict[str, bytes] | None = None,
) -> bytes | None:
    prefix = f"{normalized}-{version}/"
    allowed_private_key_entries = set()
    allowed_private_key_entries.update(
        f"{prefix}src/{name}" for name in (public_certificates or {})
    )
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
                allowed_private_key_entries=allowed_private_key_entries,
            )
            for name, expected in (public_certificates or {}).items():
                entry = f"{prefix}src/{name}"
                archived = archive.extractfile(entry) if entry in names else None
                if archived is None or archived.read() != expected:
                    raise DistributionValidationError(
                        "Sdist public certificates differ from the pinned repository asset."
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
            if expected_commit is not None:
                stamp = f"{prefix}src/alysis_code/_build_info.py"
                if stamp not in names or archive.getmember(stamp).size > 16 * 1024:
                    raise DistributionValidationError("Sdist build stamp is missing or oversized.")
                stream = archive.extractfile(stamp)
                if stream is None:
                    raise DistributionValidationError("Sdist build stamp is not a file.")
                return _validate_build_stamp(stream.read(), expected_commit=expected_commit)
    except (tarfile.TarError, OSError) as exc:
        raise DistributionValidationError("Sdist archive is invalid.") from exc
    return None


def _is_allowed_sdist_member(relative_name: str) -> bool:
    return relative_name in _SDIST_FILES or relative_name.startswith("src/alysis_code/")


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


def _smoke_wheel(
    wheel: Path, *, name: str, version: str, expected_commit: str | None = None
) -> None:
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
            if argument == "--version" and expected_commit is not None:
                for marker in (f"commit: {expected_commit[:12]}", "dirty: no", "source: git"):
                    if marker not in output:
                        raise DistributionValidationError(
                            "Installed wheel did not report its expected clean build identity."
                        )
        subprocess.run(
            [
                sys.executable,
                os.fspath(Path(__file__).with_name("smoke_hosted_login.py")),
                "--expected-root",
                os.fspath(target),
            ],
            cwd=directory,
            env=environment,
            check=True,
            timeout=60,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate and smoke exact Python release archives."
    )
    parser.add_argument("dist_dir", type=Path)
    parser.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--no-smoke", action="store_true")
    parser.add_argument(
        "--expected-commit", required=True, help="Exact independently selected source commit."
    )
    args = parser.parse_args()
    validate_distributions(
        args.dist_dir,
        project_file=args.project,
        smoke=not args.no_smoke,
        expected_commit=args.expected_commit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
