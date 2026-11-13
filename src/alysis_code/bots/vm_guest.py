"""Trusted host-only virtio-vsock bridge inside an Alysis local computer image.

No TCP listener, model credentials or host mounts. Only the hypervisor host CID
may connect. Commands and files execute as the locked, unprivileged desktop user.
The agent's Bubblewrap namespace cannot read this service's root-owned state.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PORT = 10789
MAX_WIRE = 72 * 1024 * 1024
MAX_FILE = 50 * 1024 * 1024
MAX_OUTPUT = 6 * 1024 * 1024
READY = Path("/var/lib/alysis/image-ready")


def receive(connection):
    length = int.from_bytes(read_exact(connection, 4), "big")
    if not 0 < length <= MAX_WIRE:
        raise ValueError("Invalid frame size.")
    return json.loads(read_exact(connection, length))


def read_exact(connection, size):
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(min(65536, size - len(data)))
        if not chunk:
            raise ValueError("Incomplete frame.")
        data.extend(chunk)
    return data


def send(connection, value):
    data = json.dumps(value).encode()
    if len(data) > MAX_WIRE:
        raise ValueError("Reply exceeds frame size.")
    connection.sendall(len(data).to_bytes(4, "big") + data)


def file_path(value):
    if (
        not isinstance(value, str)
        or len(value) > 4096
        or not value.startswith("/tmp/alysis-cloud-")
    ):
        raise ValueError("Invalid transfer destination.")
    path = Path(value)
    if str(path) != value or ".." in path.parts or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid transfer destination.")
    return path


def dispatch(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid operation.")
    operation = value.get("operation")
    if operation == "ping":
        return {"protocol": 1, "ready": READY.is_file()}
    if operation == "shutdown":
        subprocess.Popen(
            ["/usr/bin/systemctl", "poweroff"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"stopping": True}
    if operation == "run":
        argv, timeout = value["argv"], value["timeout"]
        if (
            not isinstance(argv, list)
            or not 1 <= len(argv) <= 64
            or any(
                not isinstance(item, str) or "\0" in item or len(item) > MAX_OUTPUT for item in argv
            )
        ):
            raise ValueError("Invalid command.")
        if type(timeout) is not int or not 1 <= timeout <= 60:
            raise ValueError("Invalid command timeout.")
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                timeout=timeout,
                user=1000,
                group=1000,
                extra_groups=[],
                cwd="/tmp",
                env={
                    "PATH": "/opt/alysis/.venv/bin:/usr/bin:/bin",
                    "HOME": "/home/alysis",
                    "LANG": "C.UTF-8",
                },
            )
            out.seek(0)
            err.seek(0)
            stdout, stderr = out.read(MAX_OUTPUT + 1), err.read(4096)
            if len(stdout) > MAX_OUTPUT:
                raise ValueError("Command output exceeds its allowance.")
            return {
                "exit_code": result.returncode,
                "stdout": stdout.decode("utf-8", "replace"),
                "stderr": stderr.decode("utf-8", "replace"),
            }
    if operation in ("upload", "download"):
        path = file_path(value["path"])
        size = value["size"]
        if type(size) is not int or not 0 <= size <= MAX_FILE:
            raise ValueError("Invalid file size.")
        # Drop filesystem access to the desktop identity for the entire open.
        # The daemon uses a separate process rather than thread-wide setuid.
        script = (
            "import os,sys\n"
            "n=int(sys.argv[2]); d=sys.stdin.buffer.read(n+1)\n"
            "if len(d)!=n: raise ValueError('Incomplete transfer')\n"
            "fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)\n"
            "with os.fdopen(fd,'wb') as stream:\n"
            " stream.write(d); stream.flush(); os.fsync(stream.fileno())\n"
            if operation == "upload"
            else "import os,sys,stat\n"
            "fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)\n"
            "with os.fdopen(fd,'rb') as stream:\n"
            " before=os.fstat(stream.fileno())\n"
            " if not stat.S_ISREG(before.st_mode) or before.st_size!=int(sys.argv[2]): raise ValueError('Changed file')\n"
            " data=stream.read(before.st_size+1); after=os.fstat(stream.fileno())\n"
            " if before.st_size!=after.st_size or before.st_mtime_ns!=after.st_mtime_ns: raise ValueError('Changed file')\n"
            " sys.stdout.buffer.write(data)\n"
        )
        body = base64.b64decode(value["data"], validate=True) if operation == "upload" else None
        if body is not None and len(body) != size:
            raise ValueError("File does not match its declared size.")
        result = subprocess.run(
            ["/usr/bin/python3", "-I", "-c", script, str(path), str(size)],
            input=body,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
            user=1000,
            group=1000,
            extra_groups=[],
            env={"PATH": "/usr/bin:/bin"},
        )
        if result.returncode:
            raise ValueError("File transfer failed; do not replay it.")
        if operation == "download" and len(result.stdout) != size:
            raise ValueError("File changed during transfer.")
        return (
            {"data": base64.b64encode(result.stdout).decode()}
            if operation == "download"
            else {"uploaded": size}
        )
    raise ValueError("Unknown operation.")


def handle(connection, peer):
    with connection:
        connection.settimeout(75)
        try:
            if peer[0] != socket.VMADDR_CID_HOST:
                return
            send(connection, {"ok": True, "result": dispatch(receive(connection))})
        except Exception:
            # Do not surface command arguments or untrusted exception strings.
            try:
                send(
                    connection,
                    {
                        "ok": False,
                        "error": "Local computer operation failed; reconcile before retrying.",
                    },
                )
            except OSError:
                pass


def main():
    if os.getuid() != 0:
        raise SystemExit("The guest bridge requires its system service identity.")
    with (
        socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM) as listener,
        ThreadPoolExecutor(max_workers=8) as pool,
    ):
        listener.bind((socket.VMADDR_CID_ANY, PORT))
        listener.listen(16)
        while True:
            connection, peer = listener.accept()
            pool.submit(handle, connection, peer)


if __name__ == "__main__":
    main()
