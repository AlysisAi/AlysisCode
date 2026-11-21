"""Read the focused window's elements on an owl computer's desktop, and act on one of them.

Standalone guest code: desktop_linux.py mounts this file alone at /opt in the computer's jail and
desktop_worker.py runs it as a short child, one request per run (the operation as its argument,
JSON on stdin, one JSON reply on stdout), in an application namespace like any task program.
It must not import the Owls service.

It speaks the AT-SPI D-Bus protocol through GLib's Gio, so every toolkit that publishes its
windows there (GTK, Qt, LibreOffice and others) is read the same way, in every language: roles,
states and interfaces are the protocol's own vocabulary; names, text and values are the
application's, untrusted, and only clipped. A password field's text is never read. An element is
named by its application's bus name and object path, which stay the same while it lives.

Every reply is a JSON object. ``unavailable`` (a fixed word) means the protocol could not be
used at all; ``refused`` (a fixed word) means nothing was sent to the application.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time

# AT-SPI names (protocol vocabulary, the same for every toolkit and language).
REGISTRY = "org.a11y.atspi.Registry"
ROOT_PATH = "/org/a11y/atspi/accessible/root"
PREFIX = "org.a11y.atspi."
ACCESSIBLE = PREFIX + "Accessible"
ACTION = PREFIX + "Action"
COMPONENT = PREFIX + "Component"
EDITABLE_TEXT = PREFIX + "EditableText"
SELECTION = PREFIX + "Selection"
TEXT = PREFIX + "Text"
VALUE = PREFIX + "Value"
PROPERTIES = "org.freedesktop.DBus.Properties"
SCREEN_COORDINATES = 0  # ATSPI_COORD_TYPE_SCREEN

# AtspiStateType in protocol order: GetState returns two 32-bit words, state n at bit n.
STATES = (
    "invalid",
    "active",
    "armed",
    "busy",
    "checked",
    "collapsed",
    "defunct",
    "editable",
    "enabled",
    "expandable",
    "expanded",
    "focusable",
    "focused",
    "has tooltip",
    "horizontal",
    "iconified",
    "modal",
    "multi line",
    "multiselectable",
    "opaque",
    "pressed",
    "resizable",
    "selectable",
    "selected",
    "sensitive",
    "showing",
    "single line",
    "stale",
    "transient",
    "vertical",
    "visible",
    "manages descendants",
    "indeterminate",
    "required",
    "truncated",
    "animated",
    "invalid entry",
    "supports autocompletion",
    "selectable text",
    "is default",
    "visited",
    "checkable",
    "has popup",
    "read only",
)
# The states an element view uses; the others only cost bytes.
REPORTED_STATES = frozenset(
    {
        "active",
        "checkable",
        "checked",
        "collapsed",
        "editable",
        "enabled",
        "expandable",
        "expanded",
        "focusable",
        "focused",
        "has popup",
        "indeterminate",
        "invalid entry",
        "is default",
        "manages descendants",
        "modal",
        "multi line",
        "pressed",
        "read only",
        "required",
        "selectable",
        "selected",
        "sensitive",
    }
)
INTERFACES = frozenset(
    {"Action", "Component", "EditableText", "Selection", "Text", "Value", "Table", "Document"}
)
# Windows that open over another one of the same application (menus, drop-down lists,
# tooltips): shown with the focused window, since they take no focus of their own.
POPUP_ROLES = frozenset({"window", "menu", "popup menu", "tool tip"})
PRIVATE_ROLES = frozenset({"password text"})
TOGGLE_ROLES = frozenset(
    {
        "check box",
        "check menu item",
        "radio button",
        "radio menu item",
        "switch",
        "toggle button",
    }
)
RADIO_ROLES = frozenset({"radio button", "radio menu item"})

# Bounds of one reading: elements, protocol calls and time, nesting, the children one element
# may list (a long list or a spreadsheet manages its own), applications and their windows.
MAX_ELEMENTS = 800
MAX_CALLS = 8000
READ_SECONDS = 4.0
ACT_SECONDS = 4.0
ACT_CALLS = 200
MAX_DEPTH = 40
MAX_CHILDREN = 400
MAX_APPLICATIONS = 40
MAX_WINDOWS = 30
NAME_CHARS = 300
TEXT_CHARS = 400
MAX_REPLY_BYTES = 300_000
MAX_REQUEST_BYTES = 64_000
MAX_SET_TEXT = 8000
# Per call: an application that does not answer within this is left out of the reading. An
# action may wait for the dialog it opens; its result then says it was not confirmed.
CALL_MS = 1000
ACTION_MS = 2000
START_MS = 2500

BUS_NAME = re.compile(
    r"(?::[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+|[A-Za-z_-][A-Za-z0-9_-]*(?:\.[A-Za-z_-][A-Za-z0-9_-]*)+)"
)
OBJECT_PATH = re.compile(r"/|(?:/[A-Za-z0-9_]+)+")
ACTIONS = frozenset({"activate", "set_text", "select", "toggle"})


class Gone(Exception):
    """The element or its application no longer answers to that name (closed or replaced)."""


class Unresponsive(Exception):
    """The application did not answer in time."""


class Exhausted(Exception):
    """A bound of this reading was reached; ``reason`` is a fixed word."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Budget:
    def __init__(self, seconds: float, calls: int):
        self.deadline = time.monotonic() + seconds
        self.calls = calls

    def spend(self) -> int:
        """Admit one protocol call; return its timeout in milliseconds."""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0.05:
            raise Exhausted("time")
        if self.calls <= 0:
            raise Exhausted("calls")
        self.calls -= 1
        return max(50, int(remaining * 1000))


