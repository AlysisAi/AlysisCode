"""Synthetic multi-folder integration against installed code and real Windows MXC."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
import uuid
from dataclasses import asdict, replace
from pathlib import Path

from alysis_code.contained_read_tools import ContainedReadTools
from alysis_code.project_folders import folder_scope
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.tools.fs import FsError, capture_file_precondition, fs_read


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    output = Path(sys.argv[1]).resolve()
    output.mkdir(exist_ok=False)
    folders = [output / name for name in ["Primary", "Design assets", "Σημειώσεις"]]
    outside = output / "Unselected"
    for path in [*folders, outside]:
        path.mkdir()
        (path / "canary.txt").write_text(str(uuid.uuid4()), encoding="utf-8")
    primary, extra, unicode = folders
    settings = ShellSandboxSettings(
        backend="windows-mxc", mode="strict", network="off", filesystem_access="workspace-write"
    )
    report = {"executable": sys.executable, "backend": settings.backend, "results": []}

    def check(name, action):
        try:
            action()
            report["results"].append({"name": name, "status": "pass"})
        except Exception:
            report["results"].append(
                {"name": name, "status": "fail", "traceback": traceback.format_exc()}
            )
        print(name, report["results"][-1]["status"], flush=True)
        (output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    def edits(folder):
        with folder_scope(primary, [str(extra), str(unicode)]):
            worker = ContainedReadTools(settings, primary)
            assert (
                fs_read(root=primary, path=str(folder / "canary.txt"))["content"]
                == (folder / "canary.txt").read_text()
            )
            for text in ["created", "edited"]:
                path = str(folder / "written.txt")
                worker.run_write(
                    "fs_write_prepared",
                    {
                        "path": path,
                        "content": text,
                        "precondition": asdict(capture_file_precondition(root=primary, path=path)),
                    },
                )
                assert (folder / "written.txt").read_text() == text
            worker.run_write(
                "fs_copy",
                {
                    "source_path": str(folder / "written.txt"),
                    "destination_path": str(folder / "copy.txt"),
                },
            )
            worker.run_write(
                "fs_move",
                {
                    "source_path": str(folder / "copy.txt"),
                    "destination_path": str(folder / "moved.txt"),
                },
            )
            assert (folder / "moved.txt").read_text() == "edited" and not (
                folder / "copy.txt"
            ).exists()

    def reads(folder):
        with folder_scope(primary, [str(extra), str(unicode)]):
            result = ContainedReadTools(settings, primary).run(
                "fs_list", {"root_path": str(folder)}
            )
            assert "canary.txt" in json.dumps(result)

    for folder in folders:
        check("contained-read-" + folder.name, lambda folder=folder: reads(folder))
        check("real-file-tools-" + folder.name, lambda folder=folder: edits(folder))

    def denied(action):
        try:
            action()
        except (FsError, ValueError, PermissionError) as exc:
            assert any(
                word in str(exc).lower()
                for word in [
                    "outside",
                    "escape",
                    "denied",
                    "read-only",
                    "hardlink",
                    "reparse",
                    "symbolic",
                    "junction",
                ]
            ), str(exc)
            return
        raise AssertionError("Operation unexpectedly admitted")

    with folder_scope(primary, [str(extra), str(unicode)]):
        check(
            "unselected-file-read-denied",
            lambda: denied(lambda: fs_read(root=primary, path=str(outside / "canary.txt"))),
        )
        for folder in folders:

            def readonly(folder=folder):
                worker = ContainedReadTools(
                    replace(settings, filesystem_access="read-only"), primary
                )
                try:
                    worker.run_write("fs_mkdir", {"path": str(folder / "must-not-create")})
                except Exception as exc:
                    assert "read-only" in str(exc)
                else:
                    raise AssertionError("read-only write admitted")
                assert not (folder / "must-not-create").exists()

            check("file-tool-readonly-" + folder.name, readonly)
    # A fresh worker has no project-derived grant after the turn scope is gone.
    check(
        "additional-folder-revoked-next-turn",
        lambda: denied(lambda: fs_read(root=primary, path=str(extra / "canary.txt"))),
    )

    def restarted():
        code = "from pathlib import Path; from alysis_code.tools.fs import fs_read, FsError; import sys;\ntry: fs_read(root=Path(sys.argv[1]),path=sys.argv[2])\nexcept FsError as e: print(str(e)); sys.exit(0)\nsys.exit(1)"
        result = subprocess.run(
            [sys.executable, "-I", "-c", code, str(primary), str(extra / "canary.txt")],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0 and any(
            word in result.stdout.lower() for word in ["outside", "escape"]
        ), result.stdout + result.stderr

    check("additional-folder-revoked-fresh-process", restarted)

    def junction():
        alias = extra / "outside-junction"
        made = subprocess.run(
            [os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(alias), str(outside)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert made.returncode == 0, made.stdout + made.stderr
        with folder_scope(primary, [str(extra), str(unicode)]):
            denied(lambda: fs_read(root=primary, path=str(alias / "canary.txt")))

    check("junction-to-unselected-denied", junction)
    report["passed"] = all(row["status"] == "pass" for row in report["results"])
    (output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
