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
from dataclasses import dataclass, field, replace
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
from ..config import ImageGenerationConfig
from ..llm.anthropic_messages import AnthropicMessagesClient
from ..llm.base import ChatClient
from ..llm.openai_compat import OpenAICompatClient
from ..llm.openai_responses import OpenAIResponsesClient
from ..llm.protocols import ANTHROPIC_MESSAGES_PROTOCOL, OPENAI_RESPONSES_PROTOCOL
from ..llm.provider_limits import ProviderRetrySettings
from ..provider_url import is_alysis_gateway_url
from .about_me import AboutMeDecision, AboutMeSettings
from .action_review import ApprovalDecision as ActionReviewDecision
from .action_review import ReviewSettings, RuleInput, RuleRevision, RuleUpdate
from .browser import BrowserComputers
from .browser_sign_ins import SignIns, SignOutInput
from .clarifications import QuestionDecision
from .computer import ComputerViews
from .computer_control import ComputerRecovery, ControlRequest
from .desktop import DesktopComputers
from .desktop_connectors import (
    AppNeedDecision,
    CatalogInput,
    DesktopConnectors,
    OwlApps,
    PluginChoice,
    ResultInput,
)
from .files import MAX_ENCODED_BYTES, BinaryFileInput, FileQuery, decode_file
from .models import (
    BotCreateInput,
    BotDuplicateInput,
    BotInput,
    BotOrganizeInput,
    ChatInput,
    ConversationPageQuery,
    FileInput,
    HistoryQuery,
    IntroInput,
    MemoryDecision,
    MemoryInput,
    NoteConsolidation,
    NoteInput,
    NoteSource,
    ReadChatInput,
    RegenerateInput,
    ReplyReviewSettings,
    RevisionQuery,
    TaskInput,
    TimeZoneSettings,
)
from .owl_models import (
    ModelConnection,
    ModelUnavailable,
    PlaceModels,
    image_input,
    model_protocol,
)
from .owl_move import (
    MAX_PAGE_REQUEST_BYTES,
    InvalidMove,
    MoveBegin,
    MoveExport,
    MoveFinish,
    MovePage,
    OwlMoves,
    export_page,
    page_path,
    require_unchanged,
)
from .owl_move import summary as move_summary
from .playbooks import PlaybookUndo
from .proposals import ActionDecision, SetupDecision
from .queued_messages import QueuedMessageChange, QueuedMessages
from .reactions import ReactionInput
from .rooms import RoomChange, RoomCreate, RoomMessage
from .routine_access import RoutineAccessDecision
from .routine_suggestions import RoutineSuggestionDecision
from .runtime import BotRuntime, RuntimeLimits
from .scheduling import (
    ScheduleDecision,
    ScheduleInput,
    ScheduleModelChoice,
    ScheduleRunNow,
    ScheduleRunsQuery,
)
from .store import Conflict, LimitReached, NotFound, Store
from .task_events import TaskEventsQuery
from .task_events import follow as follow_task
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
    # Other models this service offers on the same endpoint, which an owl may pick to think
    # with instead of `model` (owl_models.py). Empty: every owl uses `model`.
    models: tuple[str, ...] = ()
    # The image generation this connection declares (owl_images.py): the engine's image
    # settings, whose endpoint and key default to this connection's own. None: owls with this
    # connection make no pictures. Bootstrap JSON gives an object.
    image_generation: ImageGenerationConfig | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.service_mode not in ("local", "cloud", "development"):
            raise ValueError("Unknown Bots service mode.")
        if isinstance(self.image_generation, dict):
            object.__setattr__(
                self,
                "image_generation",
                ImageGenerationConfig.model_validate(self.image_generation),
            )
        if self.image_generation is not None and not isinstance(
            self.image_generation, ImageGenerationConfig
        ):
            raise ValueError("Image generation settings must be an object.")
        if not isinstance(self.models, (list, tuple)) or any(
            not isinstance(item, str) or not item.strip() or len(item) > 200 for item in self.models
        ):
            raise ValueError("Offered models must be model names.")
        # Bootstrap JSON gives a list; keep an immutable tuple like the other settings.
        object.__setattr__(self, "models", tuple(item.strip() for item in self.models))
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
            models=tuple(
                item for item in os.environ.get("ALYSIS_BOTS_MODELS", "").split(",") if item.strip()
            ),
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
    return model_protocol(settings)


