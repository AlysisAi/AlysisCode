"""Owned Chromium DevTools sessions for IDE browser automation.

The module intentionally keeps the browser/CDP boundary injectable.  Chromium
can be launched without another Python package, but speaking WebSocket CDP
correctly requires a maintained transport (including concurrent event handling
for request interception).  Integrators must supply :class:`CdpTransportFactory`;
the core fails closed when none is configured.

Security invariants:

* only an explicitly selected or known installed Chromium-family executable is
  launched, never a shell command or an inherited ``PATH`` lookup;
* every process is put in a process group owned by this service and only that
  exact group is terminated;
* the DevTools endpoint is loopback-only and uses a private, per-session profile
  outside all declared workspaces;
* the child gets a minimal environment and a fixed, owned validating egress
  proxy with no direct-network fallback;
* top-level and redirected/subresource URLs pass the same DNS-aware policy
  before the transport permits a request;
* sessions and artifacts are owner-scoped, bounded, and never accept a caller
  supplied filesystem path.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeAlias
from urllib.parse import urlsplit, urlunsplit

from ..branding import canonical_user_data_dir, env_get
from ..command_text import command_text
from .browser_egress_proxy import BrowserEgressProxy
from .browser_observation import redact_form_secrets
from .browser_profiles import BrowserProfileLease
from .protocol import redact_secrets

# A visible X server is a whole-desktop input surface, not one Chromium tab.
# Reserve it across service instances in this process. The trusted coordinator
# must additionally isolate the X server from other processes/workers.
_VISIBLE_DISPLAY_LOCK = threading.Lock()
_VISIBLE_DISPLAY_SESSIONS: dict[str, str] = {}

# Web KeyboardEvent/CDP key names and virtual-key codes, not application actions.
_PAGE_KEYS = {
    "Enter": ("Enter", 13),
    "Tab": ("Tab", 9),
    "Escape": ("Escape", 27),
    "Backspace": ("Backspace", 8),
    "Delete": ("Delete", 46),
    "ArrowLeft": ("ArrowLeft", 37),
    "ArrowUp": ("ArrowUp", 38),
    "ArrowRight": ("ArrowRight", 39),
    "ArrowDown": ("ArrowDown", 40),
    "Home": ("Home", 36),
    "End": ("End", 35),
    "PageUp": ("PageUp", 33),
    "PageDown": ("PageDown", 34),
    "Space": (" ", 32),
}

# Fixed native-control operation: neither selectors nor page text become code.
_SELECT_OPTION_FUNCTION = """function(option) {
    if (!this.isConnected || !option.isConnected || this.tagName !== 'SELECT' ||
        option.tagName !== 'OPTION' || option.closest('select') !== this ||
        this.multiple || this.matches(':disabled') || option.matches(':disabled'))
        return {dispatched: false};
    this.selectedIndex = Array.from(this.options).indexOf(option);
    this.dispatchEvent(new Event('input', {bubbles: true}));
    this.dispatchEvent(new Event('change', {bubbles: true}));
    return {dispatched: true};
}"""


def _reserve_visible_display(display: str | None, session_id: str) -> None:
    if display is not None:
        key = display.split(".", 1)[0]
        with _VISIBLE_DISPLAY_LOCK:
            if key in _VISIBLE_DISPLAY_SESSIONS:
                raise BrowserControlError(
                    "A visible browser already owns this private X11 display."
                )
            _VISIBLE_DISPLAY_SESSIONS[key] = session_id


def _release_visible_display(display: str | None, session_id: str) -> None:
    if display is not None:
        key = display.split(".", 1)[0]
        with _VISIBLE_DISPLAY_LOCK:
            if _VISIBLE_DISPLAY_SESSIONS.get(key) == session_id:
                _VISIBLE_DISPLAY_SESSIONS.pop(key)


__all__ = [
    "BrowserArtifact",
    "BrowserCancelledError",
    "BrowserDependencyError",
    "BrowserExecutable",
    "BrowserLaunchError",
    "BrowserLaunchSpec",
    "BrowserLimitError",
    "BrowserNotFoundError",
    "BrowserOwnershipError",
    "BrowserSecurityError",
    "BrowserSessionStatus",
    "BrowserTimeoutError",
    "BrowserValidationError",
    "CdpTransport",
    "CdpTransportFactory",
    "DefaultBrowserProcessLauncher",
    "DefaultProcessTreeTerminator",
    "ManagedBrowserConfig",
    "ManagedBrowserService",
    "ProcessHandle",
    "discover_browser_executable",
    "validate_browser_url",
]

CancelCheck: TypeAlias = Callable[[], bool] | None
AddressResolver: TypeAlias = Callable[[str, int], Iterable[str]]
EgressProxyFactory: TypeAlias = Callable[..., BrowserEgressProxy]
PreviewUrlProvider: TypeAlias = Callable[[], Iterable[str]]
OwnedPreviewOriginAllowed: TypeAlias = Callable[[str, str, int], bool]

_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_DEVTOOLS_PATH_RE = re.compile(r"^/devtools/browser/[A-Za-z0-9._-]{1,160}$")
_SELECTOR_MAX_CHARS = 2_000
_TYPE_MAX_CHARS = 100_000
_URL_MAX_CHARS = 8_192
_ACTIVE_PORT_MAX_BYTES = 1_024
_MAX_JSON_BYTES_HARD = 8 * 1024 * 1024
_MAX_SCREENSHOT_BYTES_HARD = 20 * 1024 * 1024
_MAX_DIAGNOSTIC_EVENTS_HARD = 2_000
_MAX_ARTIFACT_CHUNK_BYTES = 1024 * 1024
_SCREENSHOT_NAME_RE = re.compile(r"^screenshot-[0-9]{4}-[0-9a-f]{8}\.png$")
_DNS_RESOLUTION_SLOTS = threading.BoundedSemaphore(4)
_OWNERSHIP_MARKER_VERSION = 3


class BrowserError(RuntimeError):
    """Base class whose messages are safe for logs and protocol responses."""


class BrowserValidationError(BrowserError, ValueError):
    pass


class BrowserSecurityError(BrowserError):
    pass


class BrowserDependencyError(BrowserError):
    pass


class BrowserNotFoundError(BrowserError):
    pass


class BrowserLaunchError(BrowserError):
    pass


class BrowserDownloadRequiredError(BrowserError):
    """Chromium identified a download rather than a navigable document."""


class BrowserPageUnreachableError(BrowserError):
    """The address answered with a network error page; the browser and its guard are intact.

    ``net_error`` is Chromium's own error identifier (for example ``net::ERR_NAME_NOT_RESOLVED``),
    never page-controlled text.
    """

    def __init__(self, net_error: str | None = None) -> None:
        valid = isinstance(net_error, str) and re.fullmatch(r"net::ERR_[A-Z0-9_]{1,64}", net_error)
        self.net_error = net_error if valid else None
        super().__init__(
            "This address could not be reached"
            + (f" ({self.net_error})." if self.net_error else ".")
        )


class BrowserTimeoutError(BrowserError, TimeoutError):
    pass


class BrowserCancelledError(BrowserError):
    pass


class BrowserLimitError(BrowserError):
    pass


class BrowserOwnershipError(BrowserError):
    pass


class BrowserDestinationUnresolvedError(BrowserError):
    """A destination could not be checked: its DNS was slow, failing or busy. The request is
    refused, but unlike a private destination this says nothing about what the page intends."""


class BrowserDestinationTimeoutError(BrowserDestinationUnresolvedError, BrowserTimeoutError):
    pass


class BrowserControlError(BrowserError):
    """Command admission is paused or the caller's generation is stale."""


class BrowserStaleFrameError(BrowserControlError):
    """No input was sent because the observed visual frame is no longer current."""


class BrowserTargetObscuredError(BrowserControlError):
    """No click was sent because hit testing did not reach the observed element."""


class BrowserPageUnavailableError(BrowserControlError):
    """An observed page disappeared; any in-flight input may have been sent."""


@dataclass
class BrowserVisualFrame:
    owner_id: str
    session_id: str
    command_generation: int
    created_at: float
    url: str
    width: int
    height: int
    css_width: float
    css_height: float
    png: bytes = field(repr=False)
    consumed: bool = False


class ProcessHandle(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class BrowserProcessLauncher(Protocol):
    def launch(self, spec: BrowserLaunchSpec) -> ProcessHandle: ...


class ProcessTreeTerminator(Protocol):
    def terminate(self, process: ProcessHandle, *, grace_seconds: float) -> None: ...


class CdpTransport(Protocol):
    """Bounded transport contract expected from the integration layer.

    ``guarded_navigate`` must use CDP Fetch request interception.  It must call
    ``authorize_url`` before continuing every top-level, redirect, and
    subresource request.  A rejected callback must fail that request and the
    navigation.  This is stronger than validating only the initial URL and
    prevents redirect-based access to loopback/private services.

    Implementations must also cap every inbound WebSocket frame and decoded
    command/event payload at 8 MiB, reject duplicate/unknown response ids, and
    interrupt pending calls on cancellation or timeout.  The service applies
    tighter output-specific limits after that transport boundary.
    """

    @property
    def healthy(self) -> bool:
        """False once control has failed closed or the connection has ended."""
        ...

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float,
        cancel: CancelCheck = None,
    ) -> Mapping[str, Any]: ...

    def guarded_navigate(
        self,
        url: str,
        *,
        session_id: str,
        authorize_url: Callable[[str], str],
        timeout: float,
        cancel: CancelCheck = None,
        root_target_id: str | None = None,
    ) -> Mapping[str, Any]: ...

    def drain_events(
        self,
        *,
        session_id: str,
        max_events: int,
        timeout: float,
        cancel: CancelCheck = None,
    ) -> Sequence[Mapping[str, Any]]: ...

    def guarded_page_sessions(self, *, session_id: str) -> Mapping[str, str]: ...

    def close(self, *, timeout: float) -> None: ...


class CdpTransportFactory(Protocol):
    def connect(
        self,
        websocket_url: str,
        *,
        timeout: float,
        cancel: CancelCheck = None,
    ) -> CdpTransport: ...


@dataclass(frozen=True, slots=True)
class BrowserExecutable:
    path: Path
    product: str
    source: str


@dataclass(frozen=True, slots=True)
class BrowserLaunchSpec:
    executable: Path
    arguments: tuple[str, ...]
    environment: Mapping[str, str] = field(repr=False)
    profile_dir: Path


@dataclass(frozen=True, slots=True)
class BrowserArtifact:
    artifact_id: str
    relative_path: str
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class BrowserSessionStatus:
    session_id: str
    owner_id: str
    product: str
    state: str
    created_at: float
    allow_local_destinations: bool
    local_destinations_loopback_only: bool
    active_url: str | None
    artifact_count: int
    commands_paused: bool = False
    command_generation: int = 0
    operation_active: bool = False


def _default_managed_browser_data_root() -> Path:
    override = str(env_get("ALYSIS_DATA_DIR") or "").strip()
    base = Path(override).expanduser() if override else canonical_user_data_dir()
    return base / "ide-browser"


@dataclass(frozen=True, slots=True)
class ManagedBrowserConfig:
    data_root: Path = field(default_factory=_default_managed_browser_data_root)
    workspace_roots: tuple[Path, ...] = ()
    startup_timeout_seconds: float = 12.0
    operation_timeout_seconds: float = 20.0
    shutdown_grace_seconds: float = 2.0
    dns_timeout_seconds: float = 2.0
    max_sessions_total: int = 3
    max_sessions_per_owner: int = 2
    max_snapshot_bytes: int = 2 * 1024 * 1024
    max_screenshot_bytes: int = 10 * 1024 * 1024
    max_diagnostic_events: int = 500
    # Trusted host option for a visible browser in a dedicated Linux X11 desktop.
    # Never inherited from model arguments or an arbitrary DISPLAY environment.
    linux_display: str | None = None

    def __post_init__(self) -> None:
        if self.linux_display is not None and (
            not isinstance(self.linux_display, str)
            or not re.fullmatch(r":[0-9]{1,5}(?:\.[0-9]{1,3})?", self.linux_display)
        ):
            raise BrowserValidationError("Browser display must be a local Linux X11 display.")
        for value, name, upper in (
            (self.startup_timeout_seconds, "startup timeout", 120.0),
            (self.operation_timeout_seconds, "operation timeout", 300.0),
            (self.shutdown_grace_seconds, "shutdown grace", 30.0),
            (self.dns_timeout_seconds, "DNS timeout", 30.0),
        ):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise BrowserValidationError(f"Browser {name} must be numeric.")
            if not 0.05 <= float(value) <= upper:
                raise BrowserValidationError(f"Browser {name} is outside its safe range.")
        if not 1 <= self.max_sessions_total <= 16:
            raise BrowserValidationError("Browser total session limit is outside its safe range.")
        if not 1 <= self.max_sessions_per_owner <= self.max_sessions_total:
            raise BrowserValidationError("Browser owner session limit is outside its safe range.")
        if not 1 <= self.max_snapshot_bytes <= _MAX_JSON_BYTES_HARD:
            raise BrowserValidationError("Browser snapshot limit is outside its safe range.")
        if not 1 <= self.max_screenshot_bytes <= _MAX_SCREENSHOT_BYTES_HARD:
            raise BrowserValidationError("Browser screenshot limit is outside its safe range.")
        if not 1 <= self.max_diagnostic_events <= _MAX_DIAGNOSTIC_EVENTS_HARD:
            raise BrowserValidationError("Browser diagnostic limit is outside its safe range.")


@dataclass(slots=True)
class _BrowserSession:
    session_id: str
    owner_id: str
    executable: BrowserExecutable
    process: ProcessHandle
    egress_proxy: BrowserEgressProxy
    transport: CdpTransport
    cdp_session_id: str
    root_cdp_session_id: str
    root_target_id: str
    active_target_id: str
    profile_dir: Path
    artifact_dir: Path
    created_at: float
    allow_local_destinations: bool
    local_destinations_loopback_only: bool
    owned_preview_origin_allowed: OwnedPreviewOriginAllowed
    profile_lease: BrowserProfileLease | None = field(default=None, repr=False)
    profile_shutdown_attempted: bool = False
    profile_shutdown_clean: bool = False
    active_url: str | None = None
    artifact_count: int = 0
    upload_bytes: int = 0
    upload_count: int = 0
    operation_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    control_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    commands_paused: bool = False
    command_generation: int = 0
    closing: bool = False
    viewer_lease: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class _OwnershipMarker:
    session_id: str
    owner_pid: int | None
    owner_identity: str | None
    browser_pid: int | None
    browser_identity: str | None


