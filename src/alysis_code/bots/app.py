from __future__ import annotations

import asyncio
import base64
import hmac
import os
import platform
import re
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from ..cancellation import CooperativeCancellationError
from ..cloud_connections import (
    BoundConnections,
    ConnectionStore,
    ConnectionUnavailable,
    GitHubReader,
    GrantInput,
)
from ..llm.anthropic_messages import AnthropicMessagesClient
from ..llm.base import ChatClient
from ..llm.factory import resolve_model_protocol
from ..llm.openai_compat import OpenAICompatClient
from ..llm.openai_responses import OpenAIResponsesClient
from ..llm.protocols import ANTHROPIC_MESSAGES_PROTOCOL, OPENAI_RESPONSES_PROTOCOL
from ..llm.provider_limits import ProviderRetrySettings
from .action_review import ApprovalDecision as ActionReviewDecision
from .action_review import ReviewSettings, RuleInput, RuleRevision, RuleUpdate
from .browser import BrowserComputers
from .browser_images import declared_image_support
from .clarifications import QuestionDecision
from .computer import ComputerViews
from .computer_control import ComputerRecovery, ControlRequest
from .desktop import DesktopComputers
from .desktop_connectors import CatalogInput, DesktopConnectors, ResultInput
from .files import MAX_ENCODED_BYTES, BinaryFileInput, FileQuery, decode_file
from .models import (
    BotInput,
    ChatInput,
    FileInput,
    HistoryQuery,
    MemoryDecision,
    MemoryInput,
    NoteConsolidation,
    NoteInput,
    NoteSource,
    ReadChatInput,
    ReplyReviewSettings,
    RevisionQuery,
    TaskInput,
)
from .proposals import ActionDecision
from .runtime import BotRuntime, RuntimeLimits
from .scheduling import ScheduleDecision, ScheduleInput, ScheduleRunsQuery
from .store import Conflict, LimitReached, NotFound, Store
from .transfers import FileTransfers, UploadChunk, UploadInput
from .triggers import TriggerDecision, TriggerInput

MODEL_PROTOCOLS = frozenset(("openai_compat", "anthropic_messages"))


@dataclass(frozen=True)
class Settings:
    database: Path
    token: str = field(repr=False)
    api_key: str = field(repr=False)
    base_url: str
    model: str
    owner: str = "local-development"
    max_calls: int | None = None
    output_tokens: int | None = None
    protocol: str = "openai_compat"
    provider_key: str | None = None
    reasoning_effort: str | None = None
    coding_token: str | None = field(default=None, repr=False)
    managed_workspaces: bool = False
    browser_enabled: bool = False
    desktop_enabled: bool = False
    coding_calls: int | None = None
    extra_headers: dict[str, str] = field(default_factory=dict, repr=False)
    service_mode: str = "development"

    def __post_init__(self) -> None:
        if self.service_mode not in ("local", "cloud", "development"):
            raise ValueError("Unknown Bots service mode.")
        if self.coding_calls is not None and (
            type(self.coding_calls) is not int or self.coding_calls < 1
        ):
            raise ValueError("Coding execution allowance must be positive or null (autonomous).")
        if type(self.browser_enabled) is not bool:
            raise ValueError("Browser execution must be explicitly enabled with a boolean.")
        if type(self.desktop_enabled) is not bool:
            raise ValueError("Desktop execution must be explicitly enabled with a boolean.")
        if self.managed_workspaces and not self.coding_token:
            raise ValueError("Managed workspaces require a separate worker capability.")
        if self.coding_token is not None and (
            len(self.coding_token) < 32 or self.coding_token == self.token
        ):
            raise ValueError("The coding worker requires its own random token.")
        if len(self.token) < 32 or not self.api_key.strip():
            raise ValueError(
                "Set a random Bots API token (at least 32 characters) and a model API key."
            )
        url = urlsplit(self.base_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "The model endpoint must be an HTTPS URL without credentials, query, or fragment."
            )
        if (
            not self.model.strip()
            or (
                self.max_calls is not None
                and (type(self.max_calls) is not int or self.max_calls < 0)
            )
            or (
                self.output_tokens is not None
                and (type(self.output_tokens) is not int or self.output_tokens < 1)
            )
        ):
            raise ValueError("Invalid model or explicit execution limits.")
        if self.protocol not in MODEL_PROTOCOLS:
            raise ValueError(
                "This prototype supports OpenAI-compatible and Anthropic message protocols."
            )

    @classmethod
    def from_environment(cls) -> Settings:
        return cls(
            database=Path(
                os.environ.get("ALYSIS_BOTS_DATABASE", "/var/lib/alysis/bots/bots.sqlite3")
            ),
            token=os.environ.get("ALYSIS_BOTS_TOKEN", ""),
            api_key=os.environ.get("ALYSIS_BOTS_MODEL_KEY", ""),
            base_url=os.environ.get(
                "ALYSIS_BOTS_MODEL_URL", "https://opencode.ai/inference/openai/v1"
            ),
            model=os.environ.get("ALYSIS_BOTS_MODEL", "glm-5.3-flash-free"),
            max_calls=int(os.environ["ALYSIS_BOTS_MAX_CALLS"])
            if os.environ.get("ALYSIS_BOTS_MAX_CALLS")
            else None,
            coding_calls=int(os.environ["ALYSIS_BOTS_CODING_CALLS"])
            if os.environ.get("ALYSIS_BOTS_CODING_CALLS")
            else None,
            output_tokens=int(os.environ["ALYSIS_BOTS_OUTPUT_TOKENS"])
            if os.environ.get("ALYSIS_BOTS_OUTPUT_TOKENS")
            else None,
            protocol=os.environ.get("ALYSIS_BOTS_PROTOCOL", "openai_compat"),
            provider_key=os.environ.get("ALYSIS_BOTS_PROVIDER") or None,
            reasoning_effort=os.environ.get("ALYSIS_BOTS_REASONING_EFFORT") or None,
        )


