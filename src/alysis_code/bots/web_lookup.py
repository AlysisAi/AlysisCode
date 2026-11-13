"""Looking things up for owls without a computer: web search and reading public pages.

Grok-style research needs neither a desktop nor a browser: a search, then the pages it
points to, read as text. Both reuse the engine's own hardened pieces: the keyless search
adapter, and web_fetch with its address checks (public http(s) hosts only, no private,
loopback or link-local addresses, bounded size and time, redirects re-checked). Failures
come back as plain reasons the owl can act on (try another source), never as raw errors.
"""

from __future__ import annotations

from typing import Any

from .store import Conflict

_SNIPPET = 400


def web_tools_available() -> bool:
    """Off when the installation's web-tools policy is off (ALYSIS_WEB_TOOLS=off)."""
    from ..config import resolve_web_tools_enabled
    from ..tools.web_search_ddgs import ddgs_package_available

    try:
        return bool(resolve_web_tools_enabled(None)) and ddgs_package_available()
    except Exception:  # noqa: BLE001 - a broken policy read must not take tools away silently
        return False


def web_search(query: str, max_results: int) -> dict[str, Any]:
    from ..tools.web_search_ddgs import DdgsSearchError, ddgs_search

    try:
        found = ddgs_search(query=query, max_results=max_results, timeout_s=30.0)
    except DdgsSearchError as exc:
        raise Conflict(
            "The web search did not answer this time. Try again with other wording, "
            "or read a known source with web_fetch."
        ) from exc
    results = []
    for item in found.get("sources") or found.get("citations") or []:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        result = {"title": str(item.get("title") or "")[:300], "url": str(item["url"])}
        snippet = str(item.get("snippet") or "").strip()
        if snippet:
            result["snippet"] = snippet[:_SNIPPET]
        results.append(result)
    return {
        "query": query,
        "results": results,
        "note": "Snippets are leads; read pages before relying on them.",
    }


def web_fetch(url: str, max_chars: int) -> dict[str, Any]:
    from ..tools.web import WebFetchError
    from ..tools.web import web_fetch as fetch

    try:
        # A long page (a big article, a busy news site) is read up to the size limit
        # rather than refused.
        page = fetch(
            url=url,
            max_chars=max_chars,
            truncate_oversized=True,
            main_content=True,
            include_links=True,
            # Room for a long page or an official PDF (opening hours, price lists, forms).
            max_bytes=6_000_000,
            read_pdf=True,
        )
    except WebFetchError as exc:
        reason = getattr(exc, "remote_site_reason", None)
        raise Conflict(
            f"This page could not be read ({reason}). Use another source."
            if reason
            else f"This page could not be read: {str(exc)[:300]}"
        ) from exc
    content = str(page.get("content") or "")
    return {
        "url": page.get("final_url") or page.get("url") or url,
        "title": page.get("title") or "",
        "status_code": page.get("status_code"),
        "content_type": page.get("content_type"),
        "text": content,
        "truncated": bool(page.get("truncated")),
        "links": page.get("links") or [],
        **(
            {}
            if content.strip()
            else {
                "note": "The page showed no readable text without its scripts; try another source."
            }
        ),
    }
