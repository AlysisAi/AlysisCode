"""Per-subagent visual identity — accent colour only.

One tiny authority so the transcript spawn line, the
live status, and the footer badge can never disagree about who a given
subagent is. Activity comes from runtime events, never from role narration.
Result attribution prints the registry name alone and deliberately does not
consult this module — a subagent wears no per-agent symbol, so there is
nothing to keep in sync.

The name is the identity: the shared ``↪``/``↩`` marks say only that a nested
run started or ended, never which agent it was. Colours are distinct from the
fixed mode accents (green chat, cyan brand) so a
subagent badge never impersonates a mode. Custom subagents get a colour picked
deterministically from the name — the same agent looks the same every run.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass


@dataclass(frozen=True)
class SubagentIdentity:
    color: str


_BUILTIN_IDENTITIES: dict[str, SubagentIdentity] = {
    "frontend-engineer": SubagentIdentity("#a371f7"),
    # Silver keeps delegated work visually distinct from the main modes.
    "visual-designer": SubagentIdentity("#c9d1d9"),
    "explorer": SubagentIdentity("#58a6ff"),
    "general": SubagentIdentity("#f0883e"),
    "debugger": SubagentIdentity("#f47067"),
    "verifier": SubagentIdentity("#79c0ff"),
    "code-reviewer": SubagentIdentity("#db61a2"),
    "dependency-scout": SubagentIdentity("#8c959f"),
}

_FALLBACK_COLORS: tuple[str, ...] = (
    "#58a6ff",
    "#f0883e",
    "#db61a2",
    "#39c5cf",
    "#f47067",
    "#d2a8ff",
)


def subagent_identity(name: str) -> SubagentIdentity:
    """Identity for ``name`` (built-in aliases resolved, e.g. explore→explorer)."""
    clean = str(name or "").strip().lower()
    try:
        from ...subagents import canonical_subagent_name

        clean = canonical_subagent_name(clean) or clean
    except Exception:  # noqa: BLE001 - identity lookup must never raise
        pass
    known = _BUILTIN_IDENTITIES.get(clean)
    if known is not None:
        return known
    digest = zlib.crc32(clean.encode("utf-8", "replace"))
    return SubagentIdentity(_FALLBACK_COLORS[digest % len(_FALLBACK_COLORS)])


__all__ = ["SubagentIdentity", "subagent_identity"]
