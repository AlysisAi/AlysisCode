"""Opt-in Go subscription audit of real agent context and wire state.

Uses the real factory, AgentSession and fs_read on synthetic files. Credentials
and raw provider reasoning stay in memory. No production routing is replaced.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import secrets
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from rich.console import Console

from alysis_code.agent_loop import AgentSession, ToolDef
from alysis_code.config import AppConfig, load_config, resolve_profile_api_key
from alysis_code.llm.factory import make_llm_client
from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.model_reasoning import model_reasoning_capabilities
from alysis_code.model_registry import ModelRegistry
from alysis_code.profiles import get_profile
from alysis_code.session_store import SessionStore
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.tools.fs import fs_read
from alysis_code.usage_tracker import UsageSummary


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def parse_reply(value):
    text = value.strip()
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def blocks_message(role, blocks):
    if isinstance(blocks, str):
        return {"role": role, "content": blocks}
    result = {"role": role, "content": "", "tool_calls": [], "opaque_blocks": []}
    for block in blocks or []:
        kind = block.get("type")
        if kind in {"text", "output_text", "input_text"}:
            result["content"] += block.get("text", "")
        elif kind == "tool_use":
            result["tool_calls"].append(
                {
                    "id": block.get("id"),
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input")),
                    },
                }
            )
        elif kind in {"thinking", "redacted_thinking"}:
            result["opaque_blocks"].append(block)
    return result


def wire_messages(body, protocol):
    if protocol == "openai_compat":
        return body.get("messages") or []
    if protocol == "anthropic_messages":
        result = []
        for row in body.get("messages") or []:
            blocks = row.get("content")
            if isinstance(blocks, list):
                for block in blocks:
                    if block.get("type") == "tool_result":
                        result.append(
                            {
                                "role": "tool",
                                "tool_call_id": block.get("tool_use_id"),
                                "content": block.get("content"),
                            }
                        )
            result.append(blocks_message(row.get("role"), blocks))
        return result
    result = []
    pending = None
    for item in body.get("input") or []:
        kind = item.get("type")
        if kind in {"reasoning", "function_call"} or item.get("role") == "assistant":
            if pending is None:
                pending = {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [],
                    "opaque_blocks": [],
                }
                result.append(pending)
            if kind == "reasoning":
                pending["opaque_blocks"].append(item)
            elif kind == "function_call":
                pending["tool_calls"].append(
                    {
                        "id": item.get("call_id"),
                        "function": {"name": item.get("name"), "arguments": item.get("arguments")},
                    }
                )
            else:
                pending["content"] += blocks_message("assistant", item.get("content"))["content"]
        else:
            pending = None
            result.append(
                {"role": "tool", "tool_call_id": item.get("call_id"), "content": item.get("output")}
                if kind == "function_call_output"
                else item
            )
    return result


def decode_response(raw, streaming, protocol):
    if not streaming:
        data = json.loads(raw)
        if protocol == "anthropic_messages":
            return data, blocks_message("assistant", data.get("content"))
        if protocol == "openai_responses":
            normalized = wire_messages({"input": data.get("output") or []}, protocol)
            return data, next((m for m in normalized if m.get("role") == "assistant"), {})
        choices = data.get("choices") or []
        return data, (choices[0].get("message") or {}) if choices else {}
    message, calls, usage, response_model, finish = {}, {}, None, None, None
    for line in raw.decode().splitlines():
        if not line.startswith("data:") or line[5:].strip() == "[DONE]":
            continue
        row = json.loads(line[5:])
        usage = row.get("usage") or usage
        response_model = row.get("model") or response_model
        for choice in row.get("choices") or []:
            finish = choice.get("finish_reason") or finish
            delta = choice.get("delta") or {}
            for key in ("content", "reasoning_content", "reasoning"):
                if isinstance(delta.get(key), str):
                    message[key] = message.get(key, "") + delta[key]
            for call in delta.get("tool_calls") or []:
                target = calls.setdefault(call.get("index", 0), {"function": {}})
                for key in ("id", "type"):
                    if call.get(key):
                        target[key] = call[key]
                for key, value in (call.get("function") or {}).items():
                    if isinstance(value, str):
                        target["function"][key] = target["function"].get(key, "") + value
                for key, value in call.items():
                    if key not in {"id", "type", "index", "function"}:
                        target[key] = value
            # Opaque list fragments are retained as returned, not interpreted.
            if delta.get("reasoning_details"):
                message.setdefault("reasoning_details", []).extend(delta["reasoning_details"])
    if calls:
        message["tool_calls"] = list(calls.values())
    return {
        "usage": usage,
        "model": response_model,
        "choices": [{"finish_reason": finish}],
    }, message


class Capture(httpx.BaseTransport):
    def __init__(self, result, save, protocol):
        self.inner = httpx.HTTPTransport(retries=0, trust_env=False)
        self.result, self.save = result, save
        self.protocol = protocol
        self.returned = []
        self.previous_tools = []
        self.markers = {}

    def handle_request(self, request):
        assert request.url.host == "opencode.ai" and request.url.scheme == "https"
        assert request.url.path.startswith(("/zen/go/v1/", "/inference/go/"))
        body = json.loads(request.content)
        messages = wire_messages(body, self.protocol)
        wire_text = json.dumps(messages, ensure_ascii=False)
        call = {
            "ordinal": len(self.result["calls"]) + 1,
            "request_path": request.url.path,
            "request_model": body.get("model"),
            "stream": bool(body.get("stream")),
            "reasoning_effort": body.get("reasoning_effort"),
            "reasoning_control": body.get("reasoning"),
            "markers_present": {k: v in wire_text for k, v in self.markers.items()},
            "prior_tool_messages_preserved": all(m in messages for m in self.previous_tools),
            "message_roles": [m.get("role") for m in messages],
            "request_messages_sha256": digest(messages),
            "replay": [],
        }
        for index, previous in enumerate(self.returned):
            ids = {c.get("id") for c in previous.get("tool_calls") or []}
            matches = [
                m
                for m in messages
                if m.get("role") == "assistant"
                and (
                    bool(ids)
                    and ids == {c.get("id") for c in m.get("tool_calls") or []}
                    or not ids
                    and previous.get("content")
                    and m.get("content") == previous["content"]
                )
            ]
            match = matches[-1] if matches else {}
            fields = {}
            for key in ("reasoning_content", "reasoning", "reasoning_details", "opaque_blocks"):
                value = previous.get(key)
                if value:
                    fields[key] = {
                        "returned_sha256": digest(value),
                        "returned_chars": len(json.dumps(value)),
                        "sent": key in match,
                        "exact": match.get(key) == value,
                    }
                    if isinstance(value, list):
                        sent_values = match.get(key) if isinstance(match.get(key), list) else []
                        fields[key]["components"] = [
                            {
                                "returned_keys": list(item),
                                "sent_keys": list(sent_values[n])
                                if n < len(sent_values) and isinstance(sent_values[n], dict)
                                else [],
                                "field_exact": {
                                    field: n < len(sent_values)
                                    and isinstance(sent_values[n], dict)
                                    and sent_values[n].get(field) == item[field]
                                    for field in item
                                },
                            }
                            for n, item in enumerate(value)
                            if isinstance(item, dict)
                        ]
            extras = []
            sent_calls = {c.get("id"): c for c in match.get("tool_calls") or []}
            for tool_call in previous.get("tool_calls") or []:
                for key, value in tool_call.items():
                    if key not in {"id", "type", "index", "function"}:
                        sent_call = sent_calls.get(tool_call.get("id"), {})
                        extras.append(
                            {
                                "field": key,
                                "sha256": digest(value),
                                "sent": key in sent_call,
                                "exact": sent_call.get(key) == value,
                            }
                        )
            call["replay"].append(
                {
                    "response_ordinal": index + 1,
                    "assistant_present": bool(matches),
                    "tool_calls_present": bool(matches) if ids else None,
                    "reasoning_fields": fields,
                    "tool_call_extras": extras,
                }
            )
        self.previous_tools = [m for m in messages if m.get("role") == "tool"]
        self.result["calls"].append(call)
        self.save()
        started = time.monotonic()
        try:
            response = self.inner.handle_request(request)
            raw = response.read()
            call.update(status=response.status_code, seconds=round(time.monotonic() - started, 3))
            try:
                data, message = decode_response(raw, bool(body.get("stream")), self.protocol)
                call.update(
                    usage=data.get("usage"),
                    response_model=data.get("model"),
                    finish_reasons=[c.get("finish_reason") for c in data.get("choices") or []],
                    native_stop=data.get("stop_reason") or data.get("status"),
                    returned_fields=list(message),
                    returned_tool_extra_fields=[
                        key
                        for item in message.get("tool_calls") or []
                        for key in item
                        if key not in {"id", "type", "index", "function"}
                    ],
                    returned_reasoning={
                        k: {"chars": len(json.dumps(message[k])), "sha256": digest(message[k])}
                        for k in (
                            "reasoning_content",
                            "reasoning",
                            "reasoning_details",
                            "opaque_blocks",
                        )
                        if message.get(k)
                    },
                )
                if response.status_code < 400:
                    self.returned.append(message)
                else:
                    call["provider_error"] = data.get("error")
            except (ValueError, TypeError):
                call["unparsed_response"] = True
            self.save()
            return httpx.Response(
                response.status_code, headers=response.headers, content=raw, request=request
            )
        except Exception as error:
            call.update(
                error_type=type(error).__name__, seconds=round(time.monotonic() - started, 3)
            )
            self.save()
            raise

    def close(self):
        self.inner.close()


def run_case(model, streaming, root, profile, key, evidence, save):
    result = {
        "model": model,
        "protocol": profile.protocol,
        "stream": streaming,
        "calls": [],
        "tool_reads": [],
        "passed": False,
    }
    evidence["cases"].append(result)
    save()
    directory = root / (model + ("-stream" if streaming else "-nonstream"))
    directory.mkdir()
    source_name = f"source-{uuid4().hex[:12]}.json"
    label, note, nonce = (
        "Ελλάδα-" + secrets.token_hex(4),
        "東京-" + secrets.token_hex(4),
        secrets.token_hex(12),
    )
    tag, revision = "request-" + secrets.token_hex(5), "corrected-" + secrets.token_hex(5)
    amounts = [secrets.randbelow(800) + 100 for _ in range(3)]
    manifest = {"source": source_name, "label": label, "audit_nonce": nonce}
    source = {"amounts": amounts, "note": note}
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
    (directory / source_name).write_text(json.dumps(source, ensure_ascii=False))
    expected = {"tag": tag, "label": label, "note": note, "total": sum(amounts)}
    result["expected"] = expected
    capture = Capture(result, save, profile.protocol)
    session = None
    try:
        chosen = replace(
            profile,
            default_model=model,
            reasoning_effort=None,
            extra_headers={
                **profile.extra_headers,
                "x-opencode-session": "alysis-context-" + uuid4().hex,
            },
        )
        cfg = AppConfig(model=model, routing_mode="code_only", stream=streaming)
        cfg.extra_fields = {
            "profiles": {chosen.name: chosen.to_dict()},
            "active_profile": chosen.name,
        }
        capabilities = model_reasoning_capabilities(cfg)
        levels = [x["id"] for x in capabilities["reasoning_efforts"]]
        effort = next((x for x in ("max", "xhigh", "high", "medium", "low") if x in levels), None)
        result.update(advertised_efforts=levels, requested_effort=effort or "provider default")
        client = make_llm_client(
            cfg=cfg,
            profile=chosen,
            model=model,
            api_key=key,
            reasoning_effort=effort,
            transport=capture,
            session_id="context-audit-" + uuid4().hex,
        )
        client.provider_retry_settings = ProviderRetrySettings(max_retries=0, disable_retries=True)
        result.update(
            client=type(client).__name__,
            provider=client.provider_key,
            trace_adapter=client.reasoning_trace_capability.adapter,
        )

        def read(arguments):
            value = fs_read(root=directory, path=arguments["path"])
            result["tool_reads"].append(
                {"path": arguments["path"], "content_sha256": digest(value)}
            )
            if arguments["path"] == "manifest.json":
                capture.markers.update(label=label, audit_nonce=nonce)
            if arguments["path"] == source_name:
                capture.markers.update(note=note)
            save()
            return value

        tool = ToolDef(
            name="fs_read",
            description="Read an existing UTF-8 workspace file.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            run=read,
        )
        session = AgentSession(
            cfg=cfg,
            root=directory,
            mode="auto",
            yes=True,
            stream=streaming,
            routing_mode="code_only",
            max_steps=None,
            console=Console(file=io.StringIO(), force_terminal=False),
            surface=NoopSurface(),
            store=SessionStore(
                enabled=False,
                sessions_dir=directory / "sessions",
                session_id=uuid4().hex,
                cwd=str(directory),
                repo_root=str(directory),
            ),
            client=client,
            model_registry=ModelRegistry(cfg=cfg),
            usage_summary=UsageSummary(),
            usage_role="main",
            tool_output_offloader=None,
            conversation_compactor=None,
            tool_output_offload_enabled=False,
            conversation_summarization_enabled=False,
            compaction_profile="chat",
            tools={tool.name: tool},
            tool_list=[tool.as_openai_tool()],
            messages=[
                {
                    "role": "system",
                    "content": "You are a coding assistant inspecting a small data fixture. Use the provided read-only file tool for evidence and obey the user's output format.",
                }
            ],
            verification_enabled=False,
            skills_enabled=False,
        )
        capture.markers["original_user_tag"] = tag
        request = (
            f"Inspect manifest.json, then read the source file it names. Report the exact sum of the source amounts. "
            f"Return only JSON with tag='{tag}', label from the manifest, note from the source, and integer total. "
            "Preserve the exact Unicode values. Read the real files; do not invent their contents."
        )
        result["first_exit"] = session.run_turn(request)
        first = next(
            (
                m.get("content", "")
                for m in reversed(session.messages)
                if m.get("role") == "assistant" and not m.get("tool_calls")
            ),
            "",
        )
        result["first_answer"] = first
        result["first_correct"] = parse_reply(first) == expected
        if result["first_exit"] == 0:
            before_reads = len(result["tool_reads"])
            capture.markers["correction"] = revision
            followup = (
                f"Update that report: replace tag with '{revision}', keep label, note and total unchanged, "
                "and add audit_nonce from the manifest you already read. Do not read files again. Return only JSON."
            )
            result["followup_exit"] = session.run_turn(followup)
            answer = next(
                (
                    m.get("content", "")
                    for m in reversed(session.messages)
                    if m.get("role") == "assistant" and not m.get("tool_calls")
                ),
                "",
            )
            result["followup_answer"] = answer
            result["followup_correct"] = parse_reply(answer) == {
                **expected,
                "tag": revision,
                "audit_nonce": nonce,
            }
            result["followup_without_reread"] = len(result["tool_reads"]) == before_reads
            result["passed"] = (
                result["first_correct"]
                and result["followup_exit"] == 0
                and result["followup_correct"]
                and result["followup_without_reread"]
            )
    except Exception as error:
        result.update(error_type=type(error).__name__, error=str(error))
    finally:
        if session:
            session.close()
        capture.close()
        save()
    print(
        json.dumps(
            {
                "model": model,
                "stream": streaming,
                "passed": result["passed"],
                "calls": len(result["calls"]),
                "error": result.get("error"),
                "evidence": str(root / "evidence.json"),
            }
        ).replace(key, "[redacted]"),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-live", action="store_true", required=True)
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument(
        "--protocol", choices=["openai_compat", "openai_responses", "anthropic_messages"]
    )
    parser.add_argument("--base-url")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    cfg = load_config()
    profile = get_profile(cfg, "opencode")
    if not profile or profile.base_url.rstrip("/") != "https://opencode.ai/zen/go/v1":
        raise RuntimeError("Saved OpenCode Go profile required; no fallback")
    key = resolve_profile_api_key(cfg, profile.name).key
    if not key:
        raise RuntimeError("Saved Go credential unavailable")
    if args.protocol:
        profile = replace(profile, protocol=args.protocol)
    if args.base_url:
        endpoint = httpx.URL(args.base_url)
        if (
            endpoint.scheme != "https"
            or endpoint.host != "opencode.ai"
            or not endpoint.path.startswith("/inference/go/")
        ):
            raise RuntimeError("Additional endpoints must remain inside documented OpenCode Go")
        profile = replace(profile, base_url=args.base_url)
    if args.stream and profile.protocol != "openai_compat":
        raise RuntimeError("This wire decoder qualifies streaming only for Chat Completions")
    args.output.mkdir(parents=True, exist_ok=False)
    evidence = {
        "started_utc": datetime.now(UTC).isoformat(),
        "endpoint": profile.base_url,
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "engine_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "main_baseline": subprocess.check_output(
            ["git", "rev-parse", "origin/main"], text=True
        ).strip(),
        "source_hashes": {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (
                Path("src/alysis_code/reasoning_contracts.py"),
                Path("src/alysis_code/llm/openai_compat.py"),
                Path("src/alysis_code/llm/factory.py"),
                Path("src/alysis_code/llm/metadata.py"),
            )
        },
        "scope": "real provider; normal AgentSession with restricted real fs_read; synthetic fixtures; buffered wire capture; no compaction, UI, hosted or model-quality benchmark",
        "cases": [],
    }

    def save():
        text = json.dumps(evidence, ensure_ascii=False, indent=2).replace(key, "[redacted]")
        (args.output / "evidence.json").write_text(text)

    save()
    (args.output / "harness.py").write_bytes(Path(__file__).read_bytes())
    for model in args.models:
        run_case(model, args.stream, args.output, profile, key, evidence, save)
    evidence["finished_utc"] = datetime.now(UTC).isoformat()
    save()


if __name__ == "__main__":
    main()
