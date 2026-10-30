"""Deny the reserved native relay, not ordinary shell-based MCP launchers.

This is an interim negative gate, not executable provenance/qualification. The
transport checks the resolved launch path immediately before process creation.
Known shell/batch representations are inspected as data, never evaluated.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import stat
import string
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..runtime_kind import RuntimeKind
from .models import ResolvedMcpServer
from .person_approval import tool_server_approval

REFUSAL = (
    "The native Computer Use MCP relay is unavailable until shared-engine admission is qualified."
)
_NAMES = frozenset({"alysis-computer-use", "alysis-computer-mcp"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "cmd", "powershell", "pwsh"})
MAX_INSPECTION_SCRIPTS = 16
MAX_INSPECTION_BYTES = 65536
MAX_INSPECTION_TOKENS = 4096
MAX_INSPECTION_DEPTH = 4
LEGACY_WINDOWS_TOOLS = frozenset({"computer_state", "computer_action", "computer_wait"})
# Tools whose successful result carries a screen state (snapshot plus elements).
LEGACY_WINDOWS_OBSERVATION_TOOLS = frozenset({"computer_state", "computer_action"})
# Fixed, finite budget for the whole model-facing description of the app's own helper
# tools (about 2,400 characters for computer_action with its host header and note). The
# server text is cut before fencing so the closing fence survives. Every other MCP server
# keeps the ordinary 1,000-character budget.
LEGACY_WINDOWS_DESCRIPTION_MAX_CHARS = 4_000
_CONTROL_KEYS = ("ALYSIS_CU_CONTROL_ADDRESS", "ALYSIS_CU_CONTROL_EPOCH", "ALYSIS_CU_CONTROL_TOKEN")


@dataclass(frozen=True)
class _LegacyWindowsLaunch:
    """Captured availability inputs, NOT authenticated-main/W11 admission.

    The native helper still requires its original live control handshake. The
    foreign-controller and bearer/listener limitations remain open. No Mac
    pathname-socket registration or caller-supplied platform selects this lane.
    """

    command: str
    identity: tuple[int, int, int, int, int]
    control: tuple[tuple[str, str], ...] = field(repr=False)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate launch metadata member")
        result[key] = value
    return result


class LegacyWindowsRefusal(ValueError):
    """A legacy-lane parameter refusal the model receives as a tool result.

    Nothing was sent to the helper; the transport turns this into an MCP
    ``isError`` result carrying the helper's error envelope with
    ``dispatch: not_dispatched`` instead of a transport failure.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def tool_result(self) -> dict[str, object]:
        error = {"code": self.code, "message": self.message, "dispatch": "not_dispatched"}
        return {
            "content": [{"type": "text", "text": json.dumps({"error": error})}],
            "isError": True,
        }


# The paired helper's key grammar (native/computer-use-windows keys.rs and
# action.rs validate_legacy): 1 to 8 names; printable ASCII single characters
# and editing keys sent in order, or shift plus exactly one key. A chord with
# ctrl/alt/cmd/option/win passes through so the helper itself answers with its
# structured high_impact_key_refused; it never dispatches such a chord.
LEGACY_MAX_KEYS = 8
_KEY_MODIFIERS = frozenset(
    {
        "command",
        "cmd",
        "shift",
        "option",
        "alt",
        "control",
        "ctrl",
        "win",
        "windows",
        "meta",
        "super",
    }
)
_HIGH_IMPACT_MODIFIERS = _KEY_MODIFIERS - {"shift"}
_NAMED_KEYS = frozenset(
    {
        "return",
        "enter",
        "tab",
        "space",
        "delete",
        "backspace",
        "forward_delete",
        "escape",
        "esc",
        "home",
        "end",
        "left",
        "up",
        "right",
        "down",
    }
)
_PRINTABLE_KEYS = frozenset(string.ascii_lowercase + string.digits + string.punctuation)
# The helper's drag schema: start at an element or exact point, destination
# required, duration 0..5000 ms. Coordinates are finite and within this bound.
_MAX_ABSOLUTE_COORDINATE = 100_000
_MAX_DRAG_DURATION_MS = 5_000