def element_identity(value) -> tuple[str, str]:
    """An element's (bus name, object path), validated as D-Bus names before any call."""
    if not isinstance(value, dict):
        raise ValueError("Invalid element.")
    bus, path = value.get("bus"), value.get("path")
    if (
        not isinstance(bus, str)
        or not isinstance(path, str)
        or len(bus) > 255
        or len(path) > 1024
        or not BUS_NAME.fullmatch(bus)
        or not OBJECT_PATH.fullmatch(path)
    ):
        raise ValueError("Invalid element.")
    return bus, path


def _clip(value, limit: int) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _states(words) -> set[str]:
    names = set()
    for index, word in enumerate(list(words or [])[:2]):
        if type(word) is not int:
            continue
        for bit in range(32):
            if word >> bit & 1 and index * 32 + bit < len(STATES):
                names.add(STATES[index * 32 + bit])
    return names


class Reader:
    """AT-SPI requests over one accessibility bus connection (``bus``), within a budget."""

    def __init__(self, bus, budget: Budget):
        self.bus, self.budget = bus, budget
        self.role_names: dict[tuple[str, int], str] = {}
        self.unresponsive: set[str] = set()

    def call(self, ref, interface, method, signature=None, args=(), *, cap=CALL_MS):
        destination, path = ref
        if destination in self.unresponsive:
            raise Unresponsive()
        timeout = min(cap, self.budget.spend())
        try:
            return self.bus.call(destination, path, interface, method, signature, args, timeout)
        except Unresponsive:
            self.unresponsive.add(destination)
            raise

    def get(self, ref, interface, name):
        return self.call(ref, PROPERTIES, "Get", "(ss)", (interface, name))[0]

    def states(self, ref) -> set[str]:
        return _states(self.call(ref, ACCESSIBLE, "GetState")[0])

    def role(self, ref) -> str:
        number = self.call(ref, ACCESSIBLE, "GetRole")[0]
        key = (ref[0], number)
        if key not in self.role_names:
            self.role_names[key] = _clip(self.call(ref, ACCESSIBLE, "GetRoleName")[0], 60)
        return self.role_names[key]

    def interfaces(self, ref) -> set[str]:
        names = self.call(ref, ACCESSIBLE, "GetInterfaces")[0]
        return {
            name[len(PREFIX) :]
            for name in names or ()
            if isinstance(name, str) and name.startswith(PREFIX)
        } & INTERFACES

    def children(self, ref) -> list[tuple[str, str]]:
        found = []
        for item in self.call(ref, ACCESSIBLE, "GetChildren")[0] or ():
            try:
                found.append(element_identity({"bus": item[0], "path": item[1]}))
            except (ValueError, TypeError, IndexError):
                continue  # not an addressable element (an empty reference)
        return found

    def details(self, ref) -> dict:
        """Name, description and child count; one call when the application answers GetAll."""
        try:
            values = self.call(ref, PROPERTIES, "GetAll", "(s)", (ACCESSIBLE,))[0]
            if isinstance(values, dict) and "Name" in values:
                return values
        except Gone:
            pass
        return {
            "Name": self.get(ref, ACCESSIBLE, "Name"),
            "ChildCount": self.get(ref, ACCESSIBLE, "ChildCount"),
        }


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value if math.isfinite(value) else None


