"""Fixed Git metadata operations with a dedicated Windows containment grant.

Only this trusted worker receives writes to the selected repository's .git.
The working tree stays read-only; no shell, hooks, filters or network are run.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

from .git_shell_inspection import literal_git_argv
from .tools.git import GitError


def mutation_argv(command: str) -> list[str] | None:
    argv = literal_git_argv(command)
    if argv and argv[0] in {"switch", "checkout", "commit", "add"}:
        return argv
    if argv and argv[0] == "branch" and len(argv) == 2 and not argv[1].startswith("-"):
        return argv
    return None


def operation(argv: list[str]) -> dict:
    if not argv or any(not isinstance(a, str) or "\0" in a for a in argv):
        raise GitError("Invalid Git operation")
    verb, *args = argv
    if verb in {"switch", "checkout", "branch"}:
        switch = verb != "branch"
        create_flag = "-c" if verb == "switch" else "-b"
        if switch and len(args) == 2 and args[0] == create_flag:
            args = args[1:]
        elif switch:
            raise GitError("Use git switch -c <new-branch> for a branch at the current commit")
        if len(args) != 1 or args[0].startswith("-") or args[0] == "HEAD":
            raise GitError("A single new branch name is required")
        from dulwich.refs import check_ref_format

        if not check_ref_format(b"refs/heads/" + args[0].encode("utf-8")):
            raise GitError("Invalid branch name")
        return {"kind": "branch", "name": args[0], "switch": switch}
    if verb == "commit":
        messages, allow_empty, paths, only = [], False, [], False
        while args:
            value = args.pop(0)
            if value in {"-m", "--message"} and args:
                messages.append(args.pop(0))
            elif value.startswith("--message="):
                messages.append(value.partition("=")[2])
            elif value == "--allow-empty":
                allow_empty = True
            elif value in {"--only", "-o"}:
                only = True
            elif value == "--":
                paths.extend(args)
                args.clear()
            elif not value.startswith("-"):
                paths.append(value)
            elif value in {"--no-verify", "--no-gpg-sign"}:
                pass
            else:
                raise GitError(
                    "Commit staged changes using git commit -m <message>; unsupported option: "
                    + value
                )
        if not messages or not any(m.strip() for m in messages):
            raise GitError("A nonempty commit message is required")
        if only and not paths:
            raise GitError("Commit --only requires explicit file paths")
        if any(any(c in p for c in "*?[") or p.startswith("-") for p in paths):
            raise GitError("Commit paths must be explicit literal filenames")
        return {
            "kind": "commit",
            "message": "\n\n".join(messages).strip() + "\n",
            "allow_empty": allow_empty,
            "paths": paths,
        }
    if verb == "add":
        if args and args[0] == "--":
            args.pop(0)
        if not args or any(p.startswith("-") or any(c in p for c in "*?[") for p in args):
            raise GitError("Stage explicit literal file paths with git add -- <files>")
        return {"kind": "add", "paths": args}
    raise GitError("Unsupported Git metadata operation")


def execute(root: Path, plan: dict) -> dict:
    from contextlib import ExitStack

    from dulwich.file import GitFile

    # Match Git's lock convention. Another process must not switch HEAD or
    # change the staged snapshot halfway through an approved operation.
    with ExitStack() as cleanup:
        head_lock = GitFile(str(root / ".git/HEAD"), "wb")
        cleanup.callback(head_lock.abort)
        index_lock = None
        if plan["kind"] in {"add", "commit"}:
            index_lock = GitFile(str(root / ".git/index"), "wb")
            cleanup.callback(index_lock.abort)
        return _execute_locked(root, plan, head_lock, index_lock)


def _execute_locked(root: Path, plan: dict, head_lock, index_lock) -> dict:
    from dulwich.index import ConflictedIndexEntry, index_entry_from_stat
    from dulwich.objects import Blob, Commit
    from dulwich.repo import Repo

    from .tools.git_readonly import _head, _normalizer

    # The launcher selected this exact repository, not a discoverable parent.
    with Repo(str(root)) as repo:
        if Path(repo.controldir()).resolve() != root / ".git" or repo.bare:
            raise GitError("Git writes require an ordinary repository inside the selected project")
        repo.hooks.clear()
        config = repo.get_config_stack()
        head = _head(repo)
        head_ref = repo.refs.follow(b"HEAD")[0][-1]
        old = head.id if head else None
        if any(
            (root / ".git" / name).exists()
            for name in (
                "MERGE_HEAD",
                "CHERRY_PICK_HEAD",
                "REVERT_HEAD",
                "rebase-merge",
                "rebase-apply",
            )
        ):
            raise GitError("Finish the existing merge, rebase or cherry-pick before this operation")
        if plan["kind"] == "branch":
            ref = b"refs/heads/" + plan["name"].encode("utf-8")
            if ref in repo.refs:
                raise GitError("Branch already exists: " + plan["name"])
            if old is None:
                raise GitError("Create the first commit before creating another branch")
            if not repo.refs.add_if_new(ref, old):
                raise GitError("Branch already exists: " + plan["name"])
            if plan["switch"]:
                try:
                    head_lock.write(b"ref: " + ref + b"\n")
                    head_lock.close()
                except BaseException:
                    repo.refs.remove_if_equals(ref, old)
                    raise
            return {
                "stdout": ("Switched to a new branch '" if plan["switch"] else "Created branch '")
                + plan["name"]
                + "'\n"
            }
        index = repo.open_index()
        if any(isinstance(entry, ConflictedIndexEntry) for _, entry in index.iteritems()):
            raise GitError("Resolve index conflicts before staging or committing")
        if plan["kind"] == "add":
            from dulwich.ignore import IgnoreFilterManager

            ignores = IgnoreFilterManager.from_repo(repo)
            normalizer = _normalizer(repo)
            prepared = []
            for name in plan["paths"]:
                target = (root / name).resolve()
                target.relative_to(root)
                relative = target.relative_to(root).as_posix().encode("utf-8")
                if relative.startswith(b".git/") or relative == b".git":
                    raise GitError("Cannot stage repository metadata")
                if relative not in index and ignores.is_ignored(relative.decode("utf-8")):
                    raise GitError("Path is ignored; it was not staged: " + name)
                if not target.exists():
                    if relative not in index:
                        raise GitError("Path does not exist: " + name)
                    prepared.append((relative, None, None))
                    continue
                if not target.is_file() or target.is_symlink():
                    raise GitError("Stage ordinary files explicitly: " + name)
                blob = normalizer.checkin_normalize(Blob.from_string(target.read_bytes()), relative)
                prepared.append((relative, blob, target.stat()))
            for relative, blob, info in prepared:
                if blob is None:
                    del index[relative]
                else:
                    repo.object_store.add_object(blob)
                    entry = index_entry_from_stat(info, blob.id)
                    if relative in index and not config.get_boolean(b"core", b"filemode", True):
                        entry.mode = index[relative].mode
                    index[relative] = entry
            from dulwich.index import SHA1Writer, write_index_dict

            writer = SHA1Writer(index_lock)
            # Optional index caches describe the old tree; regenerate later.
            write_index_dict(writer, dict(index.iteritems()), version=index._version or 2)
            writer.close()
            return {"stdout": ""}
        if config.get_boolean(b"commit", b"gpgsign", False):
            raise GitError(
                "This repository requires signed commits; signing is not supported by the contained Git worker"
            )
        try:
            author_name, email = config.get(b"user", b"name"), config.get(b"user", b"email")
        except KeyError as exc:
            raise GitError(
                "Git author identity is missing; configure user.name and user.email before committing"
            ) from exc
        if (
            not author_name.strip()
            or not email.strip()
            or any(c in author_name + email for c in (b"\n", b"\r", b"<", b">"))
        ):
            raise GitError("Invalid configured Git author identity")
        if plan.get("paths"):
            from dulwich.index import commit_tree
            from dulwich.object_store import iter_tree_contents

            from .tools.git_readonly import _work_blob

            entries = (
                {
                    item.path: (item.sha, item.mode)
                    for item in iter_tree_contents(repo.object_store, head.tree)
                }
                if head
                else {}
            )
            normalizer = _normalizer(repo)
            for name in plan["paths"]:
                target = (root / name).resolve()
                relative = target.relative_to(root).as_posix().encode("utf-8")
                if relative == b".git" or relative.startswith(b".git/"):
                    raise GitError("Cannot commit repository metadata")
                if relative in index:
                    mode, blob = _work_blob(repo, relative, index[relative], normalizer)
                    if (
                        blob is None
                        or blob.id != index[relative].sha
                        or mode != index[relative].mode
                    ):
                        raise GitError(
                            "Commit --only would include unstaged content in "
                            + name
                            + "; inspect it and use a staged commit instead"
                        )
                    entries[relative] = (index[relative].sha, index[relative].mode)
                elif relative in entries and not target.exists():
                    del entries[relative]
                else:
                    raise GitError("Commit path is not staged: " + name)
            tree = commit_tree(
                repo.object_store, ((name, sha, mode) for name, (sha, mode) in entries.items())
            )
        else:
            tree = index.commit(repo.object_store)
        if head and head.tree == tree and not plan["allow_empty"]:
            raise GitError("Nothing staged to commit")
        if not head and len(index) == 0 and not plan["allow_empty"]:
            raise GitError("Nothing staged to commit")
        commit = Commit()
        commit.tree = tree
        commit.parents = [old] if old else []
        commit.author = commit.committer = author_name + b" <" + email + b">"
        commit.author_time = commit.commit_time = int(time.time())
        commit.author_timezone = commit.commit_timezone = -(
            time.altzone if time.localtime().tm_isdst else time.timezone
        )
        commit.message = plan["message"].encode("utf-8")
        repo.object_store.add_object(commit)
        message = b"commit: " + commit.message.splitlines()[0]
        updated = head_ref == b"HEAD" or (
            repo.refs.set_if_equals(
                head_ref,
                old,
                commit.id,
                committer=commit.committer,
                timestamp=commit.commit_time,
                timezone=commit.commit_timezone,
                message=message,
            )
            if old
            else repo.refs.add_if_new(
                head_ref,
                commit.id,
                committer=commit.committer,
                timestamp=commit.commit_time,
                timezone=commit.commit_timezone,
                message=message,
            )
        )
        if not updated:
            raise GitError("HEAD changed concurrently; the new commit was not installed")
        if head_ref == b"HEAD":
            head_lock.write(commit.id + b"\n")
            head_lock.close()
        repo.refs._log(
            b"HEAD",
            old,
            commit.id,
            committer=commit.committer,
            timestamp=commit.commit_time,
            timezone=commit.commit_timezone,
            message=message,
        )
        return {
            "stdout": "[" + commit.id.decode()[:12] + "] " + plan["message"].splitlines()[0] + "\n",
            "commit": commit.id.decode(),
            "hooks_executed": False,
        }


def run(*, settings, root: Path, cwd: Path, argv: list[str], timeout_s, cancel_check):
    from .config import ConfigError
    from .contained_read_tools import ContainedReadTools
    from .windows_mxc import WindowsMxcRunner

    if settings.filesystem_access != "workspace-write" or not settings.safe_git_writes:
        raise ConfigError("Git metadata writes are disabled by this session's policy")
    plan = operation(argv)
    repository = cwd
    while not (repository / ".git").exists() and repository != root:
        repository = repository.parent
    if not (repository / ".git").is_dir():
        raise ConfigError("Git writes require a repository with a local .git directory")
    repository.relative_to(root)
    if plan.get("paths"):
        plan["paths"] = [str((cwd / p).resolve().relative_to(repository)) for p in plan["paths"]]
    readonly = ContainedReadTools(settings, root).settings
    encoded = base64.b64encode(json.dumps(plan, ensure_ascii=True).encode()).decode()
    if len(encoded) > 16000:
        raise ConfigError("Git operation is too large")

    class MetadataRunner(WindowsMxcRunner):
        def _prepare(self, **kwargs):
            launcher, payload = super()._prepare(**kwargs)
            request = json.loads(payload)
            metadata = repository / ".git"
            # No writable working-tree grant: absent instruction/metadata names
            # are protected by the read-only ceiling and require no reservations.
            request["write"] = [str(metadata)]
            request["deny_write"] = [
                p
                for p in request["deny_write"]
                if Path(p).exists() and not metadata.is_relative_to(Path(p))
            ]
            request["deny_write"] += [
                str(metadata / p) for p in ("config", "hooks") if (metadata / p).exists()
            ]
            request["argv"] = [
                sys.executable,
                "-I",
                "-B",
                "-m",
                "alysis_code.git_metadata",
                encoded,
            ]
            request["network"] = "off"
            return launcher, json.dumps(request).encode()

    completed = MetadataRunner(replace(readonly, additional_workspace_roots=())).run(
        root=root, cwd=repository, cmd="", timeout_s=timeout_s, cancel_check=cancel_check
    )
    try:
        result = json.loads(completed.stdout)
    except ValueError as exc:
        raise ConfigError(
            "Contained Git worker could not start: " + completed.stderr[:1000]
        ) from exc
    completed.stdout = result.get("stdout", "")
    completed.stderr = result.get("error", "")
    completed.inspection_backend = "contained-git-metadata"
    return completed


def main():
    try:
        if len(sys.argv) != 2 or len(sys.argv[1]) > 16000:
            raise GitError("Invalid fixed Git operation request")
        result = execute(
            Path.cwd().resolve(), json.loads(base64.b64decode(sys.argv[1], validate=True))
        )
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)[:2000]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
