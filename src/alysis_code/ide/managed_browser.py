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
import unicodedata
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as wait_for_futures
from contextlib import AbstractContextManager, nullcontext, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeAlias
from urllib.parse import unquote, urlsplit, urlunsplit

from ..branding import canonical_user_data_dir, env_get
from ..command_text import command_text
from .browser_egress_proxy import BrowserEgressProxy
from .browser_observation import _fold, _fold_marks, redact_form_secrets
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
    **{f"F{number}": (f"F{number}", 111 + number) for number in range(1, 13)},
}
# Modifier keys held for a key chord: CDP modifier bit, key name, code and virtual-key code.
_MODIFIERS = {
    "Alt": (1, "Alt", "AltLeft", 18),
    "Control": (2, "Control", "ControlLeft", 17),
    "Meta": (4, "Meta", "MetaLeft", 91),
    "Shift": (8, "Shift", "ShiftLeft", 16),
}
# macOS runs editing shortcuts as named commands rather than from the key itself; other platforms
# run them from the key. Keyboard semantics of the platform's main modifier, not page actions.
_EDITING_COMMANDS = {"a": "selectAll", "c": "copy", "x": "cut", "v": "paste", "z": "undo"}
# Events that diagnostics reports; operations that read events keep these for it.
_DIAGNOSTIC_METHODS = {
    "Runtime.consoleAPICalled": "console",
    "Log.entryAdded": "console",
    "Network.loadingFailed": "network",
    "Network.responseReceived": "network",
}
# What a page reports about an acting step (page_report): its own requests of these kinds (CDP
# resource types) that failed or answered with an error status, listed up to a limit and counted
# beyond it, and its uncaught script errors. Requests are remembered by id, the oldest forgotten
# first, so that a later failure can name the request it belongs to.
_REPORTED_KINDS = frozenset({"Document", "XHR", "Fetch"})
_REPORTED_REQUESTS = 5
_REPORTED_PATH_CHARS = 200
_TRACKED_REQUESTS = 512
_REPORT_DRAIN_EVENTS = 512
_REQUEST_METHOD_RE = re.compile(r"[A-Za-z]{1,16}")
_NET_ERROR_RE = re.compile(r"net::ERR_[A-Z0-9_]{1,64}")
# Ids in an address path: a UUID anywhere, then each run of letters and digits that is all
# digits, a hexadecimal run of 8 or more with a digit, or any 16 or more with a digit. A
# percent-escaped byte (a letter of another script, a space) is left as it is.
_PATH_UUID_RE = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
_PATH_TOKEN_RE = re.compile(r"%[0-9A-Fa-f]{2}|[0-9A-Za-z]+")
_HEX_RE = re.compile(r"[0-9A-Fa-f]+")
# How long a pending command may run before its page is checked for a JavaScript dialog that
# blocks it. Commands that finish sooner never wait for this.
_DIALOG_CHECK_SECONDS = 0.05
# Pause between checks of an element that is not ready yet, and of a page state being waited for.
_READY_POLL_SECONDS = 0.05
_WAIT_POLL_SECONDS = 0.1
_MAX_FRAMES = 64
_MAX_FRAME_HOPS = 16
_FRAME_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_KEYS_MAX_CHARS = 1_000
_WAIT_VALUE_MAX_CHARS = 2_000
_DIALOG_TEXT_MAX_BYTES = 4_096
_READ_VALUE_MAX_BYTES = 64 * 1024
# How much of a frame's address and origin an accessibility snapshot describes it with.
_FRAME_URL_CHARS = 512
# A DOM snapshot with frames also captures each out-of-process frame's documents: no new frame is
# started after this many seconds, and the page and its frames together hold at most the
# snapshot byte limit. A frame left out (over either limit, or gone while it was read) is counted
# in omitted_frames.
_FRAME_SNAPSHOT_SECONDS = 5.0
# The DOM snapshot the page and each frame are captured with: structure, text and form values.
_DOM_SNAPSHOT_PARAMS: Mapping[str, Any] = {
    "computedStyles": (),
    "includePaintOrder": False,
    "includeDOMRects": False,
    "includeBlendedBackgroundColors": False,
    "includeTextColorOpacities": False,
}
_ISOLATED_WORLD = "alysis-actions"
_CHANGED_ELEMENT = "Observed browser element changed; take a fresh snapshot."

# Fixed functions run in an isolated world, where page scripts cannot replace the DOM methods
# they use. ``document`` there is the frame the element was observed in.
_PROBE_FUNCTION = """function() {
    if (!this.isConnected || this.ownerDocument !== document) return {attached: false};
    if (this.nodeType !== Node.ELEMENT_NODE) return {attached: true, element: false};
    const box = this.getBoundingClientRect();
    return {attached: true, element: true, visible: box.width > 0 && box.height > 0 &&
        this.checkVisibility({visibilityProperty: true})};
}"""
_READ_FUNCTION = """function() {
    if (!this.isConnected || this.ownerDocument !== document) return {attached: false};
    if (this.nodeType !== Node.ELEMENT_NODE) return {attached: true, element: false};
    const type = (this.getAttribute('type') || '').toLowerCase();
    const tokens = (this.getAttribute('autocomplete') || '').toLowerCase().split(/\\s+/);
    const secret = type === 'password' || tokens.some(token => token === 'current-password' ||
        token === 'new-password' || token === 'one-time-code' || token.startsWith('cc-'));
    const found = {attached: true, element: true, secret: secret};
    if (this instanceof HTMLInputElement && (this.type === 'checkbox' || this.type === 'radio'))
        return {...found, kind: this.type, value: this.value,
            checked: this.indeterminate ? 'mixed' : this.checked};
    if (this instanceof HTMLInputElement || this instanceof HTMLTextAreaElement)
        return {...found, kind: 'field', value: this.value};
    if (this instanceof HTMLSelectElement)
        return {...found, kind: 'select', value: this.value,
            selected: Array.from(this.selectedOptions, option => option.text)};
    const role = (this.getAttribute('role') || '').trim().split(/\\s+/)[0].toLowerCase();
    if (['checkbox', 'switch', 'radio', 'menuitemcheckbox', 'menuitemradio'].includes(role)) {
        const state = (this.getAttribute('aria-checked') || '').trim().toLowerCase();
        return {...found, kind: role, value: this.textContent,
            checked: state === 'mixed' ? 'mixed' : state === 'true'};
    }
    if (this.isContentEditable) return {...found, kind: 'editable', value: this.innerText};
    return {...found, kind: 'text', value: this.innerText};
}"""
# Notes whether a native (HTML) drag starts from the element's page; read by _DRAG_STARTED_FUNCTION.
_DRAG_WATCH_FUNCTION = """function() {
    const view = this.ownerDocument.defaultView, watch = {started: false, event: null};
    const listener = event => { watch.started = true; watch.event = event; };
    view.addEventListener('dragstart', listener, {capture: true, once: true});
    watch.stop = () => {
        view.removeEventListener('dragstart', listener, {capture: true});
        return watch.started && !watch.event.defaultPrevented;
    };
    return watch;
}"""
_DRAG_STARTED_FUNCTION = "function() { return this.stop(); }"
_CHECKABLE_KINDS = {"checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio"}
_FILE_COUNT_FUNCTION = "function() { return this.files ? this.files.length : 0; }"

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
# Choose exactly the given options of a native multiple select (the others are unselected), run
# in an isolated world. Disabled options keep their state; the chosen ones are read back.
_SELECT_OPTIONS_FUNCTION = """function(...chosen) {
    if (!this.isConnected || this.tagName !== 'SELECT' || !this.multiple ||
        this.matches(':disabled')) return {dispatched: false, multiple: false};
    for (const option of chosen)
        if (!option.isConnected || option.tagName !== 'OPTION' ||
            option.closest('select') !== this || option.matches(':disabled'))
            return {dispatched: false, multiple: true};
    const wanted = new Set(chosen);
    let changed = false;
    for (const option of Array.from(this.options)) {
        if (option.matches(':disabled') || option.selected === wanted.has(option)) continue;
        option.selected = wanted.has(option);
        changed = true;
    }
    if (changed) {
        this.dispatchEvent(new Event('input', {bubbles: true}));
        this.dispatchEvent(new Event('change', {bubbles: true}));
    }
    return {dispatched: true, multiple: true, changed: changed,
        confirmed: chosen.every(option => option.selected),
        selected: Array.from(this.selectedOptions, option => option.text)};
}"""
# Mouse buttons a click may use, with their CDP ``buttons`` bit, and the most clicks in a row.
_MOUSE_BUTTONS = {"left": 1, "right": 2, "middle": 4}
_MAX_CLICK_COUNT = 3
_MAX_SELECTED_OPTIONS = 100
# Notes whether the page handles a right click (opens its own menu) and keeps the browser's own
# menu (back, reload, save, inspect) closed: the listener runs after the page's own listeners on
# its window and only prevents what the page left to the browser. Read by
# _CONTEXT_MENU_HANDLED_FUNCTION.
_CONTEXT_MENU_WATCH_FUNCTION = """function() {
    const view = this.ownerDocument.defaultView, watch = {fired: false, handled: false};
    const listener = event => {
        watch.fired = true; watch.handled = event.defaultPrevented; event.preventDefault();
    };
    view.addEventListener('contextmenu', listener, {once: true});
    watch.finish = () => {
        view.removeEventListener('contextmenu', listener);
        return {fired: watch.fired, handled: watch.handled};
    };
    return watch;
}"""
_CONTEXT_MENU_HANDLED_FUNCTION = "function() { return this.finish(); }"
# A middle click opens a link in a new tab. On anything else it does nothing useful, and on a
# text field Linux pastes the computer's selected text into it, so it is sent only to a link.
_IN_LINK_FUNCTION = """function() {
    return !this.isContentEditable && this.closest('a[href], area[href]') !== null;
}"""
# History entries of a page the browser would load again by sending a form: reloading one could
# send that form again (Chromium's DevTools reload never asks). An entry reloaded since is
# reported as a reload, which hides what it came from.
_FORM_ENTRY_TRANSITIONS = frozenset({"form_submit", "reload"})
# An image of one element or of the whole page: at most this many CSS pixels on a side, and the
# whole page from its top down to at most this height. The image is scaled down to fit the byte
# allowance (a few tries) rather than cut.
_PAGE_IMAGE_MAX_CSS = 4096
_FULL_PAGE_MAX_CSS_HEIGHT = 8000
_PAGE_IMAGE_ATTEMPTS = 3


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
    "BrowserDialogOpenError",
    "BrowserExecutable",
    "BrowserInputUnconfirmedError",
    "BrowserLaunchError",
    "BrowserLaunchSpec",
    "BrowserLimitError",
    "BrowserNoDialogError",
    "BrowserNotFoundError",
    "BrowserOwnershipError",
    "BrowserSecurityError",
    "BrowserSessionStatus",
    "BrowserTargetHiddenError",
    "BrowserTargetNotReadyError",
    "BrowserTargetObscuredError",
    "BrowserTargetUnstableError",
    "BrowserTimeoutError",
    "BrowserValidationError",
    "BrowserWaitTimeoutError",
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
_MAX_SITE_COOKIES = 10_000
_MAX_SITE_ORIGINS = 1_000
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


class BrowserTargetNotReadyError(BrowserControlError):
    """No input was sent: the observed element could not receive it within the bounded wait."""


class BrowserTargetObscuredError(BrowserTargetNotReadyError):
    """No click was sent because hit testing did not reach the observed element."""


class BrowserTargetHiddenError(BrowserTargetNotReadyError):
    """No input was sent because the observed element has no visible box on the page."""


class BrowserTargetUnstableError(BrowserTargetNotReadyError):
    """No input was sent because the observed element kept moving (an animation or layout)."""


class BrowserInputUnconfirmedError(BrowserControlError):
    """Input was sent once, but the page does not show the requested result."""


class BrowserWaitTimeoutError(BrowserTimeoutError):
    """The page did not reach the awaited state in time. Nothing was changed."""


class BrowserNoDialogError(BrowserValidationError):
    """No JavaScript dialog is open on the page, so none was answered."""


class BrowserDialogOpenError(BrowserControlError):
    """The page shows a JavaScript dialog and answers nothing until it is answered.

    Nothing was read or sent. ``dialog_type`` and ``dialog_message`` describe the dialog; the
    message is the page's own text (bounded and redacted), quoted as such. ``dialog_url`` and
    ``dialog_frame_id`` name the frame that shows it (its address, page data, and its frame id)
    when the browser reported them: a frame from another site can show its own dialog."""

    def __init__(
        self,
        dialog_type: str,
        dialog_message: str,
        dialog_url: str | None = None,
        dialog_frame_id: str | None = None,
    ) -> None:
        self.dialog_type = str(dialog_type)[:32] or "alert"
        self.dialog_message = _bounded_text(str(dialog_message), _DIALOG_TEXT_MAX_BYTES)["text"]
        self.dialog_url = _dialog_frame_url(dialog_url)
        self.dialog_frame_id = _dialog_frame_id(dialog_frame_id)
        shown = self.dialog_message[:500] + ("…" if len(self.dialog_message) > 500 else "")
        super().__init__(
            f'The page is showing a dialog ({self.dialog_type}) that says: "{shown}". Nothing '
            "was read. Answer it with handle_dialog, then observe the page again."
        )


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

    # Optional: ``guarded_frame_sessions(*, session_id) -> Mapping[str, str]`` names the guarded
    # out-of-process iframes inside a page session (frame id -> child session id, nested ones
    # too). Without it, elements in out-of-process frames are refused as unreachable.
    # Optional: ``open_dialog(*, session_id)`` reports the JavaScript dialog a page session shows
    # (from the browser's events, without a command), and ``yielding_to_dialogs()`` is a context
    # in which this thread's commands to such a page fail with BrowserDialogOpenError instead of
    # waiting. Without them, reads rely on dialogs seen in events this service read.

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
    # How long input waits for its element to show, stop moving and be uncovered (for example
    # while a menu opens or a loading overlay fades) before it is refused with the reason.
    actionable_wait_seconds: float = 5.0

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
            (self.actionable_wait_seconds, "element wait", 60.0),
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
    # The JavaScript dialog last seen opening and not yet closed: its CDP session and details.
    dialog: tuple[str, dict[str, Any]] | None = field(default=None, repr=False)
    # Diagnostic events read by other operations, kept for diagnostics: (CDP session, event).
    held_events: deque[tuple[str, Mapping[str, Any]]] = field(
        default_factory=lambda: deque(maxlen=_MAX_DIAGNOSTIC_EVENTS_HARD), repr=False
    )
    # Requests the active page started, by (CDP session, request id), for page reports.
    page_requests: OrderedDict[tuple[str, str], _PageRequest] = field(
        default_factory=OrderedDict, repr=False
    )
    # What the page reported since the acting step that opened it; None once read (page_report).
    page_report: _PageReport | None = field(default=None, repr=False)
    # History entries (page, entry id) that history(move="reload") reloaded from an entry that
    # was not a form's result: reloading them again sends no form.
    reloaded_entries: set[tuple[str, int]] = field(default_factory=set, repr=False)


@dataclass(frozen=True, slots=True)
class _PageRequest:
    """A request the page started, as the browser announced it: its method and address, its
    kind (CDP resource type), whether the browser counts it as the page's own site (None when
    the browser does not say), the address of the document it was made for, and when it started
    (seconds since the epoch, the browser's and this service's common system clock)."""

    method: str | None
    url: str
    kind: str | None
    same_site: bool | None
    document_url: str | None
    started: float


@dataclass(slots=True)
class _PageReport:
    """What one page (``cdp_session``) reported from ``opened_at`` on: its own failed requests
    (at most _REPORTED_REQUESTS, ``more`` counts the rest), their ids, and its uncaught errors."""

    cdp_session: str
    opened_at: float
    failed: list[dict[str, Any]] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    more: int = 0
    errors: int = 0


@dataclass(frozen=True, slots=True)
class _Element:
    """An element an operation acts on, found afresh in the current page.

    ``session`` is the CDP session whose DOM holds it: the page's, or an out-of-process
    iframe's. ``hosts`` are the out-of-process iframe elements around it, outermost first, each
    with the session that holds that iframe element. Input always goes to the page's session.
    """

    session: str
    frame_id: str | None
    backend_node_id: int
    hosts: tuple[tuple[str, int], ...] = ()

    @property
    def ref(self) -> dict[str, int]:
        return {"backendNodeId": self.backend_node_id}


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

    return _checked_browser_url(
        value,
        allow_local_destinations=allow_local_destinations,
        local_destinations_loopback_only=local_destinations_loopback_only,
        resolver=resolver,
        resolution_timeout=resolution_timeout,
        owned_preview_origin_allowed=owned_preview_origin_allowed,
        resolve_hostnames=True,
    )


def preflight_browser_start_url(value: str, recorded_previews: PreviewUrlProvider | None) -> None:
    """Reject locally decidable errors before approval. Navigation must still validate the URL."""
    _checked_browser_url(
        value,
        allow_local_destinations=False,
        local_destinations_loopback_only=False,
        resolver=None,
        resolution_timeout=2.0,
        owned_preview_origin_allowed=_owned_preview_origin_check(recorded_previews),
        resolve_hostnames=False,
    )


