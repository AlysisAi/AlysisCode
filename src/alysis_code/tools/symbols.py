from __future__ import annotations

import ast
import fnmatch
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..file_classification import CODE_SCAN_SKIP_DIR_NAMES, symbol_search_backend_for_path
from ..project_folders import containing_root, relative_path
from .errors import SymbolSearchError

_SUPPORTED_KINDS = {"function", "class", "method", "constant"}
_DEFAULT_MAX_RESULTS = 100
_MAX_RESULTS = 200
_MAX_FILE_BYTES = 512 * 1024
_MAX_INLINE_CHARS = 240
_MAX_NOTE_PATHS = 3
_DEFAULT_SKIP_DIRS = set(CODE_SCAN_SKIP_DIR_NAMES)
_JS_TS_BLOCK_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "class"}
# Capture whole lexical candidates, including combining marks, before validating
# Unicode character classes. These declaration recognizers remain heuristics;
# they do not decode escaped identifiers or replace a language parser.
_IDENTIFIER_CHARACTER = r"(?:[A-Za-z0-9_$]|[^\x00-\x7f\s])"
_IDENTIFIER_CANDIDATE = rf"{_IDENTIFIER_CHARACTER}+"
_IDENTIFIER_END = rf"(?!{_IDENTIFIER_CHARACTER}|\\)"
_JS_TS_FUNCTION_RE = re.compile(
    rf"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+({_IDENTIFIER_CANDIDATE})\s*\("
)
_JS_TS_CLASS_RE = re.compile(
    rf"^(?:export\s+)?(?:default\s+)?class\s+({_IDENTIFIER_CANDIDATE}){_IDENTIFIER_END}"
)
_JS_TS_CONST_DECL_RE = re.compile(
    rf"^(?:export\s+)?const\s+({_IDENTIFIER_CANDIDATE}){_IDENTIFIER_END}(.*)$"
)
_JS_TS_FUNCTION_EXPR_RE = re.compile(r"^(?:async\s+)?function\b")
_JS_TS_METHOD_RE = re.compile(
    r"^(?:(?:public|private|protected|static|async|readonly|override|get|set)\s+)*"
    rf"(?:\*\s*)?({_IDENTIFIER_CANDIDATE})\s*(?:<[^>{{}}]*>)?\s*\("
)
_JS_TS_CLASS_FIELD_RE = re.compile(
    r"^(?:(?:public|private|protected|readonly|static|async|declare|override)\s+)*"
    rf"({_IDENTIFIER_CANDIDATE}){_IDENTIFIER_END}\s*(?:\?)?(.*)$"
)
_JS_TS_PENDING_ASSIGNMENT_MAX_LINES = 8
_JAVA_IDENTIFIER = _IDENTIFIER_CANDIDATE
_JAVA_ANNOTATION_PREFIX = rf"(?:@{_JAVA_IDENTIFIER}(?:\.{_JAVA_IDENTIFIER})*(?:\([^)]*\))?\s+)*"
_JAVA_MODIFIER_PREFIX = (
    r"(?:(?:public|protected|private|abstract|static|final|sealed|non-sealed|strictfp|"
    r"synchronized|native|default|transient|volatile)\s+)*"
)
_JAVA_GENERIC_METHOD_PREFIX = r"(?:<[^;{}()]+>\s+)?"
_JAVA_DECL_PREFIX = _JAVA_ANNOTATION_PREFIX + _JAVA_MODIFIER_PREFIX
_JAVA_TYPE_RE = re.compile(
    rf"^{_JAVA_DECL_PREFIX}(?P<kind>class|interface|enum|record)\s+"
    rf"(?P<name>{_JAVA_IDENTIFIER}){_IDENTIFIER_END}"
)
_JAVA_METHOD_RE = re.compile(
    rf"^{_JAVA_DECL_PREFIX}{_JAVA_GENERIC_METHOD_PREFIX}"
    rf"(?P<return>[\w$\u0080-\U0010ffff<>\[\].?,\s]+?)\s+(?P<name>{_JAVA_IDENTIFIER})\s*\("
)
_JAVA_CONSTRUCTOR_RE = re.compile(rf"^{_JAVA_DECL_PREFIX}(?P<name>{_JAVA_IDENTIFIER})\s*\(")
_JAVA_BLOCK_KEYWORDS = {
    "catch",
    "do",
    "else",
    "for",
    "if",
    "new",
    "return",
    "switch",
    "synchronized",
    "throw",
    "try",
    "while",
}


def _identifier_candidate_is_valid(name: str, *, java: bool) -> bool:
    """Validate Unicode lexical candidates without normalizing their spelling.

    Unicode letters/marks and identifier predicates cover scripts uniformly.
    Language-specific punctuation follows identifier syntax, not a name/script
    allowlist. Full syntax, escape processing and semantic resolution remain out
    of scope for these heuristic backends.
    """

    if name.isascii():
        return name.replace("$", "_").isidentifier()
    for index, char in enumerate(name):
        category = unicodedata.category(char)
        starts = (
            char in "_$"
            or (not java and char.isidentifier())
            or category.startswith("L")
            or category == "Nl"
            or (java and category in {"Sc", "Pc"})
        )
        if starts:
            continue
        if index == 0:
            return False
        if (
            (not java and ("a" + char).isidentifier())
            or category in {"Mn", "Mc", "Nd", "Pc"}
            or (java and category == "Cf")
            or (not java and char in "\u200c\u200d")
        ):
            continue
        return False
    return bool(name)


