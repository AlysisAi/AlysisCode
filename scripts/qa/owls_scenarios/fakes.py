"""Fakes for everything an owl turn would reach outside the computer: the web, a website in the
owl's browser and the Cloud computer that runs it.

Each fake sits at a seam the engine already has, so the real code above it runs:

- the web: the runtime's ``web_search``/``web_fetch`` and the check-backs' page reader;
- a website: the browser executor a ``BrowserComputers`` factory returns (the managed browser's
  calls: snapshots, pages, navigation and input), serving pages built from a scenario's page
  structure as Chromium reports them (a DOM snapshot and the accessibility tree of the same
  page);
- a Cloud computer: the compute provider a ``CloudDesktopFactory`` drives. It answers the
  worker's protocol on its side, so the host's whole remote path runs (requests, receipts,
  reply allowances, retiring a computer), and its browser calls reach the same fake website.

Nothing here opens a socket, a process or a file outside the scenario's temporary folder.
"""

from __future__ import annotations

import base64
import copy
import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace

from alysis_code.ide.browser_observation import redact_form_secrets
from alysis_code.ide.managed_browser import BrowserPageUnreachableError

# --------------------------------------------------------------------------------------- web


class NoNetwork(RuntimeError):
    """A scenario step tried to reach the web without a fake result for it."""


class Web:
    """Search results and page texts from the scenario (``fakes.web``); nothing else exists."""

    def __init__(self, spec: dict | None):
        spec = spec or {}
        self.enabled = bool(spec)
        self.searches = spec.get("searches") or {}
        self.pages = spec.get("pages") or {}
        self.seen: list[dict] = []

    def search(self, query: str, max_results: int) -> dict:
        self.seen.append({"search": query})
        results = self.searches.get(query, self.searches.get("*"))
        if results is None:
            raise NoNetwork(f"No search result is scripted for {query!r}.")
        return {"query": query, "results": copy.deepcopy(results)[:max_results]}

    def fetch(self, url: str, max_chars: int) -> dict:
        self.seen.append({"fetch": url})
        page = self.pages.get(url)
        if page is None:
            raise NoNetwork(f"No page is scripted at {url}.")
        text = page.get("text", "")
        return {"url": url, "title": page.get("title", ""), "text": text[:max_chars]}

    def page_text(self, url: str, max_chars: int = 20_000) -> tuple[str, bool]:
        self.seen.append({"page_text": url})
        page = self.pages.get(url)
        if page is None:
            raise NoNetwork(f"No page is scripted at {url}.")
        return page.get("text", "")[:max_chars], not page.get("partial", False)


@contextmanager
def offline_web(web: Web):
    """The runtime's web lookups answer from ``web`` for the length of a scenario run.

    Token counts use the engine's own estimate for a computer without the tokenizer's data
    (token_budget's fallback), so a run never downloads that data and counts the same on every
    computer, connected or not.
    """
    from alysis_code import token_budget
    from alysis_code.bots import runtime, web_lookup

    patches = [
        (token_budget, "_token_encoder", lambda: None),
        (runtime, "web_tools_available", lambda: web.enabled),
        (runtime, "web_search", web.search),
        (runtime, "web_fetch", web.fetch),
        (web_lookup, "web_tools_available", lambda: web.enabled),
        (web_lookup, "web_search", web.search),
        (web_lookup, "web_fetch", web.fetch),
        (web_lookup, "page_text", web.page_text),
    ]
    saved = [(module, name, getattr(module, name)) for module, name, _ in patches]
    try:
        for module, name, value in patches:
            setattr(module, name, value)
        yield web
    finally:
        for module, name, value in saved:
            setattr(module, name, value)


# --------------------------------------------------------------------------------------- pages


