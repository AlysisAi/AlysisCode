"""Ground path interpretation in user messages, independently of permissions.

This is requirement interpretation, never permission or completion evidence.
Names and directory components must be verbatim user text. The host composes
paths; the model cannot enroll an invented absolute path or certify its output.
"""

from __future__ import annotations

import math
import re
import unicodedata
from itertools import islice
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from jsonschema import Draft202012Validator

from ..branding import env_get
from .acceptance_contract import (
    _RESOURCE_LOCATOR_RE,
    AcceptancePathKind,
    AcceptancePathRef,
    AcceptancePathRole,
    AcceptanceThreshold,
    DeclaredRequirements,
    _clean_path_token,
    _iter_clauses,
    _path_source_literal,
    _resolve_acceptance_path,
)

TOOL_NAME = "task_artifacts"
TOOL_DESCRIPTION = (
    "Call this only in a turn whose host instructions ask for task_artifacts or say it is "
    "optional; in any other turn the host has read the request's files itself and the call "
    "is not needed. "
    "Declare exact user-named files and directories once per user request, before working on them. "
    "After a successful declaration, continue the task without declaring it again. "
    "When the task names files to create/change, inputs to use, or files to preserve, "
    "call before actions or verification, after any read-only context gathering such "
    "as memory reads. Include every named local path, omitting "
    "remote resource URLs. Distinguish a containing directory (location_context) "
    "from an output within it. "
    "Use the whole request to determine roles, not verbs near filenames. "
    "Detected mentions are hints, not requirements: declare the real paths, and "
    "leave out or mark as non_path anything that is not a local path. "
    "Each name and optional directory must occur verbatim together in the current "
    "or an earlier user message in this chat. Earlier names identify files, not renewed "
    "authorization; interpret their role from the current request. "
    "The host derives requirements directly from that request; do not copy supporting prose. "
    "Carry a stated containing folder across references such as 'from that folder'; "
    "do not silently relocate "
    "inputs to the workspace root. Use preservation_target for inputs the user says "
    "to preserve unchanged. When name is a complete path, directory must be empty; "
    "do not also supply its parent. For a name relative to the workspace, directory "
    "is also empty. Use directory only to combine a separately stated folder and name. Do not "
    "declare both an unqualified workspace path and a containing-folder interpretation "
    "of the same file mention; choose its intended location before working. "
    "For a bare new deliverable, location task_output selects the host's fixed default "
    "output folder; location workspace preserves an intended project/root destination. "
    "Existing inputs use workspace. Explicit paths and directories retain their locations. "
    "Relative paths use path_base active_workdir like file tools; workspace_root is for "
    "an explicitly intended repository root. Use the returned normalized path for both "
    "writing and checking. Do not "
    "invent missing names or resolve ambiguous locations by guessing. This only "
    "records the requested result: it grants no access, runs no app actions, and "
    "never proves completion. Actual output and content still need verification. "
    "The same call records what else the request makes checkable, each only when "
    "the request states it, in whatever language it is written: commands for the "
    "command lines the user wrote and wants run successfully; format on an output "
    "whose format the request states; only_declared_outputs when no other file may "
    "change; keep_running (with port when given) when a server or process must "
    "still run after you finish, which needs shell_service_start; thresholds for a "
    "numeric target on a measured result. The host checks these against what "
    "actually happened before the turn ends. Leave out any the request does not state."
)
_FORMATS = ("json", "yaml", "csv", "xml", "html", "markdown")
_OPERATORS = (">=", "<=", ">", "<")
TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "paths": {
            "type": "array",
            "maxItems": 40,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "description": "Exact name or path in the request."},
                    "directory": {
                        "type": "string",
                        "description": "Separately stated containing folder; empty when name is already a complete path.",
                    },
                    "role": {
                        "type": "string",
                        "enum": [role.value for role in AcceptancePathRole] + ["non_path"],
                    },
                    "evidence_quote": {
                        "type": "string",
                        "description": "Optional legacy excerpt; ignored for grounding and requirements. Prefer omitting it.",
                    },
                    "location": {
                        "type": "string",
                        "enum": ["workspace", "task_output"],
                        "description": "task_output selects the host default for a bare new output; workspace preserves a requested project/root location or existing input.",
                    },
                    "path_base": {
                        "type": "string",
                        "enum": ["active_workdir", "workspace_root"],
                        "description": "Base for relative paths, matching file tools. Choose active_workdir for the selected project or workspace_root for the repository root.",
                    },
                    "format": {
                        "type": "string",
                        "enum": list(_FORMATS),
                        "description": "Only for a required_output whose format the request states.",
                    },
                },
                "required": ["name", "directory", "role"],
            },
        },
        "commands": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string"},
            "description": "Command lines copied exactly from the request that the user wants run and succeeding.",
        },
        "only_declared_outputs": {
            "type": "boolean",
            "description": "True when the request allows changing no file other than the declared outputs.",
        },
        "keep_running": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"port": {"type": "integer", "minimum": 1, "maximum": 65535}},
            "description": "A server or process the request requires to still be running after the turn; port when the request gives one.",
        },
        "thresholds": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "metric": {"type": "string"},
                    "operator": {"type": "string", "enum": list(_OPERATORS)},
                    "value": {"type": "number"},
                    "unit": {"type": "string"},
                },
                "required": ["metric", "operator", "value"],
            },
            "description": "Numeric targets the request states for a measured result.",
        },
    },
    # A declaration of requirements alone names no path.
    "required": [],
}


