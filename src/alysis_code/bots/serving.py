"""Fail a hosted controller honestly when its bot workers lose ownership.

The caller owns process supervision and credentials. This wrapper only requests
normal server shutdown; it never restarts workers or replays uncertain tasks.
"""

import asyncio
from contextlib import suppress


async def serve(server, runtime, *, sockets=None):
    worker_failed = False

    async def watch_workers():
        nonlocal worker_failed
        while not server.should_exit:
            # Lifespan starts the workers before Uvicorn accepts requests. Do
            # not confuse startup or a requested shutdown with lost workers.
            if server.started and not runtime.is_running:
                worker_failed = True
                server.should_exit = True
                return
            await asyncio.sleep(1)

    monitor = asyncio.create_task(watch_workers())
    try:
        await server.serve(sockets=sockets)
    finally:
        monitor.cancel()
        with suppress(asyncio.CancelledError):
            await monitor
    if worker_failed:
        raise RuntimeError("Bot workers stopped; controller restart is required.")
    if not server.started or server.lifespan.shutdown_failed:
        raise RuntimeError("Bot controller did not complete its service lifecycle.")
