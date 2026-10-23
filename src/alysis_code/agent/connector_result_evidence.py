"""Bounded link observations; API success is not proof of a delivered outcome."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..web_research import extract_result_web_urls, normalize_web_url

# The automatic phase checks linked content, not arbitrary task work. Do not
# infer safety from connector annotations, tool descriptions or model arguments.
CONNECTOR_VERIFICATION_READ_TOOLS = frozenset({"web_fetch", "web_search"})


def connector_verification_blocked_result() -> dict[str, Any]:
    return {
        "error": "This tool cannot run during automatic link verification.",
        "error_code": "connector_verification_read_only",
        "dispatched": False,
        "guidance": (
            "Only the available built-in web read tools may run in this phase. "
            "Do not repeat or start a mutation. Report verification as unavailable "
            "or incomplete if these reads cannot establish the result."
        ),
    }


CONNECTOR_COMPLETION_GUIDANCE = (
    "External results: a connector's successful operation confirms only what its response "
    "actually establishes. Creation, configuration, an accepted job, a repository link or a "
    "source-file link does not establish a working published deliverable. When the user requests "
    "a working result, inspect its status and actual content with authorized read tools before "
    "claiming completion. Compare that observation with the original request. If verification "
    "fails or is unavailable, report the completed steps and the remaining unverified result. "
    "Do not repeat a mutation merely because its downstream result is not ready."
)


def _urls(value: Any) -> set[str]:
    return {item["normalized_url"] for item in extract_result_web_urls(value)}


@dataclass
class ConnectorLinkEvidence:
    """Track exact, current-turn references and later successful content fetches.

    A fetch observes availability only. Neither a URL nor HTTP 2xx certifies that
    an arbitrary user goal was fulfilled. Model assessment remains separate.
    """

    references: set[str] = field(default_factory=set)
    checked: set[str] = field(default_factory=set)

    def observe(self, name: str, result: dict[str, Any]) -> None:
        if name == "web_fetch":
            urls = {
                url
                for key in ("url", "final_url")
                if (url := normalize_web_url(result.get(key))) is not None
            }
            self.checked.difference_update(urls)
            status = result.get("status_code")
            if (
                not result.get("error")
                and not result.get("error_code")
                and type(status) is int
                and 200 <= status < 300
                and isinstance(result.get("content"), str)
                and result["content"].strip()
            ):
                self.checked.update(urls & self.references)
            return
        # These are host wrappers from registered MCP tools, not model assertions.
        if not result.get("server_id") or not isinstance(result.get("text"), str):
            return
        if not result["text"].startswith("[MCP_UNTRUSTED_TEXT]\n"):
            return
        if result.get("is_error") or result.get("error") or result.get("error_code"):
            return
        urls = _urls(result)
        self.checked.difference_update(urls)
        for url in sorted(urls):
            if len(self.references) >= 128:
                break
            self.references.add(url)

    def assess(self, final_text: str) -> dict[str, Any]:
        delivered = _urls(final_text) & self.references
        return {
            "unchecked_links": sorted(delivered - self.checked)[:20],
            "fetched_links": sorted(delivered & self.checked)[:20],
            "proves_requested_outcome": False,
        }

    @staticmethod
    def nudge(evidence: dict[str, Any]) -> str:
        return (
            CONNECTOR_COMPLETION_GUIDANCE
            + "\nThe proposed reply includes connector-returned links without a subsequent "
            "successful content fetch. Verify the requested deliverable if possible; otherwise "
            "give an honest partial/unverified result. Merely fetching a repository or source "
            "viewer is not evidence that a requested hosted site works. Do not invent a URL "
            "or retry a write. This automatic phase permits only the available built-in "
            "web read tools; other tools will not dispatch. Links: "
            + json.dumps(evidence["unchecked_links"], ensure_ascii=True)
        )
