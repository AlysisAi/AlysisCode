"""Adapt literal Git inspection commands to the fixed read-only worker.

This is not a shell interpreter. Only a single literal Git invocation is parsed;
write commands, shell syntax and executable overrides never enter this adapter.
The result explicitly identifies the inspection backend instead of claiming a
Git executable was launched.
"""

from __future__ import annotations

import fnmatch
import io
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .shell_command_lexer import split_simple_commands
from .tools.git import GitError

VERBS = frozenset({"status", "diff", "log", "show", "blame", "rev-parse", "ls-files", "branch"})


def _show(reader, directory, revision, path, literal):
    from dulwich.patch import write_object_diff

    with reader._open(directory) as repo:
        target = reader._revision(repo, revision)
        parent = repo[target.parents[0]].tree if target.parents else None
        output = io.BytesIO()
        statistics, summaries, changed_names, name_status = [], [], [], []
        insertions = deletions = 0
        matched = path is None
        if path is not None:
            prefix = directory.relative_to(Path(repo.path)).as_posix()
            pattern = path if prefix == "." else prefix + "/" + path
            if pattern.startswith(":"):
                raise GitError("Unsupported pathspec magic; use --literal-pathspecs")
        for (old, new), (oldmode, newmode), (oldsha, newsha) in repo.object_store.tree_changes(
            parent, target.tree
        ):
            names = [name.decode("utf-8", "replace") for name in (old, new) if name is not None]
            if path is not None and not any(
                name == pattern
                or name.startswith(pattern.rstrip("/") + "/")
                or (not literal and fnmatch.fnmatchcase(name, pattern))
                for name in names
            ):
                continue
            matched = True
            start = output.tell()
            write_object_diff(
                output, repo.object_store, (old, oldmode, oldsha), (new, newmode, newsha)
            )
            patch_lines = output.getvalue()[start:].splitlines()
            added = sum(
                line.startswith(b"+") and not line.startswith(b"+++") for line in patch_lines
            )
            removed = sum(
                line.startswith(b"-") and not line.startswith(b"---") for line in patch_lines
            )
            insertions += added
            deletions += removed
            name = reader._quoted(new or old)
            changed_names.append(name + "\n")
            name_status.append(
                ("A" if old is None else "D" if new is None else "M") + "\t" + name + "\n"
            )
            binary = any(line.startswith(b"Binary files ") for line in patch_lines)
            statistics.append(
                f" {name} | "
                + (
                    "Bin"
                    if binary
                    else f"{added + removed} " + "+" * min(added, 30) + "-" * min(removed, 30)
                )
                + "\n"
            )
            if old is None or new is None:
                summaries.append(
                    f" {'create' if old is None else 'delete'} mode {(newmode or oldmode):06o} {name}\n"
                )
            elif oldmode != newmode:
                summaries.append(f" mode change {oldmode:06o} => {newmode:06o} {name}\n")
        if not matched:
            return None  # Like git show -- <unmatched-pathspec>: success, no output.
        excerpt, truncated = reader._clip_text(
            output.getvalue().decode("utf-8", "replace"), max_chars=6000
        )
        return {
            "commit": reader._metadata(target, 1200),
            "patch_excerpt": excerpt,
            "patch_truncated": truncated,
            "stat": "".join(statistics)
            + (
                f" {len(statistics)} file(s) changed, {insertions} insertion(s), {deletions} deletion(s)\n"
                if statistics
                else ""
            ),
            "summary": "".join(summaries),
            "name-only": "".join(changed_names),
            "name-status": "".join(name_status),
        }


