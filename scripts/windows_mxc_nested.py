"""Raw pinned SDK nesting test, bypassing Alysis' helper admission code."""

import hashlib
import shutil
import subprocess
import uuid
from pathlib import Path


def qualify(probe: Path, work: Path, contained, record):
    source = probe.resolve(strict=True)
    executable = work / "raw-mxc-nested-probe.exe"
    shutil.copyfile(source, executable)
    record(
        {
            "name": "nested_probe_identity",
            "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        }
    )

    def run(name, sandboxed):
        outside = work.parent / (name + "-" + uuid.uuid4().hex)
        outside.mkdir()
        nonce = uuid.uuid4().hex
        marker = outside / "nested-marker.txt"
        argv = [str(executable), str(work), str(outside), nonce]
        if sandboxed:
            # No explicit negative carve-out for the target: absence of a
            # positive outer grant must itself prevent inner authority expansion.
            event = contained(name, argv)
        else:
            result = subprocess.run(argv, capture_output=True, timeout=15)
            event = {
                "name": name,
                "exit_code": result.returncode,
                "stdout": result.stdout.decode(),
                "stderr": result.stderr.decode(errors="replace"),
            }
            record(event)
            assert result.returncode == 0, event
        observed = {
            "name": name + "_marker",
            "exists": marker.exists(),
            "exact": marker.exists() and marker.read_text().strip() == nonce,
        }
        record(observed)
        if sandboxed:
            assert not observed["exists"], observed
            assert any(
                value in event["stdout"]
                for value in [
                    "NESTED_VALIDATE_REFUSED",
                    "NESTED_SPAWN_REFUSED",
                    "NESTED_CHILD_EXIT=1",
                ]
            ), event
        else:
            assert observed["exact"] and "NESTED_CHILD_EXIT=0" in event["stdout"], observed

    run("host_nested_before", False)
    run("contained_nested_outside", True)
    run("host_nested_after", False)
    record(
        {
            "name": "nested_boundary_passed",
            "scope": "inner SDK outside-cwd/write grant refused by outer policy",
        }
    )