def _checked_browser_url(
    value: str,
    *,
    allow_local_destinations: bool,
    local_destinations_loopback_only: bool,
    resolver: AddressResolver | None,
    resolution_timeout: float,
    owned_preview_origin_allowed: OwnedPreviewOriginAllowed | None,
    resolve_hostnames: bool,
) -> str:
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
    if not allow_local_destinations or local_destinations_loopback_only:
        addresses = _resolve_addresses(
            ascii_host,
            effective_port,
            resolver=resolver,
            timeout=float(resolution_timeout),
            resolve_hostnames=resolve_hostnames,
        )
        # Only the pre-approval pass can defer an unresolved hostname. Navigation
        # always resolves it and checks every returned address.
        if addresses is not None:
            if local_destinations_loopback_only:
                if not addresses or any(
                    not (_is_public_address(address) or ipaddress.ip_address(address).is_loopback)
                    for address in addresses
                ):
                    raise BrowserSecurityError(
                        "Only public and loopback browser destinations are enabled."
                    )
            elif owned_preview_allowed:
                if not addresses or any(
                    not _is_safe_owned_preview_address(address) for address in addresses
                ):
                    raise BrowserSecurityError(
                        "Local and private browser destinations are disabled."
                    )
            else:
                if not addresses:
                    raise BrowserSecurityError("Browser destination could not be resolved safely.")
                if any(not _is_public_address(address) for address in addresses):
                    raise BrowserSecurityError(
                        "Local and private browser destinations are disabled."
                    )
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
                    # A lasting profile keeps website storage, not Chromium's caches: they
                    # live with this session's own files and go when it closes.
                    cache_dir=artifact_dir / "browser-cache" if profile_lease is not None else None,
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
                    "newWindow": self._config.linux_display is not None,
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
            if self._config.linux_display is not None:
                # A person can navigate or open windows before the first model
                # operation. Install page tracking and interception before the
                # visible browser is handed to the desktop.
                def authorize_initial_page(candidate: str) -> str:
                    if candidate == "about:blank":
                        return candidate
                    return validate_browser_url(
                        candidate,
                        allow_local_destinations=bool(allow_local_destinations),
                        local_destinations_loopback_only=bool(local_destinations_loopback_only),
                        resolver=self._resolver,
                        resolution_timeout=self._config.dns_timeout_seconds,
                        owned_preview_origin_allowed=owned_preview_origin_allowed,
                    )

                transport.guarded_navigate(
                    "about:blank",
                    session_id=cdp_session_id,
                    authorize_url=authorize_initial_page,
                    timeout=self._config.operation_timeout_seconds,
                    cancel=cancel,
                    root_target_id=target_id,
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

    def history(
        self,
        owner_id: str,
        session_id: str,
        move: str,
        *,
        timeout: float | None = None,
        cancel: CancelCheck = None,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        """Go ``"back"`` or ``"forward"`` in the active tab's history, or ``"reload"`` its page,
        and wait (bounded) until the page has moved and loaded.

        The page it goes to passes the same address policy as :meth:`navigate` before anything
        loads, and everything it loads passes the network guard installed on the page. Moving
        never sends a form: Chromium shows a page that was a form's result again from its cache
        (or an error page), and a reload of such a page (or of an entry reloaded since, which
        hides where it came from) is refused, because Chromium would send the form again. A
        leave-page question the page asks is answered stay (the move is then reported as not
        made); any other dialog is dismissed and reported. Returns whether the page moved, its
        address, and ``load_pending`` when it moved but had not finished loading in time."""
        if move not in ("back", "forward", "reload"):
            raise BrowserValidationError('Move "back", "forward" or "reload".')
        session = self._owned_session(owner_id, session_id)
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ) as generation:
            call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
            deadline = self._monotonic() + call_timeout
            with _Actions(session, timeout=call_timeout, cancel=cancel) as act:
                index, entries = _navigation_history(act.call("Page.getNavigationHistory", {}))
                current = entries[index]
                page = session.active_target_id
                if move == "reload":
                    if current["transition"] in _FORM_ENTRY_TRANSITIONS and (
                        current["transition"] != "reload"
                        or (page, current["id"]) not in session.reloaded_entries
                    ):
                        raise BrowserValidationError(
                            "This page may be the result of sending a form, and reloading it "
                            "would send that form again, so it was not reloaded. Open its "
                            "address with browser_navigate to load it again without sending "
                            "anything."
                        )
                    target = current
                else:
                    step = -1 if move == "back" else 1
                    if not 0 <= index + step < len(entries):
                        raise BrowserValidationError(
                            f"This tab has no {'earlier' if step < 0 else 'later'} page in its "
                            "history. Nothing changed."
                        )
                    target = entries[index + step]
                if target["url"] != "about:blank":
                    validate_browser_url(
                        target["url"],
                        allow_local_destinations=session.allow_local_destinations,
                        local_destinations_loopback_only=session.local_destinations_loopback_only,
                        resolver=self._resolver,
                        resolution_timeout=self._config.dns_timeout_seconds,
                        owned_preview_origin_allowed=session.owned_preview_origin_allowed,
                    )
                # Which document the tab shows now: the move has happened once that changes. (The
                # history's current index already names an entry while it is still pending, and a
                # leave-page question can still cancel it.)
                before = _frames_of(act.call("Page.getFrameTree", {}))[0]
                if move == "reload":
                    act.call("Page.reload", {})
                else:
                    act.call("Page.navigateToHistoryEntry", {"entryId": target["id"]})
                committed, loaded, url = False, False, before["url"]
                while True:
                    _check_cancel(cancel)
                    if any(
                        item["type"] == "beforeunload" and not item["accepted"]
                        for item in act.dialogs
                    ):
                        # The page asked to stay (unsaved changes): it was not left.
                        return {
                            "session_id": session_id,
                            "command_generation": generation,
                            "moved": False,
                            **act.report(),
                        }
                    if not committed:
                        frame = _frames_of(act.call("Page.getFrameTree", {}))[0]
                        committed = (frame["loader"], frame["url"]) != (
                            before["loader"],
                            before["url"],
                        )
                        url = frame["url"]
                    if committed:
                        loaded = self._document_complete(act)
                    if loaded or self._monotonic() >= deadline:
                        break
                    self._sleep(min(_WAIT_POLL_SECONDS, max(0.0, deadline - self._monotonic())))
                if not committed:
                    raise BrowserTimeoutError(
                        "The page did not move in time; it may still be loading."
                    )
                if move == "reload" and current["transition"] not in _FORM_ENTRY_TRANSITIONS:
                    session.reloaded_entries.add((page, current["id"]))
                session.active_url = url
                return {
                    "session_id": session_id,
                    "command_generation": generation,
                    "moved": True,
                    "url": url[:_URL_MAX_CHARS],
                    **({"load_pending": True} if not loaded else {}),
                    **act.report(),
                }

    def _document_complete(self, act: _Actions) -> bool:
        """Whether the page's main document has loaded; False while it is still being replaced
        (a new document has no isolated world yet, or its old one is gone)."""
        act.forget_worlds()
        try:
            return act.evaluate(act.page, None, "document.readyState") == "complete"
        except (BrowserCancelledError, BrowserTimeoutError, BrowserLimitError):
            raise
        except BrowserError:
            if not act.transport.healthy:
                raise
            return False

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

    def site_cookies(
        self, owner_id: str, session_id: str, *, cancel: CancelCheck = None
    ) -> list[dict[str, Any]]:
        """Every cookie the browser holds, by identity only: domain, name, path and partition.

        Values never leave the browser. The caller decides which belong to a site."""
        session = self._owned_session(owner_id, session_id)
        with self._operation(session, cancel=cancel):
            result = session.transport.call(
                "Storage.getCookies",
                {},
                timeout=self._config.operation_timeout_seconds,
                cancel=cancel,
            )
        cookies = result.get("cookies")
        if not isinstance(cookies, list) or len(cookies) > _MAX_SITE_COOKIES:
            raise BrowserSecurityError("Browser cookie inventory was invalid or too large.")
        found = []
        for cookie in cookies:
            if not isinstance(cookie, Mapping):
                raise BrowserSecurityError("Browser cookie inventory was invalid.")
            identity = _cookie_identity(cookie)
            if identity is not None:
                found.append(identity)
        return found

    def clear_site_data(
        self,
        owner_id: str,
        session_id: str,
        *,
        cookies: Sequence[Mapping[str, Any]],
        origins: Sequence[str],
        cancel: CancelCheck = None,
    ) -> dict[str, int]:
        """Delete exactly these cookies and all storage of these origins in the running browser.

        Its pages may change (a site signs out), so earlier observations of them expire."""
        session = self._owned_session(owner_id, session_id)
        if len(cookies) > _MAX_SITE_COOKIES or len(origins) > _MAX_SITE_ORIGINS:
            raise BrowserValidationError("Too much site data was requested at once.")
        doomed = [_cookie_identity(cookie) for cookie in cookies]
        if any(cookie is None for cookie in doomed):
            raise BrowserValidationError("A cookie to delete was invalid.")
        checked = [_web_origin(origin) for origin in origins]
        timeout = self._config.operation_timeout_seconds
        # Not tied to an observed page: it needs no command generation of the caller's.
        with self._operation(session, cancel=cancel):
            for cookie in doomed:
                session.transport.call(
                    "Network.deleteCookies",
                    {
                        "name": cookie["name"],
                        "domain": cookie["domain"],
                        "path": cookie["path"],
                        **({"partitionKey": cookie["partition"]} if cookie["partition"] else {}),
                    },
                    session_id=session.root_cdp_session_id,
                    timeout=timeout,
                    cancel=cancel,
                )
            for origin in checked:
                session.transport.call(
                    "Storage.clearDataForOrigin",
                    {"origin": origin, "storageTypes": "all"},
                    timeout=timeout,
                    cancel=cancel,
                )
            if doomed or checked:
                with session.control_lock:
                    session.command_generation += 1
        return {"cookies": len(doomed), "origins": len(checked)}

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
        frames: bool = False,
        timeout: float | None = None,
        cancel: CancelCheck = None,
    ) -> dict[str, Any]:
        """A snapshot of the active page. ``frames`` (DOM snapshots only): also each
        out-of-process frame's documents (see :meth:`_frame_snapshots`), which the page's own
        DOM snapshot never holds because another process renders them."""
        session = self._owned_session(owner_id, session_id)
        normalized_kind = str(kind).strip().lower()
        if normalized_kind not in {"semantic", "accessibility", "dom", "text"}:
            raise BrowserValidationError("Browser snapshot kind is invalid.")
        if type(frames) is not bool or (frames and normalized_kind != "dom"):
            raise BrowserValidationError("Frame documents come only with a DOM snapshot.")
        with self._operation(session, cancel=cancel) as generation:
            if not frames:
                result = self._capture_snapshot(session, normalized_kind, timeout, cancel)
                return {**result, "command_generation": generation}
            with self._observing(session):
                result = self._read_snapshot(session, normalized_kind, timeout, cancel)
                # The page and its frames together fit the one snapshot limit (and so the
                # owl computer's link, which carries the whole reply).
                framed = self._frame_snapshots(
                    session,
                    _bounded_timeout(timeout, self._config.operation_timeout_seconds),
                    cancel,
                    budget=self._config.max_snapshot_bytes - result["size_bytes"],
                )
            return {**result, **framed, "command_generation": generation}

    @staticmethod
    def _observing(session: _BrowserSession) -> AbstractContextManager[Any]:
        """Guard a read of the page against a JavaScript dialog.

        A page showing a dialog answers no command until the dialog is answered, and a command
        left waiting would end the browser's connection. The dialog the page shows is known from
        the browser's own events (no extra command), so the read is refused at once; and with a
        transport that supports it, a dialog opening during the read releases it too."""
        opened = _open_dialog(session, (session.cdp_session_id,))
        if opened is not None:
            raise _dialog_open_error(opened[1])
        yielding = getattr(session.transport, "yielding_to_dialogs", None)
        return yielding() if callable(yielding) else nullcontext()

    def _capture_snapshot(
        self,
        session: _BrowserSession,
        normalized_kind: str,
        timeout: float | None,
        cancel: CancelCheck,
    ) -> dict[str, Any]:
        with self._observing(session):
            return self._read_snapshot(session, normalized_kind, timeout, cancel)

    def _read_snapshot(
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
                _DOM_SNAPSHOT_PARAMS,
                session_id=session.cdp_session_id,
                timeout=call_timeout,
                cancel=cancel,
            )
        else:
            return {
                "session_id": session.session_id,
                "kind": normalized_kind,
                **self._accessibility_tree(session, call_timeout, cancel),
            }
        return {
            "session_id": session.session_id,
            "kind": normalized_kind,
            **_bounded_payload(raw, self._config.max_snapshot_bytes),
        }

    def _frame_snapshots(
        self, session: _BrowserSession, call_timeout: float, cancel: CancelCheck, *, budget: int
    ) -> dict[str, Any]:
        """Each out-of-process frame's DOM snapshot, for ``snapshot(frames=True)``.

        Another site's frame runs in its own process, so the page's DOM snapshot does not hold
        its document. Each such frame's session is captured as the page is (the same command,
        the same redaction), with the frame's id, address and security origin; its documents
        include the same-process frames inside it, each naming its ``frameId``. Frames are read
        in turn while together they fit ``budget`` bytes (what the page's snapshot left of its
        limit), for at most _FRAME_SNAPSHOT_SECONDS; a frame left out (over a limit, or gone
        while it was read) is counted in ``omitted_frames``, never returned in part. A dialog in
        a frame raises BrowserDialogOpenError, as a read of the page does. Without a transport
        that names frame sessions there are none."""
        lister = getattr(session.transport, "guarded_frame_sessions", None)
        children = lister(session_id=session.cdp_session_id) if callable(lister) else {}
        if not isinstance(children, Mapping) or len(children) > _MAX_FRAMES:
            raise BrowserLimitError("This page has too many separate frames to read.")
        deadline = time.monotonic() + _FRAME_SNAPSHOT_SECONDS
        captured: list[dict[str, Any]] = []
        omitted = 0
        for frame_id, child in children.items():
            if not (isinstance(frame_id, str) and isinstance(child, str) and frame_id and child):
                raise BrowserSecurityError("Browser frame sessions were invalid.")
            if budget <= 0 or time.monotonic() >= deadline:
                omitted += 1
                continue
            try:
                root = _frames_of(
                    session.transport.call(
                        "Page.getFrameTree",
                        {},
                        session_id=child,
                        timeout=call_timeout,
                        cancel=cancel,
                    )
                )[0]
                raw = session.transport.call(
                    "DOMSnapshot.captureSnapshot",
                    _DOM_SNAPSHOT_PARAMS,
                    session_id=child,
                    timeout=call_timeout,
                    cancel=cancel,
                )
            except BrowserError as exc:
                if not _frame_gone(exc):
                    raise
                omitted += 1
                continue
            bounded = _bounded_payload(raw, budget)
            if bounded["truncated"]:
                omitted += 1
                continue
            budget -= bounded["size_bytes"]
            captured.append(
                {
                    "id": root["id"],
                    "url": str(redact_secrets(root["url"]))[:_FRAME_URL_CHARS],
                    "origin": root["origin"][:_FRAME_URL_CHARS],
                    "data": bounded["data"],
                }
            )
        return {"frames": captured, "omitted_frames": omitted}

    def _accessibility_tree(
        self, session: _BrowserSession, call_timeout: float, cancel: CancelCheck
    ) -> dict[str, Any]:
        """The whole accessibility tree of the page as one ``nodes`` list, with no depth cap.

        The main frame's nodes come first (open shadow roots included, as Chromium computes
        them), then each other frame's, same-process and out-of-process. Every frame's root node
        carries its ``frameId``, which addresses that frame's elements by ``backendDOMNodeId``;
        other frames' node ids are prefixed with their frame id, because separate processes
        number their nodes independently. Nodes are trimmed of what a page view never reads
        (see :func:`_trimmed_accessibility`) before the snapshot byte limit is applied; frames
        are added while they fit and the rest are counted in ``omitted_frames``.

        ``frames`` describes every frame of the page, main frame first: its id, address, the
        security origin of its document and that document's loader, so a reader can tell which
        site holds an element and whether a frame still shows the same document.

        A page's frames change while it is read (an ad or a sign-in frame is removed, or moves
        to another site's process when it navigates). A frame that is gone by the time its tree
        is asked for (:func:`_frame_gone`) is described with ``"unavailable": true`` and read no
        further; the main frame's tree and every other frame still come back. It is not counted
        in ``omitted_frames``, which counts frames left out for the byte limit."""
        page = session.cdp_session_id
        limit = self._config.max_snapshot_bytes

        def call(method: str, params: Mapping[str, Any], cdp_session: str) -> Mapping[str, Any]:
            return session.transport.call(
                method, params, session_id=cdp_session, timeout=call_timeout, cancel=cancel
            )

        def describe(frame: Mapping[str, Any]) -> dict[str, Any]:
            return {
                "id": frame["id"],
                "url": frame["url"][:_FRAME_URL_CHARS],
                "origin": frame["origin"][:_FRAME_URL_CHARS],
                "loader": frame["loader"][:_FRAME_URL_CHARS],
            }

        def gone(description: dict[str, Any], exc: BrowserError) -> dict[str, Any]:
            # The command and its protocol error code, for support; never the browser's text.
            code = getattr(exc, "code", None)
            return {
                **description,
                "unavailable": True,
                "error": type(exc).__name__[:60],
                **({"code": code} if type(code) is int else {}),
            }

        frames = _frames_of(call("Page.getFrameTree", {}, page))
        pending = [(page, frame) for frame in frames[1:]]
        lister = getattr(session.transport, "guarded_frame_sessions", None)
        children = lister(session_id=page) if callable(lister) else {}
        if not isinstance(children, Mapping) or len(children) > _MAX_FRAMES:
            raise BrowserLimitError("This page has too many separate frames to read.")
        unreached: list[dict[str, Any]] = []  # out-of-process frames whose session went away
        for frame_id, child in children.items():
            try:
                pending.extend(
                    (child, frame) for frame in _frames_of(call("Page.getFrameTree", {}, child))
                )
            except BrowserError as exc:
                if not _frame_gone(exc):
                    raise
                unreached.append(
                    gone({"id": str(frame_id)[:256], "url": "", "origin": "", "loader": ""}, exc)
                )

        # The page's own process may show its frames inside the main tree, so they are always
        # described; another process's frame is described when its tree is added.
        described = [describe(frames[0]), *(describe(frame) for frame in frames[1:])]
        nodes = _framed_accessibility(
            call("Accessibility.getFullAXTree", {}, page).get("nodes"), frames[0]["id"], None
        )
        used = _json_size({"nodes": nodes, "omitted_frames": 0, "frames": described})
        added: list[int] = []  # how many nodes each added frame contributed, in order
        omitted = 0
        read: set[str] = set()
        departed: dict[str, dict[str, Any]] = {}  # frame id -> its description, marked gone
        for cdp_session, frame in pending:
            if frame["id"] in read:
                continue  # listed in two processes while it moved: read once, where it is now
            if used > limit or len(added) >= _MAX_FRAMES:
                omitted += 1
                continue
            try:
                raw = call("Accessibility.getFullAXTree", {"frameId": frame["id"]}, cdp_session)
            except BrowserError as exc:
                if not _frame_gone(exc):
                    raise
                departed.setdefault(frame["id"], gone(describe(frame), exc))
                continue
            frame_nodes = _framed_accessibility(raw.get("nodes"), frame["id"], frame["id"])
            known = next(
                (index for index, item in enumerate(described) if item["id"] == frame["id"]), None
            )
            description = [] if known is not None else [describe(frame)]
            size = _json_size(frame_nodes) + _json_size(description)
            if used + size > limit:
                omitted += 1
                used = limit + 1  # stop reading further frames once one no longer fits
                continue
            if known is not None and cdp_session != page:
                # It moved to another process after the page listed it: describe it as it is now.
                described[known] = describe(frame)
            nodes.extend(frame_nodes)
            described.extend(description)
            added.append(len(frame_nodes))
            read.add(frame["id"])
            departed.pop(frame["id"], None)
            used += size
        for item in [*departed.values(), *unreached]:
            known = next(
                (index for index, d in enumerate(described) if d["id"] == item["id"]), None
            )
            if known is None:
                described.append(item)
            else:
                described[known] = item
            used += _json_size(item)
        while True:
            bounded = _bounded_payload(
                {"nodes": nodes, "omitted_frames": omitted, "frames": described}, limit
            )
            if not bounded["truncated"] or not added:
                break
            # Redaction can change sizes slightly: drop the last frame until the rest fits.
            del nodes[len(nodes) - added.pop() :]
            omitted += 1
        if omitted and not bounded["truncated"]:
            bounded["truncated"] = True
        return bounded

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
            with self._observing(session):
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

    def capture_image(
        self,
        owner_id: str,
        session_id: str,
        selector: str | None = None,
        *,
        expected_backend_node_id: int | None = None,
        frame_id: str | None = None,
        full_page: bool = False,
        timeout: float | None = None,
        cancel: CancelCheck = None,
    ) -> dict[str, Any]:
        """An image of one element (addressed as for :meth:`click`, clipped to its box) or, with
        ``full_page``, of the page from its top, for looking at only: its pixels are not a frame
        that visual clicks can use. Sends no input; the element is scrolled into view.

        The element must show; it, and the page, are cut at _PAGE_IMAGE_MAX_CSS pixels wide and
        the page at _FULL_PAGE_MAX_CSS_HEIGHT tall (``truncated``). The PNG is scaled down until
        it fits the image allowance (``scale``), at most _PAGE_IMAGE_ATTEMPTS tries; past that
        BrowserLimitError. Returned base64-encoded with its pixel size."""
        if type(full_page) is not bool:
            raise BrowserValidationError("full_page must be true or false.")
        element_wanted = selector is not None or expected_backend_node_id is not None
        if full_page == element_wanted:
            raise BrowserValidationError("Ask for an image of one element or of the full page.")
        target = (
            _validated_target(selector, expected_backend_node_id, frame_id)
            if element_wanted
            else None
        )
        session = self._owned_session(owner_id, session_id)
        with self._operation(session, cancel=cancel) as generation:
            call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
            with _Actions(session, timeout=call_timeout, cancel=cancel) as act:
                metrics: Mapping[str, Any]
                if target is not None:
                    element = self._locate(act, *target)
                    self._wait_visible(act, element)
                    act.call("DOM.scrollIntoViewIfNeeded", element.ref, session=element.session)
                    quad, offsets = self._placement(act, element)
                    left = min(quad[0::2]) + sum(offset[0] for offset in offsets)
                    top = min(quad[1::2]) + sum(offset[1] for offset in offsets)
                    width, height = (
                        max(quad[0::2]) - min(quad[0::2]),
                        max(quad[1::2]) - min(quad[1::2]),
                    )
                    metrics = act.call("Page.getLayoutMetrics", {})
                    viewport = metrics.get("cssVisualViewport")
                    left += _page_offset(viewport, "pageX")
                    top += _page_offset(viewport, "pageY")
                else:
                    metrics = act.call("Page.getLayoutMetrics", {})
                    left, top = 0.0, 0.0
                    width, height = (
                        _layout_number(metrics.get("cssContentSize"), key)
                        for key in ("width", "height")
                    )
                limit_height = _FULL_PAGE_MAX_CSS_HEIGHT if full_page else _PAGE_IMAGE_MAX_CSS
                clip = {
                    "x": max(0.0, left),
                    "y": max(0.0, top),
                    "width": min(width, _PAGE_IMAGE_MAX_CSS),
                    "height": min(height, limit_height),
                }
                if clip["width"] < 1 or clip["height"] < 1:
                    raise _not_ready("hidden")
                visible = metrics.get("cssLayoutViewport")
                inside = (
                    target is not None
                    and clip["x"] + clip["width"]
                    <= _page_offset(metrics.get("cssVisualViewport"), "pageX")
                    + _layout_number(visible, "clientWidth")
                    and clip["y"] + clip["height"]
                    <= _page_offset(metrics.get("cssVisualViewport"), "pageY")
                    + _layout_number(visible, "clientHeight")
                )
                png, image_width, image_height, scale = self._bounded_image(
                    act, clip, beyond_viewport=not inside
                )
                return {
                    "session_id": session_id,
                    "command_generation": generation,
                    "kind": "full_page" if full_page else "element",
                    "png": base64.b64encode(png).decode("ascii"),
                    "width": image_width,
                    "height": image_height,
                    "scale": round(scale, 3),
                    "truncated": clip["width"] < width or clip["height"] < height,
                    **({"page_height": round(height)} if full_page else {}),
                    **act.report(),
                }

    def _bounded_image(
        self, act: _Actions, clip: Mapping[str, float], *, beyond_viewport: bool
    ) -> tuple[bytes, int, int, float]:
        """A PNG of ``clip`` (CSS pixels of the page) within the image allowance: scaled down
        and taken again while it is too large, at most _PAGE_IMAGE_ATTEMPTS times."""
        limit = min(self._config.max_screenshot_bytes, 1024 * 1024)
        scale = 1.0
        for _ in range(_PAGE_IMAGE_ATTEMPTS):
            raw = act.call(
                "Page.captureScreenshot",
                {
                    "format": "png",
                    "fromSurface": True,
                    "captureBeyondViewport": beyond_viewport,
                    "clip": {**clip, "scale": scale},
                },
            )
            encoded = raw.get("data")
            if (
                not isinstance(encoded, str)
                or len(encoded) > ((self._config.max_screenshot_bytes + 2) // 3) * 4 + 4
            ):
                raise BrowserLimitError("The page image exceeds the image allowance.")
            try:
                png = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise BrowserSecurityError("Invalid browser image encoding.") from None
            if len(png) < 33 or png[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
                raise BrowserSecurityError("Invalid browser PNG image.")
            width, height = struct.unpack(">II", png[16:24])
            if not 1 <= width <= 8192 or not 1 <= height <= 8192:
                raise BrowserLimitError("The page image dimensions exceed the allowance.")
            if len(png) <= limit:
                return png, width, height, scale
            scale *= max(0.1, math.sqrt(limit / len(png)) * 0.9)
        raise BrowserLimitError(
            "That part of the page is too large to show as one image. Take an image of a "
            "smaller element instead."
        )

    def _capture_visual_frame(
        self, session: _BrowserSession, generation: int, *, timeout: float, cancel: CancelCheck
    ) -> BrowserVisualFrame:
        with self._observing(session):
            return self._read_visual_frame(session, generation, timeout=timeout, cancel=cancel)

    def _read_visual_frame(
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
        dialog: str = "dismiss",
        dialog_text: str | None = None,
        button: str = "left",
        click_count: int = 1,
    ) -> dict[str, Any]:
        """Click a point of the frame, once it still shows exactly what that frame showed.

        ``button`` ("left" or "right") and ``click_count`` (1 to 3) say how, as for
        :meth:`click`; the browser's own menu is kept closed. A middle click needs an element
        (:meth:`click` checks that it is a link)."""
        session = self._owned_session(owner_id, session_id)
        if frame.owner_id != owner_id or frame.session_id != session_id:
            raise BrowserOwnershipError("Visual frame belongs to another browser.")
        if (
            type(x) is not int
            or type(y) is not int
            or not (0 <= x < frame.width and 0 <= y < frame.height)
        ):
            raise BrowserValidationError("Coordinates must be inside the observed image.")
        _validated_click(button, click_count)
        if button == "middle":
            raise BrowserValidationError(
                "A middle click needs an observed link element; nothing was clicked."
            )
        answer = _dialog_answer(dialog, dialog_text)
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
            point_x = x * frame.css_width / frame.width
            point_y = y * frame.css_height / frame.height
            with _Actions(
                session, timeout=call_timeout, cancel=cancel, dialog=answer, report=True
            ) as act:
                act.hold_file_choosers(act.page)
                # Functions run on the page's main document (no element of it is addressed).
                page = _Element(act.page, None, 0)
                menu = (
                    act.call_on(
                        page,
                        act.document_object(act.page),
                        _CONTEXT_MENU_WATCH_FUNCTION,
                        by_value=False,
                    )
                    if button == "right"
                    else None
                )
                _press_mouse(act, point_x, point_y, button, click_count)
                shown = (
                    act.call_on(page, menu, _CONTEXT_MENU_HANDLED_FUNCTION)
                    if menu is not None
                    else None
                )
                act.catch_file_choosers()
                _check_cancel(cancel)
                return {
                    "session_id": session_id,
                    "command_generation": generation,
                    "dispatched": True,
                    "verification_required": True,
                    **(
                        {"page_menu": shown.get("handled") is True}
                        if isinstance(shown, Mapping) and shown.get("fired") is True
                        else {}
                    ),
                    **act.report(),
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
            # Events other operations already read (while watching for dialogs) come first.
            events: list[Mapping[str, Any]] = []
            kept: deque[tuple[str, Mapping[str, Any]]] = deque(maxlen=_MAX_DIAGNOSTIC_EVENTS_HARD)
            for item in session.held_events:
                if item[0] == session.cdp_session_id and len(events) <= limit:
                    events.append(item[1])
                else:
                    kept.append(item)
            session.held_events = kept
            if len(events) <= limit:
                drained = session.transport.drain_events(
                    session_id=session.cdp_session_id,
                    max_events=limit + 1 - len(events),
                    timeout=_bounded_timeout(timeout, self._config.operation_timeout_seconds),
                    cancel=cancel,
                )
                _note_events(session, session.cdp_session_id, drained, hold=False)
                events.extend(drained)
            output: list[dict[str, Any]] = []
            for event in events:
                method = str(event.get("method") or "")
                category = _DIAGNOSTIC_METHODS.get(method)
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

    def page_report(
        self, owner_id: str, session_id: str, *, cancel: CancelCheck = None
    ) -> dict[str, Any]:
        """What the page reported since the last acting step began, read once.

        An acting step (click, typing, a choice, a key, a check, a drag, an upload, answering a
        dialog) opens the report on its page; reading it closes it, so read it after the page
        has been observed following the step. Events still unread are read first, without
        waiting for more. It holds the page's own requests (documents, XHR and fetch, on the
        page's site as the browser judges it) that started during the step and failed or
        answered with status 400 or above, each with its method, its path without query or
        fragment (ids collapsed to ``:id``) and the status or Chromium's error name, at most
        five (``more_failed_requests`` counts the rest); and how many uncaught script errors
        the page had. Never queries, bodies, headers, cookies, console text or error values;
        never another site's requests. Empty after a step the page reported nothing about."""
        session = self._owned_session(owner_id, session_id)
        with self._operation(session, cancel=cancel):
            report = session.page_report
            if report is not None:
                drained = session.transport.drain_events(
                    session_id=report.cdp_session,
                    max_events=_REPORT_DRAIN_EVENTS,
                    timeout=_DIALOG_CHECK_SECONDS,
                    cancel=cancel,
                )
                _note_events(session, report.cdp_session, drained)
            session.page_report = None
            return {
                "session_id": session.session_id,
                "failed_requests": [dict(item) for item in report.failed] if report else [],
                "more_failed_requests": report.more if report else 0,
                "uncaught_errors": report.errors if report else 0,
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
        selector: str | None,
        *,
        expected_backend_node_id: int | None = None,
        frame_id: str | None = None,
        timeout: float | None = None,
        cancel: CancelCheck = None,
        expected_generation: int | None = None,
        label_backend_node_ids: Sequence[int] = (),
        dialog: str = "dismiss",
        dialog_text: str | None = None,
        button: str = "left",
        click_count: int = 1,
    ) -> dict[str, Any]:
        """``label_backend_node_ids``: the target's own label (from a verified DOM snapshot, not
        page script), where a click activates the target as HTML labels do. Styled checkboxes
        and radios often show only their label, with the real input underneath.

        Address the element by a top-document ``selector`` (checked against
        ``expected_backend_node_id`` when given), or with ``selector=None`` by its observed node
        identity in ``frame_id`` (any frame, including out-of-process ones and open shadow
        roots). ``dialog`` answers the JavaScript dialog the click opens ("dismiss" or "accept",
        with ``dialog_text`` for a prompt): it accepts at most one, shown by a frame of the
        clicked element's origin, and dismisses any other (see _Actions); every answered dialog
        is reported.

        ``button`` ("left", "right" or "middle") and ``click_count`` (1 to 3: a double or triple
        click) say how it clicks. A right click opens the page's own menu, never the browser's
        (``page_menu`` says whether the page handled it); a middle click is sent only to a
        link (it opens it in a new tab) and never uses the label. A file chooser the click opens
        is held for :meth:`upload_file` instead of being shown, and reported (``file_chooser``)."""
        session = self._owned_session(owner_id, session_id)
        target = _validated_target(selector, expected_backend_node_id, frame_id)
        labels = _validated_labels(label_backend_node_ids, expected_backend_node_id)
        answer = _dialog_answer(dialog, dialog_text)
        _validated_click(button, click_count)
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ) as generation:
            call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
            with _Actions(
                session, timeout=call_timeout, cancel=cancel, dialog=answer, report=True
            ) as act:
                element = self._locate(act, *target)
                if button == "middle" and act.run(element, _IN_LINK_FUNCTION) is not True:
                    raise BrowserValidationError(
                        "A middle click opens a link in a new tab, and this element is not a "
                        "link. Nothing was clicked."
                    )
                act.hold_file_choosers(element.session)
                menu = (
                    act.run(element, _CONTEXT_MENU_WATCH_FUNCTION, by_value=False)
                    if button == "right"
                    else None
                )
                self._click(
                    act,
                    element,
                    labels if button == "left" else (),
                    verify=expected_backend_node_id is not None,
                    button=button,
                    click_count=click_count,
                )
                shown = (
                    act.call_on(element, menu, _CONTEXT_MENU_HANDLED_FUNCTION)
                    if menu is not None
                    else None
                )
                act.catch_file_choosers()
                return {
                    "session_id": session.session_id,
                    "command_generation": generation,
                    "clicked": True,
                    **(
                        {"button": button, "click_count": click_count}
                        if (button, click_count) != ("left", 1)
                        else {}
                    ),
                    **(
                        {"page_menu": shown.get("handled") is True}
                        if isinstance(shown, Mapping) and shown.get("fired") is True
                        else {}
                    ),
                    **act.report(),
                }

    def hover(
        self,
        owner_id: str,
        session_id: str,
        selector: str | None,
        *,
        expected_backend_node_id: int | None = None,
        frame_id: str | None = None,
        timeout: float | None = None,
        cancel: CancelCheck = None,
        expected_generation: int | None = None,
        dialog: str = "dismiss",
        dialog_text: str | None = None,
    ) -> dict[str, Any]:
        """Move the mouse over the element once it shows, has stopped moving and is not covered
        (menus and tooltips open on hover). Addressed as for :meth:`click`."""
        session = self._owned_session(owner_id, session_id)
        target = _validated_target(selector, expected_backend_node_id, frame_id)
        answer = _dialog_answer(dialog, dialog_text)
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ) as generation:
            call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
            with _Actions(session, timeout=call_timeout, cancel=cancel, dialog=answer) as act:
                element = self._locate(act, *target)
                x, y = self._pointer_point(
                    act, element, verify=expected_backend_node_id is not None
                )
                self._show_page(act, element)
                act.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
                return {
                    "session_id": session_id,
                    "command_generation": generation,
                    "dispatched": True,
                    **act.report(),
                }

    def set_checked(
        self,
        owner_id: str,
        session_id: str,
        selector: str | None,
        checked: bool,
        *,
        expected_backend_node_id: int | None = None,
        frame_id: str | None = None,
        label_backend_node_ids: Sequence[int] = (),
        timeout: float | None = None,
        cancel: CancelCheck = None,
        expected_generation: int | None = None,
        dialog: str = "dismiss",
        dialog_text: str | None = None,
    ) -> dict[str, Any]:
        """Check or uncheck a checkbox, radio button or switch, and confirm the page shows it.

        The state is read in an isolated world (native ``checked`` or ``aria-checked``). Nothing
        is clicked when the control already shows the requested state. After one click the
        state is read again (within the bounded element wait); a control that does not show it
        raises :class:`BrowserInputUnconfirmedError`. A radio button is never "unchecked": that
        is done by choosing another option of its group."""
        session = self._owned_session(owner_id, session_id)
        if type(checked) is not bool:
            raise BrowserValidationError("The requested checked state must be true or false.")
        target = _validated_target(selector, expected_backend_node_id, frame_id)
        labels = _validated_labels(label_backend_node_ids, expected_backend_node_id)
        answer = _dialog_answer(dialog, dialog_text)
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ) as generation:
            call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
            with _Actions(
                session, timeout=call_timeout, cancel=cancel, dialog=answer, report=True
            ) as act:
                element = self._locate(act, *target)
                state = self._read(act, element)
                if state.get("kind") not in _CHECKABLE_KINDS:
                    raise BrowserValidationError(
                        "This element has no checked state (it is not a checkbox, radio "
                        "button or switch). Nothing was clicked."
                    )
                result = {"session_id": session_id, "command_generation": generation}
                if state.get("checked") is checked:
                    return {**result, "changed": False, "checked": checked, **act.report()}
                if not checked and state["kind"] in {"radio", "menuitemradio"}:
                    raise BrowserValidationError(
                        "A radio button is unchecked by choosing another option of its group. "
                        "Nothing was clicked."
                    )
                self._click(act, element, labels, verify=expected_backend_node_id is not None)
                deadline = self._monotonic() + min(
                    self._config.actionable_wait_seconds, call_timeout
                )
                while True:
                    _check_cancel(cancel)
                    state = act.run(element, _READ_FUNCTION)
                    if not isinstance(state, Mapping) or not state.get("attached"):
                        raise BrowserInputUnconfirmedError(
                            "The click was sent once, but the control was replaced, so its "
                            "state is unconfirmed. Observe the page before retrying."
                        )
                    if state.get("checked") is checked:
                        return {**result, "changed": True, "checked": checked, **act.report()}
                    if self._monotonic() >= deadline:
                        raise BrowserInputUnconfirmedError(
                            "The click was sent once, but the control does not show the "
                            "requested state. Observe the page before retrying."
                        )
                    self._sleep(min(_READY_POLL_SECONDS, max(0.0, deadline - self._monotonic())))

    def drag(
        self,
        owner_id: str,
        session_id: str,
        selector: str | None,
        target_selector: str | None,
        *,
        expected_backend_node_id: int | None = None,
        target_backend_node_id: int | None = None,
        frame_id: str | None = None,
        target_frame_id: str | None = None,
        timeout: float | None = None,
        cancel: CancelCheck = None,
        expected_generation: int | None = None,
        dialog: str = "dismiss",
        dialog_text: str | None = None,
    ) -> dict[str, Any]:
        """Drag one element onto another with the mouse, as a person does.

        A page that starts a native (HTML) drag gets the browser's own drag and drop: the drag
        is taken over and dropped on the target. Otherwise the pressed mouse moves to the target
        in steps (sliders, sortable lists) and is released there. The mouse button is always
        released, also on failure or cancellation. Both elements are addressed as for
        :meth:`click`."""
        session = self._owned_session(owner_id, session_id)
        source_target = _validated_target(selector, expected_backend_node_id, frame_id)
        drop_target = _validated_target(target_selector, target_backend_node_id, target_frame_id)
        answer = _dialog_answer(dialog, dialog_text)
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ) as generation:
            call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
            with _Actions(
                session, timeout=call_timeout, cancel=cancel, dialog=answer, report=True
            ) as act:
                source = self._locate(act, *source_target)
                drop = self._locate(act, *drop_target)
                x, y = self._pointer_point(act, source, verify=expected_backend_node_id is not None)
                self._show_page(act, source)
                self._show_page(act, drop)
                watch = act.run(source, _DRAG_WATCH_FUNCTION, by_value=False)
                act.call("Input.setInterceptDrags", {"enabled": True})
                position, pressed, data = (x, y), False, None
                try:
                    act.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
                    act.call(
                        "Input.dispatchMouseEvent",
                        {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
                    )
                    pressed = True
                    # A few pixels start the gesture: pages begin a drag past a small threshold.
                    position = (x + 6, y + 6)
                    act.call(
                        "Input.dispatchMouseEvent",
                        {
                            "type": "mouseMoved",
                            "x": position[0],
                            "y": position[1],
                            "button": "left",
                            "buttons": 1,
                        },
                    )
                    if act.call_on(source, watch, _DRAG_STARTED_FUNCTION) is True:
                        data = self._intercepted_drag(act)
                    end = self._pointer_point(act, drop, verify=False)
                    if data is not None:
                        for kind in ("dragEnter", "dragOver", "drop"):
                            act.call(
                                "Input.dispatchDragEvent",
                                {"type": kind, "x": end[0], "y": end[1], "data": data},
                            )
                    else:
                        start = position
                        for step in range(1, 6):
                            position = (
                                start[0] + (end[0] - start[0]) * step / 5,
                                start[1] + (end[1] - start[1]) * step / 5,
                            )
                            act.call(
                                "Input.dispatchMouseEvent",
                                {
                                    "type": "mouseMoved",
                                    "x": position[0],
                                    "y": position[1],
                                    "button": "left",
                                    "buttons": 1,
                                },
                            )
                    position = end
                finally:
                    try:
                        if pressed:
                            act.call(
                                "Input.dispatchMouseEvent",
                                {
                                    "type": "mouseReleased",
                                    "x": position[0],
                                    "y": position[1],
                                    "button": "left",
                                    "clickCount": 1,
                                },
                                cancel=None,
                            )
                    finally:
                        act.call("Input.setInterceptDrags", {"enabled": False}, cancel=None)
                _check_cancel(cancel)
                return {
                    "session_id": session_id,
                    "command_generation": generation,
                    "dispatched": True,
                    "native_drag": data is not None,
                    **act.report(),
                }

    def _intercepted_drag(self, act: _Actions) -> Mapping[str, Any]:
        """The drag the browser took over from the page (Input.setInterceptDrags), once it arrives."""
        deadline = self._monotonic() + min(self._config.actionable_wait_seconds, act.timeout)
        while True:
            _check_cancel(act.cancel)
            for event in act.events(act.page):
                params = event.get("params")
                if event.get("method") == "Input.dragIntercepted" and isinstance(params, Mapping):
                    data = params.get("data")
                    if not isinstance(data, Mapping):
                        raise BrowserSecurityError("Browser drag data was invalid.")
                    return data
            if self._monotonic() >= deadline:
                raise BrowserInputUnconfirmedError(
                    "The page started a drag, but the browser did not hand it over; the mouse "
                    "was released. Observe the page before retrying."
                )

    def _click(
        self,
        act: _Actions,
        element: _Element,
        labels: tuple[int, ...] = (),
        *,
        verify: bool,
        button: str = "left",
        click_count: int = 1,
    ) -> None:
        # Mouse coordinates are viewport-relative. Scrolling precedes measuring the box,
        # including when an ancestor is scrollable.
        x, y = self._pointer_point(act, element, verify=verify, labels=labels)
        self._show_page(act, element)
        _press_mouse(act, x, y, button, click_count)

    def _show_page(self, act: _Actions, element: _Element) -> None:
        """Make sure mouse input can reach an element in an out-of-process frame.

        Chromium routes mouse input into such a frame only once its page is shown and drawn; a
        background page (as a headless browser creates it) drops that input silently. A page
        in the background is brought to the front, then two drawn frames are awaited (bounded
        by the element wait); a page that draws none is refused as hidden."""
        if not element.hosts:
            return
        if act.evaluate(act.page, None, "document.visibilityState") == "visible":
            return
        act.call("Page.bringToFront", {})
        limit = round(1000 * min(self._config.actionable_wait_seconds, act.timeout))
        drawn = act.evaluate(
            act.page,
            None,
            "new Promise(resolve => { const timer = setTimeout(() => resolve(false), "
            f"{limit}); requestAnimationFrame(() => requestAnimationFrame(() => "
            "{ clearTimeout(timer); resolve(true); })); })",
            await_promise=True,
        )
        if drawn is not True:
            raise _not_ready("hidden")

    def _pointer_point(
        self, act: _Actions, element: _Element, *, verify: bool, labels: tuple[int, ...] = ()
    ) -> tuple[float, float]:
        """Wait (bounded) until the element shows, stops moving and, with ``verify``, a click at
        its centre reaches it; return that point in the page's viewport.

        Each check is fresh: visibility in an isolated world, the box twice in a row unchanged,
        and Chromium's hit test, which page scripts cannot override. A styled checkbox or radio
        hidden under its own label is clicked where the label shows, as a person would. The
        reason it never became ready is raised: hidden, moving or covered."""
        deadline = self._monotonic() + min(self._config.actionable_wait_seconds, act.timeout)
        previous = None
        while True:
            _check_cancel(act.cancel)
            reason = "hidden"
            if self._visible(act, element):
                act.call("DOM.scrollIntoViewIfNeeded", element.ref, session=element.session)
                placement = self._placement(act, element)
                if placement == previous:
                    x, y = _placement_center(placement)
                    if not verify:
                        return x, y
                    x, y = round(x), round(y)
                    if self._reaches(act, element, x, y, placement[1], labels, subtree=True):
                        return x, y
                    reason = "covered"
                else:
                    reason = "moving"
                previous = placement
            if labels and reason != "moving":
                point = self._label_point(act, element, labels)
                if point is not None:
                    return point
            if self._monotonic() >= deadline:
                raise _not_ready(reason)
            self._sleep(min(_READY_POLL_SECONDS, max(0.0, deadline - self._monotonic())))

    def _visible(self, act: _Actions, element: _Element) -> bool:
        state = act.run(element, _PROBE_FUNCTION)
        _require_element(state)
        return state.get("visible") is True

    def _wait_visible(self, act: _Actions, element: _Element) -> None:
        deadline = self._monotonic() + min(self._config.actionable_wait_seconds, act.timeout)
        while not self._visible(act, element):
            if self._monotonic() >= deadline:
                raise _not_ready("hidden")
            self._sleep(min(_READY_POLL_SECONDS, max(0.0, deadline - self._monotonic())))
            _check_cancel(act.cancel)

    @staticmethod
    def _placement(
        act: _Actions, element: _Element
    ) -> tuple[tuple[float, ...], tuple[tuple[float, float], ...]]:
        """The element's box in its own frame, and where each out-of-process frame around it
        starts in the frame that holds it (outermost first)."""
        quad = _content_quad(act.call("DOM.getBoxModel", element.ref, session=element.session))
        offsets = []
        for host_session, owner in element.hosts:
            box = _content_quad(
                act.call("DOM.getBoxModel", {"backendNodeId": owner}, session=host_session)
            )
            offsets.append((box[0], box[1]))
        return quad, tuple(offsets)

    def _reaches(
        self,
        act: _Actions,
        element: _Element,
        x: int,
        y: int,
        offsets: tuple[tuple[float, float], ...],
        labels: tuple[int, ...],
        *,
        subtree: bool,
    ) -> bool:
        # A valid node can still sit behind another control. Chromium's hit test and DOM
        # identities cannot be overridden by page JavaScript. A nested label/span is
        # legitimate; an unrelated overlay is not. This only fences the instant before
        # dispatch, not future page event handlers. Through out-of-process frames, each frame's
        # own hit test must reach the next iframe element, then the element.
        local_x, local_y = x, y
        for (host_session, owner), (offset_x, offset_y) in zip(element.hosts, offsets, strict=True):
            if self._hit(act, host_session, local_x, local_y) != owner:
                return False
            local_x, local_y = round(local_x - offset_x), round(local_y - offset_y)
        hit = self._hit(act, element.session, local_x, local_y)
        if type(hit) is not int or hit <= 0:
            return False
        if subtree:
            tree = act.call(
                "DOM.describeNode",
                {**element.ref, "depth": -1, "pierce": True},
                session=element.session,
            ).get("node", {})
            pending = [tree]
            while pending:
                node = pending.pop()
                if not isinstance(node, Mapping):
                    continue
                if node.get("backendNodeId") == hit:
                    return True
                for key in ("children", "shadowRoots"):
                    pending.extend(node.get(key) or [])
                if isinstance(node.get("contentDocument"), Mapping):
                    pending.append(node["contentDocument"])
        # The control's own label: the browser forwards the click to the control.
        return hit in labels

    @staticmethod
    def _hit(act: _Actions, cdp_session: str, x: int, y: int) -> object:
        # The box model and mouse events use viewport coordinates, but getNodeForLocation
        # hit-tests the document. Scrolling the target into view usually moves the page, so
        # without the page offset the test lands above the control (often on <body>) and every
        # click on a scrolled page was refused as covered. Each frame's session has its own.
        viewport = act.call("Page.getLayoutMetrics", {}, session=cdp_session).get(
            "cssVisualViewport"
        )
        return act.call(
            "DOM.getNodeForLocation",
            {
                "x": x + _page_offset(viewport, "pageX"),
                "y": y + _page_offset(viewport, "pageY"),
                "includeUserAgentShadowDOM": False,
                "ignorePointerEventsNone": False,
            },
            session=cdp_session,
        ).get("backendNodeId")

    def _label_point(
        self, act: _Actions, element: _Element, labels: tuple[int, ...]
    ) -> tuple[int, int] | None:
        """Where a click lands on the target's own label, checked by Chromium's hit test."""
        # Labels come first in their list (then what they contain); a few lookups find them.
        for backend in labels[:32]:
            node = act.call(
                "DOM.describeNode", {"backendNodeId": backend}, session=element.session
            ).get("node", {})
            if str(node.get("nodeName", "")).upper() != "LABEL":
                continue
            label = _Element(element.session, element.frame_id, backend, element.hosts)
            act.call("DOM.scrollIntoViewIfNeeded", label.ref, session=label.session)
            placement = self._placement(act, label)
            x, y = (round(value) for value in _placement_center(placement))
            if self._reaches(act, label, x, y, placement[1], labels, subtree=False):
                return x, y
        return None

    def download(
        self,
        owner_id: str,
        session_id: str,
        selector: str | None,
        *,
        max_bytes: int,
        collect=None,
        expected_backend_node_id: int | None = None,
        frame_id: str | None = None,
        expected_generation: int | None = None,
        cancel: CancelCheck = None,
        dialog: str = "dismiss",
        dialog_text: str | None = None,
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
        target = _validated_target(selector, expected_backend_node_id, frame_id)
        answer = _dialog_answer(dialog, dialog_text)
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ):
            transport = session.transport
            if not callable(getattr(transport, "drain_browser_events", None)):
                raise BrowserValidationError("This browser transport cannot observe downloads.")
            call_timeout = self._config.operation_timeout_seconds
            with _Actions(session, timeout=call_timeout, cancel=cancel, dialog=answer) as act:
                element = self._locate(act, *target)
                frame_tree = act.call("Page.getFrameTree", {}, session=element.session)
                frame_id = element.frame_id or frame_tree["frameTree"]["frame"]["id"]
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
                        self._click(act, element, verify=expected_backend_node_id is not None)
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
                                        raise BrowserLimitError(
                                            "Download exceeds its byte allowance."
                                        )
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
                                raise BrowserSecurityError(
                                    "Download is not a bounded regular file."
                                )

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
                                if received != info.st_size or (
                                    info.st_mtime_ns,
                                    info.st_ctime_ns,
                                ) != (
                                    after.st_mtime_ns,
                                    after.st_ctime_ns,
                                ):
                                    raise BrowserSecurityError(
                                        "Downloaded bytes changed before collection."
                                    )

                            return (
                                collect(chunks(), info.st_size) if collect else b"".join(chunks())
                            )
                        finally:
                            os.close(descriptor)
                    finally:
                        for guid in pending:
                            if guid != completed:
                                with suppress(BrowserError):
                                    transport.call(
                                        "Browser.cancelDownload",
                                        {"guid": guid},
                                        timeout=call_timeout,
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
        frame_id=None,
        expected_generation=None,
        cancel=None,
        dialog="dismiss",
        dialog_text=None,
        expected_frame_loader=None,
    ):
        """Select an owned library file in one currently observed file input.

        Selection itself can trigger website JavaScript, so the caller must
        authorize sharing these bytes with the current website before dispatch.
        Retain the owned file until browser cleanup because form upload can be lazy.
        ``expected_frame_loader``: the document the input must still be in (a file chooser's
        report names it), so an input of a later document is never given the file.
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
        target = _validated_target(selector, expected_backend_node_id, frame_id)
        answer = _dialog_answer(dialog, dialog_text)
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
                with _Actions(
                    session, timeout=timeout, cancel=cancel, dialog=answer, report=True
                ) as act:
                    element = self._locate(act, *target)
                    if expected_frame_loader is not None:
                        frames = _frames_of(
                            act.call("Page.getFrameTree", {}, session=element.session)
                        )
                        wanted = element.frame_id or frames[0]["id"]
                        if (
                            next((f["loader"] for f in frames if f["id"] == wanted), None)
                            != expected_frame_loader
                        ):
                            raise BrowserNotFoundError(_CHANGED_ELEMENT)
                    description = act.call(
                        "DOM.describeNode", element.ref, session=element.session
                    ).get("node", {})
                    attributes = description.get("attributes", [])
                    attrs = dict(zip(attributes[::2], attributes[1::2], strict=True))
                    if (
                        description.get("nodeName") != "INPUT"
                        or attrs.get("type", "").lower() != "file"
                    ):
                        raise BrowserValidationError(
                            "The observed target is no longer a file input."
                        )
                    _check_cancel(cancel)
                    register = getattr(session.transport, "allow_file_upload", None)
                    if not callable(register):
                        raise BrowserValidationError(
                            "This browser transport cannot safely observe file uploads."
                        )
                    # The upload's request is intercepted by the frame's own guarded session.
                    register(element.session, size)
                    act.call(
                        "DOM.setFileInputFiles",
                        {"files": [str(path)], **element.ref},
                        session=element.session,
                    )
                    # The browser can decline a file without an error (for example a name its
                    # locale cannot represent): confirm the field holds it.
                    if act.run(element, _FILE_COUNT_FUNCTION) != 1:
                        raise BrowserInputUnconfirmedError(
                            "The browser did not take the file into the upload field. Observe "
                            "the page before retrying."
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
                        **act.report(),
                    }
            finally:
                if not retained:
                    shutil.rmtree(staging)

    def type_text(
        self,
        owner_id: str,
        session_id: str,
        selector: str | None,
        text: str,
        *,
        replace: bool = True,
        expected_backend_node_id: int | None = None,
        frame_id: str | None = None,
        timeout: float | None = None,
        cancel: CancelCheck = None,
        expected_generation: int | None = None,
        dialog: str = "dismiss",
        dialog_text: str | None = None,
    ) -> dict[str, Any]:
        session = self._owned_session(owner_id, session_id)
        target = _validated_target(selector, expected_backend_node_id, frame_id)
        answer = _dialog_answer(dialog, dialog_text)
        if not isinstance(text, str) or len(text) > _TYPE_MAX_CHARS:
            raise BrowserValidationError("Browser input text is invalid or too long.")
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ) as generation:
            call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
            with _Actions(
                session, timeout=call_timeout, cancel=cancel, dialog=answer, report=True
            ) as act:
                element = self._locate(act, *target)
                self._focus(act, element)
                if replace:
                    self._clear(act, element)
                self._assert_focused(act, element)
                act.call("Input.insertText", {"text": text})
                return {
                    "session_id": session.session_id,
                    "command_generation": generation,
                    "typed": True,
                    "character_count": len(text),
                    **act.report(),
                }

    def fill(
        self,
        owner_id: str,
        session_id: str,
        selector: str | None,
        text: str,
        *,
        submit: bool = False,
        expected_backend_node_id: int | None = None,
        frame_id: str | None = None,
        timeout: float | None = None,
        cancel: CancelCheck = None,
        expected_generation: int | None = None,
        dialog: str = "dismiss",
        dialog_text: str | None = None,
    ) -> dict[str, Any]:
        """Clear a field, set its text and, with ``submit``, press Enter in it.

        The field must show and take focus. It is confirmed empty after clearing (otherwise
        nothing is typed and :class:`BrowserInputUnconfirmedError` is raised), and its value is
        read back afterwards in an isolated world: ``value_matches`` compares it with ``text``,
        Unicode-normalised and case-folded the same way for every language. A page may reformat
        what was typed (a phone number, a date), so a difference is reported, never retried.
        The value itself is not returned."""
        session = self._owned_session(owner_id, session_id)
        target = _validated_target(selector, expected_backend_node_id, frame_id)
        answer = _dialog_answer(dialog, dialog_text)
        if not isinstance(text, str) or len(text) > _TYPE_MAX_CHARS:
            raise BrowserValidationError("Browser input text is invalid or too long.")
        if type(submit) is not bool:
            raise BrowserValidationError("Submit must be true or false.")
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ) as generation:
            call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
            with _Actions(
                session, timeout=call_timeout, cancel=cancel, dialog=answer, report=True
            ) as act:
                element = self._locate(act, *target)
                self._focus(act, element)
                self._clear(act, element)
                if str(self._read(act, element).get("value") or "").strip("\n"):
                    raise BrowserInputUnconfirmedError(
                        "The field kept its text after it was cleared, so nothing was typed. "
                        "Observe the field before retrying."
                    )
                self._assert_focused(act, element)
                if text:
                    act.call("Input.insertText", {"text": text})
                value = self._read(act, element).get("value")
                matches = isinstance(value, str) and _fold(value).strip() == _fold(text).strip()
                if submit:
                    self._assert_focused(act, element)
                    self._press(act, _key_chord("Enter", False, ()))
                _check_cancel(cancel)
                return {
                    "session_id": session_id,
                    "command_generation": generation,
                    "filled": True,
                    "character_count": len(text),
                    "value_matches": matches,
                    "submitted": submit,
                    **act.report(),
                }

    def type_keys(
        self,
        owner_id: str,
        session_id: str,
        selector: str | None,
        text: str,
        *,
        expected_backend_node_id: int | None = None,
        frame_id: str | None = None,
        timeout: float | None = None,
        cancel: CancelCheck = None,
        expected_generation: int | None = None,
        dialog: str = "dismiss",
        dialog_text: str | None = None,
    ) -> dict[str, Any]:
        """Type ``text`` key by key into the element, as a keyboard does, for pages that react
        to each key (suggestions, one box per code digit). Any script's characters are typed
        the same way; a line break presses Enter and a tab presses Tab.

        The element must show and hold focus before the first key. Pages may move focus on
        purpose while typing (to the next code box), so later keys go where the page puts the
        focus, as for a person; ``focus_kept`` says whether the element still has it."""
        session = self._owned_session(owner_id, session_id)
        target = _validated_target(selector, expected_backend_node_id, frame_id)
        answer = _dialog_answer(dialog, dialog_text)
        if (
            not isinstance(text, str)
            or not 1 <= len(text) <= _KEYS_MAX_CHARS
            or any(char not in "\n\t" and not _typable(char) for char in text)
        ):
            raise BrowserValidationError(
                f"Typed keys must be 1 to {_KEYS_MAX_CHARS} characters; control characters "
                "other than line breaks and tabs cannot be typed."
            )
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ) as generation:
            call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
            with _Actions(
                session, timeout=call_timeout, cancel=cancel, dialog=answer, report=True
            ) as act:
                element = self._locate(act, *target)
                self._focus(act, element)
                for character in text:
                    down, up = _typing_events(character)
                    try:
                        act.call("Input.dispatchKeyEvent", down)
                    finally:
                        # Release the same key even when cancelled between key events.
                        act.call("Input.dispatchKeyEvent", up, cancel=None)
                    _check_cancel(cancel)
                return {
                    "session_id": session_id,
                    "command_generation": generation,
                    "typed": True,
                    "character_count": len(text),
                    "focus_kept": self._has_focus(act, element),
                    **act.report(),
                }

    def read_value(
        self,
        owner_id: str,
        session_id: str,
        selector: str | None,
        *,
        expected_backend_node_id: int | None = None,
        frame_id: str | None = None,
        timeout: float | None = None,
        cancel: CancelCheck = None,
    ) -> dict[str, Any]:
        """Read one element's current value in an isolated world, where page scripts cannot
        change what the DOM properties report: a field's value, a checkbox's state, a select's
        chosen options, an editable region's or other element's text.

        Values of password, one-time-code and payment fields are never returned. Text is
        bounded and redacted like snapshots. Reading sends no input."""
        session = self._owned_session(owner_id, session_id)
        target = _validated_target(selector, expected_backend_node_id, frame_id)
        with self._operation(session, cancel=cancel) as generation:
            call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
            with _Actions(session, timeout=call_timeout, cancel=cancel) as act:
                element = self._locate(act, *target)
                state = self._read(act, element)
                limit = min(_READ_VALUE_MAX_BYTES, self._config.max_snapshot_bytes)
                raw = "[private field]" if state.get("secret") else str(state.get("value") or "")
                bounded = _bounded_text(raw, limit)
                kind = state.get("kind")
                result: dict[str, Any] = {
                    "session_id": session_id,
                    "command_generation": generation,
                    "kind": kind
                    if kind in {*_CHECKABLE_KINDS, "field", "select", "editable"}
                    else "text",
                    "value": bounded["text"],
                    "truncated": bounded["truncated"],
                }
                if state.get("checked") in (True, False, "mixed"):
                    result["checked"] = state["checked"]
                selected = state.get("selected")
                if isinstance(selected, list) and not state.get("secret"):
                    result["selected"] = [
                        _bounded_text(str(item), 512)["text"] for item in selected[:64]
                    ]
                return {**result, **act.report()}

    def select_option(
        self,
        owner_id: str,
        session_id: str,
        selector: str | None,
        option_selector: str | None,
        *,
        expected_backend_node_id: int | None = None,
        expected_option_backend_node_id: int | None = None,
        frame_id: str | None = None,
        timeout: float | None = None,
        cancel: CancelCheck = None,
        expected_generation: int | None = None,
        dialog: str = "dismiss",
        dialog_text: str | None = None,
    ) -> dict[str, Any]:
        """Choose an option of a native select. Both are addressed as for :meth:`click`; an
        option addressed by identity is in the select's frame."""
        session = self._owned_session(owner_id, session_id)
        targets = (
            _validated_target(selector, expected_backend_node_id, frame_id),
            _validated_target(option_selector, expected_option_backend_node_id, frame_id),
        )
        answer = _dialog_answer(dialog, dialog_text)
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ) as generation:
            call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
            group = "alysis-select-" + secrets.token_hex(8)
            with _Actions(
                session, timeout=call_timeout, cancel=cancel, dialog=answer, report=True
            ) as act:
                objects = []
                elements = []
                try:
                    for target in targets:
                        element = self._locate(act, *target)
                        elements.append(element)
                        resolved = act.call(
                            "DOM.resolveNode",
                            {**element.ref, "objectGroup": group},
                            session=element.session,
                        )
                        object_id = resolved.get("object", {}).get("objectId")
                        if not isinstance(object_id, str) or not object_id:
                            raise BrowserNotFoundError("Observed control is no longer available.")
                        objects.append(object_id)
                    if elements[0].session != elements[1].session:
                        raise BrowserValidationError(
                            "The option must belong to the observed select."
                        )
                    result = act.call(
                        "Runtime.callFunctionOn",
                        {
                            "objectId": objects[0],
                            "functionDeclaration": _SELECT_OPTION_FUNCTION,
                            "arguments": [{"objectId": objects[1]}],
                            "returnByValue": True,
                        },
                        session=elements[0].session,
                    )
                    if result.get("exceptionDetails") or result.get("result", {}).get("value") != {
                        "dispatched": True
                    }:
                        raise BrowserValidationError(
                            "Selection failed; inspect the current page before retrying."
                        )
                finally:
                    # Cancellation must not retain remote handles. A failed release is
                    # bounded by session disposal, and must not mask input status.
                    for cdp_session in dict.fromkeys(element.session for element in elements):
                        with suppress(BrowserError):
                            act.call(
                                "Runtime.releaseObjectGroup",
                                {"objectGroup": group},
                                session=cdp_session,
                                cancel=None,
                            )
                return {
                    "session_id": session_id,
                    "command_generation": generation,
                    "dispatched": True,
                    **act.report(),
                }

    def select_options(
        self,
        owner_id: str,
        session_id: str,
        selector: str | None,
        *,
        option_backend_node_ids: Sequence[int],
        expected_backend_node_id: int | None = None,
        frame_id: str | None = None,
        timeout: float | None = None,
        cancel: CancelCheck = None,
        expected_generation: int | None = None,
        dialog: str = "dismiss",
        dialog_text: str | None = None,
    ) -> dict[str, Any]:
        """Choose exactly these options of a native multiple select (a list box where several
        can be chosen); the others are unselected. The select is addressed as for
        :meth:`click`; each option by its observed identity in the select's frame.

        Done in an isolated world with fixed code, so page scripts can't change what it does:
        every option must belong to that select, and neither it nor they may be disabled
        (otherwise nothing changes). Input and change events are sent only when the choice
        changed. Returns ``changed``, ``confirmed`` (the select shows every chosen option) and
        the chosen options' text as the page shows it (page data, bounded)."""
        session = self._owned_session(owner_id, session_id)
        target = _validated_target(selector, expected_backend_node_id, frame_id)
        if (
            not isinstance(option_backend_node_ids, (list, tuple))
            or not 1 <= len(option_backend_node_ids) <= _MAX_SELECTED_OPTIONS
            or len(set(option_backend_node_ids)) != len(option_backend_node_ids)
        ):
            raise BrowserValidationError(
                f"Choose 1 to {_MAX_SELECTED_OPTIONS} different options of the list."
            )
        options = [_validated_target(None, item, frame_id) for item in option_backend_node_ids]
        answer = _dialog_answer(dialog, dialog_text)
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ) as generation:
            call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
            with _Actions(
                session, timeout=call_timeout, cancel=cancel, dialog=answer, report=True
            ) as act:
                select = self._locate(act, *target)
                chosen = [self._locate(act, *option) for option in options]
                if any(option.session != select.session for option in chosen):
                    raise BrowserValidationError("The options must belong to the observed list.")
                objects = [act.isolated_object(item) for item in (select, *chosen)]
                result = act.call(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": objects[0],
                        "functionDeclaration": _SELECT_OPTIONS_FUNCTION,
                        "arguments": [{"objectId": item} for item in objects[1:]],
                        "returnByValue": True,
                    },
                    session=select.session,
                )
                state = _script_value(result, by_value=True)
                if not isinstance(state, Mapping) or state.get("dispatched") is not True:
                    raise BrowserValidationError(
                        "Nothing was chosen: "
                        + (
                            "an option is not one of this list's, or is disabled."
                            if isinstance(state, Mapping) and state.get("multiple")
                            else "this is not a list where several options can be chosen (a "
                            "dropdown takes one option), or it is disabled."
                        )
                    )
                selected = state.get("selected")
                return {
                    "session_id": session_id,
                    "command_generation": generation,
                    "dispatched": True,
                    "changed": state.get("changed") is True,
                    "confirmed": state.get("confirmed") is True,
                    "selected": [
                        _bounded_text(str(item), 512)["text"]
                        for item in (selected if isinstance(selected, list) else [])[:64]
                    ],
                    **act.report(),
                }

    def press_key(
        self,
        owner_id: str,
        session_id: str,
        selector: str | None,
        key: str,
        *,
        shift: bool = False,
        modifiers: Sequence[str] = (),
        expected_backend_node_id: int | None = None,
        frame_id: str | None = None,
        timeout: float | None = None,
        cancel: CancelCheck = None,
        expected_generation: int | None = None,
        dialog: str = "dismiss",
        dialog_text: str | None = None,
    ) -> dict[str, Any]:
        """Press one key in the element, optionally as a chord with ``modifiers`` held ("Alt",
        "Control", "Meta", "Shift", or "ControlOrMeta" for the platform's main modifier).

        ``key`` is a named key (Enter, Tab, Escape, arrows, Home/End, PageUp/PageDown,
        Backspace, Delete, Space, F1-F12) or one printable character of any script. Modifiers
        are pressed first and released last; every pressed key is released, also on failure. A
        file chooser the key opens is held and reported, as for :meth:`click`."""
        session = self._owned_session(owner_id, session_id)
        target = _validated_target(selector, expected_backend_node_id, frame_id)
        chord = _key_chord(key, shift, modifiers)
        answer = _dialog_answer(dialog, dialog_text)
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ) as generation:
            call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
            with _Actions(
                session, timeout=call_timeout, cancel=cancel, dialog=answer, report=True
            ) as act:
                element = self._locate(act, *target)
                self._focus(act, element)
                act.hold_file_choosers(element.session)
                self._press(act, chord)
                act.catch_file_choosers()
                _check_cancel(cancel)
                return {
                    "session_id": session_id,
                    "command_generation": generation,
                    "dispatched": True,
                    **act.report(),
                }

    def scroll(
        self,
        owner_id: str,
        session_id: str,
        selector: str | None,
        *,
        delta_x: int = 0,
        delta_y: int,
        expected_backend_node_id: int | None = None,
        frame_id: str | None = None,
        timeout: float | None = None,
        cancel: CancelCheck = None,
        expected_generation: int | None = None,
        dialog: str = "dismiss",
        dialog_text: str | None = None,
    ) -> dict[str, Any]:
        session = self._owned_session(owner_id, session_id)
        target = _validated_target(selector, expected_backend_node_id, frame_id)
        answer = _dialog_answer(dialog, dialog_text)
        if any(type(delta) is not int or abs(delta) > 2000 for delta in (delta_x, delta_y)) or not (
            delta_x or delta_y
        ):
            raise BrowserValidationError("Scroll needs a nonzero bounded pixel delta.")
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ) as generation:
            call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
            with _Actions(session, timeout=call_timeout, cancel=cancel, dialog=answer) as act:
                element = self._locate(act, *target)
                act.call("DOM.scrollIntoViewIfNeeded", element.ref, session=element.session)
                x, y = _placement_center(self._placement(act, element))
                metrics = act.call("Page.getLayoutMetrics", {})
                viewport = metrics.get("cssLayoutViewport", {})
                width, height = viewport.get("clientWidth"), viewport.get("clientHeight")
                if any(
                    type(v) not in (int, float) or not math.isfinite(v) or v < 2
                    for v in (width, height)
                ):
                    raise BrowserSecurityError("Browser viewport response was invalid.")
                self._show_page(act, element)
                act.call(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseWheel",
                        "x": max(1, min(x, width - 1)),
                        "y": max(1, min(y, height - 1)),
                        "deltaX": delta_x,
                        "deltaY": delta_y,
                    },
                )
                return {
                    "session_id": session_id,
                    "command_generation": generation,
                    "dispatched": True,
                    **act.report(),
                }

    def wait_for(
        self,
        owner_id: str,
        session_id: str,
        condition: str,
        *,
        value: str | None = None,
        selector: str | None = None,
        expected_backend_node_id: int | None = None,
        frame_id: str | None = None,
        timeout: float | None = None,
        cancel: CancelCheck = None,
        dialog: str = "dismiss",
        dialog_text: str | None = None,
    ) -> dict[str, Any]:
        """Wait, at most ``timeout`` seconds (the operation timeout by default), for the page:

        * ``"text"``: some frame of the page shows ``value`` (compared Unicode-normalised and
          case-folded, then also without diacritics, the same way for every language);
        * ``"element_present"`` / ``"element_gone"``: the element (addressed as for
          :meth:`click`) is in the page and visible / was removed or hidden;
        * ``"address"``: the page's address contains ``value`` (compared decoded, the same way);
        * ``"load"``: the page finished loading (``value`` "load", the default) or its document
          is ready (``value`` "domcontentloaded").

        Sends no input. Raises :class:`BrowserWaitTimeoutError` when the time runs out. A frame
        that navigates during a check is checked again on its new document."""
        session = self._owned_session(owner_id, session_id)
        check = _wait_condition(condition, value, selector, expected_backend_node_id, frame_id)
        answer = _dialog_answer(dialog, dialog_text)
        budget = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
        with self._operation(session, cancel=cancel) as generation:
            call_timeout = self._config.operation_timeout_seconds
            with _Actions(session, timeout=call_timeout, cancel=cancel, dialog=answer) as act:
                started = self._monotonic()
                deadline = started + budget
                while True:
                    _check_cancel(cancel)
                    if self._condition_met(act, check):
                        return {
                            "session_id": session_id,
                            "command_generation": generation,
                            "condition": check[0],
                            "satisfied": True,
                            "waited_seconds": round(self._monotonic() - started, 3),
                            **act.report(),
                        }
                    if self._monotonic() >= deadline:
                        raise BrowserWaitTimeoutError(
                            f"The page did not reach the awaited state within {budget:g} "
                            "seconds. Nothing was changed."
                        )
                    self._sleep(min(_WAIT_POLL_SECONDS, max(0.0, deadline - self._monotonic())))

    def _condition_met(self, act: _Actions, check: tuple[Any, ...]) -> bool:
        condition = check[0]
        frames = _frames_of(act.call("Page.getFrameTree", {}))
        if condition == "address":
            return _address_contains(frames[0]["url"], check[1])
        if condition in {"element_present", "element_gone"}:
            present = self._element_shown(act, check[2], check[3], check[4])
            return present if condition == "element_present" else not present
        # A frame can navigate between the frame list and the read; its old document's isolated
        # world is then gone. Read again on the new document, but only when the frame list shows
        # that it changed: any other failure is reported.
        act.forget_worlds()
        try:
            if condition == "load":
                state = act.evaluate(act.page, None, "document.readyState")
                return state == "complete" or (
                    check[1] == "domcontentloaded" and state == "interactive"
                )
            return any(_text_contains(text, check[1]) for text in self._frame_texts(act, frames))
        except (BrowserCancelledError, BrowserTimeoutError, BrowserLimitError):
            raise
        except BrowserError:
            if not act.transport.healthy:
                raise
            if _frames_of(act.call("Page.getFrameTree", {})) == frames:
                raise
            return False

    def _frame_texts(self, act: _Actions, frames: list[dict[str, Any]]) -> Iterable[str]:
        limit = self._config.max_snapshot_bytes
        expression = f"document.body ? document.body.innerText.slice(0, {int(limit)}) : ''"
        for index, frame in enumerate(frames[:_MAX_FRAMES]):
            yield str(act.evaluate(act.page, None if index == 0 else frame["id"], expression) or "")
        for child in list(self._out_of_process_frames(act).values())[:_MAX_FRAMES]:
            for frame in _frames_of(act.call("Page.getFrameTree", {}, session=child))[:_MAX_FRAMES]:
                yield str(act.evaluate(child, frame["id"], expression) or "")

    def _element_shown(
        self, act: _Actions, selector: str | None, identity: int | None, frame_id: str | None
    ) -> bool:
        if selector is not None:
            document = act.call("DOM.getDocument", {"depth": 0, "pierce": False})
            root = document.get("root")
            if not isinstance(root, Mapping):
                raise BrowserSecurityError("Browser DOM response was invalid.")
            matched = act.call(
                "DOM.querySelector",
                {"nodeId": _required_cdp_int(root, "nodeId"), "selector": selector},
            )
            node_id = _required_cdp_int(matched, "nodeId")
            if node_id <= 0:
                return False
            described = act.call("DOM.describeNode", {"nodeId": node_id, "depth": 0})
            node = described.get("node")
            if not isinstance(node, Mapping):
                raise BrowserSecurityError("Browser DOM response was invalid.")
            element = _Element(act.page, None, _required_cdp_int(node, "backendNodeId"))
        else:
            try:
                element = self._locate(act, None, identity, frame_id)
            except BrowserNotFoundError:
                return False  # its frame or the element itself is no longer in the page
        state = act.run(element, _PROBE_FUNCTION)
        return (
            isinstance(state, Mapping)
            and state.get("attached") is True
            and state.get("visible") is True
        )

    def handle_dialog(
        self,
        owner_id: str,
        session_id: str,
        *,
        accept: bool,
        prompt_text: str | None = None,
        frame_id: str | None = None,
        expected_generation: int | None = None,
        timeout: float | None = None,
        cancel: CancelCheck = None,
    ) -> dict[str, Any]:
        """Answer the JavaScript dialog (alert, confirm, prompt, leave-page) open on the page:
        accept it, with ``prompt_text`` typed into a prompt, or dismiss it.

        Use it for a dialog the page opened by itself. A dialog opened by an owl's input is
        answered by that input's ``dialog`` choice. ``frame_id`` answers only the dialog shown
        by that frame, as a dialog report names it ("" for a dialog whose frame the browser did
        not name), so an answer meant for one frame's dialog never reaches another's. Raises
        :class:`BrowserNoDialogError` when no such dialog is open. Returns the dialog's type,
        message (page text, bounded) and frame."""
        session = self._owned_session(owner_id, session_id)
        if type(accept) is not bool:
            raise BrowserValidationError("Accept must be true or false.")
        if frame_id is not None and frame_id != "" and _dialog_frame_id(frame_id) is None:
            raise BrowserValidationError("The dialog's frame is invalid.")
        answer = _dialog_answer("accept" if accept else "dismiss", prompt_text)
        with self._operation(
            session, cancel=cancel, mutation=True, expected_generation=expected_generation
        ) as generation:
            call_timeout = _bounded_timeout(timeout, self._config.operation_timeout_seconds)
            with _Actions(
                session, timeout=call_timeout, cancel=cancel, dialog=answer, step=False, report=True
            ) as act:
                for cdp_session in self._out_of_process_frames(act).values():
                    act.watch(cdp_session)
                if not callable(getattr(act.transport, "open_dialog", None)):
                    for cdp_session in tuple(act.watched):
                        act.events(cdp_session)
                answered = act.answer_open_dialog(frame_id=frame_id)
                if answered is None:
                    raise BrowserNoDialogError(
                        "No dialog is open on this page. Nothing was answered."
                    )
                return {
                    "session_id": session_id,
                    "command_generation": generation,
                    "dialog": answered,
                }

    def _out_of_process_frames(self, act: _Actions) -> dict[str, str]:
        """Guarded out-of-process iframes of the page: frame id -> CDP session (nested ones too);
        none when the transport cannot name them."""
        lister = getattr(act.transport, "guarded_frame_sessions", None)
        if not callable(lister):
            return {}
        frames = lister(session_id=act.page)
        if not isinstance(frames, Mapping) or len(frames) > _MAX_FRAMES:
            raise BrowserLimitError("This page has too many separate frames to act in.")
        for frame_id, child in frames.items():
            if not (isinstance(frame_id, str) and isinstance(child, str) and frame_id and child):
                raise BrowserSecurityError("Browser frame sessions were invalid.")
        return dict(frames)

    def _frame_route(
        self, act: _Actions, frame_id: str | None
    ) -> tuple[str, str | None, tuple[tuple[str, int], ...]]:
        """The CDP session holding ``frame_id`` (None: the page's main frame), the frame, and the
        out-of-process iframe elements around it, outermost first."""
        frames = _frames_of(act.call("Page.getFrameTree", {}))
        if frame_id is None or frame_id == frames[0]["id"]:
            return act.page, None, ()
        if any(frame["id"] == frame_id for frame in frames):
            return act.page, frame_id, ()
        children = self._out_of_process_frames(act)
        if not children and not callable(getattr(act.transport, "guarded_frame_sessions", None)):
            raise BrowserNotFoundError(
                "That frame runs in a separate browser process this connection cannot reach."
            )
        holders = {frame["id"]: act.page for frame in frames}
        parents = {frame["id"]: frame["parent"] for frame in frames}
        roots: dict[str, str] = {}

        def load(child: str) -> None:
            child_frames = _frames_of(act.call("Page.getFrameTree", {}, session=child))
            roots[child] = child_frames[0]["id"]
            for frame in child_frames:
                holders.setdefault(frame["id"], child)
                parents.setdefault(frame["id"], frame["parent"])

        def find(wanted: str) -> None:
            if wanted in children and children[wanted] not in roots:
                load(children[wanted])
            for child in children.values():
                if wanted in holders:
                    return
                if child not in roots:
                    load(child)

        find(frame_id)
        if frame_id not in holders:
            raise BrowserNotFoundError(
                "The observed frame is no longer on this page; take a fresh snapshot."
            )
        hosts: list[tuple[str, int]] = []
        current = holders[frame_id]
        while current != act.page:
            if len(hosts) >= _MAX_FRAME_HOPS:
                raise BrowserLimitError("The observed frame is nested too deeply to act in.")
            root = roots[current]
            parent = parents.get(root)
            if parent is not None:
                find(parent)
            holder = holders.get(parent) if parent is not None else None
            if holder is None or holder == current:
                raise BrowserNotFoundError(
                    "The observed frame is no longer on this page; take a fresh snapshot."
                )
            owner = act.call("DOM.getFrameOwner", {"frameId": root}, session=holder)
            hosts.append((holder, _required_cdp_int(owner, "backendNodeId")))
            current = holder
        hosts.reverse()
        return holders[frame_id], frame_id, tuple(hosts)

    def _locate(
        self,
        act: _Actions,
        selector: str | None,
        expected_backend_node_id: int | None,
        frame_id: str | None,
    ) -> _Element:
        """Find the element afresh: by a top-document selector (checked against the observed
        identity when given), or by its observed identity in its frame. An identity must still
        be connected to that frame's document, so a replaced element is never acted on."""
        if selector is not None:
            backend = self._query_node(
                act, selector, expected_backend_node_id=expected_backend_node_id
            )
            element = _Element(act.page, None, backend)
            self._note_dialog_origin(act, element)
            return element
        if expected_backend_node_id is None:  # guarded by _validated_target
            raise BrowserValidationError(
                "Address an element by a selector or its observed identity."
            )
        cdp_session, frame, hosts = self._frame_route(act, frame_id)
        act.call("DOM.getDocument", {"depth": 0, "pierce": False}, session=cdp_session)
        pushed = act.call(
            "DOM.pushNodesByBackendIdsToFrontend",
            {"backendNodeIds": [expected_backend_node_id]},
            session=cdp_session,
        ).get("nodeIds")
        if not isinstance(pushed, list) or len(pushed) != 1 or type(pushed[0]) is not int:
            raise BrowserSecurityError("Browser DOM response was invalid.")
        if pushed[0] <= 0:
            raise BrowserNotFoundError(_CHANGED_ELEMENT)
        element = _Element(cdp_session, frame, expected_backend_node_id, hosts)
        _require_element(act.run(element, _PROBE_FUNCTION))
        if hosts:
            # Dialogs of an out-of-process frame are reported on its own session.
            act.call("Page.enable", {}, session=cdp_session)
            act.watch(cdp_session)
        self._note_dialog_origin(act, element)
        return element

    @staticmethod
    def _note_dialog_origin(act: _Actions, element: _Element) -> None:
        """When the step accepts a dialog it opens, note the security origin of the frame that
        holds its element: only a dialog shown by a frame of that origin is accepted. Read
        before any input, while the frame still answers commands."""
        if not act.accepts_dialog:
            return
        frames = _frames_of(act.call("Page.getFrameTree", {}, session=element.session))
        wanted = element.frame_id or frames[0]["id"]
        origin = next(
            (_address_origin(item["origin"]) for item in frames if item["id"] == wanted), None
        )
        if origin is not None:
            act.dialog_origins.add(origin)

    def _read(self, act: _Actions, element: _Element) -> Mapping[str, Any]:
        state = act.run(element, _READ_FUNCTION)
        _require_element(state)
        return state

    def _focus(self, act: _Actions, element: _Element) -> None:
        self._wait_visible(act, element)
        act.call("DOM.focus", element.ref, session=element.session)
        self._assert_focused(act, element)

    def _clear(self, act: _Actions, element: _Element) -> None:
        modifier = 4 if os.sys.platform == "darwin" else 2  # Meta or Control CDP mask.
        act.call(
            "Input.dispatchKeyEvent",
            {
                "type": "keyDown",
                "key": "a",
                "code": "KeyA",
                "modifiers": modifier,
                "windowsVirtualKeyCode": 65,
                "commands": ["selectAll"],
            },
        )
        act.call(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": "a",
                "code": "KeyA",
                "modifiers": modifier,
                "windowsVirtualKeyCode": 65,
            },
        )
        self._assert_focused(act, element)
        for event_type in ("keyDown", "keyUp"):
            act.call(
                "Input.dispatchKeyEvent",
                {
                    "type": event_type,
                    "key": "Backspace",
                    "code": "Backspace",
                    "windowsVirtualKeyCode": 8,
                },
            )

    @staticmethod
    def _press(act: _Actions, chord: _KeyChord) -> None:
        pressed: list[str] = []
        held = 0
        attempted = False
        try:
            for name in chord.modifiers:
                bit, key_name, code, key_code = _MODIFIERS[name]
                held |= bit
                act.call(
                    "Input.dispatchKeyEvent",
                    {
                        "type": "keyDown",
                        "key": key_name,
                        "code": code,
                        "windowsVirtualKeyCode": key_code,
                        "modifiers": held,
                    },
                )
                pressed.append(name)
            attempted = True
            act.call("Input.dispatchKeyEvent", {**chord.down, "modifiers": held})
        finally:
            # Even when cancellation happens between key events, release every pressed
            # key. Do not leave one pressed in a reusable session.
            if attempted:
                act.call("Input.dispatchKeyEvent", {**chord.up, "modifiers": held}, cancel=None)
            for name in reversed(pressed):
                bit, key_name, code, key_code = _MODIFIERS[name]
                held &= ~bit
                act.call(
                    "Input.dispatchKeyEvent",
                    {
                        "type": "keyUp",
                        "key": key_name,
                        "code": code,
                        "windowsVirtualKeyCode": key_code,
                        "modifiers": held,
                    },
                    cancel=None,
                )

    def _has_focus(self, act: _Actions, element: _Element) -> bool:
        # DOM.focus can synchronously trigger page handlers which redirect focus.
        # Inspect Chromium's actual accessibility focus, not page-overridable JS.
        tree = act.call(
            "Accessibility.getPartialAXTree",
            {**element.ref, "fetchRelatives": False},
            session=element.session,
        )
        return any(
            item.get("backendDOMNodeId") == element.backend_node_id
            and any(
                p.get("name") == "focused" and p.get("value", {}).get("value") is True
                for p in item.get("properties", [])
            )
            for item in tree.get("nodes", [])
        )

    def _assert_focused(self, act: _Actions, element: _Element) -> None:
        if not self._has_focus(act, element):
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
        act: _Actions,
        selector: str,
        *,
        expected_backend_node_id: int | None = None,
    ) -> int:
        """Resolve a top-document selector afresh to its element's backend node identity."""
        document = act.call("DOM.getDocument", {"depth": 0, "pierce": False})
        root = document.get("root")
        if not isinstance(root, Mapping):
            raise BrowserSecurityError("Browser DOM response was invalid.")
        root_id = _required_cdp_int(root, "nodeId")
        matched = act.call("DOM.querySelector", {"nodeId": root_id, "selector": selector})
        node_id = _required_cdp_int(matched, "nodeId")
        if node_id <= 0:
            raise BrowserNotFoundError("Browser element was not found.")
        described = act.call("DOM.describeNode", {"nodeId": node_id, "depth": 0})
        node = described.get("node")
        if not isinstance(node, Mapping):
            raise BrowserSecurityError("Browser DOM response was invalid.")
        backend = _required_cdp_int(node, "backendNodeId")
        if expected_backend_node_id is not None and backend != expected_backend_node_id:
            # Resolve CSS afresh, but do not let a stale selector silently target a
            # replacement node. The identity must come from this session's original DOM
            # snapshot. This is opt-in; it does not fence viewer input or guarantee layout
            # stability after the check.
            raise BrowserNotFoundError(_CHANGED_ELEMENT)
        return backend


