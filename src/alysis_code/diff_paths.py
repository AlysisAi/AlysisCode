from __future__ import annotations

import re
import shlex

_GIT_C_ESCAPE_BYTES = {
    "a": 7,
    "b": 8,
    "t": 9,
    "n": 10,
    "v": 11,
    "f": 12,
    "r": 13,
    '"': 34,
    "\\": 92,
}
_GIT_C_QUOTED_PATH = r'"(?:\\.|[^"\\])*"'
_GIT_LEFT_QUOTED_DIFF_PATHS = re.compile(rf"({_GIT_C_QUOTED_PATH}) (b/.*|{_GIT_C_QUOTED_PATH})")
_GIT_RIGHT_QUOTED_DIFF_PATHS = re.compile(rf"(a/.*) ({_GIT_C_QUOTED_PATH})")


def _unquote_path(value: str) -> str:
    raw = value
    if len(raw) < 2 or raw[0] != '"' or raw[-1] != '"':
        return raw

    # Git uses C-style byte escaping (not shell escaping) for paths containing
    # control characters or non-ASCII bytes when core.quotePath is enabled.
    # shlex drops the backslash before octal escapes, yielding a wrong filename.
    body = raw[1:-1]
    path_bytes = bytearray()
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            path_bytes.extend(char.encode("utf-8", "surrogateescape"))
            index += 1
            continue
        if index + 1 >= len(body):
            return raw
        escape = body[index + 1]
        if escape in _GIT_C_ESCAPE_BYTES:
            path_bytes.append(_GIT_C_ESCAPE_BYTES[escape])
            index += 2
        elif index + 4 <= len(body) and all(
            digit in "01234567" for digit in body[index + 1 : index + 4]
        ):
            path_bytes.append(int(body[index + 1 : index + 4], 8))
            index += 4
        else:
            # Malformed quoting should not be silently interpreted as another
            # path. Git itself will reject an invalid patch.
            return raw
    return path_bytes.decode("utf-8", "surrogateescape")


def _normalize_git_path(value: str) -> str:
    raw = _unquote_path(value)
    if raw.startswith("a/") or raw.startswith("b/"):
        return raw[2:]
    return raw


def _diff_separator_positions(payload: str) -> list[int]:
    """Possible ` b/` delimiters in an unquoted Git diff header."""
    return [match.start() for match in re.finditer(r" b/", payload)]


def parse_diff_git_line(line: str) -> tuple[str | None, str | None]:
    if not line.startswith("diff --git "):
        return None, None
    payload = line[len("diff --git ") :]

    if payload.startswith('"'):
        match = _GIT_LEFT_QUOTED_DIFF_PATHS.fullmatch(payload)
        if match is None:
            return None, None
        return _normalize_git_path(match.group(1)), _normalize_git_path(match.group(2))

    if payload.startswith("a/"):
        # A rename can quote the destination without quoting the source.
        if ' "b/' in payload:
            match = _GIT_RIGHT_QUOTED_DIFF_PATHS.fullmatch(payload)
            if match is None:
                return None, None
            return _normalize_git_path(match.group(1)), _normalize_git_path(match.group(2))

        candidates = [
            (_normalize_git_path(payload[:index]), _normalize_git_path(payload[index + 1 :]))
            for index in _diff_separator_positions(payload)
        ]
        # Spaces are not C-quoted by Git. A filename containing a directory
        # component named "b" therefore introduces extra possible separators.
        # Identical old/new paths can be resolved from the line itself.
        for old, new in candidates:
            if old == new:
                return old, new
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            # A rename or copy needs the adjacent rename/copy metadata or
            # ---/+++ headers. Returning the last candidate mis-scopes paths.
            return None, None

    try:
        parts = shlex.split(payload)
    except ValueError:
        return None, None
    if len(parts) < 2:
        return None, None
    return _normalize_git_path(parts[0]), _normalize_git_path(parts[1])


