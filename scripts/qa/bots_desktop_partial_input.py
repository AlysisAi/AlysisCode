"""Actual X11 partial-input cancellation/disconnect with a synthetic model client.

A trusted fault wrapper pauses after real button-down dispatch. An independent
native application records actual X11 press/release events. No model is called;
runtime task/tool outcomes and cleanup use production code in a disposable lab.
"""

import base64
import json
import os
import signal
import tempfile
import threading
import time
from pathlib import Path

from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.desktop_linux import OfflineLinuxDesktop
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store
from alysis_code.llm.types import LLMResponse, ToolCall

WRAPPER = """import runpy, time
from pathlib import Path
module=runpy.run_path('/opt/desktop_worker.py')
original=module['Computer'].xdo
def xdo(self,*args,**kwargs):
    result=original(self,*args,**kwargs)
    if args and args[0]=='mousedown':
        deadline=time.monotonic()+4
        while not Path('/workspace/continue-input').exists():
            if time.monotonic()>deadline: raise RuntimeError('Study timeout')
            time.sleep(.01)
    return result
module['Computer'].xdo=xdo
module['main']()
"""


def native_events(path):
    if not path.exists():
        return []
    return [
        "down" if line.startswith("ButtonPress event") else "up"
        for line in path.read_text().splitlines()
        if line.startswith(("ButtonPress event", "ButtonRelease event"))
    ]


def until(check, timeout=5):
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("Expected real native input did not arrive")
        time.sleep(0.01)