def _match_identifier(
    pattern: re.Pattern[str], text: str, *, java: bool = False
) -> re.Match[str] | None:
    match = pattern.match(text)
    if match is None:
        return None
    name = match.group("name" if java else 1)
    return match if _identifier_candidate_is_valid(name, java=java) else None


@dataclass(frozen=True)
class _SymbolEntry:
    path: str
    line: int
    kind: str
    name: str
    simple_name: str
    signature: str
    end_line: int | None = None
    parent: str = ""


@dataclass(frozen=True)
class _JsTsClassScope:
    name: str
    body_depth: int


@dataclass
class _JsTsPendingAssignment:
    line: int
    kind: str
    name: str
    simple_name: str
    signature_lines: list[str]
    rhs_fragments: list[str]
    scope_depth: int
    fallback_kind: str | None = None
    parent: str = ""


@dataclass(frozen=True)
class _JavaTypeScope:
    name: str
    body_depth: int


def _clip_inline(text: str, *, max_chars: int = _MAX_INLINE_CHARS) -> str:
    compact = " ".join(str(text).split())
    if len(compact) <= max_chars:
        return compact
    return compact[:max_chars].rstrip() + "...(truncated)"


def _resolve_under_root(root: Path, user_path: str) -> Path:
    root_abs = root.resolve()
    target = (root_abs / user_path).resolve()
    try:
        containing_root(root_abs, target)
    except ValueError as e:
        raise SymbolSearchError(f"root_path escapes root: {user_path}") from e
    return target


def _rel_path(root: Path, path: Path) -> str:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return os.fspath(path).replace("\\", "/")
    return os.fspath(rel).replace("\\", "/")


def _matches_globs(rel_path: str, globs: list[str] | None) -> bool:
    if not globs:
        return True
    return any(fnmatch.fnmatchcase(rel_path, pattern) for pattern in globs)


def _iter_candidate_files(*, root: Path, base: Path, globs: list[str] | None) -> list[Path]:
    if base.is_file():
        rel = _rel_path(root, base)
        return [base] if _matches_globs(rel, globs) else []

    candidates: list[Path] = []
    for current_root, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(dirname for dirname in dirnames if dirname not in _DEFAULT_SKIP_DIRS)
        for filename in sorted(filenames):
            path = Path(current_root) / filename
            rel = _rel_path(root, path)
            if _matches_globs(rel, globs):
                candidates.append(path)
    return candidates


def _safe_unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "..."


def _format_arg(arg: ast.arg) -> str:
    text = arg.arg
    if arg.annotation is not None:
        text += f": {_safe_unparse(arg.annotation)}"
    return text


def _format_function_signature(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    display_name: str,
) -> str:
    params: list[str] = []
    posonlyargs = getattr(node.args, "posonlyargs", [])
    for arg in posonlyargs:
        params.append(_format_arg(arg))
    if posonlyargs:
        params.append("/")

    for arg in node.args.args:
        params.append(_format_arg(arg))

    if node.args.vararg is not None:
        params.append(f"*{_format_arg(node.args.vararg)}")
    elif node.args.kwonlyargs:
        params.append("*")

    for arg in node.args.kwonlyargs:
        params.append(_format_arg(arg))

    if node.args.kwarg is not None:
        params.append(f"**{_format_arg(node.args.kwarg)}")

    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    signature = f"{prefix} {display_name}({', '.join(params)})"
    if node.returns is not None:
        signature += f" -> {_safe_unparse(node.returns)}"
    return _clip_inline(signature)


def _format_class_signature(node: ast.ClassDef) -> str:
    bases = [_safe_unparse(base) for base in node.bases]
    signature = f"class {node.name}"
    if bases:
        signature += f"({', '.join(bases)})"
    return _clip_inline(signature)


def _extract_assign_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for element in target.elts:
            names.extend(_extract_assign_names(element))
        return names
    return []


def _extract_constant_entries(
    *,
    rel_path: str,
    node: ast.Assign | ast.AnnAssign,
    source_lines: list[str],
) -> list[_SymbolEntry]:
    if isinstance(node, ast.Assign):
        names: list[str] = []
        for target in node.targets:
            names.extend(_extract_assign_names(target))
    else:
        names = _extract_assign_names(node.target)

    signature = ""
    if 1 <= node.lineno <= len(source_lines):
        signature = _clip_inline(source_lines[node.lineno - 1].strip())

    out: list[_SymbolEntry] = []
    for name in names:
        if not name.isupper():
            continue
        out.append(
            _SymbolEntry(
                path=rel_path,
                line=node.lineno,
                kind="constant",
                name=name,
                simple_name=name,
                signature=signature,
                end_line=getattr(node, "end_lineno", node.lineno),
            )
        )
    return out


