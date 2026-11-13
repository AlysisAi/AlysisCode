"""Guest side of the private managed-browser bridge; no model or cloud keys."""

from __future__ import annotations

import base64
import hashlib
import os
from uuid import uuid4

from ..ide.browser_profiles import BrowserProfileStore
from ..ide.managed_browser import BrowserStaleFrameError
from .browser_linux import LinuxBrowserComputer
from .cloud_browser import METHODS
from .cloud_desktop_worker import Transfers
from .compute import transfer_chunks
from .files import MAX_FILE_BYTES
from .models import FileInput


class BrowserBridge:
    def __init__(self, session, root, preview_urls, *, persistent=False, restore=None):
        self.session, self.root = session, root
        uploads = root / "browser-uploads"
        uploads.mkdir(mode=0o700)
        self.transfers = Transfers(root, uploads)
        self.upload_names = {}
        self.frame = None
        self.profiles = BrowserProfileStore(root / "browser-profiles") if persistent else None
        if restore is not None:
            from .workspace_archive import restore_workspace

            if self.profiles is None:
                raise ValueError("Persistent browser storage was not enabled.")
            lease = self.profiles.acquire("private-browser", session)
            try:
                restore_workspace(restore, lease.path)
            finally:
                lease.release()
        self.computer = LinuxBrowserComputer(
            session,
            lambda: False,
            preview_urls=lambda: tuple(preview_urls),
            system_trust=True,
            profile_store=self.profiles,
            account_id="private-browser" if persistent else None,
        )

    def close(self):
        self.computer.close()

    def snapshot(self):
        from .workspace_archive import pack_workspace

        if self.profiles is None:
            raise ValueError("Persistent browser storage was not enabled.")
        self.close()
        # Reacquisition verifies the managed browser's clean-shutdown attestation.
        lease = self.profiles.acquire("private-browser", self.session)
        try:
            if lease.recovered:
                raise ValueError(
                    "Browser shutdown was unclean; the previous saved state was retained."
                )
            target = self.root / ("export-" + uuid4().hex)
            return {**pack_workspace(lease.path, target), "path": str(target)}
        finally:
            lease.release()

    def call(self, args):
        method = args["method"]
        browser, owner, sid = self.computer.browser, self.session, self.computer.session_id
        if method in METHODS:
            result = getattr(browser, method)(owner, sid, *args["args"], **args["kwargs"])
            return result if isinstance(result, dict) else {"dispatched": True}
        if method == "status":
            status = browser.status(owner, sid)
            return {"state": status.state, "command_generation": status.command_generation}
        if method == "screen":
            return {"png": base64.b64encode(self.computer.capture()).decode()}
        if method == "frame":
            frame = browser.capture_visual_frame(owner, sid)
            identity = uuid4().hex
            self.frame = (identity, frame)
            return {
                "frame_id": identity,
                "url": frame.url,
                "command_generation": frame.command_generation,
                "width": frame.width,
                "height": frame.height,
                "png": base64.b64encode(frame.png).decode(),
            }
        if method == "visual_click":
            saved, self.frame = self.frame, None
            if saved is None or saved[0] != args["frame_id"]:
                raise BrowserStaleFrameError("Visual observation expired.")
            return browser.click_visual_frame(owner, sid, saved[1], args["x"], args["y"])
        if method == "upload_prepare":
            name = FileInput(name=args["name"], content="").name
            prepared = self.transfers.prepare({**args, "name": uuid4().hex})
            self.upload_names[prepared["transfer_id"]] = name
            return prepared
        if method == "upload":
            identity = args["transfer_id"]
            name = self.upload_names.pop(identity)
            receipt = self.transfers.commit({"transfer_id": identity})
            path = self.transfers.workspace / receipt["name"]
            try:
                with path.open("rb") as stream:
                    return browser.upload_file(
                        owner,
                        sid,
                        args["selector"],
                        name=name,
                        size=receipt["bytes"],
                        chunks=iter(lambda: stream.read(65536), b""),
                        expected_sha256=receipt["sha256"],
                        **args["options"],
                    )
            finally:
                path.unlink(missing_ok=True)
        if method == "download":

            def collect(chunks, size):
                if not 0 <= size <= MAX_FILE_BYTES:
                    raise ValueError("Download exceeds the transfer allowance.")
                path = self.root / ("export-" + uuid4().hex)
                digest = hashlib.sha256()
                try:
                    with path.open("xb") as stream:
                        for chunk in transfer_chunks(chunks, size, lambda: None):
                            digest.update(chunk)
                            stream.write(chunk)
                        stream.flush()
                        os.fsync(stream.fileno())
                    return {"path": str(path), "bytes": size, "sha256": digest.hexdigest()}
                except BaseException:
                    path.unlink(missing_ok=True)
                    raise

            return browser.download(
                owner,
                sid,
                args["selector"],
                max_bytes=args["max_bytes"],
                collect=collect,
                **args["options"],
            )
        raise ValueError("Unsupported browser operation.")