_INHERIT: Any = object()
# JSON-RPC "invalid params": Page.handleJavaScriptDialog's answer when no dialog is showing.
_CDP_INVALID_PARAMS = -32602


class _Actions:
    """The browser commands of one operation on the active page.

    Each command runs on a helper thread while this thread watches the page for a JavaScript
    dialog: a page blocked by a dialog answers no command until the dialog is answered, and a
    command left waiting would end the browser's control connection. Every answer is reported.
    Isolated worlds and remote objects made for the operation are released when it ends.

    A ``step`` (an operation that gives the page input) accepts only a dialog it opens itself:
    its ``dialog`` answer accepts one dialog at most, the first one shown by a frame of the
    origin it acts in (``dialog_origins``, filled as its elements are located when it accepts),
    and any other dialog during the step is dismissed. A step that accepts refuses to start while
    the page already shows a dialog (BrowserDialogOpenError, nothing sent): that one was not its
    own. Any step refuses to send a command to a frame it has not reached yet that already shows
    a dialog, which would answer nothing. Otherwise (``step=False``: answering the open dialog on
    request) ``dialog`` is the answer. With ``report`` the operation is an acting step that
    opens the page's report of it (see ManagedBrowserService.page_report).
    """

    def __init__(
        self,
        session: _BrowserSession,
        *,
        timeout: float,
        cancel: CancelCheck,
        dialog: tuple[bool, str | None] = (False, None),
        step: bool = True,
        report: bool = False,
    ) -> None:
        self.session = session
        self.transport = session.transport
        self.page = session.cdp_session_id
        self.timeout = timeout
        self.cancel = cancel
        self._accept, self._prompt_text = dialog
        self._step = step
        self._report = report
        # Security origins of the frames this step acts in, when it accepts its own dialog.
        self.dialog_origins: set[str] = set()
        self._accepted = False
        self.dialogs: list[dict[str, Any]] = []
        self._watched = [self.page]
        self._worlds: dict[tuple[str, str | None], int] = {}
        self._group = "alysis-actions-" + secrets.token_hex(8)
        self._grouped: set[str] = set()
        self._executor: ThreadPoolExecutor | None = None
        # Sessions on which this operation holds file choosers instead of the browser showing
        # them (hold_file_choosers), and the choosers its input opened there, in order.
        self._choosers_held: list[str] = []
        self.file_choosers: list[dict[str, Any]] = []

    def __enter__(self) -> _Actions:
        if self.accepts_dialog:
            opened = _open_dialog(self.session, self._watched)
            if opened is not None:
                # The page opened it before this step, so the step's yes is not for it. Nothing
                # was sent.
                raise _dialog_open_error(opened[1])
        if self._report:
            # What the page reports from now on belongs to this step (page_report). Requests
            # are judged by when the browser started them, so events still unread from before
            # the step are left where they are and count for nothing here.
            self.session.page_report = _PageReport(self.page, time.time())
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            for cdp_session in self._choosers_held:
                # The browser shows file choosers again (a person at the computer uses them).
                with suppress(BrowserError):
                    self.call(
                        "Page.setInterceptFileChooserDialog",
                        {"enabled": False},
                        session=cdp_session,
                        cancel=None,
                    )
            for cdp_session in self._grouped:
                # A failed release is bounded by session disposal, and must not mask the
                # operation's own outcome.
                with suppress(BrowserError):
                    self.call(
                        "Runtime.releaseObjectGroup",
                        {"objectGroup": self._group},
                        session=cdp_session,
                        cancel=None,
                    )
        finally:
            if self._executor is not None:
                self._executor.shutdown(wait=True)

    @property
    def accepts_dialog(self) -> bool:
        """Whether this step was told to accept a dialog it opens (see dialog_origins)."""
        return self._step and self._accept

    def report(self) -> dict[str, Any]:
        """Dialogs this operation answered and the file chooser its input opened, for its
        result. A chooser is the file input that opened it (its identity, its frame, and that
        frame's address and document loader), answered with upload_file(expected_frame_loader=);
        without an input it can't be answered."""
        chooser = self.file_choosers[-1] if self.file_choosers else None
        return {
            **({"dialogs": list(self.dialogs)} if self.dialogs else {}),
            **(
                {
                    "file_chooser": {
                        key: chooser[key]
                        for key in ("backend_node_id", "frame_id", "url", "loader", "multiple")
                        if chooser.get(key) is not None
                    }
                }
                if chooser is not None
                else {}
            ),
        }

    def hold_file_choosers(self, cdp_session: str) -> None:
        """Hold a file chooser this operation's input opens, in the page and in the frame of
        ``cdp_session``, instead of the browser showing it (Page.fileChooserOpened reports it).
        Until the operation ends: a person at the computer gets the browser's own chooser."""
        for target in dict.fromkeys((self.page, cdp_session)):
            if target not in self._choosers_held:
                self.call("Page.setInterceptFileChooserDialog", {"enabled": True}, session=target)
                self._choosers_held.append(target)

    def catch_file_choosers(self) -> None:
        """After the input, read the holding sessions once more: a chooser the input opened is
        reported just after it (one more command reaches it). Each chooser gets its frame's
        address, which says which site the file would go to."""
        for target in self._choosers_held:
            tree = self.call("Page.getFrameTree", {}, session=target)
            self.events(target)
            for chooser in self.file_choosers:
                if chooser["session"] == target and "url" not in chooser:
                    frame = next(
                        (f for f in _frames_of(tree) if f["id"] == chooser["frame_id"]), None
                    )
                    chooser["url"] = _dialog_frame_url(frame["url"]) if frame else None
                    chooser["loader"] = frame["loader"] if frame else None

    def _note_file_choosers(self, cdp_session: str, events: Sequence[Mapping[str, Any]]) -> None:
        if cdp_session not in self._choosers_held:
            return
        for event in events:
            params = event.get("params")
            if event.get("method") != "Page.fileChooserOpened" or not isinstance(params, Mapping):
                continue
            backend = params.get("backendNodeId")
            self.file_choosers.append(
                {
                    "session": cdp_session,
                    "frame_id": _dialog_frame_id(params.get("frameId")),
                    "backend_node_id": backend if type(backend) is int and backend > 0 else None,
                    "multiple": params.get("mode") == "selectMultiple",
                }
            )

    def watch(self, cdp_session: str) -> None:
        if cdp_session not in self._watched:
            self._watched.append(cdp_session)

    @property
    def watched(self) -> tuple[str, ...]:
        return tuple(self._watched)

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session: str | None = None,
        cancel: Any = _INHERIT,
    ) -> Mapping[str, Any]:
        check = self.cancel if cancel is _INHERIT else cancel
        target = session or self.page
        if self._step and target not in self._watched:
            # A frame this step has not reached yet that already shows a dialog: it would answer
            # nothing until the dialog is answered, and this step did not open it.
            opened = _open_dialog(self.session, (target,))
            if opened is not None:
                raise _dialog_open_error(opened[1])
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="alysis-browser-command"
            )
        future = self._executor.submit(
            self.transport.call,
            method,
            dict(params or {}),
            session_id=target,
            timeout=self.timeout,
            cancel=check,
        )
        wait_for_futures((future,), timeout=_DIALOG_CHECK_SECONDS)
        while not future.done():
            for cdp_session in tuple(self._watched):
                if future.done():
                    break
                self.events(cdp_session, cancel=check)
                if not future.done() and _open_dialog(self.session, self._watched) is not None:
                    self.answer_open_dialog(cancel=check)
        return future.result()

    def events(self, cdp_session: str, *, cancel: Any = _INHERIT) -> Sequence[Mapping[str, Any]]:
        """Read the session's pending events, noting dialogs and keeping diagnostic events."""
        found = self.transport.drain_events(
            session_id=cdp_session,
            max_events=256,
            timeout=_DIALOG_CHECK_SECONDS,
            cancel=self.cancel if cancel is _INHERIT else cancel,
        )
        _note_events(self.session, cdp_session, found)
        self._note_file_choosers(cdp_session, found)
        return found

    def answer_open_dialog(
        self, *, cancel: Any = _INHERIT, frame_id: str | None = None
    ) -> dict[str, Any] | None:
        """Answer the dialog open on a watched session: the first one, or with ``frame_id`` the
        one that frame shows ("" for a dialog whose frame the browser did not name)."""
        opened = next(
            (
                found
                for found in _open_dialogs(self.session, self._watched)
                if frame_id is None or (found[1].get("frame_id") or "") == frame_id
            ),
            None,
        )
        if opened is None:
            return None
        cdp_session, details = opened
        accept = self._accept
        if self._step:
            accept = (
                accept
                and not self._accepted
                and _address_origin(details.get("url")) in self.dialog_origins
            )
        params: dict[str, Any] = {"accept": accept}
        if accept and details["type"] == "prompt":
            params["promptText"] = (
                self._prompt_text if self._prompt_text is not None else details["default_prompt"]
            )
        try:
            self.transport.call(
                "Page.handleJavaScriptDialog",
                params,
                session_id=cdp_session,
                timeout=self.timeout,
                cancel=self.cancel if cancel is _INHERIT else cancel,
            )
        except BrowserError as exc:
            # The browser answers "invalid params" when no dialog is showing: this one closed
            # (for example by a navigation) before its closing event was read.
            if getattr(exc, "code", None) != _CDP_INVALID_PARAMS:
                raise
            self.session.dialog = None
            return None
        self.session.dialog = None
        self._accepted = self._accepted or accept
        answered = {
            "type": details["type"],
            "message": details["message"],
            "accepted": accept,
            **{key: details[key] for key in ("url", "frame_id") if details.get(key)},
        }
        self.dialogs.append(answered)
        return answered

    def forget_worlds(self) -> None:
        self._worlds.clear()

    def world(self, cdp_session: str, frame_id: str | None) -> int:
        """This operation's isolated world in the frame (None: the session's main frame)."""
        key = (cdp_session, frame_id)
        if key not in self._worlds:
            frame = frame_id
            if frame is None:
                frame = _frames_of(self.call("Page.getFrameTree", {}, session=cdp_session))[0]["id"]
            made = self.call(
                "Page.createIsolatedWorld",
                {"frameId": frame, "worldName": _ISOLATED_WORLD},
                session=cdp_session,
            )
            self._worlds[key] = _required_cdp_int(made, "executionContextId")
        return self._worlds[key]

    def run(self, element: _Element, function: str, *, by_value: bool = True) -> Any:
        """Run a fixed function on the element in its frame's isolated world."""
        return self.call_on(element, self.isolated_object(element), function, by_value=by_value)

    def isolated_object(self, element: _Element) -> str:
        """The element as an object of its frame's isolated world, released with the operation."""
        context = self.world(element.session, element.frame_id)
        resolved = self.call(
            "DOM.resolveNode",
            {**element.ref, "executionContextId": context, "objectGroup": self._group},
            session=element.session,
        )
        self._grouped.add(element.session)
        remote = resolved.get("object")
        object_id = remote.get("objectId") if isinstance(remote, Mapping) else None
        if not isinstance(object_id, str) or not object_id:
            raise BrowserNotFoundError(_CHANGED_ELEMENT)
        return object_id

    def document_object(self, cdp_session: str) -> str:
        """The root element of the session's main document, as an isolated-world object."""
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": "document.documentElement",
                "contextId": self.world(cdp_session, None),
                "objectGroup": self._group,
                "returnByValue": False,
            },
            session=cdp_session,
        )
        self._grouped.add(cdp_session)
        return _script_value(result, by_value=False)

    def call_on(
        self, element: _Element, object_id: str, function: str, *, by_value: bool = True
    ) -> Any:
        result = self.call(
            "Runtime.callFunctionOn",
            {"objectId": object_id, "functionDeclaration": function, "returnByValue": by_value},
            session=element.session,
        )
        return _script_value(result, by_value=by_value)

    def evaluate(
        self,
        cdp_session: str,
        frame_id: str | None,
        expression: str,
        *,
        await_promise: bool = False,
    ) -> Any:
        """Evaluate a fixed expression in the frame's isolated world."""
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "contextId": self.world(cdp_session, frame_id),
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
            session=cdp_session,
        )
        return _script_value(result, by_value=True)


