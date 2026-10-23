"""Exercise legacy ACL calls under the application's actual Docker defaults.

Linux integration check; uses disposable fixture files only. This does not fix
libacl or claim that path-based APIs are safe in privileged administration tools.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch


class Acl:
    def __init__(self) -> None:
        self.lib = ctypes.CDLL("libacl.so.1", use_errno=True)
        self.lib.acl_from_text.argtypes = [ctypes.c_char_p]
        self.lib.acl_from_text.restype = ctypes.c_void_p
        self.lib.acl_set_file.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
        self.lib.acl_set_file.restype = ctypes.c_int
        self.lib.acl_delete_def_file.argtypes = [ctypes.c_char_p]
        self.lib.acl_delete_def_file.restype = ctypes.c_int
        self.lib.acl_free.argtypes = [ctypes.c_void_p]
        self.lib.acl_free.restype = ctypes.c_int

    def call(self, operation: str, path: Path) -> tuple[int, int]:
        ctypes.set_errno(0)
        if operation == "delete_default":
            result = self.lib.acl_delete_def_file(os.fsencode(path))
        else:
            acl = self.lib.acl_from_text(b"u::rwx,g::rwx,o::rwx")
            if not acl:
                raise RuntimeError("Cannot construct test ACL")
            try:
                result = self.lib.acl_set_file(
                    os.fsencode(path), 0x8000 if operation == "access" else 0x4000, acl
                )
                error = ctypes.get_errno()
            finally:
                self.lib.acl_free(acl)
            return result, error
        return result, ctypes.get_errno()


def prepare_fixture(root: Path, uid: int) -> None:
    """Runs in a separate fixture-preparation container, not the tested profile."""
    if root != Path("/fixture") or uid not in (0, 1000):
        raise ValueError("Unexpected fixture boundary")
    project = root / "project"
    for name, owner in (("owned", uid), ("foreign", 2001), (".git", uid)):
        directory = project / name
        directory.mkdir()
        os.chown(directory, owner, owner)
        directory.chmod(0o777)
        target = directory / "file"
        target.write_text("fixture\n")
        os.chown(target, owner, owner)
        target.chmod(0o777)
        assert Acl().call("default", directory)[0] == 0
        (project / (name.removeprefix(".") + "-link")).symlink_to(
            Path("/workspace") / name, target_is_directory=True
        )
    # The parent's host path exists, but it must not exist in the tested container.
    (project / "outside-link").symlink_to((project / "outside-host-path.txt").read_text())
    helper = project / "identity"
    subprocess.run(
        ["gcc", "-x", "c", "-o", str(helper), "-"],
        input=b'#include <stdio.h>\n#include <unistd.h>\nint main(void){printf("%u %u\\n",(unsigned)getuid(),(unsigned)geteuid());return 0;}\n',
        check=True,
        capture_output=True,
    )
    os.chown(helper, 0 if uid == 1000 else 2001, 0)
    helper.chmod(0o4755)


def inside_check(uid: int) -> dict:
    if os.getuid() != uid or os.geteuid() != uid:
        raise RuntimeError("Unexpected test identity")
    proc = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
        if ":" in line
    )
    capabilities = {
        key: int(proc[key].strip(), 16)
        for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
    }
    assert not any(capabilities.values()), capabilities
    assert proc["NoNewPrivs"].strip() == "1"
    assert sorted(os.listdir("/sys/class/net")) == ["lo"]
    assert not (os.statvfs("/").f_flag & os.ST_RDONLY)
    Path("/tmp/default-policy-writable").write_text("ok")
    # Setuid cannot switch the effective identity under no-new-privileges.
    identity = subprocess.check_output(["/workspace/identity"], text=True).strip()
    assert identity == f"{uid} {uid}", identity
    acl = Acl()
    checks = []
    # Follow an attacker-controlled directory symlink deliberately. Positive
    # controls prove the legacy calls ran, rather than being disabled/stubbed.
    for operation in ("access", "default", "delete_default"):
        suffix = "file" if operation == "access" else ""
        assert acl.call(operation, Path("/workspace/owned-link") / suffix)[0] == 0
        checks.append({"operation": operation, "target": "owned_symlink", "result": "allowed"})
        for name, expected_errno in (("foreign-link", errno.EPERM), ("git-link", errno.EROFS)):
            result, error = acl.call(operation, Path("/workspace") / name / suffix)
            assert result == -1 and error == expected_errno, (operation, name, result, error)
            checks.append(
                {"operation": operation, "target": name, "result": "denied", "errno": error}
            )
        result, error = acl.call(operation, Path("/workspace/outside-link") / suffix)
        assert result == -1 and error == errno.ENOENT, (operation, result, error)
        checks.append({"operation": operation, "target": "unmounted_host_path", "result": "absent"})
    # A foreign file is writable by mode, but its ACL still needs ownership or
    # CAP_FOWNER. This distinguishes the ACL privilege boundary from write access.
    with Path("/workspace/foreign/file").open("a") as stream:
        stream.write("permitted-content-write\n")
    protected = Path("/workspace/.git/file")
    try:
        protected.write_text("must not succeed")
    except OSError as error:
        assert error.errno == errno.EROFS
    else:
        raise AssertionError("Protected metadata is writable")
    for command in (
        ["cp", "--preserve=mode", "/workspace/owned/file", "/workspace/git-link/file"],
        ["cp", "-R", "--preserve=mode", "/workspace/owned/.", "/workspace/git-link/"],
    ):
        completed = subprocess.run(command, capture_output=True)
        assert completed.returncode != 0, command
    assert protected.read_text() == "fixture\n"
    assert not Path(Path("/workspace/outside-host-path.txt").read_text()).exists()
    return {
        "uid": uid,
        "capabilities": capabilities,
        "no_new_privileges": True,
        "root_filesystem_readonly": False,
        "setuid_effective_identity": identity,
        "legacy_acl_checks": checks,
        "protected_coreutils_copy": "denied",
        "status": "passed",
    }


def run_check(image: str) -> dict:
    if sys.platform != "linux":
        raise RuntimeError("Requires a native Linux Docker test host")
    from alysis_code.sandbox_runner import _build_docker_argv

    image_info = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
    results = []
    for uid in (1000, 0):
        with tempfile.TemporaryDirectory(prefix="alysis-acl-boundary-") as directory:
            fixture = Path(directory).resolve()
            fixture.chmod(0o755)
            project = fixture / "project"
            project.mkdir(mode=0o777)
            project.chmod(0o777)
            outside = fixture / "outside"
            outside.mkdir()
            (outside / "file").write_text("unmounted\n")
            (project / "outside-host-path.txt").write_text(str(outside))
            script = project / "check.py"
            script.write_bytes(Path(__file__).read_bytes())
            # Extra capabilities apply only to disposable fixture preparation.
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--user",
                    "0:0",
                    "--cap-drop=ALL",
                    "--cap-add=CHOWN",
                    "--cap-add=FOWNER",
                    "--cap-add=FSETID",
                    "--volume",
                    f"{fixture}:/fixture:rw",
                    image_info["Id"],
                    "python",
                    "/fixture/project/check.py",
                    "--prepare",
                    str(uid),
                ],
                check=True,
                capture_output=True,
            )
            protected_before = (project / ".git/file").read_bytes()
            acl_before = os.getxattr(project / ".git", "system.posix_acl_default")
            with (
                patch.object(os, "getuid", return_value=uid),
                patch.object(os, "getgid", return_value=uid),
            ):
                argv, environment = _build_docker_argv(
                    root=project,
                    cwd=project,
                    cmd=f"python /workspace/check.py --inside {uid}",
                    container_name="alysis-acl-" + uuid.uuid4().hex[:12],
                    network="off",
                    docker_image=image_info["Id"],
                    clear_env=True,
                    pids_limit=None,
                    memory_limit=None,
                    cpus=None,
                    read_only_rootfs=False,
                    protect_repo_meta=True,
                    env_allowlist=(),
                )
            assert not any(
                flag in argv for flag in ("--read-only", "--memory", "--cpus", "--pids-limit")
            )
            completed = subprocess.run(
                argv, env=environment, text=True, capture_output=True, timeout=90
            )
            if completed.returncode:
                raise RuntimeError(f"ACL boundary check failed for UID {uid}: {completed.stderr}")
            result = json.loads(completed.stdout)
            assert (project / ".git/file").read_bytes() == protected_before
            assert os.getxattr(project / ".git", "system.posix_acl_default") == acl_before
            assert (outside / "file").read_text() == "unmounted\n"
            assert stat.S_IMODE((project / "foreign/file").stat().st_mode) == 0o777
            result["docker_argv"] = shlex.join(argv)
            results.append(result)
    return {
        "schema_name": "sandbox-acl-boundary",
        "schema_version": 1,
        "image_id": image_info["Id"],
        "architecture": image_info["Architecture"],
        "checker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "application_policy_sha256": hashlib.sha256(
            Path(sys.modules[_build_docker_argv.__module__].__file__).read_bytes()
        ).hexdigest(),
        "source_commit": os.environ.get("GITHUB_SHA"),
        "application_policy": "_build_docker_argv defaults; writable rootfs; no resource limits",
        "results": results,
        "status": "passed",
        "scope": "Application-controlled Docker execution only; not a libacl caller fix or host-kernel qualification",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--inside", type=int, choices=(0, 1000))
    parser.add_argument("--prepare", type=int, choices=(0, 1000))
    args = parser.parse_args()
    if args.prepare is not None:
        prepare_fixture(Path("/fixture"), args.prepare)
    elif args.inside is not None:
        print(json.dumps(inside_check(args.inside)))
    else:
        if not args.image or not args.output:
            parser.error("--image and --output are required")
        receipt = run_check(args.image)
        args.output.write_text(json.dumps(receipt, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "status": receipt["status"],
                    "image_id": receipt["image_id"],
                    "tested_uids": [1000, 0],
                }
            )
        )


if __name__ == "__main__":
    main()
