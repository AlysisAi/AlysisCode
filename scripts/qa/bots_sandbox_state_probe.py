"""Synthetic disk/process-memory probe for a real Linux sandbox suspend trial.

Run `serve` in the guest, save `observe` output outside the guest, suspend/resume
through the hosting provider, then pass that receipt to `verify --baseline`.
Only the provider's lifecycle evidence can establish an actual VM suspension.
This script creates no cloud resources, credentials, public listener or model calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def serve(root: Path, port: int) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    note = root / "synthetic-note.txt"
    try:
        with note.open("x", encoding="utf-8") as stream:
            stream.write("Alysis Ω 日本語 " + secrets.token_hex(32))
    except FileExistsError:
        pass
    # The value is fresh for each process and is never written to a file. A new
    # process with the old disk must fail verification against the prior receipt.
    memory_nonce = secrets.token_bytes(32)
    identity = {
        "pid": os.getpid(),
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "process_start_ticks": Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19],
    }

    class Probe(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/state":
                self.send_error(404)
                return
            payload = json.dumps(
                {
                    "schema": 1,
                    **identity,
                    "memory_sha256": digest(memory_nonce),
                    "disk_sha256": digest(note.read_bytes()),
                },
                sort_keys=True,
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    with HTTPServer(("127.0.0.1", port), Probe) as server:
        server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("serve", "observe", "verify"))
    parser.add_argument("--root", type=Path, default=Path("/tmp/alysis-sandbox-state-probe"))
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    if args.phase == "serve":
        serve(args.root, args.port)
        return
    if args.phase == "verify" and args.baseline is None:
        parser.error("verify requires --baseline from an earlier observe command")
    # Never send this loopback-only probe through an environment proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{args.port}/state", timeout=5) as response:
        state = json.load(response)
    if args.phase == "observe":
        print(json.dumps(state, sort_keys=True))
        return
    baseline = json.loads(args.baseline.read_text())
    expected_keys = {
        "schema",
        "pid",
        "boot_id",
        "process_start_ticks",
        "memory_sha256",
        "disk_sha256",
    }
    if set(baseline) != expected_keys or baseline.get("schema") != 1:
        parser.error("baseline is not a version 1 state receipt")
    mismatches = sorted(key for key in expected_keys if state.get(key) != baseline[key])
    print(json.dumps({"passed": not mismatches, "changed_fields": mismatches, "state": state}))
    if mismatches:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
