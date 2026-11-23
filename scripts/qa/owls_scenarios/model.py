"""The scripted model: answers each model call from a scenario's script, never a provider.

The owls runtime calls one client for every kind of model call it makes: the owl's own turns,
the reply checker, the action reviewer, memory indexing and the rest. Each call is put on a
*channel* by the engine module that made it (read from the call stack, never from prompt text),
and each channel plays its own list of scripted replies in order. A call on a channel with no
reply left is a script error: the runner reports the scenario as broken instead of letting the
engine guess.

Owl replies (exactly one of these keys, plus optional ``repeat``):

- ``say``: a text reply.
- ``call``: tool calls, each ``{"tool", "arguments", "id"?}``; ``say`` may go with it as the text
  before the calls.
- ``empty``: an empty reply (no text, no call).
- ``cut_off``: a reply the output limit cut off, ``{"wire", "say", "call"}`` where ``wire`` is
  how the provider sends one: ``chat_completions`` (finish_reason "length"), ``messages``
  (stop_reason "max_tokens") or ``responses`` (the Responses client's output-limit error).
- ``error``: a provider error, ``{"message", "status"?, "code"?, "retry_after"?}``.
- ``refusal``: the model refuses; the client raises the provider's refusal error.

Argument values may read the latest tool results the model was shown: ``{"$result": "a.b"}``
is that key path of the newest tool result that has it, and ``{"$page_ref": "<line start>"}`` is
the element ref on the newest page view line starting with that text. That is all a model does
with its context here, so a scenario stays data.

Reply checker verdicts are ``{"decision", "reason", "issue"?, "false_claim"?}`` (default: every
draft is ready). Action reviewer verdicts are ``{"decision", "effect"?, "risk"?,
"user_authorization"?, "reason"?}``. Any channel takes ``say`` (raw text) and ``error`` too.
"""

from __future__ import annotations

import copy
import json
import sys
import threading
from collections.abc import Callable
from typing import Any

from alysis_code.llm.types import LLMError, LLMOutputLimitError, LLMResponse, LLMUsage, ToolCall

OWL = "owl"
REPLY_CHECKER = "reply_checker"
ACTION_REVIEWER = "action_reviewer"

# The engine module that made a call decides its channel: the innermost one listed wins.
CALLERS = {
    "alysis_code.bots.reply_review": REPLY_CHECKER,
    "alysis_code.bots.action_review": ACTION_REVIEWER,
    "alysis_code.bots.memory_applicability": "memory_index",
    "alysis_code.bots.context_summary": "context_summary",
    "alysis_code.bots.memory_capture": "memory_capture",
    "alysis_code.bots.research_helpers": "research_helper",
    "alysis_code.bots.cooperation": "peer",
    "alysis_code.bots.runtime": OWL,
    "alysis_code.bots.execution": OWL,
}
CHANNELS = frozenset(CALLERS.values())
OWL_REPLY_KINDS = ("say", "call", "empty", "cut_off", "error", "refusal")
CUT_OFF_WIRES = ("chat_completions", "messages", "responses")
READY = {"decision": "ready", "reason": "The draft matches the receipts."}


class ScriptError(Exception):
    """The engine made a model call the scenario did not script (or scripted wrongly)."""


def usage(entry: dict | None = None) -> LLMUsage:
    given = (entry or {}).get("usage") or {}
    prompt, completion = given.get("prompt", 100), given.get("completion", 20)
    return LLMUsage(prompt, completion, prompt + completion)


def _channel() -> str:
    frame = sys._getframe(2)
    while frame is not None:
        found = CALLERS.get(frame.f_globals.get("__name__", ""))
        if found:
            return found
        frame = frame.f_back
    return OWL


def _tool_results(messages: list[dict]) -> list[Any]:
    """The tool results in a request, newest first, parsed when they are JSON."""
    found = []
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        try:
            found.append(json.loads(content) if isinstance(content, str) else content)
        except ValueError:
            found.append(content)
    return found


