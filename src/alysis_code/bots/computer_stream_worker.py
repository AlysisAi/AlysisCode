"""Outbound viewer relay beside the existing jailed desktop/browser worker.

No TCP listener, account key or Azure credential. Bootstrap secrets arrive as
encrypted ciphertext and are kept only in memory. The relay can forward only
read-only screen requests and the existing sequence-encrypted human input.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import time
from uuid import UUID

from .cloud_desktop_worker import exchange, receive, root_for
from .private_input import PrivateInput


def address(channel):
    if UUID(channel).hex != channel:
        raise ValueError("Invalid stream identity.")
    return "/tmp/alysis-view-" + channel + ".sock"


def local(channel, message):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(address(channel))
        connection.sendall(json.dumps(message).encode() + b"\n")
        return receive(connection)


async def relay(session, config):
    from urllib.parse import urlsplit

    from websockets.asyncio.client import connect

    url = urlsplit(config["url"])
    if (
        url.scheme != "wss"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or not url.path.startswith("/internal/computer-stream/")
    ):
        raise ValueError("Invalid controller stream endpoint.")
    context = ssl.create_default_context()
    if config.get("certificate"):
        context.load_verify_locations(cadata=config["certificate"])
    async with connect(
        config["url"],
        ssl=context,
        proxy=None,
        additional_headers={"Authorization": "Bearer " + config["token"]},
        open_timeout=8,
        close_timeout=2,
        ping_interval=10,
        ping_timeout=10,
        max_size=48000,
        max_queue=2,
        compression=None,
    ) as websocket:
        config.clear()
        pending = {}

        async def forward(request, operation):
            try:
                if operation == "screen":
                    message = {"id": request["id"], "session": session}
                    reply = await asyncio.to_thread(
                        exchange, session, message, screen=True, timeout=3
                    )
                    if isinstance(reply, dict) and set(reply) == {"error"}:
                        reply = {
                            **message,
                            "error": "Screen is not ready.",
                        }
                else:
                    reply = await asyncio.to_thread(exchange, session, request, timeout=10)
                if reply.get("id") != request["id"] or reply.get("session") != session:
                    raise ValueError
                wire = json.dumps(reply, separators=(",", ":"))
                if len(wire.encode()) > 4 * 1024 * 1024:
                    raise ValueError
                await websocket.send(wire)
            except Exception:
                await websocket.close(code=1011)
            finally:
                pending.pop(operation, None)

        try:
            async for wire in websocket:
                request = json.loads(wire)
                if (
                    not isinstance(request, dict)
                    or set(request) != {"id", "session", "operation", "arguments"}
                    or request["session"] != session
                    or UUID(request["id"]).hex != request["id"]
                    or request["operation"] not in {"screen", "human_input"}
                    or not isinstance(request["arguments"], dict)
                    or (request["operation"] == "screen" and request["arguments"])
                    or request["operation"] in pending
                ):
                    raise ValueError
                operation = request["operation"]
                pending[operation] = asyncio.create_task(forward(request, operation))
        finally:
            # I/O already admitted into a Unix socket must settle, never replay.
            await asyncio.gather(*list(pending.values()), return_exceptions=True)


def serve(session, channel):
    private = PrivateInput(session)
    path = address(channel)
    config = None
    try:
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(path)
            os.chmod(path, 0o600)
            listener.listen(1)
            listener.settimeout(20)
            while config is None:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(5)
                    request = receive(connection)
                    if request == {"action": "key"}:
                        reply = {"public_key": private.public_key}
                    elif set(request) == {"id", "envelope"}:
                        data = private.open(request["id"], request["envelope"])
                        config = data["event"]
                        if set(config) != {"url", "token", "certificate"}:
                            raise ValueError
                        reply = {"configured": True}
                    else:
                        raise ValueError
                    connection.sendall(json.dumps(reply).encode() + b"\n")
        asyncio.run(relay(session, config))
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def jpeg_screen(result):
    """Reduce transport bytes without changing guest pixel coordinates."""
    from PIL import Image

    pixels = base64.b64decode(result["png"], validate=True)
    if len(pixels) > 2_000_000:
        raise ValueError("Screen exceeds allowance.")
    with Image.open(io.BytesIO(pixels)) as image:
        if image.format not in {"PNG", "JPEG"} or not (
            1 <= image.width <= 2560 and 1 <= image.height <= 1600
        ):
            raise ValueError("Invalid computer image.")
        image.load()
        output = io.BytesIO()
        clean = image.convert("RGB")
        clean.info.clear()
        clean.save(output, format="JPEG", quality=85)
    if output.tell() > 2_000_000:
        raise ValueError("Screen exceeds allowance.")
    return {"mime": "image/jpeg", "image": base64.b64encode(output.getvalue()).decode()}


def stdio(session):
    # Azure currently keeps interactive stdin open only with a PTY. Disable
    # echo, canonical line limits and newline translation before ready, so the
    # terminal carries bounded JSON bytes rather than shell gestures.
    if sys.stdin.isatty():
        import tty

        tty.setraw(sys.stdin.fileno())
    print(
        json.dumps(
            {
                "ready": True,
                "session": session,
                "uid": os.geteuid(),
                "protocol": "computer-stream-v2",
            }
        ),
        flush=True,
    )
    while wire := sys.stdin.buffer.readline(48002):
        if len(wire) > 48001 or not wire.endswith(b"\n"):
            raise ValueError("Invalid computer message.")
        request = json.loads(wire)
        if (
            not isinstance(request, dict)
            or set(request) != {"id", "session", "operation", "arguments"}
            or request["session"] != session
            or UUID(request["id"]).hex != request["id"]
            or request["operation"] not in {"screen", "human_input"}
            or not isinstance(request["arguments"], dict)
            or (request["operation"] == "screen" and request["arguments"])
        ):
            raise ValueError("Invalid computer message.")
        screen = request["operation"] == "screen"
        message = {"id": request["id"], "session": session} if screen else request
        result = exchange(session, message, screen=screen, timeout=3 if screen else 10)
        if isinstance(result, dict) and set(result) == {"error"}:
            result = {**message, "error": "Screen is not ready."}
        if result.get("id") != request["id"] or result.get("session") != session:
            raise ValueError("Invalid computer receipt.")
        if screen and "result" in result:
            result = {**result, "result": jpeg_screen(result["result"])}
        output = json.dumps(result, separators=(",", ":"))
        if len(output.encode()) > 4 * 1024 * 1024:
            raise ValueError("Computer receipt exceeds its allowance.")
        print(output, flush=True)


def main():
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(1000)
        os.setuid(1000)
    if os.geteuid() != 1000:
        raise ValueError("Worker image requires UID 1000.")
    mode, session, channel = sys.argv[1:4]
    root_for(session)
    address(channel)
    if mode == "stdio":
        stdio(session)
    elif mode == "serve":
        serve(session, channel)
    elif mode == "start":
        if os.path.exists(address(channel)):
            raise ValueError("Stream already started; do not replay startup.")
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "alysis_code.bots.computer_stream_worker",
                "serve",
                session,
                channel,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "/opt/alysis/src", "LANG": "C.UTF-8"},
            start_new_session=True,
            close_fds=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and process.poll() is None:
            if os.path.exists(address(channel)):
                print(json.dumps(local(channel, {"action": "key"})))
                return
            time.sleep(0.05)
        raise ValueError("Stream helper did not start.")
    elif mode == "connect":
        payload = base64.b64decode(sys.argv[4], validate=True)
        if len(payload) > 48000:
            raise ValueError("Stream configuration exceeds its allowance.")
        print(json.dumps(local(channel, json.loads(payload))))
    else:
        raise ValueError("Unknown stream operation.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Computer stream unavailable.") from None