class PageBuilder:
    """One page as Chromium reports it: DOM nodes and accessibility nodes built together.

    Built from a scenario's page structure: ``{"url", "title", "body": [element, ...]}`` where an
    element is ``{"tag", "role"?, "name"?, "attrs"?, "value"?, "ax_value"?, "props"?,
    "checked"?, "selected"?, "rendered"?, "text"?, "children"?}`` or a text node
    ``{"text"}``. Roles and names are what the accessibility tree says; ``attrs`` are the HTML
    attributes (input types, autocomplete tokens, form actions...).
    """

    def __init__(self, spec: dict):
        self.strings: list[str] = []
        self.url = spec["url"]
        self.url_string = self.string(self.url)
        self.title = spec.get("title", "")
        self.nodes: dict[str, list] = {
            key: []
            for key in (
                "parentIndex",
                "nodeType",
                "nodeName",
                "nodeValue",
                "backendNodeId",
                "attributes",
            )
        }
        self.values: dict[int, str] = {}
        self.checked: list[int] = []
        self.selected: list[int] = []
        self.layout: list[int] = []
        self.ax: list[dict] = []
        self.backend = 1
        document = self._dom(-1, 9, "#document", rendered=False)
        self.root = self._ax(document, None, "RootWebArea", name=self.title)
        self.root["frameId"] = "FRAME-MAIN"
        html = self._dom(document, 1, "HTML")
        html_ax = self._ax(html, self.root, "none", ignored=True)
        body = self._dom(html, 1, "BODY")
        self.body = (body, self._ax(body, html_ax, "none", ignored=True))
        for element in spec.get("body") or []:
            self.element(self.body, element)

    def string(self, value: str) -> int:
        self.strings.append(value)
        return len(self.strings) - 1

    def _dom(self, parent, kind, name, *, value=None, attrs=None, rendered=True) -> int:
        index = len(self.nodes["parentIndex"])
        self.nodes["parentIndex"].append(parent)
        self.nodes["nodeType"].append(kind)
        self.nodes["nodeName"].append(self.string(name))
        self.nodes["nodeValue"].append(-1 if value is None else self.string(value))
        self.nodes["backendNodeId"].append(self.backend)
        self.backend += 1
        self.nodes["attributes"].append(
            [
                i
                for key, item in (attrs or {}).items()
                for i in (self.string(key), self.string(str(item)))
            ]
        )
        if rendered:
            self.layout.append(index)
        return index

    def _ax(self, index, parent, role, *, name=None, value=None, props=None, ignored=False):
        node = {
            "nodeId": str(len(self.ax) + 1),
            "ignored": ignored,
            "role": {"type": "role", "value": role},
            "childIds": [],
        }
        if index is not None:
            node["backendDOMNodeId"] = self.nodes["backendNodeId"][index]
        if name is not None:
            node["name"] = {"type": "computedString", "value": name, "sources": []}
        if value is not None:
            node["value"] = {"type": "string", "value": value}
        if props:
            node["properties"] = [
                {"name": key, "value": {"type": "token", "value": item}}
                for key, item in props.items()
            ]
        if parent is not None:
            node["parentId"] = parent["nodeId"]
            parent["childIds"].append(node["nodeId"])
        self.ax.append(node)
        return node

    def text(self, parent, text: str, *, rendered=True) -> None:
        dom_parent, ax_parent = parent
        index = self._dom(dom_parent, 3, "#text", value=text, rendered=rendered)
        node = self._ax(index, ax_parent, "StaticText", name=text)
        self._ax(None, node, "InlineTextBox", name=text)

    def element(self, parent, spec: dict) -> None:
        if "tag" not in spec:
            self.text(parent, spec["text"], rendered=spec.get("rendered", True))
            return
        dom_parent, ax_parent = parent
        rendered = spec.get("rendered", True)
        index = self._dom(dom_parent, 1, spec["tag"], attrs=spec.get("attrs"), rendered=rendered)
        if spec.get("value") is not None:
            self.values[index] = spec["value"]
        if spec.get("checked"):
            self.checked.append(index)
        if spec.get("selected"):
            self.selected.append(index)
        node = ax_parent
        if spec.get("role") is not None:
            node = self._ax(
                index,
                ax_parent,
                spec["role"],
                name=spec.get("name"),
                value=spec.get("ax_value", spec.get("value")),
                props=spec.get("props"),
            )
        here = (index, node)
        if spec.get("text") is not None:
            self.text(here, spec["text"], rendered=rendered)
        for child in spec.get("children") or []:
            self.element(here, child)

    def build(self) -> tuple[dict, dict]:
        """The DOM snapshot as the browser service returns it (after its credential redaction)
        and the accessibility tree of the same moment."""
        strings = list(self.strings)

        def string(value):
            strings.append(value)
            return len(strings) - 1

        values = sorted(self.values.items())
        snapshot = {
            "strings": strings,
            "documents": [
                {
                    "documentURL": self.url_string,
                    "title": string(self.title),
                    "frameId": string("FRAME-MAIN"),
                    "nodes": {
                        **copy.deepcopy(self.nodes),
                        "inputValue": {
                            "index": [index for index, _ in values],
                            "value": [string(value) for _, value in values],
                        },
                        "inputChecked": {"index": list(self.checked)},
                        "optionSelected": {"index": list(self.selected)},
                        "shadowRootType": {"index": [], "value": []},
                    },
                    "layout": {"nodeIndex": list(self.layout)},
                }
            ],
        }
        return redact_form_secrets(snapshot), {"nodes": copy.deepcopy(self.ax)}