class DefaultBrowserProcessLauncher:
    def launch(self, spec: BrowserLaunchSpec) -> ProcessHandle:
        kwargs: dict[str, Any] = {
            "args": [str(spec.executable), *spec.arguments],
            "cwd": str(spec.profile_dir),
            "env": dict(spec.environment),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "shell": False,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )
        else:
            kwargs["start_new_session"] = True
        try:
            return subprocess.Popen(**kwargs)  # type: ignore[arg-type,return-value]
        except OSError as exc:
            raise BrowserLaunchError("The managed browser process could not be started.") from exc


def _profile_process_group_stopped(process: ProcessHandle) -> bool:
    """Fail closed unless the Linux browser and its owned group have exited."""
    if os.name != "posix" or process.poll() is None:
        return False
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


class DefaultProcessTreeTerminator:
    """Terminate only the process group created by our launcher."""

    def terminate(self, process: ProcessHandle, *, grace_seconds: float) -> None:
        pid = process.pid
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            or pid == os.getpid()
            or process.poll() is not None
        ):
            return
        if os.name == "nt":
            taskkill = _windows_taskkill_executable()
            if taskkill is not None:
                with suppress(OSError, subprocess.TimeoutExpired):
                    subprocess.run(
                        [str(taskkill), "/PID", str(pid), "/T"],
                        check=False,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=grace_seconds,
                        shell=False,
                    )
            with suppress(OSError, subprocess.TimeoutExpired):
                process.wait(timeout=grace_seconds)
            if process.poll() is None and taskkill is not None:
                with suppress(OSError, subprocess.TimeoutExpired):
                    subprocess.run(
                        [str(taskkill), "/PID", str(pid), "/T", "/F"],
                        check=False,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=grace_seconds,
                        shell=False,
                    )
            if process.poll() is None:
                with suppress(OSError):
                    process.kill()
            with suppress(OSError, subprocess.TimeoutExpired):
                process.wait(timeout=grace_seconds)
            return

        # ``pid`` is also the pgid because the process was started in a new
        # session. Refuse group signalling if the OS does not confirm that.
        try:
            if os.getpgid(pid) != pid or os.getpgrp() == pid:
                with suppress(OSError):
                    process.kill()
                return
        except (OSError, AttributeError):
            with suppress(OSError):
                process.kill()
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            with suppress(OSError):
                process.terminate()
        with suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=grace_seconds)
        # Descendants can remain after their group leader exits, so always make
        # one final signal to the exact owned pgid.
        with suppress(ProcessLookupError, OSError):
            os.killpg(pid, signal.SIGKILL)
        with suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=grace_seconds)


