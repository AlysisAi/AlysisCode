"""Opt-in real Linux screen concurrency and cancellation qualification.

Uses the portable cloud RPC over the offline local Docker provider. Commands are
scripted test inputs, not model decisions or hosted Azure evidence. No credentials,
network, published ports or mounted host workspace in the disposable computer.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from alysis_code.bots.cloud_desktop import CloudDesktopFactory, worker_argv
from alysis_code.bots.compute import ComputerSpec
from alysis_code.bots.compute_docker import DockerComputers
from alysis_code.bots.computer import ComputerViews
from alysis_code.cancellation import CooperativeCancellationError


def exercise(image):
    repository = Path(__file__).resolve().parents[2]
    evidence = Path(tempfile.mkdtemp(prefix="bots-desktop-screen-")).resolve()
    provider = DockerComputers(
        "desktop-linux", namespace_profile=repository / "scripts/cloud-dev/seccomp.json"
    )
    factory = CloudDesktopFactory(
        "screen-study",
        provider,
        ComputerSpec(image, cpu=1, memory_mib=1024),
        evidence / "ledger",
        max_computers=1,
    )
    bot = uuid4().hex
    views = ComputerViews()
    receipt = {"kind": "Real local Linux, scripted commands, portable cloud RPC", "passed": False}
    computer = None
    try:
        computer = factory(bot, evidence, cancel=lambda: False)
        receipt["resource_id"] = computer.ref.resource_id
        print(json.dumps({"evidence": str(evidence), **receipt}), flush=True)
        # Starts the actual isolated desktop and checks the nested agent jail.
        isolation = computer.call(
            bot,
            "run",
            {
                "command": "test ! -S /run/alysis-screen/screen.sock && "
                'test -z "$(ls -A /run/alysis-screen)" && '
                "printf 'screen socket hidden from task applications\\n'",
                "timeout": 5,
            },
        )
        receipt["isolation"] = isolation
        assert isolation["exit_code"] == 0, isolation
        generation = views.bind(
            "screen-study", bot, capture=computer.capture, alive=computer.alive, surface="desktop"
        )
        assert views.frame("other-owner", bot)["state"] == "not_running"
        assert views.frame("screen-study", "other-bot")["state"] == "not_running"
        session = computer.identity.allocation_id
        invalid = {"id": uuid4().hex, "session": session, "operation": "run"}
        rejected = provider.run(
            computer.ref,
            worker_argv("screen", session, base64.b64encode(json.dumps(invalid).encode()).decode()),
            timeout=5,
        )
        receipt["read_only_rejection"] = json.loads(rejected.stdout)
        assert "error" in receipt["read_only_rejection"]

        command = (
            'xmessage -title "Alysis capture check" -geometry 620x220+200+180 '
            '-timeout 18 "Real desktop: viewer remains live during a command." &\n'
            "view_pid=$!\n"
            "for remaining in $(seq 16 -1 1); do\n"
            '  xdotool search --onlyvisible --name "^Alysis capture check" '
            'set_window --name "Alysis capture check: $remaining"\n'
            "  sleep 1\n"
            "done\n"
            'wait "$view_pid"\nprintf "display command completed\\n"\n'
        )
        receipt["frames"] = []
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(computer.call, bot, "run", {"command": command, "timeout": 25})
            for index in range(5):
                time.sleep(1.5)
                assert not pending.done(), pending.result() if pending.done() else ""
                started = time.monotonic()
                frame = views.frame("screen-study", bot)
                elapsed = time.monotonic() - started
                assert frame["state"] == "live", frame
                assert not pending.done(), "Capture waited for the command to finish."
                data = base64.b64decode(frame["image"].split(",", 1)[1], validate=True)
                (evidence / f"frame-{index}.jpg").write_bytes(data)
                receipt["frames"].append(
                    {
                        "state": frame["state"],
                        "width": frame["width"],
                        "height": frame["height"],
                        "seconds": elapsed,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "command_pending_after_capture": True,
                    }
                )
            receipt["command"] = pending.result(timeout=30)
            assert receipt["command"]["exit_code"] == 0
            assert len({f["sha256"] for f in receipt["frames"]}) >= 3

            pending = pool.submit(computer.call, bot, "run", {"command": "sleep 25", "timeout": 30})
            time.sleep(1)
            assert views.frame("screen-study", bot)["state"] == "live"
            assert not pending.done()
            started = time.monotonic()
            views.revoke("screen-study", bot, generation)
            computer.close()
            receipt["cancel_seconds"] = time.monotonic() - started
            try:
                pending.result(timeout=10)
            except CooperativeCancellationError:
                receipt["cancelled_command"] = True
            else:
                raise AssertionError("Cancelled command returned a success receipt.")
        assert views.frame("screen-study", bot)["state"] == "not_running"
        receipt["passed"] = True
    finally:
        if computer is not None:
            computer.close()
            receipt["cleanup_state"] = provider.status(computer.ref).state
            assert receipt["cleanup_state"] == "missing"
        (evidence / "receipt.json").write_text(json.dumps(receipt, indent=2))
        print(json.dumps({"evidence": str(evidence), **receipt}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-local", action="store_true")
    parser.add_argument("--image", required=True, help="Explicit prepared local image; no pulls")
    arguments = parser.parse_args()
    if not arguments.execute_local:
        parser.error("Requires explicit --execute-local.")
    exercise(arguments.image)