def _literal(value: Any, *, label: str, allow_empty: bool = False, multiline: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 8192:
        raise ValueError(f"{label} must be bounded literal text")
    if not value and not allow_empty:
        raise ValueError(f"{label} is required")
    if any(ord(char) < 32 and not (multiline and char in "\n\r\t") for char in value):
        raise ValueError(f"{label} cannot contain control characters")
    return value


def _compose(name: str, directory: str) -> str:
    # Select lexical path semantics from the literal, not this host's OS.
    windows = bool(
        PureWindowsPath(name).drive or PureWindowsPath(directory).drive or "\\" in name + directory
    )
    path_type = PureWindowsPath if windows else PurePosixPath
    leaf = path_type(name)
    if (leaf.drive and not leaf.is_absolute()) or (
        path_type(directory).drive and not path_type(directory).is_absolute()
    ):
        raise ValueError("Drive-relative locations do not identify an exact directory")
    if ".." in leaf.parts or ".." in path_type(directory).parts:
        raise ValueError("Parent traversal requires an unambiguous explicit path")
    if directory and (leaf.is_absolute() or leaf.anchor or leaf.drive):
        raise ValueError("An absolute or drive-qualified name cannot also have a directory")
    return (path_type(directory) / leaf).as_posix() if directory else leaf.as_posix()


def _closes_a_mention(char: str) -> bool:
    """A closing quote or bracket in any script."""
    return char in "\"'`>" or unicodedata.category(char) in {"Pe", "Pf", "Pi"}


def _mention_ends(text: str, index: int) -> bool:
    """Whether nothing more of a path follows at ``text[index]``.

    True at the end of the text, at white space and at a closing quote or
    bracket. Punctuation ends a mention when it ends the sentence or clause:
    in ASCII, where the same characters also occur in paths, only when an end
    follows it; in other scripts, which write no space after it, always.
    """
    if index >= len(text):
        return True
    char = text[index]
    if char in "/\\":
        return False
    if char.isspace() or _closes_a_mention(char):
        return True
    if unicodedata.category(char) != "Po":
        return False
    if not char.isascii():
        return True
    following = text[index + 1 : index + 2]
    return not following or following.isspace() or _closes_a_mention(following)


def _complete_literal(text: str, value: str) -> bool:
    """Do not accept a basename/component sliced out of a longer written path."""
    start = text.find(value)
    while start >= 0:
        end = start + len(value)
        before = text[start - 1] if start else ""
        after = text[end] if end < len(text) else ""
        left = before.isalnum() or before in "_./\\:-" if before else False
        right = after.isalnum() or after in "_/\\" if after else False
        if after in ".:-" and end + 1 < len(text) and not text[end + 1].isspace():
            # Punctuation continues a path only when something follows it:
            # "calc.py: add()" and "notes.txt - draft" both end at the name.
            right = text[end + 1] not in "\"'`)]}"
        if after == "/" and value[-1:] not in ("", "/", "\\"):
            # A folder written with its separator, "pkg/", is the folder pkg:
            # the separator continues a path only when a path goes on after it.
            right = not _mention_ends(text, end + 1)
        if not left and not right:
            return True
        start = text.find(value, start + 1)
    return False


def _joined_literal(name: str, directory: str) -> str:
    separator = "\\" if "\\" in name + directory else "/"
    return directory + ("" if directory.endswith(("/", "\\")) else separator) + name


def _components_in(text: str, name: str, directory: str) -> bool:
    joined = _joined_literal(name, directory)
    return (bool(directory) and _complete_literal(text, joined)) or (
        _complete_literal(text, name) and (not directory or _complete_literal(text, directory))
    )


def _workspace_spellings(value: str, root: Path) -> tuple[str, ...]:
    """An absolute spelling may only project the trusted workspace prefix.

    This is lexical identity, not filesystem access or basename extraction.
    The resulting complete relative spelling must still occur in user text.
    """
    path_type = PureWindowsPath if PureWindowsPath(str(root)).drive else PurePosixPath
    path, workspace = path_type(value), path_type(str(root))
    if not path.is_absolute() or not workspace.is_absolute() or ".." in path.parts:
        return (value,)
    try:
        relative = path.relative_to(workspace)
    except ValueError:
        return (value,)
    if not relative.parts:
        return (value,)
    return tuple(dict.fromkeys((value, str(relative), relative.as_posix())))


def _written_form_hint(
    spellings: tuple[str, ...],
    directory: str,
    prior_refs: list[AcceptancePathRef],
    texts: tuple[str, ...],
) -> str:
    """Tell a rejected declaration how the user wrote the path.

    Guidance only: nothing is accepted here, and every quoted part is either
    user text the binder would accept or a piece of the model's own argument.
    """
    declared = {
        (_joined_literal(name, directory) if directory else name).replace("\\", "/").lstrip("/")
        for name in spellings
    }
    complete = [
        literal
        for prior in prior_refs
        if (literal := _path_source_literal(prior.raw_text))
        and any(literal.replace("\\", "/").endswith("/" + item) for item in declared if item)
        and any(_components_in(text, literal, "") for text in texts)
    ]
    if complete:
        written = " or ".join(repr(item) for item in list(dict.fromkeys(complete))[:3])
        return f" The request writes it as {written}; declare that as name with an empty directory."
    if not directory:
        for name in spellings:
            for separator in ("/", "\\"):
                parent, found, leaf = name.rpartition(separator)
                if (
                    found
                    and parent
                    and leaf
                    and any(_components_in(text, leaf, parent) for text in texts)
                ):
                    return (
                        f" The request writes {parent!r} and {leaf!r} separately; "
                        f"declare name {leaf!r} with directory {parent!r}."
                    )
    return ""


def earlier_path_refs(root: Path, earlier_instructions: tuple[str, ...]) -> list[AcceptancePathRef]:
    """Paths written in earlier user messages, each message read on its own."""
    from .acceptance_contract import _extract_path_refs

    refs: list[AcceptancePathRef] = []
    for text in earlier_instructions:
        refs.extend(_extract_path_refs(root=root, texts=[text]))
    return refs


def named_workspace_paths(
    path_refs: list[AcceptancePathRef], *, working_directory: str = "."
) -> set[str]:
    """Workspace paths the user wrote, spelled as file effects are recorded.

    In a nested project a relative name may be based at the workspace root or
    at the active folder; both spellings are kept.
    """
    named: set[str] = set()
    for ref in path_refs:
        relative = ref.workspace_relative_path
        if not relative:
            continue
        named.add(Path(relative).as_posix())
        if working_directory != "." and ref.path_kind == AcceptancePathKind.WORKSPACE_RELATIVE:
            named.add((PurePosixPath(working_directory) / relative).as_posix())
    return named


def _written_as_a_path(ref: AcceptancePathRef) -> bool:
    """Whether the user marked the mention as a path: a folder part, or quotes.

    A bare word with a dot in it ("Node.js", "e.g.", "v1.2", "user.name",
    "notes.txt") may or may not be a file. How the mention is written is the
    same in every language; what it means is not for the host to guess.
    """
    raw = ref.raw_text
    if raw[:1] in "\"'`" or "/" in raw or "\\" in raw:
        return True
    at = ref.clause.find(raw)
    if at < 0:
        return False
    before, after = ref.clause[at - 1 : at], ref.clause[at + len(raw) : at + len(raw) + 1]
    return (before != "" and before in "\"'`") or (after != "" and after in "\"'`")


def written_workspace_paths(path_refs: list[AcceptancePathRef]) -> list[str]:
    """Workspace paths the user wrote as paths (with a folder, or in quotes), in order."""
    paths: list[str] = []
    for ref in path_refs:
        relative = ref.workspace_relative_path
        if relative and _written_as_a_path(ref) and relative not in paths:
            paths.append(Path(relative).as_posix())
    return paths


def request_needs_path_interpretation(
    path_refs: list[AcceptancePathRef],
    *,
    root: Path,
    working_directory: str = ".",
    earlier_refs: list[AcceptancePathRef] | tuple[AcceptancePathRef, ...] = (),
) -> bool:
    """Whether the request's file mentions leave the model something to decide.

    The declaration costs a model round trip before any work. Whether to ask
    for it is decided on facts the host can check and on how a mention is
    written, never on how the request is worded, so the same request gets the
    same answer in every language:

    * the active project is a subfolder, where a relative name has two bases;
    * a mention, or a path named earlier in the chat, lies outside the
      workspace or does not resolve to a location in it;
    * a mention the user wrote as a path, with a folder or in quotes, does not
      exist yet: a file to create, whose place and role the model declares.

    Otherwise the host needs nothing from the model. A request that names
    existing workspace files, or none, goes straight to work, and so does one
    whose only new names are bare words with a dot, which are hints and not
    reliably files. The host guesses no role and makes no mention a
    requirement.
    """
    inside = {
        AcceptancePathKind.WORKSPACE_RELATIVE,
        AcceptancePathKind.ABSOLUTE_WITHIN_WORKSPACE,
    }
    if working_directory != ".":
        return True
    for ref in path_refs:
        relative = ref.workspace_relative_path
        if ref.path_kind not in inside or not relative:
            return True
        if not _written_as_a_path(ref):
            continue
        try:
            if not (root / relative).exists():
                return True
        except OSError:
            return True
    return any(ref.path_kind not in inside for ref in earlier_refs)


def retained_user_instructions(events: list[dict[str, Any]]) -> tuple[str, ...]:
    """Read original user text, never model summaries, tools or copied evidence.

    The current user message has already been recorded by the turn controller.
    Its authoritative instruction is supplied separately to the binder.
    """
    from .approval_context import message_text
    from .prompt_context import extract_user_visible_instruction

    messages = [event.get("payload") for event in events if event.get("type") == "user_message"]
    return tuple(
        text
        for payload in messages[:-1]
        if (text := extract_user_visible_instruction(message_text(payload)))
    )


def _request_code_spans(instruction: str) -> set[str]:
    """What the user wrote as code: inline backtick spans and lines of fenced blocks."""
    text = str(instruction or "")
    spans: set[str] = set()
    fenced = re.compile(r"```[^\n`]*\n(.*?)```", re.S)
    for block in fenced.findall(text):
        spans.update(" ".join(line.split()) for line in block.splitlines() if line.strip())
    for span in re.findall(r"`([^`\n]+)`", fenced.sub(" ", text)):
        spans.add(" ".join(span.split()))
    return spans


def _number_is_written(value: float, instruction: str) -> bool:
    """Whether the request writes this number, so a declared target is the user's own."""
    numbers = {float(found) for found in re.findall(r"\d+(?:\.\d+)?", str(instruction or ""))}
    return value in numbers or abs(value) in numbers


def bind_declared_requirements(
    *,
    instruction: str,
    arguments: dict[str, Any],
    refs: list[AcceptancePathRef],
    entry_refs: list[AcceptancePathRef | None] | None = None,
) -> DeclaredRequirements:
    """Validate what the declaration states beyond paths. Reads no file.

    The model says what the request requires; the host grounds each statement
    in what the user wrote. A command is one the request writes as code, whole.
    A port or a numeric target is a number the request writes. A format belongs
    to the declared output of its own entry.
    """
    commands: list[str] = []
    written = _request_code_spans(instruction)
    for raw in arguments.get("commands") or ():
        command = " ".join(_literal(raw, label="command").strip().strip("`").split())
        if not command:
            raise ValueError("A command cannot be empty")
        if command not in written:
            raise ValueError(
                f"{command!r} is not written in the request as code; declare only whole "
                "command lines the user wrote in backticks, copied exactly"
            )
        if command not in commands:
            commands.append(command)
    outputs = [ref for ref in refs if ref.role == AcceptancePathRole.REQUIRED_OUTPUT]
    formats: list[tuple[str, str]] = []
    entries = arguments.get("paths") or ()
    for index, entry in enumerate(entries):
        fmt = entry.get("format") if isinstance(entry, dict) else None
        if fmt is None:
            continue
        ref = entry_refs[index] if entry_refs is not None and index < len(entry_refs) else None
        if entry_refs is None:
            named = [item for item in outputs if item.raw_text == entry.get("name")]
            ref = named[0] if len(named) == 1 else None
        if (
            fmt not in _FORMATS
            or entry.get("role") != AcceptancePathRole.REQUIRED_OUTPUT.value
            or ref is None
            or ref.role != AcceptancePathRole.REQUIRED_OUTPUT
        ):
            raise ValueError("format belongs to one required_output and names a supported format")
        formats.append((ref.display_path, fmt))
    only = arguments.get("only_declared_outputs", False)
    if not isinstance(only, bool):
        raise ValueError("only_declared_outputs is true or false")
    if only and not outputs:
        raise ValueError("only_declared_outputs needs at least one required_output")
    keep = arguments.get("keep_running")
    ports: tuple[int, ...] = ()
    if keep is not None:
        if not isinstance(keep, dict) or set(keep) - {"port"}:
            raise ValueError("keep_running takes an optional port")
        port = keep.get("port")
        if port is not None:
            if type(port) is not int or not 0 < port <= 65535:
                raise ValueError("port is a number from 1 to 65535")
            if not _number_is_written(float(port), instruction):
                raise ValueError(f"port {port} is not written in the request")
            ports = (port,)
    thresholds: list[AcceptanceThreshold] = []
    for item in arguments.get("thresholds") or ():
        if (
            not isinstance(item, dict)
            or set(item) - {"metric", "operator", "value", "unit"}
            or item.get("operator") not in _OPERATORS
            or isinstance(item.get("value"), bool)
            or not isinstance(item.get("value"), int | float)
            or not math.isfinite(item["value"])
        ):
            raise ValueError("Each threshold needs metric, operator and a finite numeric value")
        metric = _literal(item.get("metric"), label="metric").strip().casefold()
        unit = _literal(item.get("unit", ""), label="unit", allow_empty=True).strip()
        if not metric or len(metric) > 80 or len(unit) > 16:
            raise ValueError("Each threshold needs a short metric name")
        if not _number_is_written(float(item["value"]), instruction):
            raise ValueError(f"The target {item['value']} is not written in the request")
        thresholds.append(
            AcceptanceThreshold(
                metric=metric, operator=item["operator"], value=float(item["value"]), unit=unit
            )
        )
    return DeclaredRequirements(
        commands=tuple(commands),
        formats=tuple(formats),
        only_declared_outputs=only,
        keep_running=keep is not None,
        ports=ports,
        thresholds=tuple(thresholds),
    )


def path_binding_identity(refs: list[AcceptancePathRef]) -> frozenset[tuple[str, ...]]:
    """Compare effective paths, roles and host-derived requirements, not tool syntax."""
    return frozenset(
        (ref.path_kind.value, ref.absolute_path, ref.role.value, ref.clause) for ref in refs
    )


def bind_path_interpretation(
    *,
    root: Path,
    instruction: str,
    arguments: dict[str, Any],
    prior_refs: list[AcceptancePathRef],
    earlier_instructions: tuple[str, ...] = (),
    output_directory: str | None = None,
    working_directory: str = ".",
    entry_refs: list[AcceptancePathRef | None] | None = None,
) -> list[AcceptancePathRef]:
    """Validate atomic, complete source bindings without reading the filesystem.

    `entry_refs`, when given, receives the path each entry became, in entry
    order, with None for an entry that is not a path.
    """
    working_directory = validated_path_base(root, working_directory)
    parameters = binding_parameters(output_directory, working_directory)
    problems = list(islice(Draft202012Validator(parameters).iter_errors(arguments), 5))
    if problems:
        raise ValueError(
            "; ".join(
                f"{'.'.join(map(str, problem.absolute_path)) or 'arguments'}: {problem.message[:300]}"
                for problem in problems
            )
        )
    entries = arguments.get("paths", [])
    others = set(arguments) - {"paths"}
    if (
        others - {"commands", "only_declared_outputs", "keep_running", "thresholds"}
        or not isinstance(entries, list)
        or len(entries) > 40
    ):
        raise ValueError("Provide between one and forty path bindings")
    if not entries and not (
        arguments.get("commands")
        or arguments.get("keep_running") is not None
        or arguments.get("thresholds")
    ):
        raise ValueError("Nothing was declared: give a path, a command or another requirement")
    refs: list[AcceptancePathRef] = []
    identities: dict[str, set[AcceptancePathRole]] = {}
    locations: dict[tuple[str, AcceptancePathRole, str], list[tuple[str, str]]] = {}
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not {"name", "directory", "role"} <= set(entry)
            or set(entry)
            - {"name", "directory", "role", "evidence_quote", "location", "path_base", "format"}
        ):
            raise ValueError("Each path needs name, directory, and role")
        name = _literal(entry["name"], label="name")
        directory = _literal(entry["directory"], label="directory", allow_empty=True)
        location = entry.get("location", "workspace")
        path_base = entry.get("path_base", "active_workdir")
        literal_root = (
            root / working_directory
            if location == "workspace" and path_base == "active_workdir"
            else root
        )
        if _RESOURCE_LOCATOR_RE.fullmatch(
            _clean_path_token(name)
        ) or _RESOURCE_LOCATOR_RE.fullmatch(_clean_path_token(directory)):
            raise ValueError("Remote resource URLs are not local artifact paths; omit them.")
        if "evidence_quote" in entry:
            _literal(
                entry["evidence_quote"], label="evidence_quote", multiline=True, allow_empty=True
            )
        # A model may expand a user-relative path with the host-owned workspace
        # root. Recover only the same complete user spelling, never an arbitrary
        # prefix/basename or a location outside that root. Non-path text is literal.
        if entry["role"] != "non_path":
            _compose(name, directory)  # Reject invalid/traversing components before normalization.
            for text in (instruction, *earlier_instructions):
                grounded = next(
                    (
                        (candidate_name, candidate_directory)
                        for candidate_name in _workspace_spellings(name, literal_root)
                        for candidate_directory in _workspace_spellings(directory, literal_root)
                        if _components_in(text, candidate_name, candidate_directory)
                    ),
                    None,
                )
                if grounded is not None:
                    name, directory = grounded
                    break
        # A model may split an explicitly written path into its directory and
        # leaf. Admit that representation only when joining the exact components
        # reproduces a complete literal in the trusted user request. A model's
        # copied excerpt is redundant and cannot add, remove or scope requirements.
        joined = _joined_literal(name, directory)
        current_literal = _components_in(instruction, name, directory)
        if not current_literal and not any(
            _components_in(text, name, directory) for text in earlier_instructions
        ):
            raise ValueError(
                f"{name!r}: Path components must be verbatim current "
                "user text or occur together in an earlier user message. "
                "Use the complete written path, not a basename sliced from it."
                + _written_form_hint(
                    _workspace_spellings(name, literal_root),
                    directory,
                    prior_refs,
                    (instruction, *earlier_instructions),
                )
            )
        joined_is_literal = bool(directory) and _complete_literal(instruction, joined)
        if entry["role"] == "non_path":
            if directory:
                raise ValueError(f"{name!r}: non_path must have an empty directory")
            if entry_refs is not None:
                entry_refs.append(None)
            continue
        try:
            role = AcceptancePathRole(entry["role"])
        except (ValueError, TypeError) as exc:
            raise ValueError("Unknown path role") from exc
        path = _compose(name, directory)
        if _clean_path_token(path) != path:
            raise ValueError(
                "Path spelling must remain exact; do not silently trim punctuation or whitespace"
            )
        # Recover the surrounding request passage on the host, as the source the
        # declaration points at. A containing directory distinguishes repeated
        # basenames when it occurs in context.
        context_literal = joined if joined_is_literal else name
        passages = [
            passage
            for passage in _iter_clauses(
                [instruction], protected_literals=(context_literal, directory)
            )
            if _complete_literal(passage, context_literal)
        ]
        scoped_passages = [
            passage for passage in passages if directory and _complete_literal(passage, directory)
        ]
        contexts = [
            clause
            for clause in _iter_clauses(
                scoped_passages or passages, protected_literals=(context_literal, directory)
            )
            if _complete_literal(clause, context_literal)
        ]
        scoped = [
            clause for clause in contexts if directory and _complete_literal(clause, directory)
        ]
        # Earlier messages establish only spelling. Do not resurrect an old
        # deliverable/format obligation or treat old permission as current access.
        # Actual actions still pass through the independent approval boundary.
        context = "\n".join(scoped or contexts) if current_literal else instruction
        if not context:
            raise ValueError("The path needs an unambiguous complete request passage")
        # Ground names and requirements before composing any host-owned prefix.
        # The same normalized identity is then used by file effects and checks.
        if location == "task_output":
            if (
                role != AcceptancePathRole.REQUIRED_OUTPUT
                or directory
                or PurePosixPath(name).name != name
                or PureWindowsPath(name).name != name
                or not output_directory
            ):
                raise ValueError(
                    "The task output location needs a bare new output name and host default"
                )
            path = _compose(name, validated_output_directory(root, output_directory))
        elif (
            path_base == "active_workdir"
            and working_directory != "."
            and not (PurePosixPath(path).is_absolute() or PureWindowsPath(path).drive)
        ):
            path = (
                PurePosixPath(working_directory) / PurePosixPath(path.replace("\\", "/"))
            ).as_posix()
        ref = _resolve_acceptance_path(root=root, raw_text=path, role=role, clause=context)
        if ref is None or ref.path_kind == AcceptancePathKind.UNRESOLVED:
            raise ValueError("The supplied components do not resolve to an exact path")
        # Keep original spelling/provenance: normalization is only path identity.
        ref = AcceptancePathRef(
            raw_text=name,
            display_path=ref.display_path,
            path_kind=ref.path_kind,
            role=role,
            workspace_relative_path=ref.workspace_relative_path,
            absolute_path=ref.absolute_path,
            clause=context,
        )
        identity = (
            ref.absolute_path.casefold()
            if PureWindowsPath(ref.absolute_path).drive
            else ref.absolute_path
        )
        roles = identities.setdefault(identity, set())
        roles.add(role)
        if (
            AcceptancePathRole.REQUIRED_OUTPUT in roles
            and AcceptancePathRole.PRESERVATION_TARGET in roles
        ):
            raise ValueError("A path cannot be both a changed output and preserved unchanged")
        if ref not in refs:
            refs.append(ref)
        if entry_refs is not None:
            entry_refs.append(ref)
        locations.setdefault((name, role, context), []).append((directory, ref.absolute_path))

    for (name, _role, _context), choices in locations.items():
        if (
            len({path for _, path in choices}) > 1
            and sum(not directory for directory, _ in choices) > 1
        ):
            raise ValueError(f"{name!r}: Choose one default location for the same file mention")
        for directory, absolute_path in choices:
            if not directory and any(
                other_directory and other_path != absolute_path
                for other_directory, other_path in choices
            ):
                raise ValueError(
                    f"{name!r}: The same file mention has both a workspace-default and "
                    "a containing-folder interpretation. Keep its intended location only; "
                    "use separate explicit directories for distinct files."
                )

    # Detection is a heuristic that reads the request's wording. The model's
    # declaration is the interpretation: a detection it did not name is not a
    # requirement, whether it was a real path, a version number or plain text.
    return refs


