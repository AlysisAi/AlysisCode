from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


def _response(content: str, *, model: str = "qa-mock-model") -> dict[str, Any]:
    return {
        "id": "chatcmpl-qa",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
    }


def _tool_response(
    name: str, arguments: dict[str, Any], *, model: str = "qa-mock-model"
) -> dict[str, Any]:
    return _tool_response_many(((name, arguments),), model=model)


def _tool_response_many(
    calls: tuple[tuple[str, dict[str, Any]], ...],
    *,
    model: str = "qa-mock-model",
) -> dict[str, Any]:
    return {
        "id": "chatcmpl-qa-tool",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"call_qa_{index}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                        for index, (name, arguments) in enumerate(calls, start=1)
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 18, "completion_tokens": 6, "total_tokens": 24},
    }


# The agent appends call-varying host context as *user*-role messages after the real prompt so
# reusable history stays a stable prompt-cache prefix (see
# ``_VOLATILE_HOST_CONTEXT_PREFIXES`` in ``alysis_code/agent/llm_calls.py``). They are not the
# user's request, so "last user text" must look past them or every keyword branch below sees
# ``<environment_context>`` instead of the prompt and falls through to the generic reply.
_VOLATILE_HOST_CONTEXT_PREFIXES = (
    "<workspace_binding_context>",
    "<task_brief>",
    "<environment_context>",
    "<subagent_context>",
)


