"""Real Linux + synthetic clients: apps must end at the peer handoff boundary.

Requires the integrated engine's task-approved offline desktop modules. Run in
an isolated network-none Linux lab with that committed source on PYTHONPATH.
Only temporary state is created; no model provider or existing service is used.
The positive control proves an app ran before handoff, then checks no delayed
write after Stop. Model calls in the result are explicitly synthetic.
"""

import json
import tempfile
import time
from pathlib import Path

from alysis_code.bots import desktop, desktop_linux
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.cooperation import PeerStore
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store
from alysis_code.llm.types import LLMResponse, ToolCall

with tempfile.TemporaryDirectory(prefix="alysis-peer-20261019-handoff-") as temporary:
    store = Store(Path(temporary) / "study.db")
    bot = store.save_bot("owner", BotInput(name="Source", purpose="Review work"))
    peer = store.save_bot("owner", BotInput(name="Peer", purpose="Text review"))
    chat = store.create_chat("owner", bot["id"], "Synthetic handoff")
    store.enqueue(
        "owner",
        chat["id"],
        TaskInput(message="Prepare an offline task", request_id="prepare-offline"),
    )
    preparation = store.claim("owner")
    proposals = ProposalStore(store, "owner")
    proposal = proposals.propose(
        preparation,
        ActionProposal(
            question="Allow offline work?",
            instructions="Launch the synthetic application and request review.",
            offline_computer=True,
        ),
    )
    store.finish("owner", preparation["id"], "completed", content="Review the offline task.")
    proposals.decide(
        bot["id"],
        proposal["id"],
        ActionDecision(action="accept", expected_revision=1, offline_computer=True),
    )
    task = store.claim("owner")
    instances = []

    def factory(owner, workspace, **kwargs):
        executor = desktop_linux.OfflineLinuxDesktop(owner, workspace, **kwargs)
        instances.append(executor)
        return executor

    manager = desktop.DesktopComputers("owner", ComputerViews(), factory=factory)

    class Client:
        model = "peer-desktop-stop-fixture"
        supports_tool_calling = True
        calls = 0

        def chat(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    "",
                    [
                        ToolCall(
                            "launch",
                            "computer_launch",
                            {
                                "command": "touch /workspace/launched; sleep 3; touch /workspace/after-stop"
                            },
                        )
                    ],
                    {},
                )
            assert self.calls == 2
            workspace = instances[0].workspace
            deadline = time.monotonic() + 2
            while not (workspace / "launched").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert (workspace / "launched").exists(), "Positive control: application did not start"
            return LLMResponse(
                "",
                [
                    ToolCall(
                        "peer",
                        "request_peer",
                        {"recipient_bot_id": peer["id"], "brief": "Review the synthetic work."},
                    )
                ],
                {},
            )

    client = Client()
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    try:
        runtime.run(task, runtime.task_token(task["id"]))
        assert store.task("owner", task["id"])["status"] == "waiting"
        workspace = instances[0].workspace
        closed_at_handoff = not instances[0].alive()
        runtime.cancel(task["id"])
        manager.reap()
        alive_after_stop = instances[0].alive()
        time.sleep(3.2)
        evidence = {
            "kind": "Synthetic provider; real Linux application lifecycle",
            "application_started_before_handoff": True,
            "computer_closed_at_handoff": closed_at_handoff,
            "task_status": store.task("owner", task["id"])["status"],
            "peer_status": PeerStore(store, "owner").for_task(task["id"])["status"],
            "computer_alive_after_stop": alive_after_stop,
            "delayed_write_after_stop": (workspace / "after-stop").exists(),
            "fixture_provider_calls": store.usage("owner")["calls"],
            "live_model_calls": 0,
        }
    finally:
        manager.close()
    evidence["computer_closed_after_cleanup"] = not instances[0].alive()
    evidence["workspace_removed_after_cleanup"] = not workspace.exists()
    print(json.dumps(evidence))
    assert evidence["computer_closed_at_handoff"]
    assert evidence["task_status"] == evidence["peer_status"] == "cancelled"
    assert not evidence["computer_alive_after_stop"]
    assert not evidence["delayed_write_after_stop"]
    assert evidence["fixture_provider_calls"] == 2
    assert evidence["computer_closed_after_cleanup"] and evidence["workspace_removed_after_cleanup"]
