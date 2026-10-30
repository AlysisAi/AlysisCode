from __future__ import annotations

import fnmatch
import json
import os
import queue
import re
import subprocess
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..command_text import command_text
from ..project_folders import containing_root
from .errors import SearchError

_RG_NO_CONFIG_SUPPORTED: bool | None = None
_SEARCH_RG_MAX_MATCHES_PER_FILE = 8
_SEARCH_RG_MAX_MATCH_TEXT_CHARS = 240
_SEARCH_RG_MAX_OUTPUT_CHARS = 12_000
_SEARCH_RG_MAX_CONTEXT_LINES = 5
_SEARCH_RG_MAX_RESULTS = 500
_RG_PROBE_TIMEOUT_S = 2.0
_SEARCH_RG_TIMEOUT_S = 15.0


def _rg_available() -> bool:
    try:
        cp = subprocess.run(
            ["rg", "--version"],
            check=False,
            capture_output=True,
            **command_text(),
            timeout=_RG_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return cp.returncode == 0


def _rg_supports_no_config() -> bool:
    global _RG_NO_CONFIG_SUPPORTED
    if _RG_NO_CONFIG_SUPPORTED is not None:
        return _RG_NO_CONFIG_SUPPORTED
    try:
        cp = subprocess.run(
            ["rg", "--no-config", "--version"],
            check=False,
            capture_output=True,
            **command_text(),
            timeout=_RG_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        _RG_NO_CONFIG_SUPPORTED = False
        return False
    _RG_NO_CONFIG_SUPPORTED = cp.returncode == 0
    return _RG_NO_CONFIG_SUPPORTED


def _clip_match_text(
    text: str, *, max_chars: int = _SEARCH_RG_MAX_MATCH_TEXT_CHARS
) -> tuple[str, bool]:
    normalized = text.rstrip("\n")
    if len(normalized) <= max_chars:
        return normalized, False
    return normalized[:max_chars].rstrip() + "...(truncated)", True


def _rel_path(root: Path, path: Path) -> str:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return os.fspath(path).replace("\\", "/")
    return os.fspath(rel).replace("\\", "/")


def _clean_globs(globs: list[str] | None) -> list[str] | None:
    cleaned = [str(pattern) for pattern in (globs or []) if str(pattern).strip()]
    return cleaned or None


def _matches_globs(rel_path: str, globs: list[str] | None) -> bool:
    if not globs:
        return True
    return any(fnmatch.fnmatchcase(rel_path, pattern) for pattern in globs)


def _is_hidden_relpath(rel_path: str) -> bool:
    return any(part.startswith(".") and part not in {"", ".", ".."} for part in rel_path.split("/"))


def _iter_candidate_files(
    *,
    root: Path,
    base: Path,
    globs: list[str] | None,
    include_hidden: bool,
):
    if base.is_file():
        # ripgrep does not apply --glob filtering to an explicitly targeted file path
        yield base
        return

    for current_root, dirnames, filenames in os.walk(base):
        if not include_hidden:
            dirnames[:] = sorted(dirname for dirname in dirnames if not dirname.startswith("."))
        else:
            dirnames[:] = sorted(dirnames)
        for filename in sorted(filenames):
            path = Path(current_root) / filename
            if not path.is_file():
                continue
            rel = _rel_path(root, path)
            if not include_hidden and _is_hidden_relpath(rel):
                continue
            base_rel = _rel_path(base, path)
            if _matches_globs(base_rel, globs):
                yield path


def _context_entries_from_lines(
    lines: list[str],
    *,
    line_number: int,
    before_context: int,
    after_context: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    before: list[dict[str, Any]] = []
    after: list[dict[str, Any]] = []
    text_truncated = False
    line_index = min(max(0, line_number - 1), len(lines))

    before_start = max(0, line_index - before_context)
    for index in range(before_start, line_index):
        clipped_text, clipped = _clip_match_text(lines[index])
        before.append({"line": index + 1, "text": clipped_text})
        text_truncated = text_truncated or clipped

    after_end = min(len(lines), line_index + 1 + after_context)
    for index in range(line_index + 1, after_end):
        clipped_text, clipped = _clip_match_text(lines[index])
        after.append({"line": index + 1, "text": clipped_text})
        text_truncated = text_truncated or clipped

    return before, after, text_truncated


def _safe_bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _read_context_lines(
    *,
    root: Path,
    path: Path,
    cache: dict[str, list[str] | None],
) -> list[str] | None:
    try:
        resolved = path.resolve()
        containing_root(root, resolved)
    except (OSError, ValueError):
        return None
    key = os.fspath(resolved)
    if key in cache:
        return cache[key]
    try:
        if resolved.stat().st_size > 1024 * 1024:
            cache[key] = None
            return None
        cache[key] = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        cache[key] = None
    return cache[key]


def _compile_python_matcher(*, pattern: str, literal: bool, case_sensitive: bool):
    if literal:
        needle = pattern if case_sensitive else pattern.casefold()

        def _literal_matches(line: str) -> bool:
            haystack = line if case_sensitive else line.casefold()
            return needle in haystack

        return _literal_matches

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        rx = re.compile(pattern, flags=flags)
    except re.error as e:
        raise SearchError(f"Invalid regex: {e}") from e

    return rx.search


def _rg_output_path_to_context_path(*, raw_path: str, root: Path) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return (root / path).resolve()


def _rg_rel_path(*, raw_path: str, root: Path) -> str:
    path = Path(raw_path)
    target = path if path.is_absolute() else root / path
    try:
        return os.fspath(target.resolve().relative_to(root)).replace("\\", "/")
    except (OSError, ValueError):
        return raw_path.replace("\\", "/")


def _queue_text_stream_lines(
    stream: Any,
    output_queue: queue.Queue[str | object],
    done_sentinel: object,
) -> None:
    try:
        for line in iter(stream.readline, ""):
            output_queue.put(line)
    finally:
        try:
            stream.close()
        except Exception:
            pass
        output_queue.put(done_sentinel)


def _collect_text_stream(stream: Any, collector: list[str]) -> None:
    try:
        for line in iter(stream.readline, ""):
            collector.append(line)
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _stop_process(process: Any) -> int:
    try:
        process.terminate()
    except OSError:
        pass
    try:
        return int(process.wait(timeout=0.5))
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        return int(process.wait())


def search_rg(
    *,
    root: Path,
    pattern: str,
    root_path: str = ".",
    globs: list[str] | None = None,
    max_results: int = 200,
    before_context: int = 0,
    after_context: int = 0,
    literal: bool = False,
    case_sensitive: bool = True,
    include_hidden: bool = False,
) -> dict[str, Any]:
    root_abs = root.resolve()
    base = (root_abs / root_path).resolve()
    try:
        containing_root(root_abs, base)
    except ValueError as e:
        raise SearchError(f"root_path escapes root: {root_path}") from e
    if pattern == "":
        raise SearchError("pattern must be a non-empty string")

    safe_max_results = _safe_bounded_int(
        max_results,
        default=200,
        minimum=1,
        maximum=_SEARCH_RG_MAX_RESULTS,
    )
    safe_before_context = _safe_bounded_int(
        before_context,
        default=0,
        minimum=0,
        maximum=_SEARCH_RG_MAX_CONTEXT_LINES,
    )
    safe_after_context = _safe_bounded_int(
        after_context,
        default=0,
        minimum=0,
        maximum=_SEARCH_RG_MAX_CONTEXT_LINES,
    )
    cleaned_globs = _clean_globs(globs)
    matches: list[dict[str, Any]] = []
    per_file_match_counts: dict[str, int] = defaultdict(int)
    context_cache: dict[str, list[str] | None] = {}
    output_chars = 0
    truncated = False
    per_file_truncated = False
    match_text_truncated = False
    context_text_truncated = False

    def _timeout_message(*, backend: str) -> str:
        return (
            f"{backend} search timed out after {_SEARCH_RG_TIMEOUT_S:g}s; "
            "try a narrower root_path, globs, or pattern"
        )

    def _append_match(
        *,
        path: str,
        line_number: int,
        line_text: str,
        context_lines: list[str] | None = None,
    ) -> str:
        nonlocal output_chars, truncated, per_file_truncated, match_text_truncated
        nonlocal context_text_truncated
        if per_file_match_counts[path] >= _SEARCH_RG_MAX_MATCHES_PER_FILE:
            truncated = True
            per_file_truncated = True
            return "per_file_limit"
        clipped_text, clipped = _clip_match_text(line_text)
        entry = {
            "path": path,
            "line": int(line_number),
            "text": clipped_text,
        }
        if clipped:
            entry["text_truncated"] = True
            match_text_truncated = True
        if context_lines is not None and (safe_before_context or safe_after_context):
            before, after, context_clipped = _context_entries_from_lines(
                context_lines,
                line_number=int(line_number),
                before_context=safe_before_context,
                after_context=safe_after_context,
            )
            if before:
                entry["before_context"] = before
            if after:
                entry["after_context"] = after
            context_text_truncated = context_text_truncated or context_clipped
        entry_chars = len(json.dumps(entry, ensure_ascii=True, separators=(",", ":")))
        if len(matches) >= safe_max_results:
            truncated = True
            return "max_results"
        if matches and (output_chars + entry_chars) > _SEARCH_RG_MAX_OUTPUT_CHARS:
            truncated = True
            return "output_cap"
        matches.append(entry)
        per_file_match_counts[path] += 1
        output_chars += entry_chars
        return "appended"

    if _rg_available():
        # A bounded search must not open ignore/Git files above its search scope.
        # Those parents may be outside the OS sandbox's approved workspace.
        cmd = ["rg", "--no-ignore-parent", "--no-require-git"]
        if _rg_supports_no_config():
            cmd.append("--no-config")
        if literal:
            cmd.append("--fixed-strings")
        if not case_sensitive:
            cmd.append("--ignore-case")
        if include_hidden:
            cmd.append("--hidden")
            cmd.extend(["--glob", "!.git/**"])
        cmd.extend(
            [
                "--json",
                "--line-buffered",
                "-e",
                pattern,
            ]
        )
        if cleaned_globs:
            for g in cleaned_globs:
                cmd.extend(["--glob", g])
        cmd.append(os.fspath(base))
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **command_text(),
                bufsize=1,
            )
        except OSError as e:
            raise SearchError("Failed to execute ripgrep") from e

        if proc.stdout is None or proc.stderr is None:
            raise SearchError("Failed to capture ripgrep output")

        stdout_queue: queue.Queue[str | object] = queue.Queue()
        stdout_done = object()
        stderr_lines: list[str] = []
        stdout_thread = threading.Thread(
            target=_queue_text_stream_lines,
            args=(proc.stdout, stdout_queue, stdout_done),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_collect_text_stream,
            args=(proc.stderr, stderr_lines),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        deadline = time.monotonic() + _SEARCH_RG_TIMEOUT_S
        stdout_finished = False
        terminated_early = False

        while not stdout_finished:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_process(proc)
                stdout_thread.join(timeout=0.5)
                stderr_thread.join(timeout=0.5)
                raise SearchError(_timeout_message(backend="ripgrep"))
            try:
                line = stdout_queue.get(timeout=min(0.1, remaining))
            except queue.Empty:
                if proc.poll() is not None and not stdout_thread.is_alive():
                    stdout_finished = True
                continue
            if line is stdout_done:
                stdout_finished = True
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "match":
                continue
            data = obj.get("data") or {}
            path = ((data.get("path") or {}).get("text")) or ""
            line_number = data.get("line_number")
            lines = ((data.get("lines") or {}).get("text")) or ""
            if not path or not line_number:
                continue
            rel = _rg_rel_path(raw_path=path, root=root_abs)
            context_lines = None
            if safe_before_context or safe_after_context:
                context_lines = _read_context_lines(
                    root=root_abs,
                    path=_rg_output_path_to_context_path(raw_path=path, root=root_abs),
                    cache=context_cache,
                )
            append_status = _append_match(
                path=rel,
                line_number=int(line_number),
                line_text=lines,
                context_lines=context_lines,
            )
            if append_status == "max_results":
                terminated_early = True
                break
            if append_status == "output_cap":
                terminated_early = True
                break

        returncode = _stop_process(proc) if terminated_early else int(proc.wait())
        stdout_thread.join(timeout=0.5)
        stderr_thread.join(timeout=0.5)
        stderr_text = "".join(stderr_lines).strip()
        if not terminated_early and returncode not in (0, 1):  # 1 means no matches
            raise SearchError(stderr_text or f"rg failed with exit code {returncode}")

        return {
            "pattern": pattern,
            "matches": matches,
            "backend": "rg",
            "truncated": truncated,
            "per_file_truncated": per_file_truncated,
            "match_text_truncated": match_text_truncated,
            "context_text_truncated": context_text_truncated,
            "returned_matches": len(matches),
            "literal": bool(literal),
            "case_sensitive": bool(case_sensitive),
            "include_hidden": bool(include_hidden),
            "before_context": safe_before_context,
            "after_context": safe_after_context,
        }

    matcher = _compile_python_matcher(
        pattern=pattern,
        literal=bool(literal),
        case_sensitive=bool(case_sensitive),
    )

    deadline = time.monotonic() + _SEARCH_RG_TIMEOUT_S

    def _raise_if_python_timeout() -> None:
        if time.monotonic() >= deadline:
            raise SearchError(_timeout_message(backend="python fallback"))

    for p in _iter_candidate_files(
        root=root_abs,
        base=base,
        globs=cleaned_globs,
        include_hidden=bool(include_hidden),
    ):
        _raise_if_python_timeout()
        if len(matches) >= safe_max_results:
            truncated = True
            break
        # Skip large files.
        try:
            if p.stat().st_size > 512 * 1024:
                continue
        except OSError:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        stop_search = False
        source_lines = text.splitlines()
        for i, line in enumerate(source_lines, start=1):
            _raise_if_python_timeout()
            if len(matches) >= safe_max_results:
                truncated = True
                stop_search = True
                break
            if matcher(line):
                rel = _rel_path(root_abs, p)
                append_status = _append_match(
                    path=rel,
                    line_number=i,
                    line_text=line,
                    context_lines=source_lines,
                )
                if append_status == "max_results":
                    stop_search = True
                    break
                if append_status == "output_cap":
                    stop_search = True
                    break
                if append_status == "per_file_limit":
                    break
        if stop_search:
            break

    return {
        "pattern": pattern,
        "matches": matches,
        "backend": "python",
        "truncated": truncated,
        "per_file_truncated": per_file_truncated,
        "match_text_truncated": match_text_truncated,
        "context_text_truncated": context_text_truncated,
        "returned_matches": len(matches),
        "literal": bool(literal),
        "case_sensitive": bool(case_sensitive),
        "include_hidden": bool(include_hidden),
        "before_context": safe_before_context,
        "after_context": safe_after_context,
    }