def describe(reader: Reader, ref, depth: int, *, root: bool = False):
    """One element's record and its child count; (None, 0) for one not on screen."""
    states = reader.states(ref)
    if "defunct" in states or (not root and "showing" not in states):
        return None, 0
    role = reader.role(ref)
    info = reader.details(ref)
    interfaces = reader.interfaces(ref)
    name = _clip(info.get("Name"), NAME_CHARS)
    node = {
        "id": list(ref),
        "depth": depth,
        "role": role,
        "name": name,
        "states": sorted(states & REPORTED_STATES),
        "interfaces": sorted(interfaces),
    }
    description = _clip(info.get("Description"), NAME_CHARS)
    if description and description != name:
        node["description"] = description
    private = role in PRIVATE_ROLES
    if private:
        node["private"] = True
    if "Text" in interfaces and (not name or "editable" in states or "EditableText" in interfaces):
        try:
            count = reader.get(ref, TEXT, "CharacterCount")
            count = count if type(count) is int and count > 0 else 0
            if private:
                node["has_text"] = count > 0  # whether it is filled, never what it holds
            elif count:
                text = reader.call(ref, TEXT, "GetText", "(ii)", (0, min(count, TEXT_CHARS)))[0]
                text = _clip(text, TEXT_CHARS)
                node["text"] = text + ("…" if count > TEXT_CHARS and text else "")
        except Gone:
            pass
    if "Value" in interfaces and not private:
        try:
            value = _number(reader.get(ref, VALUE, "CurrentValue"))
            if value is not None:
                node["value"] = value
        except Gone:
            pass
    count = info.get("ChildCount")
    return node, count if type(count) is int and count > 0 else 0


def _walk(reader: Reader, ref, depth: int, nodes: list, *, root: bool = False) -> None:
    if len(nodes) >= MAX_ELEMENTS:
        raise Exhausted("elements")
    try:
        node, count = describe(reader, ref, depth, root=root)
    except (Gone, Unresponsive):
        return
    if node is None:
        return
    nodes.append(node)
    if not count:
        return
    if depth >= MAX_DEPTH or "manages descendants" in node["states"] or count > MAX_CHILDREN:
        # A spreadsheet or a very long list manages its own children: listing them would read
        # the whole document, not the screen.
        node["children_not_listed"] = count
        return
    try:
        children = reader.children(ref)
    except (Gone, Unresponsive):
        return
    for child in children[:MAX_CHILDREN]:
        _walk(reader, child, depth + 1, nodes)


def _windows(reader: Reader) -> list[dict]:
    """Every application's windows on screen: its name, the window's role, title and states."""
    windows = []
    for application in reader.children((REGISTRY, ROOT_PATH))[:MAX_APPLICATIONS]:
        try:
            label = _clip(reader.get(application, ACCESSIBLE, "Name"), NAME_CHARS)
            tops = reader.children(application)
        except (Gone, Unresponsive):
            continue
        for top in tops[:MAX_WINDOWS]:
            try:
                states = reader.states(top)
                if "showing" not in states or "defunct" in states:
                    continue
                windows.append(
                    {
                        "ref": top,
                        "application": application[0],
                        "app": label,
                        "role": reader.role(top),
                        "title": _clip(reader.get(top, ACCESSIBLE, "Name"), NAME_CHARS),
                        "active": "active" in states,
                        "modal": "modal" in states,
                    }
                )
            except (Gone, Unresponsive):
                continue
    return windows


