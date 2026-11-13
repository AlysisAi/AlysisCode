"""Owls look things up without a computer: scripted search and pages, no live network."""

import json
import threading

import pytest

from alysis_code.bots import web_lookup
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits, tool_schema
from alysis_code.bots.store import Conflict, Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall
from alysis_code.tools import web as web_tool
from alysis_code.tools import web_search_ddgs


def response(text="", calls=()):
    return LLMResponse(text, list(calls), {}, usage=LLMUsage(10, 5, 15))


class Model:
    model = "fixture"
    supports_tool_calling = True

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(kwargs)
        return next(self.responses)


def test_search_results_are_titles_addresses_and_short_snippets(monkeypatch):
    def search(**kwargs):
        assert kwargs["query"] == "Αθήνα πληθυσμός" and kwargs["max_results"] == 3
        return {
            "sources": [
                {"url": "https://example.org/a", "title": "A", "snippet": "x" * 900},
                {"url": "", "title": "no address"},
                {"url": "https://example.org/b", "title": "B"},
            ]
        }

    monkeypatch.setattr(web_search_ddgs, "ddgs_search", search)
    found = web_lookup.web_search("Αθήνα πληθυσμός", 3)
    assert [r["url"] for r in found["results"]] == [
        "https://example.org/a",
        "https://example.org/b",
    ]
    assert len(found["results"][0]["snippet"]) == 400 and "snippet" not in found["results"][1]


def test_a_failed_search_tells_the_owl_what_to_try(monkeypatch):
    def search(**kwargs):
        raise web_search_ddgs.DdgsSearchError("boom")

    monkeypatch.setattr(web_search_ddgs, "ddgs_search", search)
    with pytest.raises(Conflict, match="other wording"):
        web_lookup.web_search("anything", 5)


def test_pages_are_read_whole_up_to_the_limit_and_by_their_content(monkeypatch):
    seen = {}

    def fetch(**kwargs):
        seen.update(kwargs)
        return {
            "url": kwargs["url"],
            "final_url": "https://example.org/final",
            "status_code": 200,
            "content_type": "text/html",
            "title": "Title",
            "content": "Body",
            "truncated": True,
            "links": [{"text": "Hours", "url": "https://example.org/hours"}],
        }

    monkeypatch.setattr(web_tool, "web_fetch", fetch)
    page = web_lookup.web_fetch("https://example.org/start", 5000)
    assert seen == {
        "url": "https://example.org/start",
        "max_chars": 5000,
        "truncate_oversized": True,
        "main_content": True,
        "include_links": True,
        "max_bytes": 6_000_000,
        "read_pdf": True,
    }
    assert page["url"] == "https://example.org/final" and page["text"] == "Body"
    assert page["truncated"] is True and "note" not in page
    assert page["links"] == [{"text": "Hours", "url": "https://example.org/hours"}]


def test_a_page_brings_its_links_as_text_and_absolute_addresses():
    page = (
        '<html><head><link href="/style.css"></head><body>'
        '<a href="/en/visit">Plan your <b>visit</b></a> <a href="#top">Top</a>'
        "<a href='https://other.example/x?a=1&amp;b=2#frag' title='Other'></a>"
        '<a href="mailto:me@example.org">Mail</a> <a href="/en/visit">Again</a>'
        '<script>var a = "<a href=/hidden>hidden</a>";</script></body></html>'
    )
    links = web_tool._extract_links(page, "https://site.example/en/home")
    assert links == [
        {"text": "Plan your visit", "url": "https://site.example/en/visit"},
        {"text": "Other", "url": "https://other.example/x?a=1&b=2"},
    ]


def test_an_unreadable_page_says_why_in_plain_words(monkeypatch):
    def fetch(**kwargs):
        raise web_tool.WebFetchError(
            "403", recoverable=True, remote_site_reason="site requires sign-in"
        )

    monkeypatch.setattr(web_tool, "web_fetch", fetch)
    with pytest.raises(Conflict, match=r"could not be read \(site requires sign-in\)"):
        web_lookup.web_fetch("https://example.org/private", 5000)


