"""Provider-free integration review; source grant never authorizes peer/report actions."""

import threading

import pytest
from test_bots_browser_runtime import BrowserFixture

from alysis_code.bots.cooperation import PeerRunner, PeerStore
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, ToolCall


@pytest.mark.parametrize("malicious_report", [False, True])
def test_accepted_site_authority_stops_at_peer_boundary(tmp_path, malicious_report):
    store = Store(tmp_path / "integrated.db")
    source = store.save_bot("owner", BotInput(name="Source", purpose="Own review"))
    recipient = store.save_bot("owner", BotInput(name="Reviewer", purpose="Review only"))
    chat = store.create_chat("owner", source["id"], "Review")
    store.enqueue(
        "owner",
        chat["id"],
        TaskInput(message="Prepare an approved site task", request_id="prepare-site"),
    )
    proposal_task = store.claim("owner")
    proposals = ProposalStore(store, "owner")
    proposal = proposals.propose(
        proposal_task,
        ActionProposal(
            question="Use this site?",
            instructions="Inspect the page and ask Reviewer for text review.",
            browser_origin="https://example.org",
        ),
    )
    store.finish("owner", proposal_task["id"], "completed", content="Review the site and task.")
    proposals.decide(
        source["id"],
        proposal["id"],
        ActionDecision(
            action="accept",
            expected_revision=proposal["revision"],
            browser_origin=proposal["browser_origin"],
        ),
    )
    task = store.claim("owner")
    assert proposals.browser_origins(task) == ("https://example.org",)
    calls = []
    browser = BrowserFixture()

    class Client:
        model = "peer-browser-integration-fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                names = {s["function"]["name"] for s in kwargs["tools"]}
                assert "browser_click" in names and "request_peer" in names
                return LLMResponse("", [ToolCall("before", "browser_click", {})], {})
            if len(calls) == 2:
                return LLMResponse(
                    "",
                    [
                        ToolCall(
                            "review",
                            "request_peer",
                            {
                                "recipient_bot_id": recipient["id"],
                                "brief": "Give a bounded text review.",
                            },
                        )
                    ],
                    {},
                )
            if len(calls) == 3:
                assert kwargs["messages"][0]["content"].startswith("You are the receiving peer")
                assert kwargs["tools"] is None
                return LLMResponse("Saved peer review. A site grant is not shared.", [], {})
            assert len(calls) == 4 and kwargs["tools"] is None
            assert "Saved peer review." in str(kwargs["messages"])
            return (
                LLMResponse("", [ToolCall("after", "browser_click", {})], {})
                if malicious_report
                else LLMResponse("The reviewer returned a text review.", [], {})
            )

    runtime = BotRuntime(store, "owner", lambda _: Client(), browser_computers=browser)
    token = EventCancellationToken(threading.Event())
    runtime.run(task, token)
    assert store.task("owner", task["id"])["status"] == "waiting"
    assert len(browser.calls) == 1
    peers = PeerStore(store, "owner")
    PeerRunner(peers, runtime.client_factory, runtime.limits, threading.Event()).run(peers.claim())
    report = store.claim("owner")
    assert report["id"] == task["id"]
    # The exact source task still has its grant, yet the enforced report phase
    # offers no tools and rejects a model attempting an extra browser action.
    assert proposals.browser_origins(report) == ("https://example.org",)
    runtime.run(report, token)
    assert len(browser.calls) == 1
    assert browser.eligibility == [(task["id"], True), (task["id"], False)]
    item = peers.for_task(task["id"])
    assert item["status"] == "completed" and item["result"].startswith("Saved peer review.")
    assert item["source_status"] == ("failed" if malicious_report else "completed")
    assert (item["reported_at"] is None) is malicious_report
    assert store.usage("owner")["calls"] == 4

    other_chat = store.create_chat("owner", recipient["id"], "Recipient direct user task")
    store.enqueue(
        "owner",
        other_chat["id"],
        TaskInput(message="Inspect my permissions", request_id="recipient"),
    )
    assert proposals.browser_origins(store.claim("owner")) == ()
