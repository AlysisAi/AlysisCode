"""Private, credential-free RPC bridge inside one disposable computer.

Only the trusted controller can reach this Unix socket. Agent programs see only
/workspace in OfflineLinuxDesktop's existing namespace, never the socket,
transfer staging, cloud credentials or the Bots database. No TCP listener.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

from .desktop_linux import DesktopError, OfflineLinuxDesktop
from .files import FILE_CHUNK_BYTES, MAX_FILE_BYTES
from .models import FileInput
from .private_input import PrivateInput
from .screen_channel import ScreenServer as ReadOnlyScreenServer
from .workspace_files import stamp

MAX_WIRE = 4 * 1024 * 1024


def root_for(session):
    if UUID(session).hex != session:
        raise ValueError("Invalid session identity.")
    return Path("/tmp") / ("alysis-cloud-" + session)


class PipeClosed(DesktopError):
    """The other end of the computer's pipe went away before a whole message arrived."""


def receive(connection):
    data = bytearray()
    while b"\n" not in data:
        chunk = connection.recv(65536)
        if not chunk:
            raise PipeClosed("Computer pipe closed without a receipt.")
        data.extend(chunk)
        if len(data) > MAX_WIRE:
            raise DesktopError("Computer message exceeds its allowance.")
    line, rest = data.split(b"\n", 1)
    if rest:
        raise DesktopError("Unexpected trailing computer message.")
    return json.loads(line)


def exchange(session, request, *, timeout=45, screen=False):
    screen = screen or request.get("operation") == "rfb"
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(timeout)
        connection.connect(str(root_for(session) / ("screen.sock" if screen else "control.sock")))
        connection.sendall(json.dumps(request).encode() + b"\n")
        return receive(connection)


class ScreenServer(ReadOnlyScreenServer):
    def __init__(self, session):
        super().__init__(root_for(session), session)


class Transfers:
    def __init__(self, root, workspace):
        self.root, self.workspace = root, workspace
        self.pending = {}

    def prepare(self, args):
        name = FileInput(name=args["name"], content="").name
        size = args["size"]
        digest = args["sha256"]
        if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
            raise ValueError("Invalid transfer size.")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ValueError("Invalid transfer digest.")
        if len(self.pending) >= 4:
            raise ValueError("Too many pending transfers.")
        identity = uuid4().hex
        path = self.root / ("transfer-" + identity)
        self.pending[identity] = (name, size, digest, path)
        return {"transfer_id": identity, "path": str(path)}

    def commit(self, args):
        name, size, digest, path = self.pending.pop(args["transfer_id"])
        directory = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fd = None
        owned = self.root / ("publish-" + uuid4().hex)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size != size:
                raise ValueError("Incomplete transfer.")
            actual = hashlib.sha256()
            with owned.open("xb") as output:
                count = 0
                while chunk := os.read(fd, FILE_CHUNK_BYTES):
                    count += len(chunk)
                    if count > size:
                        raise ValueError("Transfer grew before publication.")
                    actual.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if actual.hexdigest() != digest or stamp(os.fstat(fd)) != stamp(before):
                raise ValueError("Transfer changed before publication.")
            os.link(owned, name, dst_dir_fd=directory, follow_symlinks=False)
            os.fsync(directory)
            return {"name": name, "bytes": size, "sha256": digest, "written": True}
        finally:
            if fd is not None:
                os.close(fd)
            os.close(directory)
            path.unlink(missing_ok=True)
            owned.unlink(missing_ok=True)

    def export(self, args):
        name = FileInput(name=args["name"], content="").name
        directory = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fd = None
        target = self.root / ("export-" + uuid4().hex)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
                raise ValueError("Export requires a bounded regular file.")
            digest, size = hashlib.sha256(), 0
            with target.open("xb") as output:
                while chunk := os.read(fd, FILE_CHUNK_BYTES):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES or stamp(os.fstat(fd)) != stamp(before):
                        raise ValueError("File changed during export.")
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size != before.st_size or stamp(os.fstat(fd)) != stamp(before):
                raise ValueError("File changed during export.")
            return {"name": name, "path": str(target), "bytes": size, "sha256": digest.hexdigest()}
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        finally:
            if fd is not None:
                os.close(fd)
            os.close(directory)