def discover_browser_executable(
    explicit_path: str | os.PathLike[str] | None = None,
    *,
    platform: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> BrowserExecutable:
    """Find Chrome, Edge, or Chromium without executing a PATH-derived name."""

    if explicit_path is not None:
        raw = Path(explicit_path).expanduser()
        if not raw.is_absolute():
            raise BrowserValidationError("An explicit browser path must be absolute.")
        path = _validated_executable(raw)
        return BrowserExecutable(path=path, product=_product_for_path(path), source="explicit")

    system = (platform or os.sys.platform).lower()
    env = environment or os.environ
    candidates: list[tuple[str, Path]] = []
    if system.startswith("win"):
        for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            root_value = _case_insensitive_get(env, key)
            if not root_value:
                continue
            root = Path(root_value)
            candidates.extend(
                (
                    ("chrome", root / "Google/Chrome/Application/chrome.exe"),
                    ("edge", root / "Microsoft/Edge/Application/msedge.exe"),
                    ("chromium", root / "Chromium/Application/chrome.exe"),
                )
            )
    elif system == "darwin":
        candidates.extend(
            (
                ("chrome", Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")),
                ("edge", Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")),
                ("chromium", Path("/Applications/Chromium.app/Contents/MacOS/Chromium")),
            )
        )
    else:
        candidates.extend(
            (product, Path(path))
            for product, path in (
                ("chrome", "/usr/bin/google-chrome-stable"),
                ("chrome", "/usr/bin/google-chrome"),
                ("chromium", "/usr/bin/chromium"),
                ("chromium", "/usr/bin/chromium-browser"),
                ("edge", "/usr/bin/microsoft-edge-stable"),
            )
        )

    for product, candidate in candidates:
        try:
            path = _validated_executable(candidate)
        except BrowserValidationError:
            continue
        return BrowserExecutable(path=path, product=product, source="known_installation")
    raise BrowserNotFoundError(
        "No supported Chrome, Edge, or Chromium installation was found; configure an absolute path."
    )


def validate_browser_url(
    value: str,
    *,
    allow_local_destinations: bool = False,
    local_destinations_loopback_only: bool = False,
    resolver: AddressResolver | None = None,
    resolution_timeout: float = 2.0,
    owned_preview_origin_allowed: OwnedPreviewOriginAllowed | None = None,
) -> str:
    """Normalize a URL and reject unsafe schemes, userinfo, and destinations."""

    if not isinstance(value, str) or not value.strip():
        raise BrowserValidationError("Browser URL is required.")
    if (
        isinstance(resolution_timeout, bool)
        or not isinstance(resolution_timeout, int | float)
        or not 0.05 <= resolution_timeout <= 30.0
    ):
        raise BrowserValidationError("Browser DNS timeout is invalid.")
    if not isinstance(local_destinations_loopback_only, bool):
        raise BrowserValidationError("Browser loopback-only policy must be a boolean.")
    if local_destinations_loopback_only and not allow_local_destinations:
        raise BrowserValidationError(
            "Browser loopback-only policy requires explicit local-destination access."
        )
    text = value.strip()
    if len(text) > _URL_MAX_CHARS or any(ord(char) < 0x20 for char in text):
        raise BrowserValidationError("Browser URL is invalid or too long.")
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise BrowserValidationError("Browser URL has an invalid host or port.") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise BrowserSecurityError("Only HTTP and HTTPS browser URLs are allowed.")
    if parsed.username is not None or parsed.password is not None:
        raise BrowserSecurityError("Browser URLs containing credentials are not allowed.")
    if not parsed.hostname:
        raise BrowserValidationError("Browser URL host is required.")
    if parsed.fragment and len(parsed.fragment) > 2_000:
        raise BrowserValidationError("Browser URL fragment is too long.")
    host = parsed.hostname.rstrip(".").lower()
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise BrowserValidationError("Browser URL host is invalid.") from exc
    effective_port = port or (443 if scheme == "https" else 80)
    owned_preview_allowed = False
    if owned_preview_origin_allowed is not None:
        try:
            owned_preview_allowed = bool(
                owned_preview_origin_allowed(scheme, ascii_host, effective_port)
            )
        except Exception:
            owned_preview_allowed = False
    if not allow_local_destinations and not owned_preview_allowed:
        if ascii_host in {"localhost", "localhost.localdomain"} or ascii_host.endswith(".local"):
            raise BrowserSecurityError("Local and private browser destinations are disabled.")
        addresses = _resolve_addresses(
            ascii_host,
            effective_port,
            resolver=resolver,
            timeout=float(resolution_timeout),
        )
        if not addresses:
            raise BrowserSecurityError("Browser destination could not be resolved safely.")
        if any(not _is_public_address(address) for address in addresses):
            raise BrowserSecurityError("Local and private browser destinations are disabled.")
    elif owned_preview_allowed and not allow_local_destinations:
        addresses = _resolve_addresses(
            ascii_host,
            effective_port,
            resolver=resolver,
            timeout=float(resolution_timeout),
        )
        if not addresses or any(
            not _is_safe_owned_preview_address(address) for address in addresses
        ):
            raise BrowserSecurityError("Local and private browser destinations are disabled.")
    elif local_destinations_loopback_only:
        addresses = _resolve_addresses(
            ascii_host,
            effective_port,
            resolver=resolver,
            timeout=float(resolution_timeout),
        )
        if not addresses or any(
            not (_is_public_address(address) or ipaddress.ip_address(address).is_loopback)
            for address in addresses
        ):
            raise BrowserSecurityError("Only public and loopback browser destinations are enabled.")
    host_for_url = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    if port is not None:
        host_for_url = f"{host_for_url}:{port}"
    return urlunsplit((scheme, host_for_url, parsed.path or "/", parsed.query, parsed.fragment))


class ManagedBrowserService:
    def __init__(
        self,
        *,
        config: ManagedBrowserConfig | None = None,
        launcher: BrowserProcessLauncher | None = None,
        terminator: ProcessTreeTerminator | None = None,
        transport_factory: CdpTransportFactory | None = None,
        resolver: AddressResolver | None = None,
        egress_proxy_factory: EgressProxyFactory | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config or ManagedBrowserConfig()
        self._data_root = _prepare_data_root(
            self._config.data_root, workspace_roots=self._config.workspace_roots
        )
        self._profiles_root = _private_dir(self._data_root / "profiles")
        self._artifacts_root = _private_dir(self._data_root / "artifacts")
        self._launcher = launcher or DefaultBrowserProcessLauncher()
        self._terminator = terminator or DefaultProcessTreeTerminator()
        _scavenge_stale_owned_dirs(
            (self._profiles_root, self._artifacts_root),
            grace_seconds=self._config.shutdown_grace_seconds,
        )
        self._transport_factory = transport_factory
        self._resolver = resolver
        self._egress_proxy_factory = egress_proxy_factory or BrowserEgressProxy
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleeper
        self._sessions: dict[str, _BrowserSession] = {}
        self._starting_by_owner: dict[str, int] = {}
        self._starting_total = 0
        self._lock = threading.RLock()

    def start(
        self,
        owner_id: str,
        *,
        executable_path: str | os.PathLike[str] | None = None,
        allow_local_destinations: bool = False,
        local_destinations_loopback_only: bool = False,
        allowed_preview_urls_provider: PreviewUrlProvider | None = None,
        profile_lease: BrowserProfileLease | None = None,
        cancel: CancelCheck = None,
    ) -> BrowserSessionStatus:
        owner = _validated_owner(owner_id)
        if profile_lease is not None and (
            not isinstance(profile_lease, BrowserProfileLease) or profile_lease.owner_id != owner
        ):
            raise BrowserOwnershipError("Persistent browser profile is not owned by this caller.")
        if not isinstance(allow_local_destinations, bool):
            raise BrowserValidationError("Browser local-destination policy must be a boolean.")
        if not isinstance(local_destinations_loopback_only, bool):
            raise BrowserValidationError("Browser loopback-only policy must be a boolean.")
        if local_destinations_loopback_only and not allow_local_destinations:
            raise BrowserValidationError(
                "Browser loopback-only policy requires explicit local-destination access."
            )
        if allowed_preview_urls_provider is not None and not callable(
            allowed_preview_urls_provider
        ):
            raise BrowserValidationError("Browser preview URL provider is invalid.")
        if self._transport_factory is None:
            raise BrowserDependencyError(
                "Managed browser automation needs a CDP WebSocket transport with guarded navigation."
            )
        _check_cancel(cancel)
        executable = discover_browser_executable(executable_path)
        self._reserve_start(owner)
        session_id = secrets.token_urlsafe(24).replace("-", "A").replace("_", "B")
        profile_dir: Path | None = None
        artifact_dir: Path | None = None
        process: ProcessHandle | None = None
        egress_proxy: BrowserEgressProxy | None = None
        transport: CdpTransport | None = None
        try:
            _reserve_visible_display(self._config.linux_display, session_id)
            owned_preview_origin_allowed = _owned_preview_origin_check(
                allowed_preview_urls_provider
            )
            profile_dir = (
                profile_lease.begin(owner)
                if profile_lease is not None
                else _make_owned_session_dir(self._profiles_root, session_id)
            )
            artifact_dir = _make_owned_session_dir(self._artifacts_root, session_id)
            egress_proxy = self._egress_proxy_factory(
                resolver=self._resolver,
                allow_local_destinations=bool(allow_local_destinations),
                local_destinations_loopback_only=bool(local_destinations_loopback_only),
                owned_preview_origin_allowed=owned_preview_origin_allowed,
            )
            egress_proxy.start()
            proxy_host, proxy_port = _validated_egress_proxy_endpoint(egress_proxy)
            egress_proxy.deny_endpoint(proxy_host, proxy_port)
            spec = BrowserLaunchSpec(
                executable=executable.path,
                arguments=_browser_arguments(
                    profile_dir,
                    proxy_host,
                    proxy_port,
                    visible=self._config.linux_display is not None,
                ),
                environment={
                    **_sanitized_browser_environment(),
                    **(
                        {"DISPLAY": self._config.linux_display}
                        if self._config.linux_display
                        else {}
                    ),
                },
                profile_dir=profile_dir,
            )
            process = self._launcher.launch(spec)
            if profile_lease is not None:
                profile_lease.record_process(process.pid)
            _record_owned_browser_process(
                (artifact_dir,) if profile_lease is not None else (profile_dir, artifact_dir),
                session_id=session_id,
                process=process,
            )
            websocket_url = self._wait_for_devtools_endpoint(profile_dir, process, cancel=cancel)
            egress_proxy.deny_endpoint("127.0.0.1", _trusted_devtools_endpoint_port(websocket_url))
            transport = self._transport_factory.connect(
                websocket_url,
                timeout=self._config.operation_timeout_seconds,
                cancel=cancel,
            )
            target = transport.call(
                "Target.createTarget",
                {
                    "url": "about:blank",
                    "newWindow": False,
                    "background": self._config.linux_display is None,
                },
                timeout=self._config.operation_timeout_seconds,
                cancel=cancel,
            )
            target_id = _required_cdp_string(target, "targetId")
            attached = transport.call(
                "Target.attachToTarget",
                {"targetId": target_id, "flatten": True},
                timeout=self._config.operation_timeout_seconds,
                cancel=cancel,
            )
            cdp_session_id = _required_cdp_string(attached, "sessionId")
            # Ordinary clicks must not write untracked files. Only the bounded
            # download operation temporarily admits and collects an owned file.
            transport.call(
                "Browser.setDownloadBehavior",
                {"behavior": "deny", "eventsEnabled": False},
                timeout=self._config.operation_timeout_seconds,
                cancel=cancel,
            )
            for method in ("Page.enable", "DOM.enable", "Runtime.enable", "Network.enable"):
                transport.call(
                    method,
                    {"maxPostDataSize": 1} if method == "Network.enable" else {},
                    session_id=cdp_session_id,
                    timeout=self._config.operation_timeout_seconds,
                    cancel=cancel,
                )
            if not egress_proxy.healthy:
                raise BrowserLaunchError(
                    "The managed browser egress proxy stopped during session startup."
                )
            session = _BrowserSession(
                session_id=session_id,
                owner_id=owner,
                executable=executable,
                process=process,
                egress_proxy=egress_proxy,
                transport=transport,
                cdp_session_id=cdp_session_id,
                root_cdp_session_id=cdp_session_id,
                root_target_id=target_id,
                active_target_id=target_id,
                profile_dir=profile_dir,
                artifact_dir=artifact_dir,
                created_at=self._clock(),
                allow_local_destinations=bool(allow_local_destinations),
                local_destinations_loopback_only=bool(local_destinations_loopback_only),
                owned_preview_origin_allowed=owned_preview_origin_allowed,
                profile_lease=profile_lease,
            )
            with self._lock:
                self._sessions[session_id] = session
            return self._status(session)
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserLaunchError(
                "The managed browser session could not be initialized."
            ) from exc
        finally:
            succeeded = session_id in self._sessions
            self._release_start(owner)
            if not succeeded:
                cleanup_failed = False
                if egress_proxy is not None:
                    try:
                        egress_proxy.close(timeout=self._config.shutdown_grace_seconds)
                    except Exception:
                        cleanup_failed = True
                if transport is not None:
                    try:
                        transport.close(timeout=self._config.shutdown_grace_seconds)
                    except Exception:
                        cleanup_failed = True
                if process is not None:
                    try:
                        self._terminator.terminate(
                            process, grace_seconds=self._config.shutdown_grace_seconds
                        )
                    except Exception:
                        cleanup_failed = True
                if profile_dir is not None and profile_lease is None:
                    cleanup_failed = (
                        not _remove_owned_session_dir(self._profiles_root, profile_dir, session_id)
                        or cleanup_failed
                    )
                if artifact_dir is not None:
                    cleanup_failed = (
                        not _remove_owned_session_dir(
                            self._artifacts_root, artifact_dir, session_id
                        )
                        or cleanup_failed
                    )
                if cleanup_failed:
                    raise BrowserLaunchError(
                        "Browser startup failed and owned resource cleanup is incomplete."
                    )
                _release_visible_display(self._config.linux_display, session_id)

    def navigate(
        self,
        owner_id: str,
        session_id: str,
        url: str,
        *,
        timeout: float | None = None,
        cancel: CancelCheck = None,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        session = self._owned_session(owner_id, session_id)
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ) as generation:
            deadline = _bounded_timeout(timeout, self._config.operation_timeout_seconds)

            def authorize(candidate: str) -> str:
                return validate_browser_url(
                    candidate,
                    allow_local_destinations=session.allow_local_destinations,
                    local_destinations_loopback_only=(session.local_destinations_loopback_only),
                    resolver=self._resolver,
                    resolution_timeout=self._config.dns_timeout_seconds,
                    owned_preview_origin_allowed=session.owned_preview_origin_allowed,
                )

            normalized = authorize(url)
            result = session.transport.guarded_navigate(
                normalized,
                session_id=session.cdp_session_id,
                authorize_url=authorize,
                timeout=deadline,
                cancel=cancel,
                root_target_id=session.root_target_id,
            )
            session.active_url = normalized
            return {
                "session_id": session.session_id,
                "command_generation": generation,
                "url": normalized,
                "result": _bounded_payload(result, self._config.max_snapshot_bytes),
            }

    def _pages(self, session: _BrowserSession, timeout: float, cancel: CancelCheck):
        handles = dict(
            session.transport.guarded_page_sessions(session_id=session.root_cdp_session_id)
        )
        handles[session.root_target_id] = session.root_cdp_session_id
        result = session.transport.call("Target.getTargets", timeout=timeout, cancel=cancel)
        targets = result.get("targetInfos")
        if not isinstance(targets, list) or len(targets) > 64:
            raise BrowserSecurityError("Browser page inventory exceeded its limit.")
        pages = []
        for target in targets:
            if not isinstance(target, Mapping):
                raise BrowserSecurityError("Browser page inventory was invalid.")
            page_id = target.get("targetId")
            if target.get("type") != "page" or page_id not in handles:
                continue
            pages.append(
                {
                    "page_id": page_id,
                    "url": str(target.get("url", ""))[:8192],
                    "title": str(target.get("title", ""))[:512],
                    "active": page_id == session.active_target_id,
                }
            )
        if len(pages) > 8:
            raise BrowserLimitError("Browser page limit reached (8 pages).")
        return pages, handles

    def pages(self, owner_id: str, session_id: str, *, cancel: CancelCheck = None):
        session = self._owned_session(owner_id, session_id)
        with self._operation(session, cancel=cancel) as generation:
            pages, _ = self._pages(session, self._config.operation_timeout_seconds, cancel)
            return {"pages": pages, "command_generation": generation}

    def open_window(self, owner_id: str, session_id: str, *, cancel: CancelCheck = None) -> bool:
        """Open a blank browser window for the person at the computer screen.

        The Browser button on the owl computer's bar uses it (bots/browser_linux.py). It is not a
        model tool: it opens no website and grants nothing that Ctrl+N in the visible browser does
        not. Never waits behind an operation; returns whether a window was opened.
        """
        session = self._owned_session(owner_id, session_id)
        _check_cancel(cancel)
        with session.control_lock:
            _assert_browser_running(session)
            if not session.operation_lock.acquire(blocking=False):
                return False
        try:
            session.transport.call(
                "Target.createTarget",
                {"url": "about:blank", "newWindow": True},
                timeout=self._config.operation_timeout_seconds,
                cancel=cancel,
            )
            return True
        finally:
            with session.control_lock:
                session.operation_lock.release()

    def switch_page(
        self,
        owner_id: str,
        session_id: str,
        page_id: str,
        *,
        expected_generation: int,
        cancel: CancelCheck = None,
    ) -> None:
        session = self._owned_session(owner_id, session_id)
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ):
            timeout = self._config.operation_timeout_seconds
            pages, handles = self._pages(session, timeout, cancel)
            page = next((page for page in pages if page["page_id"] == page_id), None)
            if page is None:
                raise BrowserPageUnavailableError("Observed browser page is no longer available.")
            child = handles[page_id]
            for method in ("Page.enable", "DOM.enable", "Runtime.enable", "Network.enable"):
                session.transport.call(
                    method,
                    {"maxPostDataSize": 1} if method == "Network.enable" else {},
                    session_id=child,
                    timeout=timeout,
                    cancel=cancel,
                )
            session.transport.call(
                "Page.bringToFront", session_id=child, timeout=timeout, cancel=cancel
            )
            with session.control_lock:
                _check_cancel(cancel)
                _require_generation(session, expected_generation)
                session.cdp_session_id = child
                session.active_target_id = page_id
                session.active_url = page["url"]
                session.command_generation += 1

    def close_page(
        self,
        owner_id: str,
        session_id: str,
        page_id: str,
        *,
        expected_generation: int,
        cancel: CancelCheck = None,
    ) -> dict[str, Any]:
        """Close one guarded tab without accepting a discard-changes dialog.

        Serialized with downloads and other browser operations. Keep the final
        page so the session remains usable until its normal task/idle cleanup.
        A closed active page requires an explicit switch to a remaining page.
        """
        session = self._owned_session(owner_id, session_id)
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ):
            timeout = self._config.operation_timeout_seconds
            pages, handles = self._pages(session, timeout, cancel)
            if page_id not in {page["page_id"] for page in pages}:
                raise BrowserPageUnavailableError("Observed browser page is no longer available.")
            if len(pages) == 1:
                return {"closed": False, "reason": "last_page_retained"}
            child = handles[page_id]
            session.transport.call("Page.enable", session_id=child, timeout=timeout, cancel=cancel)
            with session.control_lock:
                _check_cancel(cancel)
                _require_generation(session, expected_generation)
                session.command_generation += 1
            try:
                session.transport.call(
                    "Page.close", session_id=child, timeout=timeout, cancel=cancel
                )
            except BrowserPageUnavailableError:
                # A detach may arrive before the command reply. Verify below;
                # never repeat an uncertain close or force Target.closeTarget.
                pass
            deadline = time.monotonic() + min(timeout, 2.0)
            while True:
                _check_cancel(cancel)
                pages, _ = self._pages(session, timeout, cancel)
                if page_id not in {page["page_id"] for page in pages}:
                    return {"closed": True, "reason": "closed"}
                events = session.transport.drain_events(
                    session_id=child, max_events=256, timeout=0.05, cancel=cancel
                )
                for event in events:
                    if event.get("method") == "Page.javascriptDialogOpening":
                        session.transport.call(
                            "Page.handleJavaScriptDialog",
                            {"accept": False},
                            session_id=child,
                            timeout=timeout,
                            cancel=cancel,
                        )
                        return {"closed": False, "reason": "dialog_preserved_page"}
                if time.monotonic() >= deadline:
                    return {"closed": False, "reason": "close_not_confirmed"}

    def snapshot(
        self,
        owner_id: str,
        session_id: str,
        *,
        kind: str = "semantic",
        timeout: float | None = None,
        cancel: CancelCheck = None,
    ) -> dict[str, Any]:
        session = self._owned_session(owner_id, session_id)
        normalized_kind = str(kind).strip().lower()
        if normalized_kind not in {"semantic", "accessibility", "dom", "text"}:
            raise BrowserValidationError("Browser snapshot kind is invalid.")
        with self._operation(session, cancel=cancel) as generation:
            result = self._capture_snapshot(session, normalized_kind, timeout, cancel)
            return {**result, "command_generation": generation}

    def _capture_snapshot(
        self,
        session: _BrowserSession,
        normalized_kind: str,
        timeout: float | None,
        cancel: CancelCheck,
    ) -> dict[str, Any]:
        call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
        if normalized_kind == "text":
            raw = session.transport.call(
                "Runtime.evaluate",
                {
                    "expression": "document.body ? document.body.innerText : ''",
                    "returnByValue": True,
                    "awaitPromise": False,
                },
                session_id=session.cdp_session_id,
                timeout=call_timeout,
                cancel=cancel,
            )
            value = raw.get("result")
            if isinstance(value, Mapping):
                value = value.get("value", "")
            return {
                "session_id": session.session_id,
                "kind": "text",
                **_bounded_text(str(value or ""), self._config.max_snapshot_bytes),
            }
        if normalized_kind == "dom":
            raw = session.transport.call(
                "DOMSnapshot.captureSnapshot",
                {
                    "computedStyles": [],
                    "includePaintOrder": False,
                    "includeDOMRects": False,
                    "includeBlendedBackgroundColors": False,
                    "includeTextColorOpacities": False,
                },
                session_id=session.cdp_session_id,
                timeout=call_timeout,
                cancel=cancel,
            )
        else:
            raw = session.transport.call(
                "Accessibility.getFullAXTree",
                {"depth": 32},
                session_id=session.cdp_session_id,
                timeout=call_timeout,
                cancel=cancel,
            )
        return {
            "session_id": session.session_id,
            "kind": normalized_kind,
            **_bounded_payload(raw, self._config.max_snapshot_bytes),
        }

    def screenshot(
        self,
        owner_id: str,
        session_id: str,
        *,
        full_page: bool = False,
        timeout: float | None = None,
        cancel: CancelCheck = None,
    ) -> BrowserArtifact:
        session = self._owned_session(owner_id, session_id)
        with self._operation(session, cancel=cancel):
            params: dict[str, Any] = {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": bool(full_page),
            }
            raw = session.transport.call(
                "Page.captureScreenshot",
                params,
                session_id=session.cdp_session_id,
                timeout=_bounded_timeout(timeout, self._config.operation_timeout_seconds),
                cancel=cancel,
            )
            encoded = raw.get("data")
            if not isinstance(encoded, str):
                raise BrowserSecurityError("Browser screenshot response was invalid.")
            estimated = (len(encoded) * 3) // 4
            if estimated > self._config.max_screenshot_bytes + 4:
                raise BrowserSecurityError("Browser screenshot exceeded the configured limit.")
            try:
                payload = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise BrowserSecurityError("Browser screenshot response was invalid.") from exc
            if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
                raise BrowserSecurityError("Browser screenshot was not a PNG image.")
            if len(payload) > self._config.max_screenshot_bytes:
                raise BrowserSecurityError("Browser screenshot exceeded the configured limit.")
            filename = f"screenshot-{session.artifact_count + 1:04d}-{secrets.token_hex(4)}.png"
            artifact_dir = _assert_owned_session_dir(
                self._artifacts_root, session.artifact_dir, session.session_id
            )
            path = _safe_artifact_path(artifact_dir, filename)
            _write_private_file(path, payload)
            session.artifact_count += 1
            return BrowserArtifact(
                artifact_id=f"browser:{session.session_id}:{filename}",
                relative_path=f"{artifact_dir.name}/{filename}",
                media_type="image/png",
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )

    def _capture_visual_frame(
        self, session: _BrowserSession, generation: int, *, timeout: float, cancel: CancelCheck
    ) -> BrowserVisualFrame:
        options = dict(session_id=session.cdp_session_id, timeout=timeout, cancel=cancel)
        tree = session.transport.call("Page.getFrameTree", {}, **options)
        url = tree.get("frameTree", {}).get("frame", {}).get("url")
        metrics = session.transport.call("Page.getLayoutMetrics", {}, **options)
        viewport = metrics.get("cssLayoutViewport", {})
        css_width, css_height = viewport.get("clientWidth"), viewport.get("clientHeight")
        if not isinstance(url, str) or any(
            type(v) not in (int, float) or not math.isfinite(v) or not 1 <= v <= 8192
            for v in (css_width, css_height)
        ):
            raise BrowserSecurityError("Invalid browser frame dimensions or location.")
        raw = session.transport.call(
            "Page.captureScreenshot",
            {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
            **options,
        )
        encoded = raw.get("data")
        # Keep one bounded frame in memory; this path creates no disk artifacts.
        limit = min(self._config.max_screenshot_bytes, 1024 * 1024)
        if not isinstance(encoded, str) or len(encoded) > ((limit + 2) // 3) * 4:
            raise BrowserLimitError("Visual frame exceeds the image allowance.")
        try:
            png = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise BrowserSecurityError("Invalid browser frame encoding.") from None
        if len(png) < 33 or len(png) > limit or png[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
            raise BrowserSecurityError("Invalid browser PNG frame.")
        width, height = struct.unpack(">II", png[16:24])
        if not 1 <= width <= 8192 or not 1 <= height <= 8192:
            raise BrowserLimitError("Visual frame dimensions exceed the allowance.")
        after = session.transport.call("Page.getFrameTree", {}, **options)
        if after.get("frameTree", {}).get("frame", {}).get("url") != url:
            raise BrowserStaleFrameError("Page changed during capture; take a new screenshot.")
        return BrowserVisualFrame(
            session.owner_id,
            session.session_id,
            generation,
            time.monotonic(),
            url,
            width,
            height,
            css_width,
            css_height,
            png,
        )

    def capture_visual_frame(
        self,
        owner_id: str,
        session_id: str,
        *,
        timeout: float | None = None,
        cancel: CancelCheck = None,
    ) -> BrowserVisualFrame:
        session = self._owned_session(owner_id, session_id)
        with self._operation(session, cancel=cancel) as generation:
            return self._capture_visual_frame(
                session,
                generation,
                timeout=_bounded_timeout(timeout, self._config.operation_timeout_seconds),
                cancel=cancel,
            )

    def click_visual_frame(
        self,
        owner_id: str,
        session_id: str,
        frame: BrowserVisualFrame,
        x: int,
        y: int,
        *,
        timeout: float | None = None,
        cancel: CancelCheck = None,
    ) -> dict[str, Any]:
        session = self._owned_session(owner_id, session_id)
        if frame.owner_id != owner_id or frame.session_id != session_id:
            raise BrowserOwnershipError("Visual frame belongs to another browser.")
        if (
            type(x) is not int
            or type(y) is not int
            or not (0 <= x < frame.width and 0 <= y < frame.height)
        ):
            raise BrowserValidationError("Coordinates must be inside the observed image.")
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=frame.command_generation
        ) as generation:
            if frame.consumed or not 0 <= time.monotonic() - frame.created_at <= 60:
                raise BrowserStaleFrameError(
                    "Visual frame expired or was used; take a new screenshot."
                )
            # Consume even when the outcome is uncertain. A new frame is needed
            # for every action; copying model arguments cannot replay this one.
            frame.consumed = True
            call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
            current = self._capture_visual_frame(
                session, generation, timeout=call_timeout, cancel=cancel
            )
            if (
                current.url != frame.url
                or (current.css_width, current.css_height) != (frame.css_width, frame.css_height)
                or current.png != frame.png
            ):
                raise BrowserStaleFrameError(
                    "The visible page changed; no click was sent. Take a new screenshot."
                )
            # PNG pixels can differ from CSS pixels on a scaled display.
            params = {
                "x": x * frame.css_width / frame.width,
                "y": y * frame.css_height / frame.height,
                "button": "left",
                "clickCount": 1,
            }
            try:
                session.transport.call(
                    "Input.dispatchMouseEvent",
                    {**params, "type": "mousePressed"},
                    session_id=session.cdp_session_id,
                    timeout=call_timeout,
                    cancel=cancel,
                )
            finally:
                session.transport.call(
                    "Input.dispatchMouseEvent",
                    {**params, "type": "mouseReleased"},
                    session_id=session.cdp_session_id,
                    timeout=call_timeout,
                )
            _check_cancel(cancel)
            return {
                "session_id": session_id,
                "command_generation": generation,
                "dispatched": True,
                "verification_required": True,
            }

    def diagnostics(
        self,
        owner_id: str,
        session_id: str,
        *,
        max_events: int | None = None,
        timeout: float | None = None,
        cancel: CancelCheck = None,
    ) -> dict[str, Any]:
        session = self._owned_session(owner_id, session_id)
        if max_events is None:
            limit = self._config.max_diagnostic_events
        elif isinstance(max_events, bool) or not isinstance(max_events, int):
            raise BrowserValidationError("Browser diagnostic event limit is invalid.")
        else:
            limit = max_events
        if not 1 <= limit <= self._config.max_diagnostic_events:
            raise BrowserValidationError("Browser diagnostic event limit is invalid.")
        with self._operation(session, cancel=cancel):
            events = session.transport.drain_events(
                session_id=session.cdp_session_id,
                max_events=limit + 1,
                timeout=_bounded_timeout(timeout, self._config.operation_timeout_seconds),
                cancel=cancel,
            )
            allowed = {
                "Runtime.consoleAPICalled": "console",
                "Log.entryAdded": "console",
                "Network.loadingFailed": "network",
                "Network.responseReceived": "network",
            }
            output: list[dict[str, Any]] = []
            for event in events:
                method = str(event.get("method") or "")
                category = allowed.get(method)
                if category is None:
                    continue
                output.append(
                    {
                        "category": category,
                        "method": method,
                        "params": _bounded_payload(
                            _sanitize_diagnostic_params(method, event.get("params", {})),
                            min(64 * 1024, self._config.max_snapshot_bytes),
                        ),
                    }
                )
                if len(output) >= limit:
                    break
            return {
                "session_id": session.session_id,
                "events": output,
                "truncated": len(events) > limit,
                "max_events": limit,
            }

    def read_artifact(
        self,
        owner_id: str,
        session_id: str,
        artifact_id: str,
        *,
        offset: int = 0,
        max_bytes: int = 256 * 1024,
    ) -> dict[str, Any]:
        session = self._owned_session(owner_id, session_id)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise BrowserValidationError("Browser artifact offset is invalid.")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 1 <= max_bytes <= _MAX_ARTIFACT_CHUNK_BYTES
        ):
            raise BrowserValidationError("Browser artifact chunk size is invalid.")
        parts = str(artifact_id).split(":", 2)
        if (
            len(parts) != 3
            or parts[0] != "browser"
            or parts[1] != session.session_id
            or _SCREENSHOT_NAME_RE.fullmatch(parts[2]) is None
        ):
            raise BrowserValidationError("Browser artifact id is invalid.")
        artifact_dir = _assert_owned_session_dir(
            self._artifacts_root, session.artifact_dir, session.session_id
        )
        path = _safe_artifact_path(artifact_dir, parts[2])
        try:
            size = path.stat().st_size
            if not path.is_file() or path.is_symlink() or size > self._config.max_screenshot_bytes:
                raise BrowserSecurityError("Browser artifact is not a trusted screenshot.")
            if offset > size:
                raise BrowserValidationError("Browser artifact offset exceeds its size.")
            with path.open("rb") as handle:
                handle.seek(offset)
                payload = handle.read(max_bytes)
        except BrowserError:
            raise
        except OSError as exc:
            raise BrowserNotFoundError("Browser artifact was not found.") from exc
        next_offset = offset + len(payload)
        return {
            "artifact_id": artifact_id,
            "media_type": "image/png",
            "encoding": "base64",
            "content": base64.b64encode(payload).decode("ascii"),
            "offset": offset,
            "next_offset": next_offset,
            "size_bytes": size,
            "truncated": next_offset < size,
        }

    def click(
        self,
        owner_id: str,
        session_id: str,
        selector: str,
        *,
        expected_backend_node_id: int | None = None,
        timeout: float | None = None,
        cancel: CancelCheck = None,
        expected_generation: int | None = None,
        label_backend_node_ids: Sequence[int] = (),
    ) -> dict[str, Any]:
        """``label_backend_node_ids``: the target's own label (from a verified DOM snapshot, not
        page script), where a click activates the target as HTML labels do. Styled checkboxes
        and radios often show only their label, with the real input underneath."""
        session = self._owned_session(owner_id, session_id)
        selector_value = _validated_selector(selector)
        if (
            not isinstance(label_backend_node_ids, (list, tuple))
            or len(label_backend_node_ids) > 512
            or any(type(item) is not int or item <= 0 for item in label_backend_node_ids)
        ):
            raise BrowserValidationError("Browser label nodes are invalid.")
        if label_backend_node_ids and expected_backend_node_id is None:
            raise BrowserValidationError("Label nodes need the target's identity.")
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ) as generation:
            call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
            self._dispatch_click(
                session,
                selector_value,
                call_timeout,
                cancel,
                expected_backend_node_id,
                tuple(label_backend_node_ids),
            )
            return {
                "session_id": session.session_id,
                "command_generation": generation,
                "clicked": True,
            }

    def _dispatch_click(
        self,
        session,
        selector_value,
        call_timeout,
        cancel,
        expected_backend_node_id,
        label_nodes: tuple[int, ...] = (),
    ):
        node_id = self._query_node(
            session,
            selector_value,
            call_timeout,
            cancel,
            expected_backend_node_id=expected_backend_node_id,
        )
        # Mouse coordinates are viewport-relative. Scrolling must precede
        # measuring the box, including when an ancestor is scrollable.
        session.transport.call(
            "DOM.scrollIntoViewIfNeeded",
            {"nodeId": node_id},
            session_id=session.cdp_session_id,
            timeout=call_timeout,
            cancel=cancel,
        )
        model = session.transport.call(
            "DOM.getBoxModel",
            {"nodeId": node_id},
            session_id=session.cdp_session_id,
            timeout=call_timeout,
            cancel=cancel,
        )
        x, y = _box_center(model)
        if expected_backend_node_id is not None:
            x, y = round(x), round(y)
            try:
                self._assert_click_target(session, node_id, x, y, call_timeout, cancel, label_nodes)
            except BrowserTargetObscuredError:
                # A styled checkbox or radio hides its input under its own label: click the label
                # where it shows, as a person would; the browser passes that click to the input.
                point = self._label_point(session, label_nodes, call_timeout, cancel)
                if point is None:
                    raise
                x, y = point
        for event_type, button in (("mousePressed", "left"), ("mouseReleased", "left")):
            session.transport.call(
                "Input.dispatchMouseEvent",
                {"type": event_type, "x": x, "y": y, "button": button, "clickCount": 1},
                session_id=session.cdp_session_id,
                timeout=call_timeout,
                cancel=cancel,
            )

    @staticmethod
    def _hit(session, x, y, options) -> object:
        # The box model and mouse events use viewport coordinates, but getNodeForLocation
        # hit-tests the document. Scrolling the target into view usually moves the page, so
        # without the page offset the test lands above the control (often on <body>) and every
        # click on a scrolled page was refused as covered.
        viewport = session.transport.call("Page.getLayoutMetrics", {}, **options).get(
            "cssVisualViewport"
        )
        return session.transport.call(
            "DOM.getNodeForLocation",
            {
                "x": x + _page_offset(viewport, "pageX"),
                "y": y + _page_offset(viewport, "pageY"),
                "includeUserAgentShadowDOM": False,
                "ignorePointerEventsNone": False,
            },
            **options,
        ).get("backendNodeId")

    @classmethod
    def _label_point(cls, session, label_nodes, timeout, cancel):
        """Where a click lands on the target's own label, checked by Chromium's hit test."""
        options = dict(session_id=session.cdp_session_id, timeout=timeout, cancel=cancel)
        # Labels come first in their list (then what they contain); a few lookups find them.
        for backend in label_nodes[:32]:
            node = session.transport.call(
                "DOM.describeNode", {"backendNodeId": backend}, **options
            ).get("node", {})
            if str(node.get("nodeName", "")).upper() != "LABEL":
                continue
            session.transport.call(
                "DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend}, **options
            )
            model = session.transport.call("DOM.getBoxModel", {"backendNodeId": backend}, **options)
            x, y = (round(value) for value in _box_center(model))
            hit = cls._hit(session, x, y, options)
            if type(hit) is int and hit in label_nodes:
                return x, y
        return None

    @classmethod
    def _assert_click_target(cls, session, node_id, x, y, timeout, cancel, label_nodes=()):
        # A valid node can still sit behind another control. Chromium's hit test
        # and DOM identities cannot be overridden by page JavaScript. A nested
        # label/span is legitimate; an unrelated overlay is not. This only
        # fences the instant before dispatch, not future page event handlers.
        options = dict(session_id=session.cdp_session_id, timeout=timeout, cancel=cancel)
        hit = cls._hit(session, x, y, options)
        tree = session.transport.call(
            "DOM.describeNode", {"nodeId": node_id, "depth": -1, "pierce": False}, **options
        ).get("node", {})
        pending = [tree]
        while type(hit) is int and hit > 0 and pending:
            node = pending.pop()
            if node.get("backendNodeId") == hit:
                return
            pending.extend(node.get("children", []))
        if type(hit) is int and hit in label_nodes:
            return  # the control's own label: the browser forwards the click to the control
        raise BrowserTargetObscuredError(
            "The observed control is covered or no longer receives clicks. No click was sent."
        )

    def download(
        self,
        owner_id: str,
        session_id: str,
        selector: str,
        *,
        max_bytes: int,
        collect=None,
        expected_backend_node_id: int | None = None,
        expected_generation: int | None = None,
        cancel: CancelCheck = None,
    ):
        """Click once and collect one completed download from this owned page.

        Files use Chromium GUID names inside a fresh trusted directory. Neither a
        remote filename nor a caller path can select host files. Partial downloads
        are never returned; failure consumes the action and requires inspection.
        """
        if type(max_bytes) is not int or not 1 <= max_bytes <= 50 * 1024 * 1024:
            raise BrowserValidationError("Download byte limit is invalid.")
        if collect is None and max_bytes > 2 * 1024 * 1024:
            raise BrowserValidationError("Large downloads require a streaming collector.")
        session = self._owned_session(owner_id, session_id)
        selector_value = _validated_selector(selector)
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ):
            transport = session.transport
            if not callable(getattr(transport, "drain_browser_events", None)):
                raise BrowserValidationError("This browser transport cannot observe downloads.")
            call_timeout = self._config.operation_timeout_seconds
            frame_tree = transport.call(
                "Page.getFrameTree",
                {},
                session_id=session.cdp_session_id,
                timeout=call_timeout,
                cancel=cancel,
            )
            frame_id = frame_tree["frameTree"]["frame"]["id"]
            transport.drain_browser_events(max_events=2000, timeout=0.05, cancel=cancel)
            directory = _assert_owned_session_dir(
                self._artifacts_root, session.artifact_dir, session.session_id
            )
            with tempfile.TemporaryDirectory(prefix="download-", dir=directory) as temporary:
                pending = set()
                completed = None
                try:
                    transport.call(
                        "Browser.setDownloadBehavior",
                        {
                            "behavior": "allowAndName",
                            "downloadPath": temporary,
                            "eventsEnabled": True,
                        },
                        timeout=call_timeout,
                        cancel=cancel,
                    )
                    self._dispatch_click(
                        session, selector_value, call_timeout, cancel, expected_backend_node_id
                    )
                    deadline = time.monotonic() + max(call_timeout, 120)
                    while completed is None:
                        _check_cancel(cancel)
                        if time.monotonic() >= deadline:
                            raise BrowserTimeoutError(
                                "Download did not complete. The click was sent once; inspect before retrying."
                            )
                        for event in transport.drain_browser_events(
                            max_events=200, timeout=0.05, cancel=cancel
                        ):
                            params = event.get("params", {})
                            if event.get("method") == "Alysis.eventsDropped":
                                raise BrowserLimitError(
                                    "Download events were lost; completion is unverified."
                                )
                            if event.get("method") == "Browser.downloadWillBegin":
                                guid = params.get("guid")
                                if (
                                    not isinstance(guid, str)
                                    or re.fullmatch(r"[A-Za-z0-9-]{1,100}", guid) is None
                                ):
                                    raise BrowserSecurityError("Invalid download identity.")
                                pending.add(guid)
                                if params.get("frameId") != frame_id or len(pending) != 1:
                                    raise BrowserSecurityError(
                                        "Download did not match one file from the observed page."
                                    )
                            elif event.get("method") == "Browser.downloadProgress":
                                guid = params.get("guid")
                                if guid not in pending:
                                    raise BrowserSecurityError(
                                        "Download progress has no owned request."
                                    )
                                if (
                                    params.get("receivedBytes", 0) > max_bytes
                                    or params.get("totalBytes", 0) > max_bytes
                                ):
                                    raise BrowserLimitError("Download exceeds its byte allowance.")
                                if params.get("state") == "canceled":
                                    raise BrowserError(
                                        "Download was cancelled; no complete file was saved."
                                    )
                                if params.get("state") == "completed":
                                    completed = guid
                    descriptor = os.open(
                        Path(temporary) / completed, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                    )
                    try:
                        info = os.fstat(descriptor)
                        if (
                            not stat.S_ISREG(info.st_mode)
                            or info.st_nlink != 1
                            or info.st_size > max_bytes
                        ):
                            raise BrowserSecurityError("Download is not a bounded regular file.")

                        def chunks():
                            received = 0
                            while data := os.read(descriptor, 256 * 1024):
                                _check_cancel(cancel)
                                received += len(data)
                                if received > max_bytes:
                                    raise BrowserLimitError(
                                        "Downloaded file exceeded its allowance."
                                    )
                                yield data
                            _check_cancel(cancel)
                            after = os.fstat(descriptor)
                            if received != info.st_size or (info.st_mtime_ns, info.st_ctime_ns) != (
                                after.st_mtime_ns,
                                after.st_ctime_ns,
                            ):
                                raise BrowserSecurityError(
                                    "Downloaded bytes changed before collection."
                                )

                        return collect(chunks(), info.st_size) if collect else b"".join(chunks())
                    finally:
                        os.close(descriptor)
                finally:
                    for guid in pending:
                        if guid != completed:
                            with suppress(BrowserError):
                                transport.call(
                                    "Browser.cancelDownload", {"guid": guid}, timeout=call_timeout
                                )
                    transport.call(
                        "Browser.setDownloadBehavior",
                        {"behavior": "deny", "eventsEnabled": False},
                        timeout=call_timeout,
                    )

    def upload_file(
        self,
        owner_id,
        session_id,
        selector,
        *,
        name,
        size,
        chunks,
        expected_sha256,
        expected_backend_node_id=None,
        expected_generation=None,
        cancel=None,
    ):
        """Select an owned library file in one currently observed file input.

        Selection itself can trigger website JavaScript, so the caller must
        authorize sharing these bytes with the current website before dispatch.
        Retain the owned file until browser cleanup because form upload can be lazy.
        """
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 160
            or name in {".", ".."}
            or any(c in name for c in "/\\")
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
        ):
            raise BrowserValidationError("Upload requires a simple library file name.")
        if type(size) is not int or not 0 <= size <= 50 * 1024 * 1024:
            raise BrowserValidationError("Upload exceeds 50 MiB.")
        session = self._owned_session(owner_id, session_id)
        with self._operation(
            session, mutation=True, expected_generation=expected_generation, cancel=cancel
        ) as generation:
            if session.upload_bytes + size > 100 * 1024 * 1024 or session.upload_count >= 100:
                raise BrowserLimitError("This browser's temporary file allowance is full.")
            root = _assert_owned_session_dir(
                self._artifacts_root, session.artifact_dir, session.session_id
            )
            staging = Path(tempfile.mkdtemp(prefix="upload-", dir=root))
            retained = False
            try:
                path = staging / name
                digest, received = hashlib.sha256(), 0
                with path.open("xb") as output:
                    for data in chunks:
                        _check_cancel(cancel)
                        if not isinstance(data, bytes) or len(data) > 256 * 1024:
                            raise BrowserValidationError("Invalid bounded upload chunk.")
                        received += len(data)
                        if received > size:
                            raise BrowserLimitError("Upload exceeded its declared size.")
                        output.write(data)
                        digest.update(data)
                if received != size or digest.hexdigest() != expected_sha256:
                    raise BrowserSecurityError("Upload source bytes did not match their receipt.")
                timeout = self._config.operation_timeout_seconds
                node = self._query_node(
                    session,
                    _validated_selector(selector),
                    timeout,
                    cancel,
                    expected_backend_node_id=expected_backend_node_id,
                )
                description = session.transport.call(
                    "DOM.describeNode",
                    {"nodeId": node},
                    session_id=session.cdp_session_id,
                    timeout=timeout,
                    cancel=cancel,
                ).get("node", {})
                attributes = description.get("attributes", [])
                attrs = dict(zip(attributes[::2], attributes[1::2], strict=True))
                if (
                    description.get("nodeName") != "INPUT"
                    or attrs.get("type", "").lower() != "file"
                ):
                    raise BrowserValidationError("The observed target is no longer a file input.")
                _check_cancel(cancel)
                register = getattr(session.transport, "allow_file_upload", None)
                if not callable(register):
                    raise BrowserValidationError(
                        "This browser transport cannot safely observe file uploads."
                    )
                register(session.cdp_session_id, size)
                session.transport.call(
                    "DOM.setFileInputFiles",
                    {"files": [str(path)], "nodeId": node},
                    session_id=session.cdp_session_id,
                    timeout=timeout,
                    cancel=cancel,
                )
                retained = True
                session.upload_bytes += size
                session.upload_count += 1
                session.artifact_count += 1
                return {
                    "command_generation": generation,
                    "selected": True,
                    "bytes": size,
                    "sha256": digest.hexdigest(),
                    "verification_required": True,
                }
            finally:
                if not retained:
                    shutil.rmtree(staging)

    def type_text(
        self,
        owner_id: str,
        session_id: str,
        selector: str,
        text: str,
        *,
        replace: bool = True,
        expected_backend_node_id: int | None = None,
        timeout: float | None = None,
        cancel: CancelCheck = None,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        session = self._owned_session(owner_id, session_id)
        selector_value = _validated_selector(selector)
        if not isinstance(text, str) or len(text) > _TYPE_MAX_CHARS:
            raise BrowserValidationError("Browser input text is invalid or too long.")
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ) as generation:
            call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
            node_id = self._query_node(
                session,
                selector_value,
                call_timeout,
                cancel,
                expected_backend_node_id=expected_backend_node_id,
            )
            session.transport.call(
                "DOM.focus",
                {"nodeId": node_id},
                session_id=session.cdp_session_id,
                timeout=call_timeout,
                cancel=cancel,
            )
            self._assert_focused_node(session, node_id, call_timeout, cancel)
            if replace:
                modifier = 4 if os.sys.platform == "darwin" else 2  # Meta or Control CDP mask.
                session.transport.call(
                    "Input.dispatchKeyEvent",
                    {
                        "type": "keyDown",
                        "key": "a",
                        "code": "KeyA",
                        "modifiers": modifier,
                        "windowsVirtualKeyCode": 65,
                        "commands": ["selectAll"],
                    },
                    session_id=session.cdp_session_id,
                    timeout=call_timeout,
                    cancel=cancel,
                )
                session.transport.call(
                    "Input.dispatchKeyEvent",
                    {
                        "type": "keyUp",
                        "key": "a",
                        "code": "KeyA",
                        "modifiers": modifier,
                        "windowsVirtualKeyCode": 65,
                    },
                    session_id=session.cdp_session_id,
                    timeout=call_timeout,
                    cancel=cancel,
                )
                self._assert_focused_node(session, node_id, call_timeout, cancel)
                session.transport.call(
                    "Input.dispatchKeyEvent",
                    {
                        "type": "keyDown",
                        "key": "Backspace",
                        "code": "Backspace",
                        "windowsVirtualKeyCode": 8,
                    },
                    session_id=session.cdp_session_id,
                    timeout=call_timeout,
                    cancel=cancel,
                )
                session.transport.call(
                    "Input.dispatchKeyEvent",
                    {
                        "type": "keyUp",
                        "key": "Backspace",
                        "code": "Backspace",
                        "windowsVirtualKeyCode": 8,
                    },
                    session_id=session.cdp_session_id,
                    timeout=call_timeout,
                    cancel=cancel,
                )
            self._assert_focused_node(session, node_id, call_timeout, cancel)
            session.transport.call(
                "Input.insertText",
                {"text": text},
                session_id=session.cdp_session_id,
                timeout=call_timeout,
                cancel=cancel,
            )
            return {
                "session_id": session.session_id,
                "command_generation": generation,
                "typed": True,
                "character_count": len(text),
            }

    def select_option(
        self,
        owner_id: str,
        session_id: str,
        selector: str,
        option_selector: str,
        *,
        expected_backend_node_id: int | None = None,
        expected_option_backend_node_id: int | None = None,
        timeout: float | None = None,
        cancel: CancelCheck = None,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        session = self._owned_session(owner_id, session_id)
        selectors = (_validated_selector(selector), _validated_selector(option_selector))
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ) as generation:
            call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
            options = dict(session_id=session.cdp_session_id, timeout=call_timeout, cancel=cancel)
            group = "alysis-select-" + secrets.token_hex(8)
            objects = []
            try:
                for selected, identity in zip(
                    selectors,
                    (expected_backend_node_id, expected_option_backend_node_id),
                    strict=True,
                ):
                    node_id = self._query_node(
                        session, selected, call_timeout, cancel, expected_backend_node_id=identity
                    )
                    resolved = session.transport.call(
                        "DOM.resolveNode", {"nodeId": node_id, "objectGroup": group}, **options
                    )
                    object_id = resolved.get("object", {}).get("objectId")
                    if not isinstance(object_id, str) or not object_id:
                        raise BrowserNotFoundError("Observed control is no longer available.")
                    objects.append(object_id)
                result = session.transport.call(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": objects[0],
                        "functionDeclaration": _SELECT_OPTION_FUNCTION,
                        "arguments": [{"objectId": objects[1]}],
                        "returnByValue": True,
                    },
                    **options,
                )
                if result.get("exceptionDetails") or result.get("result", {}).get("value") != {
                    "dispatched": True
                }:
                    raise BrowserValidationError(
                        "Selection failed; inspect the current page before retrying."
                    )
            finally:
                # Cancellation must not retain remote handles. A failed release
                # is bounded by session disposal, and must not mask input status.
                with suppress(BrowserError):
                    session.transport.call(
                        "Runtime.releaseObjectGroup",
                        {"objectGroup": group},
                        session_id=session.cdp_session_id,
                        timeout=call_timeout,
                    )
            return {"session_id": session_id, "command_generation": generation, "dispatched": True}

    def press_key(
        self,
        owner_id: str,
        session_id: str,
        selector: str,
        key: str,
        *,
        shift: bool = False,
        expected_backend_node_id: int | None = None,
        timeout: float | None = None,
        cancel: CancelCheck = None,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        session = self._owned_session(owner_id, session_id)
        selector = _validated_selector(selector)
        if key not in _PAGE_KEYS or type(shift) is not bool:
            raise BrowserValidationError("Unsupported page key or modifier.")
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ) as generation:
            call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
            options = dict(session_id=session.cdp_session_id, timeout=call_timeout, cancel=cancel)
            node_id = self._query_node(
                session,
                selector,
                call_timeout,
                cancel,
                expected_backend_node_id=expected_backend_node_id,
            )
            session.transport.call("DOM.focus", {"nodeId": node_id}, **options)
            self._assert_focused_node(session, node_id, call_timeout, cancel)
            key_value, key_code = _PAGE_KEYS[key]
            params = {
                "key": key_value,
                "code": key,
                "windowsVirtualKeyCode": key_code,
                "modifiers": 8 if shift else 0,
            }
            try:
                down = {**params, "type": "keyDown"}
                if key in {"Enter", "Space"}:
                    down["text"] = "\r" if key == "Enter" else " "
                session.transport.call("Input.dispatchKeyEvent", down, **options)
            finally:
                # Even when cancellation happens between key events, release
                # the same key. Do not leave it pressed in a reusable session.
                session.transport.call(
                    "Input.dispatchKeyEvent",
                    {**params, "type": "keyUp"},
                    session_id=session.cdp_session_id,
                    timeout=call_timeout,
                )
            _check_cancel(cancel)
            return {"session_id": session_id, "command_generation": generation, "dispatched": True}

    def scroll(
        self,
        owner_id: str,
        session_id: str,
        selector: str,
        *,
        delta_x: int = 0,
        delta_y: int,
        expected_backend_node_id: int | None = None,
        timeout: float | None = None,
        cancel: CancelCheck = None,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        session = self._owned_session(owner_id, session_id)
        selector = _validated_selector(selector)
        if any(type(delta) is not int or abs(delta) > 2000 for delta in (delta_x, delta_y)) or not (
            delta_x or delta_y
        ):
            raise BrowserValidationError("Scroll needs a nonzero bounded pixel delta.")
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ) as generation:
            call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
            options = dict(session_id=session.cdp_session_id, timeout=call_timeout, cancel=cancel)
            node_id = self._query_node(
                session,
                selector,
                call_timeout,
                cancel,
                expected_backend_node_id=expected_backend_node_id,
            )
            session.transport.call("DOM.scrollIntoViewIfNeeded", {"nodeId": node_id}, **options)
            box = session.transport.call("DOM.getBoxModel", {"nodeId": node_id}, **options)
            x, y = _box_center(box)
            metrics = session.transport.call("Page.getLayoutMetrics", {}, **options)
            viewport = metrics.get("cssLayoutViewport", {})
            width, height = viewport.get("clientWidth"), viewport.get("clientHeight")
            if any(
                type(v) not in (int, float) or not math.isfinite(v) or v < 2
                for v in (width, height)
            ):
                raise BrowserSecurityError("Browser viewport response was invalid.")
            session.transport.call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": max(1, min(x, width - 1)),
                    "y": max(1, min(y, height - 1)),
                    "deltaX": delta_x,
                    "deltaY": delta_y,
                },
                **options,
            )
            return {"session_id": session_id, "command_generation": generation, "dispatched": True}

    @staticmethod
    def _assert_focused_node(
        session: _BrowserSession, node_id: int, timeout: float, cancel: CancelCheck
    ) -> None:
        # DOM.focus can synchronously trigger page handlers which redirect focus.
        # Inspect Chromium's actual accessibility focus, not page-overridable JS.
        options = dict(session_id=session.cdp_session_id, timeout=timeout, cancel=cancel)
        node = session.transport.call("DOM.describeNode", {"nodeId": node_id}, **options)
        identity = node.get("node", {}).get("backendNodeId")
        tree = session.transport.call(
            "Accessibility.getPartialAXTree",
            {"nodeId": node_id, "fetchRelatives": False},
            **options,
        )
        if type(identity) is not int or not any(
            item.get("backendDOMNodeId") == identity
            and any(
                p.get("name") == "focused" and p.get("value", {}).get("value") is True
                for p in item.get("properties", [])
            )
            for item in tree.get("nodes", [])
        ):
            raise BrowserControlError(
                "Focus moved away from the observed target; keyboard input stopped. Inspect the current result before retrying."
            )

    def status(self, owner_id: str, session_id: str) -> BrowserSessionStatus:
        return self._status(self._owned_session(owner_id, session_id))

    def claim_viewer_control(self, owner_id: str, session_id: str) -> tuple[str, str]:
        """Reserve one input viewer for this owned visible session.

        This trusted claim is not an input grant: pause/drain must still succeed
        before launching a viewer. Resume is blocked until the claim is released.
        """
        session = self._owned_session(owner_id, session_id)
        with session.control_lock:
            _assert_browser_running(session)
            if self._config.linux_display is None:
                raise BrowserControlError("Browser session has no local X11 display.")
            if session.viewer_lease is not None:
                raise BrowserControlError("Browser session already has an input viewer claim.")
            session.viewer_lease = secrets.token_urlsafe(24)
            return self._config.linux_display, session.viewer_lease

    def release_viewer_control(self, owner_id: str, session_id: str, lease: str) -> None:
        """Trusted coordinator confirms that its viewer and held input are gone."""
        session = self._owned_session(owner_id, session_id)
        with session.control_lock:
            if session.viewer_lease is None or not secrets.compare_digest(
                session.viewer_lease, lease
            ):
                raise BrowserControlError("Browser viewer claim does not match.")
            session.viewer_lease = None

    def pause_commands(
        self,
        owner_id: str,
        session_id: str,
        *,
        timeout: float | None = None,
        cancel: CancelCheck = None,
    ) -> BrowserSessionStatus:
        """Fence new commands, then drain the active operation without cancelling it.

        A timeout or cancellation after fencing leaves admission closed. Each
        request supersedes earlier pause/resume requests. This only controls this
        service's commands: it neither disables a viewer nor rolls back input.
        """
        session = self._owned_session(owner_id, session_id)
        deadline = self._monotonic() + _bounded_timeout(
            timeout, self._config.operation_timeout_seconds
        )
        _check_cancel(cancel)
        with session.control_lock:
            _assert_browser_running(session)
            session.commands_paused = True
            session.command_generation += 1
            generation = session.command_generation
        while True:
            _check_cancel(cancel)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise BrowserTimeoutError(
                    "Browser commands are paused; active operation did not drain in time."
                )
            if not session.operation_lock.acquire(timeout=min(0.05, remaining)):
                continue
            # Admission remains fenced while we release the operation lock and
            # record the drained status. A newer pause must not be acknowledged.
            with session.control_lock:
                try:
                    _check_cancel(cancel)
                    _assert_browser_running(session)
                    _require_generation(session, generation)
                finally:
                    session.operation_lock.release()
                return self._status(session)

    def resume_commands(
        self,
        owner_id: str,
        session_id: str,
        *,
        expected_generation: int,
        timeout: float | None = None,
        cancel: CancelCheck = None,
    ) -> dict[str, Any]:
        """Capture a fresh DOM before reopening admission for a new generation.

        Trusted coordinator primitive, not a model tool. The coordinator must
        first independently confirm viewer input is disabled. Capture failure,
        cancellation, shutdown or a newer pause leaves admission closed.
        """
        session = self._owned_session(owner_id, session_id)
        call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
        _check_cancel(cancel)
        with session.control_lock:
            _assert_browser_running(session)
            _require_generation(session, expected_generation)
            if session.viewer_lease is not None:
                raise BrowserControlError("Browser input viewer has not been revoked.")
            if not session.commands_paused:
                raise BrowserControlError("Browser commands are not paused.")
            if not session.operation_lock.acquire(blocking=False):
                raise BrowserLimitError(
                    "Browser operation has not drained; commands remain paused."
                )
        try:
            result = self._capture_snapshot(session, "dom", call_timeout, cancel)
            with session.control_lock:
                _check_cancel(cancel)
                _assert_browser_running(session)
                _require_generation(session, expected_generation)
                if session.viewer_lease is not None:
                    raise BrowserControlError("Browser input viewer has not been revoked.")
                session.command_generation += 1
                session.commands_paused = False
                return {**result, "command_generation": session.command_generation}
        finally:
            with session.control_lock:
                session.operation_lock.release()

    def list(self, owner_id: str) -> tuple[BrowserSessionStatus, ...]:
        owner = _validated_owner(owner_id)
        with self._lock:
            sessions = [session for session in self._sessions.values() if session.owner_id == owner]
        return tuple(
            sorted(
                (self._status(session) for session in sessions), key=lambda item: item.created_at
            )
        )

    def close(self, owner_id: str, session_id: str, *, delete_artifacts: bool = True) -> bool:
        owner = _validated_owner(owner_id)
        sid = _validated_session_id(session_id)
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                return False
            if session.owner_id != owner:
                raise BrowserOwnershipError("Browser session is not owned by this caller.")
        with session.control_lock:
            if session.viewer_lease is not None:
                raise BrowserControlError("Revoke the input viewer before closing its browser.")
            if not session.operation_lock.acquire(blocking=False):
                raise BrowserLimitError(
                    "Browser session has an active operation and cannot close yet."
                )
            session.closing = True
            session.commands_paused = True
            session.command_generation += 1
        try:
            cleanup_failed = False
            self._shutdown_persistent_profile(session)
            try:
                session.egress_proxy.close(timeout=self._config.shutdown_grace_seconds)
            except Exception:
                cleanup_failed = True
            try:
                session.transport.close(timeout=self._config.shutdown_grace_seconds)
            except Exception:
                cleanup_failed = True
            try:
                self._terminator.terminate(
                    session.process, grace_seconds=self._config.shutdown_grace_seconds
                )
            except Exception:
                cleanup_failed = True
            profile_removed = self._finish_profile(session, cleanup_failed)
            artifacts_removed = (
                _remove_owned_session_dir(self._artifacts_root, session.artifact_dir, sid)
                if delete_artifacts
                else True
            )
            if cleanup_failed or not profile_removed or not artifacts_removed:
                raise BrowserLaunchError(
                    "Browser stopped, but owned session data cleanup is incomplete; retry close."
                )
            with self._lock:
                if self._sessions.get(sid) is session:
                    self._sessions.pop(sid)
                    _release_visible_display(self._config.linux_display, sid)
            return True
        finally:
            session.operation_lock.release()

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        cleanup_failed = False
        for session in sessions:
            if session.profile_lease is not None:
                # Persistent storage cannot be declared clean while a command
                # still owns the browser. Use the same admission gate as close.
                try:
                    self.close(session.owner_id, session.session_id)
                except Exception:
                    cleanup_failed = True
                continue
            with session.control_lock:
                if session.viewer_lease is not None:
                    cleanup_failed = True
                    continue
                session.closing = True
                session.commands_paused = True
                session.command_generation += 1
            session_failed = False
            try:
                session.egress_proxy.close(timeout=self._config.shutdown_grace_seconds)
            except Exception:
                session_failed = True
            try:
                session.transport.close(timeout=self._config.shutdown_grace_seconds)
            except Exception:
                session_failed = True
            try:
                self._terminator.terminate(
                    session.process, grace_seconds=self._config.shutdown_grace_seconds
                )
            except Exception:
                session_failed = True
            profile_removed = _remove_owned_session_dir(
                self._profiles_root, session.profile_dir, session.session_id
            )
            artifacts_removed = _remove_owned_session_dir(
                self._artifacts_root, session.artifact_dir, session.session_id
            )
            if session_failed or not profile_removed or not artifacts_removed:
                cleanup_failed = True
            else:
                with self._lock:
                    if self._sessions.get(session.session_id) is session:
                        self._sessions.pop(session.session_id)
                        _release_visible_display(self._config.linux_display, session.session_id)
        if cleanup_failed:
            raise BrowserLaunchError(
                "One or more browser sessions did not close cleanly; retry shutdown cleanup."
            )

    def _shutdown_persistent_profile(self, session: _BrowserSession) -> bool:
        if session.profile_lease is None:
            return True
        if not session.profile_shutdown_attempted:
            session.profile_shutdown_attempted = True
            # A previously crashed/exited process is not evidence of our clean
            # shutdown. Only the live browser can flush its profile via CDP.
            if session.process.poll() is None:
                try:
                    session.transport.call(
                        "Browser.close", {}, timeout=self._config.shutdown_grace_seconds
                    )
                except Exception:
                    # Chrome may close the socket before replying. Its exit and
                    # the absence of descendants, not that reply, are decisive.
                    pass
                deadline = self._monotonic() + self._config.shutdown_grace_seconds
                while self._monotonic() < deadline:
                    if session.process.poll() == 0 and _profile_process_group_stopped(
                        session.process
                    ):
                        session.profile_shutdown_clean = True
                        break
                    if session.process.poll() not in (None, 0):
                        break
                    self._sleep(0.02)
        return session.profile_shutdown_clean

    def _finish_profile(self, session: _BrowserSession, cleanup_failed: bool) -> bool:
        if session.profile_lease is None:
            return _remove_owned_session_dir(
                self._profiles_root, session.profile_dir, session.session_id
            )
        # Keep data dirty after forced termination, but permit Chromium recovery
        # only once this owned process group is proved stopped.
        if cleanup_failed or not _profile_process_group_stopped(session.process):
            return False
        if session.profile_shutdown_clean:
            session.profile_lease.mark_clean()
        else:
            session.profile_lease.mark_stopped()
        return True

    def _reserve_start(self, owner: str) -> None:
        with self._lock:
            owned = sum(1 for item in self._sessions.values() if item.owner_id == owner)
            owner_starting = self._starting_by_owner.get(owner, 0)
            if len(self._sessions) + self._starting_total >= self._config.max_sessions_total:
                raise BrowserLimitError("The managed browser session limit has been reached.")
            if owned + owner_starting >= self._config.max_sessions_per_owner:
                raise BrowserLimitError("The managed browser owner session limit has been reached.")
            self._starting_total += 1
            self._starting_by_owner[owner] = owner_starting + 1

    def _release_start(self, owner: str) -> None:
        with self._lock:
            self._starting_total = max(0, self._starting_total - 1)
            remaining = self._starting_by_owner.get(owner, 1) - 1
            if remaining <= 0:
                self._starting_by_owner.pop(owner, None)
            else:
                self._starting_by_owner[owner] = remaining

    def _wait_for_devtools_endpoint(
        self, profile_dir: Path, process: ProcessHandle, *, cancel: CancelCheck
    ) -> str:
        deadline = self._monotonic() + self._config.startup_timeout_seconds
        active_port = profile_dir / "DevToolsActivePort"
        while self._monotonic() < deadline:
            _check_cancel(cancel)
            exit_code = process.poll()
            if exit_code is not None:
                raise BrowserLaunchError("The managed browser exited before DevTools was ready.")
            parsed = _read_active_port(active_port, profile_dir)
            if parsed is not None:
                port, browser_path = parsed
                return f"ws://127.0.0.1:{port}{browser_path}"
            self._sleep(min(0.05, max(0.0, deadline - self._monotonic())))
        raise BrowserTimeoutError("The managed browser did not become ready in time.")

    def _owned_session(self, owner_id: str, session_id: str) -> _BrowserSession:
        owner = _validated_owner(owner_id)
        sid = _validated_session_id(session_id)
        with self._lock:
            session = self._sessions.get(sid)
        if session is None:
            raise BrowserNotFoundError("Browser session was not found.")
        if session.owner_id != owner:
            raise BrowserOwnershipError("Browser session is not owned by this caller.")
        return session

    def _operation(
        self,
        session: _BrowserSession,
        *,
        cancel: CancelCheck,
        mutation: bool = False,
        expected_generation: int | None = None,
    ) -> _OperationGuard:
        return _OperationGuard(
            session, cancel=cancel, mutation=mutation, expected_generation=expected_generation
        )

    def _status(self, session: _BrowserSession) -> BrowserSessionStatus:
        if session.process.poll() is not None or not session.egress_proxy.healthy:
            state = "crashed"
        elif not session.transport.healthy:
            state = "disconnected"
        else:
            state = "running"
        with session.control_lock:
            return BrowserSessionStatus(
                session_id=session.session_id,
                owner_id=session.owner_id,
                product=session.executable.product,
                state=state,
                created_at=session.created_at,
                allow_local_destinations=session.allow_local_destinations,
                local_destinations_loopback_only=session.local_destinations_loopback_only,
                active_url=session.active_url,
                artifact_count=session.artifact_count,
                commands_paused=session.commands_paused,
                command_generation=session.command_generation,
                operation_active=session.operation_lock.locked(),
            )

    def _query_node(
        self,
        session: _BrowserSession,
        selector: str,
        timeout: float,
        cancel: CancelCheck,
        *,
        expected_backend_node_id: int | None = None,
    ) -> int:
        if expected_backend_node_id is not None and (
            type(expected_backend_node_id) is not int or expected_backend_node_id <= 0
        ):
            raise BrowserValidationError("Observed browser node identity is invalid.")
        document = session.transport.call(
            "DOM.getDocument",
            {"depth": 0, "pierce": False},
            session_id=session.cdp_session_id,
            timeout=timeout,
            cancel=cancel,
        )
        root = document.get("root")
        if not isinstance(root, Mapping):
            raise BrowserSecurityError("Browser DOM response was invalid.")
        root_id = _required_cdp_int(root, "nodeId")
        matched = session.transport.call(
            "DOM.querySelector",
            {"nodeId": root_id, "selector": selector},
            session_id=session.cdp_session_id,
            timeout=timeout,
            cancel=cancel,
        )
        node_id = _required_cdp_int(matched, "nodeId")
        if node_id <= 0:
            raise BrowserNotFoundError("Browser element was not found.")
        if expected_backend_node_id is not None:
            # Resolve CSS afresh, but do not let a stale selector silently target
            # a replacement node. The identity must come from this session's
            # original DOM snapshot. This is opt-in; it does not fence viewer
            # input or guarantee layout stability after the check.
            described = session.transport.call(
                "DOM.describeNode",
                {"nodeId": node_id, "depth": 0},
                session_id=session.cdp_session_id,
                timeout=timeout,
                cancel=cancel,
            )
            node = described.get("node")
            if not isinstance(node, Mapping):
                raise BrowserSecurityError("Browser DOM response was invalid.")
            if _required_cdp_int(node, "backendNodeId") != expected_backend_node_id:
                raise BrowserNotFoundError(
                    "Observed browser element changed; take a fresh snapshot."
                )
        return node_id