def declared_changed_output_paths(
    state: Any, *, relative_to: str = ".", root: Path | None = None
) -> list[str]:
    """Declare current required outputs; the desktop independently proves saved bytes.

    This exports no contents and grants no read access. Model prose, untouched
    inputs, prior outputs and external paths cannot enroll a reply attachment.

    The host guesses no role from the wording of a request. An output is a
    file this turn changed that the model declared as one, that the user named
    and the model did not declare, or that was saved in the host's default
    folder for new deliverables. With `root`, the last two must still be files.
    """
    contract = getattr(state, "acceptance_contract", None)
    if contract is None:
        return []
    touched = {Path(path).as_posix() for path in getattr(state, "turn_touched_repo_paths", ())}
    prefix = PurePosixPath(relative_to.replace("\\", "/"))
    if prefix.is_absolute() or ".." in prefix.parts or ":" in relative_to:
        raise ValueError("Invalid output workspace prefix")
    candidates: set[str] = set()
    for ref in contract.path_refs:
        path = ref.workspace_relative_path
        if (
            ref.role != AcceptancePathRole.REQUIRED_OUTPUT
            or ref.path_kind
            not in {
                AcceptancePathKind.WORKSPACE_RELATIVE,
                AcceptancePathKind.ABSOLUTE_WITHIN_WORKSPACE,
            }
            or not isinstance(path, str)
        ):
            continue
        candidates.add(Path(path).as_posix())
    observed = {Path(path).as_posix() for path in getattr(state, "turn_host_read_paths", ())}
    folder = PurePosixPath(str(getattr(state, "turn_host_output_directory", "") or ""))
    if folder.parts and not folder.is_absolute() and ".." not in folder.parts:
        observed.update(path for path in touched if folder in PurePosixPath(path).parents)
    observed &= touched
    if root is not None:
        # A folder, or a file the turn moved away or deleted, is not an output.
        observed = {path for path in observed if _is_file(root / path)}
    candidates |= observed
    paths: set[str] = set()
    for path in candidates:
        if path not in touched:
            continue
        try:
            path = PurePosixPath(path).relative_to(prefix).as_posix()
        except ValueError:
            continue
        parts = PurePosixPath(path).parts
        if not parts or any(part in {".", ".."} or part.startswith((".", "~$")) for part in parts):
            continue
        if PurePosixPath(path).is_absolute() or ":" in path or "\\" in path or len(path) > 2000:
            continue
        paths.add(path)
    return sorted(paths)[:40]


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def validated_output_directory(root: Path, value: str) -> str:
    """Validate a launcher's default, never an access grant or a completion receipt."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or any(ord(char) < 32 for char in value)
        or PureWindowsPath(value).drive
        or value.startswith(("/", "\\"))
        or ".." in PurePosixPath(value.replace("\\", "/")).parts
        or ":" in value
    ):
        raise ValueError("Invalid host task output directory")
    canonical_root = root.resolve()
    target = canonical_root / value
    # Check every existing ancestor without following a link into a different scope.
    for ancestor in (target, *target.parents):
        if ancestor == canonical_root:
            break
        if ancestor.is_symlink() or getattr(ancestor, "is_junction", lambda: False)():
            raise ValueError("Host task output directories cannot contain links")
    if not target.resolve().is_relative_to(canonical_root) or target.resolve() == canonical_root:
        raise ValueError("Host task output directory must be inside the task")
    return value.replace("\\", "/")


def validated_path_base(root: Path, value: str) -> str:
    if value == ".":
        return value
    try:
        return validated_output_directory(root, value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Path base must be inside the workspace without links or traversal"
        ) from exc


def host_output_directory(
    root: Path, *, relative_to: str = ".", session_value: str | None = None
) -> str | None:
    """The host's default task output folder: the session's own (session.create's
    task_output_directory), else ALYSIS_DESKTOP_TASK_OUTPUT_DIRECTORY (hosts that set it at
    launch). Checked against the task the same way either way; a location, never a grant."""
    raw = (
        session_value
        if session_value is not None
        else env_get("ALYSIS_DESKTOP_TASK_OUTPUT_DIRECTORY")
    )
    if raw is None:
        return None
    prefix = validated_path_base(root, relative_to)
    output = validated_output_directory(root, raw)
    return (
        validated_output_directory(root, (PurePosixPath(prefix) / output).as_posix())
        if prefix != "."
        else output
    )


def binding_parameters(
    output_directory: str | None, working_directory: str = "."
) -> dict[str, Any]:
    """Only hosts with a default need an explicit choice between two locations."""
    from copy import deepcopy

    parameters = deepcopy(TOOL_PARAMETERS)
    if output_directory is not None:
        parameters["properties"]["paths"]["items"]["required"].append("location")
    if working_directory != ".":
        parameters["properties"]["paths"]["items"]["required"].append("path_base")
    return parameters
