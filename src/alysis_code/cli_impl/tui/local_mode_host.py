"""Adapters for reserved local planning/integration actions in the live TUI."""

from concurrent.futures import Future
from contextvars import copy_context
from dataclasses import dataclass
from pathlib import Path

from ...mcp.config import load_resolved_mcp_config
from .integration_inspector import ActionOutcome, IntegrationController


@dataclass
class PendingLocalIntegration:
    future: Future

    def cancel_pending(self):
        return self.future.cancel()


class ReservedIntegrationDispatcher:
    def __init__(self, lifecycle, session_provider, owner):
        self.lifecycle, self.session_provider, self.owner = lifecycle, session_provider, owner

    def submit(self, work, *, started, completed):
        lifecycle = self.lifecycle
        session = self.session_provider()
        if lifecycle is None or session is not self.owner:
            raise RuntimeError("Live session reservation unavailable; reopen the inspector.")
        identity = lifecycle.identity_for(session)

        def run():
            if self.session_provider() is not session:
                return ActionOutcome("cancelled", "Session changed; reopen the inspector.")
            started()
            return work()

        owner_context = copy_context()
        future = lifecycle.submit_local(identity, lambda: owner_context.run(run))

        def done(result):
            if result.cancelled():
                return
            try:
                outcome = result.result()
            except Exception:
                outcome = ActionOutcome(
                    "failed",
                    "Integration change refused or failed. Refresh; check session state and configuration.",
                )
            completed(outcome)

        future.add_done_callback(done)
        return PendingLocalIntegration(future)


def integration_controller(session_provider, workspace: Path):
    """Read the existing session manager dynamically; never construct a connection."""
    return IntegrationController(
        repo_root=workspace,
        manager_provider=lambda: getattr(session_provider(), "mcp_manager", None),
        configured_servers_provider=lambda: (
            load_resolved_mcp_config(workspace_root=workspace).servers
        ),
    )