class _OperationGuard:
    def __init__(
        self,
        session: _BrowserSession,
        *,
        cancel: CancelCheck,
        mutation: bool,
        expected_generation: int | None,
    ) -> None:
        self._session = session
        self._cancel = cancel
        self._mutation = mutation
        self._expected_generation = expected_generation

    def __enter__(self) -> int:
        _check_cancel(self._cancel)
        with self._session.control_lock:
            _assert_browser_running(self._session)
            if self._session.commands_paused:
                raise BrowserControlError("Browser commands are paused.")
            if self._expected_generation is not None or (
                self._mutation and self._session.command_generation != 0
            ):
                _require_generation(self._session, self._expected_generation)
            _check_cancel(self._cancel)
            if not self._session.operation_lock.acquire(blocking=False):
                raise BrowserLimitError(
                    "Another browser operation is already running for this session."
                )
            return self._session.command_generation

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        with self._session.control_lock:
            self._session.operation_lock.release()


def _require_generation(session: _BrowserSession, expected: int | None) -> None:
    if type(expected) is not int or expected < 0 or expected != session.command_generation:
        raise BrowserControlError(
            "Browser command generation is missing or stale; take a fresh snapshot."
        )


def _assert_browser_running(session: _BrowserSession) -> None:
    if session.closing:
        raise BrowserControlError("The managed browser session is closing.")
    if session.process.poll() is not None:
        # The exit status tells support how it ended (for example killed for memory, or crashed).
        raise BrowserLaunchError(
            f"The managed browser process is no longer running (exit {session.process.returncode})."
        )
    if not session.egress_proxy.healthy:
        raise BrowserLaunchError("The managed browser egress proxy is no longer running.")
    if not session.transport.healthy:
        failure = getattr(session.transport, "failure", None)
        raise BrowserLaunchError(
            "The managed browser control connection is no longer available"
            + (
                f" ({type(failure).__name__}: {failure})."
                if isinstance(failure, BrowserError)
                else "."
            )
        )


