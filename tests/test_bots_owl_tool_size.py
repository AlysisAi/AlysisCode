"""How much an owl's own tool definitions take (owl_tools.py, browser_tools.py, desktop.py).

A model reads every offered tool's name, description and inputs on every step, before the
conversation, and connected apps' tools go behind look-up once the whole list passes a quarter of
the model's declared window (owl_toolset.py). These bounds keep the definitions an owl with every
feature is offered, with and without its browser and its computer, from growing back unnoticed,
and keep room for a connected app's tools on a 128,000-token window (the model registry's default
for a model it doesn't know).
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from alysis_code.bots.browser_tools import DEFINITIONS as BROWSER_TOOLS
from alysis_code.bots.desktop import DEFINITIONS as DESKTOP_TOOLS
from alysis_code.bots.desktop_connectors import CatalogInput, DesktopConnectors
from alysis_code.bots.owl_tools import tool_schema
from alysis_code.bots.owl_toolset import (
    FULL,
    TOOL_SCHEMA_WINDOW_SHARE,
    Turn,
    app_tools,
    offer,
    tool_names,
)
from alysis_code.bots.tool_parameters import tool_parameters
from alysis_code.cloud_connections import BoundConnections, ConnectionStore
from alysis_code.request_estimation import estimate_tool_schema_tokens

WINDOW = 128_000
EVERY_FEATURE = Turn(
    answers_person=True,
    web_lookup=True,
    tool_calling=True,
    files=True,
    documents=True,
    images=True,
    memory=True,
    coding=True,
    other_owls=True,
    about_me=True,
    playbooks=True,
    reply_checked=True,
    own_message=True,
)

# Characters of each full list, about 3% above its size when these bounds were set. Measured in
# characters, as the token estimate serializes the list, so a bound means the same on every
# machine whichever tokenizer it has. Raise one only for a tool that is worth what it costs.
BOUNDS = {
    (False, False): 73_000,
    (False, True): 88_000,
    (True, False): 102_000,
    (True, True): 116_000,
}


def offered_as_managed(definitions):
    """Browser or computer tools as their managers offer them, with every option a newer owl
    computer declares."""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": tool_parameters(model),
            },
        }
        for name, (model, description) in definitions.items()
    ]


def every_tool(*, browser: bool, computer: bool) -> list[dict]:
    turn = replace(EVERY_FEATURE, browser=browser, computer_handoff=browser or computer)
    return [
        *(
            tool_schema(name, offline_computer=computer, browser=browser)
            for name in tool_names(turn)
        ),
        *(offered_as_managed(BROWSER_TOOLS) if browser else []),
        *(offered_as_managed(DESKTOP_TOOLS) if computer else []),
    ]


def characters(schemas: list[dict]) -> int:
    return len(json.dumps(schemas, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


@pytest.mark.parametrize(("browser", "computer"), list(BOUNDS))
def test_every_tool_an_owl_is_offered_stays_within_its_bound(browser, computer):
    schemas = every_tool(browser=browser, computer=computer)
    assert characters(schemas) <= BOUNDS[browser, computer]


def test_a_connected_app_stays_direct_beside_every_owl_tool_on_a_128k_window(tmp_path):
    own = every_tool(browser=False, computer=False)
    room = WINDOW * TOOL_SCHEMA_WINDOW_SHARE - estimate_tool_schema_tokens(own)
    assert room >= 500
    # One small app, connected as the desktop app connects it, its words in Greek and Japanese.
    hub = DesktopConnectors()
    hub.publish(
        CatalogInput(
            revision=1,
            plugins=[
                {
                    "plugin_id": "custom.notes",
                    "name": "Σημειώσεις",
                    "tools": [
                        {
                            "name": "search",
                            "description": "Βρίσκει σημειώσεις. ノートを探します。",
                            "input_schema": {
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                            },
                            "effect": "read",
                        }
                    ],
                }
            ],
        )
    )
    tools = BoundConnections(
        ConnectionStore(tmp_path / "connections.sqlite3"), "owner", "bot:owl", None, hub
    ).tools()
    connected = [tool.as_openai_tool() for tool in tools.values()]
    made = offer([*own, *connected], context_window=WINDOW, apps=app_tools(tools))
    assert made.mode == FULL and not made.hidden and made.reasons == ()
