"""Managed browser RPC on the same disposable computer as offline file work.

All page/task authorization stays in BrowserComputers. The guest also enforces
ManagedBrowserService's current node/generation, visual frame and egress guards.
Only bytes and typed receipts cross the private operator transport.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
from contextlib import closing
from types import SimpleNamespace
from uuid import uuid4

from ..ide.managed_browser import (
    BrowserCancelledError,
    BrowserDialogOpenError,
    BrowserDownloadRequiredError,
    BrowserError,
    BrowserInputUnconfirmedError,
    BrowserLimitError,
    BrowserNoDialogError,
    BrowserNotFoundError,
    BrowserPageUnavailableError,
    BrowserPageUnreachableError,
    BrowserSecurityError,
    BrowserStaleFrameError,
    BrowserTargetHiddenError,
    BrowserTargetNotReadyError,
    BrowserTargetObscuredError,
    BrowserTargetUnstableError,
    BrowserTimeoutError,
    BrowserValidationError,
    BrowserWaitTimeoutError,
)
from .cloud_desktop import CloudDesktopFiles, worker_argv
from .compute import transfer_chunks
from .desktop_linux import ComputerReplyTooLarge

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
        "fill",
        "hover",
        "set_checked",
        "drag",
        "wait_for",
        "read_value",
        "handle_dialog",
        "page_report",
        "history",
        "select_options",
        "capture_image",
    }
)
# What this version's browser does beyond the first images', by the names its computer declares
# when the browser starts (cloud_browser_worker.FEATURES; LinuxBrowserComputer for the in-process
# lab). A host offers a tool only for what the running browser declared, so an older image keeps
# today's tools. "element_identity": an element is addressed by its observed identity in any
# frame (same- and out-of-process iframes) and in open shadow roots, and is verified there, and
# the hit test and focus checks keep input on it. "key_chord": press_key takes held modifiers.
# "frame_snapshots": snapshot(kind="dom", frames=True) also returns each out-of-process frame's
# documents (another site's frame), with the frame's id, address and origin. "step_dialog": an
# acting operation's dialog answer accepts only the one dialog the step itself opens in a frame
# of the origin it acts in, and a dialog the page opened before the step refuses the step before
# anything is sent. "dialog_frames": a dialog is reported with the frame that shows it (address
# and frame id), and handle_dialog(frame_id=...) answers only that frame's dialog.
# "page_reported": page_report says what the page reported during the last acting step: its
# own requests that failed or answered with an error status, and its uncaught script errors.
# "history": history(move=...) goes back, forward or reloads without sending a form again.
# "click_options": click and visual_click take a button (right, middle) and a click count
# (double, triple). "select_options": select_options chooses several options of a multiple
# select. "page_images": capture_image shows one element or the full page as an image.
# "file_chooser": a step that opens a file chooser holds it and reports its file input, which
# upload_file answers.
OPERATIONS = (
    "element_identity",
    "fill",
    "key_chord",
    "hover",
    "set_checked",
    "drag",
    "wait_for",
    "read_value",
    "handle_dialog",
    "frame_snapshots",
    "step_dialog",
    "dialog_frames",
    "page_reported",
    "history",
    "click_options",
    "select_options",
    "page_images",
    "file_chooser",
)
ERRORS = {
    cls.__name__: cls
    for cls in (
        BrowserError,
        BrowserCancelledError,
        BrowserDialogOpenError,
        BrowserDownloadRequiredError,
        BrowserInputUnconfirmedError,
        BrowserLimitError,
        BrowserNoDialogError,
        BrowserNotFoundError,
        BrowserPageUnavailableError,
        BrowserPageUnreachableError,
        BrowserSecurityError,
        BrowserStaleFrameError,
        BrowserTargetHiddenError,
        BrowserTargetNotReadyError,
        BrowserTargetObscuredError,
        BrowserTargetUnstableError,
        BrowserTimeoutError,
        BrowserValidationError,
        BrowserWaitTimeoutError,
    )
}


def remote_error(reply: dict) -> BrowserError:
    """The typed browser error a computer reported, rebuilt from its reply.

    Only the error's type and fixed details cross: never the guest's own message. A dialog's
    type, message and frame address are the page's text (the guest bounds them), kept as page
    data, with the frame's id (checked again by the error itself); a network error is
    Chromium's identifier, validated again by the error itself."""
    error = ERRORS.get(reply.get("error_type"), BrowserError)
    if error is BrowserDialogOpenError:
        dialog = reply.get("dialog")
        dialog = dialog if isinstance(dialog, dict) else {}
        return BrowserDialogOpenError(
            str(dialog.get("type") or "alert"),
            str(dialog.get("message") or ""),
            dialog.get("url"),
            dialog.get("frame_id"),
        )
    if error is BrowserPageUnreachableError:
        found = re.search(r"net::ERR_[A-Z0-9_]{1,64}", str(reply.get("error_detail") or ""))
        return BrowserPageUnreachableError(found.group(0) if found else None)
    return error("Cloud browser rejected the operation; inspect before retrying.")


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

    @property
    def browser_features(self):
        """What this computer's browser declared it can do when it started (OPERATIONS)."""
        return self._surface.browser_features

    def bind_task(self, task_id):
        self._surface.bind_task(task_id)

    @property
    def surfaces(self):
        return self._surface.surfaces

    def rfb(self, args):
        return self._surface.rfb(args)

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
        try:
            return self.executor._surface._request(
                "browser_call", {"method": method, **arguments}, cancel=cancel or (lambda: False)
            )
        except ComputerReplyTooLarge:
            # Only a read can end here (cloud_desktop.reads_only); the browser and page are as
            # they were, so the reader falls back to less (the page as text, its parts).
            raise BrowserLimitError(
                "This page is too large to read in one step. Read its text in parts with "
                "browser_source."
            ) from None

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

    def click_visual_frame(self, owner, sid, frame, x, y, *, cancel=None, **options):
        self._owned(owner, sid)
        # Options (button, click count) are sent only to a computer that declares them.
        return self._rpc(
            "visual_click",
            {
                "frame_id": frame.frame_id,
                "x": x,
                "y": y,
                **({"options": options} if options else {}),
            },
            cancel,
        )

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
