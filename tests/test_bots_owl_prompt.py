"""An owl's standing guidance and the prompt cache its model calls use (owl_prompt.py).

The guidance is short named sections that stay within a byte budget for every combination of
features, say each rule once, and are the same bytes for every turn and every language. Each
owl model call carries the engine's prompt-cache policy for its route, keyed to the owl. Real
store, runtime and provider clients; the provider is a local transport; messages in Greek and
Japanese.
"""

from __future__ import annotations

import itertools
import json
import re
import threading
from collections import Counter
from uuid import uuid4

import httpx
import pytest

from alysis_code.bots import owl_prompt
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.store import Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient

OWNER = "owner"
OWL = {
    "name": "Γλαύκα",
    "purpose": "Βοηθός γραφείου",
    "instructions": "Απάντα σύντομα.",
    "memory_enabled": True,
    "files_enabled": True,
}
FEATURES = ("plugins", "browser", "web_tools", "desktop", "coding_workspace", "documents")
# The most the standing guidance may take, in UTF-8 bytes, for this owl (short setup, no About
# me). Every feature on is about 20,500; before the rewrite it was about 26,200.
GUIDANCE_BUDGET_BYTES = 21_000
# An owl with memory and files but no computer, browser, web lookup or plugins: about 14,500
# (before: about 17,900).
BARE_GUIDANCE_BUDGET_BYTES = 15_000
SECTIONS = [
    "Who you are",
    "How to work",
    "Research and reading pages",
    "Websites",
    "Connected plugins",
    "Files",
    "The private computer",
    "Code",
    "Approvals and safety",
    "Memory and notes",
    "Routines",
    "Background work",
    "How to reply",
    "Bot setup",
]
ENDPOINT = "https://models.example.invalid/v1"


def render(*, memory=True, files=True, **features):
    return owl_prompt.guidance(
        bot={**OWL, "memory_enabled": memory, "files_enabled": files},
        about_me="",
        **{name: features.get(name, False) for name in FEATURES},
    )


def combinations():
    for values in itertools.product((False, True), repeat=len(FEATURES) + 2):
        *features, memory, files = values
        yield dict(zip(FEATURES, features, strict=True), memory=memory, files=files)


# ---------------------------------------------------------------------------------------------
# The guidance


def test_every_combination_of_features_stays_within_the_byte_budget():
    sizes = {json.dumps(c): len(render(**c).encode()) for c in combinations()}
    assert max(sizes.values()) <= GUIDANCE_BUDGET_BYTES, max(sizes.items(), key=lambda i: i[1])
    assert len(render().encode()) <= BARE_GUIDANCE_BUDGET_BYTES


def test_document_tools_and_the_page_view_are_explained_where_they_apply():
    def section(text, title):
        return text.split(f"## {title}\n", 1)[1].split("\n## ", 1)[0]

    made_here = render(desktop=True, browser=True, documents=True)
    assert "create_document" in section(made_here, "Files")
    assert "Make other binary deliverables with the enabled computer" in made_here
    assert "a generated binary file that create_document can't make" in section(
        made_here, "The private computer"
    )
    computer_only = render(desktop=True, browser=True)
    assert "create_document" not in computer_only
    assert "Make binary deliverables with the enabled computer" in computer_only
    # Without files the tools aren't offered, so the guidance never names them.
    assert "create_document" not in render(desktop=True, documents=True, files=False)
    websites = section(made_here, "Websites")
    assert "[ref=eN]" in websites and "observe again" in websites
    assert "each action uses up its reference" not in made_here


def test_it_is_short_named_sections_in_a_fixed_order():
    full = render(plugins=True, browser=True, web_tools=True, desktop=True, coding_workspace=True)
    assert re.findall(r"^## (.+)$", full, re.M) == SECTIONS
    bare = render(memory=False, files=False)
    assert re.findall(r"^## (.+)$", bare, re.M) == [
        s for s in SECTIONS if s not in {"Websites", "Connected plugins", "Files"}
    ]
    # Only what the owl has is described; what it lacks is said plainly.
    assert "browser_download" not in bare and "propose_memory" not in bare
    assert "Looking things up online is unavailable here." in bare
    assert owl_prompt.NO_PRIVATE_COMPUTER in bare
    assert "Memory is off for this bot" in bare


def test_each_rule_is_said_once():
    for combination in combinations():
        sentences = [
            " ".join(part.split()).casefold()
            for part in re.split(r"(?<=[.:;?!])\s+|\n", render(**combination))
            if len(part.split()) >= 5
        ]
        repeated = [s for s, n in Counter(sentences).items() if n > 1]
        assert repeated == [], (combination, repeated)


