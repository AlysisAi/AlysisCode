"""Refuse to publish a CLI package without its exact offline sandbox payload."""

import ast
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import sysconfig
import tempfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


def load_source_module(name, path):
    spec = spec_from_file_location(name, path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def candidate_file(path, limit):
    """Read bounded, ordinary candidate inputs without following aliases."""
    if not path.is_absolute():
        raise ValueError("Local sandbox candidate requires an absolute path")
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError("Local sandbox candidate cannot traverse links or reparse points")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise ValueError("Local sandbox candidate requires bounded single-link files")
    return path.read_bytes()


def require_clean_candidate(root, source_commit):
    """Permit only the official generator's data-only stamp for this clean commit.

    The native inventory still binds the exact stamp bytes before and after the
    build. Untracked/staged files, executable stamp code and other edits refuse.
    """
    status = subprocess.check_output(["git", "status", "--porcelain", "-z"], cwd=root)
    if not status:
        return
    if status != b" M src/alysis_code/_build_info.py\0":
        raise ValueError("Local sandbox candidate packaging requires a clean committed checkout")
    path = root / "src/alysis_code/_build_info.py"
    values = {}
    try:
        tree = ast.parse(candidate_file(path, 64 * 1024).decode("utf-8"))
        for index, statement in enumerate(tree.body):
            if (
                index == 0
                and isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            ):
                continue
            if (
                not isinstance(statement, ast.Assign)
                or len(statement.targets) != 1
                or not isinstance(statement.targets[0], ast.Name)
            ):
                raise ValueError("Build stamp must contain only generated constants")
            name = statement.targets[0].id
            if name in values:
                raise ValueError("Duplicate build stamp constant")
            values[name] = ast.literal_eval(statement.value)
        subject = (
            subprocess.check_output(["git", "show", "-s", "--format=%s", "HEAD"], cwd=root)
            .decode()
            .strip()
        )
        main = subprocess.run(
            ["git", "rev-parse", "refs/remotes/origin/main"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        allowed_main = {"unavailable"}
        if main.returncode == 0:
            allowed_main.add(main.stdout.strip())
        if (
            set(values)
            != {
                "BUILD_COMMIT",
                "BUILD_COMMIT_SUBJECT",
                "BUILD_TIMESTAMP",
                "BUILD_DIRTY",
                "BUILD_SOURCE",
                "BUILD_RESOLVED_ORIGIN_MAIN",
                "BUILD_INFO_SCHEMA_VERSION",
            }
            or values["BUILD_COMMIT"] != source_commit
            or values["BUILD_COMMIT_SUBJECT"] != subject
            or values["BUILD_DIRTY"] is not False
            or values["BUILD_SOURCE"] != "git"
            or type(values["BUILD_INFO_SCHEMA_VERSION"]) is not int
            or values["BUILD_INFO_SCHEMA_VERSION"] != 2
            or values["BUILD_RESOLVED_ORIGIN_MAIN"] not in allowed_main
            or not isinstance(values["BUILD_TIMESTAMP"], str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", values["BUILD_TIMESTAMP"])
        ):
            raise ValueError("Build stamp does not identify the clean current commit")
    except (KeyError, SyntaxError, TypeError, UnicodeError, ValueError) as error:
        raise ValueError(
            "Local sandbox candidate requires a valid generated build stamp"
        ) from error


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if os.environ.get("ALYSIS_LOCAL_CANDIDATE") == "1":
            if sys.platform == "darwin" and not os.environ.get("ALYSIS_WINDOWS_SANDBOX_BUNDLE"):
                if (
                    self.target_name != "wheel"
                    or version != "standard"
                    or any(
                        os.environ.get(name) == "1"
                        for name in (
                            "ALYSIS_RELEASE_CANDIDATE",
                            "ALYSIS_WINDOWS_RELEASE",
                            "ALYSIS_RELEASE_BUILD",
                        )
                    )
                ):
                    raise ValueError(
                        "Local Mac candidates support standard non-release wheels only"
                    )
                # The candidate is usable only on this Mac platform. Windows
                # payload qualification is separate; never emit a universal
                # wheel claiming the changed Windows source/payload is verified.
                platform_tag = sysconfig.get_platform().replace("-", "_").replace(".", "_")
                build_data["tag"] = "py3-none-" + platform_tag
                build_data["pure_python"] = False
                return
            self.initialize_candidate(version, build_data)
            return
        module_path = Path(self.root) / "src/alysis_code/windows_mxc_bundle.py"
        spec = spec_from_file_location("_alysis_windows_bundle_build", module_path)
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        module.verify_bundle()
        if (Path(self.root) / "native/windows-sandbox").is_dir():
            inventory = json.loads((module.BUNDLE_ROOT / "SOURCE.json").read_text())
            for name, expected in inventory["files"].items():
                if not name.startswith("native/windows-sandbox/") and name not in {
                    "src/alysis_code/windows_mxc_contract.py",
                    "scripts/build_windows_mxc.py",
                }:
                    continue
                actual = hashlib.sha256((Path(self.root) / name).read_bytes()).hexdigest()
                if actual != expected:
                    raise ValueError(
                        f"Rebuild/sign the bundled sandbox after native source changes: {name}"
                    )

    def initialize_candidate(self, version, build_data):
        """Package a real local build without changing any checked-in release pins."""
        if any(
            os.environ.get(name) == "1"
            for name in ("ALYSIS_RELEASE_CANDIDATE", "ALYSIS_WINDOWS_RELEASE")
        ):
            raise ValueError("Local sandbox candidates cannot be used for release packaging")
        if self.target_name != "wheel" or version != "standard":
            raise ValueError("Local sandbox candidates support standard wheels only, not releases")
        configured = os.environ.get("ALYSIS_WINDOWS_SANDBOX_BUNDLE", "")
        expected = os.environ.get("ALYSIS_WINDOWS_SANDBOX_BUNDLE_SHA256", "")
        if not configured or not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise ValueError("Local sandbox candidate requires a bundle and independent SHA-256")
        root = Path(self.root)
        candidate = Path(configured)
        payload = {
            "alysis-sandbox.exe": candidate_file(
                candidate / "alysis-sandbox.exe", 32 * 1024 * 1024
            ),
            "SOURCE.json": candidate_file(candidate / "SOURCE.json", 4 * 1024 * 1024),
            "manifest.json": candidate_file(candidate / "manifest.json", 1024 * 1024),
            "LICENSE.mxc": candidate_file(root / "native/windows-sandbox/LICENSE.mxc", 1024 * 1024),
        }
        raw = json.loads(payload["manifest.json"])
        inventory = json.loads(payload["SOURCE.json"])
        builder = load_source_module(
            "_alysis_mxc_builder", root / "src/alysis_code/windows_mxc_candidate_builder.py"
        )
        if any(raw.get(key) != value for key, value in builder.helper_contract().items()):
            raise ValueError("Local sandbox candidate policy contract does not match this engine")
        binary = payload["alysis-sandbox.exe"]
        pe_offset = int.from_bytes(binary[60:64], "little")
        if (
            len(binary) < 128
            or binary[:2] != b"MZ"
            or binary[pe_offset : pe_offset + 6] != b"PE\0\0\x64\x86"
            or raw.get("platform") != "win32-x64"
            or hashlib.sha256(binary).hexdigest() != expected
            or raw.get("sha256") != expected
        ):
            raise ValueError(
                "Local sandbox candidate binary does not match the independent x64 pin"
            )
        if (
            hashlib.sha256(payload["SOURCE.json"]).hexdigest() != raw.get("source_inventory_sha256")
            or inventory != builder.source_inventory(root)
            or raw.get("source_commit") != inventory.get("source_commit")
            or "hatch_build.py" not in inventory.get("files", {})
        ):
            raise ValueError(
                "Local sandbox candidate provenance does not match this source checkout"
            )
        require_clean_candidate(root, inventory["source_commit"])
        self._candidate_inventory = inventory
        self._candidate_builder = builder
        self._candidate_temp = tempfile.TemporaryDirectory(prefix="alysis-local-mxc-wheel-")
        staging = Path(self._candidate_temp.name)
        forced = build_data.setdefault("force_include", {})
        for name, data in payload.items():
            destination = staging / name
            destination.write_bytes(data)
            forced[str(destination)] = f"alysis_code/assets/windows-mxc/{name}"
        marker = {
            "schema_version": 1,
            "qualification": "prototype-unqualified",
            "source_commit": inventory["source_commit"],
            "helper_bytes": len(binary),
            "files": {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()},
        }
        marker_path = staging / "_windows_mxc_local.json"
        marker_path.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        forced[str(marker_path)] = "alysis_code/_windows_mxc_local.json"

    def finalize(self, version, build_data, artifact_path):
        if not hasattr(self, "_candidate_temp"):
            return
        try:
            if (
                self._candidate_builder.source_inventory(Path(self.root))
                != self._candidate_inventory
            ):
                raise ValueError("Local sandbox candidate sources changed while building the wheel")
        finally:
            self._candidate_temp.cleanup()