# Owls use the same per-model route as the desktop agent (llm.factory.resolve_model_protocol):
# the managed Alysis endpoint serves hosted OpenAI models only through Responses when tools are
# present, and its Claude models through Messages. Without this, a signed-in owl on such a model
# failed its first model call.
OWLS_CLIENTS = {
    ANTHROPIC_MESSAGES_PROTOCOL: AnthropicMessagesClient,
    OPENAI_RESPONSES_PROTOCOL: OpenAIResponsesClient,
}


def owls_protocol(settings) -> str:
    return resolve_model_protocol(
        provider_key=settings.provider_key, model=settings.model, protocol=settings.protocol
    )


def create_app(
    settings: Settings,
    *,
    client_factory: Callable[[str], ChatClient] | None = None,
    github_reader: GitHubReader | None = None,
    computer_views: ComputerViews | None = None,
    browser_computers: BrowserComputers | None = None,
    desktop_computers: DesktopComputers | None = None,
    cloud_computers=None,
    object_storage=None,
    task_store=None,
    work_authorizer=None,
) -> FastAPI:
    store = (
        task_store if task_store is not None else Store(settings.database, objects=object_storage)
    )
    if (
        task_store is not None
        and object_storage is not None
        and store.objects is not object_storage
    ):
        raise ValueError("Use the task store's configured object storage.")
    connections = ConnectionStore(settings.database.parent / "connections.sqlite3")
    # The desktop app's plugins, account-wide for every owl (desktop_connectors.py).
    desktop_plugins = DesktopConnectors()
    computers = computer_views or (
        browser_computers.views
        if browser_computers
        else desktop_computers.views
        if desktop_computers
        else ComputerViews()
    )
    if cloud_computers is not None:
        if desktop_computers is not None or cloud_computers.owner != settings.owner:
            raise ValueError("Cloud computers require this owner and a single desktop backend.")
        from .cloud_desktop import CloudDesktopFiles

        if (
            cloud_computers.store is not None
            and cloud_computers.store.path.resolve() != store.path.resolve()
        ):
            raise ValueError("Cloud workspaces require this service's task database.")
        cloud_computers.store = store
        cloud_computers.lifecycle = lambda bot_id, phase: computers.set_lifecycle(
            settings.owner, bot_id, phase
        )
        desktop_computers = DesktopComputers(
            settings.owner,
            computers,
            factory=cloud_computers,
            # The shared factory owns capacity, including its waiting queue and
            # uncertain starts. A manager-side count would refuse queued Bots.
            max_computers=None,
            store=store,
            file_transfer=CloudDesktopFiles(),
        )
        if cloud_computers.browser_enabled:
            if browser_computers is not None:
                raise ValueError("Cloud computers require a single browser backend.")
            browser_computers = BrowserComputers(
                settings.owner,
                computers,
                cloud_computers.browser,
                max_computers=None,
                store=store,
            )
    if settings.desktop_enabled and desktop_computers is None:
        if platform.system() != "Linux":
            raise ValueError(
                "Private desktop execution requires Linux or an installed managed local VM."
            )
        from .workspaces import WorkspaceStore

        desktop_computers = DesktopComputers(
            settings.owner,
            computers,
            store=store,
            workspaces=WorkspaceStore(settings.database.parent / "computer-workspaces"),
        )
    if settings.browser_enabled and browser_computers is None:
        if platform.system() != "Linux":
            raise ValueError("The browser executor requires the isolated Linux development lab.")
        from ..ide.browser_profiles import BrowserProfileStore
        from .browser_linux import LinuxBrowserComputer

        profiles = BrowserProfileStore(settings.database.parent / "browser-profiles")
        browser_computers = BrowserComputers(
            settings.owner,
            computers,
            lambda bot_id, cancel: LinuxBrowserComputer(
                bot_id, cancel, profile_store=profiles, account_id=settings.owner
            ),
            store=store,
        )
    if browser_computers and (
        browser_computers.owner != settings.owner or browser_computers.views is not computers
    ):
        raise ValueError("Browser computers must use this service's owner and ComputerViews.")
    if desktop_computers and (
        desktop_computers.owner != settings.owner or desktop_computers.views is not computers
    ):
        raise ValueError("Offline computers must use this service's owner and ComputerViews.")
    if desktop_computers:
        if (
            desktop_computers.store
            and desktop_computers.store.path.resolve() != store.path.resolve()
        ):
            raise ValueError("Offline file transfers must use this service's library database.")
        desktop_computers.store = store

    def make_client(task_id: str) -> ChatClient:
        protocol = owls_protocol(settings)
        client_class = OWLS_CLIENTS.get(protocol, OpenAICompatClient)
        client = client_class(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
            timeout_s=90,
            enable_thinking=bool(settings.reasoning_effort),
            reasoning_effort=settings.reasoning_effort,
            provider_key=settings.provider_key,
            extra_headers={
                **settings.extra_headers,
                **(
                    {"x-opencode-session": task_id}
                    if urlsplit(settings.base_url).hostname == "opencode.ai"
                    else {}
                ),
            },
            provider_retry_settings=ProviderRetrySettings(max_retries=0, disable_retries=True),
        )

        # The canonical image-content path is qualified here only for the
        # compatible protocol. Other adapters need their own delivery proof.
        client._alysis_browser_image_input = protocol == "openai_compat" and declared_image_support(
            settings.model, settings.base_url
        )
        return client

    runtime = BotRuntime(
        store,
        settings.owner,
        client_factory or make_client,
        limits=RuntimeLimits.for_model(
            model=settings.model,
            base_url=settings.base_url,
            max_calls=settings.max_calls,
            output_tokens=settings.output_tokens,
            coding_calls=settings.coding_calls,
            background_workers=min(16, max(2, cloud_computers.max_computers or 16))
            if cloud_computers
            else 2,
        ),
        coding_enabled=bool(settings.coding_token),
        connections=lambda bot_id: BoundConnections(
            connections, settings.owner, "bot:" + bot_id, github_reader, desktop_plugins
        ),
        browser_computers=browser_computers,
        desktop_computers=desktop_computers,
    )
    from .reply_review import ReplyReview

    if work_authorizer is not None:
        runtime.work_authorizer = work_authorizer

    runtime.reply_review = ReplyReview(runtime)
    runtime.action_review_enabled = True

    cleanup_state = {"pending": False, "last_success_at": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cleanup_stop = threading.Event()

        def cleanup_once():
            from .retention import ObjectRetention

            if cleanup_stop.is_set():
                raise CooperativeCancellationError("Object cleanup cancelled.")
            transfers.expire()
            return ObjectRetention(store, settings.owner).sweep(cancel=cleanup_stop.is_set)

        async def expire_uploads():
            while True:
                await asyncio.sleep(60)
                work = asyncio.create_task(asyncio.to_thread(cleanup_once))
                try:
                    import time

                    # Cancelling an asyncio waiter cannot stop its I/O thread.
                    # Keep the actual work awaitable until it has settled.
                    receipt = await asyncio.shield(work)
                    cleanup_state.update(
                        pending=bool(receipt["pending"]), last_success_at=time.time()
                    )
                except asyncio.CancelledError:
                    # A repeated shutdown cancellation must not cancel the
                    # retained task and detach its still-running I/O thread.
                    while not work.done():
                        try:
                            await asyncio.shield(work)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    try:
                        work.result()
                    except Exception:
                        cleanup_state["pending"] = True
                    raise
                except Exception:
                    # Keep retrying after a storage/database outage. Never log
                    # an upstream body or silently report successful cleanup.
                    cleanup_state["pending"] = True

        await asyncio.to_thread(runtime.start)
        cleanup = asyncio.create_task(expire_uploads())
        try:
            yield
        finally:
            cleanup_stop.set()
            cleanup.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup
            await asyncio.to_thread(runtime.stop)

    def authorize(request: Request, authorization: str | None = Header(default=None)) -> None:
        from starlette.routing import get_route_path

        path = get_route_path(request.scope)
        if path.startswith("/v1/local-computers/"):
            match = re.fullmatch(
                r"/v1/local-computers/([a-f0-9]{32})/(pair|revoke|connect|requests/[a-f0-9]{32}(?:/transfer)?)",
                path,
            )
            if not match or request.method != "POST":
                raise HTTPException(404, "Desktop route not found.")
            if match[2] not in ("pair", "revoke"):
                from .local_device_auth import DeviceGrants

                credential = DeviceGrants.digest(authorization)
                with store.connect() as db:
                    app.state.device_grants.require(
                        db,
                        match[1],
                        credential,
                        receipt=bool(re.fullmatch(r"requests/[a-f0-9]{32}", match[2])),
                    )
                request.state.device_credential = credential
                return
        expected = settings.coding_token if path.startswith("/internal/coding/") else settings.token
        if (
            not expected
            or not authorization
            or not hmac.compare_digest(authorization, "Bearer " + expected)
        ):
            raise HTTPException(status_code=401, detail="Bots connection authorization required.")

    app = FastAPI(
        title="Alysis Bots development service",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(authorize)],
    )
    runtime.computer_controls.views = computers
    app.state.store, app.state.runtime = store, runtime
    transfers = FileTransfers(store, settings.owner)
    from .computer_http import ComputerFrameCompression

    app.add_middleware(ComputerFrameCompression)
    app.state.connections = connections
    app.state.desktop_plugins = desktop_plugins
    from .coding_api import install_coding_routes

    install_coding_routes(
        app, runtime=runtime, settings=settings, client_factory=client_factory or make_client
    )

    @app.middleware("http")
    async def bounded_requests(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH"):
            try:
                length = int(request.headers.get("content-length", "-1"))
            except ValueError:
                length = -1
            if length < 0:
                return JSONResponse({"detail": "Content-Length is required."}, status_code=411)
            limit = (
                MAX_ENCODED_BYTES + 4096 if request.url.path.endswith("/files/binary") else 512000
            )
            if length > limit:
                return JSONResponse({"detail": "Request is too large."}, status_code=413)
        elif request.method == "DELETE":
            # A delete usually has no body; one that does gets the same bound.
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                length = -1
            if length < 0 or request.headers.get("transfer-encoding"):
                return JSONResponse({"detail": "Content-Length is required."}, status_code=411)
            if length > 512000:
                return JSONResponse({"detail": "Request is too large."}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(NotFound)
    async def missing(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    from .object_storage import ObjectStorageError
    from .postgres import SharedDatabaseError

    @app.exception_handler(SharedDatabaseError)
    async def database_unavailable(_request, _exc):
        return JSONResponse(
            {
                "detail": "Bot data is unavailable; this request did not confirm success. "
                "Check saved work before retrying."
            },
            status_code=503,
        )

    @app.exception_handler(ObjectStorageError)
    async def storage_unavailable(_request, _exc):
        return JSONResponse(
            {"detail": "File storage is unavailable or could not verify the saved data."},
            status_code=503,
        )

    @app.exception_handler(Conflict)
    async def conflict(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(LimitReached)
    async def limit(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=429)

    @app.exception_handler(ConnectionUnavailable)
    async def connection_unavailable(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    # Native-main-only bridge. The renderer's generic route grammar excludes these.
    from .local_device_api import install_local_device_routes

    install_local_device_routes(app, runtime, transfers)

    @app.get("/v1/bots/{bot_id}/apps")
    def bot_apps(bot_id: str):
        store.bot(settings.owner, bot_id)
        return [
            {**row, "availability": "configured" if github_reader else "unavailable"}
            for row in connections.catalog(settings.owner, "bot:" + bot_id)
        ]

    @app.get("/v1/bots/{bot_id}/computer")
    def bot_computer(bot_id: str):
        store.bot(settings.owner, bot_id)
        frame = computers.frame(settings.owner, bot_id)
        phase = computers.lifecycle(settings.owner, bot_id)
        if phase is not None:
            frame["lifecycle"] = phase
        control = runtime.computer_controls.status(bot_id)
        if control["available"] or control["state"] != "agent":
            frame["control"] = control
        if bot_id in runtime.computer_open_errors:
            frame["error"] = runtime.computer_open_errors[bot_id]
        factory = getattr(runtime.desktop_computers, "factory", None)
        if getattr(getattr(factory, "spec", None), "suspend_mode", None) == "Memory":
            frame["resume_kind"] = "memory"
            frame["recovery_available"] = factory.recovery_available(bot_id)
        return frame

    @app.post("/v1/bots/{bot_id}/computer/open", status_code=202)
    def open_bot_computer(bot_id: str):
        runtime.open_computer(bot_id)
        return {"state": "starting"}

    @app.post("/v1/bots/{bot_id}/computer/recover", status_code=202)
    def recover_bot_computer(bot_id: str, decision: ComputerRecovery):
        runtime.open_computer(bot_id, recover=decision.confirm)
        return {"state": "starting"}

    @app.post("/v1/bots/{bot_id}/computer/control")
    async def bot_computer_control(bot_id: str, request: Request):
        await asyncio.to_thread(store.bot, settings.owner, bot_id)
        try:
            decision = ControlRequest.model_validate(await request.json())
        except ClientDisconnect:
            # No complete gesture was admitted. Do not log a traceback/body or
            # treat reconnecting as permission to replay an incomplete request.
            raise HTTPException(
                status_code=400, detail="Computer input connection closed."
            ) from None
        except ValueError:
            # Validation errors must not echo private keyboard input.
            raise HTTPException(
                status_code=422, detail="Invalid computer control request."
            ) from None
        operation = (
            runtime.return_computer
            if decision.action == "resume"
            else runtime.computer_controls.request
        )
        result = await asyncio.to_thread(operation, bot_id, decision)
        runtime.wake()
        return result

    @app.patch("/v1/bots/{bot_id}/apps/{connection_id}")
    def bot_app_grant(bot_id: str, connection_id: str, decision: GrantInput):
        store.bot(settings.owner, bot_id)
        connections.set_grant(settings.owner, "bot:" + bot_id, connection_id, decision)
        return bot_apps(bot_id)

    # The desktop app's plugins (desktop_connectors.py). Only Electron main calls these; the
    # renderer's request allowlist doesn't include them.
    @app.put("/v1/desktop-plugins/catalog")
    def desktop_plugins_catalog(data: CatalogInput):
        return desktop_plugins.publish(data)

    @app.post("/v1/desktop-plugins/next")
    async def desktop_plugins_next():
        return await asyncio.to_thread(desktop_plugins.next)

    @app.post("/v1/desktop-plugins/calls/{call_id}")
    def desktop_plugins_answer(call_id: str, data: ResultInput):
        if not re.fullmatch(r"[a-f0-9]{32}", call_id):
            raise HTTPException(404, "This plugin call is no longer waiting.")
        result = desktop_plugins.answer(call_id, data)
        runtime.wake()
        return result

    @app.get("/v1/storage/library")
    def library_storage():
        return store.library_usage(settings.owner)

    @app.get("/v1/status")
    def status():
        from .hosted_maintenance import MaintenanceLease

        with store.connect() as db:
            maintenance_pending = MaintenanceLease.pending(db)
        cloud_billing = getattr(cloud_computers, "billing", None)
        billing_status = cloud_billing.status() if cloud_billing else None
        return {
            "cloud_billing": billing_status,
            "mode": settings.service_mode,
            "computer_backend": getattr(cloud_computers.provider, "name", "remote-computer")
            if cloud_computers
            else "bubblewrap"
            if desktop_computers
            else None,
            "private_computer_available": desktop_computers is not None,
            "platform": {"os": platform.system(), "architecture": platform.machine()},
            "worker_ready": runtime.is_running,
            "browser_cleanup_pending": runtime.browser_cleanup_pending,
            "storage_cleanup": dict(cleanup_state),
            "maintenance_pending": maintenance_pending,
            "computer_capacity": cloud_computers.capacity() if cloud_computers else None,
            "library_storage": store.library_usage(settings.owner),
            "state_backend": "postgresql" if store.shared else "sqlite",
            "model": settings.model,
            "max_calls": settings.max_calls,
            "usage": store.usage(settings.owner),
            "capabilities": [
                "conversations",
                "memory_review",
                "text_files",
                "history_retrieval",
                "working_notes",
                "schedules",
                "file_event_activations",
            ],
            "coding_connected": bool(settings.coding_token)
            and any(w["online"] for w in runtime.coding.workspaces()),
        }

    @app.get("/v1/settings/reply-review")
    def reply_review_settings():
        return store.reply_review_settings(settings.owner)

    @app.put("/v1/settings/reply-review")
    def update_reply_review_settings(data: ReplyReviewSettings):
        return store.reply_review_settings(settings.owner, data)

    @app.get("/v1/bots")
    def bots():
        listed = store.list_bots(settings.owner)
        # Where each owl's computer stands, so lists can say "waiting for a computer" when every
        # private computer on this device is busy, instead of looking stuck.
        for bot in listed:
            bot["computer"] = computers.lifecycle(settings.owner, bot["id"])
        return listed

    @app.post("/v1/bots", status_code=201)
    def create_bot(data: BotInput):
        return store.save_bot(settings.owner, data)

    @app.put("/v1/bots/{bot_id}")
    def update_bot(bot_id: str, data: BotInput):
        return store.save_bot(settings.owner, data, bot_id)

    @app.delete("/v1/bots/{bot_id}", status_code=204)
    def delete_bot(bot_id: str):
        store.bot(settings.owner, bot_id)
        factory = getattr(runtime.desktop_computers, "factory", None)
        if getattr(getattr(factory, "spec", None), "suspend_mode", None) == "Memory":
            with store.connect() as db:
                if db.execute(
                    "SELECT 1 FROM tasks WHERE bot_id=? AND status IN ('queued','running','waiting','cancelling') LIMIT 1",
                    (bot_id,),
                ).fetchone():
                    raise Conflict("Stop this Owl's work before deleting it.")
            factory.recover(bot_id, deleting=True)
        store.delete_bot(settings.owner, bot_id)

    @app.get("/v1/bots/{bot_id}/chats")
    def chats(bot_id: str):
        return store.list_chats(settings.owner, bot_id)

    @app.post("/v1/bots/{bot_id}/chats", status_code=201)
    def create_chat(bot_id: str, data: ChatInput):
        return store.create_chat(settings.owner, bot_id, data.title)

    @app.get("/v1/chats/{chat_id}")
    def conversation(chat_id: str):
        return store.conversation(settings.owner, chat_id)

    @app.post("/v1/chats/{chat_id}/read")
    def read_chat(chat_id: str, data: ReadChatInput):
        return store.mark_chat_read(settings.owner, chat_id, data.activity_seq)

    @app.post("/v1/chats/{chat_id}/tasks", status_code=202)
    def submit(chat_id: str, data: TaskInput):
        if not runtime.is_running:
            raise HTTPException(
                status_code=503,
                detail="The task worker is unavailable. Restart the development service; saved work is preserved.",
            )
        task = store.enqueue(settings.owner, chat_id, data)
        runtime.wake()
        return task

    @app.get("/v1/tasks/{task_id}")
    def task(task_id: str):
        peer = runtime.peers.for_task(task_id)
        return {
            **store.task(settings.owner, task_id),
            "coding_job": runtime.coding.for_task(task_id),
            "peer_exchange": runtime.peers.public(peer) if peer else None,
        }

    @app.post("/v1/tasks/{task_id}/cancel")
    def cancel(task_id: str):
        return runtime.cancel(task_id)

    @app.get("/v1/bots/{bot_id}/exchanges")
    def peer_exchanges(bot_id: str):
        return runtime.peers.list(bot_id)

    @app.get("/v1/bots/{bot_id}/questions")
    def questions(bot_id: str):
        return runtime.clarifications.list(bot_id)

    @app.patch("/v1/bots/{bot_id}/questions/{question_id}")
    def decide_question(bot_id: str, question_id: str, data: QuestionDecision):
        if data.action == "answer" and not runtime.is_running:
            raise HTTPException(
                status_code=503,
                detail="The task worker is unavailable. Your question is still pending.",
            )
        result = runtime.clarifications.decide(bot_id, question_id, data)
        runtime.wake()
        return result

    @app.get("/v1/bots/{bot_id}/action-reviews")
    def action_reviews(bot_id: str):
        return runtime.action_reviews.list(bot_id)

    @app.get("/v1/bots/{bot_id}/action-review-usage")
    def action_review_usage(bot_id: str):
        return runtime.action_reviews.usage(bot_id)

    @app.patch("/v1/bots/{bot_id}/action-reviews/{review_id}")
    def decide_action_review(bot_id: str, review_id: str, data: ActionReviewDecision):
        if data.action in {"allow", "always_allow"} and not runtime.is_running:
            raise HTTPException(
                status_code=503,
                detail="The task worker is unavailable. Your approval request is still pending.",
            )
        result = runtime.action_reviews.decide(bot_id, review_id, data)
        runtime._background_wake.set()
        return result

    @app.get("/v1/bots/{bot_id}/action-review-settings")
    def action_review_settings(bot_id: str):
        return runtime.action_reviews.settings(bot_id)

    @app.patch("/v1/bots/{bot_id}/action-review-settings")
    def update_action_review_settings(bot_id: str, data: ReviewSettings):
        return runtime.action_reviews.settings(bot_id, data)

    @app.get("/v1/bots/{bot_id}/action-review-rules")
    def action_review_rules(bot_id: str):
        return runtime.action_reviews.rules(bot_id)

    @app.post("/v1/bots/{bot_id}/action-review-rules")
    def create_action_review_rule(bot_id: str, data: RuleInput):
        return runtime.action_reviews.save_rule(bot_id, data)

    @app.patch("/v1/bots/{bot_id}/action-review-rules/{rule_id}")
    def update_action_review_rule(bot_id: str, rule_id: str, data: RuleUpdate):
        return runtime.action_reviews.save_rule(bot_id, data, rule_id)

    @app.delete("/v1/bots/{bot_id}/action-review-rules/{rule_id}")
    def delete_action_review_rule(bot_id: str, rule_id: str, data: RuleRevision):
        return runtime.action_reviews.remove_rule(bot_id, rule_id, data)

    @app.get("/v1/bots/{bot_id}/proposals")
    def proposals(bot_id: str):
        return runtime.proposals.list(bot_id)

    @app.patch("/v1/bots/{bot_id}/proposals/{proposal_id}")
    def decide_proposal(bot_id: str, proposal_id: str, data: ActionDecision):
        proposal = runtime.proposals.decide(bot_id, proposal_id, data)
        runtime.wake()
        return proposal

    @app.get("/v1/bots/{bot_id}/triggers")
    def triggers(bot_id: str):
        return runtime.triggers.list(bot_id)

    @app.post("/v1/bots/{bot_id}/triggers", status_code=201)
    def add_trigger(bot_id: str, data: TriggerInput):
        return runtime.triggers.create(bot_id, data)

    @app.patch("/v1/bots/{bot_id}/triggers/{trigger_id}")
    def decide_trigger(bot_id: str, trigger_id: str, data: TriggerDecision):
        result = runtime.triggers.decide(bot_id, trigger_id, data)
        runtime.wake()
        return result

    @app.delete("/v1/bots/{bot_id}/triggers/{trigger_id}", status_code=204)
    def delete_trigger(bot_id: str, trigger_id: str):
        runtime.triggers.delete(bot_id, trigger_id)

    @app.delete("/v1/bots/{bot_id}/schedules/{schedule_id}", status_code=204)
    def delete_schedule(bot_id: str, schedule_id: str):
        store.delete_schedule(settings.owner, bot_id, schedule_id)

    @app.get("/v1/bots/{bot_id}/schedules")
    def schedules(bot_id: str):
        return store.list_schedules(settings.owner, bot_id)

    @app.post("/v1/bots/{bot_id}/schedules", status_code=201)
    def add_schedule(bot_id: str, data: ScheduleInput):
        return store.save_schedule(settings.owner, bot_id, data)

    @app.put("/v1/bots/{bot_id}/schedules/{schedule_id}")
    def edit_schedule(bot_id: str, schedule_id: str, data: ScheduleInput):
        return store.save_schedule(settings.owner, bot_id, data, schedule_id=schedule_id)

    @app.patch("/v1/bots/{bot_id}/schedules/{schedule_id}")
    def decide_schedule(bot_id: str, schedule_id: str, data: ScheduleDecision):
        return store.decide_schedule(
            settings.owner, bot_id, schedule_id, data.state, data.expected_revision
        )

    @app.post("/v1/bots/{bot_id}/schedules/{schedule_id}/runs")
    def schedule_runs(bot_id: str, schedule_id: str, data: ScheduleRunsQuery):
        return store.schedule_runs(settings.owner, bot_id, schedule_id, data.before)

    @app.get("/v1/bots/{bot_id}/memories")
    def memories(bot_id: str):
        return store.list_memories(settings.owner, bot_id)

    @app.post("/v1/bots/{bot_id}/memories", status_code=201)
    def add_memory(bot_id: str, data: MemoryInput):
        return store.save_memory(settings.owner, bot_id, data)

    @app.put("/v1/bots/{bot_id}/memories/{memory_id}")
    def edit_memory(bot_id: str, memory_id: str, data: MemoryInput):
        return store.save_memory(settings.owner, bot_id, data, memory_id=memory_id)

    @app.patch("/v1/bots/{bot_id}/memories/{memory_id}")
    def review_memory(bot_id: str, memory_id: str, data: MemoryDecision):
        return store.decide_memory(settings.owner, bot_id, memory_id, data.status)

    @app.delete("/v1/bots/{bot_id}/memories/{memory_id}", status_code=204)
    def delete_memory(bot_id: str, memory_id: str):
        store.delete_item(settings.owner, bot_id, memory_id, kind="memories")

    @app.get("/v1/bots/{bot_id}/files")
    def files(bot_id: str):
        files = store.list_files(settings.owner, bot_id, limit=101)
        if len(files) > 100:
            raise Conflict("This Library needs paged access. Update the client to use files/page.")
        return files

    @app.post("/v1/bots/{bot_id}/files/page")
    def files_page(bot_id: str, data: FileQuery):
        return store.files_page(settings.owner, bot_id, data)

    @app.post("/v1/bots/{bot_id}/history/search")
    def search_history(bot_id: str, data: HistoryQuery):
        return store.search_history(settings.owner, bot_id, data)

    @app.get("/v1/bots/{bot_id}/notes")
    def notes(bot_id: str):
        return store.list_notes(settings.owner, bot_id, include_archived=True)

    @app.post("/v1/bots/{bot_id}/notes", status_code=201)
    def add_note(bot_id: str, data: NoteInput):
        return store.save_note(settings.owner, bot_id, data)

    @app.post("/v1/bots/{bot_id}/notes/consolidate", status_code=201)
    def consolidate_notes(bot_id: str, data: NoteConsolidation):
        return store.consolidate_notes(settings.owner, bot_id, data)

    @app.post("/v1/bots/{bot_id}/notes/restore")
    def restore_note(bot_id: str, data: NoteSource):
        return store.restore_note(settings.owner, bot_id, data.note_id, data.expected_revision)

    @app.get("/v1/bots/{bot_id}/notes/{note_id}")
    def read_note(bot_id: str, note_id: str):
        return store.note(settings.owner, bot_id, note_id)

    @app.post("/v1/bots/{bot_id}/notes/{note_id}/history")
    def note_history(bot_id: str, note_id: str, data: RevisionQuery):
        return store.note(settings.owner, bot_id, note_id, before_revision=data.before_revision)

    @app.put("/v1/bots/{bot_id}/notes/{note_id}")
    def edit_note(bot_id: str, note_id: str, data: NoteInput):
        return store.save_note(settings.owner, bot_id, data, note_id=note_id)

    @app.post("/v1/bots/{bot_id}/files", status_code=201)
    def add_file(bot_id: str, data: FileInput):
        return store.save_file(settings.owner, bot_id, data)

    @app.post("/v1/bots/{bot_id}/files/binary", status_code=201)
    def add_binary_file(bot_id: str, data: BinaryFileInput):
        return store.save_bytes(
            settings.owner,
            bot_id,
            data.name,
            decode_file(data.data_base64),
            previous_file_id=data.previous_file_id,
        )

    @app.post("/v1/bots/{bot_id}/uploads", status_code=201)
    def begin_upload(bot_id: str, data: UploadInput):
        return transfers.begin(bot_id, data)

    @app.get("/v1/bots/{bot_id}/uploads/{upload_id}")
    def read_upload(bot_id: str, upload_id: str):
        return transfers.read(bot_id, upload_id)

    @app.put("/v1/bots/{bot_id}/uploads/{upload_id}")
    def append_upload(bot_id: str, upload_id: str, data: UploadChunk):
        return transfers.append(bot_id, upload_id, data)

    @app.post("/v1/bots/{bot_id}/uploads/{upload_id}/complete")
    def complete_upload(bot_id: str, upload_id: str):
        return transfers.complete(bot_id, upload_id)

    @app.delete("/v1/bots/{bot_id}/uploads/{upload_id}")
    def cancel_upload(bot_id: str, upload_id: str):
        return transfers.cancel(bot_id, upload_id)

    @app.get("/v1/bots/{bot_id}/files/{file_id}/chunks/{offset}")
    def file_chunk(bot_id: str, file_id: str, offset: int):
        try:
            return transfers.chunk(bot_id, file_id, offset)
        except ValueError:
            raise HTTPException(416, "That part of the file doesn't exist.") from None

    @app.get("/v1/bots/{bot_id}/files/{file_id}/download")
    def download_file(bot_id: str, file_id: str):
        payload = store.file_data(settings.owner, bot_id, file_id)
        return {"data_base64": base64.b64encode(payload).decode("ascii")}

    @app.get("/v1/bots/{bot_id}/files/{file_id}")
    def read_file(bot_id: str, file_id: str):
        return store.file(settings.owner, bot_id, file_id)

    @app.delete("/v1/bots/{bot_id}/files/{file_id}", status_code=204)
    def delete_file(bot_id: str, file_id: str):
        store.delete_item(settings.owner, bot_id, file_id, kind="files")

    return app
