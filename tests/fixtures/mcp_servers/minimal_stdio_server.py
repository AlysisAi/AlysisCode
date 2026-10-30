from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any


def _load_config() -> dict[str, Any]:
    config_path = os.environ.get("ALYSIS_TEST_MCP_CONFIG")
    if not config_path:
        return {}
    return json.loads(Path(config_path).read_text(encoding="utf-8"))


def _write_json_line(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _write_json_lines(payloads: list[dict[str, Any]]) -> None:
    for payload in payloads:
        sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _write_json_burst(payloads: list[dict[str, Any]]) -> None:
    burst = "".join(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n" for payload in payloads
    )
    sys.stdout.write(burst)
    sys.stdout.flush()


def _write_stderr_lines(lines: list[str]) -> None:
    for line in lines:
        sys.stderr.write(str(line) + "\n")
    sys.stderr.flush()


def _sleep_for(config: dict[str, Any], key: str) -> None:
    method_delays = config.get("method_delays")
    if not isinstance(method_delays, dict):
        return
    value = method_delays.get(key)
    if value is None:
        return
    time.sleep(float(value))


def _initialize_result(config: dict[str, Any]) -> dict[str, Any]:
    capabilities = config.get("capabilities")
    if not isinstance(capabilities, dict):
        capabilities = {"tools": {}}
    server_info = config.get("server_info")
    if not isinstance(server_info, dict):
        server_info = {"name": "fixture-stdio", "version": "0.0.1"}
    return {
        "protocolVersion": str(config.get("protocol_version") or "2024-11-05"),
        "capabilities": capabilities,
        "serverInfo": server_info,
        **({"instructions": config["instructions"]} if "instructions" in config else {}),
    }


def _tools_page(
    config: dict[str, Any], cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    pages = config.get("tools_pages")
    if not isinstance(pages, list) or not pages:
        return [], None
    if cursor is None:
        index = 0
    else:
        if not cursor.startswith("page:"):
            return [], None
        index = int(cursor.split(":", 1)[1])
    raw_page = pages[index]
    if not isinstance(raw_page, list):
        raise RuntimeError("tools_pages entries must be arrays.")
    next_cursor = None
    if index + 1 < len(pages):
        next_cursor = f"page:{index + 1}"
    return [dict(item) for item in raw_page if isinstance(item, dict)], next_cursor


def _tool_call_result(config: dict[str, Any], tool_name: str) -> dict[str, Any]:
    results = config.get("tool_call_results")
    if not isinstance(results, dict):
        results = {}
    raw = results.get(tool_name)
    if isinstance(raw, dict):
        return dict(raw)
    return {
        "isError": False,
        "structuredContent": {"tool": tool_name, "ok": True},
        "content": [{"type": "text", "text": f"tool:{tool_name}:ok"}],
    }


def _resources_page(
    config: dict[str, Any], cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    pages = config.get("resources_pages")
    if not isinstance(pages, list) or not pages:
        return [], None
    if cursor is None:
        index = 0
    else:
        if not cursor.startswith("page:"):
            return [], None
        index = int(cursor.split(":", 1)[1])
    raw_page = pages[index]
    if not isinstance(raw_page, list):
        raise RuntimeError("resources_pages entries must be arrays.")
    next_cursor = None
    if index + 1 < len(pages):
        next_cursor = f"page:{index + 1}"
    return [dict(item) for item in raw_page if isinstance(item, dict)], next_cursor


def _resource_read_result(config: dict[str, Any], resource_uri: str) -> dict[str, Any]:
    results = config.get("resource_read_results")
    if not isinstance(results, dict):
        results = {}
    raw = results.get(resource_uri)
    if isinstance(raw, dict):
        return dict(raw)
    return {
        "contents": [
            {
                "uri": resource_uri,
                "mimeType": "text/plain",
                "text": f"resource:{resource_uri}",
            }
        ]
    }


def _prompts_page(
    config: dict[str, Any], cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    pages = config.get("prompts_pages")
    if not isinstance(pages, list) or not pages:
        return [], None
    if cursor is None:
        index = 0
    else:
        if not cursor.startswith("page:"):
            return [], None
        index = int(cursor.split(":", 1)[1])
    raw_page = pages[index]
    if not isinstance(raw_page, list):
        raise RuntimeError("prompts_pages entries must be arrays.")
    next_cursor = None
    if index + 1 < len(pages):
        next_cursor = f"page:{index + 1}"
    return [dict(item) for item in raw_page if isinstance(item, dict)], next_cursor


def _prompt_get_result(config: dict[str, Any], prompt_name: str) -> dict[str, Any]:
    results = config.get("prompt_get_results")
    if not isinstance(results, dict):
        results = {}
    raw = results.get(prompt_name)
    if isinstance(raw, dict):
        return dict(raw)
    return {
        "name": prompt_name,
        "messages": [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": f"prompt:{prompt_name}",
                },
            }
        ],
    }


def _server_request_payload(config: dict[str, Any]) -> dict[str, Any]:
    method = str(config.get("unexpected_request_method") or "roots/list")
    return {
        "jsonrpc": "2.0",
        "id": "server-request-1",
        "method": method,
        "params": {},
    }


def _tools_list_changed_notification() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "notifications/tools/list_changed",
        "params": {},
    }


def _resources_list_changed_notification() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "notifications/resources/list_changed",
        "params": {},
    }