def literal_git_argv(command: str) -> list[str] | None:
    if len(command) > 8000 or any(c in command for c in "\n\r"):
        return None
    # Preserve literal quoted argv before the conservative approval lexer
    # rejects opaque-looking text even inside quotes. Double-quoted expansion
    # is not literal; unquoted expressions remain rejected. No shell executes
    # the restored strings in the native Git path.
    selectors = {}
    unsafe = False

    def token(value):
        key = "ALYSIS_GIT_LITERAL_" + uuid.uuid4().hex
        while key in command or key in selectors:
            key = "ALYSIS_GIT_LITERAL_" + uuid.uuid4().hex
        selectors[key] = value
        return key

    def quoted(match):
        nonlocal unsafe
        quote, content = match[0][0], match[0][1:-1]
        if quote == '"' and any(c in content for c in "$`"):
            unsafe = True
        return quote + token(content.replace(quote * 2, quote)) + quote

    text = re.sub(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", quoted, command.strip())
    if unsafe or any(c in text for c in "$`#"):
        return None
    # Unquoted numeric reflog selectors are also unambiguous Git literals.
    text = re.sub(r"[A-Za-z0-9_./-]*@\{[0-9]+\}", lambda match: token(match[0]), text)
    parsed = split_simple_commands(text, dialect="powershell", strict=True)
    if not parsed or len(parsed) != 1 or parsed[0].separator_before or parsed[0].redirects:
        return None
    if command.rstrip().endswith((";", "&", "|", ">", "<")):
        return None
    words = list(parsed[0].words)
    for key, selector in selectors.items():
        words = [word.replace(key, selector) for word in words]
    if not words or words.pop(0).lower() not in {"git", "git.exe"}:
        return None
    prefix = []
    while words and words[0] in {"--no-pager", "--no-optional-locks", "--literal-pathspecs"}:
        prefix.append(words.pop(0))
    return prefix + words if words else None


def inspection_argv(command: str) -> list[str] | None:
    argv = literal_git_argv(command)
    if not argv:
        return None
    words = [*argv]
    while words and words[0].startswith("--"):
        words.pop(0)
    if not words or words[0] not in VERBS:
        return None
    if (
        words[0] == "branch"
        and len(words) > 1
        and words[1] not in {"--list", "--show-current", "-a", "--all", "-r", "--remotes"}
    ):
        return None
    return argv


def git_shell_inspect(*, root: Path, argv: list[str], cwd: str = "."):
    from .tools import git_readonly as reader

    try:
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(a, str) or "\0" in a for a in argv)
        ):
            raise GitError("Invalid Git inspection arguments")
        directory = (root / cwd).resolve()
        directory.relative_to(root.resolve())
        words = list(argv)
        literal = False
        while words and words[0] in {"--no-pager", "--no-optional-locks", "--literal-pathspecs"}:
            literal |= words.pop(0) == "--literal-pathspecs"
        verb = words.pop(0)
        if verb not in VERBS:
            raise GitError("Not a Git inspection command")
        paths = []
        if "--" in words:
            index = words.index("--")
            paths, words = words[index + 1 :], words[:index]
        if len(paths) > 1:
            raise GitError("Use the dedicated Git tool to inspect one literal path at a time")
        path = paths[0] if paths else None
        if path is not None:
            if path.startswith(":(literal)"):
                path, literal = path[len(":(literal)") :], True
            if verb != "show" and not literal and any(c in path for c in "*?[:"):
                raise GitError("Use --literal-pathspecs for an exact inspection path")
        output, truncated = "", False
        if verb == "branch":
            with reader._open(directory) as repo:
                refs, _ = repo.refs.follow(b"HEAD")
                current = refs[-1].decode("utf-8")
                if words == ["--show-current"]:
                    output = (
                        current.removeprefix("refs/heads/") + "\n"
                        if current.startswith("refs/heads/")
                        else ""
                    )
                else:
                    remote = any(w in {"-a", "--all", "-r", "--remotes"} for w in words)
                    local = not any(w in {"-r", "--remotes"} for w in words)
                    patterns = [
                        w for w in words if w not in {"--list", "-a", "--all", "-r", "--remotes"}
                    ]
                    if paths or any(p.startswith("-") for p in patterns):
                        raise GitError("Unsupported branch inspection option")
                    for ref in sorted(repo.refs.keys()):
                        name = ref.decode("utf-8")
                        if local and name.startswith("refs/heads/"):
                            short = name.removeprefix("refs/heads/")
                        elif remote and name.startswith("refs/remotes/"):
                            short = name.removeprefix("refs/")
                        else:
                            continue
                        if not patterns or any(fnmatch.fnmatchcase(short, p) for p in patterns):
                            output += ("* " if name == current else "  ") + short + "\n"
        elif verb == "status":
            allowed = {
                "--porcelain",
                "--porcelain=v1",
                "--porcelain=v2",
                "--porcelain=2",
                "--short",
                "-s",
                "--branch",
                "-b",
                "-sb",
                "-bs",
                "--untracked-files=all",
                "--untracked-files=normal",
                "--untracked-files=no",
                "-uno",
            }
            if paths or any(w not in allowed for w in words):
                raise GitError("Unsupported status option; use git_status")
            version2 = any(w in {"--porcelain=v2", "--porcelain=2"} for w in words)
            untracked = (
                "all"
                if "--untracked-files=all" in words
                else "no"
                if any(w in {"--untracked-files=no", "-uno"} for w in words)
                else "normal"
            )
            branch = not words or any(w in {"--branch", "-b", "-sb", "-bs"} for w in words)
            output = reader.git_status(
                root=directory,
                porcelain_v2=version2,
                untracked_files=untracked,
                branch_header=branch,
            )["status"]
            if version2:
                return {"stdout": output, "stderr": "", "exit_code": 0, "truncated": False}
        elif verb == "diff":
            allowed = {
                "--cached",
                "--staged",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--color=never",
                "--patch",
                "-p",
                "--name-only",
                "--name-status",
            }
            if any(w not in allowed for w in words):
                raise GitError("Unsupported diff option; use git_diff")
            result = reader.git_diff(
                root=directory, path=path, staged=any(w in {"--cached", "--staged"} for w in words)
            )
            output, truncated = result["diff"], result["truncated"]
            if any(w in {"--name-only", "--name-status"} for w in words):
                with reader._open(directory) as repo:
                    selected = reader._path(repo, directory, path)
                    index = repo.open_index()
                    head = reader._head(repo)
                    changes = []
                    if any(w in {"--cached", "--staged"} for w in words):
                        for (old, new), _, _ in index.changes_from_tree(
                            repo.object_store, head.tree if head else None
                        ):
                            changes.append(
                                (new or old, "D" if new is None else "A" if old is None else "M")
                            )
                    else:
                        normalizer = reader._normalizer(repo)
                        for name, entry in index.iteritems():
                            mode, blob = reader._work_blob(repo, name, entry, normalizer)
                            if blob is None or blob.id != entry.sha or mode != entry.mode:
                                changes.append((name, "D" if blob is None else "M"))
                    output = "".join(
                        (status + "\t" if "--name-status" in words else "")
                        + reader._quoted(name)
                        + "\n"
                        for name, status in sorted(changes)
                        if reader._matches(name, selected)
                    )
                    truncated = False
        elif verb in {"show", "log"}:
            revision, limit, format_value = "HEAD", 10, None
            no_patch = verb == "log"
            extra_views = []
            i = 0
            while i < len(words):
                value = words[i]
                if value in {
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-color",
                    "--color=never",
                    "--end-of-options",
                }:
                    pass
                elif value in {"--no-patch", "-s"}:
                    no_patch = True
                elif value in {"--patch", "-p"}:
                    no_patch = False
                elif value in {"--stat", "--summary", "--name-only", "--name-status"}:
                    extra_views.append(value[2:])
                    no_patch = True
                elif value == "--oneline":
                    format_value = "%h %s"
                elif value.startswith(("--format=", "--pretty=")):
                    format_value = value.split("=", 1)[1]
                elif value in {"-n", "--max-count"}:
                    i += 1
                    limit = int(words[i])
                elif re.fullmatch(r"-\d+|-n\d+|--max-count=\d+", value):
                    limit = int(re.search(r"\d+", value)[0])
                elif value.startswith("-"):
                    raise GitError("Unsupported history option; use git_history")
                elif revision != "HEAD":
                    raise GitError("Only one revision is supported by this inspection command")
                else:
                    revision = value
                i += 1
            result = (
                _show(reader, directory, revision, path, literal)
                if verb == "show"
                else reader.git_history(
                    root=directory, mode=verb, ref=revision, path=path, limit=limit
                )
            )
            if result is None:
                return {"stdout": "", "stderr": "", "exit_code": 0, "truncated": False}
            commits = result["commits"] if verb == "log" else [result["commit"]]
            for item in commits:
                if format_value not in {None, "fuller", "full", "medium", "short", ""}:
                    format_value = format_value.removeprefix("format:")
                    if re.search(r"%(?!an|ae|aI|[Hhsbn%])", format_value):
                        raise GitError("Unsupported history format; use git_history")
                    fields = {
                        "%H": item["commit"],
                        "%h": item["short_commit"],
                        "%s": item["subject"],
                        "%an": item["author_name"],
                        "%ae": item["author_email"],
                        "%aI": item["authored_at"],
                        "%b": item["body_excerpt"],
                        "%n": "\n",
                        "%%": "%",
                    }
                    rendered = re.sub(
                        r"%an|%ae|%aI|%[Hhsbn%]",
                        lambda m, fields=fields: fields[m[0]],
                        format_value,
                    )
                    output += rendered + "\n"
                elif format_value != "":
                    output += f"commit {item['commit']}\nAuthor: {item['author_name']} <{item['author_email']}>\nAuthorDate: {item['authored_at']}\n"
                    if format_value in {"fuller", "full"}:
                        with reader._open(directory) as repo:
                            raw = reader._revision(repo, item["commit"])
                            date = datetime.fromtimestamp(
                                raw.commit_time, timezone(timedelta(seconds=raw.commit_timezone))
                            ).isoformat()
                            output += f"Commit: {raw.committer.decode('utf-8', 'replace')}\nCommitDate: {date}\n"
                    output += f"\n    {item['subject']}\n"
                    if item["body_excerpt"]:
                        output += "\n    " + item["body_excerpt"].replace("\n", "\n    ") + "\n"
                truncated |= item["body_truncated"]
                if extra_views:
                    shown = (
                        result
                        if verb == "show"
                        else _show(reader, directory, item["commit"], path, literal)
                    )
                    if shown:
                        for view in extra_views:
                            output += shown[view]
                if not no_patch:
                    shown = (
                        result
                        if verb == "show"
                        else reader.git_history(
                            root=directory, mode="show", ref=item["commit"], path=path
                        )
                    )
                    output += "\n" + shown["patch_excerpt"] + "\n"
                    truncated |= shown["patch_truncated"]
            truncated |= result.get("truncated", False)
        elif verb == "rev-parse":
            with reader._open(directory) as repo:
                if words == ["--show-toplevel"]:
                    output = str(Path(repo.path).resolve()) + "\n"
                elif words == ["--abbrev-ref", "HEAD"]:
                    output = (
                        repo.refs.follow(b"HEAD")[0][-1].removeprefix(b"refs/heads/").decode()
                        + "\n"
                    )
                elif len(words) == 1 and not words[0].startswith("-"):
                    output = reader._revision(repo, words[0]).id.decode() + "\n"
                else:
                    raise GitError("Unsupported revision inspection; use git_history")
        elif verb == "ls-files":
            with reader._open(directory) as repo:
                index = repo.open_index()
                if set(words) <= {"--others", "--exclude-standard"} and "--others" in words:
                    from dulwich.porcelain import get_untracked_paths

                    names = list(
                        get_untracked_paths(
                            repo.path,
                            repo.path,
                            index,
                            exclude_ignored="--exclude-standard" in words,
                            untracked_files="all",
                        )
                    )
                elif not words or words == ["--cached"]:
                    names = [p.decode("utf-8", "replace") for p in index.paths()]
                else:
                    raise GitError("Unsupported file-list option; use git_status")
                output = "".join(
                    name + "\n"
                    for name in sorted(names)
                    if path is None or name == path or name.startswith(path + "/")
                )
        else:
            raise GitError("Use git_history mode=blame for bounded line inspection")
        return {"stdout": output, "stderr": "", "exit_code": 0, "truncated": truncated}
    except (GitError, ValueError, IndexError, KeyError) as exc:
        return {"stdout": "", "stderr": str(exc) + "\n", "exit_code": 2, "truncated": False}
