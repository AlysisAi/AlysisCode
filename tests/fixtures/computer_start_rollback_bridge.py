"""Inert paired APP fixture: real bridge/session/parser with synthetic model I/O.

Only this executable reads ALYSIS_S2BR_*; these are not production switches.
Launch with fresh HOME/config/data/TMPDIR and the standard offline test guard.
"""

# ruff: noqa: E402 -- install denying ports before any target import
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace


def deny_network(event, args):
    if event in {
        "socket.connect",
        "socket.connect_ex",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.bind",
        "socket.sendto",
    }:
        raise RuntimeError("paired fixture forbids network")


sys.addaudithook(deny_network)
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
os.environ["ALYSIS_UPDATE_CHECK_ENABLED"] = "0"
os.environ["ALYSIS_UPDATE_PROMPT_ENABLED"] = "0"

import httpx

from alysis_code import token_budget
from alysis_code.agent import session as sessions
from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.llm.openai_compat import OpenAICompatClient

scenario = os.environ["ALYSIS_S2BR_SCENARIO"]
receipt_path = Path(os.environ["ALYSIS_S2BR_RECEIPT"])
receipt = {"scenario": scenario, "requests": [], "outcomes": []}
corpus = json.loads(Path(__file__).with_name("computer_use_start_resolved_v1.json").read_text())
plan = {}


def save():
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=True))


def handler(request):
    if request.url.path.endswith("/input_tokens"):
        return httpx.Response(200, json={"input_tokens": 100})
    receipt["requests"].append(json.loads(request.content))
    n = len(receipt["requests"])
    if scenario == "ordinary-no-tools" or (scenario == "legacy" and n > 3):
        message = {
            "role": "assistant",
            "content": (
                "Synthetic ordinary answer."
                if scenario == "ordinary-no-tools"
                else "The synthetic selection remains unresolved."
            ),
        }
    else:
        name, arguments = ("computer_start_resolved_v1", plan)
        if scenario == "legacy":
            name, arguments = [
                ("skill_read", {"name": "computer-use"}),
                ("computer_discover", {}),
                ("computer_start", plan),
            ][min(n - 1, 2)]
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"fixture-{n}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }
    save()
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": message,
                    "finish_reason": "tool_calls" if "tool_calls" in message else "stop",
                }
            ]
        },
    )


cfg = AppConfig(model="test-model", routing_mode="code_only", web_search_mode="off", stream=False)
token_budget._token_encoder = lambda: None
stdio_bridge.load_config = lambda: cfg
sessions._make_session_llm_client = lambda **kw: OpenAICompatClient(
    base_url="https://fixture.invalid/v1",
    api_key="fixture",
    model="test-model",
    transport=httpx.MockTransport(handler),
)


def create(**kwargs):
    global plan
    kwargs.update(cfg=cfg, api_key_override="fixture", no_log=True, max_steps=5)
    if scenario.startswith("owned-"):
        descriptor = json.loads(json.dumps(corpus["bases"]["descriptor"]))
        if scenario in {"owned-incomplete", "owned-unavailable"}:
            descriptor["discovery"].update(coverage=scenario.removeprefix("owned-"), targets=[])
        kwargs.pop("computer_discovery", None)
        kwargs["computer_start_resolved_host"] = descriptor
        kwargs["computer_start_resolved_request"] = lambda *_: (_ for _ in ()).throw(
            AssertionError("unexpected admission")
        )
        plan = corpus["bases"]["plan"]
        if scenario == "owned-exhausted":
            plan = json.loads(json.dumps(plan))
            plan["app_steps"][0]["app"] = "Missing fixture app"
    elif scenario == "legacy":
        inventory = kwargs["computer_discovery"]
        target = next(t for t in inventory["targets"] if t.get("availability") != "installed")
        plan = dict(
            goal="Observe the synthetic selected app.",
            constraints=["Do not edit or activate."],
            operates_apps=False,
            app_steps=[dict(app=target["name"], actions=[])],
        )
    agent = sessions.create_session(**kwargs)
    original = agent._record_task_outcome

    def record(**kw):
        value = original(**kw)
        receipt["outcomes"].append(value)
        receipt["evidence"] = {
            k: v
            for k, v in getattr(agent, "_computer_use_evidence_state", {}).items()
            if not callable(v)
        }
        save()
        return value

    agent._record_task_outcome = record
    return agent


bridge = stdio_bridge.StdioBridge(create_session_fn=create)
bridge._managed_browser_factory = lambda **kw: SimpleNamespace(close_all=lambda: None)
try:
    raise SystemExit(bridge.run())
finally:
    save()
    bridge.close()