@dataclass(frozen=True, slots=True)
class _KeyChord:
    modifiers: tuple[str, ...]
    down: dict[str, Any]
    up: dict[str, Any]


def _script_value(result: Mapping[str, Any], *, by_value: bool) -> Any:
    if result.get("exceptionDetails") is not None:
        raise BrowserError("A fixed page check could not run in this browser.")
    remote = result.get("result")
    if not isinstance(remote, Mapping):
        raise BrowserSecurityError("Browser script response was invalid.")
    if by_value:
        return remote.get("value")
    object_id = remote.get("objectId")
    if not isinstance(object_id, str) or not object_id:
        raise BrowserSecurityError("Browser script response was invalid.")
    return object_id


def _note_events(
    session: _BrowserSession,
    cdp_session: str,
    events: Sequence[Mapping[str, Any]],
    *,
    hold: bool = True,
) -> None:
    """Track the page's JavaScript dialog and, with ``hold``, keep diagnostic events for
    diagnostics."""
    for event in events:
        if not isinstance(event, Mapping):
            raise BrowserSecurityError("Browser event was invalid.")
        method = event.get("method")
        params = event.get("params")
        params = params if isinstance(params, Mapping) else {}
        _note_page_event(session, cdp_session, method, params)
        if method == "Page.javascriptDialogOpening":
            session.dialog = (
                cdp_session,
                _dialog_details(
                    {
                        "type": params.get("type"),
                        "message": params.get("message"),
                        "default_prompt": params.get("defaultPrompt"),
                        "url": params.get("url"),
                        "frame_id": params.get("frameId"),
                    }
                ),
            )
        elif method == "Page.javascriptDialogClosed":
            if session.dialog is not None and session.dialog[0] == cdp_session:
                session.dialog = None
        elif hold and method in _DIAGNOSTIC_METHODS:
            session.held_events.append((cdp_session, event))


