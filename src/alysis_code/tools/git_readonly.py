"""Git inspection without chdir, child programs, index refreshes or scratch files.

The Windows MXC read-only filesystem rejects Git for Windows' directory opens.
Keep the same OS boundary and read Git objects/index directly instead. This module
is used only by the fixed contained reader; mutations still use the command runner.
Do not use porcelain.diff: it materializes the index tree in the object database.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from dulwich.annotate import annotate_lines
from dulwich.filters import FilterBlobNormalizer, FilterContext, FilterRegistry
from dulwich.index import ConflictedIndexEntry
from dulwich.object_store import MemoryObjectStore
from dulwich.objects import S_ISGITLINK, Blob, Commit
from dulwich.objectspec import parse_commit
from dulwich.patch import write_object_diff
from dulwich.porcelain import get_untracked_paths
from dulwich.repo import Repo

from .git import GitError, _clip_text, _resolve_git_path


def write_blob_diff(output, old, new):
    # Memory-only objects retain binary diff handling without writing blobs to
    # .git/objects or using a temporary directory in the user's project.
    store = MemoryObjectStore()
    for item in (old, new):
        if item[2] is not None:
            store.add_object(item[2])
    write_object_diff(
        output,
        store,
        (old[0], old[1], old[2].id if old[2] else None),
        (new[0], new[1], new[2].id if new[2] else None),
    )


class _InspectionFilters(FilterRegistry):
    def get_driver(self, name):
        # Even a driver called "text" can be configured by a repository. Never
        # construct an external-process or LFS driver in an inspection worker.
        configured = False
        for key in (b"clean", b"process", b"smudge"):
            try:
                configured |= bool(self.config.get((b"filter", name.encode()), key))
            except KeyError:
                pass
        if name != "text" or configured:
            raise GitError(
                "git_content_filter_required: This path needs a content filter; inspect it through an explicitly approved command."
            )
        return super().get_driver(name)


def _normalizer(repo):
    config = repo.get_config_stack()
    return FilterBlobNormalizer(
        config,
        repo.get_gitattributes(),
        filter_context=FilterContext(_InspectionFilters(config)),
    )


def _open(root):
    # Discovery remains inside the granted tree. External worktree/object paths
    # are still governed by the OS sandbox, never granted by this reader.
    try:
        return Repo.discover(str(root))
    except Exception as exc:
        from dulwich.errors import NotGitRepository

        if isinstance(exc, NotGitRepository):
            raise GitError("not a git repository") from exc
        raise


def _path(repo, root, path):
    if path is None:
        return None
    relative = _resolve_git_path(root, path)
    return (Path(root) / relative).relative_to(Path(repo.path)).as_posix().encode("utf-8")


def _matches(path, selected):
    return selected is None or path == selected or path.startswith(selected.rstrip(b"/") + b"/")


def _quoted(path):
    value = os.fsdecode(path)
    return json.dumps(value, ensure_ascii=False) if any(c in value for c in '\n\r\t"\\') else value


def _head(repo):
    refs, sha = repo.refs.follow(b"HEAD")
    if sha is None:
        return None
    value = repo[sha]
    if not isinstance(value, Commit):
        raise GitError("HEAD does not refer to a commit")
    return value


def _work_blob(repo, path, entry, normalizer, depth=0):
    full = Path(repo.path) / os.fsdecode(path)
    try:
        info = full.lstat()
    except FileNotFoundError:
        return None, None
    if S_ISGITLINK(entry.mode) and stat.S_ISDIR(info.st_mode):
        if depth >= 32:
            raise GitError("Submodule nesting exceeds the inspection limit")
        # An uninitialized submodule is not a deleted tracked file. Open only
        # this exact submodule, never discover its parent as its own repository.
        if not (full / ".git").exists():
            return entry.mode, SimpleNamespace(id=entry.sha, submodule="S...")
        with Repo(str(full)) as child:
            child_head = _head(child)
            sha = child_head.id if child_head else entry.sha
        status = git_status(root=full, depth=depth + 1)["status"].splitlines()[1:]
        dirty = any(not line.startswith("?? ") for line in status)
        untracked = any(line.startswith("?? ") for line in status)
        flags = (
            "S"
            + ("C" if sha != entry.sha else ".")
            + ("M" if dirty else ".")
            + ("U" if untracked else ".")
        )
        return entry.mode, SimpleNamespace(
            id=sha + (b"-dirty" if dirty or untracked else b""), submodule=flags
        )
    if stat.S_ISDIR(info.st_mode):
        return None, None
    if stat.S_ISLNK(info.st_mode):
        return 0o120000, Blob.from_string(os.fsencode(os.readlink(full)))
    if not stat.S_ISREG(info.st_mode):
        raise GitError(f"Cannot inspect special file: {os.fsdecode(path)}")
    data = full.read_bytes()
    # core.symlinks=false stores link targets as ordinary files on Windows.
    if stat.S_ISLNK(entry.mode) and not repo.get_config_stack().get_boolean(
        b"core", b"symlinks", True
    ):
        return entry.mode, Blob.from_string(data)
    mode = 0o100755 if info.st_mode & stat.S_IXUSR else 0o100644
    if stat.S_ISREG(entry.mode) and not repo.get_config_stack().get_boolean(
        b"core", b"filemode", True
    ):
        mode = entry.mode
    return mode, normalizer.checkin_normalize(Blob.from_string(data), path)


def git_status(
    *, root: Path, porcelain_v2=False, untracked_files="normal", branch_header=True, depth=0
):
    with _open(root) as repo:
        index = repo.open_index()
        head = _head(repo)
        refs, _ = repo.refs.follow(b"HEAD")
        branch = refs[-1].removeprefix(b"refs/heads/").decode("utf-8", "replace")
        heading = branch if branch != "HEAD" else "HEAD (no branch)"
        if head is None:
            heading = "No commits yet on " + heading
        states = {}
        conflicts = {p: e for p, e in index.iteritems() if isinstance(e, ConflictedIndexEntry)}
        # changes_from_tree cannot represent unmerged entries. Compare the
        # nonconflicted view in memory; never flush or rewrite the index.
        from dulwich.index import Index

        clean = Index(index.path, read=False)
        for p, e in index.iteritems():
            if p not in conflicts:
                clean[p] = e
        for (old, new), (oldmode, newmode), _ in clean.changes_from_tree(
            repo.object_store, head.tree if head else None
        ):
            path = new or old
            if path in conflicts:
                continue
            letter = (
                "A"
                if old is None
                else "D"
                if new is None
                else "T"
                if stat.S_IFMT(oldmode) != stat.S_IFMT(newmode)
                else "M"
            )
            states[path] = [letter, " "]
        normalizer = _normalizer(repo)
        for path, entry in clean.iteritems():
            mode, blob = _work_blob(repo, path, entry, normalizer, depth)
            if blob is None:
                states.setdefault(path, [" ", " "])[1] = "D"
            elif blob.id != entry.sha or mode != entry.mode:
                states.setdefault(path, [" ", " "])[1] = (
                    "T" if stat.S_IFMT(mode) != stat.S_IFMT(entry.mode) else "M"
                )
        for path, entry in conflicts.items():
            present = (entry.ancestor is not None, entry.this is not None, entry.other is not None)
            states[path] = {
                (True, True, True): "UU",
                (False, True, True): "AA",
                (True, True, False): "UD",
                (True, False, True): "DU",
                (True, False, False): "DD",
                (False, True, False): "AU",
                (False, False, True): "UA",
            }[present]
        untracked = (
            sorted(
                get_untracked_paths(
                    repo.path,
                    repo.path,
                    index,
                    exclude_ignored=True,
                    untracked_files=untracked_files,
                )
            )
            if untracked_files != "no"
            else []
        )
        untracked = [
            os.fsencode(path).replace(b"\\", b"/") if os.name == "nt" else os.fsencode(path)
            for path in untracked
        ]
        gitlinks = [path for path, entry in clean.iteritems() if S_ISGITLINK(entry.mode)]
        untracked = [
            path
            for path in untracked
            if not any(
                path.rstrip(b"/") == link or path.startswith(link + b"/") for link in gitlinks
            )
        ]
        if porcelain_v2:
            zero = "0" * 40
            lines = (
                [
                    "# branch.oid " + (head.id.decode() if head else "(initial)"),
                    "# branch.head " + (branch if branch != "HEAD" else "(detached)"),
                ]
                if branch_header
                else []
            )
            for path in sorted(states):
                xy = "".join(states[path]).replace(" ", ".")
                if path in conflicts:
                    entry = conflicts[path]
                    entries = [entry.ancestor, entry.this, entry.other]
                    current = entry.this or entry.other or entry.ancestor
                    mode, _ = _work_blob(repo, path, current, normalizer, depth)
                    modes = " ".join(f"{item.mode:06o}" if item else "000000" for item in entries)
                    hashes = " ".join(item.sha.decode() if item else zero for item in entries)
                    lines.append(f"u {xy} N... {modes} {mode or 0:06o} {hashes} {_quoted(path)}")
                    continue
                entry = clean[path] if path in clean else None
                try:
                    oldmode, oldsha = (
                        repo[head.tree].lookup_path(repo.object_store.__getitem__, path)
                        if head
                        else (0, zero.encode())
                    )
                except KeyError:
                    oldmode, oldsha = 0, zero.encode()
                mode, blob = (
                    _work_blob(repo, path, entry, normalizer, depth) if entry else (None, None)
                )
                sub = getattr(
                    blob, "submodule", "S..." if entry and S_ISGITLINK(entry.mode) else "N..."
                )
                lines.append(
                    f"1 {xy} {sub} {oldmode:06o} {entry.mode if entry else 0:06o} {mode or 0:06o} {oldsha.decode()} {entry.sha.decode() if entry else zero} {_quoted(path)}"
                )
            lines += ["? " + _quoted(path) for path in untracked]
            return {"status": "\n".join(lines) + ("\n" if lines else "")}
        lines = ["## " + heading] if branch_header else []
        lines += ["".join(states[p]) + " " + _quoted(p) for p in sorted(states)]
        lines += ["?? " + _quoted(p) for p in untracked]
        return {"status": "\n".join(lines) + "\n"}


def _diff(repo, *, staged=False, selected=None):
    output = io.BytesIO()
    index = repo.open_index()
    if staged:
        head = _head(repo)
        for (old, new), (oldmode, newmode), (oldsha, newsha) in index.changes_from_tree(
            repo.object_store, head.tree if head else None
        ):
            if _matches(new or old, selected):
                write_object_diff(
                    output, repo.object_store, (old, oldmode, oldsha), (new, newmode, newsha)
                )
    else:
        normalizer = _normalizer(repo)
        for path, entry in index.iteritems():
            if not _matches(path, selected):
                continue
            if isinstance(entry, ConflictedIndexEntry):
                output.write(b"* Unmerged path " + path + b"\n")
                entry = entry.this
                if entry is None:
                    continue
            mode, blob = _work_blob(repo, path, entry, normalizer)
            if blob is None or blob.id != entry.sha or mode != entry.mode:
                if S_ISGITLINK(entry.mode):
                    submodule_output = io.BytesIO()
                    submodule_store = MemoryObjectStore()
                    if isinstance(blob, Blob):
                        submodule_store.add_object(blob)
                    if blob and not S_ISGITLINK(mode):
                        write_object_diff(
                            submodule_output,
                            submodule_store,
                            (path, entry.mode, entry.sha),
                            (None, None, None),
                        )
                        write_object_diff(
                            submodule_output,
                            submodule_store,
                            (None, None, None),
                            (path, mode, blob.id),
                        )
                    else:
                        write_object_diff(
                            submodule_output,
                            submodule_store,
                            (path, entry.mode, entry.sha),
                            (path, mode, blob.id) if blob else (None, None, None),
                        )
                    patch = submodule_output.getvalue()
                    if blob and blob.id == entry.sha + b"-dirty":
                        patch = b"".join(
                            line
                            for line in patch.splitlines(keepends=True)
                            if not line.startswith(b"index ")
                        )
                    output.write(patch)
                    continue
                write_blob_diff(
                    output,
                    (path, entry.mode, repo[entry.sha]),
                    (path, mode, blob) if blob else (None, None, None),
                )
    return output.getvalue().decode("utf-8", "replace")


def git_diff(*, root: Path, path=None, staged=False, offset=0, diff_id=None):
    if type(staged) is not bool:
        raise GitError("staged must be a boolean")
    if type(offset) is not int or offset < 0:
        raise GitError("offset must be a non-negative integer")
    if offset and not diff_id:
        raise GitError("Pass the previous page's diff_id when continuing a diff.")
    with _open(root) as repo:
        value = _diff(repo, staged=staged, selected=_path(repo, root, path))
    current = hashlib.sha256(value.encode("utf-8")).hexdigest()
    if diff_id is not None and diff_id != current:
        raise GitError("The diff changed between pages. Restart with offset=0 and no diff_id.")
    end = min(offset + 20000, len(value))
    return {
        "view": "staged" if staged else "unstaged",
        "diff_id": current,
        "offset": offset,
        "next_offset": end if end < len(value) else None,
        "total_chars": len(value),
        "truncated": end < len(value),
        "diff": value[offset:end],
    }


def _revision(repo, name):
    if name.startswith("-") or "\0" in name:
        raise GitError("invalid revision")
    match = re.fullmatch(r"(.+?)([~^])(\d*)", name)
    try:
        if match:
            commit = _revision(repo, match[1])
            count = int(match[3] or "1")
            for _ in range(count if match[2] == "~" else min(count, 1)):
                commit = repo[commit.parents[0 if match[2] == "~" else count - 1]]
            return commit
        return parse_commit(repo, name)
    except (KeyError, ValueError, IndexError) as exc:
        raise GitError("invalid revision") from exc


def _metadata(commit, body_limit=240):
    encoding = (commit.encoding or b"utf-8").decode("ascii", "replace")
    try:
        message = commit.message.decode(encoding, "replace").strip()
        author = commit.author.decode(encoding, "replace")
    except LookupError:
        message = commit.message.decode("utf-8", "replace").strip()
        author = commit.author.decode("utf-8", "replace")
    subject, _, body = message.partition("\n\n")
    name, _, email = author.rpartition(" <")
    excerpt, truncated = _clip_text(body, max_chars=body_limit)
    return {
        "commit": commit.id.decode(),
        "short_commit": commit.id[:7].decode(),
        "author_name": name or author,
        "author_email": email.rstrip(">"),
        "authored_at": datetime.fromtimestamp(
            commit.author_time, timezone(timedelta(seconds=commit.author_timezone))
        ).isoformat(),
        "subject": " ".join(subject.splitlines()),
        "body_excerpt": excerpt,
        "body_truncated": truncated,
    }


def git_history(
    *,
    root: Path,
    mode,
    path=None,
    limit=10,
    ref=None,
    grep=None,
    author=None,
    commit=None,
    start_line=None,
    end_line=None,
):
    mode = str(mode or "").strip().lower()
    if mode not in {"log", "show", "blame"}:
        raise GitError(f"Unsupported git_history mode: {mode!r}")
    normalized = (
        _resolve_git_path(root, str(path)) if path is not None and str(path).strip() else None
    )
    with _open(root) as repo:
        selected = _path(repo, root, normalized)
        if mode == "log":
            limit = max(1, min(int(limit), 50))
            ref = str(ref or "HEAD").strip() or "HEAD"
            commits = []
            if not (ref == "HEAD" and _head(repo) is None):
                target = _revision(repo, ref)
                for entry in repo.get_walker(
                    include=[target.id], paths=[selected] if selected else None
                ):
                    item = _metadata(entry.commit)
                    if grep and not re.search(
                        str(grep), entry.commit.message.decode("utf-8", "replace")
                    ):
                        continue
                    if author and not re.search(
                        str(author), entry.commit.author.decode("utf-8", "replace")
                    ):
                        continue
                    commits.append(item)
                    if len(commits) > limit:
                        break
            return {
                "mode": mode,
                "path": normalized,
                "limit": limit,
                "ref": ref,
                "grep": str(grep) if grep else None,
                "author": str(author) if author else None,
                "commits": commits[:limit],
                "truncated": len(commits) > limit,
            }
        if mode == "show":
            commit, ref = str(commit or "").strip(), str(ref or "").strip()
            if commit and ref and commit != ref:
                raise GitError("show mode commit and ref must name the same revision")
            if not (commit or ref):
                raise GitError("show mode requires commit or ref")
            target = _revision(repo, commit or ref)
            output = io.BytesIO()
            parent = repo[target.parents[0]].tree if target.parents else None
            for (old, new), (oldmode, newmode), (oldsha, newsha) in repo.object_store.tree_changes(
                parent, target.tree
            ):
                if _matches(new or old, selected):
                    write_object_diff(
                        output, repo.object_store, (old, oldmode, oldsha), (new, newmode, newsha)
                    )
            excerpt, truncated = _clip_text(
                output.getvalue().decode("utf-8", "replace"), max_chars=6000
            )
            return {
                "mode": mode,
                "path": normalized,
                "commit": _metadata(target, 1200),
                "patch_excerpt": excerpt,
                "patch_truncated": truncated,
                "comparison": "first parent" if target.parents else "empty tree",
            }
        if normalized is None:
            raise GitError("blame mode requires path")
        if start_line is None or end_line is None:
            raise GitError("blame mode requires start_line and end_line")
        if start_line < 1 or end_line < start_line or end_line - start_line + 1 > 200:
            raise GitError("Invalid blame line range (maximum 200 lines)")
        head = _head(repo)
        if head is None:
            raise GitError("No commits yet")
        annotations = annotate_lines(repo.object_store, head.id, selected)
        # Match Git blame's default worktree view, including uncommitted lines.
        current = (Path(repo.path) / os.fsdecode(selected)).read_bytes().splitlines(keepends=True)
        values = [None] * len(current)
        matcher = difflib.SequenceMatcher(
            a=[line for _, line in annotations], b=current, autojunk=False
        )
        for block in matcher.get_matching_blocks():
            for i in range(block.size):
                values[block.b + i] = annotations[block.a + i][0][0]
        if start_line > len(current):
            raise GitError("Invalid blame line range")
        lines = []
        for number in range(start_line, min(end_line, len(current)) + 1):
            origin = values[number - 1]
            meta = (
                _metadata(origin)
                if origin
                else {
                    "commit": "0" * 40,
                    "author_name": "Not Committed Yet",
                    "author_email": "not.committed.yet",
                    "subject": "Not Committed Yet",
                }
            )
            content, truncated = _clip_text(
                current[number - 1].decode("utf-8", "replace"), max_chars=240
            )
            lines.append(
                {
                    "line_number": number,
                    "commit": meta["commit"],
                    "short_commit": meta["commit"][:12],
                    "author_name": meta["author_name"],
                    "author_email": meta["author_email"],
                    "summary": meta["subject"],
                    "content": content,
                    "content_truncated": truncated,
                }
            )
        return {
            "mode": mode,
            "path": normalized,
            "start_line": start_line,
            "end_line": lines[-1]["line_number"],
            "lines": lines,
            "truncated": False,
        }