def _parse_patch_header_path(line: str, prefix: str) -> str | None:
    if not line.startswith(prefix):
        return None
    raw = line[len(prefix) :].split("\t", 1)[0]
    if not raw:
        return None
    path = _normalize_git_path(raw)
    if not path or path == "/dev/null":
        return None
    return path


def _resolved_diff_paths(
    header: str, metadata: dict[str, str | None]
) -> tuple[str | None, str | None]:
    old, new = parse_diff_git_line(header)
    payload = header[len("diff --git ") :]
    if payload.startswith('"') or ' "b/' in payload:
        if not old or not new:
            raise ValueError("Malformed quoted Git diff header")
        return old, new
    if not (payload.startswith("a/") and len(_diff_separator_positions(payload)) > 1):
        if not old or not new:
            raise ValueError("Malformed Git diff header")
        return old, new

    candidates = {
        (_normalize_git_path(payload[:index]), _normalize_git_path(payload[index + 1 :]))
        for index in _diff_separator_positions(payload)
    }

    def verified(source: str, destination: str) -> tuple[str, str]:
        if (source, destination) not in candidates:
            raise ValueError("Git diff path metadata does not match its file header")
        return source, destination

    # Prefer explicit rename/copy metadata: an apparent same-path candidate
    # can actually represent a rename to a pathname containing " b/".
    for kind in ("rename", "copy"):
        source = metadata.get(f"{kind} from")
        destination = metadata.get(f"{kind} to")
        if source is not None and destination is not None:
            return verified(source, destination)

    source = metadata.get("---")
    destination = metadata.get("+++")
    if source is not None and destination is not None:
        return verified(source, destination)

    if old is not None and new is not None and old == new:
        return old, new

    # Never report a guessed filename to scope guards. Git's header alone is
    # ambiguous for distinct names containing ` b/`; proper patches provide
    # rename/copy records or old/new file headers to disambiguate.
    raise ValueError("Ambiguous unquoted Git diff paths: missing file or rename metadata")


def _iter_diff_header_paths(patch_text: str):
    header: str | None = None
    metadata: dict[str, str | None] = {}
    in_header = False
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            if header is not None:
                yield _resolved_diff_paths(header, metadata)
            header = line
            metadata = {}
            in_header = True
        elif header is not None and in_header:
            if line.startswith(("@@", "GIT binary patch", "Binary files ")):
                in_header = False
            elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
                # Preserve a literal `a/` prefix in repo-relative rename paths.
                key = line.split(" ", 2)
                metadata[f"{key[0]} {key[1]}"] = _unquote_path(key[2])
            elif line.startswith(("--- ", "+++ ")):
                metadata[line[:3]] = _parse_patch_header_path(line, line[:4])
    if header is not None:
        yield _resolved_diff_paths(header, metadata)


def iter_patch_paths(patch_text: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    saw_diff_header = False

    for a_path, b_path in _iter_diff_header_paths(patch_text):
        saw_diff_header = True
        for path in (a_path, b_path):
            if not path or path == "/dev/null" or path in seen:
                continue
            seen.add(path)
            paths.append(path)

    if saw_diff_header:
        return paths

    for line in patch_text.splitlines():
        for prefix in ("--- ", "+++ "):
            path = _parse_patch_header_path(line, prefix)
            if path is None or path in seen:
                continue
            seen.add(path)
            paths.append(path)
    return paths


def parse_patch_changed_files(patch_text: str) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    saw_diff_header = False

    for _a_path, b_path in _iter_diff_header_paths(patch_text):
        saw_diff_header = True
        if b_path and b_path != "/dev/null" and b_path not in seen:
            seen.add(b_path)
            files.append(b_path)

    if saw_diff_header:
        return files

    for line in patch_text.splitlines():
        path = _parse_patch_header_path(line, "+++ ")
        if path is None or path in seen:
            continue
        seen.add(path)
        files.append(path)
    return files