def _note_page_event(
    session: _BrowserSession, cdp_session: str, method: Any, params: Mapping[str, Any]
) -> None:
    """Remember the active page's requests, and add to the open page report what it reports
    (see ManagedBrowserService.page_report). Only fixed fields are read: a request's method,
    address, kind and site, a response's status, Chromium's error name, and that a script
    error happened."""
    report = session.page_report
    if method == "Network.requestWillBeSent":
        if cdp_session != session.cdp_session_id and (
            report is None or cdp_session != report.cdp_session
        ):
            return
        request = params.get("request")
        request_id = params.get("requestId")
        if not isinstance(request, Mapping) or not isinstance(request_id, str):
            return
        url = request.get("url")
        if not isinstance(url, str) or not url:
            return
        method_name = request.get("method")
        same_site = request.get("isSameSite")
        document_url = params.get("documentURL")
        key = (cdp_session, request_id)
        session.page_requests.pop(key, None)
        session.page_requests[key] = _PageRequest(
            method=method_name.upper()
            if isinstance(method_name, str) and _REQUEST_METHOD_RE.fullmatch(method_name)
            else None,
            url=url[:_URL_MAX_CHARS],
            kind=params.get("type") if isinstance(params.get("type"), str) else None,
            same_site=same_site if isinstance(same_site, bool) else None,
            document_url=document_url if isinstance(document_url, str) else None,
            started=_epoch_seconds(params.get("wallTime"), 1.0),
        )
        while len(session.page_requests) > _TRACKED_REQUESTS:
            session.page_requests.popitem(last=False)
        return
    if method not in {
        "Network.responseReceived",
        "Network.loadingFailed",
        "Network.loadingFinished",
        "Runtime.exceptionThrown",
    }:
        return
    request_id = params.get("requestId")
    known = (
        session.page_requests.get((cdp_session, request_id))
        if isinstance(request_id, str)
        else None
    )
    if method in {"Network.loadingFailed", "Network.loadingFinished"} and known is not None:
        # The request is over: nothing more is reported for it.
        del session.page_requests[(cdp_session, request_id)]
    if report is None or cdp_session != report.cdp_session:
        return
    if method == "Runtime.exceptionThrown":
        # Script errors that started before the step are not the step's.
        if _epoch_seconds(params.get("timestamp"), 1000.0) >= report.opened_at:
            report.errors += 1
        return
    if (
        method == "Network.loadingFinished"
        or known is None
        or known.started < report.opened_at
        or request_id in report.seen
    ):
        return
    kind = params.get("type") if isinstance(params.get("type"), str) else known.kind
    if kind not in _REPORTED_KINDS or not _own_site(known):
        return
    path = _reported_path(known.url)
    if path is None:
        return
    entry: dict[str, Any] = {**({"method": known.method} if known.method else {}), "path": path}
    if method == "Network.responseReceived":
        response = params.get("response")
        status = response.get("status") if isinstance(response, Mapping) else None
        if isinstance(status, float) and status.is_integer():
            status = int(status)
        if isinstance(status, bool) or not isinstance(status, int) or not 400 <= status <= 999:
            return
        entry["status"] = status
    else:
        if params.get("canceled") is True:
            # Cancelled by the page or by leaving it (a navigation ends its requests): not a
            # failure the site reported.
            return
        error = params.get("errorText")
        entry["failure"] = (
            error if isinstance(error, str) and _NET_ERROR_RE.fullmatch(error) else "failed"
        )
    report.seen.add(request_id)
    if len(report.failed) < _REPORTED_REQUESTS:
        report.failed.append(entry)
    else:
        report.more += 1


