"""Sessionless desktop settings and composer reads, using the engine's management handlers."""

from __future__ import annotations

import sys
from contextlib import redirect_stdout
from typing import Any, TextIO

from .. import __version__
from .. import computer_use_attribution as attribution
from ..computer_use_platform_policy import computer_use_blocked
from .management_common import dispatch_management_handler
from .management_settings import SETTINGS_HANDLERS
from .parent_watchdog import start_parent_watchdog
from .protocol import (
    EXPLICIT_SKILL_INVOCATION,
    PROTOCOL_VERSION,
    ProtocolError,
    ProtocolRequest,
    dumps_message,
    error_message,
    parse_request_line,
    reject_inline_secrets,
    response_message,
)
from .stdio_transport import JsonLineBridge, claim_protocol_streams


class ManagementBridge(JsonLineBridge):
    def __init__(self, stdin: TextIO, stdout: TextIO) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def _write(self, payload: dict[str, Any]) -> None:
        self.stdout.write(dumps_message(payload))
        self.stdout.flush()

    def _dispatch(self, request: ProtocolRequest) -> dict[str, Any]:
        if request.method in {"initialize", "health", "getCapabilities"}:
            capabilities = {
                "protocol_version": PROTOCOL_VERSION,
                "transport": "stdio-jsonl",
                "methods": [
                    "initialize",
                    "health",
                    "getCapabilities",
                    "bridge.shutdown",
                    *SETTINGS_HANDLERS,
                ],
                "events": [],
                "modes": [],
                "features": {"explicit_skill_invocation": dict(EXPLICIT_SKILL_INVOCATION)},
            }
            if not computer_use_blocked():
                capabilities["features"]["computer_use_attribution_v1"] = {
                    "version": 1,
                    "supported": True,
                }
            return (
                capabilities
                if request.method == "getCapabilities"
                else {
                    "ok": True,
                    "name": "alysis-ide-bridge",
                    "alysis_version": __version__,
                    "protocol_version": PROTOCOL_VERSION,
                    "capabilities": capabilities,
                }
            )
        elif request.method == "bridge.shutdown":
            self.close()
            return {"status": "shutting_down"}
        else:
            return dispatch_management_handler(
                request.method,
                request.params,
                request.id,
                SETTINGS_HANDLERS.get(request.method),
            )

    def process_line(self, line: str) -> None:
        request_id = None
        collector = None
        try:
            request = parse_request_line(line)
            request_id = request.id
            reject_inline_secrets(request.params, request_id=request_id)
            collector = attribution.management_rpc_collector(request)
            with attribution.bind_collector(collector):
                result = self._dispatch(request)
            reply = response_message(request_id, result)
        except ProtocolError as exc:
            reply = error_message(
                exc.request_id if exc.request_id is not None else request_id, exc.code, exc.message
            )
        except Exception:
            reply = error_message(
                request_id,
                "internal_error",
                "The IDE bridge encountered an unexpected internal error.",
            )
        self._write(attribution.rpc_reply(collector, reply) or reply)


def run_management_bridge() -> int:
    stdin, stdout = claim_protocol_streams()
    with redirect_stdout(sys.stderr):
        bridge = ManagementBridge(stdin, stdout)
        start_parent_watchdog(grace_seconds=2.0, on_parent_exit=lambda _pid: bridge.close())
        return bridge.run()
