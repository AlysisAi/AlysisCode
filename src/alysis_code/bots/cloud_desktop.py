"""Task-scoped cloud computers behind the existing bot permission boundary.

The controller retains model credentials, library and a durable allocation intent.
Each task gets a fresh worker. Enabled workspace checkpoints preserve files from
completed tasks; library exports are separate. SQLite uses filesystem leases.
A shared store uses the worker epoch and durable allocation ledger for recovery.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from ..cancellation import CooperativeCancellationError
from . import diagnostics
from .compute import (
    ComputeCreationRefused,
    ComputeError,
    ComputerIdentity,
    ComputerRef,
    ComputeStopped,
    wait_for,
)
from .computer import ComputerLocks
from .computer_context import opening_task
from .desktop_linux import (
    ComputerActionUncertain,
    DesktopCapacityBusy,
    DesktopError,
    DesktopStartupCleanupError,
)
from .files import FILE_CHUNK_BYTES, MAX_FILE_BYTES
from .models import FileInput
from .object_storage import ObjectStorageError
from .private_input import seal_input
from .workspaces import WorkspaceError, WorkspaceStore, sync_directory

MODULE = "alysis_code.bots.cloud_desktop_worker"
PYTHON = "/opt/alysis/.venv/bin/python"
# A native disk image need not retain OCI ENV metadata or an editable install.
# Use only the qualified image's fixed source root, independent of ambient env.
BOOTSTRAP = (
    'import runpy,sys; sys.path.insert(0,"/opt/alysis/src"); '
    f'runpy.run_module("{MODULE}",run_name="__main__")'
)


def worker_argv(mode, session, *args):
    return [PYTHON, "-I", "-c", BOOTSTRAP, mode, session, *args]


class CloudDesktopFactory:
    def __init__(
        self,
        owner,
        provider,
        spec,
        ledger: Path,
        *,
        browser_enabled=False,
        preview_urls=(),
        persist_workspace=False,
        store=None,
        browser_state_key=None,
        max_computers=2,
        wait_for_capacity=False,
        computer_streams=None,
        retain_browser_while_waiting=False,
        billing=None,
    ):
        if max_computers is not None and (type(max_computers) is not int or max_computers < 1):
            raise ValueError("Computer capacity must be positive or explicit null.")
        if (
            spec.suspend_mode == "Memory"
            and getattr(provider, "supports_memory_sleep", False) is not True
        ):
            raise ValueError("This provider does not support memory-preserving sleep.")
        self.owner, self.provider, self.spec = owner, provider, spec
        self.billing = billing
        self.max_computers = max_computers
        self.wait_for_capacity = wait_for_capacity
        self.leases = WorkspaceStore(ledger)
        self.browser_enabled = browser_enabled
        self.preview_urls = tuple(preview_urls)
        self.persist_workspace = persist_workspace
        self.store = store
        self.computer_streams = computer_streams
        # Computers on this device: a task waiting for the person keeps its browser page until
        # idle expiry, so an approved click still finds it. Cloud computers close (billing).
        self.retain_browser_while_waiting = retain_browser_while_waiting
        self.lifecycle = lambda bot_id, phase: None
        from .browser_state import BrowserStates

        self.browser_states = (
            BrowserStates(store, browser_state_key) if browser_state_key is not None else None
        )
        self._instances = {}
        self._pending = []
        self._starting = set()
        self._operations = ComputerLocks()
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._waiters = []
        self._update_capacity()

    def _update_capacity(self):
        self._capacity_snapshot = {
            "active": len(self._instances)
            + sum(not c._cleaned for c in self._pending)
            + len(self._starting),
            "limit": self.max_computers,
            "queued": len(self._waiters),
            "cleanup_pending": sum(
                c._closed and not c._cleaned for c, _ in self._instances.values()
            )
            + sum(not c._cleaned for c in self._pending),
            "observed_at": time.time(),
        }

    def reconcile(self):
        if getattr(self.store, "shared", False):
            from .allocation_ledger import AllocationLedger

            AllocationLedger(self.store, self.owner).reconcile(
                self.provider, preserve_memory=self.spec.suspend_mode == "Memory"
            )

    def recovery_available(self, bot_id):
        if self.spec.suspend_mode != "Memory" or self.store is None:
            return False
        from .workspace_objects import WorkspaceObjects

        if self.store.objects is not None:
            return WorkspaceObjects(self.store).current(self.owner, bot_id) is not None
        return False

    def recover(self, bot_id, *, deleting=False):
        """Explicit recovery discards only this Bot's detached computer."""
        from .allocation_ledger import AllocationLedger
        from .compute import ComputerRef

        with self._operations.hold(bot_id):
            with self._condition:
                if bot_id in self._instances or bot_id in self._starting:
                    raise DesktopError("Close the computer's active session before recovering it.")
            if not deleting and not self.recovery_available(bot_id):
                raise DesktopError("There is no saved workspace recovery point yet.")
            ledger = AllocationLedger(self.store, self.owner)
            record = ledger.get(bot_id)
            if not record or record["state"] == "deleted":
                return
            if record.get("resume_kind") != "memory" or record["scope"] != self.provider.scope:
                raise DesktopError("Computer ownership requires operator recovery.")
            if record["state"] not in {"sleeping", "sleep_requested", "sleep_lost", "resuming"}:
                raise DesktopError("Wait for this computer to sleep before recovering it.")
            identity = ComputerIdentity(self.owner, bot_id, record["allocation_id"])
            ref = ComputerRef(record["provider"], record["scope"], record["resource_id"], identity)
            state = self.provider.status(ref).state
            if state == "running":
                self.provider.stop(ref)
                wait_for(self.provider, ref, "stopped", timeout=60)
                state = "stopped"
            if state == "stopped":
                self.provider.delete(ref)
                wait_for(self.provider, ref, "missing", timeout=60)
            elif state != "missing":
                raise DesktopError("Computer recovery is waiting for its state to settle.")
            ledger.save(bot_id, {**record, "state": "deleted"})

    def __call__(self, bot_id, _workspace, *, cancel):
        return self._acquire(bot_id, "desktop", cancel)

    def _acquire(self, bot_id, surface, cancel):
        # Only this Bot's surface attachment/startup is serialized. Reserve a
        # slot under the condition, then provision outside the shared lock.
        with self._operations.hold(bot_id):
            if self.persist_workspace and self.store is None:
                raise DesktopError("Durable cloud workspaces require the owning task store.")
            ticket = None
            try:
                while True:
                    with self._condition:
                        if cancel():
                            raise CooperativeCancellationError("Computer allocation cancelled.")
                        entry = self._instances.get(bot_id)
                        if entry is not None:
                            computer, surfaces = entry
                            if surface in surfaces or computer._closed:
                                raise WorkspaceError(
                                    "This bot's cloud computer is already in use or retiring."
                                )
                            surfaces.add(surface)
                            return CloudSurface(self, computer, surface)
                        if self.wait_for_capacity and ticket is None:
                            ticket = object()
                            self._waiters.append(ticket)
                        self._pending = [c for c in self._pending if not c._cleaned]
                        full = (
                            self.max_computers is not None
                            and len(self._instances) + len(self._pending) + len(self._starting)
                            >= self.max_computers
                        )
                        if full or (ticket is not None and self._waiters[0] is not ticket):
                            self._update_capacity()
                            if not self.wait_for_capacity:
                                raise DesktopCapacityBusy(
                                    "All available computers are busy. Try again after current work finishes."
                                )
                            self.lifecycle(bot_id, "queued")
                            self._condition.wait(timeout=0.25)
                            continue
                        self._starting.add(bot_id)
                        if ticket is not None:
                            self._waiters.remove(ticket)
                            ticket = None
                        self._update_capacity()
                        self._condition.notify_all()
                    self.lifecycle(bot_id, "starting")
                    try:
                        if self.billing:
                            self.billing.admit()
                        computer = CloudDesktop(self, bot_id, cancel=cancel)
                    except DesktopCapacityBusy:
                        with self._condition:
                            self._starting.discard(bot_id)
                            self._update_capacity()
                            self._condition.notify_all()
                            if self.wait_for_capacity:
                                self.lifecycle(bot_id, "queued")
                                self._condition.wait(timeout=0.5)
                                continue
                        self.lifecycle(bot_id, "failed")
                        raise
                    except BaseException as exc:
                        if not isinstance(exc, CooperativeCancellationError):
                            diagnostics.record("computer_start_failed", exc)
                        with self._condition:
                            if isinstance(exc, DesktopStartupCleanupError):
                                self._pending.append(exc.executor)
                            self._starting.discard(bot_id)
                            self._update_capacity()
                            self._condition.notify_all()
                        self.lifecycle(bot_id, "failed")
                        raise
                    with self._condition:
                        self._instances[bot_id] = (computer, {surface})
                        self._starting.discard(bot_id)
                        self._update_capacity()
                        self._condition.notify_all()
                    # Return the handle even if cancellation arrived just now:
                    # the caller owns cleanup rather than losing a created VM.
                    self.lifecycle(bot_id, "ready")
                    return CloudSurface(self, computer, surface)
            except CooperativeCancellationError:
                with self._condition:
                    if bot_id not in self._instances:
                        self.lifecycle(bot_id, "stopped")
                raise
            finally:
                with self._condition:
                    if ticket is not None:
                        self._waiters.remove(ticket)
                        self._condition.notify_all()
                    self._update_capacity()

    def capacity(self):
        # A snapshot never waits for provisioning, checkpointing or deletion.
        result = dict(self._capacity_snapshot)
        if getattr(self.store, "shared", False):
            from .allocation_ledger import AllocationLedger

            result["active"] = AllocationLedger(self.store, self.owner).capacity(
                self.provider.scope
            )
        return result

    def browser(self, bot_id, cancel):
        from .browser import BrowserStartupCleanupError
        from .cloud_browser import CloudBrowser

        if not self.browser_enabled:
            raise ValueError("Cloud browser is disabled.")
        try:
            surface = self._acquire(bot_id, "browser", cancel)
        except DesktopStartupCleanupError as exc:
            raise BrowserStartupCleanupError(exc.executor) from None
        try:
            browser = CloudBrowser(surface, self.preview_urls, cancel)
            if self.retain_browser_while_waiting:
                browser.retain_while_waiting = True
            return browser
        except BaseException:
            try:
                surface.close()
            except Exception:
                raise BrowserStartupCleanupError(surface) from None
            raise

    def _release(self, surface, *, completed=False, cancel=lambda: False, pause_session=None):
        with self._operations.hold(surface.owner):
            with self._condition:
                computer, surfaces = self._instances[surface.owner]
                if computer is not surface._computer:
                    raise DesktopError("Cloud computer ownership changed.")
            computer._completed = computer._completed and completed
            last = surfaces == {surface.surface}
            try:
                if not completed:
                    computer.close()
                if last:
                    self.lifecycle(surface.owner, "saving" if computer._completed else "stopped")
                    if pause_session is not None and self.spec.suspend_mode == "Memory":
                        computer.hibernate()
                    elif computer._completed:
                        computer.finish(cancel=cancel, pause_session=pause_session)
                    else:
                        computer.close()
                    self.lifecycle(surface.owner, "stopped")
                else:
                    surfaces.remove(surface.surface)
            except BaseException:
                self.lifecycle(surface.owner, "failed")
                raise
            finally:
                with self._condition:
                    if last and computer._cleaned:
                        del self._instances[surface.owner]
                        self._condition.notify_all()
                    self._update_capacity()