def _prompts_list_changed_notification() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "notifications/prompts/list_changed",
        "params": {},
    }


def _record_client_response(config: dict[str, Any], payload: dict[str, Any]) -> None:
    output_path = config.get("record_client_responses_path")
    if not isinstance(output_path, str) or not output_path.strip():
        return
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")


def _record_client_request(config: dict[str, Any], payload: dict[str, Any]) -> None:
    output_path = config.get("record_client_requests_path")
    if not isinstance(output_path, str) or not output_path.strip():
        return
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")


def _emit_post_response_events(
    *,
    config: dict[str, Any],
    key: str,
    state: dict[str, Any],
) -> None:
    raw_events = config.get(key)
    if raw_events is None:
        return
    if not isinstance(raw_events, list):
        raise RuntimeError(f"{key} must be a list.")
    for raw_event in raw_events:
        if not isinstance(raw_event, dict):
            raise RuntimeError(f"{key} entries must be objects.")
        delay_s = raw_event.get("delay_s")
        if delay_s is not None:
            time.sleep(float(delay_s))
        kind = str(raw_event.get("kind") or "")
        if kind == "tools_list_changed":
            if state.get("tools_list_changed_sent"):
                continue
            _write_json_line(_tools_list_changed_notification())
            state["tools_list_changed_sent"] = True
            continue
        if kind == "resources_list_changed":
            if state.get("resources_list_changed_sent"):
                continue
            _write_json_line(_resources_list_changed_notification())
            state["resources_list_changed_sent"] = True
            continue
        if kind == "prompts_list_changed":
            if state.get("prompts_list_changed_sent"):
                continue
            _write_json_line(_prompts_list_changed_notification())
            state["prompts_list_changed_sent"] = True
            continue
        if kind == "unexpected_request":
            if state.get("unexpected_request_sent"):
                continue
            _write_json_line(_server_request_payload(config))
            state["unexpected_request_sent"] = True
            continue
        raise RuntimeError(f"Unsupported post-response event kind: {kind!r}")