def _validated_executable(candidate: Path) -> Path:
    try:
        path = candidate.resolve(strict=True)
        info = path.stat()
    except OSError as exc:
        raise BrowserValidationError("Browser executable does not exist.") from exc
    if not stat.S_ISREG(info.st_mode):
        raise BrowserValidationError("Browser executable path is not a regular file.")
    if os.name != "nt" and not os.access(path, os.X_OK):
        raise BrowserValidationError("Browser executable is not executable.")
    if os.name == "nt" and path.suffix.lower() != ".exe":
        raise BrowserValidationError("Browser executable must be an .exe file on Windows.")
    return path


def _product_for_path(path: Path) -> str:
    text = path.name.lower()
    if "edge" in text:
        return "edge"
    if "chromium" in text:
        return "chromium"
    return "chrome"


def _case_insensitive_get(environment: Mapping[str, str], key: str) -> str | None:
    for current, value in environment.items():
        if str(current).upper() == key:
            return str(value)
    return None


def _windows_taskkill_executable() -> Path | None:
    root_value = _case_insensitive_get(os.environ, "SYSTEMROOT")
    if not root_value:
        return None
    root = Path(root_value)
    if not root.is_absolute():
        return None
    try:
        resolved_root = root.resolve(strict=True)
        candidate = (resolved_root / "System32" / "taskkill.exe").resolve(strict=True)
        candidate.relative_to(resolved_root)
        if not candidate.is_file():
            return None
    except (OSError, ValueError):
        return None
    return candidate


