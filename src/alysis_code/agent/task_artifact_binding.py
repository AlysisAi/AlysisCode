"""Ground path interpretation in user messages, independently of permissions.

This is requirement interpretation, never permission or completion evidence.
Names and directory components must be verbatim user text. The host composes
paths; the model cannot enroll an invented absolute path or certify its output.
"""

from __future__ import annotations

from itertools import islice
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from jsonschema import Draft202012Validator

from ..branding import env_get
from .acceptance_contract import (
    _EXPLICIT_FORMAT_RE,
    _RESOURCE_LOCATOR_RE,
    AcceptancePathKind,
    AcceptancePathRef,
    AcceptancePathRole,
    _clean_path_token,
    _iter_clauses,
    _path_source_literal,
    _resolve_acceptance_path,
)

TOOL_NAME = "task_artifacts"
TOOL_DESCRIPTION = (
    "Declare exact user-named files and directories once per user request, before working on them. "
    "After a successful declaration, continue the task without declaring it again. "
    "When the task names files to create/change, inputs to use, or files to preserve, "
    "call before actions or verification, after any read-only context gathering such "
    "as memory reads. Include every named local path, omitting "
    "remote resource URLs. Distinguish a containing directory (location_context) "
    "from an output within it. "
    "Use the whole request to determine roles, not verbs near filenames. "
    "If validation reports a detected mention that is not a local path (for "
    "example a configuration property or literal content), include that exact "
    "mention with role non_path and an empty directory. "
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
    "never proves completion. Actual output and content still need verification."
)
TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "paths": {
            "type": "array",
            "minItems": 1,
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
                },
                "required": ["name", "directory", "role"],
            },
        },
    },
    "required": ["paths"],
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


def _complete_literal(text: str, value: str) -> bool:
    """Do not accept a basename/component sliced out of a longer written path."""
    start = text.find(value)
    while start >= 0:
        end = start + len(value)
        before = text[start - 1] if start else ""
        after = text[end] if end < len(text) else ""
        left = before.isalnum() or before in "_./\\:-" if before else False
        right = after.isalnum() or after in "_/\\:-" if after else False
        if after == "." and end + 1 < len(text) and not text[end + 1].isspace():
            right = text[end + 1] not in "\"'`)]}"
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
) -> list[AcceptancePathRef]:
    """Validate atomic, complete source bindings without reading the filesystem."""
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
    entries = arguments.get("paths")
    if set(arguments) != {"paths"} or not isinstance(entries, list) or not 1 <= len(entries) <= 40:
        raise ValueError("Provide between one and forty path bindings")
    refs: list[AcceptancePathRef] = []
    source_literals: list[str] = []
    non_paths: set[str] = set()
    identities: dict[str, set[AcceptancePathRole]] = {}
    locations: dict[tuple[str, AcceptancePathRole, str], list[tuple[str, str]]] = {}
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not {"name", "directory", "role"} <= set(entry)
            or set(entry) - {"name", "directory", "role", "evidence_quote", "location", "path_base"}
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
            )
        joined_is_literal = bool(directory) and _complete_literal(instruction, joined)
        if entry["role"] == "non_path":
            if directory:
                raise ValueError(f"{name!r}: non_path must have an empty directory")
            non_paths.add(_clean_path_token(name))
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
        # Recover the surrounding request clauses on the host. A model's optional
        # excerpt cannot omit e.g. "as valid JSON" or add new requirements. A containing
        # directory distinguishes repeated basenames when it occurs in context.
        context_literal = joined if joined_is_literal else name
        passages = [
            passage
            for passage in _iter_clauses(
                [instruction], split_actions=False, protected_literals=(context_literal, directory)
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
        formats = {
            (m.group(1) or m.group(2)).casefold() for m in _EXPLICIT_FORMAT_RE.finditer(context)
        }
        if len(formats) > 1:
            raise ValueError(
                "Repeated path names have ambiguous format requirements; use distinct complete paths"
            )
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
        locations.setdefault((name, role, context), []).append((directory, ref.absolute_path))
        source_literals.extend([context_literal, directory])

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

    # Detection is a heuristic, not an authoritative path classification. Require
    # explicit interpretation of every candidate, including false positives.
    # A suffix such as notes.txt may be covered by the full Planning notes.txt.
    omitted: list[str] = []
    for prior in prior_refs:
        # The recognizer retains prose punctuation in raw_text, but resolves
        # identity with _clean_path_token. Use that same spelling here so a
        # sentence-ending period cannot make an exact binding look omitted.
        raw = _clean_path_token(prior.raw_text)
        if raw not in non_paths and not any(
            raw and raw in literal.replace("\\", "/") for literal in source_literals
        ):
            omitted.append(_path_source_literal(prior.raw_text))
    if omitted:
        raise ValueError(
            f"The interpretation omitted detected mentions: {omitted!r}. "
            "Include actual local paths with their roles. For false detections, use "
            "the exact mention as name, an empty directory, and "
            "role non_path. This classification grants no permissions."
        )
    return refs


def declared_changed_output_paths(state: Any, *, relative_to: str = ".") -> list[str]:
    """Declare current required outputs; the desktop independently proves saved bytes.

    This exports no contents and grants no read access. Model prose, untouched
    inputs, prior outputs and external paths cannot enroll a reply attachment.
    """
    contract = getattr(state, "acceptance_contract", None)
    if contract is None:
        return []
    touched = {Path(path).as_posix() for path in getattr(state, "turn_touched_repo_paths", ())}
    prefix = PurePosixPath(relative_to.replace("\\", "/"))
    if prefix.is_absolute() or ".." in prefix.parts or ":" in relative_to:
        raise ValueError("Invalid output workspace prefix")
    paths: set[str] = set()
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
        path = Path(path).as_posix()
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


def host_output_directory(root: Path, *, relative_to: str = ".") -> str | None:
    raw = env_get("ALYSIS_DESKTOP_TASK_OUTPUT_DIRECTORY")
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
