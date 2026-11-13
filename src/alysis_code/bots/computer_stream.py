"""An allocation-bound outbound guest link, never a public VM listener.

The normal account API still admits every screen/input request. This channel
only replaces the repeated provider shell command between controller and guest.
Capabilities and pending images live in RAM and cannot be resumed or replayed.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import threading
import time
from concurrent.futures import Future
from urllib.parse import urlsplit
from uuid import UUID, uuid4

MAX_MESSAGE = 4 * 1024 * 1024
STREAM_PATH = "/internal/computer-stream"


class StreamUnavailable(RuntimeError):
    pass


def validate_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "wss"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != STREAM_PATH
    ):
        raise ValueError("Use the managed controller's verified WSS stream endpoint.")
    return url.rstrip("/")


class ComputerStream:
    def __init__(self, hub, session, valid):
        if UUID(session).hex != session:
            raise ValueError("Invalid computer allocation.")
        self.hub, self.session, self.valid = hub, session, valid
        self.identity, self.token = uuid4().hex, secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.closed = False
        self.claimed = False
        self.loop = self.queue = self.websocket = None
        self.pending = {}
        self.created = time.monotonic()

    def wait(self, timeout=10):
        if not self.ready.wait(timeout) or self.closed:
            self.close()
            raise StreamUnavailable("Computer stream did not connect.")

    def request(self, request, *, timeout):
        # This callback verifies the same process/session and shared worker epoch
        # used by provider RPC. No network status probe is needed per gesture.
        if not self.valid():
            self.close()
            raise StreamUnavailable("Computer stream ownership ended.")
        identity = request.get("id")
        wire = json.dumps(request, separators=(",", ":"))
        if (
            request.get("session") != self.session
            or not isinstance(identity, str)
            or UUID(identity).hex != identity
            or request.get("operation") not in {"screen", "human_input"}
            or len(wire.encode()) > 48000
        ):
            raise StreamUnavailable("Invalid computer stream request.")
        future = Future()
        with self.lock:
            if self.closed or not self.ready.is_set() or identity in self.pending:
                raise StreamUnavailable("Computer stream is disconnected.")
            if len(self.pending) >= 2:
                raise StreamUnavailable("Computer stream is busy.")
            self.pending[identity] = future
            loop = self.loop

        def enqueue():
            try:
                self.queue.put_nowait(wire)
            except asyncio.QueueFull:
                self.close()

        try:
            loop.call_soon_threadsafe(enqueue)
            reply = future.result(timeout)
            if not self.valid():
                raise StreamUnavailable("Computer stream ownership ended.")
            return reply
        except Exception:
            # A timeout may follow an applied input. Close, never retry it.
            self.close()
            raise StreamUnavailable("Computer stream receipt was not confirmed.") from None
        finally:
            with self.lock:
                self.pending.pop(identity, None)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.token = ""
            pending = list(self.pending.values())
            self.pending.clear()
            loop, websocket = self.loop, self.websocket
            self.ready.set()
        self.hub.remove(self)
        for future in pending:
            if not future.done():
                future.set_exception(StreamUnavailable("Computer stream disconnected."))
        if loop and websocket:
            try:
                loop.call_soon_threadsafe(lambda: asyncio.create_task(websocket.close()))
            except RuntimeError:
                pass  # Event loop already stopped; capabilities are still gone.


class ComputerStreams:
    def __init__(self, url, *, certificate=None, bots=None):
        self.url = validate_url(url)
        self.certificate = certificate
        self.bots = None if bots is None else frozenset(bots)
        self.lock = threading.Lock()
        self.links = {}

    def enabled(self, bot_id):
        return self.bots is None or bot_id in self.bots

    def create(self, session, valid):
        link = ComputerStream(self, session, valid)
        with self.lock:
            self.links[link.identity] = link
        return link

    def remove(self, link):
        with self.lock:
            if self.links.get(link.identity) is link:
                del self.links[link.identity]

    def close(self):
        with self.lock:
            links = list(self.links.values())
        for link in links:
            link.close()

    async def __call__(self, scope, receive, send):
        from starlette.websockets import WebSocket

        if scope["type"] != "websocket":
            from starlette.responses import Response

            await Response(status_code=404)(scope, receive, send)
            return
        websocket = WebSocket(scope, receive, send)
        identity = scope["path"].rstrip("/").rsplit("/", 1)[-1]
        bearer = websocket.headers.get("authorization", "")
        with self.lock:
            link = self.links.get(identity)
        if link is None:
            await websocket.close(code=1008)
            return
        with link.lock:
            admitted = (
                not link.closed
                and not link.claimed
                and time.monotonic() - link.created < 30
                and bool(link.token)
                and hmac.compare_digest(bearer, "Bearer " + link.token)
            )
            if admitted:
                link.claimed = True
                link.token = ""  # Single-use handshake; no automatic reconnect.
        if not admitted:
            await websocket.close(code=1008)
            return
        sender = None
        try:
            if not await asyncio.to_thread(link.valid):
                await websocket.close(code=1008)
                return
            await websocket.accept()
            with link.lock:
                if link.closed:
                    return
                link.loop = asyncio.get_running_loop()
                link.queue = asyncio.Queue(maxsize=2)
                link.websocket = websocket
                link.ready.set()

            async def transmit():
                while True:
                    await websocket.send_text(await link.queue.get())

            sender = asyncio.create_task(transmit())
            while True:
                # Large frames are bounded again before JSON parsing. Uvicorn's
                # own maximum remains in force; errors never include the body.
                wire = await websocket.receive_text()
                if len(wire.encode()) > MAX_MESSAGE:
                    raise ValueError
                reply = json.loads(wire)
                if not isinstance(reply, dict) or reply.get("session") != link.session:
                    raise ValueError
                with link.lock:
                    future = link.pending.get(reply.get("id"))
                    if future is None or future.done():
                        raise ValueError
                    future.set_result(reply)
        except Exception:
            pass  # No private frame/input, token or upstream error in logs.
        finally:
            link.close()  # Revoke even when ASGI shutdown cancels the cleanup await.
            if sender:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