def _legacy_key_refusal(keys: object) -> LegacyWindowsRefusal | None:
    if not isinstance(keys, list) or not 1 <= len(keys) <= LEGACY_MAX_KEYS:
        return LegacyWindowsRefusal(
            "invalid_keys",
            "Legacy Windows key input takes 1 to 8 key names per action; send longer input in "
            "several key actions, or use type for text.",
        )
    if any(not isinstance(key, str) for key in keys):
        return LegacyWindowsRefusal("invalid_keys", "The requested key chord is unsupported.")
    names = [key.strip().lower() for key in keys]
    is_modifier = [name in _KEY_MODIFIERS for name in names]
    if any(name in _HIGH_IMPACT_MODIFIERS for name in names) and not all(is_modifier):
        return None  # The helper refuses it as high_impact_key_refused, not dispatched.
    if any(
        not (modifier or name in _NAMED_KEYS or name in _PRINTABLE_KEYS)
        for name, modifier in zip(names, is_modifier, strict=True)
    ):
        return LegacyWindowsRefusal("invalid_keys", "The requested key chord is unsupported.")
    others = is_modifier.count(False)
    if others == 0 or (any(is_modifier) and others != 1):
        return LegacyWindowsRefusal(
            "invalid_keys",
            "A key input must contain at least one key, and a modifier chord must contain exactly "
            "one non-modifier key.",
        )
    return None


def _coordinate(value: object) -> bool:
    # Compare integers exactly: math.isfinite() converts to float, which raises
    # OverflowError for a valid huge JSON integer such as 10**400. bool is not int here.
    if type(value) is int:
        return abs(value) <= _MAX_ABSOLUTE_COORDINATE
    return type(value) is float and math.isfinite(value) and abs(value) <= _MAX_ABSOLUTE_COORDINATE


def _legacy_drag_refusal(arguments: dict[str, object]) -> LegacyWindowsRefusal | None:
    if "to_x" not in arguments or "to_y" not in arguments:
        return LegacyWindowsRefusal(
            "invalid_action", "drag requires exact destination coordinates."
        )
    for coordinate_name in ("x", "y", "to_x", "to_y"):
        if coordinate_name in arguments and not _coordinate(arguments[coordinate_name]):
            return LegacyWindowsRefusal("invalid_request", f"{coordinate_name} is invalid.")
    if "duration_ms" in arguments:
        duration = arguments["duration_ms"]
        if type(duration) is not int or not 0 <= duration <= _MAX_DRAG_DURATION_MS:
            return LegacyWindowsRefusal("invalid_request", "duration_ms is invalid.")
    element = "element_id" in arguments
    point = [field in arguments for field in ("x", "y")]
    if element and any(point):
        return LegacyWindowsRefusal(
            "invalid_action", "Choose either an element id or exact coordinates, not both."
        )
    if not element and not all(point):
        return LegacyWindowsRefusal(
            "invalid_action", "drag requires an element id or exact coordinates."
        )
    return None


def legacy_windows_parameters(params: object) -> dict[str, object]:
    """Copy and restrict this lane's calls before any process/write effect.

    Native validation remains responsible for snapshots, targets, consent and
    effect safety. Do not infer existing-window versus launch from an app name:
    the paired helper must retain the legacy activation policy independently.
    Every refusal is a ``LegacyWindowsRefusal`` (a ``ValueError``) that the
    transport returns to the model as a structured, not-dispatched tool error.
    """
    try:
        value = json.loads(json.dumps(params, allow_nan=False))
    except (ValueError, TypeError, RecursionError) as exc:
        raise LegacyWindowsRefusal(
            "invalid_request", "Legacy Windows Computer Use parameters are invalid."
        ) from exc
    message = "This request is outside legacy Windows Computer Use."
    if not isinstance(value, dict) or set(value) != {"name", "arguments"}:
        raise LegacyWindowsRefusal("invalid_request", message)
    name, arguments = value["name"], value["arguments"]
    if (
        not isinstance(name, str)
        or name not in LEGACY_WINDOWS_TOOLS
        or not isinstance(arguments, dict)
    ):
        raise LegacyWindowsRefusal("unsupported_action", message)
    if name == "computer_state":
        fields = {"bundle_id", "window_title", "max_elements", "page_cursor"}
    elif name == "computer_wait":
        fields = {"duration_ms"}
    else:
        fields = {
            "action",
            "snapshot_id",
            "element_id",
            "text",
            "keys",
            "x",
            "y",
            "delta_x",
            "delta_y",
            "application",
        }
        action = arguments.get("action")
        if not isinstance(action, str) or action.strip().lower() not in {
            "press",
            "focus",
            "click",
            "drag",
            "type",
            "set_value",
            "key",
            "scroll",
            "activate_application",
        }:
            raise LegacyWindowsRefusal("unsupported_action", message)
        action = action.strip().lower()
        # The special focused-input target is a new helper route, not legacy
        # element input. The native validator still rejects stale/unknown real IDs.
        element = arguments.get("element_id")
        if isinstance(element, str) and element.strip() == "focus":
            raise LegacyWindowsRefusal("unsupported_action", message)
        refusal = None
        if action == "drag":
            # Drag takes only its own schema: no text, keys, scroll or app fields.
            fields = {
                "action",
                "snapshot_id",
                "element_id",
                "x",
                "y",
                "to_x",
                "to_y",
                "duration_ms",
            }
            if not set(arguments) - fields:
                refusal = _legacy_drag_refusal(arguments)
        elif action == "key":
            refusal = _legacy_key_refusal(arguments.get("keys"))
        if refusal is not None:
            raise refusal
    if set(arguments) - fields:
        raise LegacyWindowsRefusal(
            "invalid_action", "The Computer Use action contains unsupported fields."
        )
    return value