def browser_error_reply(error, *, fatal: bool) -> dict:
    """The reply fields for a browser operation that failed: its type (the host rebuilds the
    error from it, cloud_browser.remote_error) and whether the browser stopped; for support its
    class and, for a stopped browser, its fixed reason (process exit status or failed
    connection), for a refused protocol command its method name and numeric code, or a network
    error's identifier, never page data. A dialog's type, message and frame (address and id)
    are the page's own (bounded by the browser), sent for the owl to answer it, never put in
    diagnostics."""
    from ..ide.cdp_websocket_transport import CdpCommandError
    from ..ide.managed_browser import (
        BrowserDialogOpenError,
        BrowserLaunchError,
        BrowserPageUnreachableError,
    )
    from .cloud_browser import ERRORS

    reply = {
        "error": "Browser operation rejected; inspect before retrying.",
        "error_type": type(error).__name__ if type(error).__name__ in ERRORS else "BrowserError",
        "fatal": fatal,
        "error_class": type(error).__name__[:60],
    }
    if isinstance(error, BrowserLaunchError | CdpCommandError | BrowserPageUnreachableError):
        reply["error_detail"] = str(error)[:200]
    if isinstance(error, BrowserDialogOpenError):
        reply["dialog"] = {
            "type": error.dialog_type,
            "message": error.dialog_message,
            **({"url": error.dialog_url} if error.dialog_url else {}),
            **({"frame_id": error.dialog_frame_id} if error.dialog_frame_id else {}),
        }
    return reply


def rejected_reply(error, *, fatal: bool) -> dict:
    """The reply fields for an operation the computer refused (not a browser error): what kind
    of error it was, as ``error_type`` and ``error_class``, and for support a reason that says
    why without any data: a refusal whose every message is fixed text written in this code
    (an archive or browser profile boundary) gives that text, an operating-system error its
    errno name (e.g. ENOSPC), and anything else no detail, since a ValueError or KeyError text
    can quote what it was given."""
    import errno

    from ..ide.browser_profiles import BrowserProfileError
    from .workspace_archive import WorkspaceArchiveError

    name = type(error).__name__[:60]
    reply = {
        "error": "Computer operation rejected; inspect before retrying.",
        "error_type": name,
        "error_class": name,
        "fatal": fatal,
    }
    if isinstance(error, WorkspaceArchiveError | BrowserProfileError):
        reply["error_detail"] = str(error)[:200]
    elif isinstance(error, OSError) and isinstance(error.errno, int):
        reply["error_detail"] = errno.errorcode.get(error.errno, str(error.errno))
    return reply