def _prepare_data_root(root: Path, *, workspace_roots: Sequence[Path]) -> Path:
    data_root = Path(root).expanduser()
    if not data_root.is_absolute():
        raise BrowserValidationError("Managed browser data root must be absolute.")
    resolved_workspaces = tuple(Path(item).expanduser().resolve() for item in workspace_roots)
    candidate = data_root.resolve()
    for workspace in resolved_workspaces:
        if _is_within(candidate, workspace):
            raise BrowserSecurityError("Managed browser data root must be outside the workspace.")
    return _private_dir(candidate)


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise BrowserSecurityError("Managed browser data path is not a directory.")
    with suppress(OSError):
        resolved.chmod(0o700)
    return resolved


def _make_owned_session_dir(root: Path, session_id: str) -> Path:
    prefix = f"{session_id}-"
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=root)).resolve(strict=True)
    if not _is_within(path, root):
        raise BrowserSecurityError("Managed browser session path escaped its root.")
    with suppress(OSError):
        path.chmod(0o700)
    _write_owned_marker(
        path / ".alysis-owned",
        _OwnershipMarker(
            session_id=session_id,
            owner_pid=os.getpid(),
            owner_identity=_process_identity(os.getpid()),
            browser_pid=None,
            browser_identity=None,
        ),
    )
    return path