def legacy_windows_launch(
    server: ResolvedMcpServer,
    runtime_kind: RuntimeKind | None,
    host_env: Mapping[str, str] | None = None,
) -> _LegacyWindowsLaunch | None:
    """Match only the APP's selected opt-in legacy interactive registration.

    This is the LR-41 Windows availability exception. Host metadata selects a
    path; it is not proof of host identity, native custody, or W11 qualification.
    Preserve user tool restrictions and shorter timeouts; never widen them.
    """
    host = os.environ if host_env is None else host_env
    if (
        sys.platform != "win32"
        or runtime_kind != RuntimeKind.INTERACTIVE_CHAT
        or server.id != "alysis-computer"
        or server.transport != "stdio"
        or server.enabled is not True
        or server.enabled_in != (RuntimeKind.INTERACTIVE_CHAT,)
        or server.trust != "explicit"
        or server.args != ("--allow-all",)
        or server.tool_prefix is not None
        or server.roots_mode != "disabled"
        or server.resources_mode != "disabled"
        or server.prompts_mode != "disabled"
        or server.url is not None
        or server.headers
        or server.oauth is not None
        or not set(server.allowed_tools).issubset(LEGACY_WINDOWS_TOOLS)
        or not set(server.denied_tools).issubset(LEGACY_WINDOWS_TOOLS)
    ):
        return None
    for value, maximum in ((server.startup_timeout_s, 10), (server.call_timeout_s, 1080)):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
            return None
    command = server.command
    if (
        not isinstance(command, str)
        or not os.path.isabs(command)
        or Path(command).name != "alysis-computer-use.exe"
    ):
        return None
    raw = host.get("ALYSIS_DESKTOP_TOOL_SERVER_APPROVAL")
    if not isinstance(raw, str) or len(raw) > 65536:
        return None
    try:
        selection = json.loads(raw, object_pairs_hook=_unique_object)
        if (
            not isinstance(selection, dict)
            or type(selection.get("version")) is not int
            or selection["version"] != 1
            or not isinstance(selection.get("exempt"), list)
        ):
            return None
        selected = [
            item
            for item in selection["exempt"]
            if isinstance(item, dict) and item.get("server_id") == "alysis-computer"
        ]
        if selected != [{"server_id": "alysis-computer", "command": command}]:
            return None
        if set(server.env) != set(_CONTROL_KEYS):
            return None
        control = tuple((key, server.env[key]) for key in _CONTROL_KEYS)
        if any(not isinstance(value, str) or host.get(key) != value for key, value in control):
            return None
        address = server.env[_CONTROL_KEYS[0]]
        match = re.fullmatch(r"127\.0\.0\.1:([1-9][0-9]{0,4})", address)
        if (
            match is None
            or int(match[1]) > 65535
            or re.fullmatch(r"[0-9a-f]{32}", server.env[_CONTROL_KEYS[1]]) is None
            or re.fullmatch(r"[0-9a-f]{64}", server.env[_CONTROL_KEYS[2]]) is None
        ):
            return None
        # An exact absolute invocation, not a PATH/shell/symlink/hardlink alias.
        if os.path.realpath(command) != command:
            return None
        info = os.stat(command, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None
        return _LegacyWindowsLaunch(
            command,
            (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns),
            control,
        )
    except (OSError, ValueError, TypeError, RecursionError):
        return None


@dataclass(frozen=True)
class LaunchInspection:
    """Negative scan evidence only. Even a complete scan is NOT admission."""

    scripts: int
    bytes_read: int
    tokens: int
    depth: int
    limitations: frozenset[str]
    state: str = field(default="unproven", init=False)
    obligation: str = field(default="OBLIGATION-HELPER-ADMISSION", init=False)


@dataclass
class _Budget:
    scripts: int = 0
    bytes_read: int = 0
    tokens: int = 0
    depth: int = 0
    limitations: set[str] = field(default_factory=set)

    def text(self, value: str) -> str:
        remaining = MAX_INSPECTION_BYTES - self.bytes_read
        # Slice before encoding: even an enormous environment substitution may
        # not allocate an enormous intermediate byte string.
        encoded = value[:remaining].encode("utf-8", errors="replace")
        if len(value) > remaining or len(encoded) > remaining:
            self.limitations.add("byte_budget")
        encoded = encoded[:remaining]
        self.bytes_read += len(encoded)
        return encoded.decode("utf-8", errors="ignore")

    def token(self) -> bool:
        if self.tokens >= MAX_INSPECTION_TOKENS:
            self.limitations.add("token_budget")
            return False
        self.tokens += 1
        return True

    def result(self) -> LaunchInspection:
        return LaunchInspection(
            self.scripts, self.bytes_read, self.tokens, self.depth, frozenset(self.limitations)
        )


def _name(value: str) -> str:
    name = value.replace("\\", "/").rsplit("/", 1)[-1].strip().strip("\"'").rstrip(" .").casefold()
    return re.sub(r"\.(exe|com|cmd|bat)$", "", name)


def legacy_windows_observation_tool(server_id: object, tool_name: object) -> bool:
    """True for the legacy Windows helper's state-returning tools."""

    return (
        str(server_id or "") == "alysis-computer"
        and str(tool_name or "") in LEGACY_WINDOWS_OBSERVATION_TOOLS
    )


LEGACY_WINDOWS_OBSERVATION_GUIDANCE = (
    "Host note: each successful computer_action already returns {receipt, state} and a "
    "screenshot when available; trust that state for the next step and call computer_state "
    "only when it is missing, stale or uncertain."
)


def legacy_windows_description_max_chars(server_id: object, tool_name: object) -> int | None:
    """The wider description budget for the legacy Windows helper's tools, else None."""

    if str(server_id or "") == "alysis-computer" and str(tool_name or "") in LEGACY_WINDOWS_TOOLS:
        return LEGACY_WINDOWS_DESCRIPTION_MAX_CHARS
    return None


def legacy_windows_tool_guidance(server_id: object, tool_name: object) -> str | None:
    """Fixed host guidance for the legacy Windows helper's state-returning tools."""

    if legacy_windows_observation_tool(server_id, tool_name):
        return LEGACY_WINDOWS_OBSERVATION_GUIDANCE
    return None


def is_reserved_relay_name(value: str) -> bool:
    """A command identity check, never a predicate for arbitrary data args."""
    return _name(value) in _NAMES


def installed_relay_paths(host_env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    # Existing APP launch metadata identifies its installed helper. Exemptions
    # are used only to strengthen denial here, never as an admission grant.
    policy = tool_server_approval(host_env)
    if policy is None:
        return ()
    return tuple(
        Path(command)
        for server_id, command in policy.exempt
        if server_id.rsplit("/", 1)[-1] == "alysis-computer"
    )


def resolve_launch(
    command: str, *, env: Mapping[str, str], cwd: Path, windows: bool, windows_pathext: bool = True
) -> str | None:
    """Select an invocation path without substituting its canonical target.

    Native Windows first launch only appends .exe; child-PATH/PATHEXT fallback
    remains the existing transport resolver. Shell inspection uses PATHEXT.
    """
    environment = {key.upper(): value for key, value in env.items()} if windows else env
    path_value = environment.get("PATH", os.defpath)
    if not windows:
        if "/" in command:
            candidate = Path(command)
            candidate = candidate if candidate.is_absolute() else cwd / candidate
            try:
                return os.fspath(candidate) if candidate.is_file() else None
            except (OSError, ValueError):
                return None
        # Relative PATH entries are relative to the child's actual working dir.
        search = os.pathsep.join(
            os.fspath(Path(part) if Path(part).is_absolute() else cwd / part)
            for part in path_value.split(os.pathsep)
        )
        found = shutil.which(command, path=search)
        return found
    extensions = (
        [
            part.strip()
            for part in environment.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
            if part.strip()
        ]
        if windows_pathext
        else [".exe"]
    )
    suffix = Path(command).suffix.casefold()
    names = [command] if suffix or command.endswith(".") else [command + ext for ext in extensions]
    # Include case variants for deterministic non-Windows authoring fixtures;
    # the Windows filesystem performs the same case-insensitive lookup itself.
    if not suffix:
        names += [command + ext.lower() for ext in extensions]
    has_directory = "/" in command or "\\" in command
    roots = (
        [cwd]
        if has_directory
        else [cwd, *(Path(part.strip('"')) for part in path_value.split(os.pathsep) if part)]
    )
    if not windows_pathext and os.name == "nt":
        # CreateProcess uses the parent's application/current/system directories
        # and PATH, not the child's cwd or its overridden PATH/PATHEXT.
        cwd = Path.cwd()
        if has_directory:
            roots = [cwd]
        else:
            import ctypes

            application = ctypes.create_unicode_buffer(32768)
            system = ctypes.create_unicode_buffer(32768)
            windows_dir = ctypes.create_unicode_buffer(32768)
            if not ctypes.windll.kernel32.GetModuleFileNameW(None, application, len(application)):
                raise OSError("Cannot inspect the Windows executable search path.")
            if not ctypes.windll.kernel32.GetSystemDirectoryW(system, len(system)):
                raise OSError("Cannot inspect the Windows executable search path.")
            if not ctypes.windll.kernel32.GetWindowsDirectoryW(windows_dir, len(windows_dir)):
                raise OSError("Cannot inspect the Windows executable search path.")
            roots = [
                Path(application.value).parent,
                cwd,
                Path(system.value),
                Path(windows_dir.value) / "System",
                Path(windows_dir.value),
                *(Path(part.strip('"')) for part in path_value.split(os.pathsep) if part),
            ]
    for root in roots:
        root = root if root.is_absolute() else cwd / root
        for name in names:
            candidate = root / name
            try:
                if candidate.is_file():
                    return os.path.abspath(candidate)
            except (OSError, ValueError):
                continue
    return None


def _is_relay(
    value: str, *, env: Mapping[str, str], cwd: Path, windows: bool, installed: Sequence[Path]
) -> bool:
    if is_reserved_relay_name(value):
        return True
    resolved = resolve_launch(value, env=env, cwd=cwd, windows=windows)
    if resolved is None:
        return False
    if _name(os.path.realpath(resolved)) in _NAMES:
        return True
    for reference in installed:
        try:
            if os.path.samefile(resolved, reference):
                return True
        except OSError:
            continue
    return False


@dataclass(frozen=True)
class _Word:
    value: str
    operator: bool = False
    quoted: bool = False


def _tokens(
    text: str,
    env: Mapping[str, str],
    *,
    windows: bool,
    script_dir: Path | None,
    budget: _Budget,
    truncated: bool = False,
    powershell: bool = False,
) -> Iterator[_Word]:
    """Bounded lexical roles, not a shell evaluator.

    Quoted/escaped separators and expanded data never become command operators.
    Unsupported expansion remains unproven. Every word, including data, counts
    toward the inspection budget, but only execution positions get identity checks.
    """
    values = {key.upper(): value for key, value in env.items()} if windows else env
    index = 0
    size = len(text)
    operators = ";&|()<>\n" if not windows or powershell else "&|()<>\n"
    quotes = "\"'" if not windows or powershell else '"'
    while index < size:
        char = text[index]
        if char.isspace() and char != "\n":
            index += 1
            continue
        if (not windows or powershell) and char == "#":
            end = text.find("\n", index)
            index = size if end < 0 else end
            continue
        if not budget.token():
            return
        if char in operators:
            index += 1
            if index < size and text[index] == char and char in "&|<>":
                char += text[index]
                index += 1
            yield _Word(char, operator=True)
            continue
        pieces: list[str] = []
        quote = ""
        complete = True
        starts_quoted = text[index] in quotes
        while index < size:
            char = text[index]
            if not quote and (char.isspace() or char in operators):
                break
            if (
                powershell
                and quote
                and char == quote
                and index + 1 < size
                and text[index + 1] == quote
            ):
                pieces.append(char)
                index += 2
                continue
            if char in quotes and (not quote or quote == char):
                quote = "" if quote else char
                index += 1
                continue
            escape = "`" if powershell else "^" if windows else "\\"
            if char == escape and quote != "'" and index + 1 < size:
                # In double-quoted POSIX strings a backslash only escapes a
                # small set; retaining other backslashes preserves path data.
                following = text[index + 1]
                if windows or powershell or not quote or following in '$`"\\\n':
                    if following != "\n":
                        pieces.append(following)
                    index += 2
                    continue
            if windows and not powershell and char == "%":
                if script_dir is not None and text[index : index + 5].lower() == "%~dp0":
                    original = os.fspath(script_dir) + os.sep
                    expanded = budget.text(original)
                    complete = complete and expanded == original
                    pieces.append(expanded)
                    index += 5
                    continue
                match = re.match(r"%([^%]+)%", text[index:])
                if match and match[1].upper() in values:
                    original = values[match[1].upper()]
                    expanded = budget.text(original)
                    complete = complete and expanded == original
                    pieces.append(expanded)
                    index += len(match[0])
                    continue
            if not windows and not powershell and char == "$" and quote != "'":
                match = re.match(
                    r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", text[index:]
                )
                if match and (match[1] or match[2]) in values:
                    original = values[match[1] or match[2]]
                    expanded = budget.text(original)
                    complete = complete and expanded == original
                    # Field splitting/globbing is not evaluated. An exact
                    # expanded executable is still inspected below.
                    if not quote and any(c.isspace() for c in expanded):
                        budget.limitations.add("expanded_fields")
                    pieces.append(expanded)
                    index += len(match[0])
                    continue
            if char in "$`" or (windows and char in "%!"):
                budget.limitations.add("computed_shell_target")
            pieces.append(char)
            index += 1
        if quote:
            budget.limitations.add("malformed_shell")
            return
        if truncated and index == size:
            return  # a cut word must never become a different executable
        yield _Word("".join(pieces) if complete else "", quoted=starts_quoted)


def _commands(
    words: Iterator[_Word], budget: _Budget, *, powershell: bool = False
) -> Iterator[list[str]]:
    command: list[str] = []
    redirect = False
    call_operator = False
    for word in words:
        if word.operator:
            if word.value in {"<", ">", ">>", "<<"}:
                if word.value == "<<":
                    # Heredoc contents are data, not subsequent shell commands.
                    budget.limitations.add("heredoc")
                    if command:
                        yield command
                    return
                if command and command[-1].isdigit():
                    command.pop()  # optional file descriptor before redirect
                redirect = True
            else:
                # At a statement's beginning PowerShell & invokes its operand;
                # after a command it is a background operator, not that grant.
                call_operator = powershell and word.value == "&" and not command
                if command:
                    yield command
                command = []
                redirect = False
        elif redirect:
            redirect = False  # redirection operand, never executable identity
        else:
            # A bare quoted PowerShell string is an expression, not a command.
            # Keep its operand position so following data cannot become a target.
            command.append(
                ""
                if powershell and not command and word.quoted and not call_operator
                else word.value
            )
    if command:
        yield command


def check_native_relay_launch(
    executable: str,
    args: Sequence[str],
    *,
    server_id: str,
    env: Mapping[str, str],
    cwd: Path,
    windows: bool,
    host_env: Mapping[str, str] | None = None,
    legacy: _LegacyWindowsLaunch | None = None,
    server: ResolvedMcpServer | None = None,
    runtime_kind: RuntimeKind | None = None,
) -> LaunchInspection:
    if legacy is not None:
        current = (
            legacy_windows_launch(server, runtime_kind, host_env) if server is not None else None
        )
        if (
            not windows
            or current != legacy
            or executable != legacy.command
            or server_id != "alysis-computer"
            or tuple(args) != ("--allow-all",)
            or any(env.get(key) != value for key, value in legacy.control)
        ):
            raise ValueError(REFUSAL)
        return LaunchInspection(
            0,
            0,
            0,
            0,
            frozenset(
                {
                    "legacy_availability_only",
                    "main_authentication_unproven",
                    "native_control_required",
                }
            ),
        )
    installed = installed_relay_paths(host_env)
    if server_id.rsplit("/", 1)[-1] == "alysis-computer":
        raise ValueError(REFUSAL)
    budget = _Budget()
    scripts = {".bat", ".cmd", ".sh"}
    pending_scripts: list[tuple[str, int, bool, bool]] = []
    scheduled: set[str] = set()

    def inspect(
        candidate: str,
        depth: int,
        *,
        script_mode: bool | None = None,
        identity_only: bool = False,
        powershell: bool = False,
    ) -> str | None:
        if not candidate:
            return None
        if _is_relay(candidate, env=env, cwd=cwd, windows=windows, installed=installed):
            raise ValueError(REFUSAL)
        resolved = resolve_launch(candidate, env=env, cwd=cwd, windows=windows)
        if script_mode is not None:
            # Interpreter operands are readable files, not necessarily executable
            # PATH entries. Check their canonical/file identity without launching.
            path = Path(candidate)
            path = path if path.is_absolute() else cwd / path
            try:
                if path.is_file():
                    resolved = os.fspath(path)
            except (OSError, ValueError):
                pass
            if resolved is not None and _is_relay(
                resolved, env=env, cwd=cwd, windows=windows, installed=installed
            ):
                raise ValueError(REFUSAL)
        if resolved is None:
            return None
        canonical = os.path.realpath(resolved)
        suffix = Path(canonical).suffix.casefold()
        if identity_only:
            return resolved
        if script_mode is None and suffix not in scripts:
            return resolved
        if canonical in scheduled:
            return resolved
        if depth > MAX_INSPECTION_DEPTH:
            budget.limitations.add("depth_budget")
            return resolved
        if len(scheduled) >= MAX_INSPECTION_SCRIPTS:
            budget.limitations.add("script_budget")
            return resolved
        scheduled.add(canonical)
        pending_scripts.append(
            (
                canonical,
                depth,
                script_mode if script_mode is not None else suffix != ".sh",
                powershell,
            )
        )
        return resolved

    def shell_text(
        text: str,
        depth: int,
        dialect_windows: bool,
        script_dir: Path | None = None,
        *,
        truncated: bool = False,
        powershell: bool = False,
    ) -> None:
        budget.limitations.add("computed_shell_target")
        if depth > MAX_INSPECTION_DEPTH:
            budget.limitations.add("depth_budget")
            return
        words = _tokens(
            text,
            env,
            windows=dialect_windows,
            script_dir=script_dir,
            budget=budget,
            truncated=truncated,
            powershell=powershell,
        )
        for command in _commands(words, budget, powershell=powershell):
            if powershell and command and command[0] == ".":
                if len(command) > 1:
                    inspect(command[1], depth, script_mode=True, powershell=True)
            else:
                inspect_command(command, depth, dialect_windows)

    def inspect_command(argv: Sequence[str], depth: int, dialect_windows: bool) -> None:
        if not argv:
            return
        command = argv[0].lstrip("@") if dialect_windows else argv[0]
        rest = list(argv[1:])
        # Shell control/exec prefixes; operands of normal commands are data.
        if command in {"then", "do", "else", "if", "while", "until", "!"}:
            inspect_command(
                rest, depth + 1, dialect_windows
            ) if depth < MAX_INSPECTION_DEPTH else budget.limitations.add("depth_budget")
            return
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", command):
            budget.limitations.add("local_environment")
            if depth < MAX_INSPECTION_DEPTH:
                inspect_command(rest, depth + 1, dialect_windows)
            else:
                budget.limitations.add("depth_budget")
            return
        resolved = inspect(command, depth)
        kind = _name(os.path.realpath(resolved or command))
        prefix = "env" if kind == "env" else command
        if prefix.casefold() in ({"call"} if dialect_windows else {"exec", "command", "env"}):
            while rest:
                first = rest[0]
                if first == "--":
                    rest.pop(0)
                    break
                if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", first):
                    budget.limitations.add("local_environment")
                    rest.pop(0)
                    continue
                if first in {"-a", "-u", "--unset"} and prefix in {"exec", "env"}:
                    rest = rest[2:]
                    continue
                if first in {"-i", "--ignore-environment", "-p", "-c", "-l"}:
                    rest.pop(0)
                    continue
                if first.startswith("-"):
                    budget.limitations.add("unsupported_prefix")
                    return
                break
            if depth >= MAX_INSPECTION_DEPTH:
                budget.limitations.add("depth_budget")
                return
            inspect_command(rest, depth + 1, dialect_windows)
            return
        if command in {".", "source"} and not dialect_windows:
            if rest:
                inspect(rest[0], depth, script_mode=False)
            return
        if kind not in _SHELLS and not re.fullmatch(
            r"python(?:\d+(?:\.\d+)*)?|node|nodejs|ruby|perl|php", kind
        ):
            return
        index = 0
        while index < len(rest):
            option = rest[index]
            folded = option.casefold()
            if option == "-":
                return  # stdin program; later operands are its data
            if kind in {"cmd", "powershell", "pwsh"}:
                if folded in {"/c", "/k", "-command", "-c"}:
                    shell_text(
                        " ".join(rest[index + 1 :]),
                        depth + 1,
                        True,
                        powershell=kind in {"powershell", "pwsh"},
                    )
                    return
                if folded in {"-file", "-f"}:
                    if index + 1 < len(rest):
                        inspect(
                            rest[index + 1],
                            depth + 1,
                            script_mode=True,
                            powershell=kind in {"powershell", "pwsh"},
                        )
                    return
                if folded in {"-encodedcommand", "-enc"}:
                    budget.limitations.add("computed_shell_target")
                    return
                index += 1
                continue
            if (
                kind in _SHELLS
                and option.startswith("-")
                and "c" in option[1:]
                and not option.startswith("--")
            ):
                if index + 1 < len(rest):
                    shell_text(rest[index + 1], depth + 1, False)
                return
            if (
                kind in _SHELLS
                and option.startswith("-")
                and "s" in option[1:]
                and not option.startswith("--")
            ):
                return  # shell stdin mode, not a script-file operand
            if kind not in _SHELLS and option in {"-c", "-m", "-e", "--eval", "--print", "-p"}:
                budget.limitations.add("interpreter_code")
                return
            if kind == "php" and option == "-r":
                budget.limitations.add("interpreter_code")
                return
            if option == "--":
                index += 1
                if index >= len(rest):
                    return
                option = rest[index]
            elif option.startswith("-"):
                if option in {"-W", "-X", "-r", "--require", "--loader", "--import"}:
                    index += 2
                elif kind not in _SHELLS and option not in {
                    "-u",
                    "-B",
                    "-E",
                    "-I",
                    "-O",
                    "-OO",
                    "-q",
                    "-s",
                    "-S",
                    "-v",
                    "--no-warnings",
                    "--trace-warnings",
                }:
                    budget.limitations.add("unsupported_interpreter_option")
                    return
                else:
                    index += 1
                continue
            if kind in _SHELLS:
                inspect(option, depth + 1, script_mode=False)
            else:
                # Identity of an interpreter's selected script is inspected;
                # its language is not executed or parsed as a shell program.
                path = Path(option)
                path = path if path.is_absolute() else cwd / path
                inspect(os.fspath(path), depth + 1, identity_only=True)
            return

    # The selected executable's identity is always checked, even if arguments
    # or nested contents exhaust the remaining inspection budget.
    inspect(executable, 1)
    bounded_args = []
    for argument in args:
        if not budget.token():
            break
        bounded = budget.text(argument)
        if bounded != argument:
            break  # incomplete arguments are unproven, not executable names
        bounded_args.append(bounded)
    inspect_command([executable, *bounded_args], 1, windows)

    while pending_scripts and budget.tokens < MAX_INSPECTION_TOKENS:
        script, depth, dialect_windows, powershell = pending_scripts.pop()
        budget.scripts += 1
        budget.depth = max(budget.depth, depth)
        budget.limitations.add("computed_shell_target")
        remaining = MAX_INSPECTION_BYTES - budget.bytes_read
        if remaining <= 0:
            budget.limitations.add("byte_budget")
            break
        try:
            with open(script, "rb") as source:
                data = source.read(remaining)
            if len(data) == remaining:
                budget.limitations.add("byte_budget")
            budget.bytes_read += len(data)
            shell_text(
                data.decode("utf-8", errors="replace"),
                depth,
                dialect_windows,
                Path(script).parent,
                truncated=len(data) == remaining,
                powershell=powershell,
            )
        except OSError:
            budget.limitations.add("unreadable_script")
    if pending_scripts and budget.tokens >= MAX_INSPECTION_TOKENS:
        budget.limitations.add("token_budget")
    # No grant is returned: unreadable, computed or exhausted contents remain
    # covered by the OPEN/BLOCKING helper-admission obligation. Ordinary MCP
    # launchers are not globally denied because a negative scan is incomplete.
    return budget.result()
