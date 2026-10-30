"""Shell-command rules for Windows command processors (cmd.exe and PowerShell).

The other shell policies (``policy.evaluate_shell_command`` and the Full-access denylist in
``agent.tools_assembly``) match POSIX commands with regular expressions. On Windows the same
intents are spelled differently (``rd /s /q C:\\``, ``Remove-Item -Recurse -Force C:\\``,
``iwr … | iex``), and they are often wrapped (``powershell -Command "…"``, ``cmd /c "…"``,
``powershell -EncodedCommand …``), where a regular expression sees one opaque token.

So these rules parse instead of matching text:

1. Split the command line into simple commands at unquoted ``&&``, ``||``, ``&``, ``|``, ``;``
   and line breaks.
2. Unwrap ``cmd /c``/``/k``, ``powershell``/``pwsh -Command``/``-c`` and ``-EncodedCommand``
   (decoded from Base64 UTF-16LE), and check what they run.
3. Name each simple command the way the shell resolves it: case-insensitively, without a
   directory or an ``.exe`` suffix, with PowerShell aliases (``ri``, ``rm``, ``rd``, ``del``,
   ``iwr``, ``iex`` …) and abbreviated parameters (``-r``, ``-rec``, ``-fo``) resolved.

Each finding has a severity:

* ``catastrophic``: blocked in every mode, Full access included (the Windows counterpart of
  ``rm -rf /``, ``mkfs``, ``sudo`` and ``curl … | sh``).
* ``dangerous``: blocked outside Full access (the counterpart of ``rm -rf`` anywhere).
* ``confirm``: allowed after confirmation where the mode asks (package installs).

The rules apply on every platform, because PowerShell runs on macOS and Linux too, and the
cmd.exe forms mean nothing to a POSIX shell. They are guardrails against accidents, not a
sandbox: a determined script can always reach the same effect another way.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from typing import Literal

Severity = Literal["catastrophic", "dangerous", "confirm"]
_SEVERITY_ORDER: dict[str, int] = {"catastrophic": 0, "dangerous": 1, "confirm": 2}
_MAX_UNWRAP_DEPTH = 4


@dataclass(frozen=True)
class WindowsCommandFinding:
    severity: Severity
    reason: str


# PowerShell names (and their default aliases) for each cmdlet the rules look at.
_REMOVE_ITEM = {"remove-item", "ri", "rm", "rmdir", "rd", "del", "erase"}
_DOWNLOADERS = {"invoke-webrequest", "iwr", "invoke-restmethod", "irm", "curl", "wget"}
_EXPRESSION_RUNNERS = {"invoke-expression", "iex"}
_DISK_CMDLETS = {
    "format-volume": "Volume formatting (Format-Volume)",
    "clear-disk": "Disk wipe (Clear-Disk)",
    "initialize-disk": "Disk initialisation (Initialize-Disk)",
    "remove-partition": "Partition removal (Remove-Partition)",
    "reset-physicaldisk": "Physical disk reset (Reset-PhysicalDisk)",
    "diskpart": "Disk partitioning tool (diskpart)",
}
_PACKAGE_INSTALLERS = {
    ("winget", "install"),
    ("choco", "install"),
    ("scoop", "install"),
    ("install-module", ""),
    ("install-package", ""),
}
_SHELLS_CMD = {"cmd"}
_SHELLS_POWERSHELL = {"powershell", "pwsh"}
_EXECUTABLE_SUFFIXES = (".exe", ".com", ".cmd", ".bat")
_SEPARATORS = ("&&", "||", "&", "|", ";", "\n")

# A drive or filesystem root, alone or with a wildcard: C:, C:\, \, /, C:\*, C:\*.*
_DRIVE_ROOT = re.compile(r"^(?:[a-z]:[\\/]?|[\\/])(?:\*(?:\.\*)?)?$")
_SYSTEM_PATH = re.compile(
    r"^(?:[a-z]:)?[\\/](?:windows|program files(?: \(x86\))?|programdata|users(?:[\\/][^\\/]+)?)"
    r"(?:[\\/]\*?)?$"
)
_PROFILE_ROOT = re.compile(
    r"^(?:~|\$home|\$env:userprofile|%userprofile%|\$env:homedrive\$env:homepath)(?:[\\/]\*?)?$"
)
_REGISTRY_HIVE = re.compile(r"^(?:hklm|hkey_local_machine|hkcu|hkey_current_user)(?::)?(?:[\\/]|$)")


def _split_simple_commands(command: str) -> list[str]:
    """Split at unquoted command separators; quotes stay part of the text."""

    parts: list[str] = []
    current: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            current.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            current.append(ch)
            i += 1
            continue
        separator = next((s for s in _SEPARATORS if command.startswith(s, i)), None)
        if separator is not None:
            parts.append("".join(current))
            parts.append(separator)
            current = []
            i += len(separator)
            continue
        current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts


def _tokens(simple_command: str) -> list[str]:
    """Whitespace-separated words; quotes group and are removed; backslashes stay literal."""

    words: list[str] = []
    current: list[str] = []
    quote = ""
    started = False
    for ch in simple_command:
        if quote:
            if ch == quote:
                quote = ""
            else:
                current.append(ch)
            continue
        if ch in "\"'":
            quote = ch
            started = True
        elif ch.isspace():
            if started or current:
                words.append("".join(current))
            current, started = [], False
        else:
            current.append(ch)
            started = True
    if started or current:
        words.append("".join(current))
    return words


def command_name(token: str) -> str:
    # This is an already-tokenized executable, not display text. Trimming can
    # turn a quoted filename such as NBSP + echo into a different program.
    name = re.split(r"[\\/]", token.lower())[-1]
    for suffix in _EXECUTABLE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _parameter_spellings(name: str, shortest: int) -> frozenset[str]:
    """Every abbreviation PowerShell accepts for parameter ``name`` (``-r`` … ``-recurse``)."""

    return frozenset(f"-{name[:length]}" for length in range(shortest, len(name) + 1))


_RECURSE = _parameter_spellings("recurse", 1)
_FORCE = _parameter_spellings("force", 2)  # "-f" alone is ambiguous with -Filter
_VERB = _parameter_spellings("verb", 2)
_PS_ENCODED_COMMAND = _parameter_spellings("encodedcommand", 1) | {"-ec"}
_PS_COMMAND = _parameter_spellings("command", 1)
_PS_FILE = _parameter_spellings("file", 1)
_PS_PARAMETERS_WITH_VALUE = (
    _parameter_spellings("executionpolicy", 2)
    | _parameter_spellings("windowstyle", 1)
    | _parameter_spellings("outputformat", 2)
    | _parameter_spellings("inputformat", 2)
    | _parameter_spellings("configurationname", 4)
    | _parameter_spellings("psconsolefile", 3)
    | _parameter_spellings("version", 1)
    | _parameter_spellings("workingdirectory", 2)
    | _parameter_spellings("settingsfile", 2)
    | {"-ep", "-of", "-if", "-wd"}
)


def _has_parameter(arguments: list[str], spellings: frozenset[str]) -> bool:
    return any(argument.lower().split(":", 1)[0] in spellings for argument in arguments)


def _cmd_switches(arguments: list[str]) -> set[str]:
    """cmd switches such as ``/s /q`` or ``/s/q``; a bare ``/`` or ``\\`` is a path."""

    switches: set[str] = set()
    for argument in arguments:
        if len(argument) >= 2 and argument[0] == "/" and argument[1] not in "\\/":
            switches.update(f"/{part.lower()}" for part in argument.split("/") if part)
    return switches


def _clean_target(target: str) -> str:
    # CommandLineToArgvW turns a trailing backslash-quote into a quote, so "C:\\" can arrive as C:".
    return target.strip().strip("\"'").strip().lower()


def _target_severity(targets: list[str]) -> Severity:
    for target in targets:
        cleaned = _clean_target(target)
        if (
            _DRIVE_ROOT.match(cleaned)
            or _SYSTEM_PATH.match(cleaned)
            or _PROFILE_ROOT.match(cleaned)
            or _REGISTRY_HIVE.match(cleaned)
        ):
            return "catastrophic"
    return "dangerous"


def _decode_encoded_command(value: str) -> str | None:
    try:
        return base64.b64decode(value, validate=True).decode("utf-16-le")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def powershell_payload(arguments: list[str]) -> tuple[str | None, bool]:
    """The script a powershell/pwsh command line runs, and whether it could be read.

    ``-File`` runs a script file, which is not inspected. Without ``-Command`` or ``-File``,
    the first positional argument starts the command text (Windows PowerShell's default).
    """

    index = 0
    while index < len(arguments):
        argument = arguments[index].lower()
        if argument in _PS_ENCODED_COMMAND:
            if index + 1 >= len(arguments):
                return None, False
            decoded = _decode_encoded_command(arguments[index + 1])
            return decoded, decoded is not None
        if argument in _PS_COMMAND:
            return " ".join(arguments[index + 1 :]), True
        if argument in _PS_FILE:
            return None, True
        if argument in _PS_PARAMETERS_WITH_VALUE:
            index += 2
            continue
        if argument.startswith("-"):
            index += 1
            continue
        return " ".join(arguments[index:]), True
    return None, True


def _non_switch_arguments(arguments: list[str], switch_prefix: str) -> list[str]:
    return [a for a in arguments if not a.startswith(switch_prefix) or _DRIVE_ROOT.match(a.lower())]


def _simple_command_findings(simple_command: str, depth: int) -> list[WindowsCommandFinding]:
    words = _tokens(simple_command)
    while words and words[0].lower() in {"call", "start", "."}:
        words = [w for w in words[1:] if not (w.startswith("/") and len(w) > 1)]
    if not words:
        return []
    name, arguments = command_name(words[0]), words[1:]
    lowered = [a.lower() for a in arguments]
    findings: list[WindowsCommandFinding] = []

    def add(severity: Severity, reason: str) -> None:
        findings.append(WindowsCommandFinding(severity, reason))

    if name in _SHELLS_CMD:
        for index, argument in enumerate(lowered):
            if argument in {"/c", "/k", "/r"}:
                return _findings(" ".join(arguments[index + 1 :]), depth + 1)
        return []
    if name in _SHELLS_POWERSHELL:
        payload, readable = powershell_payload(arguments)
        if not readable:
            return [WindowsCommandFinding("dangerous", "Unreadable encoded PowerShell command")]
        return _findings(payload, depth + 1) if payload else []

    switches = _cmd_switches(arguments)
    if name in {"rd", "rmdir"} and "/s" in switches:
        severity = _target_severity(_non_switch_arguments(arguments, "/"))
        if "/q" in switches or severity == "catastrophic":
            add(severity, "Recursive delete (rd /s /q)")
    if name in {"del", "erase"} and "/s" in switches:
        severity = _target_severity(_non_switch_arguments(arguments, "/"))
        if switches & {"/q", "/f"} or severity == "catastrophic":
            add(severity, "Recursive delete (del /s /q)")
    if name in _REMOVE_ITEM and _has_parameter(arguments, _RECURSE):
        severity = _target_severity([a for a in arguments if not a.startswith("-")])
        if _has_parameter(arguments, _FORCE) or severity == "catastrophic":
            add(severity, "Recursive delete (Remove-Item -Recurse -Force)")
    if name == "format" and any(re.fullmatch(r"[a-z]:", a) for a in lowered):
        add("catastrophic", "Drive formatting (format)")
    if name in _DISK_CMDLETS:
        add("catastrophic", _DISK_CMDLETS[name])
    if name == "vssadmin" and lowered[:1] in (["delete"], ["resize"]):
        add("catastrophic", "Shadow copy deletion (vssadmin)")
    if name == "wmic" and "shadowcopy" in lowered and "delete" in lowered:
        add("catastrophic", "Shadow copy deletion (wmic)")
    if name == "wbadmin" and lowered[:1] == ["delete"]:
        add("catastrophic", "Backup deletion (wbadmin)")
    if name == "bcdedit" and switches & {"/delete", "/set", "/deletevalue", "/import"}:
        add("catastrophic", "Boot configuration change (bcdedit)")
    if name == "cipher" and any(a.startswith("/w") for a in lowered):
        add("catastrophic", "Free-space wipe (cipher /w)")
    if name == "reg" and lowered[:1] == ["delete"] and len(lowered) > 1:
        if _REGISTRY_HIVE.match(_clean_target(lowered[1])):
            add("catastrophic", "Registry deletion (reg delete)")
    if name in {"icacls", "takeown"} and switches & {"/t", "/r"}:
        paths = _non_switch_arguments(arguments, "/")
        if _target_severity(paths[:1]) == "catastrophic":
            add("catastrophic", f"Recursive permission change ({name})")
    elevated = any(
        argument in _VERB and index + 1 < len(lowered) and lowered[index + 1] == "runas"
        for index, argument in enumerate(lowered)
    )
    if name in {"runas", "gsudo"} or elevated:
        add("catastrophic", "Elevation (run as administrator)")
    if name in _EXPRESSION_RUNNERS:
        words_inside = set(re.split(r"[\s(){};]+", simple_command.lower()))
        if words_inside & _DOWNLOADERS or "downloadstring" in simple_command.lower():
            add("catastrophic", "Download and run (iwr … | iex)")
    for installer, verb in _PACKAGE_INSTALLERS:
        if name == installer and (not verb or lowered[:1] == [verb]):
            add("confirm", "Package install")
    return findings


def _findings(command: str, depth: int) -> list[WindowsCommandFinding]:
    if depth > _MAX_UNWRAP_DEPTH:
        return [WindowsCommandFinding("dangerous", "Command nested too deeply to check")]
    parts = _split_simple_commands(command)
    findings: list[WindowsCommandFinding] = []
    previous_name = ""
    for index in range(0, len(parts), 2):
        simple = parts[index]
        findings += _simple_command_findings(simple, depth)
        words = _tokens(simple)
        name = command_name(words[0]) if words else ""
        separator = parts[index - 1] if index > 0 else ""
        if separator == "|" and previous_name in _DOWNLOADERS and name in _EXPRESSION_RUNNERS:
            findings.append(WindowsCommandFinding("catastrophic", "Download and run (iwr … | iex)"))
        previous_name = name
    return findings


def windows_command_findings(command: str) -> list[WindowsCommandFinding]:
    """Findings for ``command``, most severe first, without duplicates."""

    unique = {(f.severity, f.reason): f for f in _findings(str(command or ""), 0)}
    return sorted(unique.values(), key=lambda f: (_SEVERITY_ORDER[f.severity], f.reason))


def most_severe_windows_finding(command: str) -> WindowsCommandFinding | None:
    findings = windows_command_findings(command)
    return findings[0] if findings else None