def _epoch_seconds(value: Any, per_second: float) -> float:
    """A browser's time since the epoch (in seconds, or ``per_second`` units of a second) as
    seconds, read on the system clock this service also reads; now when the browser gave
    none."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return time.time()
    return float(value) / per_second


def _own_site(request: _PageRequest) -> bool:
    """Whether a request is the page's own: on the page's site as the browser judges it, or,
    when the browser does not say, from the same origin as the document that made it."""
    if request.same_site is not None:
        return request.same_site
    origin = _address_origin(request.url)
    return origin is not None and origin == _address_origin(request.document_url)


def _reported_path(url: str) -> str | None:
    """A request's path as a page report gives it: no query, fragment or credentials, ids
    (numbers, UUIDs, long hexadecimal or mixed letters and digits) as ``:id``, redacted and
    bounded."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"}:
        return None
    path = _PATH_TOKEN_RE.sub(_path_token, _PATH_UUID_RE.sub(":id", parts.path or "/"))
    return str(redact_secrets(path))[:_REPORTED_PATH_CHARS]


def _path_token(match: re.Match[str]) -> str:
    token = match.group(0)
    if token.startswith("%"):
        return token
    digits = sum(character.isdigit() for character in token)
    if digits and (
        digits == len(token) or len(token) >= 16 or (len(token) >= 8 and _HEX_RE.fullmatch(token))
    ):
        return ":id"
    return token