def _user_message_text(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return None


def _is_volatile_host_context(text: str) -> bool:
    return text.lstrip().startswith(_VOLATILE_HOST_CONTEXT_PREFIXES)


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    fallback: str | None = None
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = _user_message_text(message)
        if text is None:
            continue
        if _is_volatile_host_context(text):
            if fallback is None:
                fallback = text
            continue
        return text
    return fallback or ""


def _has_tool_result(messages: list[dict[str, Any]]) -> bool:
    return any(message.get("role") == "tool" for message in messages)


def _has_tool_call(messages: list[dict[str, Any]], tool_name: str) -> bool:
    for message in messages:
        for tool_call in message.get("tool_calls") or ():
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if isinstance(function, dict) and function.get("name") == tool_name:
                return True
    return False


def _has_completion_gate_nudge(messages: list[dict[str, Any]], marker: str) -> bool:
    normalized_marker = marker.casefold()
    for message in messages:
        if message.get("role") != "system":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        normalized = content.casefold()
        is_nudge = (
            "completion gate:" in normalized
            or "finalization check - one pass before you finish:" in normalized
        )
        marker_present = normalized_marker in normalized or (
            normalized_marker == "git_diff" and "reviewing the current diff" in normalized
        )
        if is_nudge and marker_present:
            return True
    return False


def _all_message_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
        for tool_call in message.get("tool_calls") or ():
            if isinstance(tool_call, dict):
                function = tool_call.get("function")
                if isinstance(function, dict):
                    parts.append(str(function.get("name") or ""))
                    parts.append(str(function.get("arguments") or ""))
    return "\n".join(parts)


def _raw_agent_proxy_response(
    *,
    messages: list[dict[str, Any]],
    text: str,
) -> dict[str, Any] | None:
    lowered = text.lower()
    if "subagent_bench_m01" in lowered:
        is_child = "<subagent_turn_context>" not in lowered
        if not is_child:
            if _has_tool_call(messages, "subagent_run"):
                return _response("Explorer read README.md and reported its contents.")
            return _tool_response(
                "subagent_run",
                {
                    "name": "explorer",
                    "task": "SUBAGENT_BENCH_M01 read existing README.md and report its contents.",
                },
            )
        if _has_tool_call(messages, "fs_read"):
            return _response("README.md contents: `# Smoke`")
        return _tool_response("fs_read", {"path": "README.md"})
    if "read existing readme.md and report its contents." in lowered and _has_tool_call(
        messages, "fs_read"
    ):
        return _response("README.md contents: `# Smoke`")
    if "subagent_bench_m03" in lowered:
        is_child = "<subagent_turn_context>" not in lowered
        if not is_child:
            if _has_tool_call(messages, "subagent_run"):
                return _response("Implementer completed and verified the requested files.")
            return _tool_response(
                "subagent_run",
                {
                    "name": "general",
                    "task": (
                        "SUBAGENT_BENCH_M03 update src/a.py and tests/test_a.py, "
                        "run focused tests, and report changed files."
                    ),
                },
            )
        if _has_tool_call(messages, "verify_run"):
            return _response(
                "Implemented src/a.py and tests/test_a.py; the focused verification passed."
            )
        if _has_tool_call(messages, "fs_write"):
            return _tool_response("verify_run", {})
        return _tool_response_many(
            (
                _write_call(
                    "src/a.py",
                    "def add(left: int, right: int) -> int:\n    return left + right\n",
                ),
                _write_call(
                    "tests/test_a.py",
                    (
                        "from src.a import add\n\n\n"
                        "def test_add() -> None:\n"
                        "    assert add(2, 3) == 5\n"
                    ),
                ),
            )
        )
    if "subagent_bench_m04_child_" in lowered and not _has_tool_call(messages, "subagent_run"):
        if _has_tool_call(messages, "fs_read"):
            return _response("Readonly inspection completed with file-backed evidence.")
        path = "src/app.py" if "child_beta" in lowered else "README.md"
        return _tool_response(
            "fs_read",
            {"path": path, "start_line": 1, "end_line": 40},
        )
    if "subagent_bench_m04" in lowered:
        if _has_tool_call(messages, "subagent_run"):
            return _response(
                "README.md identifies the benchmark repository; src/app.py defines main()."
            )
        return _tool_response_many(
            (
                (
                    "subagent_run",
                    {
                        "name": "explorer",
                        "task": "SUBAGENT_BENCH_M04_CHILD_ALPHA inspect README.md; do not edit.",
                    },
                ),
                (
                    "subagent_run",
                    {
                        "name": "code-reviewer",
                        "task": "SUBAGENT_BENCH_M04_CHILD_BETA inspect src/app.py; do not edit.",
                    },
                ),
            )
        )
    if "raw_proxy_force_verify" in lowered:
        if _has_tool_call(messages, "verify_run"):
            return _response("Done. Forced verification was completed.")
        if _has_tool_call(messages, "fs_edit") or _has_tool_call(messages, "fs_write"):
            if _has_completion_gate_nudge(messages, "verify_run"):
                return _tool_response("verify_run", {})
            return _response("Implemented the marker. Ready to finish.")
        return _tool_response(
            "fs_edit",
            {
                "path": "src/calc.py",
                "edits": [
                    {
                        "op": "append",
                        "content": "\n# forced verify marker\n",
                    }
                ],
            },
        )
    if "raw_proxy_force_diff" in lowered:
        if _has_tool_call(messages, "git_diff"):
            return _response("Done. Verification passed and diff was reviewed.")
        if _has_tool_call(messages, "verify_run"):
            if _has_completion_gate_nudge(messages, "git_diff"):
                return _tool_response("git_diff", {})
            return _response("Verification passed. Ready to finish.")
        if _has_tool_call(messages, "fs_edit") or _has_tool_call(messages, "fs_write"):
            return _tool_response("verify_run", {})
        return _tool_response(
            "fs_edit",
            {
                "path": "README.md",
                "edits": [
                    {
                        "op": "append",
                        "content": "\nforced diff marker\n",
                    }
                ],
            },
        )
    if "write file" not in lowered or "run the configured verification" not in lowered:
        return None
    if _has_tool_call(messages, "verify_run"):
        return _response("Done. Verified raw proxy write.")
    if _has_tool_call(messages, "fs_write"):
        return _tool_response("verify_run", {})
    return None


def _router_response(text: str) -> dict[str, Any] | None:
    if "You classify one user message for a coding assistant." not in text:
        return None
    return {
        "route": "repo",
        "execution_posture": "execute",
        "confidence": 0.95,
        "reply": "",
        "language": "English",
        "script": "Latin",
        "explicit_language_override": False,
        "tool_family": "none",
        "tool_candidates": [],
    }


def _write_call(path: str, content: str) -> tuple[str, dict[str, Any]]:
    return ("fs_write", {"path": path, "content": content})


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/v1/models":
            auth = self.headers.get("Authorization", "")
            if "bad" in auth:
                self._send_json(401, {"error": {"message": "invalid mock key"}})
                return
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": "gpt-4o-mini"},
                        {"id": "qa-mock-model"},
                        {"id": "claude-sonnet-4-6"},
                    ],
                },
            )
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/v1/chat/completions":
            self._send_json(404, {"error": {"message": "not found"}})
            return
        auth = self.headers.get("Authorization", "")
        if "bad" in auth:
            self._send_json(401, {"error": {"message": "invalid mock key"}})
            return
        length = int(self.headers.get("Content-Length") or "0")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": {"message": "bad json"}})
            return
        self.server.requests.append(payload)
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        user_text = _last_user_text(messages).lower()
        all_text = _all_message_text(messages)
        if (
            "subagent_bench_m04_child_" in all_text.lower()
            and not _has_tool_call(messages, "subagent_run")
            and not _has_tool_call(messages, "fs_read")
        ):
            child_label = "beta" if "child_beta" in all_text.lower() else "alpha"
            with self.server.benchmark_parallel_lock:
                self.server.benchmark_parallel_arrivals.add(child_label)
                if len(self.server.benchmark_parallel_arrivals) == 2:
                    self.server.benchmark_parallel_event.set()
            reached_rendezvous = self.server.benchmark_parallel_event.wait(timeout=5.0)
            with self.server.benchmark_parallel_lock:
                self.server.benchmark_parallel_rendezvous[child_label] = reached_rendezvous
        if "qa_429" in user_text:
            self._send_json(429, {"error": {"message": "rate limit exceeded"}})
            return
        if "qa_500" in user_text:
            self._send_json(500, {"error": {"message": "mock upstream failed"}})
            return
        if "qa_reject_tool_choice" in user_text and "tool_choice" in payload:
            self._send_json(
                400,
                {
                    "error": {
                        "message": "Thinking mode does not support this tool_choice",
                        "param": "tool_choice",
                    }
                },
            )
            return
        if "qa_reject_tools" in user_text and payload.get("tools"):
            self._send_json(
                400,
                {
                    "error": {
                        "message": (
                            "The requested tools are not supported for this model's "
                            "function calling request."
                        ),
                        "param": "tools",
                    }
                },
            )
            return
        router_payload = _router_response(all_text)
        if router_payload is not None:
            self._send_json(200, _response(json.dumps(router_payload, sort_keys=True)))
            return
        raw_agent_payload = _raw_agent_proxy_response(messages=messages, text=all_text)
        if raw_agent_payload is not None:
            self._send_json(200, raw_agent_payload)
            return
        if _has_tool_result(messages):
            self._send_json(200, _response("Done. Tool completed and result was inspected."))
            return
        if "non-existent" in user_text or "missing file" in user_text:
            self._send_json(200, _tool_response("fs_read", {"path": "missing.txt"}))
            return
        if "long file" in user_text or "100kb" in user_text:
            self._send_json(200, _tool_response("fs_read", {"path": "long.txt", "max_bytes": 1200}))
            return
        if "read existing" in user_text or "readme" in user_text or "list files" in user_text:
            self._send_json(200, _tool_response("fs_read", {"path": "README.md"}))
            return
        if "write file" in user_text:
            self._send_json(
                200, _tool_response("fs_write", {"path": "qa_written.txt", "content": "qa write\n"})
            )
            return
        if "failing command" in user_text:
            self._send_json(
                200, _tool_response("shell_run", {"cmd": "sh -lc 'echo qa-fail >&2; exit 7'"})
            )
            return
        if "echo" in user_text or "shell" in user_text:
            self._send_json(200, _tool_response("shell_run", {"cmd": "echo qa-smoke"}))
            return
        if "subagent" in user_text or "explorer" in user_text:
            self._send_json(
                200,
                _tool_response(
                    "subagent_run", {"name": "explorer", "task": "Map the repository briefly."}
                ),
            )
            return
        self._send_json(
            200, _response("Done. This is a concise mock assistant response for QA visual review.")
        )


class _Server(ThreadingHTTPServer):
    requests: list[dict[str, Any]]
    benchmark_parallel_arrivals: set[str]
    benchmark_parallel_rendezvous: dict[str, bool]
    benchmark_parallel_event: threading.Event
    benchmark_parallel_lock: threading.Lock


@dataclass
class MockLLMServer:
    host: str = "127.0.0.1"
    port: int = 0

    def __post_init__(self) -> None:
        self._server = _Server((self.host, self.port), _Handler)
        self._server.requests = []
        self._server.benchmark_parallel_arrivals = set()
        self._server.benchmark_parallel_rendezvous = {}
        self._server.benchmark_parallel_event = threading.Event()
        self._server.benchmark_parallel_lock = threading.Lock()
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self._server.requests

    @property
    def benchmark_parallel_rendezvous(self) -> dict[str, bool]:
        with self._server.benchmark_parallel_lock:
            return dict(self._server.benchmark_parallel_rendezvous)

    def start(self) -> MockLLMServer:
        self._thread.start()
        # Give the server a tiny deterministic startup window before subprocesses connect.
        time.sleep(0.02)
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def __enter__(self) -> MockLLMServer:
        return self.start()

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.stop()