def study(mode, evidence_dir):
    with tempfile.TemporaryDirectory(prefix="bots-linux-night-20261019-partial-input-") as temp:
        root = Path(temp)
        wrapper = root / "wrapper.py"
        wrapper.write_text(WRAPPER)
        store = Store(root / "bots.db")
        bot = store.save_bot("owner", BotInput(name="Study", purpose="Check real partial input"))
        chat = store.create_chat("owner", bot["id"], "Input study")
        store.enqueue("owner", chat["id"], TaskInput(message="Prepare", request_id="input-study"))
        preparation = store.claim("owner")
        proposals = ProposalStore(store, "owner")
        proposal = proposals.propose(
            preparation,
            ActionProposal(
                question="Use computer?",
                instructions="Click the synthetic native window.",
                offline_computer=True,
            ),
        )
        store.finish("owner", preparation["id"], "completed", content="Review")
        proposals.decide(
            bot["id"],
            proposal["id"],
            ActionDecision(
                action="accept",
                expected_revision=1,
                offline_computer=True,
            ),
        )
        task = store.claim("owner")
        instances, saved_events, traces, failures = [], [], [], []
        input_started = threading.Event()

        class InstrumentedDesktop(OfflineLinuxDesktop):
            def build_argv(self, root):
                argv = super().build_argv(root)
                index = argv.index("--")
                argv[index:index] = ["--ro-bind", str(wrapper), "/opt/qa_wrapper.py"]
                argv[-1] = "/opt/qa_wrapper.py"
                return argv

            def close(self):
                super().close()
                path = self.workspace / "input-events"
                if path.exists():
                    saved_events[:] = native_events(path)

        def factory(owner, workspace, **kwargs):
            desktop = InstrumentedDesktop(owner, workspace, **kwargs)
            instances.append(desktop)
            try:
                desktop.call(
                    owner,
                    "launch",
                    {
                        "command": "stdbuf -oL xev -geometry 400x200+200+200 -event button -1 > /workspace/input-events 2>&1"
                    },
                )
                focused = desktop.call(
                    owner,
                    "run",
                    {
                        "command": "xdotool search --sync --name 'Event Tester' windowmap --sync windowactivate --sync",
                        "timeout": 3,
                    },
                )
                assert focused["exit_code"] == 0, focused
                return desktop
            except BaseException as exc:
                print(
                    json.dumps(
                        {
                            "setup_error": str(exc),
                        }
                    ),
                    flush=True,
                )
                desktop.close()
                raise

        views = ComputerViews()
        manager = DesktopComputers("owner", views, factory=factory, store=store)

        class Client:
            model = "partial-input-synthetic-client"
            supports_tool_calling = True
            _alysis_browser_image_input = True
            calls = 0

            def chat(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return LLMResponse("", [ToolCall("observe", "computer_observe", {})], {})
                result = next(
                    json.loads(m["content"])
                    for m in reversed(kwargs["messages"])
                    if m["role"] == "tool"
                )
                traces.append(result)
                if self.calls == 2:
                    image = next(
                        m
                        for m in reversed(kwargs["messages"])
                        if m["role"] == "user" and isinstance(m["content"], list)
                    )
                    data = next(
                        p["image_url"]["url"] for p in image["content"] if p["type"] == "image_url"
                    )
                    (evidence_dir / f"{mode}-initial.png").write_bytes(
                        base64.b64decode(data.split(",", 1)[1])
                    )
                    input_started.set()
                    # Controlled window coordinates; this is not model vision.
                    return LLMResponse(
                        "",
                        [
                            ToolCall(
                                "click",
                                "computer_click",
                                {
                                    "source_id": result["source_id"],
                                    "x": 350,
                                    "y": 300,
                                },
                            )
                        ],
                        {},
                    )
                assert self.calls == 3 and mode != "cancel"
                if mode == "disconnect":
                    assert "uncertain" in result["error"] or "may have been sent" in result["error"]
                else:
                    assert result["dispatched"] and result["verification_required"]
                return LLMResponse("Synthetic report of the actual tool result.", [], {})

        client = Client()
        runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
        timings = {}

        def inject():
            try:
                assert input_started.wait(5)
                workspace = instances[0].workspace
                receipt = workspace / "input-events"
                until(lambda: "down" in native_events(receipt))
                timings["native_down_at"] = time.monotonic()
                if mode == "cancel":
                    runtime.cancel(task["id"])
                elif mode == "disconnect":
                    os.kill(instances[0]._process.pid, signal.SIGKILL)
                else:
                    (workspace / "continue-input").touch()
            except BaseException as exc:
                failures.append(str(exc))

        injector = threading.Thread(target=inject)
        injector.start()
        try:
            runtime.run(task, runtime.task_token(task["id"]))
            if "native_down_at" not in timings:
                print(
                    json.dumps(
                        {
                            "mode": mode,
                            "task": store.task("owner", task["id"]),
                            "traces": traces,
                            "failures": failures,
                            "calls": client.calls,
                        }
                    ),
                    flush=True,
                )
                raise AssertionError("Native press was not observed")
            elapsed = time.monotonic() - timings["native_down_at"]
            injector.join(3)
            assert not injector.is_alive() and not failures, failures
            assert not instances[0].alive() and not instances[0].workspace.exists()
            assert not manager._computers and views.frame("owner", bot["id"]) == {
                "state": "not_running"
            }
            final = store.task("owner", task["id"])
            if mode == "normal":
                assert saved_events == ["down", "up"], saved_events
                assert final["status"] == "completed"
            else:
                assert saved_events == ["down"], saved_events
                assert elapsed < 2, elapsed
                assert not any(
                    e["kind"] == "tool_completed" and e["data"].get("name") == "computer_click"
                    for e in final["events"]
                )
                if mode == "cancel":
                    assert final["status"] == "cancelled" and client.calls == 2
                    assert not final["output"]
                else:
                    assert any(
                        e["kind"] == "tool_failed" and e["data"].get("name") == "computer_click"
                        for e in final["events"]
                    )
            return {
                "mode": mode,
                "native_events": saved_events,
                "task_status": final["status"],
                "seconds_after_button_down": elapsed,
                "tool_results": traces,
                "computer_and_workspace_closed": True,
                "live_model_calls": 0,
                "synthetic_client_calls": client.calls,
            }
        finally:
            manager.close()
            injector.join(5)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    evidence = parser.parse_args().evidence
    evidence.mkdir(parents=True, exist_ok=True)
    print(
        json.dumps(
            {
                "passed": True,
                "cases": [study(mode, evidence) for mode in ("normal", "cancel", "disconnect")],
            }
        )
    )