def _dialog_details(raw: Mapping[str, Any]) -> dict[str, Any]:
    """A dialog's type, message (page text: bounded and redacted), default prompt text, and the
    frame that shows it when the browser named it: its address (``url``) and ``frame_id``."""
    return {
        "type": str(raw.get("type") or "alert")[:32],
        "message": _bounded_text(str(raw.get("message") or ""), _DIALOG_TEXT_MAX_BYTES)["text"],
        "default_prompt": str(raw.get("default_prompt") or "")[:_TYPE_MAX_CHARS],
        "url": _dialog_frame_url(raw.get("url")),
        "frame_id": _dialog_frame_id(raw.get("frame_id")),
    }


def _dialog_frame_url(value: Any) -> str | None:
    """The address of the frame showing a dialog, as a dialog report carries it (redacted and
    bounded like a frame's address in a snapshot); None when the browser gave none."""
    if not isinstance(value, str) or not value:
        return None
    return str(redact_secrets(value))[:_FRAME_URL_CHARS]


def _dialog_frame_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _FRAME_ID_RE.fullmatch(value) else None


def _dialog_open_error(details: Mapping[str, Any]) -> BrowserDialogOpenError:
    return BrowserDialogOpenError(
        details["type"], details["message"], details.get("url"), details.get("frame_id")
    )


def _address_origin(url: Any) -> str | None:
    """The web origin (scheme://host[:port], as a frame's security origin is written) of an
    address, or None for one without a web origin (about:blank, data:, a missing address)."""
    try:
        parts = urlsplit(str(url or ""))
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return None
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    default = 443 if scheme == "https" else 80
    return f"{scheme}://{host}" + (f":{port}" if port not in (None, default) else "")


def _open_dialogs(
    session: _BrowserSession, cdp_sessions: Iterable[str]
) -> Iterator[tuple[str, dict[str, Any]]]:
    """The JavaScript dialogs shown on these CDP sessions, each with its session.

    A transport that tracks dialogs from the browser's events answers without a command;
    otherwise the dialog last seen in events this service read counts."""
    tracker = getattr(session.transport, "open_dialog", None)
    for cdp_session in cdp_sessions:
        if callable(tracker):
            found = tracker(session_id=cdp_session)
            if found is not None:
                yield cdp_session, _dialog_details(found)
        elif session.dialog is not None and session.dialog[0] == cdp_session:
            yield session.dialog


def _open_dialog(
    session: _BrowserSession, cdp_sessions: Iterable[str]
) -> tuple[str, dict[str, Any]] | None:
    """The first JavaScript dialog shown on one of these CDP sessions, and its session."""
    return next(_open_dialogs(session, cdp_sessions), None)