def serve(session, *, memory_sleep=False):
    from ..ide.browser_profiles import BrowserProfileError
    from ..ide.managed_browser import BrowserError
    from .cloud_browser_worker import FEATURES, BrowserBridge
    from .workspace_archive import WorkspaceArchiveError

    root = root_for(session)
    root.mkdir(mode=0o700)
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    transfers = Transfers(root, workspace)
    checkpoints = root / "checkpoints"
    checkpoints.mkdir(mode=0o700)
    checkpoint_transfers = Transfers(root, checkpoints)
    desktop = None
    display = None
    packages = None
    launchers = root / "launchers"
    launchers.mkdir(mode=0o700)

    def ensure_desktop(*, shared=False):
        nonlocal desktop, display, packages
        if desktop is None:
            if packages is None:
                from .package_access import start_package_guard

                # This computer reaches the web; its shell reaches only the declared package
                # indexes, through this guard. Without one the shell stays fully offline.
                packages = start_package_guard(root / "packages") or False
            guard = packages.directory if packages else None
            if shared:
                from .linux_display import PrivateLinuxDisplay

                display = PrivateLinuxDisplay()
                desktop = OfflineLinuxDesktop(
                    session,
                    workspace,
                    shared_display=display.name,
                    launchers=launchers,
                    packages=guard,
                )
            else:
                desktop = OfflineLinuxDesktop(session, workspace, packages=guard)
        if shared and display is None:
            raise DesktopError("Open a new computer to use the shared Cloud desktop.")
        screen.bind_live(desktop.rfb)
        return desktop

    browser = None
    active_surface = None
    browser_restore = None
    seen = set()
    screen = ScreenServer(session)
    private_input = PrivateInput(session)
    try:
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(root / "control.sock"))
            os.chmod(root / "control.sock", 0o600)
            listener.listen(2)
            # An abandoned bridge exits; cleanup also kills its private children.
            # The provider owns the idle guard for memory-preserving workers.
            # A guest timeout can expire while its VM sleeps and kill unsaved
            # applications immediately on resume.
            listener.settimeout(None if memory_sleep else 180)
            while True:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(45)
                    try:
                        request = receive(connection)
                    except (PipeClosed, ConnectionError, TimeoutError):
                        # The bridge client went away before its request arrived whole, so
                        # nothing ran; the computer stays for the next request.
                        continue
                    identity = request.get("id")
                    is_input = request.get("operation") == "human_input"
                    memory_read = memory_sleep and request.get("operation") in {
                        "ping",
                        "human_input_key",
                        "resume_memory",
                        "observe",
                    }
                    if (
                        request.get("session") != session
                        or not isinstance(identity, str)
                        or UUID(identity).hex != identity
                        or (
                            not is_input
                            and not memory_read
                            and (identity in seen or len(seen) >= 4096)
                        )
                    ):
                        raise ValueError("Invalid or replayed RPC.")
                    if not is_input and not memory_read:
                        seen.add(identity)
                    reply = {"id": identity, "session": session}
                    try:
                        operation, args = request["operation"], request["arguments"]
                        if not isinstance(args, dict):
                            raise ValueError("Invalid arguments.")
                        if operation == "ping":
                            result = {
                                "ready": desktop is None or desktop.alive(),
                                "uid": os.getuid(),
                            }
                            if memory_sleep:
                                result["memory_sleep"] = True
                        elif operation == "resume_memory" and memory_sleep:
                            # Revoke the previous controller's encrypted input
                            # channel and clear held keys before exposing a
                            # restored desktop. No agent operation is replayed.
                            if browser is not None:
                                browser.computer.human_input({"kind": "release"})
                            if desktop is not None:
                                desktop.human_input({"kind": "release"})
                            private_input = PrivateInput(session)
                            result = {
                                "ready": True,
                                "uid": os.getuid(),
                                "memory_sleep": True,
                                "surface": active_surface,
                            }
                        elif operation == "human_input_key":
                            result = {"public_key": private_input.public_key}
                        elif operation == "human_input":
                            data = private_input.open(identity, args)
                            if data["surface"] == "browser":
                                if browser is None:
                                    raise ValueError("Browser has not started.")
                                screen.bind(browser.computer.capture)
                                active_surface = "browser"
                                browser.frame = None
                                result = browser.computer.human_input(data["event"])
                            else:
                                if desktop is None:
                                    raise ValueError("Desktop has not started.")
                                screen.bind(desktop.capture)
                                active_surface = "desktop"
                                result = desktop.human_input(data["event"])
                        elif operation == "browser_start":
                            if browser is not None:
                                raise ValueError("Browser already started.")
                            ensure_desktop(shared=True)
                            browser = BrowserBridge(
                                session,
                                root,
                                args["preview_urls"],
                                persistent=args.get("persistent", False),
                                restore=browser_restore,
                                shared_display=display.name,
                                open_request=launchers / "open-browser",
                            )
                            if browser_restore is not None:
                                browser_restore.unlink()
                                browser_restore = None
                            screen.bind(browser.computer.capture)
                            active_surface = "browser"
                            # What this computer's browser can do beyond the first images'.
                            result = {"ready": browser.computer.alive(), "features": list(FEATURES)}
                        elif operation == "browser_call":
                            if browser is None:
                                raise ValueError("Browser has not started.")
                            screen.bind(browser.computer.capture)
                            active_surface = "browser"
                            result = browser.call(args)
                        elif operation == "browser_site_data":
                            # Sign-ins from the owl's details: the screen the person sees stays.
                            if browser is None:
                                raise ValueError("Browser has not started.")
                            if args.get("method") not in {"site_cookies", "clear_site_data"}:
                                raise ValueError("Unsupported site data operation.")
                            result = browser.call(args)
                        elif operation == "browser_restore_prepare":
                            if browser is not None or browser_restore is not None:
                                raise ValueError("Restore must precede browser startup.")
                            result = checkpoint_transfers.prepare({**args, "name": uuid4().hex})
                        elif operation == "browser_restore":
                            if browser is not None or browser_restore is not None:
                                raise ValueError("Restore must precede browser startup.")
                            receipt = checkpoint_transfers.commit(args)
                            browser_restore = checkpoints / receipt["name"]
                            result = {"ready": True}
                        elif operation == "browser_snapshot":
                            if browser is None:
                                raise ValueError("Browser has not started.")
                            screen.bind(None)
                            result = browser.snapshot()
                        elif operation == "capture":
                            ensure_desktop()
                            screen.bind(desktop.capture)
                            active_surface = "desktop"
                            result = {"png": base64.b64encode(desktop.capture()).decode()}
                        elif operation == "import_prepare":
                            result = transfers.prepare(args)
                        elif operation == "import_commit":
                            result = transfers.commit(args)
                        elif operation == "export":
                            result = transfers.export(args)
                        elif operation == "workspace_restore_prepare":
                            result = checkpoint_transfers.prepare({**args, "name": uuid4().hex})
                        elif operation == "workspace_restore":
                            from .workspace_archive import restore_workspace

                            if desktop is not None:
                                raise ValueError("Restore must precede all workspace writers.")
                            receipt = checkpoint_transfers.commit(args)
                            archive = checkpoints / receipt["name"]
                            try:
                                result = restore_workspace(archive, workspace)
                            finally:
                                archive.unlink(missing_ok=True)
                        elif operation in {"workspace_snapshot", "workspace_checkpoint"}:
                            from .workspace_archive import pack_workspace

                            # A human handback saves stable files without destroying
                            # the applications whose current state the Owl must inspect.
                            if operation == "workspace_snapshot":
                                screen.bind(None)
                                if browser is not None:
                                    browser.close()
                                if desktop is not None:
                                    desktop.close()
                            target = root / ("export-" + uuid4().hex)
                            result = {**pack_workspace(workspace, target), "path": str(target)}
                        else:
                            ensure_desktop()
                            screen.bind(desktop.capture)
                            active_surface = "desktop"
                            result = desktop.call(session, operation, args)
                        reply["result"] = result
                    except BrowserError as error:
                        reply.update(
                            browser_error_reply(
                                error, fatal=not browser or not browser.computer.alive()
                            )
                        )
                    except (
                        ValueError,
                        KeyError,
                        OSError,
                        DesktopError,
                        WorkspaceArchiveError,
                        BrowserProfileError,
                    ) as error:
                        reply.update(
                            rejected_reply(error, fatal=desktop is not None and not desktop.alive())
                        )
                    if desktop is not None:
                        # What the running desktop declared at its start (window elements);
                        # a host offers tools for it only while it is declared.
                        reply["desktop_features"] = sorted(getattr(desktop, "desktop_features", ()))
                    try:
                        connection.sendall(
                            json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n"
                        )
                    except (ConnectionError, TimeoutError):
                        # The bridge client gave up before this answer (its wait ran out, or
                        # its command was stopped). The operation ran; its host never got this
                        # answer and treats the outcome as unconfirmed. Everything open on the
                        # computer stays for the next request.
                        pass
    finally:
        screen.close()
        if browser is not None:
            browser.close()
        if desktop is not None:
            desktop.close()
        if display is not None:
            display.close()
        if packages:
            packages.close()


