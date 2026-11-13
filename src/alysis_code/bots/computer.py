"""Read-only views of executor-owned computers; never discover a global display.

Only trusted executor code can bind a capture callback to a bot. Neither HTTP
clients nor model arguments can supply a display, path, URL or session identity.
The executor must revoke the binding before releasing/reusing its computer.
"""

from __future__ import annotations

import base64
import io
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from uuid import uuid4

from PIL import Image


class LinuxScreenCapture:
    """Bounded capture of a private X11 display selected by its trusted executor.

    This does not create a computer or establish ownership. Never attach a shared
    lab/host display to a user bot. The caller supplies the binding's liveness
    check and revokes it before releasing the private display.
    """

    def __init__(self, display: str):
        if not re.fullmatch(r":[0-9]{1,5}(?:\.[0-9]{1,3})?", display):
            raise ValueError("A private local X11 display is required.")
        self.display = display

    def __call__(self) -> bytes:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys; from PIL import ImageGrab; "
                "im=ImageGrab.grab(xdisplay=sys.argv[1]); "
                "assert 0 < im.width <= 2560 and 0 < im.height <= 1600; "
                "im.save(sys.stdout.buffer, format='PNG')",
                self.display,
            ],
            env={"PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=True,
        )
        return result.stdout


@dataclass
class _View:
    capture: Callable[[], bytes]
    alive: Callable[[], bool]
    generation: str = field(default_factory=lambda: uuid4().hex)
    lock: threading.Lock = field(default_factory=threading.Lock)
    human_input: Callable[[dict], dict] | None = None
    invalidate: Callable[[], None] | None = None
    checkpoint: Callable[[str], None] | None = None
    input_ready: Callable[[], bool] | None = None
    prepare_input: Callable[[], None] | None = None


class ComputerControlPause(Exception):
    """Park this turn without disposing of its human-controlled computer."""


class ComputerLocks:
    """Serialize one computer while unrelated computers perform I/O independently."""

    def __init__(self):
        self._guard = threading.Lock()
        self._entries = {}

    @contextmanager
    def hold(self, key, *, blocking=True):
        with self._guard:
            entry = self._entries.setdefault(key, [threading.RLock(), 0])
            entry[1] += 1  # Include waiters so the lock cannot be replaced under them.
        acquired = entry[0].acquire(blocking=blocking)
        try:
            yield acquired
        finally:
            if acquired:
                entry[0].release()
            with self._guard:
                entry[1] -= 1
                if entry[1] == 0:
                    del self._entries[key]


def computer_operation(method):
    @wraps(method)
    def guarded(self, task, *args, **kwargs):
        with self.views.agent_operation(self.owner, task["bot_id"]):
            return method(self, task, *args, **kwargs)

    return guarded