def test_private_addresses_are_never_fetched():
    with pytest.raises(Conflict, match="loopback"):
        web_lookup.web_fetch("http://127.0.0.1:9/", 2000)


def test_main_content_skips_menus_and_keeps_quoted_markup_out_of_the_text():
    article = "Real article text. " * 20
    page = (
        "<html><head><title>T</title></head><body><nav>Menu Home About</nav>"
        '<main id="content"><div data-mw=\'{"a":"<ref>x</ref>"}\'>'
        f"<p>{article}</p></div></main><footer>Footer links</footer></body></html>"
    )
    title, text = web_tool._extract_html_title_and_text(page, main_content=True)
    assert title == "T"
    assert text.startswith("Real article text.") and "Menu" not in text
    assert "<ref>" not in text and "Footer" not in text
    # Without a real main region the page is kept, minus its furniture.
    _, plain = web_tool._extract_html_title_and_text(
        f"<body><header>Brand</header><div>{article}</div><aside>Ads</aside></body>",
        main_content=True,
    )
    assert "Brand" not in plain and "Ads" not in plain and "Real article text." in plain


def test_owls_search_and_read_in_a_chat_turn_without_a_computer(tmp_path, monkeypatch):
    monkeypatch.setattr(web_lookup, "web_tools_available", lambda: True)
    monkeypatch.setattr("alysis_code.bots.runtime.web_tools_available", lambda: True)
    monkeypatch.setattr(
        "alysis_code.bots.runtime.web_search",
        lambda query, limit: {
            "query": query,
            "results": [{"title": "S", "url": "https://s.example/"}],
        },
    )
    monkeypatch.setattr(
        "alysis_code.bots.runtime.web_fetch",
        lambda url, chars: {"url": url, "title": "S", "text": "The answer is 42."},
    )
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Mira", purpose="Help"))
    chat = store.create_chat("owner", bot["id"], "Chat")
    store.enqueue(
        "owner", chat["id"], TaskInput(message="What is the answer?", request_id="web-lookup-1")
    )
    task = store.claim("owner")
    model = Model(
        [
            response("Looking it up.", [ToolCall("s", "web_search", {"query": "the answer"})]),
            response(calls=[ToolCall("f", "web_fetch", {"url": "https://s.example/"})]),
            response("It is 42 ([S](https://s.example/))."),
        ]
    )
    runtime = BotRuntime(store, "owner", lambda _: model, limits=RuntimeLimits(calls_per_task=8))
    runtime.reply_review = None
    runtime.run(task, EventCancellationToken(threading.Event()))
    final = store.task("owner", task["id"])
    assert final["status"] == "completed" and final["output"].startswith("It is 42")
    offered = {t["function"]["name"] for t in model.requests[0]["tools"]}
    assert {"web_search", "web_fetch"} <= offered
    tool_results = [m for m in model.requests[2]["messages"] if m.get("role") == "tool"]
    assert json.loads(tool_results[-1]["content"])["text"] == "The answer is 42."
    assert "web_fetch" in tool_schema("web_fetch")["function"]["name"]


def test_a_pdf_is_read_as_its_pages_text():
    canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    import io

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(72, 720, "Opening hours 9:00-17:00")
    pdf.showPage()
    pdf.drawString(72, 720, "Full ticket 20 EUR")
    pdf.save()
    text = web_tool._pdf_text(buffer.getvalue(), truncated=False)
    assert "Opening hours 9:00-17:00" in text and "Full ticket 20 EUR" in text
    assert text.index("Opening") < text.index("Full ticket")
    with pytest.raises(web_tool.WebFetchError, match="larger than the reading limit"):
        web_tool._pdf_text(buffer.getvalue(), truncated=True)
    with pytest.raises(web_tool.WebFetchError, match="could not be opened"):
        web_tool._pdf_text(b"%PDF-1.4 not really", truncated=False)
