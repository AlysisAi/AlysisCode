"""Own a hypervisor process until the controller closes its private stdin pipe."""

from __future__ import annotations

import json
import select
import signal
import subprocess
import sys


def main():
    value = json.loads(sys.stdin.buffer.readline(65537))
    argv = value["argv"]
    if not isinstance(argv, list) or not argv or any(not isinstance(item, str) for item in argv):
        raise SystemExit(1)
    child = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )

    def stop(*_args):
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        print(json.dumps({"pid": child.pid}), flush=True)
        while child.poll() is None:
            if select.select([sys.stdin.buffer], [], [], 0.2)[0]:
                sys.stdin.buffer.read(1)
                break
    finally:
        stop()


if __name__ == "__main__":
    main()