class ComputerViews:
    """Process-local bindings, deliberately not restored after service restart."""

    def __init__(self):
        self._views: dict[tuple[str, str, str], _View] = {}
        self._active: dict[tuple[str, str], str] = {}
        self._claims: dict[tuple[str, str], dict[str, str]] = {}
        self._lock = threading.Lock()
        self._fenced = set()
        self._operations = {}
        self._lifecycle = {}

    def set_lifecycle(self, owner, bot_id, phase):
        if phase not in {"queued", "starting", "ready", "saving", "stopped", "failed"}:
            raise ValueError("Unknown computer lifecycle phase.")
        with self._lock:
            key = (owner, bot_id)
            # A cleanup receipt must not hide the failure that prompted it.
            if phase != "stopped" or self._lifecycle.get(key) != "failed":
                self._lifecycle[key] = phase

    def lifecycle(self, owner, bot_id):
        with self._lock:
            return self._lifecycle.get((owner, bot_id))

    @contextmanager
    def agent_operation(self, owner, bot_id):
        key = (owner, bot_id)
        with self._lock:
            if key in self._fenced:
                raise ComputerControlPause()
            self._operations[key] = self._operations.get(key, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                self._operations[key] -= 1

    def fenced(self, owner, bot_id):
        with self._lock:
            return (owner, bot_id) in self._fenced

    def fence(self, owner, bot_id, enabled):
        with self._lock:
            if enabled:
                self._fenced.add((owner, bot_id))
            else:
                self._fenced.discard((owner, bot_id))

    def current(self, owner, bot_id):
        with self._lock:
            key = (owner, bot_id)
            view = self._views.get((*key, self._active.get(key, "computer")))
            claims = set(self._claims.get(key, {}).values())
            return view, next(iter(claims), None), self._operations.get(key, 0)

    def invalidate(self, owner, bot_id):
        with self._lock:
            views = [view for key, view in self._views.items() if key[:2] == (owner, bot_id)]
        for view in views:
            if view.invalidate:
                view.invalidate()

    def bind(
        self,
        owner: str,
        bot_id: str,
        *,
        capture: Callable[[], bytes],
        alive: Callable[[], bool],
        surface: str = "computer",
        human_input: Callable[[dict], dict] | None = None,
        invalidate: Callable[[], None] | None = None,
        checkpoint: Callable[[str], None] | None = None,
        input_ready: Callable[[], bool] | None = None,
        prepare_input: Callable[[], None] | None = None,
    ) -> str:
        view = _View(
            capture,
            alive,
            human_input=human_input,
            invalidate=invalidate,
            checkpoint=checkpoint,
            input_ready=input_ready,
            prepare_input=prepare_input,
        )
        with self._lock:
            if surface not in {"computer", "browser", "desktop"}:
                raise ValueError("Unknown computer surface.")
            existing = [key[2] for key in self._views if key[:2] == (owner, bot_id)]
            if surface in existing or (
                existing and (surface == "computer" or "computer" in existing)
            ):
                raise ValueError("Revoke the previous computer before binding another.")
            self._views[owner, bot_id, surface] = view
            self._active[owner, bot_id] = surface
        return view.generation

    def claim(self, owner: str, bot_id: str, task_id: str, surface: str):
        with self._lock:
            claims = self._claims.setdefault((owner, bot_id), {})
            if any(current != task_id for current in claims.values()):
                raise ValueError("This bot's computer is busy with another task.")
            claims[surface] = task_id

    def release_claim(self, owner: str, bot_id: str, task_id: str | None, surface: str):
        with self._lock:
            claims = self._claims.get((owner, bot_id), {})
            if claims.get(surface) == task_id:
                claims.pop(surface, None)
            if not claims:
                self._claims.pop((owner, bot_id), None)

    def activate(self, owner: str, bot_id: str, generation: str):
        with self._lock:
            for (account, bot, surface), view in self._views.items():
                if (account, bot) == (owner, bot_id) and view.generation == generation:
                    self._active[owner, bot_id] = surface
                    return

    def release_task(self, owner: str, task_id: str, surface: str):
        with self._lock:
            for key, claims in list(self._claims.items()):
                if key[0] == owner and claims.get(surface) == task_id:
                    del claims[surface]
                    if not claims:
                        del self._claims[key]

    def revoke(self, owner: str, bot_id: str, generation: str) -> None:
        with self._lock:
            for key, view in list(self._views.items()):
                if key[:2] == (owner, bot_id) and view.generation == generation:
                    del self._views[key]
                    if self._active.get((owner, bot_id)) == key[2]:
                        self._active.pop((owner, bot_id), None)
                        for other in self._views:
                            if other[:2] == (owner, bot_id):
                                self._active[owner, bot_id] = other[2]

    def frame(self, owner: str, bot_id: str) -> dict:
        with self._lock:
            surface = self._active.get((owner, bot_id), "computer")
            key = (owner, bot_id, surface)
            view = self._views.get(key)
        if view is None:
            return {"state": "not_running"}
        # One capture per computer at a time; watching never waits for or pauses
        # an agent action. Slow/stuck capture cannot accumulate queued requests.
        if not view.lock.acquire(blocking=False):
            return {"state": "connecting"}
        try:
            if not view.alive():
                return {"state": "disconnected"}
            started = time.monotonic()
            pixels = view.capture()
            captured_at = time.time()
            if len(pixels) > 4_000_000 or time.monotonic() - started > 5:
                return {"state": "disconnected"}
            with Image.open(io.BytesIO(pixels)) as image:
                if image.format not in {"PNG", "JPEG"} or not (
                    1 <= image.width <= 2560 and 1 <= image.height <= 1600
                ):
                    return {"state": "disconnected"}
                image.load()
                width, height = image.size
                output = io.BytesIO()
                # Drop metadata and bound IPC size. Frames live only in memory.
                clean = image.convert("RGB")
                clean.info.clear()
                clean.save(output, format="JPEG", quality=80)
            if output.tell() > 2_000_000 or time.monotonic() - started > 5 or not view.alive():
                return {"state": "disconnected"}
            with self._lock:
                if self._views.get(key) is not view or self._active.get((owner, bot_id)) != surface:
                    return {"state": "disconnected"}
            return {
                "state": "live",
                "session_id": view.generation,
                "captured_at": captured_at,
                "width": width,
                "height": height,
                "image": "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode(),
            }
        except BlockingIOError:
            return {"state": "connecting"}
        except Exception:
            # Never return raw capture errors (which can contain private paths).
            return {"state": "disconnected"}
        finally:
            view.lock.release()
