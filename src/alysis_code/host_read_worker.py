"""Fixed file-tool dispatcher, launched only inside the OS sandbox by the host.

No configuration, credentials, model calls or arbitrary module/function dispatch.
The OS policy, rather than a directory snapshot, governs concurrent path changes.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import stat
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from .project_folders import allowed_roots, containing_root, folder_scope, validate_worker_folders

READ_TOOLS = frozenset(
    {
        "fs_read",
        "fs_list",
        "search_rg",
        "symbol_search",
        "repo_map",
        "test_discover",
        "git_diff",
        "git_history",
        "git_status",
        "git_shell_inspect",
        "mutation_snapshot",
    }
)


def mutation_snapshot(*, root: Path) -> dict[str, Any]:
    """Observe metadata without executing Git or deliberately traversing links.

    The worker's native policy still governs concurrent directory replacements.
    """
    from .runtime_artifacts import is_runtime_artifact_path
    from .sandbox_bookkeeping import command_file_signature

    signatures = {}
    pending = [root]
    visited = 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                visited += 1
                if visited > 20_000:
                    raise ValueError("Mutation snapshot exceeded its entry limit")
                relative = Path(entry.path).relative_to(root).as_posix()
                if is_runtime_artifact_path(relative, root=root):
                    continue
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(Path(entry.path))
                else:
                    signatures[relative] = command_file_signature(metadata)
    return {"signatures": signatures}


MAX_REQUEST_BYTES = 12_000
MAX_RESULT_BYTES = 2 * 1024 * 1024
WRITE_TOOLS = frozenset(
    {
        "git_apply_patch",
        "fs_write_prepared",
        "fs_edit_prepared",
        "fs_move",
        "fs_copy",
        "fs_delete",
        "fs_mkdir",
        "image_create_bytes",
    }
)


def execute_write(name: str, arguments: dict[str, Any], root: Path) -> dict[str, Any]:
    from .tools.fs import (
        FilePrecondition,
        PreparedFsEdit,
        PreparedFsWrite,
        assert_file_precondition,
        fs_copy,
        fs_delete,
        fs_mkdir,
        fs_move,
        write_prepared_fs_edit,
        write_prepared_fs_write,
    )
    from .tools.git import git_apply_patch

    if name not in WRITE_TOOLS or not isinstance(arguments, dict):
        raise ValueError("Invalid contained mutation request")
    args = dict(arguments)
    if any(key in args for key in ("root", "root_obj", "path_obj")):
        raise ValueError("Mutation root belongs to the launcher")
    if name == "image_create_bytes":
        from .tools.image_generation import _atomic_create_bytes

        target = (root / args["path"]).resolve()
        containing_root(root, target)
        _atomic_create_bytes(target, base64.b64decode(args["payload"], validate=True))
        return {"created": True}
    for key in ("precondition", "source_precondition", "destination_precondition"):
        if key in args:
            args[key] = FilePrecondition(**args[key])
    if name in {"fs_write_prepared", "fs_edit_prepared"}:
        path = str(args["path"])
        target = (root / path).resolve()
        containing_root(root, target)
        if name == "fs_write_prepared":
            prepared = PreparedFsWrite(root_obj=root, path_obj=target, **args)
            return write_prepared_fs_write(prepared, root=root)
        prepared = PreparedFsEdit(root_obj=root, path_obj=target, **args)
        return write_prepared_fs_edit(prepared, root=root)
    if name == "git_apply_patch":
        for item in args.pop("preconditions", []):
            assert_file_precondition(root=root, precondition=FilePrecondition(**item))
        return git_apply_patch(root=root, isolated_object_store=True, **args)
    function = {
        "fs_move": fs_move,
        "fs_copy": fs_copy,
        "fs_delete": fs_delete,
        "fs_mkdir": fs_mkdir,
    }[name]
    return function(root=root, **args)


def execute(name: str, arguments: dict[str, Any], root: Path) -> dict[str, Any]:
    from .tools.fs import fs_list, fs_read
    from .tools.git import git_diff, git_history, git_status
    from .tools.repo_map import repo_map
    from .tools.search import search_rg
    from .tools.symbols import symbol_search
    from .tools.test_discovery import test_discover

    if os.name == "nt":
        # This worker already has read-only OS grants. Avoid Git for Windows'
        # incompatible directory opens, without a failed call or write fallback.
        from .tools.git_readonly import git_diff, git_history, git_status

    from .git_shell_inspection import git_shell_inspect

    readers = {
        "fs_read": fs_read,
        "fs_list": fs_list,
        "search_rg": search_rg,
        "symbol_search": symbol_search,
        "repo_map": repo_map,
        "test_discover": test_discover,
        "git_diff": git_diff,
        "git_history": git_history,
        "git_status": git_status,
        "git_shell_inspect": git_shell_inspect,
        "mutation_snapshot": mutation_snapshot,
    }
    if name not in readers or not isinstance(arguments, dict) or "root" in arguments:
        raise ValueError("Invalid contained read request")
    function = readers[name]
    # path_base is already resolved by main's tool wrapper. Never forward unknown
    # keywords or a caller-supplied host root into a reader.
    accepted = set(inspect.signature(function).parameters) - {"root"}
    supplied = {key: value for key, value in arguments.items() if key != "path_base"}
    if set(supplied) - accepted:
        raise ValueError("Unsupported contained read argument")
    return function(root=root, **supplied)


def inspect_with_opaque_files(request: dict[str, Any], root: Path) -> dict[str, Any]:
    """Apply extra denials inside the fixed worker, including on opened handles."""
    from .host_file_handles import host_file_action, windows_workspace_paths
    from .host_file_policy import HostFilePolicy
    from .macos_access import FileIdentity

    values = request["opaque_files"]
    if request["tool"] != "fs_read" or not isinstance(values, list) or not 0 < len(values) <= 16:
        raise ValueError("Invalid opaque file inspection")
    identities = tuple(FileIdentity(**value) for value in values)
    for identity in identities:
        identity.check()
    policy = HostFilePolicy.capture()
    policy = replace(
        policy, private_roots=(*policy.private_roots, *(Path(i.path) for i in identities))
    )
    with windows_workspace_paths(root, additional=allowed_roots(root)[1:]):
        with host_file_action(policy, readonly=True):
            result = execute(request["tool"], request["arguments"], root)
    # A renamed/replaced authentication input must not return bytes under an
    # unrelated spelling. Check before the result can leave the contained worker.
    for identity in identities:
        identity.check()
    return result


def main() -> int:
    try:
        if len(sys.argv) == 4 and sys.argv[1] == "--request-file":
            # The expected digest travels in the launch command, not in the
            # workspace packet. A racing writer cannot change the approved input.
            from .host_file_handles import open_windows_packet_read

            packet = Path(sys.argv[2])
            opener = open_windows_packet_read(packet) if os.name == "nt" else packet.open("rb")
            with opener as stream:
                raw = stream.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != sys.argv[3]:
                raise ValueError("Contained request packet changed")
        elif len(sys.argv) != 2 or len(sys.argv[1]) > 4 * MAX_REQUEST_BYTES // 3 + 4:
            raise ValueError("Contained read request is too large")
        else:
            raw = base64.b64decode(sys.argv[1], validate=True)
            if len(raw) > MAX_REQUEST_BYTES:
                raise ValueError("Contained read request is too large")
        request = json.loads(raw)
        if (
            not isinstance(request, dict)
            or not {"tool", "arguments"}.issubset(request)
            or set(request) - {"tool", "arguments", "project_folders", "opaque_files"}
        ):
            raise ValueError("Invalid contained read envelope")
        root = Path.cwd()
        # The launcher supplies these roots independently of model arguments;
        # the OS sandbox grants the same folders for this invocation.
        with folder_scope(root, validate_worker_folders(request.get("project_folders"))):
            if "opaque_files" in request:
                result = inspect_with_opaque_files(request, root)
            elif request["tool"] in WRITE_TOOLS:
                from .host_file_handles import windows_workspace_paths

                with windows_workspace_paths(root, additional=allowed_roots(root)[1:]):
                    result = execute_write(request["tool"], request["arguments"], root)
            else:
                result = execute(request["tool"], request["arguments"], root)
        encoded = json.dumps({"ok": True, "result": result}, ensure_ascii=True)
        if len(encoded) > MAX_RESULT_BYTES:
            raise ValueError("Contained read result is too large; narrow the request")
        print(encoded)
        return 0
    except Exception as exc:
        from .tools.errors import GitDirectoryAccessError
        from .tools.fs import StaleFileError

        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc)[:2000],
                    "error_code": "git_directory_access_denied"
                    if isinstance(exc, GitDirectoryAccessError)
                    else None,
                    "stale_path": exc.path if isinstance(exc, StaleFileError) else None,
                },
                ensure_ascii=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