def _dig(value: Any, path: str) -> Any:
    for key in path.split("."):
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and key.isdigit() and int(key) < len(value):
            value = value[int(key)]
        else:
            raise KeyError(path)
    return value


def page_ref(view: str, line_start: str) -> int:
    """The ref number on the page view line that starts (after indentation) with ``line_start``."""
    for line in view.splitlines():
        if line.lstrip().startswith(line_start) and "[ref=e" in line:
            return int(line.split("[ref=e", 1)[1].split("]", 1)[0])
    raise KeyError(line_start)


def resolve(value: Any, messages: list[dict]) -> Any:
    """Arguments with ``$result`` and ``$page_ref`` read from the newest tool results."""
    if isinstance(value, list):
        return [resolve(item, messages) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"$result"}:
        for result in _tool_results(messages):
            try:
                return _dig(result, value["$result"])
            except KeyError:
                continue
        raise ScriptError(f"No tool result has {value['$result']!r} yet.")
    if set(value) == {"$page_ref"}:
        for result in _tool_results(messages):
            view = result.get("page_view") if isinstance(result, dict) else None
            if isinstance(view, str):
                try:
                    return page_ref(view, value["$page_ref"])
                except KeyError:
                    continue
        raise ScriptError(f"No page view line starts with {value['$page_ref']!r}.")
    return {key: resolve(item, messages) for key, item in value.items()}


def _calls(entries: list[dict], messages: list[dict], number: int) -> list[ToolCall]:
    return [
        ToolCall(
            str(entry.get("id") or f"call-{number}-{index}"),
            entry["tool"],
            resolve(copy.deepcopy(entry.get("arguments") or {}), messages),
        )
        for index, entry in enumerate(entries, 1)
    ]


def _wire_calls(calls: list[ToolCall]) -> list[dict]:
    return [
        {
            "id": call.id,
            "type": "function",
            "function": {
                "name": call.name,
                "arguments": json.dumps(call.arguments, ensure_ascii=False),
            },
        }
        for call in calls
    ]


def _finished(text: str, calls: list[ToolCall], entry: dict) -> LLMResponse:
    """A complete reply as a chat-completions provider sends it."""
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = _wire_calls(calls)
    raw = {"choices": [{"finish_reason": "tool_calls" if calls else "stop", "message": message}]}
    return LLMResponse(text, calls, raw, usage=usage(entry))


def _cut_off(spec: dict, calls: list[ToolCall], entry: dict) -> LLMResponse:
    text, wire = spec.get("say", ""), spec.get("wire", "chat_completions")
    if wire == "chat_completions":
        message: dict[str, Any] = {"role": "assistant", "content": text}
        if calls:
            message["tool_calls"] = _wire_calls(calls)
        raw = {"choices": [{"finish_reason": "length", "message": message}]}
        return LLMResponse(text, calls, raw, usage=usage(entry))
    if wire == "messages":
        content: list[dict] = [{"type": "text", "text": text}] if text else []
        content += [
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in calls
        ]
        raw = {"stop_reason": "max_tokens", "content": content}
        return LLMResponse(text, calls, raw, usage=usage(entry))
    output: list[dict] = (
        [{"type": "message", "content": [{"type": "output_text", "text": text}]}] if text else []
    )
    # The Responses client keeps no call it could not finish reading: only its name is left.
    output += [
        {"type": "function_call", "name": c.name, "arguments": json.dumps(c.arguments)[:12]}
        for c in calls
    ]
    raw = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": output,
    }
    raise LLMOutputLimitError(LLMResponse(text, [], raw, usage=usage(entry)))


def provider_error(spec: dict) -> LLMError:
    error = LLMError(spec.get("message") or "Provider error")
    if spec.get("status") is not None:
        error.provider_status_code = spec["status"]
    if spec.get("code") is not None:
        error.provider_error_code = spec["code"]
    if spec.get("retry_after") is not None:
        error.provider_retry_after = spec["retry_after"]
    return error