class CloudSurface:
    """One manager's lease on a task computer, with fail-closed shared teardown."""

    def __init__(self, factory, computer, surface):
        self._factory, self._computer, self.surface = factory, computer, surface
        self._released = False

    def __getattr__(self, name):
        return getattr(self._computer, name)

    def alive(self):
        return not self._released and self._computer.alive()

    def _request(self, *args, **kwargs):
        if self._released:
            raise DesktopError("Cloud surface has been released.")
        return self._computer._request(*args, **kwargs)

    def call(self, *args, **kwargs):
        if self._released:
            raise DesktopError("Cloud surface has been released.")
        return self._computer.call(*args, **kwargs)

    def capture(self):
        if self._released:
            raise DesktopError("Cloud surface has been released.")
        pixels = self._computer.capture()
        if self._released:
            raise DesktopError("Cloud surface was revoked.")
        return pixels

    def human_input(self, event):
        if self._released:
            raise DesktopError("Cloud surface has been released.")
        surface = (
            self._computer._resume_surface
            if self.surface == "desktop" and self._computer.memory_reopened
            else None
        )
        return self._computer.human_input(event, surface=surface or self.surface)

    def finish(self, *, cancel=lambda: False):
        self._release(completed=True, cancel=cancel)

    def suspend(self, session):
        self._release(completed=True, pause_session=session)

    def close(self):
        self._release(completed=False)

    def _release(self, *, completed, cancel=lambda: False, pause_session=None):
        if not self._released:
            try:
                self._factory._release(
                    self, completed=completed, cancel=cancel, pause_session=pause_session
                )
            finally:
                with self._factory._condition:
                    entry = self._factory._instances.get(self.owner)
                    if (
                        entry is None
                        or entry[0] is not self._computer
                        or self.surface not in entry[1]
                    ):
                        self._released = True


