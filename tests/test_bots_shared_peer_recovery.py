"""Real PostgreSQL/process-death peer recovery with explicit fixture models.

No external model, computer or cloud calls. Lease expiry uses the normal
60-second database clock, never timestamp rewriting or shortened leases.
"""

import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from test_bots_backup import databases  # noqa: F401

from alysis_code.bots.cooperation import PeerStore
from alysis_code.bots.models import BotInput, FileInput, MemoryInput, TaskInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.postgres import PostgresStore
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import NotFound
from alysis_code.bots.worker_lease import WorkerLease, WorkerLeaseLost
from alysis_code.llm.types import LLMResponse, ToolCall


def until(read, accept=bool, *, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = read()
        if accept(value):
            return value
        time.sleep(0.1)
    raise AssertionError("Shared peer study did not reach its expected state.")


def child(config):
    store = PostgresStore(
        config["dsn"],
        objects=LocalObjectStorage(Path(config["objects"])),
        state_directory=Path(config["scratch"]),
    )
    if config["mode"] == "contender":
        lease = WorkerLease(store, config["owner"])
        try:
            lease.acquire()
        except WorkerLeaseLost:
            print(json.dumps({"blocked": True}), flush=True)
        else:
            lease.close()
            raise AssertionError("A second coordinator acquired the live account.")
        finally:
            store.close()
        return

    def trace(phase, **fields):
        # Synthetic phase evidence only, never connection strings or prompts.
        with Path(config["trace"]).open("a") as output:
            output.write(json.dumps({"phase": phase, **fields}) + "\n")

    class PeerFixture:
        model = "explicit-shared-peer-recovery-fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            messages = kwargs["messages"]
            if messages[0]["content"].startswith("You are the receiving peer"):
                assert config["mode"] in {"normal", "block"}, "Uncertain peer was replayed"
                assert kwargs["tools"] is None
                assert [m["role"] for m in messages] == ["system", "user"]
                memories = json.loads(
                    messages[0]["content"].split(
                        "Your approved memories (use only relevant facts in the reply): ", 1
                    )[1]
                )
                assert memories == config["expected_memories"]
                scoped = json.loads(messages[1]["content"].split("\n", 1)[1])
                assert set(scoped) == {
                    "exchange_id",
                    "from_bot",
                    "brief",
                    "shared_files",
                    "current_user_corrections",
                }
                assert scoped["from_bot"] == config["source_name"]
                assert scoped["brief"] == config["brief"]
                assert scoped["shared_files"] == [config["shared_snapshot"]]
                assert scoped["current_user_corrections"] == []
                encoded = json.dumps(messages, ensure_ascii=False)
                assert all(value not in encoded for value in config["excluded"])
                trace("peer_scope_checked", shared_hash=config["shared_snapshot"]["sha256"])
                if config["mode"] == "block":
                    token = kwargs["cancellation_token"]
                    while not token.wait(0.1):
                        pass
                    token.throw_if_cancelled()
                return LLMResponse(config["peer_result"], [], {})
            if kwargs["tools"]:
                assert config["mode"] in {"normal", "block"}, "Original request was replayed"
                assert "request_peer" in {t["function"]["name"] for t in kwargs["tools"]}
                trace("request_peer")
                return LLMResponse(
                    "",
                    [
                        ToolCall(
                            "peer-call",
                            "request_peer",
                            {
                                "recipient_bot_id": config["recipient"],
                                "brief": config["brief"],
                                "shared_file_ids": [config["shared_snapshot"]["id"]],
                            },
                        )
                    ],
                    {},
                )
            reports = [m for m in messages if m["role"] == "tool"]
            assert len(reports) == 1 and reports[0]["tool_call_id"] == "peer-call"
            result = json.loads(reports[0]["content"])
            expected = "interrupted" if config["mode"] == "recovery" else "completed"
            assert result["status"] == expected
            assert result["source_task_id"] == config["task"]
            assert result["calls"] == 1 and result["reported_at"] is None
            assert result["shared_files"] == [
                {key: config["shared_snapshot"][key] for key in ("id", "name", "sha256")}
            ]
            if expected == "interrupted":
                assert not result["result"]
                assert "uncertain response was not replayed" in result["error"]
                text = config["interrupted_report"]
            else:
                assert result["result"] == config["peer_result"] and result["error"] is None
                text = config["completed_report"]
            trace("report", status=expected)
            return LLMResponse(text, [], {})

    stopping = False

    def request_stop(*_):
        nonlocal stopping
        # A Python signal handler may interrupt Event.wait while its lock is
        # held. Only assign a flag here; shutdown runs outside the handler.
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    runtime = BotRuntime(store, config["owner"], lambda _: PeerFixture())
    try:
        runtime.start()
        print(
            json.dumps(
                {
                    "ready": True,
                    "epoch": runtime._shared_lease.epoch,
                    "ttl": runtime._shared_lease.ttl,
                }
            ),
            flush=True,
        )
        while not stopping:
            time.sleep(0.1)
            if not runtime.is_running:
                raise AssertionError("Study coordinator stopped unexpectedly.")
    finally:
        runtime.stop()
        store.close()


def test_peer_recovers_real_controller_death_without_replay_or_scope_expansion(
    databases,  # noqa: F811
    tmp_path,
):
    objects = tmp_path / "objects"
    store = databases(objects=LocalObjectStorage(objects))
    processes, groups = [], []
    receipt = {
        "passed": False,
        "kind": "Actual PostgreSQL and controller processes; explicit fixture models",
        "external_model_calls": 0,
        "computer_provider_calls": 0,
        "azure_calls": 0,
    }

    def start(group, mode):
        trace_path = tmp_path / (uuid4().hex + ".jsonl")
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        processes.append(process)
        config = {
            **group,
            "dsn": store._dsn,
            "objects": str(objects),
            "scratch": str(tmp_path / uuid4().hex),
            "trace": str(trace_path),
            "mode": mode,
        }
        process.stdin.write(json.dumps(config) + "\n")
        process.stdin.close()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(25), "Study worker did not return startup status."
            line = process.stdout.readline()
        assert line, "Study worker exited before startup status."
        status = json.loads(line)
        if mode == "contender":
            assert status == {"blocked": True} and process.wait(timeout=10) == 0
        else:
            assert status["ready"] and status["ttl"] == 60
        return process, status, trace_path

    def events(path):
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def terminate(process):
        if process.poll() is None:
            process.terminate()
            try:
                assert process.wait(timeout=25) == 0
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                raise AssertionError("Study worker did not shut down cleanly.") from None

    def remaining_lease(group):
        with store.connect() as db:
            row = db.execute(
                "SELECT expires FROM worker_leases WHERE owner=?", (group["owner"],)
            ).fetchone()
            return row[0] - WorkerLease.now(db)

    def counts():
        with store.connect() as db:
            return {
                table: db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                for table in (
                    "tasks",
                    "messages",
                    "files",
                    "memories",
                    "peer_exchanges",
                    "peer_calls",
                    "provider_calls",
                )
            }

    try:
        for index in range(2):
            owner = "peer-recovery-" + uuid4().hex
            source = store.save_bot(owner, BotInput(name=f"Planner {index}", purpose="Scope study"))
            recipient = store.save_bot(
                owner, BotInput(name=f"Νεφέλη {index}", purpose="Review only")
            )
            chat = store.create_chat(owner, source["id"], "Original continuous chat")
            recipient_chat = store.create_chat(owner, recipient["id"], "Private peer chat")
            shared = store.save_file(
                owner,
                source["id"],
                FileInput(
                    name="入力 Δ.txt",
                    content="  Αθήνα / 東京\n\n" + uuid4().hex + "\n",
                ),
            )
            excluded = ["private-" + uuid4().hex for _ in range(4)]
            store.save_file(owner, source["id"], FileInput(name="private.txt", content=excluded[0]))
            store.save_file(
                owner, recipient["id"], FileInput(name="private.txt", content=excluded[1])
            )
            store.save_memory(
                owner, source["id"], MemoryInput(title="Private", content=excluded[2])
            )
            rejected = store.save_memory(
                owner, recipient["id"], MemoryInput(title="Rejected", content=excluded[3])
            )
            store.decide_memory(owner, recipient["id"], rejected["id"], "rejected")
            memories = [
                store.save_memory(
                    owner,
                    recipient["id"],
                    MemoryInput(
                        title=f"Constraint {n}",
                        content="Approved Ελληνικά 日本語 " + uuid4().hex,
                    ),
                )
                for n in range(2)
            ]
            request = "Review only the selected input. Original private context: " + uuid4().hex
            task = store.enqueue(
                owner, chat["id"], TaskInput(message=request, request_id=uuid4().hex)
            )
            groups.append(
                {
                    "owner": owner,
                    "source": source["id"],
                    "source_name": source["name"],
                    "recipient": recipient["id"],
                    "chat": chat["id"],
                    "recipient_chat": recipient_chat["id"],
                    "task": task["id"],
                    "brief": "Review the selected 東京 file; text advice only.",
                    "shared_snapshot": {
                        **{key: shared[key] for key in ("id", "name", "content")},
                        "sha256": hashlib.sha256(shared["content"].encode()).hexdigest(),
                    },
                    "expected_memories": [
                        {key: memory[key] for key in ("id", "title", "content", "updated_at")}
                        for memory in sorted(memories, key=lambda m: m["id"])
                    ],
                    "excluded": [*excluded, request],
                    "peer_result": "Fixture review preserves 東京 and both saved constraints.",
                    "completed_report": f"Νεφέλη {index} returned the scoped fixture review; no actions performed.",
                    "interrupted_report": f"Νεφέλη {index}'s review was interrupted. No response was confirmed or replayed.",
                }
            )
        private_values = [list(group["excluded"]) for group in groups]
        for index, group in enumerate(groups):
            other = groups[1 - index]
            group["excluded"].extend(
                private_values[1 - index] + [m["content"] for m in other["expected_memories"]]
            )

        victim, first, victim_trace = start(groups[0], "block")
        until(lambda: any(e["phase"] == "peer_scope_checked" for e in events(victim_trace)))
        uncertain = PeerStore(store, groups[0]["owner"]).for_task(groups[0]["task"])
        assert uncertain["status"] == "running" and uncertain["calls"] == 1
        assert store.task(groups[0]["owner"], groups[0]["task"])["status"] == "waiting"
        start(groups[0], "contender")
        victim.kill()
        assert victim.wait(timeout=10) == -signal.SIGKILL
        killed_at = time.monotonic()
        remaining_at_kill = remaining_lease(groups[0])
        assert 0 < remaining_at_kill <= 60
        start(groups[0], "contender")

        _, _, unrelated_trace = start(groups[1], "normal")
        until(lambda: store.task(groups[1]["owner"], groups[1]["task"])["status"] == "completed")
        assert remaining_lease(groups[0]) > 0, "Unrelated account did not progress before expiry"
        assert (
            PeerStore(store, groups[0]["owner"]).for_task(groups[0]["task"])["status"] == "running"
        )
        until(lambda: remaining_lease(groups[0]) <= 0, seconds=70)
        _, second, recovery_trace = start(groups[0], "recovery")
        assert second["epoch"] == first["epoch"] + 1
        until(lambda: store.task(groups[0]["owner"], groups[0]["task"])["status"] == "completed")
        recovery_seconds = round(time.monotonic() - killed_at, 3)
        assert recovery_seconds >= remaining_at_kill

        for index, group in enumerate(groups):
            peer = PeerStore(store, group["owner"]).for_task(group["task"])
            assert peer["status"] == ("interrupted" if index == 0 else "completed")
            assert peer["calls"] == 1 and peer["reported_at"] is not None
            assert peer["shared_files"] == [group["shared_snapshot"]]
            assert peer["result"] == ("" if index == 0 else group["peer_result"])
            task = store.task(group["owner"], group["task"])
            assert task["calls"] == 3
            assert sorted(call["status"] for call in task["usage"]) == (
                ["completed", "completed", "unknown"] if index == 0 else ["completed"] * 3
            )
            messages = store.conversation(group["owner"], group["chat"])["messages"]
            assert [m["role"] for m in messages] == ["user", "assistant"]
            assert messages[-1]["task_id"] == group["task"]
            assert (
                messages[-1]["content"]
                == group["interrupted_report" if index == 0 else "completed_report"]
            )
            assert store.conversation(group["owner"], group["recipient_chat"])["messages"] == []
            assert len(store.list_files(group["owner"], group["source"])) == 2
            assert len(store.list_files(group["owner"], group["recipient"])) == 1
            assert len(store.list_memories(group["owner"], group["recipient"])) == 2
            assert (
                store.file_data(group["owner"], group["source"], group["shared_snapshot"]["id"])
                == group["shared_snapshot"]["content"].encode()
            )
            with pytest.raises(NotFound):
                PeerStore(store, groups[1 - index]["owner"]).read(
                    groups[1 - index]["source"], peer["id"]
                )
            with pytest.raises(NotFound):
                store.file_data(
                    groups[1 - index]["owner"], group["source"], group["shared_snapshot"]["id"]
                )
        assert [e["phase"] for e in events(victim_trace)] == ["request_peer", "peer_scope_checked"]
        assert [e["phase"] for e in events(unrelated_trace)] == [
            "request_peer",
            "peer_scope_checked",
            "report",
        ]
        assert events(recovery_trace) == [{"phase": "report", "status": "interrupted"}]

        for process in processes:
            terminate(process)
        before = counts()
        restart_traces = [start(group, "restart")[2] for group in groups]
        time.sleep(3.2)  # Several real worker polling ticks after clean restart.
        for process in processes:
            terminate(process)
        assert before == counts()
        assert all(events(path) == [] for path in restart_traces)
        assert before == {
            "tasks": 2,
            "messages": 4,
            "files": 6,
            "memories": 8,
            "peer_exchanges": 2,
            "peer_calls": 2,
            "provider_calls": 6,
        }
        receipt.update(
            passed=True,
            accounts=2,
            bots=4,
            peer_reviews_completed=1,
            peer_reviews_interrupted=1,
            source_chat_reports=2,
            duplicate_reports=0,
            fixture_calls=6,
            unknown_fixture_calls=1,
            lease_ttl_seconds=60,
            lease_remaining_at_kill_seconds=round(remaining_at_kill, 3),
            controller_recovery_seconds=recovery_seconds,
            original_epoch=first["epoch"],
            recovered_epoch=second["epoch"],
            unrelated_progress_before_expiry=True,
            exact_authorized_peer_contexts=2,
            preserved_shared_snapshots=True,
            unchanged_after_clean_restart=True,
            final_counts=before,
            shared_file_hashes=[g["shared_snapshot"]["sha256"] for g in groups],
        )
    finally:
        cleanup_errors = []
        for process in processes:
            try:
                terminate(process)
            except (AssertionError, subprocess.TimeoutExpired):
                cleanup_errors.append("Study child did not shut down cleanly.")
            finally:
                if process.stdout:
                    process.stdout.close()
        receipt["children_stopped"] = all(p.poll() is not None for p in processes)
        receipt["cleanup_errors"] = cleanup_errors
        output = os.environ.get("ALYSIS_BOTS_PEER_RECEIPT")
        if output:
            path = Path(output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(receipt, indent=2) + "\n")
        assert not cleanup_errors


if __name__ == "__main__":
    assert sys.argv[1:] == ["--worker"]
    child(json.loads(sys.stdin.readline()))