def expand(entries: list[dict]) -> list[dict]:
    """A script with each ``repeat`` written out."""
    out = []
    for entry in entries:
        times = entry.get("repeat", 1)
        out += [{k: v for k, v in entry.items() if k != "repeat"} for _ in range(times)]
    return out


class ScriptedModel:
    """A chat client whose every answer comes from the scenario's script."""

    def __init__(
        self,
        script: dict,
        *,
        model: str = "scripted-owl-model",
        base_url: str = "https://models.invalid/v1",
        image_input: bool = False,
        tool_calling: bool = True,
    ):
        self.model = model
        self.base_url = base_url
        self.supports_tool_calling = tool_calling
        # What the service sets from the model's declared vision (owl_models.image_input).
        self._alysis_browser_image_input = image_input
        checker = script.get(REPLY_CHECKER, "ready")
        self.always_ready = checker == "ready"
        self.queues = {
            channel: expand(list(script.get(channel) or []))
            for channel in CHANNELS
            if not (channel == REPLY_CHECKER and self.always_ready)
        }
        self.calls: list[dict] = []
        self.errors: list[str] = []
        self.counts = {channel: 0 for channel in CHANNELS}
        self.before_owl_reply: Callable[[int], None] = lambda number: None
        # The turn a call belongs to (the runner says which is running).
        self.current_task: Callable[[], str | None] = lambda: None
        self._lock = threading.RLock()

    def unused(self) -> dict[str, int]:
        return {channel: len(queue) for channel, queue in self.queues.items() if queue}

    def chat(self, **kwargs):
        channel = _channel()
        with self._lock:
            self.counts[channel] += 1
            number = self.counts[channel]
            messages = copy.deepcopy(kwargs.get("messages") or [])
            record = {
                "channel": channel,
                "number": number,
                "task_id": self.current_task(),
                "messages": messages,
                "tools": [t["function"]["name"] for t in kwargs.get("tools") or []],
                "max_tokens": kwargs.get("max_tokens"),
            }
            self.calls.append(record)
        if channel == OWL:
            # The person may act while the model writes this reply.
            self.before_owl_reply(number)
        with self._lock:
            if channel == REPLY_CHECKER and self.always_ready:
                entry = dict(READY)
            elif self.queues[channel]:
                entry = self.queues[channel].pop(0)
            else:
                message = f"{channel} call {number} has no scripted reply."
                self.errors.append(message)
                record["reply"] = {"script_error": message}
                raise ScriptError(message)
            record["reply"] = copy.deepcopy(entry)
        try:
            return self._answer(channel, entry, messages, number, record)
        except ScriptError as exc:
            self.errors.append(f"{channel} call {number}: {exc}")
            raise

    def _answer(self, channel: str, entry: dict, messages: list[dict], number: int, record: dict):
        if "error" in entry:
            raise provider_error(entry["error"])
        if "refusal" in entry:
            raise LLMError(f"Model refusal: {entry['refusal']}")
        if channel == OWL:
            calls = _calls(entry.get("call") or [], messages, number)
            if "cut_off" in entry:
                spec = entry["cut_off"]
                calls = _calls(spec.get("call") or [], messages, number)
                record["made"] = [
                    {"name": c.name, "arguments": c.arguments, "cut_off": True} for c in calls
                ]
                return _cut_off(spec, calls, entry)
            record["made"] = [{"name": c.name, "arguments": c.arguments} for c in calls]
            if "empty" in entry:
                return _finished("", [], entry)
            return _finished(entry.get("say", ""), calls, entry)
        if "say" in entry:
            return _finished(entry["say"], [], entry)
        if channel == ACTION_REVIEWER:
            verdict = {
                "risk": "low",
                "user_authorization": "high",
                "effect": "none",
                "reason": "Scripted assessment.",
                **entry,
            }
            return _finished(json.dumps(verdict, ensure_ascii=False), [], entry)
        return _finished(json.dumps(entry, ensure_ascii=False), [], entry)