def main():
    # Azure's operator API is root. Never run the bridge or agent as root.
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(1000)
        os.setuid(1000)
    if os.geteuid() != 1000:
        raise ValueError("Worker image requires UID 1000.")
    mode, session = sys.argv[1:3]
    root_for(session)
    if mode in {"serve", "serve_memory"}:
        serve(session, memory_sleep=mode == "serve_memory")
        return
    if mode in {"start", "start_memory"}:
        if root_for(session).exists():
            raise ValueError("Session already exists; do not replay startup.")
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "alysis_code.bots.cloud_desktop_worker",
                "serve_memory" if mode == "start_memory" else "serve",
                session,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "/opt/alysis/src", "LANG": "C.UTF-8"},
            start_new_session=True,
            close_fds=True,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and process.poll() is None:
            if (root_for(session) / "control.sock").exists():
                ready = {"ready": True, "session": session, "uid": os.getuid()}
                if mode == "start_memory":
                    ready["memory_sleep"] = True
                print(json.dumps(ready))
                return
            time.sleep(0.1)
        raise DesktopError("Computer startup did not complete.")
    if mode not in {"request", "screen"}:
        raise ValueError("Unknown bridge mode.")
    payload = base64.b64decode(sys.argv[3], validate=True)
    if len(payload) > 48000:
        raise ValueError("Computer request too large.")
    reply = exchange(
        session,
        json.loads(payload),
        timeout=4 if mode == "screen" else 45,
        screen=mode == "screen",
    )
    sys.stdout.buffer.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        # No guest traceback, paths, file contents or operator credentials.
        print(json.dumps({"error": "Computer bridge unavailable; outcome may be uncertain."}))
        sys.exit(1)