def read(reader: Reader) -> dict:
    """The focused window's elements on screen, in order, with the windows of its application
    that open over it; and the other windows on screen by title."""
    stopped = None
    nodes: list = []
    focused = None
    windows: list = []
    try:
        windows = _windows(reader)
        active = [window for window in windows if window["active"]]
        # A modal dialog is where the person's input goes, even when its owner reports active.
        focused = next((w for w in reversed(active) if w["modal"]), None) or (
            active[-1] if active else None
        )
        if focused is not None:
            popups = [
                window
                for window in windows
                if window is not focused
                and window["application"] == focused["application"]
                and window["role"] in POPUP_ROLES
            ]
            for window in [focused, *popups]:
                _walk(reader, window["ref"], 0, nodes, root=True)
    except Exhausted as exc:
        stopped = exc.reason
    shown = {id(focused)} if focused else set()
    reply = {
        "focused": (
            {"app": focused["app"], "role": focused["role"], "title": focused["title"]}
            if focused
            else None
        ),
        "windows": [
            {"app": w["app"], "role": w["role"], "title": w["title"]}
            for w in windows
            if id(w) not in shown and w["role"] not in POPUP_ROLES
        ][:MAX_WINDOWS],
        "nodes": nodes,
        "stopped": stopped,
        "unresponsive": bool(reader.unresponsive),
    }
    # Bounded reply: drop elements from the end and say the reading stopped there.
    while len(json.dumps(reply, ensure_ascii=False).encode()) > MAX_REPLY_BYTES and nodes:
        del nodes[max(1, len(nodes) * 3 // 4) :]
        reply["stopped"] = "size"
    return reply


def _resolve(reader: Reader, request: dict, *, acting: bool):
    """The element of ``request`` as it is now, or why it can't be addressed (a refusal)."""
    ref = element_identity(request.get("element"))
    expected = request["element"].get("role")
    try:
        states = reader.states(ref)
        role = reader.role(ref)
    except Gone:
        return ref, None, "gone"
    except Unresponsive:
        return ref, None, "unresponsive"
    if "defunct" in states:
        return ref, None, "gone"
    if role != expected:
        return ref, None, "changed"
    if acting and "showing" not in states:
        return ref, None, "hidden"
    if acting and not states & {"enabled", "sensitive"}:
        return ref, None, "disabled"
    try:
        interfaces = reader.interfaces(ref)
    except Gone:
        return ref, None, "gone"
    except Unresponsive:
        return ref, None, "unresponsive"
    return ref, {"states": states, "role": role, "interfaces": interfaces}, None


def _pointer(reader: Reader, ref, element: dict) -> dict:
    """Where a person would click: the element's centre on the screen."""
    if "Component" not in element["interfaces"]:
        return {"refused": "no_position"}
    extents = reader.call(ref, COMPONENT, "GetExtents", "(u)", (SCREEN_COORDINATES,))[0]
    if not isinstance(extents, list | tuple) or len(extents) != 4:
        return {"refused": "no_position"}
    x, y, width, height = extents
    if not all(type(v) is int for v in extents) or width <= 0 or height <= 0:
        return {"refused": "no_position"}
    return {"pointer": {"x": x + width // 2, "y": y + height // 2}}


def _activate(reader: Reader, ref, element: dict) -> dict:
    """The element's default action (the first one its toolkit lists), else a click."""
    if "Action" in element["interfaces"]:
        count = reader.get(ref, ACTION, "NActions")
        if type(count) is int and count > 0:
            try:
                done = reader.call(ref, ACTION, "DoAction", "(i)", (0,), cap=ACTION_MS)[0]
            except Unresponsive:
                # Sent, but the application did not answer (it may be showing a dialog).
                return {"sent": "accessibility", "confirmed": False}
            if done is True:
                return {"sent": "accessibility", "confirmed": True}
    return _pointer(reader, ref, element)


def _text_of(reader: Reader, ref) -> str | None:
    count = reader.get(ref, TEXT, "CharacterCount")
    if type(count) is not int or count < 0:
        return None
    return reader.call(ref, TEXT, "GetText", "(ii)", (0, count))[0] if count else ""


def _set_text(reader: Reader, ref, element: dict, text: str) -> dict:
    states, interfaces = element["states"], element["interfaces"]
    if "read only" in states:
        return {"refused": "read_only"}
    if "EditableText" in interfaces:
        done = reader.call(ref, EDITABLE_TEXT, "SetTextContents", "(s)", (text,))[0]
        if done is not True and "Text" in interfaces:
            current = reader.get(ref, TEXT, "CharacterCount")
            if type(current) is int and current > 0:
                reader.call(ref, EDITABLE_TEXT, "DeleteText", "(ii)", (0, current))
            done = (
                reader.call(
                    ref, EDITABLE_TEXT, "InsertText", "(isi)", (0, text, len(text.encode()))
                )[0]
                if text
                else True
            )
        if done is True:
            reply = {"sent": "accessibility", "confirmed": True}
            if element["role"] not in PRIVATE_ROLES and "Text" in interfaces:
                # Read back: a field may reformat what it was given (a date, a number).
                reply["value_matches"] = _text_of(reader, ref) == text
            return reply
    # No text it can set: focus it, and the worker types at its cursor.
    if "Component" in interfaces:
        try:
            if reader.call(ref, COMPONENT, "GrabFocus")[0] is True:
                return {"focused": True, "then": "paste"}
        except Gone:
            pass
    reply = _pointer(reader, ref, element)
    if "pointer" in reply:
        reply["then"] = "paste"
    return reply


def _checked(states: set[str]) -> bool:
    return bool(states & {"checked", "pressed"})


def act(reader: Reader, request: dict) -> dict:
    action = request.get("action")
    if action not in ACTIONS:
        raise ValueError("Invalid action.")
    ref, element, refusal = _resolve(reader, request, acting=True)
    if refusal:
        return {"refused": refusal}
    states, role = element["states"], element["role"]
    if action == "activate":
        return _activate(reader, ref, element)
    if action == "set_text":
        text = request.get("text")
        if not isinstance(text, str) or len(text) > MAX_SET_TEXT or "\0" in text:
            raise ValueError("Invalid text.")
        return _set_text(reader, ref, element, text)
    if action == "toggle":
        wanted = request.get("checked")
        if type(wanted) is not bool:
            raise ValueError("Invalid state.")
        if role not in TOGGLE_ROLES and not states & {"checkable", "checked", "pressed"}:
            return {"refused": "not_toggle"}
        if _checked(states) == wanted:
            return {"unchanged": True, "checked": wanted}
        if role in RADIO_ROLES and not wanted:
            return {"refused": "radio"}
        reply = _activate(reader, ref, element)
        if "sent" in reply:
            reply["checked"] = _checked(reader.states(ref))
        elif "pointer" in reply:
            reply["then"] = "verify"
        return reply
    # select: through the container that holds it, else as a person picks it.
    if "selected" in states:
        return {"unchanged": True, "selected": True}
    parent = reader.get(ref, ACCESSIBLE, "Parent")
    try:
        container = element_identity({"bus": parent[0], "path": parent[1]})
    except (ValueError, TypeError, IndexError):
        container = None
    if container is not None and "Selection" in reader.interfaces(container):
        index = reader.call(ref, ACCESSIBLE, "GetIndexInParent")[0]
        if type(index) is int and index >= 0:
            if reader.call(container, SELECTION, "SelectChild", "(i)", (index,))[0] is True:
                return {
                    "sent": "accessibility",
                    "confirmed": True,
                    "selected": "selected" in reader.states(ref),
                }
    reply = _activate(reader, ref, element)
    if "sent" in reply:
        reply["selected"] = "selected" in reader.states(ref)
    elif "pointer" in reply:
        reply["then"] = "verify"
    return reply


def state(reader: Reader, request: dict) -> dict:
    """The element's checked and selected states now (after a click the worker sent)."""
    ref, element, refusal = _resolve(reader, request, acting=False)
    if refusal:
        return {"refused": refusal}
    return {
        "checked": _checked(element["states"]),
        "selected": "selected" in element["states"],
    }


def locate(reader: Reader, request: dict) -> dict:
    """Where the element is on the screen now (its centre), for a pointer action there."""
    ref, element, refusal = _resolve(reader, request, acting=False)
    if refusal:
        return {"refused": refusal}
    if "showing" not in element["states"]:
        return {"refused": "hidden"}
    return _pointer(reader, ref, element)


def probe(reader: Reader) -> dict:
    """Whether the accessibility bus and its registry answer."""
    reader.children((REGISTRY, ROOT_PATH))
    return {"ready": True}


class GioBus:
    """The accessibility bus through GLib's Gio: the bus that the AT-SPI launcher announces
    on the session bus (or AT_SPI_BUS_ADDRESS, the standard override)."""

    def __init__(self):
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        self.Gio, self.GLib = Gio, GLib
        address = os.environ.get("AT_SPI_BUS_ADDRESS")
        if not address:
            session = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            reply = session.call_sync(
                "org.a11y.Bus",
                "/org/a11y/bus",
                "org.a11y.Bus",
                "GetAddress",
                None,
                GLib.VariantType.new("(s)"),
                Gio.DBusCallFlags.NONE,
                START_MS,
                None,
            )
            address = reply.unpack()[0]
        self.connection = Gio.DBusConnection.new_for_address_sync(
            address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )

    def call(self, destination, path, interface, method, signature, args, timeout_ms):
        Gio, GLib = self.Gio, self.GLib
        try:
            reply = self.connection.call_sync(
                destination,
                path,
                interface,
                method,
                GLib.Variant(signature, tuple(args)) if signature else None,
                None,
                Gio.DBusCallFlags.NONE,
                timeout_ms,
                None,
            )
        except GLib.Error as error:
            if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.TIMED_OUT) or (
                error.matches(Gio.dbus_error_quark(), Gio.DBusError.NO_REPLY)
                or error.matches(Gio.dbus_error_quark(), Gio.DBusError.TIMEOUT)
                or error.matches(Gio.dbus_error_quark(), Gio.DBusError.TIMED_OUT)
            ):
                raise Unresponsive() from None
            if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CLOSED):
                raise Exhausted("bus") from None
            # Any other error reply: that name or path does not answer as an element now.
            raise Gone() from None
        return reply.unpack() if reply is not None else ()


OPERATIONS = {
    "probe": (lambda reader, request: probe(reader), READ_SECONDS, 50),
    "read": (lambda reader, request: read(reader), READ_SECONDS, MAX_CALLS),
    "act": (act, ACT_SECONDS, ACT_CALLS),
    "state": (state, ACT_SECONDS, ACT_CALLS),
    "locate": (locate, ACT_SECONDS, ACT_CALLS),
}


def run(operation: str, request: dict, bus) -> dict:
    handler, seconds, calls = OPERATIONS[operation]
    reader = Reader(bus, Budget(seconds, calls))
    try:
        return handler(reader, request)
    except Exhausted as exc:
        return {"unavailable": exc.reason}
    except Unresponsive:
        return {"refused": "unresponsive"}
    except Gone:
        return {"refused": "gone"}


def main(argv=None) -> None:
    argv = sys.argv if argv is None else argv
    operation = argv[1] if len(argv) == 2 else ""
    sys.stdout.write(json.dumps(_reply(operation), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _reply(operation: str) -> dict:
    # No exception text in a reply: it can hold application data or bus addresses.
    if operation not in OPERATIONS:
        return {"unavailable": "operation"}
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        request = json.loads(raw or b"{}") if len(raw) <= MAX_REQUEST_BYTES else None
    except ValueError:
        request = None
    if not isinstance(request, dict):
        return {"unavailable": "request"}
    try:
        bus = GioBus()
    except Exception:
        return {"unavailable": "bus"}
    try:
        return run(operation, request, bus)
    except ValueError:
        return {"unavailable": "request"}
    except Exception:
        return {"unavailable": "failed"}


if __name__ == "__main__":
    main()
