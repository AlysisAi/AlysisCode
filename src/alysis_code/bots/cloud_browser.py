"""Managed browser RPC on the same disposable computer as offline file work.

All page/task authorization stays in BrowserComputers. The guest also enforces
ManagedBrowserService's current node/generation, visual frame and egress guards.
Only bytes and typed receipts cross the private operator transport.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from contextlib import closing
from types import SimpleNamespace
from uuid import uuid4

from ..ide.managed_browser import (
    BrowserCancelledError,
    BrowserDownloadRequiredError,
    BrowserError,
    BrowserLimitError,
    BrowserNotFoundError,
    BrowserPageUnavailableError,
    BrowserPageUnreachableError,
    BrowserSecurityError,
    BrowserStaleFrameError,
    BrowserTargetObscuredError,
    BrowserTimeoutError,
    BrowserValidationError,
)
from .cloud_desktop import CloudDesktopFiles, worker_argv
from .compute import transfer_chunks

# Explicit RPC interface, not arbitrary reflection or model-selected methods.
METHODS = frozenset(
    {
        "navigate",
        "snapshot",
        "pages",
        "switch_page",
        "close_page",
        "click",
        "type_text",
        "select_option",
        "press_key",
        "scroll",
    }
)
ERRORS = {
    cls.__name__: cls
    for cls in (
        BrowserError,
        BrowserCancelledError,
        BrowserDownloadRequiredError,
        BrowserLimitError,
        BrowserNotFoundError,
        BrowserPageUnavailableError,
        BrowserPageUnreachableError,
        BrowserSecurityError,
        BrowserStaleFrameError,
        BrowserTargetObscuredError,
        BrowserTimeoutError,
        BrowserValidationError,
    )
}


class CloudBrowser:
    retire_on_release = True
    lifetime = (
        "Cloud browser closes at task completion. Tabs and website storage are temporary. "
        "Saved library downloads survive. Inspect results before finishing."
    )

    def __init__(self, surface, previews, cancel):
        self._surface = surface
        self.owner = surface.owner
        self.session_id = surface.identity.allocation_id
        self.preview_urls = lambda: previews
        self.browser = RemoteBrowser(self)
        self._view_lock = threading.Lock()
        surface.start_browser(previews, cancel)
        if surface._browser_states is not None:
            self.lifetime = (
                "Cloud browser closes at task completion. Website storage is encrypted and "
                "retained for this bot after successful completion and verified clean shutdown. "
                "Stop or failure retains the previous checkpoint; changes in that task can be lost. "
                "Reopening starts a blank page; tabs and unsaved page state are not restored. "
                "Website sessions can expire. Saved library downloads survive."
            )

    def alive(self):
        return self._surface.alive()

    def bind_task(self, task_id):
        self._surface.bind_task(task_id)

    def human_input(self, event):
        return self._surface.human_input(event)

    def input_ready(self):
        return self._surface.input_ready()

    def prepare_input(self):
        self._surface.prepare_input()

    def close(self):
        self._surface.close()

    def finish(self, *, cancel=lambda: False):
        self._surface.finish(cancel=cancel)

    def checkpoint(self, session):
        self._surface.checkpoint(session)

    def suspend(self, session):
        self._surface.suspend(session)

    def capture(self):
        if not self._view_lock.acquire(blocking=False):
            raise BlockingIOError("Screen capture is already running.")
        try:
            if not self.alive():
                raise BrowserError("Cloud browser is closed.")
            if self._surface._streams is not None or self._surface._native_stream:
                return self._surface.capture()
            identity = uuid4().hex
            request = {"id": identity, "session": self.session_id}
            receipt = self._surface.provider.run(
                self._surface.ref,
                worker_argv(
                    "screen",
                    self.session_id,
                    base64.b64encode(json.dumps(request).encode()).decode(),
                ),
                timeout=4,
            )
            reply = json.loads(receipt.stdout)
            if (
                receipt.exit_code != 0
                or not self.alive()
                or reply.get("id") != identity
                or reply.get("session") != self.session_id
            ):
                raise BrowserError("No current screen receipt.")
            return base64.b64decode(reply["result"]["png"], validate=True)
        finally:
            self._view_lock.release()


class RemoteBrowser:
    def __init__(self, executor):
        self.executor = executor

    def _owned(self, owner, sid):
        if owner != self.executor.owner or sid != self.executor.session_id:
            raise BrowserError("Cloud browser ownership does not match.")

    def _rpc(self, method, arguments, cancel=None):
        return self.executor._surface._request(
            "browser_call", {"method": method, **arguments}, cancel=cancel or (lambda: False)
        )

    def __getattr__(self, name):
        if name not in METHODS:
            raise AttributeError(name)

        def invoke(owner, sid, *args, cancel=None, **kwargs):
            self._owned(owner, sid)
            return self._rpc(name, {"args": list(args), "kwargs": kwargs}, cancel)

        return invoke

    def status(self, owner, sid):
        self._owned(owner, sid)
        return SimpleNamespace(**self._rpc("status", {}))

    def capture_visual_frame(self, owner, sid, *, cancel=None):
        self._owned(owner, sid)
        result = self._rpc("frame", {}, cancel)
        result["png"] = base64.b64decode(result["png"], validate=True)
        return SimpleNamespace(**result)

    def click_visual_frame(self, owner, sid, frame, x, y, *, cancel=None):
        self._owned(owner, sid)
        return self._rpc("visual_click", {"frame_id": frame.frame_id, "x": x, "y": y}, cancel)

    def upload_file(
        self, owner, sid, selector, *, name, size, chunks, expected_sha256, cancel=None, **options
    ):
        self._owned(owner, sid)
        executor = self.executor._surface
        check = cancel or (lambda: False)
        prepared = self._rpc(
            "upload_prepare", {"name": name, "size": size, "sha256": expected_sha256}, check
        )
        path = CloudDesktopFiles._path(executor, prepared["path"], "transfer-")
        executor.provider.upload(executor.ref, path, chunks, size=size, cancel=check)
        return self._rpc(
            "upload",
            {"transfer_id": prepared["transfer_id"], "selector": selector, "options": options},
            check,
        )

    def download(self, owner, sid, selector, *, max_bytes, collect, cancel=None, **options):
        self._owned(owner, sid)
        executor = self.executor._surface
        check = cancel or (lambda: False)
        receipt = self._rpc(
            "download", {"selector": selector, "max_bytes": max_bytes, "options": options}, check
        )
        path = CloudDesktopFiles._path(executor, receipt["path"], "export-")
        size = receipt["bytes"]
        if type(size) is not int or not 0 <= size <= max_bytes:
            raise BrowserError("Cloud download size is invalid.")

        def checkpoint():
            if check():
                from ..cancellation import CooperativeCancellationError

                raise CooperativeCancellationError("Cloud download cancelled.")

        with closing(
            executor.provider.download(executor.ref, path, size=size, cancel=check)
        ) as data:

            def verified():
                digest = hashlib.sha256()
                for chunk in transfer_chunks(data, size, checkpoint, BrowserError):
                    digest.update(chunk)
                    yield chunk
                if digest.hexdigest() != receipt["sha256"]:
                    raise BrowserError("Cloud download integrity check failed.")

            with closing(verified()) as chunks:
                return collect(chunks, size)
