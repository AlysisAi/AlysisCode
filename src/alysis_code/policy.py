from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from .command_review_rules import review_reasons
from .shell_command_lexer import ShellDialect, split_simple_commands, unwrap_shell_payload
from .windows_command_policy import most_severe_windows_finding


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    needs_confirm: bool
    reason: str
    # Whether ``--yes`` answers the confirmation. It does not for a command that
    # removes a folder with everything in it: that is decided each time, and
    # refused where nobody can decide.
    waivable: bool = True


_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bmkfs(\.\w+)?\b"), "Filesystem formatting (mkfs)"),
    (re.compile(r"\bwipefs\b"), "Filesystem wipe (wipefs)"),
    (re.compile(r"\bdd\b.*\bof="), "Raw disk write (dd ... of=)"),
    (re.compile(r"\b(parted|fdisk|sfdisk|sgdisk)\b"), "Disk partitioning tool"),
]

_CONFIRM_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bgit\b\s+push\b"), "git push"),
    (re.compile(r"\b(curl|wget)\b.*\|\s*(sh|bash)\b"), "pipe-to-shell installer"),
    (re.compile(r"\bapt(-get)?\b\s+install\b"), "system package install"),
    (re.compile(r"\bbrew\b\s+install\b"), "package install"),
    (re.compile(r"\bpip\b\s+install\b"), "python package install"),
    (re.compile(r"\bnpm\b\s+install\b"), "node package install"),
]


def _is_rm_command_token(token: str) -> bool:
    if not token:
        return False
    return token.rsplit("/", 1)[-1] == "rm"


# A filesystem root, a home folder or one of the folders directly under the
# root (/etc, /usr, /Users): nothing a task in a project folder asks to remove.
_ROOT_OR_HOME_TARGET = re.compile(
    r"^(?:/+\*?|/+[^/]+/*\*?|(?:~[^/]*|\$home|\$\{home\})(?:/+\*?)?)$"
)


def _forced_recursive_rm_targets(lowered_cmd: str, *, depth: int = 0) -> list[list[str]]:
    """What each ``rm`` with both a recursive and a force option names, in order.

    Read one simple command at a time, so that the options and operands of the
    command after it are never taken for rm's, and through ``sh -c`` payloads.
    """
    found: list[list[str]] = []
    # The braces of ${HOME} are grouping to the lenient reader, which splits there.
    lowered_cmd = lowered_cmd.replace("${home}", "$home")
    for item in split_simple_commands(lowered_cmd, dialect="posix", strict=False) or []:
        words = list(item.words)
        unwrapped = unwrap_shell_payload(words) if words and depth < 4 else None
        if unwrapped:
            # Whatever shell runs the payload, an rm in it is read the same way.
            found += _forced_recursive_rm_targets(unwrapped[0], depth=depth + 1)
            continue
        for idx, token in enumerate(words):
            if not _is_rm_command_token(token):
                continue
            has_recursive = False
            has_force = False
            targets: list[str] = []
            options_ended = False
            for word in words[idx + 1 :]:
                if word.startswith("#"):
                    break  # the rest of the line is a comment
                if options_ended or not word.startswith("-") or word == "-":
                    targets.append(word)
                elif word == "--":
                    options_ended = True
                elif word.startswith("--"):
                    if word == "--recursive" or word.startswith("--recursive="):
                        has_recursive = True
                    if word == "--force" or word.startswith("--force="):
                        has_force = True
                    if word == "--no-preserve-root":
                        targets.append("/")
                else:
                    flags = word[1:]
                    if "r" in flags:  # the text is lowered: -R as well
                        has_recursive = True
                    if "f" in flags:
                        has_force = True
            if has_recursive and has_force:
                found.append(targets)
    if not found and depth == 0 and "{" in lowered_cmd:
        # Options written as a brace list (rm -{r,f} x): the lenient reader ends
        # the command at the brace. Read the words as the shell's own splitter
        # gives them; what they name is then not known.
        try:
            tokens = shlex.split(lowered_cmd)
        except ValueError:
            tokens = lowered_cmd.split()
        for idx, token in enumerate(tokens):
            if _is_rm_command_token(token):
                flags = "".join(word for word in tokens[idx + 1 :] if word.startswith("-"))
                if "r" in flags and "f" in flags:
                    found.append([])
    return found


def evaluate_shell_command(cmd: str, *, dialect: ShellDialect | None = None) -> PolicyDecision:
    """Judge a command; ``dialect`` is the shell that will run it (default: the host's)."""
    c = cmd.strip()
    if not c:
        return PolicyDecision(allowed=False, needs_confirm=False, reason="Empty command")

    lowered = c.lower()

    # Removing a folder with everything in it is ordinary work on a project
    # (build output, a dependency folder), so it is decided, not refused: by
    # the reviewer in "Approve for me", by a person where the mode asks one.
    # --yes does not answer it, and where nobody can decide it is refused, as
    # it always was. Aimed at a root, a home folder or a system folder it is
    # refused outright, like formatting a disk. The Windows spellings (rd /s /q,
    # Remove-Item -Recurse -Force, also as rm -r -fo) are refused below and are
    # decided only where the Windows sandbox bounds them; see guard_shell.
    forced_removals = _forced_recursive_rm_targets(lowered)
    if any(_ROOT_OR_HOME_TARGET.match(target) for targets in forced_removals for target in targets):
        return PolicyDecision(
            allowed=False, needs_confirm=False, reason="Destructive remove (rm -rf)"
        )

    for pat, reason in _DANGEROUS_PATTERNS:
        if pat.search(lowered):
            return PolicyDecision(allowed=False, needs_confirm=False, reason=reason)

    # cmd.exe and PowerShell spell the same intents differently (rd /s /q, Remove-Item
    # -Recurse -Force, Format-Volume …); see windows_command_policy.
    windows_finding = most_severe_windows_finding(c)
    if windows_finding is not None and windows_finding.severity != "confirm":
        return PolicyDecision(allowed=False, needs_confirm=False, reason=windows_finding.reason)

    if forced_removals:
        return PolicyDecision(
            allowed=True,
            needs_confirm=True,
            reason="removes a folder and its contents (rm -rf)",
            waivable=False,
        )

    for pat, reason in _CONFIRM_PATTERNS:
        if pat.search(lowered):
            return PolicyDecision(allowed=True, needs_confirm=True, reason=reason)

    if windows_finding is not None:
        return PolicyDecision(allowed=True, needs_confirm=True, reason=windows_finding.reason)

    # Harm a workspace sandbox does not contain: lost work, rewritten history, publishing,
    # shared systems, credentials. See command_review_rules.
    reasons = review_reasons(c, dialect=dialect)
    if reasons:
        return PolicyDecision(allowed=True, needs_confirm=True, reason="; ".join(reasons))

    return PolicyDecision(allowed=True, needs_confirm=False, reason="OK")