def page_text(spec: dict) -> str:
    """The rendered text of a page structure, in document order."""
    parts: list[str] = []

    def walk(element):
        if element.get("rendered", True) is False:
            return
        if element.get("text") is not None:
            parts.append(element["text"])
        for child in element.get("children") or []:
            walk(child)

    for element in spec.get("body") or []:
        walk(element)
    return "\n".join(parts)


# Calls that put input into a page (what a person would see the website receive).
INPUTS = frozenset(
    {
        "click",
        "type_text",
        "fill",
        "press_key",
        "select_option",
        "select_options",
        "set_checked",
        "drag",
        "hover",
        "upload_file",
        "handle_dialog",
    }
)


class FakeBrowser:
    """The managed browser's calls, answered from the scenario's pages (``fakes.browser``).

    ``pages`` maps an address to a page structure; the first is open when the browser starts.
    Every call is recorded; input changes the page's generation (so the observation after it
    sees the page answer) but never its structure.
    """

    def __init__(self, spec: dict):
        self.pages = {page["url"]: page for page in spec.get("pages") or []}
        start = spec.get("start") or next(iter(self.pages), "about:blank")
        self.calls: list[dict] = []
        self.generation = 1
        self._open(start)

    def _open(self, url: str) -> None:
        page = self.pages.get(url)
        if page is None:
            raise BrowserPageUnreachableError("net::ERR_NAME_NOT_RESOLVED")
        self.url, self.spec = url, page
        self.dom, self.tree = PageBuilder(page).build()
        self.generation += 1

    def _record(self, name: str, *args, **kwargs) -> None:
        kwargs.pop("cancel", None)
        self.calls.append(
            {"call": name, "args": json.loads(json.dumps(args, default=str)), "kwargs": kwargs}
        )

    def _input(self, name: str, *args, **kwargs):
        self._record(name, *args, **kwargs)
        # The page answers input: its generation moves on, its structure stays.
        self.dom = copy.deepcopy(self.dom)
        self.dom["strings"].append("acted")
        self.generation += 1
        return {}

    # Reads

    def snapshot(self, owner=None, session_id=None, **kwargs):
        self._record("snapshot", **kwargs)
        kind = kwargs.get("kind", "dom")
        if kind == "text":
            return {"text": page_text(self.spec), "command_generation": self.generation}
        data = self.tree if kind == "accessibility" else self.dom
        return {
            "data": copy.deepcopy(data),
            "command_generation": self.generation,
            "truncated": False,
        }

    def pages_open(self) -> dict:
        return {
            "pages": [{"page_id": "root", "url": self.url, "active": True}],
            "command_generation": self.generation,
        }

    def status(self, owner=None, session_id=None, **kwargs):
        return SimpleNamespace(state="running", command_generation=self.generation)

    def page_report(self, owner=None, session_id=None, **kwargs):
        self._record("page_report", **kwargs)
        return {"failed_requests": [], "script_errors": 0, "command_generation": self.generation}

    def read_value(self, owner=None, session_id=None, *args, **kwargs):
        self._record("read_value", *args, **kwargs)
        return {"value": "", "command_generation": self.generation}

    # Navigation

    def navigate(self, owner=None, session_id=None, url=None, *args, **kwargs):
        self._record("navigate", url, *args, **kwargs)
        self._open(url)
        return {}

    def history(self, owner=None, session_id=None, *args, **kwargs):
        self._record("history", *args, **kwargs)
        return {}

    def switch_page(self, owner=None, session_id=None, *args, **kwargs):
        self._record("switch_page", *args, **kwargs)
        return {}

    def close_page(self, owner=None, session_id=None, *args, **kwargs):
        self._record("close_page", *args, **kwargs)
        return {}

    def scroll(self, owner=None, session_id=None, *args, **kwargs):
        self._record("scroll", *args, **kwargs)
        return {}

    def wait_for(self, owner=None, session_id=None, *args, **kwargs):
        self._record("wait_for", *args, **kwargs)
        return {}

    def __getattr__(self, name):
        if name in INPUTS:
            return lambda owner=None, session_id=None, *args, **kwargs: self._input(
                name, *args, **kwargs
            )
        raise AttributeError(name)

    def received(self) -> list[dict]:
        """The input the website received, in order."""
        return [call for call in self.calls if call["call"] in INPUTS]