def _record_owned_browser_process(
    paths: Sequence[Path], *, session_id: str, process: ProcessHandle
) -> None:
    pid = process.pid
    if isinstance(pid, bool) or not isinstance(pid, int) or not 1 <= pid <= 2**31 - 1:
        raise BrowserLaunchError("Managed browser process identity is invalid.")
    identity = _process_identity(pid)
    if identity is None and isinstance(process, subprocess.Popen):
        raise BrowserLaunchError("Managed browser process identity could not be recorded safely.")
    for path in paths:
        marker_path = path / ".alysis-owned"
        marker = _read_owned_marker(marker_path)
        if marker.session_id != session_id:
            raise BrowserSecurityError("Managed browser ownership marker changed during startup.")
        _write_owned_marker(
            marker_path,
            _OwnershipMarker(
                session_id=session_id,
                owner_pid=marker.owner_pid,
                owner_identity=marker.owner_identity,
                browser_pid=pid if identity is not None else None,
                browser_identity=identity,
            ),
            replace=True,
        )


def _remove_owned_session_dir(root: Path, path: Path, session_id: str) -> bool:
    try:
        resolved = path.resolve(strict=True)
        marker = resolved / ".alysis-owned"
        if not _is_within(resolved, root) or marker.is_symlink():
            return False
        if _read_owned_marker(marker).session_id != session_id:
            return False
        for attempt in range(20):
            try:
                shutil.rmtree(resolved, onerror=_retry_readonly_remove)
                return True
            except FileNotFoundError:
                return True
            except OSError:
                if attempt == 19:
                    return False
                time.sleep(0.05)
    except (OSError, UnicodeError, ValueError):
        return not path.exists()
    return not resolved.exists()


