"""App-owned loopback service. Bootstrap authority travels only over stdin.

The desktop keeps the service credential in its OS-backed vault. The engine
resolves model credentials itself; neither the renderer nor a Bot receives them.
Closing the parent pipe shuts down the scheduler and its owned computers.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import socket
import sys
from pathlib import Path

from ..config import load_config, resolve_profile_api_key
from ..profiles import (
    get_active_profile,
    list_profiles,
    resolve_effective_base_url,
    set_active_profile,
)


def profiles() -> dict:
    from .app import MODEL_PROTOCOLS

    cfg = load_config()
    active = get_active_profile(cfg)
    return {
        "protocol": 1,
        "active": active.name,
        "profiles": [
            {
                "name": item.name,
                "model": item.default_model or cfg.model,
                "available": not item.auth_provider
                and item.protocol in MODEL_PROTOCOLS
                and bool(resolve_profile_api_key(cfg, item.name).key),
            }
            for item in list_profiles(cfg)
        ],
    }


def settings_from_bootstrap(value: object):
    from .app import Settings

    if not isinstance(value, dict) or set(value) - {
        "directory",
        "token",
        "profile",
        "desktop",
        "computer_bundle",
        "resources",
    }:
        raise ValueError("Invalid service bootstrap.")
    directory = Path(value["directory"])
    if not directory.is_absolute() or directory.is_symlink():
        raise ValueError("An owned absolute data directory is required.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    cfg = load_config()
    if value.get("profile"):
        set_active_profile(cfg, value["profile"])
    profile = get_active_profile(cfg)
    if profile.auth_provider:
        raise ValueError("Choose a saved API profile for local Bots.")
    key = resolve_profile_api_key(cfg, profile.name).key
    if not key:
        raise ValueError("Sign in or save a model connection in Alysis first.")
    desktop_enabled = value.get("desktop", False)
    if type(desktop_enabled) is not bool:
        raise ValueError("Private computer setup must be a boolean.")
    if desktop_enabled:
        from .desktop_linux import available

        desktop_enabled = available()
    return Settings(
        database=directory / "bots.sqlite3",
        token=value["token"],
        api_key=key,
        base_url=resolve_effective_base_url(cfg=cfg, profile=profile),
        model=profile.default_model or cfg.model,
        protocol=profile.protocol,
        provider_key=profile.name,
        reasoning_effort=profile.reasoning_effort,
        owner="local",
        desktop_enabled=desktop_enabled,
        extra_headers=profile.extra_headers,
        service_mode="local",
    )


def start_failure_code(error: BaseException) -> str:
    """Why the service could not start, as a code the app shows in the person's language.

    Only a fixed code crosses to the app: exception text can carry paths or settings, and the
    app has to explain the failure in its own words and language anyway.
    """
    text = str(error).lower()
    disk_full = (
        (
            isinstance(error, OSError)
            and (error.errno == errno.ENOSPC or getattr(error, "winerror", None) in (39, 112))
        )
        or "disk is full" in text
        or "no space left" in text
    )
    if disk_full:
        return "disk_full"
    if isinstance(error, OSError):
        return "storage"
    if isinstance(error, ValueError):
        return "settings"
    return "internal"


async def serve() -> None:
    """Run the local service; before it is ready, a failure is announced as one JSON line.

    The app reads stdout for exactly one line: ``ready`` with the address, or ``error`` with a
    code. Without the error line the app could only say "could not start", because the reason
    went to stderr, which it never shows.
    """
    announced = False

    def announce(payload: dict) -> None:
        nonlocal announced
        print(json.dumps(payload), flush=True)
        announced = True

    try:
        await _serve(announce)
    except Exception as error:
        if not announced:
            from . import diagnostics

            code = start_failure_code(error)
            diagnostics.record("service_start_failed", error, reason=code)
            try:
                announce({"type": "error", "protocol": 1, "code": code})
            except OSError:
                pass  # the app went away; the exit status still reports the failure
        raise


async def _serve(announce) -> None:
    import uvicorn

    from .app import create_app

    line = await asyncio.to_thread(sys.stdin.buffer.readline, 16385)
    if len(line) > 16384 or not line.endswith(b"\n"):
        raise ValueError("Invalid service bootstrap.")
    value = json.loads(line)
    from . import diagnostics

    if isinstance(value.get("directory"), str) and Path(value["directory"]).is_absolute():
        diagnostics.configure(Path(value["directory"]))
    settings = settings_from_bootstrap(value)
    from .object_storage import LocalObjectStorage
    from .store import Store

    store = Store(
        settings.database, objects=LocalObjectStorage(settings.database.parent / "objects")
    )
    provider = None
    factory = None
    if value.get("computer_bundle"):
        from .cloud_desktop import CloudDesktopFactory
        from .compute import ComputerSpec
        from .local_resources import CPU_PER_COMPUTER, MEMORY_PER_COMPUTER, LocalResources

        if sys.platform == "win32":
            from .local_hcs import WindowsVmComputers as LocalComputers
        else:
            from .local_vm import LocalVmComputers as LocalComputers

        bundle = Path(value["computer_bundle"])
        if not bundle.is_absolute():
            raise ValueError("A trusted absolute computer bundle path is required.")
        resources = LocalResources.parse(value.get("resources"))
        provider = LocalComputers(bundle, settings.database.parent / "computers")
        _record_start(bundle, resources, settings.database.parent)
        factory = CloudDesktopFactory(
            settings.owner,
            provider,
            ComputerSpec(
                "bundled-linux", cpu=CPU_PER_COMPUTER, memory_mib=MEMORY_PER_COMPUTER, internet=True
            ),
            settings.database.parent / "computer-leases",
            browser_enabled=True,
            persist_workspace=True,
            store=store,
            max_computers=resources.max_computers,
            wait_for_capacity=True,
            retain_browser_while_waiting=True,
            browser_state_key=hashlib.sha256(
                b"alysis-local-browser-v1\0" + settings.token.encode()
            ).digest(),
        )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    app = create_app(settings, task_store=store, cloud_computers=factory)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        while not server.started and not task.done():
            await asyncio.sleep(0.025)
        if task.done():
            await task
            raise RuntimeError("Local service did not start.")
        announce(
            {
                "type": "ready",
                "url": f"http://127.0.0.1:{listener.getsockname()[1]}",
                "protocol": 1,
                "private_computer_available": factory is not None or settings.desktop_enabled,
            }
        )
        # A child cannot outlive its owning desktop and silently keep executing.
        await asyncio.to_thread(sys.stdin.buffer.read, 1)
    finally:
        server.should_exit = True
        await task
        listener.close()
        if provider is not None:
            provider.close()


def _record_start(bundle, resources, directory) -> None:
    """Machine facts support needs first: system, versions, size, free disk, computer limit."""
    import platform
    import shutil

    from .. import __version__
    from . import diagnostics
    from .local_resources import host_resources

    try:
        cores, memory_mib = host_resources()
        free = shutil.disk_usage(directory).free
    except Exception:
        cores, memory_mib, free = 0, 0, 0
    diagnostics.record(
        "service_started",
        system=f"{platform.system()} {platform.release()} {platform.version()}"[:120],
        machine=platform.machine(),
        python=platform.python_version(),
        engine=__version__,
        image=bundle.name[:120],
        cores=cores,
        memory_gb=round(memory_mib / 1024, 1),
        free_disk_gb=round(free / 1024**3, 1),
        max_computers=resources.max_computers,
    )


def main() -> None:
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(serve())
    except (Exception, KeyboardInterrupt) as error:
        from . import diagnostics

        diagnostics.record("service_failed", error)
        # Exceptions can contain provider arguments or credentials. Only emit a
        # stable, credential-free failure; diagnostics remain in the local API.
        print(
            json.dumps(
                {
                    "type": "error",
                    "error": "Local Bots could not start. Check the selected model connection and computer setup.",
                }
            ),
            flush=True,
        )
        raise SystemExit(1) from None
