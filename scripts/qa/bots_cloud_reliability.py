"""Opt-in real Azure/GLM reliability study. Caller supplies authenticated provider.

Loss is injected AFTER Azure returns an actual command receipt. Stop waits for a
marker written by the guest. No credential is written to evidence or the guest.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path
from uuid import uuid4

from alysis_code.bots.app import create_app
from alysis_code.bots.cloud_desktop import CloudDesktopFactory
from alysis_code.bots.compute import ComputeUncertain
from alysis_code.bots.models import BotInput, FileInput, TaskInput
from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore
from alysis_code.cancellation import EventCancellationToken


class ObservedProvider:
    def __init__(self, provider):
        self.provider = provider
        self.refs = []
        self.commands = []
        self.mode = None
        self.dispatched = threading.Event()
        self.dropped_receipt = None

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def create(self, identity, spec):
        ref = self.provider.create(identity, spec)
        self.refs.append(ref)
        return ref

    def run(self, ref, argv, *, timeout):
        request = None
        if len(argv) >= 3 and argv[-3] == "request":
            request = json.loads(base64.b64decode(argv[-1]))
        action = request and request["operation"] == "run"
        if action:
            self.commands.append(request["arguments"])
            self.dispatched.set()
        receipt = self.provider.run(ref, argv, timeout=timeout)
        if action and self.mode == "lost":
            self.dropped_receipt = json.loads(receipt.stdout)
            raise ComputeUncertain("Injected response loss after actual execution")
        return receipt


def checks(provider, spec, settings, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    observed = ObservedProvider(provider)
    factory = CloudDesktopFactory(
        settings.owner, observed, spec, output / "leases", persist_workspace=True
    )
    app = create_app(settings, cloud_computers=factory)
    store, runtime = app.state.store, app.state.runtime
    desktop = runtime.desktop_computers
    bot = store.save_bot(
        settings.owner,
        BotInput(
            name="Reliability verification",
            purpose="Carry out explicitly requested offline file work; verify actual results.",
        ),
    )
    chat = store.create_chat(settings.owner, bot["id"], "Reliability verification")
    evidence = {
        "passed": False,
        "mode": "real Azure + real GLM, injected receipt loss",
        "stages": {},
    }
    token = EventCancellationToken(threading.Event())

    def task(message):
        queued = store.enqueue(
            settings.owner, chat["id"], TaskInput(message=message, request_id=uuid4().hex)
        )
        claimed = store.claim(settings.owner)
        assert claimed["id"] == queued["id"]
        return claimed

    def approved(message):
        proposed = task("Prepare this explicitly authorized synthetic verification.")
        proposals = ProposalStore(store, settings.owner)
        item = proposals.propose(
            proposed,
            ActionProposal(
                question="Run the synthetic offline check?",
                instructions=message,
                offline_computer=True,
            ),
        )
        store.finish(
            settings.owner,
            proposed["id"],
            "completed",
            content="Synthetic scope approved by the study operator.",
        )
        proposals.decide(
            bot["id"],
            item["id"],
            ActionDecision(action="accept", expected_revision=1, offline_computer=True),
        )
        return store.claim(settings.owner)

    def save():
        (output / "cloud-receipt.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2)
        )

    try:
        baseline = task("Create the synthetic recovery baseline.")
        result = desktop.call(
            baseline,
            "computer_run",
            {"command": "printf 'saved Ω 東京\n' > baseline.txt; cat baseline.txt", "timeout": 10},
            token,
            approved=True,
        )
        assert result["exit_code"] == 0 and "saved Ω 東京" in result["output"]
        desktop.release(baseline["id"], cancelled=False)
        store.finish(
            settings.owner, baseline["id"], "completed", content="Verified baseline saved."
        )
        kept = store.save_file(
            settings.owner,
            bot["id"],
            FileInput(name="kept.txt", content="persistent library Ω 東京"),
        )
        evidence["stages"]["baseline"] = True
        save()

        observed.mode = "lost"
        before = len(observed.commands)
        lost = approved(
            "Use computer_run now with timeout 10 to execute: printf 'uncertain-write\\n' >> attempts.txt; cat attempts.txt . This is one synthetic test command. Do not delegate or start background work. Report only the verified receipt."
        )
        runtime.run(lost, runtime.task_token(lost["id"]))
        final = store.task(settings.owner, lost["id"])
        assert final["status"] == "interrupted", final["status"]
        assert len(observed.commands) - before == 1
        assert observed.dropped_receipt and observed.dropped_receipt["result"]["exit_code"] == 0
        assert "uncertain-write" in observed.dropped_receipt["result"]["output"]
        assert provider.status(observed.refs[-1]).state == "missing"
        evidence["stages"]["lost_response"] = {
            "task_id": lost["id"],
            "status": final["status"],
            "dispatches": 1,
            "actual_execution_verified": True,
            "vm_deleted": True,
            "error": final["error"],
        }
        save()

        observed.mode = None
        observed.dispatched.clear()
        stopped = approved(
            "Use computer_run with timeout 30 to run exactly this synthetic bounded check now: printf 'started\\n' > started.flag; sleep 25; printf 'should not survive\\n' > late.txt . Do not use background work or computer_launch. Report only the actual receipt."
        )
        runner = threading.Thread(
            target=runtime.run, args=(stopped, runtime.task_token(stopped["id"])), daemon=True
        )
        runner.start()
        assert observed.dispatched.wait(90), "No command dispatched"
        deadline = time.monotonic() + 15
        marker = False
        while time.monotonic() < deadline:
            probe = provider.run(
                observed.refs[-1],
                [
                    "/usr/bin/test",
                    "-f",
                    f"/tmp/alysis-cloud-{observed.refs[-1].identity.allocation_id}/workspace/started.flag",
                ],
                timeout=5,
            )
            if probe.exit_code == 0:
                marker = True
                break
            time.sleep(0.3)
        assert marker, "Guest command did not start"
        began = time.monotonic()
        runtime.cancel(stopped["id"])
        runner.join(90)
        assert not runner.is_alive()
        final = store.task(settings.owner, stopped["id"])
        assert final["status"] == "cancelled", final["status"]
        assert provider.status(observed.refs[-1]).state == "missing"
        evidence["stages"]["stop"] = {
            "task_id": stopped["id"],
            "status": final["status"],
            "guest_started": True,
            "seconds_to_shutdown": round(time.monotonic() - began, 2),
            "vm_deleted": True,
        }
        save()

        recovery = task("Verify previous successful state after both interrupted tasks.")
        restored = desktop.call(
            recovery,
            "computer_run",
            {
                "command": "cat baseline.txt; test ! -e attempts.txt && test ! -e started.flag && test ! -e late.txt",
                "timeout": 10,
            },
            token,
            approved=True,
        )
        assert restored["exit_code"] == 0 and "saved Ω 東京" in restored["output"]
        assert restored["workspace_reopened"]
        assert (
            store.file(settings.owner, bot["id"], kept["id"])["content"]
            == "persistent library Ω 東京"
        )
        desktop.release(recovery["id"], cancelled=False)
        store.finish(
            settings.owner,
            recovery["id"],
            "completed",
            content="Baseline and saved library file verified.",
        )
        evidence["stages"]["recovery"] = {
            "previous_successful_workspace_restored": True,
            "interrupted_writes_not_promoted": True,
            "library_preserved": True,
        }
        evidence["passed"] = True
    except BaseException as exc:
        evidence["error_type"] = type(exc).__name__
        raise
    finally:
        runtime.stop()
        evidence["usage"] = store.usage(settings.owner)
        evidence["computers"] = [
            {"id": ref.resource_id, "state": provider.status(ref).state} for ref in observed.refs
        ]
        save()
    return evidence
