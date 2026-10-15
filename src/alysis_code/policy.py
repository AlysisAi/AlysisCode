from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from .command_review_rules import review_reasons
from .windows_command_policy import most_severe_windows_finding


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    needs_confirm: bool
    reason: str


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


def _is_control_operator(token: str) -> bool:
    return token in {"&&", "||", ";", "|"}


def _is_destructive_rm(lowered_cmd: str) -> bool:
    try:
        tokens = shlex.split(lowered_cmd)
    except ValueError:
        return False

    for idx, token in enumerate(tokens):
        if not _is_rm_command_token(token):
            continue
        has_recursive = False
        has_force = False

        for opt in tokens[idx + 1 :]:
            if _is_control_operator(opt) or opt == "--":
                break
            if not opt.startswith("-") or opt == "-":
                break
            if opt.startswith("--"):
                if opt == "--recursive" or opt.startswith("--recursive="):
                    has_recursive = True
                if opt == "--force" or opt.startswith("--force="):
                    has_force = True
            else:
                flags = opt[1:]
                if "r" in flags or "R" in flags:
                    has_recursive = True
                if "f" in flags:
                    has_force = True
            if has_recursive and has_force:
                return True
    return False


def evaluate_shell_command(cmd: str) -> PolicyDecision:
    c = cmd.strip()
    if not c:
        return PolicyDecision(allowed=False, needs_confirm=False, reason="Empty command")

    lowered = c.lower()

    if _is_destructive_rm(lowered):
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

    for pat, reason in _CONFIRM_PATTERNS:
        if pat.search(lowered):
            return PolicyDecision(allowed=True, needs_confirm=True, reason=reason)

    if windows_finding is not None:
        return PolicyDecision(allowed=True, needs_confirm=True, reason=windows_finding.reason)

    # Harm a workspace sandbox does not contain: lost work, rewritten history, publishing,
    # shared systems, credentials. See command_review_rules.
    reasons = review_reasons(c)
    if reasons:
        return PolicyDecision(allowed=True, needs_confirm=True, reason="; ".join(reasons))

    return PolicyDecision(allowed=True, needs_confirm=False, reason="OK")
