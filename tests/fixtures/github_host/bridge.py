"""Provider-free test bridge using production tool assembly and host-action transport.

Run with PYTHONPATH=src python tests/fixtures/github_host/bridge.py. Send ordinary
initialize/session.create/chat.send JSONL requests; chat message is JSON containing
{"tool": "github_repo_read", "arguments": {}}. It does not generate model requests.
The connected host still owns all GitHub side effects and approval. Tests must use
a fake host for writes; this is an explicit fixture, never a production launcher.
"""

from __future__ import annotations

import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from alysis_code.agent.tools_assembly import build_tools
from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.session_store import SessionStore


class FixtureSession:
    def __init__(self, **kwargs: Any) -> None:
        self.surface = kwargs["surface"]
        self.root = Path(kwargs["root"])
        self.store = SessionStore(
            enabled=False,
            sessions_dir=self.root / ".github-host-fixture",
            session_id=kwargs["session_id_override"],
            cwd=str(self.root),
            repo_root=str(self.root),
        )
        self.store.session_artifact_root.mkdir(parents=True, exist_ok=True)
        self.tools = build_tools(
            root=self.root,
            console=None,
            store=self.store,
            mode=kwargs["mode"],
            yes=True,
            cfg=kwargs["cfg"],
            surface=self.surface,
            host_action_handler=kwargs["host_action_handler"],
            host_action_capabilities=kwargs["host_action_capabilities"],
            host_mcp=kwargs.get("host_mcp"),
            google_mcp=kwargs.get("google_mcp"),
        )

    def run_turn(self, message: str) -> int:
        request = json.loads(message)
        requests = request if isinstance(request, list) else [request]
        for item in requests:
            tool_name = item["tool"]
            if not isinstance(tool_name, str) or not tool_name.startswith(
                ("github_", "mcp__github__", "mcp__google_")
            ):
                raise ValueError("Fixture only executes negotiated plugin tools")
            result = self.tools[tool_name].run(item["arguments"])
            self.surface.emit_message_end(json.dumps(result))
        return 0

    def close(self) -> None:
        pass


def main() -> int:
    stdio_bridge.load_config = lambda: AppConfig(model="github-host-fixture")
    protocol_stdin, protocol_stdout = stdio_bridge.claim_protocol_streams()
    bridge = stdio_bridge.StdioBridge(
        stdin=protocol_stdin,
        stdout=protocol_stdout,
        create_session_fn=FixtureSession,
    )
    with redirect_stdout(sys.stderr):
        return bridge.run()


if __name__ == "__main__":
    raise SystemExit(main())