def _frames_of(tree: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The frames of a CDP frame tree in document order, main frame first: id, parent, url, the
    security origin of its document (which a blank or srcdoc frame inherits) and the loader of
    the current document."""
    root = tree.get("frameTree") if isinstance(tree, Mapping) else None
    pending = [root]
    found: list[dict[str, Any]] = []
    while pending:
        node = pending.pop()
        frame = node.get("frame") if isinstance(node, Mapping) else None
        frame_id = frame.get("id") if isinstance(frame, Mapping) else None
        children = node.get("childFrames") or [] if isinstance(node, Mapping) else None
        if not isinstance(frame_id, str) or not frame_id or not isinstance(children, list):
            raise BrowserSecurityError("Browser frame tree was invalid.")
        if len(found) >= 4 * _MAX_FRAMES:
            raise BrowserLimitError("This page has too many frames to act in.")
        parent = frame.get("parentId")
        found.append(
            {
                "id": frame_id[:256],
                "parent": parent if isinstance(parent, str) else None,
                "url": str(frame.get("url") or "")[:_URL_MAX_CHARS],
                "origin": str(frame.get("securityOrigin") or "")[:_URL_MAX_CHARS],
                "loader": str(frame.get("loaderId") or ""),
            }
        )
        pending.extend(reversed(children))
    return found


def _validated_click(button: Any, click_count: Any) -> None:
    if not isinstance(button, str) or button not in _MOUSE_BUTTONS:
        raise BrowserValidationError('A click uses the "left", "right" or "middle" button.')
    if type(click_count) is not int or not 1 <= click_count <= _MAX_CLICK_COUNT:
        raise BrowserValidationError(f"A click is 1 to {_MAX_CLICK_COUNT} clicks in a row.")


def _press_mouse(act: _Actions, x: float, y: float, button: str, click_count: int) -> None:
    """Press and release ``button`` at a viewport point, ``click_count`` times in a row (each
    press counts up, as a double or triple click does). A pressed button is always released,
    also on failure or cancellation."""
    held = {} if button == "left" else {"buttons": _MOUSE_BUTTONS[button]}
    for count in range(1, click_count + 1):
        params = {"x": x, "y": y, "button": button, "clickCount": count}
        try:
            act.call("Input.dispatchMouseEvent", {**params, **held, "type": "mousePressed"})
        finally:
            act.call("Input.dispatchMouseEvent", {**params, "type": "mouseReleased"}, cancel=None)


def _navigation_history(raw: Mapping[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    """A tab's history as Page.getNavigationHistory reports it: the current entry's index, and
    each entry's id, address and how it was reached (its transition type)."""
    index = raw.get("currentIndex") if isinstance(raw, Mapping) else None
    entries = raw.get("entries") if isinstance(raw, Mapping) else None
    if (
        type(index) is not int
        or not isinstance(entries, list)
        or len(entries) > 4 * _MAX_FRAMES
        or not 0 <= index < len(entries)
    ):
        raise BrowserSecurityError("Browser history response was invalid.")
    found = []
    for entry in entries:
        if (
            not isinstance(entry, Mapping)
            or type(entry.get("id")) is not int
            or not isinstance(entry.get("url"), str)
        ):
            raise BrowserSecurityError("Browser history response was invalid.")
        transition = entry.get("transitionType")
        found.append(
            {
                "id": entry["id"],
                # An address too long to check is never opened (the address policy refuses "").
                "url": entry["url"] if len(entry["url"]) <= _URL_MAX_CHARS else "",
                "transition": transition[:32] if isinstance(transition, str) else "",
            }
        )
    return index, found


def _not_ready(reason: str) -> BrowserTargetNotReadyError:
    if reason == "covered":
        return BrowserTargetObscuredError(
            "The observed control is covered or no longer receives clicks. No click was sent."
        )
    if reason == "moving":
        return BrowserTargetUnstableError(
            "The observed control kept moving (an animation or a page still settling). "
            "No input was sent."
        )
    return BrowserTargetHiddenError(
        "The observed control is not visible on the page (it has no size or is hidden). "
        "No input was sent."
    )


def _require_element(state: Any) -> None:
    if not isinstance(state, Mapping) or state.get("attached") is not True:
        raise BrowserNotFoundError(_CHANGED_ELEMENT)
    if state.get("element") is not True:
        raise BrowserValidationError(
            "The observed node is not an element; act on the element that contains it."
        )


def _validated_target(
    selector: str | None, identity: int | None, frame_id: str | None
) -> tuple[str | None, int | None, str | None]:
    """How an operation addresses its element: a top-document selector (optionally checked
    against the observed identity), or the observed identity in a frame (None: main frame)."""
    if identity is not None and (type(identity) is not int or identity <= 0):
        raise BrowserValidationError("Observed browser node identity is invalid.")
    if frame_id is not None and (
        not isinstance(frame_id, str) or not _FRAME_ID_RE.fullmatch(frame_id)
    ):
        raise BrowserValidationError("Observed browser frame is invalid.")
    if selector is None:
        if identity is None:
            raise BrowserValidationError(
                "Address an element by a selector or by its observed node identity."
            )
        return None, identity, frame_id
    if frame_id is not None:
        raise BrowserValidationError(
            "A selector addresses the page's top document; address an element in a frame by "
            "its observed node identity."
        )
    return _validated_selector(selector), identity, None


def _validated_labels(labels: Sequence[int], identity: int | None) -> tuple[int, ...]:
    if (
        not isinstance(labels, (list, tuple))
        or len(labels) > 512
        or any(type(item) is not int or item <= 0 for item in labels)
    ):
        raise BrowserValidationError("Browser label nodes are invalid.")
    if labels and identity is None:
        raise BrowserValidationError("Label nodes need the target's identity.")
    return tuple(labels)


def _dialog_answer(dialog: Any, text: Any) -> tuple[bool, str | None]:
    if not isinstance(dialog, str) or dialog not in ("dismiss", "accept"):
        raise BrowserValidationError('A dialog answer is "accept" or "dismiss".')
    if text is not None:
        if dialog != "accept":
            raise BrowserValidationError("Prompt text is only typed when a dialog is accepted.")
        if not isinstance(text, str) or len(text) > _TYPE_MAX_CHARS:
            raise BrowserValidationError("Prompt text is invalid or too long.")
    return dialog == "accept", text


def _typable(character: str) -> bool:
    """Whether a character can be typed as text: anything but control characters and lone
    surrogates, so joiners and marks of every script (Persian, Indic, emoji sequences) count."""
    return unicodedata.category(character) not in {"Cc", "Cs"}


def _character_code(character: str) -> tuple[str, int]:
    """The US-layout key code and virtual-key code of a character, where it has one; other
    characters (any script) are typed by their text alone."""
    if character == " ":
        return "Space", 32
    if character.isascii() and character.isalpha():
        return f"Key{character.upper()}", ord(character.upper())
    if character.isascii() and character.isdigit():
        return f"Digit{character}", ord(character)
    return "", 0


def _key_chord(key: Any, shift: Any, modifiers: Any) -> _KeyChord:
    if (
        type(shift) is not bool
        or not isinstance(modifiers, (list, tuple))
        or len(modifiers) > len(_MODIFIERS)
        or not isinstance(key, str)
    ):
        raise BrowserValidationError("Unsupported page key or modifier.")
    main = "Meta" if os.sys.platform == "darwin" else "Control"
    names: list[str] = []
    for name in (*modifiers, *(("Shift",) if shift else ())):
        resolved = main if name == "ControlOrMeta" else name
        if not isinstance(resolved, str) or resolved not in _MODIFIERS:
            raise BrowserValidationError("Unsupported page key or modifier.")
        if resolved not in names:
            names.append(resolved)
    if key in _PAGE_KEYS:
        key_value, key_code = _PAGE_KEYS[key]
        code = key
        text = "\r" if key == "Enter" else " " if key == "Space" else None
    elif len(key) == 1 and _typable(key) and not key.isspace():
        key_value, text = key, key
        code, key_code = _character_code(key)
    else:
        raise BrowserValidationError("Unsupported page key or modifier.")
    params = {"key": key_value, "code": code, "windowsVirtualKeyCode": key_code}
    down: dict[str, Any] = {**params, "type": "keyDown"}
    if text is not None and not {"Alt", "Control", "Meta"} & set(names):
        down["text"] = text
    command = _EDITING_COMMANDS.get(key.casefold()) if len(key) == 1 else None
    if os.sys.platform == "darwin" and command is not None:
        if names == [main]:
            down["commands"] = [command]
        elif command == "undo" and set(names) == {main, "Shift"}:
            down["commands"] = ["redo"]
    return _KeyChord(tuple(names), down, {**params, "type": "keyUp"})


def _typing_events(character: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Key down and up for one typed character: a line break is Enter, a tab is Tab."""
    if character == "\n":
        params: dict[str, Any] = {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13}
        text: str | None = "\r"
    elif character == "\t":
        params, text = {"key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9}, None
    else:
        code, key_code = _character_code(character)
        params = {"key": character, "code": code, "windowsVirtualKeyCode": key_code}
        text = character
    down = {**params, "type": "keyDown"}
    if text is not None:
        down.update(text=text, unmodifiedText=text)
    return down, {**params, "type": "keyUp"}


def _wait_condition(
    condition: Any,
    value: Any,
    selector: str | None,
    identity: int | None,
    frame_id: str | None,
) -> tuple[Any, ...]:
    element = selector is not None or identity is not None or frame_id is not None
    if condition in ("element_present", "element_gone"):
        if value is not None:
            raise BrowserValidationError("Waiting for an element takes no value.")
        return (condition, None, *_validated_target(selector, identity, frame_id))
    if element:
        raise BrowserValidationError("Only element conditions take an element.")
    if condition == "text":
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= _WAIT_VALUE_MAX_CHARS
            or not _fold(value).strip()
        ):
            raise BrowserValidationError(
                f"Waiting for text needs the text, up to {_WAIT_VALUE_MAX_CHARS} characters."
            )
        return ("text", value)
    if condition == "address":
        if not isinstance(value, str) or not 1 <= len(value) <= _URL_MAX_CHARS or not value.strip():
            raise BrowserValidationError("Waiting for an address needs part of the address.")
        return ("address", value)
    if condition == "load":
        if value not in (None, "load", "domcontentloaded"):
            raise BrowserValidationError('A load state is "load" or "domcontentloaded".')
        return ("load", value or "load")
    raise BrowserValidationError("Wait for text, element_present, element_gone, address or load.")


def _text_contains(text: str, phrase: str) -> bool:
    """Whether ``text`` contains ``phrase`` Unicode-normalised and case-folded the same way
    in every script, or failing that also without diacritics (capitals often drop them)."""
    return any(fold(phrase).strip() in fold(text) for fold in (_fold, _fold_marks))


_IDNA_LABEL = re.compile(r"xn--[a-z0-9-]{1,59}", re.IGNORECASE)


def _readable_address(value: str) -> str:
    """An address as a person reads it: percent-escapes and international domain names decoded."""

    def decode(match: re.Match[str]) -> str:
        try:
            return match.group(0).encode("ascii").decode("idna")
        except UnicodeError:
            return match.group(0)  # not a valid international label: compared as written

    return _IDNA_LABEL.sub(decode, unquote(value))


def _address_contains(url: str, phrase: str) -> bool:
    return _text_contains(_readable_address(url), _readable_address(phrase))


def _content_quad(payload: Mapping[str, Any]) -> tuple[float, ...]:
    model = payload.get("model")
    if not isinstance(model, Mapping):
        raise BrowserSecurityError("Browser element box response was invalid.")
    quad = model.get("content") or model.get("border")
    if not isinstance(quad, Sequence) or isinstance(quad, str | bytes) or len(quad) != 8:
        raise BrowserSecurityError("Browser element box response was invalid.")
    try:
        numbers = tuple(float(value) for value in quad)
    except (TypeError, ValueError) as exc:
        raise BrowserSecurityError("Browser element box response was invalid.") from exc
    if not all(math.isfinite(value) for value in numbers):
        raise BrowserSecurityError("Browser element box response was invalid.")
    return numbers


def _placement_center(
    placement: tuple[tuple[float, ...], tuple[tuple[float, float], ...]],
) -> tuple[float, float]:
    """The element's centre in the page's viewport: its box centre in its own frame plus where
    each out-of-process frame around it starts."""
    quad, offsets = placement
    return (
        sum(quad[0::2]) / 4.0 + sum(offset[0] for offset in offsets),
        sum(quad[1::2]) / 4.0 + sum(offset[1] for offset in offsets),
    )


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
    profile_dir: Path,
    proxy_host: str,
    proxy_port: int,
    *,
    visible: bool = False,
    cache_dir: Path | None = None,
) -> tuple[str, ...]:
    """Chromium's command line. ``cache_dir``: where its disk caches (HTTP and code cache) go
    instead of the profile, for a profile that outlives the session: Chromium otherwise keeps
    them inside a ``--user-data-dir`` outside the usual config folder, where they would grow the
    saved storage with every site visited."""
    if proxy_host != "127.0.0.1" or not 1 <= proxy_port <= 65_535:
        raise BrowserSecurityError("Managed browser egress proxy endpoint is not trusted.")
    return (
        *(("--ozone-platform=x11", "--start-maximized") if visible else ("--headless=new",)),
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=0",
        f"--user-data-dir={profile_dir}",
        *((f"--disk-cache-dir={cache_dir}",) if cache_dir is not None else ()),
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
        # The owned target is created over CDP. Do not leave an extra visible
        # startup tab outside the managed page inventory.
        "--no-startup-window" if visible else "about:blank",
    )


def _sanitized_browser_environment(
    source: Mapping[str, str] | None = None, *, platform: str | None = None
) -> dict[str, str]:
    env = source or os.environ
    allowed_names = {"LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "WINDIR"}
    output: dict[str, str] = {}
    for key, value in env.items():
        if str(key).upper() in allowed_names and "\x00" not in str(value):
            output[str(key)] = str(value)
    # On Linux, Chromium converts file names through the character locale. Without a UTF-8
    # one (a bare service environment), it silently refuses files whose names are not plain
    # ASCII, such as uploads named in Greek or Chinese. Give it UTF-8 characters only; the
    # language settings (LANG, LC_ALL) are left as they are.
    effective = output.get("LC_ALL") or output.get("LC_CTYPE") or output.get("LANG") or ""
    if (platform or os.sys.platform).startswith("linux") and not re.search(
        r"utf-?8", effective, re.IGNORECASE
    ):
        if not output.get("LC_ALL"):
            output["LC_CTYPE"] = "C.UTF-8"
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
    resolve_hostnames: bool,
) -> tuple[str, ...] | None:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not resolve_hostnames:
            return None
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


def _layout_number(metrics: Any, key: str) -> float:
    """A size from Page.getLayoutMetrics, in CSS pixels; a missing or invalid one is refused."""
    value = metrics.get(key) if isinstance(metrics, Mapping) else None
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0 <= value <= 10_000_000
    ):
        raise BrowserSecurityError("Browser page size response was invalid.")
    return float(value)


def _trimmed_accessibility(nodes: Any) -> list[dict[str, Any]]:
    """Accessibility nodes without what a page view never reads: each name's ``sources``,
    ``ignoredReasons``, ``chromeRole``, and the ``InlineTextBox`` nodes (text layout pieces whose
    text their parent already names). Their children are linked to their nearest kept ancestor,
    so no ``childIds`` or ``parentId`` names a removed node. Everything else is kept as is."""
    if not isinstance(nodes, list) or any(not isinstance(node, Mapping) for node in nodes):
        raise BrowserSecurityError("Browser accessibility tree was invalid.")
    by_id = {str(node.get("nodeId")): node for node in nodes}

    def removed(node_id: Any) -> bool:
        node = by_id.get(str(node_id))
        role = node.get("role") if node is not None else None
        return isinstance(role, Mapping) and role.get("value") == "InlineTextBox"

    def kept_children(child_ids: Any) -> list[Any]:
        kept: list[Any] = []
        pending = list(reversed(child_ids)) if isinstance(child_ids, list) else []
        seen: set[str] = set()
        while pending:
            child = pending.pop()
            if not removed(child):
                kept.append(child)
            elif str(child) not in seen:
                seen.add(str(child))
                grandchildren = by_id[str(child)].get("childIds")
                if isinstance(grandchildren, list):
                    pending.extend(reversed(grandchildren))
        return kept

    trimmed: list[dict[str, Any]] = []
    for node in nodes:
        if removed(node.get("nodeId")):
            continue
        copy = {
            key: value for key, value in node.items() if key not in {"ignoredReasons", "chromeRole"}
        }
        name = copy.get("name")
        if isinstance(name, Mapping):
            copy["name"] = {key: value for key, value in name.items() if key != "sources"}
        if "childIds" in copy:
            copy["childIds"] = kept_children(copy["childIds"])
        if "parentId" in copy:
            parent, seen = copy["parentId"], set()
            while removed(parent) and str(parent) not in seen:
                seen.add(str(parent))
                parent = by_id[str(parent)].get("parentId")
            if parent is None or removed(parent):
                del copy["parentId"]
            else:
                copy["parentId"] = parent
        trimmed.append(copy)
    return trimmed


def _frame_gone(exc: BrowserError) -> bool:
    """Whether a command about one frame failed because that frame is no longer there to read:
    its target detached, or the browser refused the command with a numbered protocol error
    (Chromium answers "invalid params" for a frame id that left the process it was listed in,
    removed or moved to another site's process since the page's frames were listed). Anything
    else (a timeout, a broken connection, a dialog) is not about that one frame."""
    if isinstance(exc, BrowserPageUnavailableError):
        return True
    return not isinstance(exc, BrowserDialogOpenError) and type(getattr(exc, "code", None)) is int


def _framed_accessibility(nodes: Any, frame_id: str, prefix: str | None) -> list[dict[str, Any]]:
    """One frame's trimmed accessibility nodes: its root nodes carry ``frameId``, and with
    ``prefix`` every node id (and the ids nodes name) is ``prefix/<id>``."""

    def named(node_id: Any) -> Any:
        return node_id if prefix is None else f"{prefix}/{node_id}"

    framed = _trimmed_accessibility(nodes)
    for node in framed:
        node["nodeId"] = named(node.get("nodeId"))
        if "parentId" in node:
            node["parentId"] = named(node["parentId"])
        else:
            node["frameId"] = frame_id
        if isinstance(node.get("childIds"), list):
            node["childIds"] = [named(child) for child in node["childIds"]]
    return framed


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise BrowserSecurityError("Browser CDP response was not JSON-compatible.") from exc


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


def _cookie_identity(cookie: Mapping[str, Any]) -> dict[str, Any] | None:
    """A cookie's identity as the browser reports it, bounded; never its value."""
    name, domain, path = cookie.get("name"), cookie.get("domain"), cookie.get("path")
    if not all(isinstance(item, str) for item in (name, domain, path)):
        return None
    if not domain or len(domain) > 256 or len(name) > 4_096 or len(path) > 4_096:
        return None
    partition = cookie.get("partition", cookie.get("partitionKey"))
    if isinstance(partition, Mapping):
        site = partition.get("topLevelSite")
        ancestor = partition.get("hasCrossSiteAncestor", False)
        if not isinstance(site, str) or len(site) > 2_048 or not isinstance(ancestor, bool):
            return None
        partition = {"topLevelSite": site, "hasCrossSiteAncestor": ancestor}
    elif isinstance(partition, str):
        # Earlier browser versions name the partition by its top-level site alone.
        if len(partition) > 2_048:
            return None
    elif partition is not None:
        return None
    return {"name": name, "domain": domain, "path": path, "partition": partition or None}


def _web_origin(value: Any) -> str:
    """A plain http(s) origin: scheme and host, no path, credentials or port."""
    if not isinstance(value, str) or len(value) > 300:
        raise BrowserValidationError("A site origin was invalid.")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or f"{parsed.scheme}://{parsed.hostname}" != value
    ):
        raise BrowserValidationError("A site origin was invalid.")
    return value
