"""Mutable view-model for the full-screen TUI.

Holds only what the footer/header need to render. The footer HUD fields
(``context_pct`` / ``tokens`` / ``cost_usd``) are wired live: ``loop.py`` seeds
them when the session is built and refreshes them after each turn (and mid-turn,
throttled) from the session's usage summary + context cache. Other fields are
toggled by the user via Tab (persona) / Shift+Tab (execution mode).

``exec_mode`` mirrors ``session.mode`` and is the single authority for what the
agent is allowed to do without asking. ``pending_exec_mode`` is only the user's
selection for the next turn: it never grants capabilities until the chat host
activates it immediately before a real user message starts. There is deliberately
no second approval-policy flag here: an independent "auto-approve" toggle could
silently answer every gate that ``review`` mode raised, leaving the footer
advertising a guarded mode while the session behaved like ``fullaccess``.
Planning posture is likewise not a separate flag: it is the ``architect``
persona (``persona``), which narrows the execution mode and write scope through
the persona clamp.
"""

from __future__ import annotations

from dataclasses import dataclass

# Shift+Tab cycles the execution mode in this order (wrapping). This is the same
# set the /permissions picker offers, kept in escalating-capability order so the cycle
# reads as "loosen the guards one notch" rather than an arbitrary rotation.
# ``fullaccess`` is included deliberately — the caller echoes its warning every
# time the cycle lands there, so reaching the unguarded mode is never silent.
EXEC_MODE_CYCLE: tuple[str, ...] = ("readonly", "review", "auto", "fullaccess")


def next_exec_mode(current: str) -> str:
    """Return the execution mode one step along :data:`EXEC_MODE_CYCLE`.

    An unknown or empty ``current`` starts the cycle at its first entry, so a
    session whose mode has not been synced yet still advances predictably
    instead of raising.
    """
    normalized = str(current or "").strip().lower()
    try:
        index = EXEC_MODE_CYCLE.index(normalized)
    except ValueError:
        return EXEC_MODE_CYCLE[0]
    return EXEC_MODE_CYCLE[(index + 1) % len(EXEC_MODE_CYCLE)]


@dataclass
class TuiState:
    model_name: str = ""
    # Non-empty while the shell is available but model calls are not (for
    # example, a selected subscription still needs browser authentication).
    connection_status: str = ""
    tokens: int = 0
    tokens_estimated: bool = False
    # Session cost. ``None`` means pricing is unknown (an unmetered/free model with
    # real usage) — the footer renders that as "n/a" rather than a misleading
    # "$0.0000". A literal 0.0 means nothing has been spent yet.
    cost_usd: float | None = 0.0
    cost_unknown_calls: int = 0  # calls whose cost couldn't be metered (footer "+N")
    # Preformatted billing status (for example ``subscription`` or ``~$0.0123``).
    # Empty retains the legacy numeric fallback for callers that do not have a
    # UsageSummary available yet.
    cost_display: str = ""
    exec_mode: str = ""  # execution mode: review | auto | readonly | fullaccess
    # A user-selected execution mode that will become active at the next real
    # message boundary. Empty means there is no pending change. It is display
    # state only; ``exec_mode`` remains the active permission authority.
    pending_exec_mode: str = ""
    # Active persona (code|architect|ask|debug); "" or "code" renders no
    # persona badge — the execution-mode badge alone stays authoritative.
    persona: str = ""
    # Forge: True while the user is inside a Forge planning session (ui_mode ==
    # "forge"); drives the footer FORGE badge + the forge-specific placeholder.
    forge_mode: bool = False
    forge_run_id: str = ""  # short run id shown in the FORGE badge, e.g. "run-1a2b"
    # Name of the subagent currently running through auto-delegation; drives
    # the footer badge so the user knows nested work is active. Empty otherwise.
    active_subagent: str = ""
    # Every concurrently running subagent, in start order. ``active_subagent``
    # remains the compatibility view of the most recently started survivor.
    active_subagents: tuple[str, ...] = ()
    username: str = ""
    workspace: str = ""  # short display form, e.g. "~/coder-plugin-install"
    branch: str = ""  # git branch name, e.g. "feat/tui-rebuild"
    usage_hud_enabled: bool = True
    # Conversation headroom %, seeded from the session before first paint.
    # ``None`` means "not measured yet" so a failed compute reads n/a in the
    # footer instead of a fabricated 100%.
    context_pct: float | None = None


__all__ = [
    "TuiState",
    "EXEC_MODE_CYCLE",
    "next_exec_mode",
]