def test_it_is_the_same_bytes_for_every_turn_and_language(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot(OWNER, BotInput(name=OWL["name"], purpose=OWL["purpose"]))
    chat = store.create_chat(OWNER, bot["id"], "Chat")
    runtime = BotRuntime(store, OWNER, lambda _: None)
    seen = []
    for text in ("Γράψε μου μια σύντομη απάντηση.", "短い返事を書いてください。"):
        store.enqueue(OWNER, chat["id"], TaskInput(message=text, request_id=uuid4().hex))
        task = store.claim(OWNER)
        seen.append(runtime._context(task)[0]["content"])
        store.finish(OWNER, task["id"], "completed", content="Εντάξει.")
    assert seen[0] == seen[1]
    assert seen[0].startswith("## Who you are\nYou are a persistent bot in Alysis.")
    assert "Task start UTC time" not in seen[0] and task["id"] not in seen[0]


# ---------------------------------------------------------------------------------------------
# The prompt cache


def test_a_route_that_declares_cache_control_gets_it():
    client = AnthropicMessagesClient(
        base_url=ENDPOINT, api_key="k", model="cache-fixture", provider_key="anthropic"
    )
    assert client.prompt_cache_control_enabled is False
    policy = owl_prompt.use_prompt_cache(client, owl_id="owl-1")
    assert policy.status == "enabled" and client.prompt_cache_control_enabled is True


def test_a_cache_key_is_kept_per_owl():
    def key(owl_id):
        client = OpenAIResponsesClient(
            base_url=ENDPOINT, api_key="k", model="cache-fixture", provider_key="openai"
        )
        owl_prompt.use_prompt_cache(client, owl_id=owl_id)
        return client.prompt_cache_key

    assert key("owl-1") and key("owl-1") == key("owl-1") != key("owl-2")
    assert "owl-1" not in key("owl-1")  # a digest, never the owl's id


def test_a_route_without_a_declared_cache_gets_no_fields():
    client = OpenAICompatClient(base_url=ENDPOINT, api_key="k", model="cache-fixture")
    policy = owl_prompt.use_prompt_cache(client, owl_id="owl-1")
    assert policy.emitted_fields == () and client.prompt_cache_request_field_values == {}


def test_turning_the_engine_cache_off_turns_it_off_for_owls(monkeypatch):
    monkeypatch.setenv("ALYSIS_PROMPT_CACHE_MODE", "off")
    client = AnthropicMessagesClient(
        base_url=ENDPOINT, api_key="k", model="cache-fixture", provider_key="anthropic"
    )
    assert owl_prompt.use_prompt_cache(client, owl_id="owl-1").status == "disabled"
    assert client.prompt_cache_control_enabled is False


def test_a_client_that_is_not_a_provider_client_is_left_alone():
    class Double:
        model = "fixture"

    double = Double()
    assert owl_prompt.use_prompt_cache(double, owl_id="owl-1") is None
    assert vars(double) == {}


@pytest.mark.parametrize("text", ["Πες μου μια καλημέρα.", "おはようと言って。"])
def test_every_owl_request_carries_the_cache_marker(tmp_path, text):
    sent = []

    def provider(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "m",
                "type": "message",
                "role": "assistant",
                "model": "cache-fixture",
                "content": [{"type": "text", "text": "Καλημέρα. / おはよう。"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        )

    store = Store(tmp_path / "bots.db")
    bot = store.save_bot(OWNER, BotInput(name=OWL["name"], purpose=OWL["purpose"]))
    chat = store.create_chat(OWNER, bot["id"], "Chat")
    runtime = BotRuntime(
        store,
        OWNER,
        lambda _: AnthropicMessagesClient(
            base_url=ENDPOINT,
            api_key="k",
            model="cache-fixture",
            provider_key="anthropic",
            transport=httpx.MockTransport(provider),
        ),
        limits=RuntimeLimits(calls_per_task=4),
    )
    runtime.reply_review = None
    for _ in range(2):
        store.enqueue(OWNER, chat["id"], TaskInput(message=text, request_id=uuid4().hex))
        task = store.claim(OWNER)
        runtime.run(task, EventCancellationToken(threading.Event()))
        assert store.task(OWNER, task["id"])["status"] == "completed"
    assert len(sent) == 2
    guidance = [payload["system"][0] for payload in sent]
    # The standing guidance is a system block of its own with a cache marker, the same bytes
    # in both turns; the task's own state follows in later blocks.
    assert guidance[0] == guidance[1]
    assert guidance[0]["cache_control"] == {"type": "ephemeral"}
    assert guidance[0]["text"].startswith("## Who you are")
    assert owl_prompt.TASK_STATE_HEADING not in guidance[0]["text"]
    for payload in sent:
        assert payload["system"][1]["text"].startswith(owl_prompt.TASK_STATE_HEADING.strip())
        assert text in json.dumps(payload["messages"], ensure_ascii=False)
