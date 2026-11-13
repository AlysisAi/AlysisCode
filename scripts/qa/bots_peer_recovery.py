"""Kill an owned peer worker mid-call; verify durable recovery without a provider.

Run with Python locally or in an isolated Linux container. This seeds a valid
request directly; only the peer call and recovery report use synthetic clients.
No credentials, network service, existing database or model provider is used.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from alysis_code.bots.cooperation import PeerRequest, PeerStore
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store
from alysis_code.llm.types import LLMResponse

CHILD = """
import sys, time
from pathlib import Path
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store
class BlockingPeer:
    model = 'peer-crash-fixture'
    supports_tool_calling = True
    def chat(self, **kwargs):
        assert kwargs['messages'][0]['content'].startswith('You are the receiving peer')
        assert kwargs['tools'] is None
        Path(sys.argv[2]).write_text('peer fixture entered')
        while True:
            time.sleep(.1)
runtime = BotRuntime(Store(Path(sys.argv[1])), 'owner', lambda _: BlockingPeer())
runtime.start()
while True:
    time.sleep(.1)
"""


def until(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Peer crash fixture did not reach the expected state")


def verify(root: Path) -> dict:
    store = Store(root / "peer-crash.db")
    source = store.save_bot("owner", BotInput(name="Source", purpose="Coordinate review"))
    peer = store.save_bot("owner", BotInput(name="Reviewer", purpose="Review the draft"))
    chat = store.create_chat("owner", source["id"], "Synthetic crash recovery")
    store.enqueue(
        "owner", chat["id"], TaskInput(message="Ask for a review", request_id="crash-fixture")
    )
    task = store.claim("owner")
    peers = PeerStore(store, "owner")
    request = PeerRequest(recipient_bot_id=peer["id"], brief="Review this synthetic request")
    peers.submit(
        task,
        request,
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "peer-request",
                    "type": "function",
                    "function": {"name": "request_peer", "arguments": request.model_dump_json()},
                }
            ],
        },
        8,
    )
    marker = root / "peer-entered"
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(store.path), str(marker)],
        env={
            "PATH": os.defpath,
            "PYTHONPATH": os.pathsep.join(sys.path),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    try:
        until(marker.exists)
        assert child.poll() is None
        assert peers.for_task(task["id"])["status"] == "running"
        assert store.task("owner", task["id"])["usage"][0]["status"] == "started"
        # The only process killed is the fault-injection child created above.
        child.kill()
        child.wait(timeout=5)
        assert child.returncode == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)

    calls = []
    output = "The peer review was interrupted; no result is available."

    class RecoveryFixture:
        model = "peer-recovery-report-fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            calls.append(kwargs)
            assert len(calls) == 1, "A completed recovery report was replayed"
            assert not kwargs["messages"][0]["content"].startswith("You are the receiving peer")
            assert kwargs["tools"] is None
            receipts = [m for m in kwargs["messages"] if m["role"] == "tool"]
            receipt = json.loads(receipts[-1]["content"])
            assert receipt["status"] == "interrupted"
            assert not receipt["result"]
            return LLMResponse(output, [], {})

    runtime = BotRuntime(Store(store.path), "owner", lambda _: RecoveryFixture())
    runtime.start()  # Also proves the killed process released the database lease.
    try:
        until(lambda: store.task("owner", task["id"])["status"] == "completed")
    finally:
        runtime.stop()
    first = store.conversation("owner", chat["id"])["messages"]
    assert [m["role"] for m in first] == ["user", "assistant"]
    assert first[-1]["content"] == output
    exchange = peers.for_task(task["id"])
    assert exchange["status"] == "interrupted" and not exchange["result"]
    assert exchange["calls"] == 1 and exchange["reported_at"] is not None
    assert [u["status"] for u in store.task("owner", task["id"])["usage"]] == [
        "unknown",
        "completed",
    ]

    # Recovery runs again on the same database, with no new user request.
    runtime = BotRuntime(Store(store.path), "owner", lambda _: RecoveryFixture())
    runtime.start()
    try:
        assert store.claim("owner") is None
        assert peers.claim() is None
    finally:
        runtime.stop()
    assert store.conversation("owner", chat["id"])["messages"] == first
    assert peers.for_task(task["id"])["reported_at"] == exchange["reported_at"]
    assert len(calls) == 1
    usage = store.usage("owner")
    assert usage["calls"] == 2 and usage["unconfirmed_calls"] == 1
    return {
        "kind": "Synthetic provider; real process kill and durable peer recovery",
        "platform": sys.platform,
        "live_model_calls": 0,
        "fixture_provider_calls": usage["calls"],
        "unknown_fixture_calls": usage["unconfirmed_calls"],
        "peer_status": exchange["status"],
        "peer_calls": exchange["calls"],
        "source_reports": 1,
        "second_restart_replayed": False,
        "child_reaped": child.returncode == -signal.SIGKILL,
    }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="alysis-peer-20261019-crash-") as temporary:
        print(json.dumps(verify(Path(temporary)), indent=2))