class _LocalBrowser:
    """The browser as an executor reaches it locally: ``pages`` is a call, not a property."""

    def __init__(self, browser: FakeBrowser):
        self._browser = browser

    def pages(self, owner=None, session_id=None, **kwargs):
        return self._browser.pages_open()

    def __getattr__(self, name):
        return getattr(self._browser, name)


class LocalBrowserExecutor:
    """What a ``BrowserComputers`` factory returns: one owl's browser on its computer."""

    def __init__(self, bot_id: str, browser: FakeBrowser):
        self.owner = "executor-" + bot_id
        self.session_id = "session-" + bot_id
        self.browser = _LocalBrowser(browser)
        self.preview_urls = lambda: ()
        self.closed = False

    def alive(self):
        return not self.closed

    def close(self):
        self.closed = True

    def capture(self):
        return _PIXEL


# A 1x1 PNG: what a screenshot of the fake browser shows.
_PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYPgPAAEDAQAIicLsAAAAAElFTkSuQmCC"
)


class LocalBrowsers:
    """A ``BrowserComputers`` factory: each owl's browser computer serves the scenario's pages."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.browsers: list[FakeBrowser] = []
        self.executors: list[LocalBrowserExecutor] = []

    def __call__(self, bot_id, cancel):
        browser = FakeBrowser(self.spec)
        executor = LocalBrowserExecutor(bot_id, browser)
        self.browsers.append(browser)
        self.executors.append(executor)
        return executor

    def evidence(self) -> dict:
        return {
            "computers": len(self.executors),
            "closed": sum(1 for e in self.executors if e.closed),
            "calls": [call for b in self.browsers for call in b.calls],
            "inputs": [call for b in self.browsers for call in b.received()],
        }


# --------------------------------------------------------------------------------------- Cloud


class CloudProvider:
    """A compute provider whose computers answer the Cloud worker's protocol themselves.

    ``fakes.cloud_computer``: ``{"browser": <as fakes.browser>, "reply_too_large":
    [{"method", "kind"?, "times"?}]}``. A browser call that matches a ``reply_too_large``
    rule is sent but its receipt is too large for the host to read, as a page whose snapshot
    is over the host's reply allowance.
    """

    scope = "scenario-scope"

    def __init__(self, spec: dict):
        self.spec = spec
        self.allocations: dict[str, list] = {}
        self.created = 0
        self.deleted: list[str] = []
        self.requests: list[dict] = []
        self.browsers: list[FakeBrowser] = []
        self.rules = [dict(rule) for rule in spec.get("reply_too_large") or []]
        self.too_large: list[dict] = []
        self._lock = threading.Lock()

    # The provider's lifecycle

    def find(self, identity):
        for ref, state in self.allocations.values():
            if ref.identity == identity and state != "missing":
                return ref
        return None

    def create(self, identity, spec):
        from alysis_code.bots.compute import ComputerRef

        ref = ComputerRef("scenario", self.scope, identity.allocation_id, identity)
        with self._lock:
            self.allocations[ref.resource_id] = [ref, "running"]
            self.created += 1
        return ref

    def status(self, ref):
        from alysis_code.bots.compute import ComputerStatus

        state = self.allocations[ref.resource_id][1]
        return ComputerStatus(state, state)

    def stop(self, ref):
        self.allocations[ref.resource_id][1] = "stopped"
        return self.status(ref)

    def delete(self, ref):
        self.allocations[ref.resource_id][1] = "missing"
        self.deleted.append(ref.resource_id)
        return self.status(ref)

    def upload(self, ref, path, chunks, *, size, cancel):
        for _ in chunks:
            pass

    # The worker's side

    def _browser(self) -> FakeBrowser:
        if not self.browsers:
            self.browsers.append(FakeBrowser(self.spec.get("browser") or {}))
        return self.browsers[-1]

    def _too_large(self, method: str, kwargs: dict) -> bool:
        for rule in self.rules:
            if rule.get("method") != method:
                continue
            if "kind" in rule and kwargs.get("kind", "dom") != rule["kind"]:
                continue
            if rule.get("times", 1) <= 0:
                continue
            rule["times"] = rule.get("times", 1) - 1
            self.too_large.append({"method": method, **kwargs})
            return True
        return False

    def run(self, ref, argv, *, timeout):
        from alysis_code.bots.compute import CommandReceipt, ComputeReplyTooLarge

        if argv[-2] == "start":
            return CommandReceipt(
                0, json.dumps({"ready": True, "session": argv[-1], "uid": 1000}), ""
            )
        request = json.loads(base64.b64decode(argv[-1]))
        reply = {key: request[key] for key in ("id", "session") if key in request}
        if argv[-3] == "screen":
            reply["result"] = {"png": base64.b64encode(_PIXEL).decode()}
            return CommandReceipt(0, json.dumps(reply), "")
        operation, arguments = request.get("operation"), request.get("arguments") or {}
        self.requests.append({"operation": operation, "method": arguments.get("method")})
        if operation == "browser_call":
            method = arguments["method"]
            args, kwargs = arguments.get("args") or [], arguments.get("kwargs") or {}
            browser = self._browser()
            if self._too_large(method, kwargs):
                # Sent, and answered, but the receipt is over what the host reads.
                raise ComputeReplyTooLarge("The receipt exceeded the host's reply allowance.")
            try:
                if method == "pages":
                    result = browser.pages_open()
                elif method == "status":
                    result = {"state": "running", "command_generation": browser.generation}
                else:
                    result = getattr(browser, method)(None, None, *args, **kwargs)
            except BrowserPageUnreachableError:
                reply.update(
                    error="unreachable",
                    error_type="BrowserPageUnreachableError",
                    error_detail="net::ERR_NAME_NOT_RESOLVED",
                )
                return CommandReceipt(0, json.dumps(reply), "")
            reply["result"] = result if result is not None else {}
            return CommandReceipt(0, json.dumps(reply, default=str), "")
        if operation == "browser_start":
            from alysis_code.bots.cloud_browser_worker import FEATURES

            reply["result"] = {"ready": True, "features": list(FEATURES)}
        else:
            reply["result"] = {"ready": True}
        return CommandReceipt(0, json.dumps(reply), "")

    def evidence(self) -> dict:
        return {
            "computers": self.created,
            "retired_or_released": len(self.deleted),
            "states": [state for _, state in self.allocations.values()],
            "too_large": list(self.too_large),
            "inputs": [call for b in self.browsers for call in b.received()],
        }
