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
_GIT_QUOTED_DIFF_PATHS = re.compile(r'("(?:\\.|[^"\\])*")\s+("(?:\\.|[^"\\])*")')


def _unquote_path(value: str) -> str:
    raw = value.strip()
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
    raw = _unquote_path(value.strip())
    if raw.startswith("a/") or raw.startswith("b/"):
        return raw[2:]
    return raw


def parse_diff_git_line(line: str) -> tuple[str | None, str | None]:
    if not line.startswith("diff --git "):
        return None, None
    payload = line[len("diff --git ") :].strip()

    if payload.startswith('"'):
        # Preserve Git's backslashes until _unquote_path decodes C-style
        # sequences. shlex.split would discard octal-escape delimiters.
        match = _GIT_QUOTED_DIFF_PATHS.fullmatch(payload)
        if match is None:
            return None, None
        return _normalize_git_path(match.group(1)), _normalize_git_path(match.group(2))

    if payload.startswith("a/"):
        split = payload.rsplit(" b/", 1)
        if len(split) == 2:
            a_raw = split[0]
            b_raw = "b/" + split[1]
            return _normalize_git_path(a_raw), _normalize_git_path(b_raw)

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
    raw = line[len(prefix) :].split("\t", 1)[0].strip()
    if not raw:
        return None
    path = _normalize_git_path(raw)
    if not path or path == "/dev/null":
        return None
    return path


def iter_patch_paths(patch_text: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    saw_diff_header = False

    for line in patch_text.splitlines():
        a_path, b_path = parse_diff_git_line(line)
        if a_path is None and b_path is None:
            continue
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

    for line in patch_text.splitlines():
        a_path, b_path = parse_diff_git_line(line)
        if a_path is None and b_path is None:
            continue
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