class CloudDesktop:
    def __init__(self, factory, bot_id, *, cancel):
        self.owner = bot_id
        self.provider, self.spec = factory.provider, factory.spec
        self.ref = None
        self._closed = False
        self._cleaned = False
        self._completed = True
        self.workspace_persistent = factory.persist_workspace
        self.workspace_reopened = False
        self.memory_reopened = False
        self._resume_surface = None
        self._store = factory.store
        self._lifecycle = factory.lifecycle
        self._objects = getattr(self._store, "objects", None)
        self._remote_snapshot = None
        self._account = factory.owner
        self._billing = factory.billing
        self._meter = None
        self._task_id = None
        self._browser_states = factory.browser_states
        self._browser_started = False
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._keepalive_thread = None
        self._view_lock = threading.Lock()
        self._stream_lock = threading.Lock()
        self._streams = (
            factory.computer_streams
            if factory.computer_streams and factory.computer_streams.enabled(bot_id)
            else None
        )
        self._native_stream = callable(getattr(factory.provider, "open_stream", None)) and (
            factory.computer_streams is None or factory.computer_streams.enabled(bot_id)
        )
        self._stream = None
        self._stream_attempted = False
        self._stream_connecting = False
        self._pending_stream = None
        self._input_key = None
        self._input_sequence = 0
        self._sleep_intent = False
        self._retained_memory = False
        self._lease = factory.leases.acquire(factory.owner, bot_id)
        self._shared_ledger = None
        self._max_computers = factory.max_computers
        if getattr(self._store, "shared", False):
            from .allocation_ledger import AllocationLedger

            self._shared_ledger = AllocationLedger(self._store, self._account)
        self._record = Path(self._lease.name) / "allocation.json"
        self._snapshot = Path(self._lease.name) / "workspace.tar"
        self._candidate = Path(self._lease.name) / "candidate.tar"
        self._candidate_record = Path(self._lease.name) / "candidate.json"
        # The paused task's own candidate, when that same task is now resuming.
        self._resume_source: Path | None = None
        self.identity = None
        try:
            previous = (
                self._shared_ledger.get(bot_id)
                if self._shared_ledger
                else json.loads(self._record.read_text())
                if self._record.exists()
                else None
            )
            if previous is not None:
                if previous["state"] != "deleted" and previous["scope"] != self.provider.scope:
                    raise DesktopError("Previous allocation belongs to another provider scope.")
                if previous.get("resume_kind") == "memory" and previous["state"] in {
                    "sleeping",
                    "sleep_requested",
                    "sleep_lost",
                    "resuming",
                }:
                    self._retained_memory = True
                    self._resume_memory(factory, previous, cancel)
                    return
                if previous["state"] != "deleted":
                    self.identity = ComputerIdentity(
                        factory.owner, bot_id, previous["allocation_id"]
                    )
                    self.ref = self.provider.find(self.identity)
                    # A crash while creating: in the cloud the VM may exist where find() cannot see
                    # it yet, so an operator decides; a local computer is its folder, so a missing
                    # one is simply gone and the owl starts fresh.
                    if (
                        self.ref is None
                        and previous["state"] == "creating"
                        and not getattr(self.provider, "find_is_complete", False)
                    ):
                        raise DesktopError(
                            "Earlier creation is uncertain; operator reconciliation required."
                        )
                    self._retire()
            if self.workspace_persistent:
                self._settle_checkpoint()
                if self._objects is not None:
                    from .workspace_objects import WorkspaceObjects

                    checkpoints = WorkspaceObjects(self._store)
                    self._remote_snapshot = checkpoints.current(
                        self._account, self.owner, task_id=opening_task()
                    )
                    if self._remote_snapshot is None and self._snapshot.exists():
                        with self._snapshot.open("rb") as stream:
                            self._remote_snapshot = checkpoints.import_baseline(
                                self._account,
                                self.owner,
                                iter(lambda: stream.read(65536), b""),
                                self._snapshot.stat().st_size,
                            )
            if cancel():
                raise CooperativeCancellationError("Computer allocation cancelled.")
            self.identity = ComputerIdentity(factory.owner, bot_id, uuid4().hex)
            self.ref = None
            self._save("creating")
            try:
                self.ref = self.provider.create(self.identity, self.spec)
            except ComputeCreationRefused:
                # A typed pre-dispatch refusal proves there is no resource to
                # reconcile. Unknown provider outcomes keep their intent below.
                self._save("deleted")
                raise
            self._save("allocated")
            wait_for(self.provider, self.ref, "running", timeout=60)
            if cancel():
                raise CooperativeCancellationError("Computer allocation cancelled.")
            if self.spec.suspend_mode == "Memory":
                # The qualified image supplies the OS/dependencies. Deliver this
                # release's credential-free bridge before its first start, so
                # the image's old idle-exit behavior cannot erase sleeping apps.
                for name in ("cloud_desktop_worker.py", "screen_channel.py"):
                    source = Path(__file__).with_name(name).read_bytes()
                    target = "/opt/alysis/src/alysis_code/bots/" + name
                    self.provider.upload(
                        self.ref, target, [source], size=len(source), cancel=cancel
                    )
                    checked = self.provider.run(
                        self.ref,
                        [
                            PYTHON,
                            "-I",
                            "-c",
                            "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path("
                            + repr(target)
                            + ").read_bytes()).hexdigest())",
                        ],
                        timeout=15,
                    )
                    if (
                        checked.exit_code != 0
                        or checked.stdout.strip() != hashlib.sha256(source).hexdigest()
                    ):
                        raise DesktopError("Computer bridge installation was not verified.")
            receipt = self.provider.run(
                self.ref,
                worker_argv(
                    "start_memory" if self.spec.suspend_mode == "Memory" else "start",
                    self.identity.allocation_id,
                ),
                timeout=30,
            )
            if receipt.exit_code != 0:
                raise DesktopError("Cloud computer runtime failed to start; no task action ran.")
            try:
                ready = json.loads(receipt.stdout)
            except (ValueError, TypeError):
                raise DesktopError("Cloud computer returned no valid readiness receipt.") from None
            expected = {
                "ready": True,
                "session": self.identity.allocation_id,
                "uid": 1000,
            }
            if self.spec.suspend_mode == "Memory":
                expected["memory_sleep"] = True
            if receipt.exit_code != 0 or ready != expected:
                raise DesktopError(
                    "Cloud computer did not return its unprivileged readiness receipt."
                )
            if self.workspace_persistent and (
                self._remote_snapshot or self._resume_source or self._snapshot.exists()
            ):
                self._restore(cancel)
            self._start_meter()
            self._keepalive_thread = threading.Thread(
                target=self._keepalive, name="alysis-computer-lease", daemon=True
            )
            self._keepalive_thread.start()
        except DesktopCapacityBusy:
            # Reservation was definitely refused before provider.create.
            self._closed = self._cleaned = True
            self._lease.cleanup()
            raise
        except BaseException:
            if self._retained_memory:
                # A failed reconnect must not erase the only copy of unsaved
                # application state. The durable record retains ownership.
                self._closed = True
                self._stop.set()
                self._lease.cleanup()
                self._cleaned = True
                raise
            try:
                self.close()
            except BaseException:
                raise DesktopStartupCleanupError(self) from None
            raise

    def _resume_memory(self, factory, previous, cancel):
        if self.spec.suspend_mode != "Memory":
            raise DesktopError("This connection cannot resume the saved computer memory.")
        self.identity = ComputerIdentity(factory.owner, self.owner, previous["allocation_id"])
        self._browser_started = previous.get("browser_started", False)
        self._task_id = previous.get("task_id")
        self.ref = ComputerRef(
            previous["provider"], previous["scope"], previous["resource_id"], self.identity
        )
        if self._shared_ledger:
            self._shared_ledger.activate(
                self.owner, self.identity.allocation_id, max_active=self._max_computers
            )
        else:
            self._save("resuming")
        self.ref = self.provider.find(self.identity)
        if self.ref is None:
            raise DesktopError("The sleeping computer is unavailable. Recover from saved work.")
        if self.ref.resource_id != previous.get("resource_id"):
            raise DesktopError("Sleeping computer identity does not match.")
        if cancel():
            raise CooperativeCancellationError("Computer resume cancelled.")
        state = self.provider.status(self.ref).state
        if state == "stopped":
            self.provider.start(self.ref)
            wait_for(self.provider, self.ref, "running", timeout=60)
        elif state != "running":
            raise DesktopError("The sleeping computer is still changing state.")
        if self._shared_ledger:
            self._shared_ledger.get(self.owner)  # Recheck the epoch after provider resume I/O.
        identity = uuid4().hex
        request = {
            "id": identity,
            "session": self.identity.allocation_id,
            "operation": "resume_memory",
            "arguments": {},
        }
        receipt = self.provider.run(
            self.ref,
            worker_argv(
                "request",
                self.identity.allocation_id,
                base64.b64encode(json.dumps(request).encode()).decode(),
            ),
            timeout=30,
        )
        reply = json.loads(receipt.stdout)
        restored = reply.get("result", {})
        surface = restored.get("surface")
        if (
            receipt.exit_code != 0
            or reply.get("id") != identity
            or reply.get("session") != self.identity.allocation_id
            or restored != {"ready": True, "uid": 1000, "memory_sleep": True, "surface": surface}
            or surface not in {"desktop", "browser"}
        ):
            raise DesktopError("Computer memory could not be reconnected. Recover from saved work.")
        self._input_sequence = 0
        self._resume_surface = surface
        self.memory_reopened = True
        self._save("allocated")
        self.workspace_reopened = True
        self._start_meter()
        self._keepalive_thread = threading.Thread(
            target=self._keepalive, name="alysis-computer-lease", daemon=True
        )
        self._keepalive_thread.start()
        self._retained_memory = False

    def _start_meter(self):
        if self._billing:
            self._meter = self._billing.start(
                self.owner,
                self._billing_exhausted,
                running=lambda: self.provider.status(self.ref).state == "running",
            )

    def _billing_exhausted(self):
        self._stop.set()  # Fence tool input before waiting for the provider lock.
        try:
            if self._store is not None and self._task_id is not None:
                self._store.event(
                    self._account,
                    self._task_id,
                    "computer_billing_paused",
                    {
                        "message": "The Cloud computer paused because its credits or billing connection could not be confirmed. Check your Cloud allowance before reopening it."
                    },
                )
        finally:
            self.hibernate()

    def _end_meter(self):
        if self._meter:
            self._meter.close()

    def hibernate(self):
        """Stop a quiescent viewer without closing its applications or deleting it."""
        self._end_meter()
        with self._lock:
            if self._cleaned:
                return
            self._sleep_intent = True
            self._closed = True
            self._stop.set()
            if self._stream:
                self._stream.close()
            if self._pending_stream:
                self._pending_stream.close()
            self._save("sleep_requested")
            state = self.provider.status(self.ref).state
            if state == "running":
                self.provider.stop(self.ref)
                wait_for(self.provider, self.ref, "stopped", timeout=60)
            elif state == "missing":
                self._save("sleep_lost")
                self._lease.cleanup()
                self._cleaned = True
                return
            elif state != "stopped":
                raise DesktopError("Computer sleep is unconfirmed; its memory was retained.")
            self._save("sleeping")
            self._lease.cleanup()
            self._cleaned = True

    def _keepalive(self):
        # A model call is active work even while no guest command is running.
        # Ping the guest dispatcher too: screen polling uses a separate socket
        # and cannot keep its command bridge alive. Both idle guards remain
        # enabled if this controller disappears.
        while not self._stop.wait(min(60, self.spec.idle_seconds / 3)):
            if not self._lock.acquire(blocking=False):
                continue
            try:
                if self._closed or self._stop.is_set():
                    return
                self._request("ping", {}, cancel=lambda: False)
            except Exception:
                if self._store is not None and self._task_id is not None:
                    self._store.event(
                        self._account,
                        self._task_id,
                        "computer_keepalive_failed",
                        {
                            "message": "Computer activity could not be confirmed. The idle safety guard remains enabled."
                        },
                    )
            finally:
                self._lock.release()

    def _save(self, state):
        value = {
            "scope": self.provider.scope,
            "allocation_id": self.identity.allocation_id,
            "state": state,
        }
        if self.spec.suspend_mode == "Memory":
            value.update(
                resume_kind="memory",
                image=self.spec.image,
                browser_started=self._browser_started,
                task_id=self._task_id,
            )
        if self.ref is not None:
            value.update(provider=self.ref.provider, resource_id=self.ref.resource_id)
        if self._shared_ledger is not None:
            if state == "creating":
                self._shared_ledger.reserve(self.owner, value, max_active=self._max_computers)
            else:
                self._shared_ledger.save(self.owner, value)
            return
        temporary = self._record.with_suffix(".tmp")
        with temporary.open("w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self._record)
        sync_directory(self._record.parent)

    def bind_task(self, task_id):
        if self._task_id not in (None, task_id):
            raise DesktopError("This cloud computer belongs to a different task.")
        if self.workspace_persistent or self._browser_states is not None:
            task = self._store.task(self._account, task_id)
            if task["bot_id"] != self.owner:
                raise DesktopError("Workspace task ownership does not match.")
        self._task_id = task_id
        if self.spec.suspend_mode == "Memory":
            self._save("allocated")

    def start_browser(self, previews, cancel):
        if self._browser_started:
            return
        if self._browser_states is None and self._store is not None:
            with self._store.connect() as db:
                self._store._bot(db, self._account, self.owner)
                saved = db.execute(
                    "SELECT 1 FROM browser_states WHERE bot_id=? LIMIT 1", (self.owner,)
                ).fetchone()
            if saved:
                raise DesktopError(
                    "This bot has saved browser storage; its stable encryption key is required. The previous state was retained."
                )
        if self._browser_states is not None:
            data = self._browser_states.current(
                self._account, self.owner, cancel=cancel, task_id=opening_task() or self._task_id
            )
            if data is not None:
                prepared = self._request(
                    "browser_restore_prepare",
                    {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()},
                    cancel=cancel,
                )
                path = CloudDesktopFiles._path(self, prepared["path"], "transfer-")
                chunks = (
                    data[index : index + FILE_CHUNK_BYTES]
                    for index in range(0, len(data), FILE_CHUNK_BYTES)
                )
                CloudDesktopFiles._upload(self, path, chunks, size=len(data), cancel=cancel)
                self._request(
                    "browser_restore", {"transfer_id": prepared["transfer_id"]}, cancel=cancel
                )
        self._request(
            "browser_start",
            {"preview_urls": list(previews), "persistent": self._browser_states is not None},
            cancel=cancel,
        )
        self._browser_started = True

    def _settle_checkpoint(self):
        # The task database is the authority. A candidate staged before final
        # cleanup cannot replace the prior checkpoint if Stop/failure won the
        # terminal task transaction, even after the last cancellation check.
        if not self._candidate_record.exists():
            self._candidate.unlink(missing_ok=True)
            return
        record = json.loads(self._candidate_record.read_text())
        task = self._store.task(self._account, record["task_id"])
        if task["bot_id"] != self.owner:
            raise DesktopError("Workspace checkpoint belongs to another bot.")
        resuming = (
            task["status"]
            not in {
                "completed",
                "failed",
                "cancelled",
                "interrupted",
            }
            and record["task_id"] == opening_task()
        )
        if resuming and not self._candidate.exists():
            return  # Its state is already the snapshot; the record still awaits its outcome.
        if task["status"] == "completed" or resuming:
            source = self._candidate if self._candidate.exists() else self._snapshot
            if source.stat().st_size != record["bytes"] or source.stat().st_size > MAX_FILE_BYTES:
                raise DesktopError("Workspace checkpoint integrity check failed.")
            with source.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != record["sha256"]:
                    raise DesktopError("Workspace checkpoint integrity check failed.")
            if resuming:
                # The same task continues after a pause (an approval, a question, background
                # work): it starts from its own latest state. The record stays, so the task's
                # final outcome still decides whether that state is kept.
                self._resume_source = source
                return
            if source == self._candidate:
                os.replace(self._candidate, self._snapshot)
        elif task["status"] in {"failed", "cancelled", "interrupted"}:
            self._candidate.unlink(missing_ok=True)
        else:
            raise DesktopError("Previous workspace task is still finalizing; try again later.")
        self._candidate_record.unlink()
        sync_directory(self._snapshot.parent)

    def _retire(self):
        self._end_meter()
        if self.ref is None:
            return
        state = self.provider.status(self.ref).state
        if state == "transitioning":
            raise DesktopError(
                "Cloud lifecycle transition is unconfirmed; cleanup remains pending."
            )
        if state == "running":
            self.provider.stop(self.ref)
            wait_for(self.provider, self.ref, "stopped", timeout=60)
            state = "stopped"
        if state == "stopped":
            self.provider.delete(self.ref)
            wait_for(self.provider, self.ref, "missing", timeout=60)
        elif state != "missing":
            raise DesktopError("Cloud shutdown is unconfirmed; cleanup remains pending.")
        self._save("deleted")

    def alive(self):
        # This is the local session fence, not an Azure management-plane probe.
        # Every screen/input RPC still checks the owned resource's running state
        # and exact guest receipt. Slow ARM status must not hold the input lock
        # or turn an otherwise fresh screen into an expired frame.
        return not self._closed and not self._stop.is_set() and self.ref is not None

    def input_ready(self):
        return self._stream is None or not self._stream.closed

    def prepare_input(self):
        with self._lock:
            if self._stream is not None and self._stream.closed:
                # A lost input may or may not have consumed its guest sequence.
                # The existing memory-resume fence clears held keys and rotates
                # the in-memory receiver. Never guess a sequence or replay it.
                if self.spec.suspend_mode != "Memory":
                    raise DesktopError("Reconnect this computer before taking control.")
                receipt = self._input_receiver(reset=True)
                if receipt.get("ready") is not True or receipt.get("memory_sleep") is not True:
                    raise DesktopError("Computer input reset was not confirmed.")
                self._input_key = None
                self._input_sequence = 0
                self._stream = None
        self._ensure_stream(reconnect=True, background=True)
        with self._lock:
            if self._stream is not None and not self._stream.closed and self._input_key is None:
                self._input_key = self._input_receiver().get("public_key")
                if not isinstance(self._input_key, str):
                    raise DesktopError("Private input receiver was not confirmed.")

    def _input_receiver(self, *, reset=False):
        identity = uuid4().hex
        request = {
            "id": identity,
            "session": self.identity.allocation_id,
            "operation": "resume_memory" if reset else "human_input_key",
            "arguments": {},
        }
        try:
            if not self._stream_owned():
                raise ValueError
            receipt = self.provider.run(
                self.ref,
                worker_argv(
                    "request",
                    self.identity.allocation_id,
                    base64.b64encode(json.dumps(request).encode()).decode(),
                ),
                timeout=15,
            )
            if receipt.exit_code != 0 or not self._stream_owned():
                raise ValueError
            return self._stream_reply(
                json.loads(receipt.stdout), identity, self.identity.allocation_id
            )
        except Exception:
            # Key reads/resets cannot perform an application action. An uncertain
            # receipt must not retire a VM holding unsaved applications.
            raise DesktopError("Computer input reset was not confirmed.") from None

    def _stream_owned(self):
        if not self.alive():
            return False
        if self._shared_ledger is not None:
            record = self._shared_ledger.get(self.owner)
            return record["allocation_id"] == self.identity.allocation_id
        return True

    def _ensure_stream(self, *, reconnect=False, background=False):
        if self._streams is None and not self._native_stream:
            return
        with self._stream_lock:
            if self._stream is not None and not self._stream.closed:
                return
            if self._stream_connecting or (self._stream_attempted and not reconnect):
                return
            if not self._stream_owned():
                raise DesktopError("Computer ownership ended.")
            self._stream_attempted = True
            self._stream_connecting = True
        if background:
            # Provider upload/start may take seconds. Keep the existing screen
            # and input RPC usable until a verified persistent link is ready.
            threading.Thread(target=self._connect_stream, daemon=True).start()
        else:
            self._connect_stream()

    def _connect_stream(self):
        link = None
        try:
            # The relay is independent of the desktop worker, so existing
            # memory-preserved browsers can adopt it without losing windows.
            source = Path(__file__).with_name("computer_stream_worker.py").read_bytes()
            target = "/opt/alysis/src/alysis_code/bots/computer_stream_worker.py"
            session = self.identity.allocation_id
            if not self._native_stream:
                link = self._streams.create(session, self._stream_owned)
                self._pending_stream = link
            if not self._stream_owned():
                raise DesktopError("Computer ownership ended.")
            self.provider.upload(self.ref, target, [source], size=len(source))
            bootstrap = (
                "import hashlib,pathlib,runpy,sys; "
                f"assert hashlib.sha256(pathlib.Path({target!r}).read_bytes()).hexdigest()=="
                f"{hashlib.sha256(source).hexdigest()!r}; "
                'sys.path.insert(0,"/opt/alysis/src"); '
                'runpy.run_module("alysis_code.bots.computer_stream_worker",run_name="__main__")'
            )

            def command(mode, *args):
                return [
                    PYTHON,
                    "-I",
                    "-c",
                    bootstrap,
                    mode,
                    session,
                    link.identity if link is not None else uuid4().hex,
                    *args,
                ]

            if self._native_stream:
                # Azure authenticates the persistent shell; the guest receives
                # only this trusted UID1000 relay, never an Azure credential.
                link = self.provider.open_stream(
                    self.ref, command("stdio"), valid=self._stream_owned
                )
                self._pending_stream = link
                with self._lock:
                    if not self._stream_owned():
                        raise DesktopError("Computer ownership ended.")
                    self._stream = link
                return
            receipt = self.provider.run(self.ref, command("start"), timeout=15)
            key = json.loads(receipt.stdout)
            if receipt.exit_code != 0 or set(key) != {"public_key"}:
                raise ValueError
            identity = uuid4().hex
            config = {
                "url": self._streams.url + "/" + link.identity,
                "token": link.token,
                "certificate": self._streams.certificate,
            }
            envelope = seal_input(
                key["public_key"],
                session,
                identity,
                1,
                {"surface": "desktop", "event": config},
            )
            payload = base64.b64encode(
                json.dumps({"id": identity, "envelope": envelope}).encode()
            ).decode()
            receipt = self.provider.run(self.ref, command("connect", payload), timeout=10)
            if receipt.exit_code != 0 or json.loads(receipt.stdout) != {"configured": True}:
                raise ValueError
            link.wait()
            with self._lock:
                if not self._stream_owned():
                    raise DesktopError("Computer ownership ended.")
                self._stream = link
        except Exception:
            if link is not None:
                link.close()
            # A failed initial setup can use the existing authenticated RPC.
            # An established link's lost inputs are never sent through it.
        finally:
            with self._stream_lock:
                self._pending_stream = None
                self._stream_connecting = False

    @staticmethod
    def _stream_reply(reply, identity, session):
        if (
            not isinstance(reply, dict)
            or reply.get("id") != identity
            or reply.get("session") != session
            or not (isinstance(reply.get("result"), dict) ^ isinstance(reply.get("error"), str))
        ):
            raise DesktopError("Computer stream has no matching complete receipt.")
        if "error" in reply:
            raise DesktopError("Computer stream operation rejected; inspect before retrying.")
        return reply["result"]

    def _request(self, operation, arguments, *, cancel):
        with self._lock:
            if self._closed or self._stop.is_set():
                raise DesktopError("Cloud computer is closed.")
            if self._shared_ledger is not None:
                self._shared_ledger.get(self.owner)  # Checks the live worker epoch before dispatch.
            identity = uuid4().hex
            if operation in {"browser_start", "browser_call"}:
                self._resume_surface = "browser"
            elif operation in {
                "observe",
                "capture",
                "run",
                "launch",
                "click",
                "type",
                "key",
                "scroll",
            }:
                self._resume_surface = "desktop"
            if operation == "human_input":
                if self._input_key is None:
                    raise DesktopError("Private input channel is unavailable.")
                sequence = self._input_sequence + 1
                arguments = seal_input(
                    self._input_key,
                    self.identity.allocation_id,
                    identity,
                    sequence,
                    arguments,
                )
                self._input_sequence = sequence
            request = {
                "id": identity,
                "session": self.identity.allocation_id,
                "operation": operation,
                "arguments": arguments,
            }
            payload = json.dumps(request).encode()
            if len(payload) > 48000:
                raise DesktopError("Cloud computer request exceeds its allowance.")
            argv = worker_argv(
                "request", self.identity.allocation_id, base64.b64encode(payload).decode()
            )
            # Cancellation can stop the entire VM while its RPC is outstanding.
            # Never retry the command, even if the transport response is lost.
            pool = ThreadPoolExecutor(max_workers=1)
            future = None
            matched = False
            try:
                if cancel() or self._stop.is_set():
                    raise CooperativeCancellationError("Cloud computer operation cancelled.")
                future = pool.submit(
                    self.provider.run,
                    self.ref,
                    argv,
                    timeout=50,
                )
                while not future.done():
                    if cancel() or self._stop.is_set():
                        raise CooperativeCancellationError("Cloud computer operation cancelled.")
                    time.sleep(0.05)
                receipt = future.result()
                if cancel() or self._stop.is_set():
                    raise CooperativeCancellationError("Cloud computer operation cancelled.")
                reply = json.loads(receipt.stdout)
                if (
                    receipt.exit_code != 0
                    or not isinstance(reply, dict)
                    or reply.get("id") != identity
                    or reply.get("session") != self.identity.allocation_id
                    or not (
                        isinstance(reply.get("result"), dict) ^ isinstance(reply.get("error"), str)
                    )
                ):
                    raise ComputerActionUncertain(
                        "Cloud action has no matching complete receipt; outcome is uncertain."
                    )
                matched = True
                if "error" in reply:
                    from . import diagnostics

                    # What the computer reported, for support: a class name and a flag only.
                    diagnostics.record(
                        "computer_operation_rejected",
                        operation=operation,
                        error_type=str(reply.get("error_type") or "")[:60],
                        error_class=str(reply.get("error_class") or "")[:60],
                        detail=str(reply.get("error_detail") or "")[:120],
                        fatal=bool(reply.get("fatal")),
                    )
                    if reply.get("fatal"):
                        self._lifecycle(self.owner, "failed")
                        self.close()
                    if operation.startswith("browser_"):
                        from ..ide.managed_browser import BrowserError
                        from .cloud_browser import ERRORS

                        error = ERRORS.get(reply.get("error_type"), BrowserError)
                        if error.__name__ == "BrowserPageUnreachableError":
                            # Chromium's own network error identifier, validated again there.
                            found = re.search(
                                r"net::ERR_[A-Z0-9_]{1,64}", str(reply.get("error_detail") or "")
                            )
                            raise error(found.group(0) if found else None)
                        raise error(
                            "Cloud browser rejected the operation; inspect before retrying."
                        )
                    raise DesktopError("Cloud operation rejected; inspect state before retrying.")
                return reply["result"]
            except CooperativeCancellationError:
                self.close()
                raise
            except ComputeStopped as stopped:
                # Refused before dispatch, so nothing ran; say why the computer stopped.
                self._lifecycle(self.owner, "failed")
                self.close()
                raise DesktopError(str(stopped)) from None
            except (ComputeError, ValueError, TypeError):
                self._lifecycle(self.owner, "failed")
                self.close()
                raise ComputerActionUncertain(
                    "Cloud operation interrupted; no success is confirmed."
                ) from None
            except DesktopError:
                # Validation rejection with a matched receipt is safe to inspect;
                # malformed or missing receipts retire the VM, never replay.
                if not matched:
                    self._lifecycle(self.owner, "failed")
                    self.close()
                raise
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

    def human_input(self, event, *, surface="desktop"):
        with self._lock:
            if self._input_key is None:
                receipt = self._request("human_input_key", {}, cancel=lambda: False)
                self._input_key = receipt.get("public_key")
                if not isinstance(self._input_key, str):
                    raise DesktopError(
                        "This worker does not support private input. Update its image."
                    )
            if self._stream is not None and not self._stream.closed:
                identity = uuid4().hex
                session = self.identity.allocation_id
                sequence = self._input_sequence + 1
                envelope = seal_input(
                    self._input_key,
                    session,
                    identity,
                    sequence,
                    {"surface": surface, "event": event},
                )
                self._input_sequence = sequence
                try:
                    reply = self._stream.request(
                        {
                            "id": identity,
                            "session": session,
                            "operation": "human_input",
                            "arguments": envelope,
                        },
                        timeout=12,
                    )
                    return self._stream_reply(reply, identity, session)
                except Exception:
                    self._stream.close()
                    # Keep applications and the paused task intact. The next
                    # explicit take may establish a new channel; never replay.
                    raise ComputerActionUncertain(
                        "Computer input was not confirmed; inspect before taking control again."
                    ) from None
            if self._stream is not None and event.get("kind") != "release":
                raise DesktopError("Computer input disconnected. Take control again.")
            return self._request(
                "human_input", {"surface": surface, "event": event}, cancel=lambda: False
            )

    def call(self, owner, operation, arguments, *, cancel=lambda: False):
        if owner != self.owner:
            raise DesktopError("This computer belongs to another bot.")
        if operation == "check_file":
            from .artifact_validation import await_check

            return await_check(
                lambda operation, **args: self._request(
                    operation, {**arguments, **args}, cancel=cancel
                )
            )
        if operation not in {
            "observe",
            "run",
            "launch",
            "status",
            "stop_process",
            "click",
            "type",
            "key",
            "scroll",
        }:
            raise ValueError("Unsupported remote computer action.")
        return self._request(operation, arguments, cancel=cancel)

    def capture(self):
        if not self._view_lock.acquire(blocking=False):
            raise BlockingIOError("Screen capture is already running.")
        try:
            if not self.alive():
                raise DesktopError("Cloud computer is closed.")
            self._ensure_stream(background=True)
            identity = uuid4().hex
            session = self.identity.allocation_id
            request = {"id": identity, "session": session}
            if self._stream is not None and not self._stream.closed:
                try:
                    reply = self._stream.request(
                        {
                            **request,
                            "operation": "screen",
                            "arguments": {},
                        },
                        timeout=4,
                    )
                    if (
                        reply.get("id") == identity
                        and reply.get("session") == session
                        and isinstance(reply.get("error"), str)
                        and "result" not in reply
                    ):
                        raise BlockingIOError("Computer screen is not ready.")
                    result = self._stream_reply(reply, identity, session)
                    if not self.alive():
                        raise DesktopError("Cloud screen was revoked.")
                    if self._native_stream:
                        if result.get("mime") != "image/jpeg":
                            raise DesktopError("Invalid computer stream image format.")
                        return base64.b64decode(result["image"], validate=True)
                    return base64.b64decode(result["png"], validate=True)
                except BlockingIOError:
                    raise
                except Exception:
                    self._stream.close()
                    raise DesktopError("Computer screen stream disconnected.") from None
            receipt = self.provider.run(
                self.ref,
                worker_argv(
                    "screen", session, base64.b64encode(json.dumps(request).encode()).decode()
                ),
                timeout=4,
            )
            reply = json.loads(receipt.stdout)
            if (
                receipt.exit_code != 0
                or not self.alive()
                or reply.get("id") != identity
                or reply.get("session") != session
            ):
                raise DesktopError("No current screen receipt.")
            return base64.b64decode(reply["result"]["png"], validate=True)
        finally:
            self._view_lock.release()

    @property
    def workspace_lifetime(self):
        return (
            "Workspace files are checkpointed after successful task completion or a verified "
            "human-control idle pause, and restored into a fresh computer. Cancelled, failed or "
            "interrupted tasks keep the last committed checkpoint; later scratch writes are not "
            "retained. Unsaved windows and running processes are not preserved. Up to 50 MiB per workspace "
            "archive, regular files and directories only. Export deliverables to the library."
        )

    def _restore(self, cancel):
        if self._remote_snapshot is not None:
            ref = self._remote_snapshot
            prepared = self._request(
                "workspace_restore_prepare", {"size": ref.size, "sha256": ref.sha256}, cancel=cancel
            )
            path = CloudDesktopFiles._path(self, prepared["path"], "transfer-")
            with closing(self._objects.read(ref, cancel=cancel)) as chunks:
                CloudDesktopFiles._upload(self, path, chunks, size=ref.size, cancel=cancel)
            self._request(
                "workspace_restore", {"transfer_id": prepared["transfer_id"]}, cancel=cancel
            )
            self.workspace_reopened = True
            return
        snapshot = self._resume_source or self._snapshot
        size = snapshot.stat().st_size
        digest = hashlib.sha256()
        with snapshot.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        prepared = self._request(
            "workspace_restore_prepare", {"size": size, "sha256": digest.hexdigest()}, cancel=cancel
        )
        path = CloudDesktopFiles._path(self, prepared["path"], "transfer-")
        with snapshot.open("rb") as stream:
            self.provider.upload(
                self.ref, path, iter(lambda: stream.read(65536), b""), size=size, cancel=cancel
            )
        self._request("workspace_restore", {"transfer_id": prepared["transfer_id"]}, cancel=cancel)
        self.workspace_reopened = True

    def checkpoint(self, session):
        """Commit saved human work before the paused task can execute again.

        This intentionally leaves the guest, applications and in-memory state
        alive. Archive validation rejects writers changing files during capture.
        Stop/ownership loss is rechecked before publishing the durable pointer.
        """
        from .workspace_objects import WorkspaceObjects, check_checkpoint

        if not self.workspace_persistent or self._objects is None:
            raise DesktopError("Durable workspace storage is required before returning control.")

        def cancelled():
            return self._stop.is_set() or (
                self._task_id is not None
                and self._store.task(self._account, self._task_id)["status"] != "waiting"
            )

        temporary = self._snapshot.with_suffix(".handback")
        with self._lock:
            try:
                with self._store.connect() as db:
                    check_checkpoint(
                        self._store, db, self._account, self.owner, self._task_id, session
                    )
                receipt = self._request("workspace_checkpoint", {}, cancel=cancelled)
                path = CloudDesktopFiles._path(self, receipt["path"], "export-")
                size = receipt["bytes"]
                if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
                    raise DesktopError("Workspace checkpoint exceeds its allowance.")
                digest, total = hashlib.sha256(), 0
                with closing(
                    self.provider.download(self.ref, path, size=size, cancel=cancelled)
                ) as chunks:
                    with temporary.open("wb") as stream:
                        for chunk in chunks:
                            total += len(chunk)
                            if total > size:
                                raise DesktopError("Workspace checkpoint exceeds its receipt.")
                            digest.update(chunk)
                            stream.write(chunk)
                        if total != size or digest.hexdigest() != receipt["sha256"]:
                            raise DesktopError("Workspace checkpoint integrity check failed.")
                        stream.flush()
                        os.fsync(stream.fileno())
                with temporary.open("rb") as stream:
                    WorkspaceObjects(self._store).stage_pause(
                        self._account,
                        self.owner,
                        self._task_id,
                        session,
                        iter(lambda: stream.read(65536), b""),
                        size,
                        cancel=cancelled,
                    )
            finally:
                temporary.unlink(missing_ok=True)

    def finish(self, *, cancel=lambda: False, pause_session=None):
        # A quiescent human pause can commit a checkpoint without completing the
        # task. Failure/Stop retains the last committed point in either case.
        temporary = self._snapshot.with_suffix(".pending")
        try:
            if pause_session is not None:
                from .workspace_objects import check_checkpoint

                if not self.workspace_persistent:
                    raise DesktopError("Idle checkpoints require durable workspace storage.")
                with self._store.connect() as db:
                    check_checkpoint(
                        self._store, db, self._account, self.owner, self._task_id, pause_session
                    )
            browser_data = None
            if self._browser_states is not None and self._browser_started and not self._closed:
                if self._task_id is None and pause_session is None:
                    raise DesktopError("Browser checkpoint requires an owning task.")
                receipt = self._request("browser_snapshot", {}, cancel=cancel)
                path = CloudDesktopFiles._path(self, receipt["path"], "export-")
                size = receipt["bytes"]
                if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
                    raise DesktopError("Browser snapshot exceeds its allowance.")
                with closing(
                    self.provider.download(self.ref, path, size=size, cancel=cancel)
                ) as chunks:
                    parts, total = [], 0
                    for chunk in chunks:
                        total += len(chunk)
                        if total > size:
                            raise DesktopError("Browser snapshot exceeds its receipt.")
                        parts.append(chunk)
                    browser_data = b"".join(parts)
                if (
                    len(browser_data) != size
                    or hashlib.sha256(browser_data).hexdigest() != receipt["sha256"]
                ):
                    raise DesktopError("Browser snapshot integrity check failed.")
            if self.workspace_persistent and not self._closed:
                if self._task_id is None and pause_session is None:
                    raise DesktopError("Workspace checkpoint requires an owning task.")
                receipt = self._request("workspace_snapshot", {}, cancel=cancel)
                path = CloudDesktopFiles._path(self, receipt["path"], "export-")
                size = receipt["bytes"]
                if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
                    raise DesktopError("Workspace snapshot exceeds its allowance.")
                digest, total = hashlib.sha256(), 0
                with closing(
                    self.provider.download(self.ref, path, size=size, cancel=cancel)
                ) as chunks:
                    with temporary.open("wb") as stream:
                        for chunk in chunks:
                            total += len(chunk)
                            if total > size:
                                raise DesktopError("Workspace snapshot exceeds its receipt.")
                            digest.update(chunk)
                            stream.write(chunk)
                        if total != size or digest.hexdigest() != receipt["sha256"]:
                            raise DesktopError("Workspace snapshot integrity check failed.")
                        stream.flush()
                        os.fsync(stream.fileno())
                # Delete the disposable machine BEFORE publishing a replacement.
                # A cleanup failure must not claim successful durable completion.
                self._retire()
                if browser_data is not None and pause_session is None:
                    self._browser_states.stage(
                        self._account, self.owner, self._task_id, browser_data, cancel=cancel
                    )
                if cancel():
                    raise CooperativeCancellationError("Workspace checkpoint cancelled.")
                if self._objects is not None:
                    from .workspace_objects import WorkspaceObjects

                    with temporary.open("rb") as stream:
                        checkpoints = WorkspaceObjects(self._store)
                        if pause_session is not None:
                            checkpoints.stage_pause(
                                self._account,
                                self.owner,
                                self._task_id,
                                pause_session,
                                iter(lambda: stream.read(65536), b""),
                                size,
                                browser_states=self._browser_states,
                                browser_data=browser_data,
                                cancel=cancel,
                            )
                            return
                        checkpoints.stage(
                            self._account,
                            self.owner,
                            self._task_id,
                            iter(lambda: stream.read(65536), b""),
                            size,
                            cancel=cancel,
                        )
                    return
                if pause_session is not None:
                    from .workspace_objects import check_checkpoint

                    with self._store.connect(write=True) as db:
                        check_checkpoint(
                            self._store, db, self._account, self.owner, self._task_id, pause_session
                        )
                        os.replace(temporary, self._snapshot)
                        sync_directory(self._snapshot.parent)
                    return
                os.replace(temporary, self._candidate)
                candidate_record = self._candidate_record.with_suffix(".pending")
                with candidate_record.open("w") as stream:
                    json.dump(
                        {"task_id": self._task_id, "bytes": size, "sha256": digest.hexdigest()},
                        stream,
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(candidate_record, self._candidate_record)
                sync_directory(self._snapshot.parent)
            elif browser_data is not None:
                self._retire()
                self._browser_states.stage(
                    self._account, self.owner, self._task_id, browser_data, cancel=cancel
                )
        finally:
            temporary.unlink(missing_ok=True)
            self.close()

    def close(self):
        if self._sleep_intent and not self._cleaned:
            return self.hibernate()
        self._stop.set()
        if self._stream:
            self._stream.close()
        if self._pending_stream:
            self._pending_stream.close()
        with self._lock:
            if self._cleaned:
                return
            self._closed = True
            if self.identity is not None and self.ref is None:
                self.ref = self.provider.find(self.identity)
                if self.ref is None:
                    # Shared allocation intent lives only in PostgreSQL. Local
                    # scratch cannot confirm cleanup or release its retry handle.
                    record = (
                        self._shared_ledger.get(self.owner)
                        if self._shared_ledger is not None
                        else json.loads(self._record.read_text())
                        if self._record.exists()
                        else None
                    )
                    if record is not None and record["state"] == "creating":
                        raise DesktopError(
                            "Creation outcome unknown; retain allocation intent for reconciliation."
                        )
            self._retire()
            self._lease.cleanup()
            self._cleaned = True


class CloudDesktopFiles:
    @staticmethod
    def _upload(executor, path, chunks, *, size, cancel):
        # Compute adapters sanitize source exceptions along with transport
        # errors. Retain only the typed storage failure, never its private text.
        storage_failed = False

        def source():
            nonlocal storage_failed
            try:
                yield from chunks
            except ObjectStorageError:
                storage_failed = True
                raise

        with closing(source()) as body:
            try:
                executor.provider.upload(executor.ref, path, body, size=size, cancel=cancel)
            except ComputeError:
                if storage_failed:
                    raise ObjectStorageError("Stored file could not be read or verified.") from None
                raise

    @staticmethod
    def _path(executor, path, prefix):
        root = f"/tmp/alysis-cloud-{executor.identity.allocation_id}/"
        suffix = path.removeprefix(root + prefix)
        from uuid import UUID

        if path != root + prefix + suffix or UUID(suffix).hex != suffix:
            raise DesktopError("Cloud transfer receipt has an invalid staging path.")
        return path

    def import_file(self, executor, store, owner, task, source, token):
        token.throw_if_cancelled()
        prepared = executor._request(
            "import_prepare",
            {"name": source["name"], "size": source["size_bytes"], "sha256": source["sha256"]},
            cancel=lambda: token.is_cancelled,
        )
        path = self._path(executor, prepared["path"], "transfer-")
        try:
            with closing(store.file_chunks(owner, task["bot_id"], source["id"])) as chunks:
                self._upload(
                    executor,
                    path,
                    chunks,
                    size=source["size_bytes"],
                    cancel=lambda: token.is_cancelled,
                )
        except ComputeError:
            token.throw_if_cancelled()
            raise DesktopError("Cloud upload was not confirmed; inspect before retrying.") from None
        token.throw_if_cancelled()
        store.file(owner, task["bot_id"], source["id"])
        if not store.bot(owner, task["bot_id"])["files_enabled"]:
            raise DesktopError("File transfer permission was removed.")
        result = executor._request(
            "import_commit",
            {"transfer_id": prepared["transfer_id"]},
            cancel=lambda: token.is_cancelled,
        )
        if any(
            result.get(k) != v
            for k, v in {
                "name": source["name"],
                "bytes": source["size_bytes"],
                "sha256": source["sha256"],
                "written": True,
            }.items()
        ):
            raise DesktopError("Cloud import did not return the expected file receipt.")
        return {**result, "source_file_id": source["id"]}

    def export_file(
        self,
        executor,
        store,
        owner,
        task,
        name,
        token,
        *,
        previous_file_id=None,
        expected_sha256=None,
    ):
        saved = executor._request("export", {"name": name}, cancel=lambda: token.is_cancelled)
        path = self._path(executor, saved["path"], "export-")
        if (
            saved.get("name") != name
            or type(saved.get("bytes")) is not int
            or not 0 <= saved["bytes"] <= MAX_FILE_BYTES
        ):
            raise DesktopError("Cloud export receipt is invalid.")
        if expected_sha256 is not None and saved.get("sha256") != expected_sha256:
            raise DesktopError("The file changed after validation; check it again before saving.")

        def chunks():
            digest, count = hashlib.sha256(), 0
            try:
                with closing(
                    executor.provider.download(
                        executor.ref, path, size=saved["bytes"], cancel=lambda: token.is_cancelled
                    )
                ) as download:
                    for chunk in download:
                        token.throw_if_cancelled()
                        digest.update(chunk)
                        count += len(chunk)
                        yield chunk
            except ComputeError:
                token.throw_if_cancelled()
                raise DesktopError("Cloud download was incomplete; no file was saved.") from None
            if count != saved["bytes"] or digest.hexdigest() != saved.get("sha256"):
                raise DesktopError(
                    "Cloud file failed its independent integrity check; nothing was saved."
                )

        if saved["bytes"] <= 100000:
            data = b"".join(chunks())
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                content = None
            if content is not None and "\0" not in content:
                return store.save_file(
                    owner,
                    task["bot_id"],
                    FileInput(name=name, content=content, previous_file_id=previous_file_id),
                    source_task_id=task["id"],
                )
            return store.save_stream(
                owner,
                task["bot_id"],
                name,
                iter([data]),
                len(data),
                source_task_id=task["id"],
                previous_file_id=previous_file_id,
            )
        with closing(chunks()) as download:
            return store.save_stream(
                owner,
                task["bot_id"],
                name,
                download,
                saved["bytes"],
                source_task_id=task["id"],
                previous_file_id=previous_file_id,
            )