def offered_models(settings: Settings) -> Callable[[], list[ModelConnection]]:
    """The service's own model, the default, and the others it offers on the same endpoint."""
    connections = [ModelConnection(settings.model, settings.model, settings, default=True)]
    connections += [
        ModelConnection(model, model, replace(settings, model=model))
        for model in dict.fromkeys(settings.models)
        if model != settings.model
    ]
    return lambda: list(connections)


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
    model_connections: Callable[[], list[ModelConnection]] | None = None,
) -> FastAPI:
    """``model_connections``: the models owls here may pick (owl_models.py); by default the
    service's own model and ``settings.models``."""
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
    # The desktop app's plugins, each owl using those on for it (desktop_connectors.py).
    desktop_plugins = DesktopConnectors()
    # Owls on this computer also use the account's More apps (owl_hub_apps.py); a hosted service
    # has no account key of its own to call them with.
    more_apps = None
    if settings.service_mode == "local":
        from .owl_hub_apps import OwlHubApps

        more_apps = OwlHubApps()
    owl_apps = OwlApps(store, settings.owner, desktop_plugins, more_apps)
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

    def model_client(task_id: str, connection: Settings) -> ChatClient:
        """A client for one model connection: the service's own, or one an owl picked."""
        client_class = OWLS_CLIENTS.get(owls_protocol(connection), OpenAICompatClient)
        client = client_class(
            base_url=connection.base_url,
            api_key=connection.api_key,
            model=connection.model,
            timeout_s=90,
            enable_thinking=bool(connection.reasoning_effort),
            reasoning_effort=connection.reasoning_effort,
            provider_key=connection.provider_key,
            extra_headers={
                **connection.extra_headers,
                **(
                    {"x-opencode-session": task_id}
                    if urlsplit(connection.base_url).hostname == "opencode.ai"
                    else {}
                ),
            },
            provider_retry_settings=ProviderRetrySettings(max_retries=0, disable_retries=True),
        )

        # The canonical image-content path is qualified here only for the
        # compatible protocol. Other adapters need their own delivery proof.
        client._alysis_browser_image_input = image_input(connection)
        return client

    def make_client(task_id: str) -> ChatClient:
        return model_client(task_id, settings)

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
            connections, settings.owner, "bot:" + bot_id, github_reader, owl_apps.for_owl(bot_id)
        ),
        browser_computers=browser_computers,
        desktop_computers=desktop_computers,
    )
    from .reply_review import ReplyReview

    if work_authorizer is not None:
        runtime.work_authorizer = work_authorizer

    runtime.reply_review = ReplyReview(runtime)
    runtime.action_review_enabled = True
    runtime.models = PlaceModels(model_connections or offered_models(settings), model_client)
    # Refused credentials then mean signing in to Alysis again, not fixing a model key.
    runtime.model_account = is_alysis_gateway_url(settings.base_url)

    cleanup_state = {"pending": False, "last_success_at": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cleanup_stop = threading.Event()

        def cleanup_once():
            from .retention import ObjectRetention

            if cleanup_stop.is_set():
                raise CooperativeCancellationError("Object cleanup cancelled.")
            transfers.expire()
            # A move nobody finished within a day gives up its never-shown copy.
            moves.expire()
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
    # Owls arriving from the person's other place (owl_move.py).
    moves = OwlMoves(store, settings.owner, models=runtime.models)
    app.state.owl_moves = moves
    from .computer_http import ComputerFrameCompression

    app.add_middleware(ComputerFrameCompression)
    app.state.connections = connections
    app.state.desktop_plugins = desktop_plugins
    app.state.owl_apps = owl_apps
    from .coding_api import install_coding_routes

    # A coding worker's model calls think with the model of the owl whose task it works on.
    install_coding_routes(
        app, runtime=runtime, settings=settings, client_factory=runtime.task_client
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
                MAX_ENCODED_BYTES + 4096
                if request.url.path.endswith("/files/binary")
                else MAX_PAGE_REQUEST_BYTES
                if page_path(request.url.path)
                else 512000
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

    @app.exception_handler(InvalidMove)
    async def invalid_move(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

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
    def bot_computer(bot_id: str, status_only: bool = False):
        store.bot(settings.owner, bot_id)
        frame = (computers.status if status_only else computers.frame)(settings.owner, bot_id)
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

    @app.get("/v1/bots/{bot_id}/computer/status")
    def bot_computer_status(bot_id: str):
        return bot_computer(bot_id, status_only=True)

    @app.post("/v1/bots/{bot_id}/computer/live")
    async def bot_live_display(bot_id: str, request: Request):
        await asyncio.to_thread(store.bot, settings.owner, bot_id)
        try:
            value = await request.json()
            if not isinstance(value, dict) or set(value) != {"session_id", "stream"}:
                raise ValueError
            return await asyncio.to_thread(
                computers.live_display, settings.owner, bot_id, value["session_id"], value["stream"]
            )
        except Exception:
            raise HTTPException(
                status_code=409, detail="Desktop stream unavailable. Reconnect the viewer."
            ) from None

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

    # Sites the owl's browser is signed in to, from its saved browser storage.
    sign_ins = (
        SignIns(store, settings.owner, cloud_computers)
        if cloud_computers is not None and cloud_computers.browser_states is not None
        else None
    )

    def owl_sign_ins(bot_id):
        store.bot(settings.owner, bot_id)
        if sign_ins is None:
            raise HTTPException(404, "This connection keeps no browser sign-ins.")
        return sign_ins

    @app.get("/v1/bots/{bot_id}/sign-ins")
    def bot_sign_ins(bot_id: str):
        return owl_sign_ins(bot_id).list(bot_id)

    @app.post("/v1/bots/{bot_id}/sign-ins/sign-out")
    def bot_sign_out(bot_id: str, data: SignOutInput):
        return owl_sign_ins(bot_id).sign_out(
            bot_id, None if data.everywhere else data.site, cancel=runtime._stop.is_set
        )

    @app.patch("/v1/bots/{bot_id}/apps/{connection_id}")
    def bot_app_grant(bot_id: str, connection_id: str, decision: GrantInput):
        store.bot(settings.owner, bot_id)
        connections.set_grant(settings.owner, "bot:" + bot_id, connection_id, decision)
        return bot_apps(bot_id)

    # Which connected apps an owl may use, and its cards asking for one (desktop_connectors.py).
    @app.get("/v1/bots/{bot_id}/plugins")
    def bot_plugins(bot_id: str):
        return owl_apps.listing(bot_id)

    @app.put("/v1/bots/{bot_id}/plugins")
    def choose_bot_plugin(bot_id: str, data: PluginChoice):
        return owl_apps.choose(bot_id, data)

    @app.patch("/v1/bots/{bot_id}/app-needs/{need_id}")
    def decide_app_need(bot_id: str, need_id: str, data: AppNeedDecision):
        result = owl_apps.decide(bot_id, need_id, data)
        runtime.wake()
        return result

    # The desktop app's plugins (desktop_connectors.py). Only Electron main calls these; the
    # renderer's request allowlist doesn't include them.
    @app.put("/v1/desktop-plugins/catalog")
    def desktop_plugins_catalog(data: CatalogInput):
        result = desktop_plugins.publish(data)
        # A request waiting for an app the person chose to connect carries on once it is.
        if owl_apps.resume_connected():
            runtime.wake()
        return result

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
                # A message can carry file_ids: Library files the person attached to it.
                "message_files",
                # PUT /v1/settings/time-zone: owls read dates and times in the person's zone.
                "time_zone",
                # A failed or interrupted task carries failure: {kind} (failures.py).
                "task_failure_kinds",
                # Creating an owl takes request_id: the same id returns the owl already made.
                "bot_create_request_id",
                # POST /v1/chats/{id}/page reads a conversation a page at a time, and polls
                # only what changed since the last read.
                "conversation_pages",
                # GET /v1/needs-you lists everything waiting for the person across owls.
                "needs_you",
                # Routines: Run now, their next run times, monthly and daily-window timings,
                # and failed runs that are retried or wait for a limit instead of pausing.
                "schedule_run_now",
                "schedule_next_runs",
                "schedule_monthly",
                "schedule_windows",
                "schedule_retries",
                # GET/PUT /v1/settings/about-me: what the person wrote about themselves, read by
                # every owl turn; owls suggest additions the person accepts or declines.
                "about_me",
                # An owl proposes its own setup (purpose, instructions, optional routine) as one
                # card in its chat; PATCH /v1/bots/{id}/setup-proposals/{id} applies or declines it.
                "owl_setup",
                # GET/PUT /v1/bots/{id}/plugins: which connected apps each owl may use; owls ask
                # for an app with a card (task app_needs, PATCH /v1/bots/{id}/app-needs/{id});
                # the desktop app's catalog may name every app owls could use (apps).
                "owl_apps",
                # Routines declare the owl's computer, browser and websites they need
                # (schedule needs); the person allows them once per routine
                # (PUT/DELETE /v1/bots/{id}/schedules/{id}/access), runs use exactly that, and
                # a run that needs more pauses its routine (pause_code "access").
                "routine_access",
                # GET /v1/bots/{id}/sign-ins and POST .../sign-ins/sign-out: the sites an owl's
                # browser is signed in to, and signing it out of one or all of them.
                *(["owl_sign_ins"] if sign_ins is not None else []),
                # An owl takes color and background: the look the person picked for its face;
                # owls and Needs you items carry it back (bot_color, bot_background).
                "owl_looks",
                # PATCH /v1/bots/{id}/organize pins an owl to the top of the list or archives it
                # (owls carry pinned_at and archived_at; an archived owl's routines and file
                # events rest and Needs you leaves it out), and POST /v1/bots/{id}/duplicate
                # makes a new owl like it, with a request_id like creating one.
                "owl_organize",
                # An owl takes model: the model it thinks with, one of those GET /v1/models
                # lists here (null: the place's own); its turns fail with failure kind "model"
                # when that model's connection is no longer offered. The list also carries each
                # connection's catalog models ("<connection>/<model>", grouped by connection,
                # with available and unavailable); one its catalog no longer opens gives way to
                # its connection's own model with a model_fallback event (owl_models.py).
                "owl_models",
                # An owl moves between the person's places (owl_move.py): GET /v1/bots/{id}/move
                # and POST .../move/export read it here, /v1/owl-moves takes it in there, and
                # POST .../move/finish deletes the original once its copy is complete.
                "owl_move",
                # POST /v1/bots/{id}/move/export takes model: true and then carries the owl's
                # model in its owl record (model, model_name); /v1/owl-moves keeps it when this
                # place offers it (that id, else its one offer of the same model), otherwise the
                # copy thinks with this place's own model. Asked for only between places that
                # both list this.
                "owl_move_model",
                # A routine takes model: the model its runs think with, one of those GET
                # /v1/models lists (null: its owl's), set with PUT
                # /v1/bots/{id}/schedules/{id}/model (only the model changes) or with the routine,
                # and proposed by the owl in create_schedule, edit_schedule and propose_schedule
                # (only a model offered here). A run thinks with the one its routine had when it
                # was queued, by the owl's rules: a model no longer offered fails it (kind
                # "model"), a closed catalog model gives way with model_fallback (routine: true).
                # POST /v1/bots/{id}/move/export takes routine_models: true and then carries each
                # routine's model (model, model_name); /v1/owl-moves keeps it where this place
                # offers it, otherwise the routine thinks with its owl's. Asked for only between
                # places that both list this.
                "routine_models",
                # POST /v1/tasks/{id}/regenerate: another answer to the person's message the
                # latest reply answered; the chat keeps both answers, the owl's history the new
                # one (task config regenerate). Conversations carry read_at, when the person
                # last read them, so the app can mark the messages that came after.
                "regenerate_reply",
                # PATCH /v1/tasks/{id}/queued changes or cancels a message the person sent while
                # it waits for its owl (task queued_message); tasks carry started_at.
                "queued_message_edits",
                # The same PATCH with action "steer" sends a waiting message now to the person's
                # turn that is running (steering.py): it reads it at its next model round (outcome
                # "steering", then task queued_message.steer applied, events steer_received and
                # steer_applied), or the message waits its turn ("queued" with a reason; a steer
                # whose turn ended before reading it becomes queued too, event steer_queued).
                "queued_message_steer",
                # PUT/DELETE /v1/bots/{id}/messages/{id}/reaction: thumbs up or down on an owl's
                # reply with an optional note (task reactions), read back by later turns.
                "reply_reactions",
                # Owls in GET /v1/bots carry next_routine_at: when their next routine run is due
                # (a routine that is on, or a failed run's retry), null for an archived owl or
                # one without. The desktop app keeps its computer awake for it.
                "next_routine_at",
                # An owl offers a routine for a need the person shows repeats, as a card under its
                # reply (task routine_suggestion); PATCH /v1/bots/{id}/routine-suggestions/{id}
                # turns it on (allowing what it uses, with the person's changes) or declines it.
                # A move carries suggestion records only to a place that lists this.
                "routine_suggestions",
                # Rooms (rooms.py): /v1/rooms lists, makes, changes and deletes conversations with
                # several owls; POST /v1/rooms/{id}/messages writes in one (@mentions, or to);
                # a room's id is its chat's, read with /v1/chats/{id}/page and /read. Needs you
                # items of a room's turns carry room_id and room_name.
                "owl_rooms",
                # Playbooks (playbooks.py): procedures an owl keeps after runs that worked, kept
                # only with a checked reply. GET /v1/bots/{id}/playbooks lists them, GET
                # .../playbooks/{id} reads one, POST .../undo takes back its latest version
                # (body: the version shown) and DELETE removes it. The turn that kept one carries
                # playbook (id, name, version, current). A move carries playbook records only to
                # a place that lists this.
                "owl_playbooks",
                # Owls keep what the person stated in passing once their chat has been quiet a
                # while (memory_capture.py): such memories carry captured: true in GET
                # /v1/bots/{id}/memories and show under the reply they quote, with Undo.
                "memory_capture",
                # POST /v1/tasks/{id}/events: a working task's events after the last one the app
                # has (body: after, its seq; wait, up to 10 seconds), answered as soon as there are
                # any, so its live line and steps follow without polling. Progress only: the
                # reply, and what quotes or travels with it, is read once the task has ended.
                "live_task_events",
                # Owls make and change pictures (owl_images.py) with steps create_image and
                # edit_image, whose tool_started carries file (the picture's name). GET /v1/models
                # says which models can: image_generation, the image generation their model
                # connection declares.
                "owl_images",
                # An owl comes back to its chat later by itself (check_backs.py): its check_back
                # tool keeps a time, a note and an optional condition the service checks without
                # a model, then starts one turn in the same chat. The turn that set one carries
                # check_backs (pending ones show with Cancel); the turn one started carries
                # checked_back (id, note, outcome: time, met or deadline). GET
                # /v1/chats/{id}/check-backs lists those waiting, POST
                # .../check-backs/{id}/cancel cancels one. A move carries waiting ones (records
                # of kind check_back) only to a place that lists this.
                "check_back",
                # A routine's run that checked and found nothing new ends completed with no reply
                # and leaves a read conversation read: its task has a nothing_new event (data:
                # the owl's private reason) and, in conversations, nothing_new_at; its run in
                # /schedules/{id}/runs and a schedule's last_run carry nothing_new_at, and an
                # owl's latest_activity does when that run is its newest.
                "quiet_routine_runs",
                # A turn a restart cut off (an update, a quit, a crash, sleep) starts again once,
                # with a restarted event and restarts on the task, when nothing it did reached
                # outside the owl's own records; otherwise it is interrupted with failure kind
                # service, not_rerun (acted, unconfirmed or already_rerun) and the step's name.
                "restart_reruns",
            ],
            # The person's IANA time zone as last sent, so an app sends it only when it changed.
            "time_zone": store.time_zone(settings.owner),
            # The app's version of the About me stored here, so it writes only when it changed;
            # null before its first write, and after a fact was added here.
            "about_me_version": runtime.about_me.version(),
            "coding_connected": bool(settings.coding_token)
            and any(w["online"] for w in runtime.coding.workspaces()),
        }

    @app.get("/v1/settings/reply-review")
    def reply_review_settings():
        return store.reply_review_settings(settings.owner)

    @app.put("/v1/settings/reply-review")
    def update_reply_review_settings(data: ReplyReviewSettings):
        return store.reply_review_settings(settings.owner, data)

    @app.get("/v1/settings/time-zone")
    def time_zone_settings():
        return store.time_zone_settings(settings.owner)

    @app.put("/v1/settings/time-zone")
    def update_time_zone_settings(data: TimeZoneSettings):
        return store.time_zone_settings(settings.owner, data)

    @app.get("/v1/settings/about-me")
    def about_me():
        return runtime.about_me.read()

    @app.put("/v1/settings/about-me")
    def write_about_me(data: AboutMeSettings):
        return runtime.about_me.write(data)

    @app.patch("/v1/bots/{bot_id}/about-me/suggestions/{suggestion_id}")
    def decide_about_me_suggestion(bot_id: str, suggestion_id: str, data: AboutMeDecision):
        return runtime.about_me.decide(bot_id, suggestion_id, data)

    @app.get("/v1/bots")
    def bots():
        listed = store.list_bots(settings.owner)
        # Where each owl's computer stands, so lists can say "waiting for a computer" when every
        # private computer on this device is busy, instead of looking stuck.
        for bot in listed:
            bot["computer"] = computers.lifecycle(settings.owner, bot["id"])
        return listed

    def offered(data: BotInput | ScheduleInput | ScheduleModelChoice, current: str | None = None):
        """A newly picked model (an owl's or a routine's) must be one this place offers, open to
        the person now; one kept as it was may not be any more (its turns say so), and saving
        the other settings still works."""
        if data.model is not None and data.model != current:
            if not runtime.models.offers(data.model):
                raise ModelUnavailable.for_choice(data.model)

    def routine_model(bot_id: str, schedule_id: str) -> str | None:
        """The model a routine has now (null: its owl's)."""
        with store.connect() as db:
            store._bot(db, settings.owner, bot_id)
            return store._schedule(db, bot_id, schedule_id)["model"]

    @app.get("/v1/models")
    def owl_models():
        """The models an owl here can think with; the place's own (the default) first."""
        return {"models": runtime.models.list()}

    @app.post("/v1/bots", status_code=201)
    def create_bot(data: BotCreateInput):
        offered(data)
        return store.save_bot(settings.owner, data)

    @app.put("/v1/bots/{bot_id}")
    def update_bot(bot_id: str, data: BotInput):
        if "model" in data.model_fields_set:
            offered(data, store.bot(settings.owner, bot_id).get("model"))
        return store.save_bot(settings.owner, data, bot_id)

    @app.patch("/v1/bots/{bot_id}/organize")
    def organize_bot(bot_id: str, data: BotOrganizeInput):
        return store.organize_bot(settings.owner, bot_id, data)

    @app.post("/v1/bots/{bot_id}/duplicate", status_code=201)
    def duplicate_bot(bot_id: str, data: BotDuplicateInput):
        return store.duplicate_bot(settings.owner, bot_id, data)

    def remove_bot(bot_id: str, check=None):
        store.bot(settings.owner, bot_id)
        with store.connect() as db:
            if db.execute(
                "SELECT 1 FROM tasks WHERE bot_id=? AND status IN ('queued','running','waiting','cancelling') LIMIT 1",
                (bot_id,),
            ).fetchone():
                raise Conflict("Stop this Owl's work before deleting it.")
            if check is not None:
                check(db)
        # Its computer ends with it, also one opened to watch it or kept awake between turns.
        runtime.end_computer(bot_id)
        factory = getattr(runtime.desktop_computers, "factory", None)
        if getattr(getattr(factory, "spec", None), "suspend_mode", None) == "Memory":
            from .desktop_linux import DesktopError

            try:
                factory.recover(bot_id, deleting=True)
            except DesktopError:
                raise Conflict(
                    "This owl's computer is still shutting down. Try deleting it again in a moment."
                ) from None
        store.delete_bot(settings.owner, bot_id, check=check)

    @app.delete("/v1/bots/{bot_id}", status_code=204)
    def delete_bot(bot_id: str):
        remove_bot(bot_id)

    # Moving an owl to the person's other place (owl_move.py). Only Electron main calls these;
    # the renderer's request allowlist doesn't include them.
    @app.get("/v1/bots/{bot_id}/move")
    def owl_move_summary(bot_id: str):
        return move_summary(store, settings.owner, bot_id)

    @app.post("/v1/bots/{bot_id}/move/export")
    def owl_move_export(bot_id: str, data: MoveExport):
        return export_page(store, settings.owner, bot_id, data, models=runtime.models)

    @app.post("/v1/bots/{bot_id}/move/finish", status_code=204)
    def owl_move_finish(bot_id: str, data: MoveFinish):
        """The copy is complete in the other place: delete this original, unless it changed."""
        remove_bot(bot_id, check=lambda db: require_unchanged(db, bot_id, data.fingerprint))

    @app.post("/v1/owl-moves")
    def owl_move_begin(data: MoveBegin):
        return moves.begin(data)

    @app.get("/v1/owl-moves/{move_id}")
    def owl_move_receipt(move_id: str):
        return moves.receipt(move_id)

    @app.post("/v1/owl-moves/{move_id}/pages")
    def owl_move_page(move_id: str, data: MovePage):
        return moves.receive(move_id, data)

    @app.post("/v1/owl-moves/{move_id}/complete")
    def owl_move_complete(move_id: str):
        return moves.complete(move_id)

    @app.delete("/v1/owl-moves/{move_id}")
    def owl_move_cancel(move_id: str):
        return moves.cancel(move_id)

    @app.post("/v1/bots/{bot_id}/intro", status_code=202)
    def intro(bot_id: str, data: IntroInput):
        """A new owl writes its first message itself; nothing happens once its chat has begun."""
        if not runtime.is_running:
            raise HTTPException(
                status_code=503,
                detail="The task worker is unavailable. Restart the development service; saved work is preserved.",
            )
        task = store.start_intro(settings.owner, bot_id, data)
        if task is not None:
            runtime.wake()
        return {"task": task}

    @app.get("/v1/bots/{bot_id}/chats")
    def chats(bot_id: str):
        return store.list_chats(settings.owner, bot_id)

    @app.post("/v1/bots/{bot_id}/chats", status_code=201)
    def create_chat(bot_id: str, data: ChatInput):
        return store.create_chat(settings.owner, bot_id, data.title)

    @app.get("/v1/chats/{chat_id}")
    def conversation(chat_id: str):
        return store.conversation(settings.owner, chat_id)

    @app.post("/v1/chats/{chat_id}/page")
    def conversation_page(chat_id: str, data: ConversationPageQuery):
        return store.conversation_page(settings.owner, chat_id, data)

    @app.get("/v1/needs-you")
    def needs_you():
        return store.needs_you(settings.owner)

    @app.post("/v1/chats/{chat_id}/read")
    def read_chat(chat_id: str, data: ReadChatInput):
        return store.mark_chat_read(settings.owner, chat_id, data.activity_seq)

    @app.get("/v1/chats/{chat_id}/check-backs")
    def check_backs(chat_id: str):
        """The check-backs still waiting in a chat (check_backs.py), soonest first."""
        return runtime.check_backs.list(chat_id)

    @app.post("/v1/chats/{chat_id}/check-backs/{check_back_id}/cancel")
    def cancel_check_back(chat_id: str, check_back_id: str):
        """The person's Cancel on one: it never starts its turn."""
        return runtime.check_backs.cancel(chat_id, check_back_id)

    rooms = runtime.rooms

    @app.get("/v1/rooms")
    def list_rooms():
        return rooms.list()

    @app.post("/v1/rooms", status_code=201)
    def create_room(data: RoomCreate):
        return rooms.create(data)

    @app.get("/v1/rooms/{room_id}")
    def read_room(room_id: str):
        return rooms.read(room_id)

    @app.patch("/v1/rooms/{room_id}")
    def change_room(room_id: str, data: RoomChange):
        return rooms.change(room_id, data)

    @app.delete("/v1/rooms/{room_id}", status_code=204)
    def delete_room(room_id: str):
        rooms.delete(room_id)

    @app.post("/v1/rooms/{room_id}/messages", status_code=202)
    def send_to_room(room_id: str, data: RoomMessage):
        """The person's message in a room: a turn for each owl that answers it, in order."""
        if not runtime.is_running:
            raise HTTPException(
                status_code=503,
                detail="The task worker is unavailable. Restart the development service; saved work is preserved.",
            )
        result = rooms.send(room_id, data)
        runtime.wake()
        return result

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

    @app.post("/v1/tasks/{task_id}/events")
    async def task_events(task_id: str, data: TaskEventsQuery):
        """The task's events after ``after``, as soon as there are any (task_events.py)."""
        return await follow_task(store, settings.owner, task_id, data)

    @app.post("/v1/tasks/{task_id}/cancel")
    def cancel(task_id: str):
        return runtime.cancel(task_id)

    @app.post("/v1/tasks/{task_id}/regenerate", status_code=202)
    def regenerate(task_id: str, data: RegenerateInput):
        """Another answer to the person's message the latest reply answered (regenerate.py)."""
        if not runtime.is_running:
            raise HTTPException(
                status_code=503,
                detail="The task worker is unavailable. Restart the development service; saved work is preserved.",
            )
        task = store.regenerate(settings.owner, task_id, data)
        runtime.wake()
        return task

    queued_messages = QueuedMessages(store, settings.owner)

    @app.patch("/v1/tasks/{task_id}/queued")
    def change_queued_message(task_id: str, data: QueuedMessageChange):
        """Change or cancel a message the person sent while it still waits for its owl, or send
        it now to the turn the owl is running (steering.py)."""
        return queued_messages.change(task_id, data)

    @app.put("/v1/bots/{bot_id}/messages/{message_id}/reaction")
    def react(bot_id: str, message_id: str, data: ReactionInput):
        return runtime.reactions.react(bot_id, message_id, data)

    @app.delete("/v1/bots/{bot_id}/messages/{message_id}/reaction")
    def remove_reaction(bot_id: str, message_id: str):
        return runtime.reactions.remove(bot_id, message_id)

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

    @app.patch("/v1/bots/{bot_id}/setup-proposals/{proposal_id}")
    def decide_setup(bot_id: str, proposal_id: str, data: SetupDecision):
        return runtime.setups.decide(bot_id, proposal_id, data)

    @app.patch("/v1/bots/{bot_id}/routine-suggestions/{suggestion_id}")
    def decide_routine_suggestion(bot_id: str, suggestion_id: str, data: RoutineSuggestionDecision):
        """Turn a suggested routine on (the person's own yes) or decline it (Not now)."""
        return runtime.routine_suggestions.decide(bot_id, suggestion_id, data)

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
        offered(data)
        return store.save_schedule(settings.owner, bot_id, data)

    @app.put("/v1/bots/{bot_id}/schedules/{schedule_id}")
    def edit_schedule(bot_id: str, schedule_id: str, data: ScheduleInput):
        if "model" in data.model_fields_set:
            offered(data, routine_model(bot_id, schedule_id))
        return store.save_schedule(settings.owner, bot_id, data, schedule_id=schedule_id)

    @app.put("/v1/bots/{bot_id}/schedules/{schedule_id}/model")
    def set_schedule_model(bot_id: str, schedule_id: str, data: ScheduleModelChoice):
        """The model a routine's runs think with, from those GET /v1/models lists; null: the
        owl's own. Only the model changes."""
        offered(data, routine_model(bot_id, schedule_id))
        return store.set_schedule_model(settings.owner, bot_id, schedule_id, data)

    @app.patch("/v1/bots/{bot_id}/schedules/{schedule_id}")
    def decide_schedule(bot_id: str, schedule_id: str, data: ScheduleDecision):
        return store.decide_schedule(
            settings.owner, bot_id, schedule_id, data.state, data.expected_revision
        )

    @app.put("/v1/bots/{bot_id}/schedules/{schedule_id}/access")
    def allow_routine_access(bot_id: str, schedule_id: str, data: RoutineAccessDecision):
        """Allow what a routine needs; optionally turn it on and run it once now."""
        if data.run_request_id and not runtime.is_running:
            raise HTTPException(
                status_code=503,
                detail="The task worker is unavailable. Restart the development service; saved work is preserved.",
            )
        schedule = store.allow_routine_access(settings.owner, bot_id, schedule_id, data)
        if data.run_request_id:
            store.run_schedule_now(
                settings.owner,
                bot_id,
                schedule_id,
                data.run_request_id,
                max_calls=settings.max_calls,
            )
            runtime.wake()
        return schedule

    @app.delete("/v1/bots/{bot_id}/schedules/{schedule_id}/access")
    def revoke_routine_access(bot_id: str, schedule_id: str):
        """Take back what a routine was allowed; its next run asks again."""
        return store.revoke_routine_access(settings.owner, bot_id, schedule_id)

    @app.post("/v1/bots/{bot_id}/schedules/{schedule_id}/runs")
    def schedule_runs(bot_id: str, schedule_id: str, data: ScheduleRunsQuery):
        return store.schedule_runs(settings.owner, bot_id, schedule_id, data.before)

    @app.post("/v1/bots/{bot_id}/schedules/{schedule_id}/run", status_code=202)
    def run_schedule_now(bot_id: str, schedule_id: str, data: ScheduleRunNow):
        """Run a routine once now; its schedule stays as it is."""
        if not runtime.is_running:
            raise HTTPException(
                status_code=503,
                detail="The task worker is unavailable. Restart the development service; saved work is preserved.",
            )
        result = store.run_schedule_now(
            settings.owner, bot_id, schedule_id, data.request_id, max_calls=settings.max_calls
        )
        runtime.wake()
        return result

    @app.get("/v1/bots/{bot_id}/memories")
    def memories(bot_id: str):
        return store.list_memories(settings.owner, bot_id, for_person=True)

    # Each change the person makes to an owl's memories is sorted while the owl is idle, so its
    # next turn does not wait for it (runtime.memories_changed).
    @app.post("/v1/bots/{bot_id}/memories", status_code=201)
    def add_memory(bot_id: str, data: MemoryInput):
        saved = store.save_memory(settings.owner, bot_id, data)
        runtime.memories_changed(bot_id)
        return saved

    @app.put("/v1/bots/{bot_id}/memories/{memory_id}")
    def edit_memory(bot_id: str, memory_id: str, data: MemoryInput):
        saved = store.save_memory(settings.owner, bot_id, data, memory_id=memory_id)
        runtime.memories_changed(bot_id)
        return saved

    @app.patch("/v1/bots/{bot_id}/memories/{memory_id}")
    def review_memory(bot_id: str, memory_id: str, data: MemoryDecision):
        decided = store.decide_memory(settings.owner, bot_id, memory_id, data.status)
        runtime.memories_changed(bot_id)
        return decided

    @app.post("/v1/bots/{bot_id}/memories/{memory_id}/undo")
    def undo_memory(bot_id: str, memory_id: str):
        undone = store.undo_memory(settings.owner, bot_id, memory_id)
        runtime.memories_changed(bot_id)
        return undone

    @app.delete("/v1/bots/{bot_id}/memories/{memory_id}", status_code=204)
    def delete_memory(bot_id: str, memory_id: str):
        store.delete_item(settings.owner, bot_id, memory_id, kind="memories")
        runtime.memories_changed(bot_id)

    @app.get("/v1/bots/{bot_id}/playbooks")
    def playbooks(bot_id: str):
        return runtime.playbooks.list(bot_id)

    @app.get("/v1/bots/{bot_id}/playbooks/{playbook_id}")
    def playbook(bot_id: str, playbook_id: str):
        return runtime.playbooks.read(bot_id, playbook_id, for_person=True)

    @app.post("/v1/bots/{bot_id}/playbooks/{playbook_id}/undo")
    def undo_playbook(bot_id: str, playbook_id: str, data: PlaybookUndo | None = None):
        """Take back a playbook's latest version: the one before comes back, or it goes."""
        return runtime.playbooks.undo(bot_id, playbook_id, data)

    @app.delete("/v1/bots/{bot_id}/playbooks/{playbook_id}", status_code=204)
    def delete_playbook(bot_id: str, playbook_id: str):
        runtime.playbooks.delete(bot_id, playbook_id)

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
        # People see the words around each match; the owl's own tool reads previews.
        return store.search_history(settings.owner, bot_id, data, excerpts=True)

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
