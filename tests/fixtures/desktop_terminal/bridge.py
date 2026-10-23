"""Provider-free terminal acceptance bridge; production assembly and transport.

Test chat messages explicitly select a tool. Never used by the app launcher.
"""

import json
import runpy
from pathlib import Path

fixture = runpy.run_path(str(Path(__file__).parents[1] / "github_host" / "bridge.py"))


class TerminalSession(fixture["FixtureSession"]):
    def run_turn(self, message: str) -> int:
        request = json.loads(message)
        name = request["tool"]
        if not isinstance(name, str) or not name.startswith("desktop_terminal_"):
            raise ValueError("Only desktop terminal tools are allowed in this fixture")
        result = self.tools[name].run(request["arguments"])
        self.surface.emit_message_end(json.dumps(result))
        return 0


fixture["main"].__globals__["FixtureSession"] = TerminalSession
raise SystemExit(fixture["main"]())