def _handle_request(
    *,
    config: dict[str, Any],
    payload: dict[str, Any],
    state: dict[str, Any],
) -> None:
    method = str(payload.get("method") or "")
    request_id = payload.get("id")
    params = payload.get("params")
    if method == "initialize":
        _sleep_for(config, "initialize")
        _write_json_line(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _initialize_result(config),
            }
        )
        return
    if method == "tools/list":
        _sleep_for(config, "tools/list")
        cursor = None
        if isinstance(params, dict) and isinstance(params.get("cursor"), str):
            cursor = params["cursor"]
        tools, next_cursor = _tools_page(config, cursor)
        result: dict[str, Any] = {"tools": tools}
        if next_cursor is not None:
            result["nextCursor"] = next_cursor
        response_payload = {"jsonrpc": "2.0", "id": request_id, "result": result}
        if bool(config.get("burst_unexpected_request_after_list_response")):
            _write_json_burst([response_payload, _server_request_payload(config)])
            state["unexpected_request_sent"] = True
        elif bool(config.get("burst_tools_list_changed_after_list")):
            _write_json_burst(
                [
                    response_payload,
                    _tools_list_changed_notification(),
                ]
            )
            state["tools_list_changed_sent"] = True
        elif bool(config.get("duplicate_tools_list_response")):
            _write_json_lines([response_payload, response_payload])
        elif bool(config.get("unexpected_request_after_list_response")):
            _write_json_lines([response_payload, _server_request_payload(config)])
            state["unexpected_request_sent"] = True
        else:
            _write_json_line(response_payload)
        _emit_post_response_events(
            config=config,
            key="post_tools_list_response_events",
            state=state,
        )
        if bool(config.get("send_tools_list_changed_after_list")) and not state.get(
            "tools_list_changed_sent"
        ):
            _write_json_line(_tools_list_changed_notification())
            state["tools_list_changed_sent"] = True
        return
    if method == "tools/call":
        tool_name = ""
        if isinstance(params, dict):
            tool_name = str(params.get("name") or "")
        _sleep_for(config, f"tools/call/{tool_name}")
        _sleep_for(config, "tools/call")
        response_payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": _tool_call_result(config, tool_name),
        }
        if bool(config.get("burst_unexpected_request_after_call_response")):
            _write_json_burst([response_payload, _server_request_payload(config)])
            state["unexpected_request_sent"] = True
        elif bool(config.get("burst_tools_list_changed_after_call")):
            _write_json_burst(
                [
                    response_payload,
                    _tools_list_changed_notification(),
                ]
            )
            state["tools_list_changed_sent"] = True
        elif bool(config.get("unexpected_request_after_call_response")):
            _write_json_lines([response_payload, _server_request_payload(config)])
            state["unexpected_request_sent"] = True
        else:
            _write_json_line(response_payload)
        _emit_post_response_events(
            config=config,
            key="post_tools_call_response_events",
            state=state,
        )
        if bool(config.get("send_tools_list_changed_after_call")) and not state.get(
            "tools_list_changed_sent"
        ):
            _write_json_line(_tools_list_changed_notification())
            state["tools_list_changed_sent"] = True
        return
    if method == "resources/list":
        _sleep_for(config, "resources/list")
        cursor = None
        if isinstance(params, dict) and isinstance(params.get("cursor"), str):
            cursor = params["cursor"]
        resources, next_cursor = _resources_page(config, cursor)
        result: dict[str, Any] = {"resources": resources}
        if next_cursor is not None:
            result["nextCursor"] = next_cursor
        response_payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        }
        if bool(config.get("burst_resources_list_changed_after_list")):
            _write_json_burst(
                [
                    response_payload,
                    _resources_list_changed_notification(),
                ]
            )
            state["resources_list_changed_sent"] = True
        else:
            _write_json_line(response_payload)
        _emit_post_response_events(
            config=config,
            key="post_resources_list_response_events",
            state=state,
        )
        if bool(config.get("send_resources_list_changed_after_list")) and not state.get(
            "resources_list_changed_sent"
        ):
            _write_json_line(_resources_list_changed_notification())
            state["resources_list_changed_sent"] = True
        return
    if method == "resources/read":
        resource_uri = ""
        if isinstance(params, dict):
            resource_uri = str(params.get("uri") or "")
        _sleep_for(config, f"resources/read/{resource_uri}")
        _sleep_for(config, "resources/read")
        _write_json_line(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _resource_read_result(config, resource_uri),
            }
        )
        return
    if method == "prompts/list":
        _sleep_for(config, "prompts/list")
        cursor = None
        if isinstance(params, dict) and isinstance(params.get("cursor"), str):
            cursor = params["cursor"]
        prompts, next_cursor = _prompts_page(config, cursor)
        result: dict[str, Any] = {"prompts": prompts}
        if next_cursor is not None:
            result["nextCursor"] = next_cursor
        response_payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        }
        if bool(config.get("burst_prompts_list_changed_after_list")):
            _write_json_burst(
                [
                    response_payload,
                    _prompts_list_changed_notification(),
                ]
            )
            state["prompts_list_changed_sent"] = True
        else:
            _write_json_line(response_payload)
        _emit_post_response_events(
            config=config,
            key="post_prompts_list_response_events",
            state=state,
        )
        if bool(config.get("send_prompts_list_changed_after_list")) and not state.get(
            "prompts_list_changed_sent"
        ):
            _write_json_line(_prompts_list_changed_notification())
            state["prompts_list_changed_sent"] = True
        return
    if method == "prompts/get":
        prompt_name = ""
        if isinstance(params, dict):
            prompt_name = str(params.get("name") or "")
        _sleep_for(config, f"prompts/get/{prompt_name}")
        _sleep_for(config, "prompts/get")
        _write_json_line(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _prompt_get_result(config, prompt_name),
            }
        )
        return
    _write_json_line(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32601,
                "message": f"Unknown method: {method}",
            },
        }
    )


def _handle_notification(
    *,
    config: dict[str, Any],
    payload: dict[str, Any],
    state: dict[str, Any],
) -> None:
    method = str(payload.get("method") or "")
    if method != "notifications/initialized":
        return
    delay_after_initialized = config.get("delay_after_initialized_output_s")
    if delay_after_initialized is not None:
        time.sleep(float(delay_after_initialized))
    if bool(config.get("unexpected_request_after_initialized")) and not state.get(
        "unexpected_request_sent"
    ):
        _write_json_line(_server_request_payload(config))
        state["unexpected_request_sent"] = True
    if bool(config.get("send_tools_list_changed_after_initialized")) and not state.get(
        "tools_list_changed_sent"
    ):
        _write_json_line(_tools_list_changed_notification())
        state["tools_list_changed_sent"] = True


def main() -> int:
    config = _load_config()
    stderr_lines = config.get("stderr_lines")
    if isinstance(stderr_lines, list):
        _write_stderr_lines([str(item) for item in stderr_lines])
    stderr_before_malformed = config.get("stderr_lines_before_malformed_stdout")
    if isinstance(stderr_before_malformed, list):
        _write_stderr_lines([str(item) for item in stderr_before_malformed])
    if bool(config.get("malformed_stdout_line_on_start")):
        sys.stdout.write("not-json\n")
        sys.stdout.flush()
    stderr_after_malformed = config.get("stderr_lines_after_malformed_stdout")
    if isinstance(stderr_after_malformed, list):
        _write_stderr_lines([str(item) for item in stderr_after_malformed])

    state: dict[str, Any] = {}
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            continue
        if "method" not in payload and "id" in payload:
            _record_client_response(config, payload)
            continue
        _record_client_request(config, payload)
        if "method" in payload and "id" in payload:
            _handle_request(config=config, payload=payload, state=state)
            continue
        if "method" in payload:
            _handle_notification(config=config, payload=payload, state=state)
            continue
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