def _extract_python_symbols(*, root: Path, path: Path, text: str) -> list[_SymbolEntry]:
    rel_path = _rel_path(root, path)
    tree = ast.parse(text, filename=rel_path)
    source_lines = text.splitlines()
    symbols: list[_SymbolEntry] = []

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            symbols.append(
                _SymbolEntry(
                    path=rel_path,
                    line=node.lineno,
                    kind="class",
                    name=node.name,
                    simple_name=node.name,
                    signature=_format_class_signature(node),
                    end_line=getattr(node, "end_lineno", node.lineno),
                )
            )
            for body_item in node.body:
                if not isinstance(body_item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                qualified_name = f"{node.name}.{body_item.name}"
                symbols.append(
                    _SymbolEntry(
                        path=rel_path,
                        line=body_item.lineno,
                        kind="method",
                        name=qualified_name,
                        simple_name=body_item.name,
                        signature=_format_function_signature(
                            body_item,
                            display_name=qualified_name,
                        ),
                        end_line=getattr(body_item, "end_lineno", body_item.lineno),
                        parent=node.name,
                    )
                )
            continue

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(
                _SymbolEntry(
                    path=rel_path,
                    line=node.lineno,
                    kind="function",
                    name=node.name,
                    simple_name=node.name,
                    signature=_format_function_signature(node, display_name=node.name),
                    end_line=getattr(node, "end_lineno", node.lineno),
                )
            )
            continue

        if isinstance(node, ast.Assign):
            symbols.extend(
                _extract_constant_entries(
                    rel_path=rel_path,
                    node=node,
                    source_lines=source_lines,
                )
            )
            continue

        if isinstance(node, ast.AnnAssign):
            symbols.extend(
                _extract_constant_entries(
                    rel_path=rel_path,
                    node=node,
                    source_lines=source_lines,
                )
            )

    return symbols


def _sanitize_js_ts_line(
    line: str,
    *,
    in_block_comment: bool,
    in_backtick_string: bool,
) -> tuple[str, bool, bool]:
    out: list[str] = []
    index = 0
    quote: str | None = None
    line_len = len(line)
    while index < line_len:
        ch = line[index]
        nxt = line[index + 1] if index + 1 < line_len else ""

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                index += 2
                continue
            index += 1
            continue

        if in_backtick_string:
            if ch == "\\":
                index += 2
                continue
            if ch == "`":
                in_backtick_string = False
            index += 1
            continue

        if quote is not None:
            if ch == "\\":
                index += 2
                continue
            if ch == quote:
                quote = None
            index += 1
            continue

        if ch == "/" and nxt == "*":
            in_block_comment = True
            index += 2
            continue
        if ch == "/" and nxt == "/":
            break
        if ch == "`":
            in_backtick_string = True
            index += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            index += 1
            continue

        out.append(ch)
        index += 1

    return "".join(out), in_block_comment, in_backtick_string


def _consume_js_ts_identifier(text: str, start: int) -> int | None:
    if start >= len(text):
        return None
    ch = text[start]
    if ch != "_" and ch != "$" and not ch.isalpha():
        return None
    index = start + 1
    while index < len(text):
        ch = text[index]
        if ch != "_" and ch != "$" and not ch.isalnum():
            break
        index += 1
    return index


def _consume_js_ts_parenthesized(text: str, start: int) -> int | None:
    if start >= len(text) or text[start] != "(":
        return None

    depth = 0
    for index in range(start, len(text)):
        ch = text[index]
        if ch == "(":
            depth += 1
            continue
        if ch == ")":
            depth -= 1
            if depth == 0:
                return index + 1

    return None


def _consume_js_ts_generic_prefix(text: str, start: int) -> int | None:
    if start >= len(text) or text[start] != "<":
        return start

    angle_depth = 0
    paren_depth = 0
    bracket_depth = 0
    brace_depth = 0

    for index in range(start, len(text)):
        ch = text[index]
        prev = text[index - 1] if index > start else ""
        if ch == "<":
            angle_depth += 1
            continue
        if ch == ">" and angle_depth > 0 and prev != "=":
            angle_depth -= 1
            if angle_depth == 0 and paren_depth == 0 and bracket_depth == 0 and brace_depth == 0:
                return index + 1
            continue
        if ch == "(":
            paren_depth += 1
            continue
        if ch == ")" and paren_depth > 0:
            paren_depth -= 1
            continue
        if ch == "[":
            bracket_depth += 1
            continue
        if ch == "]" and bracket_depth > 0:
            bracket_depth -= 1
            continue
        if ch == "{":
            brace_depth += 1
            continue
        if ch == "}" and brace_depth > 0:
            brace_depth -= 1

    return None


def _has_js_ts_top_level_arrow(text: str, start: int) -> bool:
    paren_depth = 0
    bracket_depth = 0
    brace_depth = 0
    angle_depth = 0

    for index in range(start, len(text)):
        ch = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        prev = text[index - 1] if index > start else ""

        if ch == "(":
            paren_depth += 1
            continue
        if ch == ")" and paren_depth > 0:
            paren_depth -= 1
            continue
        if ch == "[":
            bracket_depth += 1
            continue
        if ch == "]" and bracket_depth > 0:
            bracket_depth -= 1
            continue
        if ch == "{":
            brace_depth += 1
            continue
        if ch == "}" and brace_depth > 0:
            brace_depth -= 1
            continue
        if ch == "<":
            angle_depth += 1
            continue
        if ch == ">" and angle_depth > 0 and prev != "=":
            angle_depth -= 1
            continue
        if (
            ch == "="
            and nxt == ">"
            and paren_depth == 0
            and bracket_depth == 0
            and brace_depth == 0
            and angle_depth == 0
        ):
            return True

    return False


def _looks_like_js_ts_arrow_rhs(value: str) -> bool:
    candidate = value.strip()
    if not candidate:
        return False

    if candidate.startswith("async "):
        candidate = candidate[6:].lstrip()

    generic_end = _consume_js_ts_generic_prefix(candidate, 0)
    if generic_end is None:
        return False
    candidate = candidate[generic_end:].lstrip()
    if not candidate:
        return False

    if candidate.startswith("("):
        params_end = _consume_js_ts_parenthesized(candidate, 0)
    else:
        params_end = _consume_js_ts_identifier(candidate, 0)
    if params_end is None:
        return False

    return _has_js_ts_top_level_arrow(candidate, params_end)


def _is_js_ts_function_rhs(value: str) -> bool:
    candidate = value.strip().rstrip(";").strip()
    if _looks_like_js_ts_arrow_rhs(candidate):
        return True
    if _JS_TS_FUNCTION_EXPR_RE.match(candidate):
        tail = candidate.partition("function")[2]
        return ")" in tail or "{" in tail
    return False


def _looks_like_js_ts_multiline_function_rhs(value: str) -> bool:
    candidate = value.strip()
    if not candidate:
        return True
    prefixes = (
        "(",
        "async (",
        "function",
        "async function",
        "<",
        "async <",
    )
    return candidate.startswith(prefixes)


def _split_js_ts_assignment(value: str) -> tuple[str, str] | None:
    paren_depth = 0
    bracket_depth = 0
    brace_depth = 0
    angle_depth = 0

    for index, ch in enumerate(value):
        nxt = value[index + 1] if index + 1 < len(value) else ""
        prev = value[index - 1] if index > 0 else ""

        if ch == "(":
            paren_depth += 1
            continue
        if ch == ")" and paren_depth > 0:
            paren_depth -= 1
            continue
        if ch == "[":
            bracket_depth += 1
            continue
        if ch == "]" and bracket_depth > 0:
            bracket_depth -= 1
            continue
        if ch == "{":
            brace_depth += 1
            continue
        if ch == "}" and brace_depth > 0:
            brace_depth -= 1
            continue
        if ch == "<":
            angle_depth += 1
            continue
        if ch == ">" and angle_depth > 0:
            angle_depth -= 1
            continue
        if (
            ch == "="
            and paren_depth == 0
            and bracket_depth == 0
            and brace_depth == 0
            and angle_depth == 0
            and nxt != ">"
            and nxt != "="
            and prev not in {"!", "<", ">", "="}
        ):
            lhs = value[:index].strip()
            rhs = value[index + 1 :].strip()
            return lhs, rhs

    return None


def _parse_js_ts_const_assignment(line: str) -> tuple[str, str] | None:
    match = _match_identifier(_JS_TS_CONST_DECL_RE, line)
    if match is None:
        return None
    assignment = _split_js_ts_assignment(match.group(2))
    if assignment is None:
        return None
    _lhs, rhs = assignment
    return match.group(1), rhs


def _parse_js_ts_class_field_assignment(line: str) -> tuple[str, str] | None:
    match = _match_identifier(_JS_TS_CLASS_FIELD_RE, line)
    if match is None:
        return None
    assignment = _split_js_ts_assignment(match.group(2))
    if assignment is None:
        return None
    _lhs, rhs = assignment
    return match.group(1), rhs


def _js_ts_pending_signature(signature_lines: list[str]) -> str:
    parts = [part for part in signature_lines if part]
    return _clip_inline(" ".join(parts))


def _js_ts_assignment_entry(
    *,
    rel_path: str,
    line: int,
    kind: str,
    name: str,
    simple_name: str,
    signature_lines: list[str],
    parent: str = "",
) -> _SymbolEntry:
    return _SymbolEntry(
        path=rel_path,
        line=line,
        kind=kind,
        name=name,
        simple_name=simple_name,
        signature=_js_ts_pending_signature(signature_lines),
        end_line=line + max(0, len(signature_lines) - 1),
        parent=parent,
    )


def _start_js_ts_pending_assignment(
    *,
    line: int,
    kind: str,
    name: str,
    simple_name: str,
    signature_line: str,
    rhs_fragment: str,
    scope_depth: int,
    fallback_kind: str | None = None,
    parent: str = "",
) -> _JsTsPendingAssignment:
    return _JsTsPendingAssignment(
        line=line,
        kind=kind,
        name=name,
        simple_name=simple_name,
        signature_lines=[signature_line] if signature_line else [],
        rhs_fragments=[rhs_fragment] if rhs_fragment else [],
        scope_depth=scope_depth,
        fallback_kind=fallback_kind,
        parent=parent,
    )


def _finalize_js_ts_pending_assignment(
    *,
    rel_path: str,
    symbols: list[_SymbolEntry],
    pending: _JsTsPendingAssignment | None,
    resolved: bool,
) -> None:
    if pending is None:
        return
    if resolved:
        symbols.append(
            _js_ts_assignment_entry(
                rel_path=rel_path,
                line=pending.line,
                kind=pending.kind,
                name=pending.name,
                simple_name=pending.simple_name,
                signature_lines=pending.signature_lines,
                parent=pending.parent,
            )
        )
        return
    if pending.fallback_kind is not None:
        symbols.append(
            _js_ts_assignment_entry(
                rel_path=rel_path,
                line=pending.line,
                kind=pending.fallback_kind,
                name=pending.name,
                simple_name=pending.simple_name,
                signature_lines=pending.signature_lines,
                parent=pending.parent,
            )
        )


def _append_js_ts_pending_assignment_line(
    *,
    rel_path: str,
    symbols: list[_SymbolEntry],
    pending: _JsTsPendingAssignment,
    raw_signature_line: str,
    rhs_fragment: str,
) -> _JsTsPendingAssignment | None:
    if raw_signature_line:
        pending.signature_lines.append(raw_signature_line)
    if rhs_fragment:
        pending.rhs_fragments.append(rhs_fragment)

    combined_rhs = " ".join(fragment for fragment in pending.rhs_fragments if fragment)
    if _is_js_ts_function_rhs(combined_rhs):
        _finalize_js_ts_pending_assignment(
            rel_path=rel_path,
            symbols=symbols,
            pending=pending,
            resolved=True,
        )
        return None

    signature_line_count = len(pending.signature_lines)
    if (
        signature_line_count >= _JS_TS_PENDING_ASSIGNMENT_MAX_LINES
        or rhs_fragment.rstrip().endswith(";")
    ):
        _finalize_js_ts_pending_assignment(
            rel_path=rel_path,
            symbols=symbols,
            pending=pending,
            resolved=False,
        )
        return None
    return pending


def _extract_js_ts_symbols(*, root: Path, path: Path, text: str) -> list[_SymbolEntry]:
    rel_path = _rel_path(root, path)
    symbols: list[_SymbolEntry] = []
    brace_depth = 0
    in_block_comment = False
    in_backtick_string = False
    pending_class_name: str | None = None
    class_stack: list[_JsTsClassScope] = []
    pending_assignment: _JsTsPendingAssignment | None = None

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        sanitized, in_block_comment, in_backtick_string = _sanitize_js_ts_line(
            raw_line,
            in_block_comment=in_block_comment,
            in_backtick_string=in_backtick_string,
        )
        stripped = sanitized.strip()
        if not stripped:
            continue

        depth_before = brace_depth
        while class_stack and depth_before < class_stack[-1].body_depth:
            class_stack.pop()

        if pending_class_name is not None and depth_before == 0 and "{" in stripped:
            class_stack.append(_JsTsClassScope(name=pending_class_name, body_depth=1))
            pending_class_name = None

        source_signature = _clip_inline(raw_line.strip())
        consumed_by_pending = False

        if pending_assignment is not None:
            if depth_before != pending_assignment.scope_depth:
                _finalize_js_ts_pending_assignment(
                    rel_path=rel_path,
                    symbols=symbols,
                    pending=pending_assignment,
                    resolved=False,
                )
                pending_assignment = None
            else:
                pending_assignment = _append_js_ts_pending_assignment_line(
                    rel_path=rel_path,
                    symbols=symbols,
                    pending=pending_assignment,
                    raw_signature_line=raw_line.strip(),
                    rhs_fragment=stripped,
                )
                consumed_by_pending = True

        if not consumed_by_pending and depth_before == 0:
            function_match = _match_identifier(_JS_TS_FUNCTION_RE, stripped)
            if function_match:
                function_name = function_match.group(1)
                symbols.append(
                    _SymbolEntry(
                        path=rel_path,
                        line=line_number,
                        kind="function",
                        name=function_name,
                        simple_name=function_name,
                        signature=source_signature,
                        end_line=line_number,
                    )
                )
            else:
                class_match = _match_identifier(_JS_TS_CLASS_RE, stripped)
                if class_match:
                    class_name = class_match.group(1)
                    symbols.append(
                        _SymbolEntry(
                            path=rel_path,
                            line=line_number,
                            kind="class",
                            name=class_name,
                            simple_name=class_name,
                            signature=source_signature,
                            end_line=line_number,
                        )
                    )
                    class_tail = stripped[class_match.end() :]
                    if "{" in class_tail:
                        class_stack.append(_JsTsClassScope(name=class_name, body_depth=1))
                        pending_class_name = None
                    else:
                        pending_class_name = class_name
                else:
                    const_assignment = _parse_js_ts_const_assignment(stripped)
                    if const_assignment is not None:
                        const_name, const_value = const_assignment
                        if _is_js_ts_function_rhs(const_value):
                            symbols.append(
                                _SymbolEntry(
                                    path=rel_path,
                                    line=line_number,
                                    kind="function",
                                    name=const_name,
                                    simple_name=const_name,
                                    signature=source_signature,
                                    end_line=line_number,
                                )
                            )
                        elif _looks_like_js_ts_multiline_function_rhs(const_value):
                            pending_assignment = _start_js_ts_pending_assignment(
                                line=line_number,
                                kind="function",
                                name=const_name,
                                simple_name=const_name,
                                signature_line=raw_line.strip(),
                                rhs_fragment=const_value,
                                scope_depth=depth_before,
                                fallback_kind="constant",
                            )
                        else:
                            symbols.append(
                                _SymbolEntry(
                                    path=rel_path,
                                    line=line_number,
                                    kind="constant",
                                    name=const_name,
                                    simple_name=const_name,
                                    signature=source_signature,
                                    end_line=line_number,
                                )
                            )

        if not consumed_by_pending and class_stack and depth_before == class_stack[-1].body_depth:
            class_name = class_stack[-1].name
            field_assignment = _parse_js_ts_class_field_assignment(stripped)
            if field_assignment is not None:
                field_name, field_value = field_assignment
                qualified_name = f"{class_name}.{field_name}"
                if _is_js_ts_function_rhs(field_value):
                    symbols.append(
                        _SymbolEntry(
                            path=rel_path,
                            line=line_number,
                            kind="method",
                            name=qualified_name,
                            simple_name=field_name,
                            signature=source_signature,
                            end_line=line_number,
                            parent=class_name,
                        )
                    )
                elif _looks_like_js_ts_multiline_function_rhs(field_value):
                    pending_assignment = _start_js_ts_pending_assignment(
                        line=line_number,
                        kind="method",
                        name=qualified_name,
                        simple_name=field_name,
                        signature_line=raw_line.strip(),
                        rhs_fragment=field_value,
                        scope_depth=depth_before,
                        parent=class_name,
                    )
            if pending_assignment is None:
                method_match = _match_identifier(_JS_TS_METHOD_RE, stripped)
                if method_match:
                    method_name = method_match.group(1)
                    if method_name not in _JS_TS_BLOCK_KEYWORDS:
                        qualified_name = f"{class_name}.{method_name}"
                        symbols.append(
                            _SymbolEntry(
                                path=rel_path,
                                line=line_number,
                                kind="method",
                                name=qualified_name,
                                simple_name=method_name,
                                signature=source_signature,
                                end_line=line_number,
                                parent=class_name,
                            )
                        )

        brace_depth = max(0, brace_depth + stripped.count("{") - stripped.count("}"))
        while class_stack and brace_depth < class_stack[-1].body_depth:
            class_stack.pop()

    if pending_assignment is not None:
        _finalize_js_ts_pending_assignment(
            rel_path=rel_path,
            symbols=symbols,
            pending=pending_assignment,
            resolved=False,
        )

    return symbols


def _sanitize_java_line(line: str, *, in_block_comment: bool) -> tuple[str, bool]:
    out: list[str] = []
    index = 0
    quote: str | None = None
    line_len = len(line)
    while index < line_len:
        ch = line[index]
        nxt = line[index + 1] if index + 1 < line_len else ""

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                index += 2
                continue
            index += 1
            continue

        if quote is not None:
            if ch == "\\":
                index += 2
                continue
            if ch == quote:
                quote = None
            index += 1
            continue

        if ch == "/" and nxt == "*":
            in_block_comment = True
            index += 2
            continue
        if ch == "/" and nxt == "/":
            break
        if ch in {"'", '"'}:
            quote = ch
            index += 1
            continue

        out.append(ch)
        index += 1

    return "".join(out), in_block_comment


def _extract_java_symbols(*, root: Path, path: Path, text: str) -> list[_SymbolEntry]:
    rel_path = _rel_path(root, path)
    symbols: list[_SymbolEntry] = []
    brace_depth = 0
    in_block_comment = False
    pending_type_name: str | None = None
    type_stack: list[_JavaTypeScope] = []

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        sanitized, in_block_comment = _sanitize_java_line(
            raw_line,
            in_block_comment=in_block_comment,
        )
        stripped = sanitized.strip()
        if not stripped:
            continue

        depth_before = brace_depth
        while type_stack and depth_before < type_stack[-1].body_depth:
            type_stack.pop()

        if pending_type_name is not None and "{" in stripped:
            type_stack.append(_JavaTypeScope(name=pending_type_name, body_depth=depth_before + 1))
            pending_type_name = None

        source_signature = _clip_inline(raw_line.strip())
        type_match = _match_identifier(_JAVA_TYPE_RE, stripped, java=True)
        if type_match:
            type_name = type_match.group("name")
            symbols.append(
                _SymbolEntry(
                    path=rel_path,
                    line=line_number,
                    kind="class",
                    name=type_name,
                    simple_name=type_name,
                    signature=source_signature,
                    end_line=line_number,
                )
            )
            type_tail = stripped[type_match.end() :]
            if "{" in type_tail:
                type_stack.append(_JavaTypeScope(name=type_name, body_depth=depth_before + 1))
            else:
                pending_type_name = type_name
        elif type_stack and depth_before == type_stack[-1].body_depth:
            class_name = type_stack[-1].name
            method_match = _match_identifier(_JAVA_METHOD_RE, stripped, java=True)
            method_name = method_match.group("name") if method_match else ""
            if method_name and method_name not in _JAVA_BLOCK_KEYWORDS:
                qualified_name = f"{class_name}.{method_name}"
                symbols.append(
                    _SymbolEntry(
                        path=rel_path,
                        line=line_number,
                        kind="method",
                        name=qualified_name,
                        simple_name=method_name,
                        signature=source_signature,
                        end_line=line_number,
                        parent=class_name,
                    )
                )
            elif not method_match:
                constructor_match = _match_identifier(_JAVA_CONSTRUCTOR_RE, stripped, java=True)
                constructor_name = constructor_match.group("name") if constructor_match else ""
                if constructor_name == class_name:
                    qualified_name = f"{class_name}.{constructor_name}"
                    symbols.append(
                        _SymbolEntry(
                            path=rel_path,
                            line=line_number,
                            kind="method",
                            name=qualified_name,
                            simple_name=constructor_name,
                            signature=source_signature,
                            end_line=line_number,
                            parent=class_name,
                        )
                    )

        brace_depth = max(0, brace_depth + stripped.count("{") - stripped.count("}"))
        while type_stack and brace_depth < type_stack[-1].body_depth:
            type_stack.pop()

    return symbols


def _matches_query(entry: _SymbolEntry, *, query: str, exact: bool) -> bool:
    if exact:
        return query == entry.name or query == entry.simple_name

    query_cf = query.casefold()
    return query_cf in entry.name.casefold() or query_cf in entry.simple_name.casefold()


def _add_note(notes: list[str], message: str) -> None:
    if message and message not in notes:
        notes.append(message)


def _entry_end_line(entry: _SymbolEntry) -> int:
    end_line = entry.end_line if entry.end_line is not None else entry.line
    return max(entry.line, int(end_line))


def _entry_parent(entry: _SymbolEntry) -> str:
    if entry.parent:
        return entry.parent
    if entry.kind == "method" and "." in entry.name:
        return entry.name.rsplit(".", 1)[0]
    return ""


def _symbol_snippet(
    *,
    source_lines: list[str],
    start_line: int,
    end_line: int,
    max_lines: int = 8,
) -> dict[str, Any] | None:
    if start_line < 1 or start_line > len(source_lines):
        return None
    bounded_end = min(max(start_line, end_line), len(source_lines), start_line + max_lines - 1)
    lines = [
        {
            "line": line_number,
            "text": _clip_inline(source_lines[line_number - 1], max_chars=160),
        }
        for line_number in range(start_line, bounded_end + 1)
    ]
    return {
        "start_line": start_line,
        "end_line": bounded_end,
        "truncated": bounded_end < min(max(start_line, end_line), len(source_lines)),
        "lines": lines,
    }


def _reference_hints_for_entry(
    *,
    entry: _SymbolEntry,
    source_lines_by_path: dict[str, list[str]],
    max_references: int = 5,
) -> list[dict[str, Any]]:
    needle = entry.simple_name or entry.name
    if not needle:
        return []
    out: list[dict[str, Any]] = []
    defining_range = range(entry.line, _entry_end_line(entry) + 1)
    for rel_path, source_lines in source_lines_by_path.items():
        for line_number, line in enumerate(source_lines, start=1):
            if rel_path == entry.path and line_number in defining_range:
                continue
            if needle not in line:
                continue
            out.append(
                {
                    "path": rel_path,
                    "line": line_number,
                    "text": _clip_inline(line.strip(), max_chars=200),
                }
            )
            if len(out) >= max_references:
                return out
    return out


def symbol_search(
    *,
    root: Path,
    query: str,
    kind: str | None = None,
    root_path: str = ".",
    globs: list[str] | None = None,
    max_results: int = _DEFAULT_MAX_RESULTS,
    exact: bool = False,
    include_details: bool = False,
    include_snippet: bool = False,
    include_references: bool = False,
) -> dict[str, Any]:
    cleaned_query = str(query or "").strip()
    if not cleaned_query:
        raise SymbolSearchError("query must be a non-empty string")

    cleaned_kind = str(kind or "").strip().lower() or None
    if cleaned_kind is not None and cleaned_kind not in _SUPPORTED_KINDS:
        allowed = ", ".join(sorted(_SUPPORTED_KINDS))
        raise SymbolSearchError(f"Unsupported kind: {kind!r}. Expected one of: {allowed}")

    base = _resolve_under_root(root, root_path)
    if not base.exists():
        raise SymbolSearchError(f"Not found: {root_path}")

    cleaned_globs = [str(pattern) for pattern in (globs or []) if str(pattern).strip()]
    safe_max_results = max(1, min(int(max_results), _MAX_RESULTS))
    candidate_files = _iter_candidate_files(
        root=root,
        base=base,
        globs=cleaned_globs or None,
    )

    matches: list[dict[str, Any]] = []
    matched_entries: list[_SymbolEntry] = []
    source_lines_by_path: dict[str, list[str]] = {}
    notes: list[str] = []
    truncated = False
    parsed_files = 0
    examined_files = 0
    heuristic_files = 0
    unsupported_files = 0
    large_or_unreadable_files = 0
    unparsable_files = 0
    parsed_backends: set[str] = set()
    skipped_unsupported: list[str] = []
    skipped_large: list[str] = []
    skipped_unparsable: list[str] = []

    for path in candidate_files:
        examined_files += 1
        rel_path = _rel_path(root, path)
        backend = symbol_search_backend_for_path(rel_path)
        if backend is None:
            unsupported_files += 1
            if len(skipped_unsupported) < _MAX_NOTE_PATHS:
                skipped_unsupported.append(rel_path)
            continue

        try:
            size = path.stat().st_size
        except OSError:
            large_or_unreadable_files += 1
            if len(skipped_large) < _MAX_NOTE_PATHS:
                skipped_large.append(rel_path)
            continue
        if size > _MAX_FILE_BYTES:
            large_or_unreadable_files += 1
            if len(skipped_large) < _MAX_NOTE_PATHS:
                skipped_large.append(rel_path)
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            large_or_unreadable_files += 1
            if len(skipped_large) < _MAX_NOTE_PATHS:
                skipped_large.append(rel_path)
            continue
        source_lines = text.splitlines()
        if include_snippet or include_references:
            source_lines_by_path[rel_path] = source_lines

        if backend == "python_ast":
            try:
                symbols = _extract_python_symbols(root=root, path=path, text=text)
            except SyntaxError:
                unparsable_files += 1
                if len(skipped_unparsable) < _MAX_NOTE_PATHS:
                    skipped_unparsable.append(rel_path)
                continue
        elif backend == "js_ts_heuristic":
            heuristic_files += 1
            symbols = _extract_js_ts_symbols(root=root, path=path, text=text)
        else:
            heuristic_files += 1
            symbols = _extract_java_symbols(root=root, path=path, text=text)

        parsed_files += 1
        parsed_backends.add(backend)
        for entry in symbols:
            if cleaned_kind is not None and entry.kind != cleaned_kind:
                continue
            if not _matches_query(entry, query=cleaned_query, exact=exact):
                continue
            match = {
                "path": entry.path,
                "line": entry.line,
                "kind": entry.kind,
                "name": entry.name,
                "signature": entry.signature,
            }
            if include_details:
                match["end_line"] = _entry_end_line(entry)
                parent = _entry_parent(entry)
                if parent:
                    match["parent"] = parent
            if include_snippet:
                snippet = _symbol_snippet(
                    source_lines=source_lines,
                    start_line=entry.line,
                    end_line=_entry_end_line(entry),
                )
                if snippet is not None:
                    match["snippet"] = snippet
            matches.append(match)
            matched_entries.append(entry)
            if len(matches) > safe_max_results:
                truncated = True
                break
        if truncated:
            break

    if skipped_unsupported:
        paths = ", ".join(skipped_unsupported)
        _add_note(
            notes,
            f"Skipped unsupported file(s): {paths}",
        )
    if skipped_large:
        paths = ", ".join(skipped_large)
        _add_note(notes, f"Skipped large or unreadable source file(s): {paths}")
    if skipped_unparsable:
        paths = ", ".join(skipped_unparsable)
        _add_note(notes, f"Skipped unparsable source file(s): {paths}")
    if heuristic_files:
        _add_note(
            notes,
            "JavaScript/TypeScript and Java symbol extraction is heuristic, not a complete "
            "parser; valid declarations can be missed. Use search_rg for lexical fallback.",
        )
    if examined_files < len(candidate_files):
        _add_note(
            notes, "Result limit stopped scanning before all scoped candidate files were examined."
        )
    if skipped_unsupported or skipped_large or skipped_unparsable:
        _add_note(
            notes,
            "Skipped paths are samples; coverage counts include all examined files. "
            "No symbol match does not prove absence; use search_rg for lexical fallback.",
        )
    if not candidate_files:
        _add_note(notes, "No files matched the requested scope.")
    elif parsed_files == 0 and not skipped_unsupported:
        _add_note(notes, "No supported source files matched the requested scope.")

    if parsed_backends == {"python_ast"}:
        backend_name = "python_ast"
    elif parsed_backends == {"js_ts_heuristic"}:
        backend_name = "js_ts_heuristic"
    elif parsed_backends == {"java_heuristic"}:
        backend_name = "java_heuristic"
    elif parsed_backends:
        backend_name = "mixed_static"
    else:
        backend_name = "none"

    root_abs = root.resolve()
    scope_display = "."
    try:
        rel_base = Path(relative_path(root_abs, base))
    except ValueError:
        scope_display = root_path
    else:
        if os.fspath(rel_base):
            scope_display = os.fspath(rel_base).replace("\\", "/")

    if include_references and matches:
        for match, entry in zip(matches, matched_entries, strict=False):
            references = _reference_hints_for_entry(
                entry=entry,
                source_lines_by_path=source_lines_by_path,
            )
            if references:
                match["references"] = references

    return {
        "query": cleaned_query,
        "kind": cleaned_kind,
        "root_path": scope_display,
        "globs": cleaned_globs or None,
        "exact": bool(exact),
        "include_details": bool(include_details),
        "include_snippet": bool(include_snippet),
        "include_references": bool(include_references),
        "matches": matches[:safe_max_results],
        "truncated": truncated,
        "notes": notes,
        "backend": backend_name,
        "parsed_files": parsed_files,
        "coverage": {
            "candidate_files": len(candidate_files),
            "examined_files": examined_files,
            "heuristic_files": heuristic_files,
            "unsupported_files": unsupported_files,
            "large_or_unreadable_files": large_or_unreadable_files,
            "unparsable_files": unparsable_files,
            "unexamined_files": len(candidate_files) - examined_files,
        },
    }
