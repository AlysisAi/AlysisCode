"""Bounded installed-runtime qualification; creates only a new synthetic directory.

Run with the installed candidate's Python, never a source PYTHONPATH. Native MXC
requires the separately verified ALYSIS_WINDOWS_MXC_HELPER/SHA256 environment.
Independent checks continue after failures. A dependent check is NOT RUN when
its prerequisite failed; neither missing observations nor NOT RUN count as passes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import traceback
from dataclasses import asdict
from pathlib import Path

CHECK_NAMES = (
    "fs_list",
    "search_rg",
    "symbol_search",
    "repo_map",
    "test_discover",
    "git_status",
    "git_diff",
    "git_history",
    "contained_git_patch",
    "git_metadata_unchanged",
    "large_prepared_write",
    "stale_write_refused",
    "contained_copy",
    "contained_root_overwrite",
    "contained_move",
    "contained_cross_directory_move",
    "contained_delete",
    "contained_mkdir",
    "image_bytes_publication",
    "tool_fs_read",
    "tool_fs_write",
    "tool_search",
    "private_hardlink_not_disclosed",
    "request_packets_removed",
    "private_canary_unchanged",
)
PRIVATE_CANARY = "SYNTHETIC_PRIVATE_FILE_CANARY"


def require(value):
    if not value:
        raise AssertionError("qualification assertion failed")


class Observations:
    def __init__(self, report):
        self.report = report
        self.seen = set()

    def claim(self, name):
        if name not in CHECK_NAMES or name in self.seen:
            raise ValueError("unknown or duplicate qualification check: " + name)
        self.seen.add(name)

    def not_run(self, name, reason, *, blocked_by=()):
        self.claim(name)
        self.report["not_run"].append(
            {"check": name, "reason": reason, "blocked_by": list(blocked_by)}
        )
        print(name + ": NOT RUN", flush=True)

    def check(self, name, action, *, requires=()):
        missing = [n for n in requires if n not in self.report["checks"]]
        if missing:
            self.not_run(name, "prerequisite did not pass", blocked_by=missing)
            return False
        self.claim(name)
        try:
            action()
        except Exception:
            self.report["failures"].append({"check": name, "traceback": traceback.format_exc()})
            print(name + ": FAILED", flush=True)
            return False
        self.report["checks"].append(name)
        print(name + ": passed", flush=True)
        return True

    def finish(self):
        for name in CHECK_NAMES:
            if name not in self.seen:
                self.not_run(name, "qualification stopped before this check")
        self.report["passed"] = (
            len(self.report["checks"]) == len(CHECK_NAMES)
            and not self.report["failures"]
            and not self.report["not_run"]
            and not self.report.get("failure")
            and not self.report.get("fixture_failures")
        )
        return 0 if self.report["passed"] else 1


def run_file_checks(observations, *, root, canary, worker, tools_factory, stamp, stale_error):
    """Exercise real operations; dependencies and fixture setup stay explicit.

    Host-seeded inputs make copy/overwrite/move/delete independent observations.
    They do not stand in for a failed sandbox write. Only the stale-write check
    reuses the preceding sandbox write and must therefore depend on its success.
    """
    check = observations.check
    for name, arguments in (
        ("fs_list", {}),
        ("search_rg", {"pattern": "test_marker_function"}),
        ("symbol_search", {"query": "test_marker_function"}),
        ("repo_map", {"paths": ["test_sample.py"]}),
        ("test_discover", {"paths": ["test_sample.py"]}),
        ("git_status", {}),
        ("git_diff", {}),
        ("git_history", {"mode": "log"}),
    ):

        def inspect_reader(n=name, a=arguments):
            result = worker.run(n, a)
            require(isinstance(result, dict))
            # Empty success envelopes can hide denied reads: require content.
            text = json.dumps(result)
            if n in {"fs_list", "repo_map", "test_discover"}:
                require("test_sample.py" in text)
            elif n in {"search_rg", "symbol_search"}:
                require("test_marker_function" in text)
                require(bool(result.get("matches")))
            elif n == "git_history":
                require("Synthetic fixture" in text)

        check(name, inspect_reader)

    def git_snapshot():
        return {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (root / ".git").rglob("*")
            if p.is_file()
        }

    # Snapshot errors belong to the metadata check, without hiding the patch.
    try:
        before = git_snapshot()
        snapshot_error = None
    except Exception:
        before = None
        snapshot_error = traceback.format_exc()

    def inspect_patch():
        require(
            worker.run_write(
                "git_apply_patch",
                {
                    "patch": "diff --git a/ordinary.txt b/ordinary.txt\n--- a/ordinary.txt\n+++ b/ordinary.txt\n@@ -1 +1 @@\n-old\n+patched\n",
                    "preconditions": [stamp("ordinary.txt")],
                },
            )
            == {"applied": True}
        )
        require((root / "ordinary.txt").read_text() == "patched\n")

    def inspect_metadata():
        if snapshot_error:
            raise RuntimeError("initial Git metadata snapshot failed:\n" + snapshot_error)
        require(before == git_snapshot())

    check("contained_git_patch", inspect_patch)
    check("git_metadata_unchanged", inspect_metadata)

    payload = {"path": "nested/large.txt", "content": "synthetic\n" * 12000}

    def inspect_large_write():
        payload["precondition"] = stamp(payload["path"])
        require(worker.run_write("fs_write_prepared", payload)["created"])
        require((root / payload["path"]).read_text() == payload["content"])

    def inspect_stale_write():
        try:
            worker.run_write("fs_write_prepared", payload)
        except stale_error:
            return
        raise AssertionError("stale mutation succeeded")

    check("large_prepared_write", inspect_large_write)
    check("stale_write_refused", inspect_stale_write, requires=("large_prepared_write",))

    def seed(path, content):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def inspect_copy():
        seed("copy-source/large.txt", payload["content"])
        require(
            isinstance(
                worker.run_write(
                    "fs_copy",
                    {"source_path": "copy-source/large.txt", "destination_path": "copy.txt"},
                ),
                dict,
            )
        )
        require((root / "copy.txt").read_text() == payload["content"])

    def inspect_overwrite():
        seed("overwrite.txt", "initial content")
        require(
            isinstance(
                worker.run_write(
                    "fs_write_prepared",
                    {
                        "path": "overwrite.txt",
                        "content": "root replacement",
                        "precondition": stamp("overwrite.txt"),
                    },
                ),
                dict,
            )
        )
        require((root / "overwrite.txt").read_text() == "root replacement")

    def inspect_move(source, destination):
        seed(source, "move fixture")
        # This operation tests moving a file into an existing directory.
        (root / destination).parent.mkdir(parents=True, exist_ok=True)
        require(
            isinstance(
                worker.run_write(
                    "fs_move", {"source_path": source, "destination_path": destination}
                ),
                dict,
            )
        )
        require(not (root / source).exists())
        require((root / destination).read_text() == "move fixture")

    def inspect_delete():
        seed("delete-source/file.txt", "delete fixture")
        require(isinstance(worker.run_write("fs_delete", {"path": "delete-source/file.txt"}), dict))
        require(not (root / "delete-source/file.txt").exists())

    def inspect_mkdir():
        require(isinstance(worker.run_write("fs_mkdir", {"path": "created-folder"}), dict))
        require((root / "created-folder").is_dir())

    def inspect_image():
        require(
            worker.run_write(
                "image_create_bytes",
                {
                    "path": "images/synthetic.bin",
                    "payload": base64.b64encode(b"bounded synthetic bytes").decode(),
                },
            )
            == {"created": True}
        )
        require((root / "images/synthetic.bin").read_bytes() == b"bounded synthetic bytes")

    check("contained_copy", inspect_copy)
    check("contained_root_overwrite", inspect_overwrite)
    check("contained_move", lambda: inspect_move("move-source.txt", "moved.txt"))
    check(
        "contained_cross_directory_move",
        lambda: inspect_move("cross-move-source.txt", "move-destination/moved.txt"),
    )
    check("contained_delete", inspect_delete)
    check("contained_mkdir", inspect_mkdir)
    check("image_bytes_publication", inspect_image)

    try:
        tools = tools_factory()
    except Exception:
        observations.report.setdefault("fixture_failures", []).append(
            {"fixture": "tools", "traceback": traceback.format_exc()}
        )
        for name in (
            "tool_fs_read",
            "tool_fs_write",
            "tool_search",
            "private_hardlink_not_disclosed",
        ):
            observations.not_run(name, "tool fixture initialization failed", blocked_by=("tools",))
    else:
        check(
            "tool_fs_read",
            lambda: require(
                "READ_TOOL_FIXTURE"
                in json.dumps(tools["fs_read"].run({"path": "read-sentinel.txt"}))
            ),
        )

        def inspect_tool_write():
            require(
                isinstance(
                    tools["fs_write"].run({"path": "tool-created.txt", "content": "tool content"}),
                    dict,
                )
            )
            require((root / "tool-created.txt").read_text() == "tool content")

        check("tool_fs_write", inspect_tool_write)
        check(
            "tool_search",
            lambda: require(
                "test_marker_function"
                in json.dumps(tools["search_rg"].run({"pattern": "test_marker_function"}))
            ),
        )

        def inspect_private_hardlink():
            link = root / "linked-private.txt"
            os.link(canary, link)
            try:
                for name, arguments in (
                    ("fs_read", {"path": "linked-private.txt"}),
                    ("search_rg", {"pattern": "SYNTHETIC_PRIVATE"}),
                ):
                    try:
                        value = tools[name].run(arguments)
                    except Exception:
                        value = {}
                    require(PRIVATE_CANARY not in json.dumps(value))
            finally:
                link.unlink()

        check("private_hardlink_not_disclosed", inspect_private_hardlink)

    check(
        "request_packets_removed",
        lambda: require(not list(root.glob(".alysis-file-request-*.json"))),
    )
    check("private_canary_unchanged", lambda: require(canary.read_text() == PRIVATE_CANARY))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--backend", choices=("windows-mxc", "bwrap"), required=True)
    options = parser.parse_args()
    output = options.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    root = output / "workspace"
    root.mkdir()
    private = output / "private"
    private.mkdir()
    canary = private / "canary.txt"
    canary.write_text(PRIVATE_CANARY, encoding="utf-8")
    os.environ.update(
        {
            "ALYSIS_CONFIG_DIR": str(private / "config"),
            "ALYSIS_DATA_DIR": str(private / "data"),
            "ALYSIS_DESKTOP_CONTROL_PATHS": json.dumps([str(private)]),
            "ALYSIS_SHELL_SANDBOX_MODE": "strict",
            "ALYSIS_SHELL_SANDBOX_BACKEND": options.backend,
            "ALYSIS_SHELL_SANDBOX_NETWORK": "off",
            "ALYSIS_SHELL_SANDBOX_FILESYSTEM_ACCESS": "workspace-write",
            "ALYSIS_SHELL_SANDBOX_BWRAP_PROFILE": "hardened",
            "ALYSIS_SHELL_SANDBOX_FALLBACK": "refuse",
            "ALYSIS_SHELL_SANDBOX_ADDITIONAL_WORKSPACE_ROOTS": "[]",
            "ALYSIS_DESKTOP_RUNTIME_ROOTS": "[]",
        }
    )
    report = {
        "python": sys.version,
        "executable": sys.executable,
        "backend": options.backend,
        "checks": [],
        "failures": [],
        "not_run": [],
        "passed": False,
    }
    observations = Observations(report)
    try:
        from alysis_code import contained_read_tools
        from alysis_code.agent_loop import build_tools
        from alysis_code.config import AppConfig
        from alysis_code.sandbox_settings import resolve_shell_sandbox_settings
        from alysis_code.session_store import SessionStore
        from alysis_code.tools.fs import StaleFileError, capture_file_precondition

        module = Path(contained_read_tools.__file__)
        report["module"] = str(module)
        report["module_sha256"] = hashlib.sha256(module.read_bytes()).hexdigest()
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
        (root / "ordinary.txt").write_text("old\n", encoding="utf-8")
        (root / "read-sentinel.txt").write_text("READ_TOOL_FIXTURE\n", encoding="utf-8")
        (root / "test_sample.py").write_text("def test_marker_function():\n    assert True\n")
        subprocess.run(
            ["git", "-C", str(root), "add", "ordinary.txt", "test_sample.py"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "user.name=Sandbox Qualification",
                "-c",
                "user.email=qualification@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "-m",
                "Synthetic fixture",
            ],
            check=True,
            capture_output=True,
        )
        worker = contained_read_tools.ContainedReadTools(
            resolve_shell_sandbox_settings(AppConfig()), root
        )

        def tools_factory():
            store = SessionStore(
                enabled=False,
                sessions_dir=private / "sessions",
                session_id="qualification",
                cwd=str(root),
                repo_root=str(root),
            )
            return build_tools(
                root=root,
                console=None,
                mode="auto",
                yes=True,
                non_interactive=True,
                cfg=AppConfig(),
                store=store,
            )

        run_file_checks(
            observations,
            root=root,
            canary=canary,
            worker=worker,
            tools_factory=tools_factory,
            stamp=lambda path: asdict(capture_file_precondition(root=root, path=path)),
            stale_error=StaleFileError,
        )
    except Exception:
        report["failure"] = traceback.format_exc()
        print(report["failure"], file=sys.stderr)
    finally:
        exit_code = observations.finish()
        (output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