def _write_owned_marker(path: Path, marker: _OwnershipMarker, *, replace: bool = False) -> None:
    payload = json.dumps(
        {
            "schema_version": _OWNERSHIP_MARKER_VERSION,
            "session_id": marker.session_id,
            "owner_pid": marker.owner_pid,
            "owner_identity": marker.owner_identity,
            "browser_pid": marker.browser_pid,
            "browser_identity": marker.browser_identity,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if not replace:
        _write_private_file(path, payload)
        return
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        _write_private_file(temporary, payload)
        os.replace(temporary, path)
    finally:
        with suppress(OSError):
            temporary.unlink()


def _read_owned_marker(marker: Path) -> _OwnershipMarker:
    """Return the session and owner PID from a trusted marker.

    Plain session-id markers are accepted for safe cleanup compatibility, but
    only versioned markers carry enough ownership evidence for crash scavenging.
    """

    raw = marker.read_text(encoding="ascii")
    if _SESSION_RE.fullmatch(raw):
        return _OwnershipMarker(raw, None, None, None, None)
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("schema_version") != _OWNERSHIP_MARKER_VERSION:
        raise ValueError("Unsupported browser ownership marker.")
    session_id = payload.get("session_id")
    owner_pid = payload.get("owner_pid")
    owner_identity = payload.get("owner_identity")
    browser_pid = payload.get("browser_pid")
    browser_identity = payload.get("browser_identity")
    if (
        not isinstance(session_id, str)
        or _SESSION_RE.fullmatch(session_id) is None
        or isinstance(owner_pid, bool)
        or not isinstance(owner_pid, int)
        or not 1 <= owner_pid <= 2**31 - 1
        or not isinstance(owner_identity, str)
        or not owner_identity
        or (
            browser_pid is not None
            and (
                isinstance(browser_pid, bool)
                or not isinstance(browser_pid, int)
                or not 1 <= browser_pid <= 2**31 - 1
                or not isinstance(browser_identity, str)
                or not browser_identity
            )
        )
        or (browser_pid is None and browser_identity is not None)
    ):
        raise ValueError("Invalid browser ownership marker.")
    return _OwnershipMarker(
        session_id=session_id,
        owner_pid=owner_pid,
        owner_identity=owner_identity,
        browser_pid=browser_pid,
        browser_identity=browser_identity,
    )


def _process_identity(pid: int) -> str | None:
    if isinstance(pid, bool) or not isinstance(pid, int) or not 1 <= pid <= 2**31 - 1:
        return None
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        open_process.restype = ctypes.c_void_p
        get_process_times = kernel32.GetProcessTimes
        get_process_times.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
        )
        get_process_times.restype = ctypes.c_int
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_int
        handle = open_process(0x00001000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            creation = ctypes.c_uint64()
            exit_time = ctypes.c_uint64()
            kernel = ctypes.c_uint64()
            user = ctypes.c_uint64()
            if not get_process_times(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            return f"windows-filetime:{creation.value}"
        finally:
            close_handle(handle)
    proc_stat = Path("/proc") / str(pid) / "stat"
    if proc_stat.is_file():
        try:
            raw = proc_stat.read_text(encoding="ascii")
            _head, separator, tail = raw.rpartition(")")
            fields = tail.strip().split()
            if separator and len(fields) > 19:
                return f"proc-start-ticks:{fields[19]}"
        except (OSError, UnicodeError):
            return None
    ps = Path("/bin/ps")
    if ps.is_file():
        try:
            result = subprocess.run(
                [str(ps), "-o", "lstart=", "-p", str(pid)],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=1.0,
                shell=False,
                **command_text(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        value = result.stdout.strip()
        if result.returncode == 0 and value:
            return f"ps-lstart:{value}"
    return None


def _process_is_alive(pid: int) -> bool:
    return _process_identity(pid) is not None


def _terminate_recovered_browser_process(pid: int, identity: str, *, grace_seconds: float) -> bool:
    if _process_identity(pid) != identity:
        return True
    if os.name == "nt":
        taskkill = _windows_taskkill_executable()
        if taskkill is None:
            return False
        for force in (False, True):
            if _process_identity(pid) != identity:
                return True
            args = [str(taskkill), "/PID", str(pid), "/T"]
            if force:
                args.append("/F")
            try:
                subprocess.run(
                    args,
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=grace_seconds,
                    shell=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            deadline = time.monotonic() + grace_seconds
            while time.monotonic() < deadline:
                if _process_identity(pid) != identity:
                    return True
                time.sleep(0.05)
        return _process_identity(pid) != identity
    try:
        if os.getpgid(pid) != pid or os.getpgrp() == pid:
            return False
    except (OSError, AttributeError):
        return _process_identity(pid) != identity
    with suppress(ProcessLookupError, OSError):
        os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if _process_identity(pid) != identity:
            break
        time.sleep(0.05)
    # The group can retain descendants after the leader exits. Its numeric pgid
    # remains reserved while those descendants exist, so this targets the exact
    # group originally validated above rather than a new process with a reused pid.
    with suppress(ProcessLookupError, OSError):
        os.killpg(pid, signal.SIGKILL)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if _process_identity(pid) != identity:
            return True
        time.sleep(0.05)
    return _process_identity(pid) != identity


def _scavenge_stale_owned_dirs(roots: Sequence[Path], *, grace_seconds: float) -> None:
    """Remove only versioned session directories whose owning process is gone."""

    failures: list[Path] = []
    terminated_browsers: set[tuple[int, str]] = set()
    for root in roots:
        trusted_root = root.resolve(strict=True)
        try:
            candidates = tuple(trusted_root.iterdir())
        except OSError as exc:
            raise BrowserSecurityError(
                "Managed browser stale-resource cleanup could not inspect its private root."
            ) from exc
        for candidate in candidates:
            try:
                if candidate.is_symlink() or not candidate.is_dir():
                    continue
                resolved = candidate.resolve(strict=True)
                if resolved.parent != trusted_root:
                    continue
                marker = resolved / ".alysis-owned"
                if marker.is_symlink() or not marker.is_file():
                    continue
                ownership = _read_owned_marker(marker)
                if not resolved.name.startswith(f"{ownership.session_id}-"):
                    continue
                if (
                    ownership.owner_pid is None
                    or ownership.owner_identity is None
                    or _process_identity(ownership.owner_pid) == ownership.owner_identity
                ):
                    continue
                if ownership.browser_pid is not None and ownership.browser_identity is not None:
                    browser_key = (ownership.browser_pid, ownership.browser_identity)
                    if browser_key not in terminated_browsers:
                        if not _terminate_recovered_browser_process(
                            ownership.browser_pid,
                            ownership.browser_identity,
                            grace_seconds=grace_seconds,
                        ):
                            failures.append(resolved)
                            continue
                        terminated_browsers.add(browser_key)
                if not _remove_owned_session_dir(trusted_root, resolved, ownership.session_id):
                    failures.append(resolved)
            except (OSError, UnicodeError, ValueError):
                # Unrecognized directories are never deleted. They may belong to
                # another version or have been tampered with.
                continue
    if failures:
        raise BrowserSecurityError(
            "Managed browser stale owned-resource cleanup is incomplete; retry startup."
        )


def _retry_readonly_remove(function: Callable[[str], Any], path: str, _error: Any) -> None:
    with suppress(OSError):
        os.chmod(path, stat.S_IWRITE)
    function(path)


def _assert_owned_session_dir(root: Path, path: Path, session_id: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        marker = resolved / ".alysis-owned"
        if not _is_within(resolved, root.resolve(strict=True)) or marker.is_symlink():
            raise BrowserSecurityError("Browser artifact directory is no longer trusted.")
        if _read_owned_marker(marker).session_id != session_id:
            raise BrowserSecurityError("Browser artifact directory is no longer trusted.")
    except BrowserSecurityError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise BrowserSecurityError("Browser artifact directory is no longer trusted.") from exc
    return resolved


def _validated_egress_proxy_endpoint(proxy: BrowserEgressProxy) -> tuple[str, int]:
    host = proxy.host
    port = proxy.port
    if (
        host != "127.0.0.1"
        or isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65_535
    ):
        raise BrowserSecurityError("Managed browser egress proxy endpoint is not trusted.")
    if not proxy.healthy:
        raise BrowserLaunchError("Managed browser egress proxy did not start safely.")
    return host, port


def _trusted_devtools_endpoint_port(websocket_url: str) -> int:
    try:
        parsed = urlsplit(websocket_url)
        port = parsed.port
    except ValueError as exc:
        raise BrowserSecurityError("Managed browser DevTools endpoint is not trusted.") from exc
    if (
        parsed.scheme != "ws"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65_535
        or not _DEVTOOLS_PATH_RE.fullmatch(parsed.path)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise BrowserSecurityError("Managed browser DevTools endpoint is not trusted.")
    return port


def _browser_arguments(
    profile_dir: Path, proxy_host: str, proxy_port: int, *, visible: bool = False
) -> tuple[str, ...]:
    if proxy_host != "127.0.0.1" or not 1 <= proxy_port <= 65_535:
        raise BrowserSecurityError("Managed browser egress proxy endpoint is not trusted.")
    return (
        *(("--ozone-platform=x11", "--start-maximized") if visible else ("--headless=new",)),
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=0",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        f"--proxy-server=http://{proxy_host}:{proxy_port}",
        "--proxy-bypass-list=<-loopback>",
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
        "--disable-quic",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-sync",
        "--metrics-recording-only",
        "--password-store=basic",
        "--disable-features=Translate,MediaRouter,OptimizationHints",
        "about:blank",
    )


def _sanitized_browser_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    env = source or os.environ
    allowed_names = {"LANG", "LC_ALL", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "WINDIR"}
    output: dict[str, str] = {}
    for key, value in env.items():
        if str(key).upper() in allowed_names and "\x00" not in str(value):
            output[str(key)] = str(value)
    return output


def _read_active_port(path: Path, profile_dir: Path) -> tuple[int, str] | None:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise BrowserSecurityError("DevTools endpoint metadata was not a regular file.")
        if info.st_size > _ACTIVE_PORT_MAX_BYTES:
            raise BrowserSecurityError("DevTools endpoint metadata was too large.")
        resolved = path.resolve(strict=True)
        if resolved.parent != profile_dir.resolve(strict=True):
            raise BrowserSecurityError("DevTools endpoint metadata escaped its profile.")
        flags = os.O_RDONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            current = os.fstat(descriptor)
            if not stat.S_ISREG(current.st_mode) or current.st_size > _ACTIVE_PORT_MAX_BYTES:
                raise BrowserSecurityError("DevTools endpoint metadata was invalid.")
            payload = os.read(descriptor, _ACTIVE_PORT_MAX_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(payload) > _ACTIVE_PORT_MAX_BYTES:
            raise BrowserSecurityError("DevTools endpoint metadata was too large.")
        text = payload.decode("ascii")
    except FileNotFoundError:
        return None
    except UnicodeError as exc:
        raise BrowserSecurityError("DevTools endpoint metadata was invalid.") from exc
    lines = text.splitlines()
    if len(lines) != 2:
        raise BrowserSecurityError("DevTools endpoint metadata was invalid.")
    try:
        port = int(lines[0])
    except ValueError as exc:
        raise BrowserSecurityError("DevTools endpoint port was invalid.") from exc
    browser_path = lines[1]
    if not 1 <= port <= 65_535 or not _DEVTOOLS_PATH_RE.fullmatch(browser_path):
        raise BrowserSecurityError("DevTools endpoint metadata was invalid.")
    return port, browser_path


def _resolve_addresses(
    host: str,
    port: int,
    *,
    resolver: AddressResolver | None,
    timeout: float,
) -> tuple[str, ...]:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return (host,)
    # A page with many sites can have several lookups pending; wait briefly for a slot rather
    # than refusing the request at once.
    if not _DNS_RESOLUTION_SLOTS.acquire(timeout=timeout):
        raise BrowserDestinationUnresolvedError(
            "Browser destination resolution capacity was exhausted."
        )
    completed = threading.Event()
    outcome: dict[str, Any] = {}

    def resolve() -> None:
        try:
            if resolver is not None:
                values = resolver(host, port)
            else:
                values = {
                    item[4][0]
                    for item in socket.getaddrinfo(
                        host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
                    )
                }
            result = tuple(dict.fromkeys(str(item) for item in values))
            for item in result:
                ipaddress.ip_address(item)
            outcome["result"] = result
        except BaseException as exc:  # noqa: BLE001 - converted to a safe error below
            outcome["error"] = exc
        finally:
            _DNS_RESOLUTION_SLOTS.release()
            completed.set()

    threading.Thread(target=resolve, name="alysis-browser-dns", daemon=True).start()
    if not completed.wait(timeout=timeout):
        raise BrowserDestinationTimeoutError("Browser destination resolution timed out.")
    if "error" in outcome:
        raise BrowserDestinationUnresolvedError(
            "Browser destination could not be resolved safely."
        ) from outcome["error"]
    return tuple(outcome.get("result", ()))


def _is_public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and (
        address.ipv4_mapped is not None
        or address.sixtofour is not None
        or address.teredo is not None
    ):
        return False
    return bool(address.is_global) and not any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def _is_safe_owned_preview_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and (
        address.ipv4_mapped is not None
        or address.sixtofour is not None
        or address.teredo is not None
    ):
        return False
    return not any(
        (
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def _owned_preview_origin_check(
    provider: PreviewUrlProvider | None,
) -> OwnedPreviewOriginAllowed:
    def allowed(scheme: str, host: str, port: int) -> bool:
        if provider is None:
            return False
        try:
            candidates = provider()
        except Exception:
            return False
        for index, raw in enumerate(candidates):
            if index >= 64:
                break
            try:
                parsed = urlsplit(str(raw or "").strip())
                candidate_scheme = parsed.scheme.lower()
                candidate_host = (parsed.hostname or "").rstrip(".").lower()
                candidate_host = candidate_host.encode("idna").decode("ascii")
                candidate_port = parsed.port or (443 if candidate_scheme == "https" else 80)
            except (TypeError, ValueError, UnicodeError):
                continue
            if (
                candidate_scheme in {"http", "https"}
                and parsed.username is None
                and parsed.password is None
                and (candidate_scheme, candidate_host, candidate_port) == (scheme, host, port)
            ):
                return True
        return False

    return allowed


def _validated_owner(owner_id: str) -> str:
    value = str(owner_id or "").strip()
    if not _OWNER_RE.fullmatch(value):
        raise BrowserValidationError("Browser owner id is invalid.")
    return value


def _validated_session_id(session_id: str) -> str:
    value = str(session_id or "").strip()
    if not _SESSION_RE.fullmatch(value):
        raise BrowserValidationError("Browser session id is invalid.")
    return value


def _validated_selector(selector: str) -> str:
    if not isinstance(selector, str) or not selector or len(selector) > _SELECTOR_MAX_CHARS:
        raise BrowserValidationError("Browser selector is invalid or too long.")
    if "\x00" in selector:
        raise BrowserValidationError("Browser selector is invalid or too long.")
    return selector


def _bounded_timeout(value: float | None, default: float) -> float:
    if value is None:
        return default
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not 0.05 <= value <= default
    ):
        raise BrowserValidationError("Browser operation timeout is invalid.")
    return float(value)


def _check_cancel(cancel: CancelCheck) -> None:
    if cancel is not None and cancel():
        raise BrowserCancelledError("Browser operation was cancelled.")


def _required_cdp_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > 256:
        raise BrowserSecurityError("Browser CDP response was invalid.")
    return value


def _required_cdp_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BrowserSecurityError("Browser CDP response was invalid.")
    return value


def _page_offset(viewport: Any, key: str) -> int:
    """The visual viewport's scroll position in the page, in whole CSS pixels (0 if unknown)."""
    value = viewport.get(key) if isinstance(viewport, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return 0
    return round(value)


def _box_center(payload: Mapping[str, Any]) -> tuple[float, float]:
    model = payload.get("model")
    if not isinstance(model, Mapping):
        raise BrowserSecurityError("Browser element box response was invalid.")
    quad = model.get("content") or model.get("border")
    if not isinstance(quad, Sequence) or isinstance(quad, str | bytes) or len(quad) != 8:
        raise BrowserSecurityError("Browser element box response was invalid.")
    try:
        numbers = [float(value) for value in quad]
    except (TypeError, ValueError) as exc:
        raise BrowserSecurityError("Browser element box response was invalid.") from exc
    if not all(math.isfinite(value) for value in numbers):
        raise BrowserSecurityError("Browser element box response was invalid.")
    return sum(numbers[0::2]) / 4.0, sum(numbers[1::2]) / 4.0


def _bounded_payload(value: Any, max_bytes: int) -> dict[str, Any]:
    safe = redact_secrets(redact_form_secrets(value))
    try:
        encoded = json.dumps(safe, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BrowserSecurityError("Browser CDP response was not JSON-compatible.") from exc
    if len(encoded) <= max_bytes:
        return {"data": safe, "truncated": False, "size_bytes": len(encoded)}
    preview = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return {
        "data": {"preview": redact_secrets(preview)},
        "truncated": True,
        "size_bytes": len(encoded),
    }


def _sanitize_diagnostic_params(method: str, value: Any) -> dict[str, Any]:
    """Return a useful CDP diagnostic allowlist without headers or credentials."""

    if not isinstance(value, Mapping):
        return {}

    def scalars(source: Mapping[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for name in names:
            if name not in source:
                continue
            item = source.get(name)
            if item is None or isinstance(item, str | int | float | bool):
                output[name] = redact_secrets(item)
        return output

    if method == "Runtime.consoleAPICalled":
        output = scalars(value, ("type", "executionContextId", "timestamp"))
        raw_args = value.get("args")
        if isinstance(raw_args, Sequence) and not isinstance(raw_args, str | bytes):
            # Console values and descriptions are intentionally omitted. A page
            # can echo text entered through browser.type into console.log(), and
            # recognizable-secret redaction cannot protect arbitrary user input.
            output["args"] = [
                scalars(
                    item,
                    ("type", "subtype"),
                )
                for item in raw_args[:50]
                if isinstance(item, Mapping)
            ]
        return output

    if method == "Log.entryAdded":
        entry = value.get("entry")
        if not isinstance(entry, Mapping):
            return {}
        output = scalars(
            entry,
            # Log text is another free-form console channel and is therefore
            # omitted for the same typed-input confidentiality boundary.
            ("source", "level", "lineNumber", "timestamp", "category"),
        )
        if "url" in entry:
            output["url"] = _public_diagnostic_url(entry.get("url"))
        return {"entry": output}

    if method == "Network.loadingFailed":
        return scalars(
            value,
            (
                "requestId",
                "timestamp",
                "type",
                "errorText",
                "canceled",
                "blockedReason",
            ),
        )

    if method == "Network.responseReceived":
        output = scalars(value, ("requestId", "loaderId", "timestamp", "type"))
        response = value.get("response")
        if isinstance(response, Mapping):
            safe_response = scalars(
                response,
                (
                    "status",
                    "statusText",
                    "mimeType",
                    "protocol",
                    "fromDiskCache",
                    "fromServiceWorker",
                    "fromPrefetchCache",
                    "encodedDataLength",
                    "remoteIPAddress",
                    "remotePort",
                    "securityState",
                ),
            )
            safe_response["url"] = _public_diagnostic_url(response.get("url"))
            output["response"] = safe_response
        return output

    return {}


def _public_diagnostic_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        if scheme not in {"http", "https"} or not hostname:
            return None
        host = f"[{hostname}]" if ":" in hostname else hostname
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        public = urlunsplit((scheme, host, parsed.path or "/", "", ""))
    except (TypeError, ValueError):
        return None
    return str(redact_secrets(public))


def _bounded_text(value: str, max_bytes: int) -> dict[str, Any]:
    safe = str(redact_secrets(value))
    encoded = safe.encode("utf-8")
    return {
        "text": encoded[:max_bytes].decode("utf-8", errors="ignore"),
        "truncated": len(encoded) > max_bytes,
        "size_bytes": len(encoded),
    }


def _safe_artifact_path(root: Path, filename: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", filename):
        raise BrowserSecurityError("Browser artifact name was invalid.")
    path = root / filename
    if path.parent.resolve(strict=True) != root.resolve(strict=True):
        raise BrowserSecurityError("Browser artifact path escaped its root.")
    return path


def _write_private_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
