"""Window elements on an owl computer's desktop: the reading inside the computer (over a scripted
AT-SPI bus), the element view and its refs, element actions and their fallbacks, and the
capability handshake that keeps older computer images on today's tools."""

from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from alysis_code.bots import desktop_accessibility as atspi
from alysis_code.bots import desktop_elements
from alysis_code.bots.browser import BrowserImageResult
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.desktop_accessibility import Gone, Unresponsive
from alysis_code.bots.desktop_linux import DesktopError
from alysis_code.cancellation import EventCancellationToken

ON = ("showing", "visible", "enabled", "sensitive")
STATE_BITS = {name: index for index, name in enumerate(atspi.STATES)}


def el(role, name="", *children, states=(), off=False, interfaces=(), **extra):
    """One element of a scripted application: its role, name, states, interfaces and children."""
    return {
        "role": role,
        "name": name,
        "children": list(children),
        "states": set(() if off else ON) | set(states),
        "interfaces": set(interfaces),
        **extra,
    }


class FakeBus:
    """AT-SPI as an accessibility bus serves it: the registry's applications, and each element's
    methods and properties with the protocol's reply shapes (the same tuples Gio unpacks)."""

    def __init__(self, applications):
        self.objects, self.apps, self.calls = {}, [], []
        self.slow: set[str] = set()
        self.roles: list[str] = []
        self.counter = 0
        for bus, (label, windows) in applications.items():
            self._add(bus, atspi.ROOT_PATH, el("application", label, *windows), None)
            self.apps.append(bus)

    def _add(self, bus, path, record, parent):
        record.update(bus=bus, path=path, parent=parent)
        self.objects[(bus, path)] = record
        record["refs"] = []
        for child in record["children"]:
            self.counter += 1
            child_path = f"/org/a11y/atspi/accessible/{self.counter}"
            self._add(bus, child_path, child, (bus, path))
            record["refs"].append((bus, child_path))

    def find(self, name, role=None):
        # The last match: a dialog's control rather than its namesake behind it.
        return [
            r for r in self.objects.values() if r["name"] == name and role in (None, r["role"])
        ][-1]

    def element(self, name, role=None):
        record = self.find(name, role)
        return {"bus": record["bus"], "path": record["path"], "role": record["role"]}

    def call(self, destination, path, interface, method, signature, args, timeout_ms):
        assert 0 < timeout_ms <= 2000
        self.calls.append((destination, path, method, tuple(args)))
        if destination == atspi.REGISTRY:
            return ([(bus, atspi.ROOT_PATH) for bus in self.apps],)
        if destination in self.slow:
            raise Unresponsive()
        record = self.objects.get((destination, path))
        if record is None:
            raise Gone()
        interfaces = record["interfaces"]
        text = record.get("text") or ""
        if method == "GetChildren":
            return (list(record["refs"]),)
        if method == "GetRole":
            if record["role"] not in self.roles:
                self.roles.append(record["role"])
            return (self.roles.index(record["role"]),)
        if method == "GetRoleName":
            return (self.roles[self.roles.index(record["role"])],)
        if method == "GetState":
            words = [0, 0]
            for state in record["states"]:
                words[STATE_BITS[state] // 32] |= 1 << STATE_BITS[state] % 32
            return (words,)
        if method == "GetInterfaces":
            return (["org.a11y.atspi.Accessible", *(atspi.PREFIX + i for i in interfaces)],)
        if method == "GetIndexInParent":
            return (self.objects[record["parent"]]["refs"].index((destination, path)),)
        if method == "GetAll":
            count = record.get("child_count", len(record["refs"]))
            return ({"Name": record["name"], "Description": "", "ChildCount": count},)
        if method == "Get":
            prop = args[1]
            values = {
                "Name": record["name"],
                "ChildCount": record.get("child_count", len(record["refs"])),
                "Parent": record["parent"] or ("", "/org/a11y/atspi/null"),
                "CharacterCount": len(text),
                "CurrentValue": record.get("value", 0.0),
                "NActions": record.get("actions", 1 if "Action" in interfaces else 0),
            }
            return (values[prop],)
        if method == "GetText":
            assert record["role"] not in atspi.PRIVATE_ROLES, "a password is never read"
            return (text[args[0] : args[1]],)
        if method == "DoAction":
            record.setdefault("done", []).append(args[0])
            if "checkable" in record["states"]:
                record["states"] ^= {"checked"}
            return (record.get("action_result", True),)
        if method == "GetExtents":
            return (record.get("extents", (0, 0, 0, 0)),)
        if method == "GrabFocus":
            return (record.get("focusable", True),)
        if method == "SetTextContents":
            if record.get("settable", True):
                record["text"] = args[0]
            return (record.get("settable", True),)
        if method == "SelectChild":
            for index, ref in enumerate(record["refs"]):
                self.objects[ref]["states"].discard("selected")
                if index == args[0]:
                    self.objects[ref]["states"].add("selected")
            return (True,)
        raise Gone()


def reader(bus, seconds=4.0, calls=8000):
    return atspi.Reader(bus, atspi.Budget(seconds, calls))


def save_dialog():
    """A Greek save dialog over its document window: a field, a password, a box, a list."""
    return FakeBus(
        {
            ":1.7": (
                "Γραφείο",
                [
                    el("frame", "Έγγραφο 1 — Γραφείο", el("push button", "Αποθήκευση")),
                    el(
                        "dialog",
                        "Αποθήκευση ως",
                        el(
                            "filler",
                            "",
                            el("label", "Όνομα αρχείου:", interfaces={"Text"}),
                            el(
                                "text",
                                "Όνομα αρχείου:",
                                states={"focusable", "focused", "editable"},
                                interfaces={"Text", "EditableText", "Component"},
                                text="Έκθεση Μαρτίου.odt",
                                extents=(220, 140, 300, 24),
                            ),
                        ),
                        el(
                            "password text",
                            "Κωδικός",
                            states={"focusable", "editable"},
                            interfaces={"Text", "EditableText", "Component"},
                            text="s3cret-Λ",
                        ),
                        el(
                            "check box",
                            "Αποθήκευση με κωδικό",
                            states={"checkable", "focusable"},
                            interfaces={"Action", "Component"},
                            extents=(220, 200, 160, 20),
                        ),
                        el(
                            "list",
                            "Τύπος αρχείου",
                            el("list item", "ODF", states={"selectable", "selected"}),
                            el("list item", "Word 2007–365", states={"selectable"}),
                            interfaces={"Selection"},
                        ),
                        el("push button", "Άκυρο", interfaces={"Action", "Component"}),
                        el(
                            "push button",
                            "Αποθήκευση",
                            states={"is default"},
                            interfaces={"Action", "Component"},
                            extents=(500, 400, 90, 30),
                        ),
                        el("push button", "Βοήθεια", interfaces={"Action"}, off=True),
                        states={"active", "modal"},
                        interfaces={"Component"},
                    ),
                ],
            ),
            ":1.9": ("Αρχεία", [el("frame", "workspace — Αρχεία")]),
        }
    )


def view_of(bus, state=None):
    state = state if state is not None else desktop_elements.new_state()
    return desktop_elements.render(atspi.read(reader(bus)), state), state


def test_a_dialog_reads_as_one_line_per_element_with_refs_and_its_password_masked():
    bus = save_dialog()
    view, state = view_of(bus)
    assert view["focused_window"] == 'dialog "Αποθήκευση ως" (Γραφείο)'
    assert view["other_windows"] == [
        'frame "Έγγραφο 1 — Γραφείο"',
        'frame "workspace — Αρχεία"',
    ]
    assert view["text"].splitlines() == [
        '- dialog "Αποθήκευση ως" [modal]',
        "  - label: Όνομα αρχείου:",
        '  - text "Όνομα αρχείου:" [focused] [ref=e1]: Έκθεση Μαρτίου.odt',
        '  - password text "Κωδικός" [ref=e2]: [private field]',
        '  - check box "Αποθήκευση με κωδικό" [ref=e3]',
        '  - list "Τύπος αρχείου"',
        '    - list item "ODF" [selected] [ref=e4]',
        '    - list item "Word 2007–365" [ref=e5]',
        '  - push button "Άκυρο" [ref=e6]',
        '  - push button "Αποθήκευση" [default] [ref=e7]',
    ]
    assert view["complete"] is True
    # The password's text never left the application, and nothing hidden was read.
    assert "s3cret" not in json.dumps(atspi.read(reader(save_dialog())), ensure_ascii=False)
    assert "Βοήθεια" not in view["text"]
    assert state["refs"][2]["role"] == "password text"


def test_a_form_without_a_filled_password_shows_no_private_marker():
    bus = FakeBus(
        {
            ":1.3": (
                "Sign-in form",
                [
                    el(
                        "frame",
                        "Account",
                        el("label", "Email"),
                        el("text", "Email", interfaces={"Text", "EditableText"}, text=""),
                        el("password text", "Password", interfaces={"Text", "EditableText"}),
                        el("check box", "Remember me", states={"checkable", "checked"}),
                        el("push button", "Sign in", states={"is default"}, off=True),
                        states={"active"},
                    )
                ],
            )
        }
    )
    lines = view_of(bus)[0]["text"].splitlines()
    assert lines == [
        '- frame "Account"',
        "  - label: Email",
        '  - text "Email" [ref=e1]',
        '  - password text "Password" [ref=e2]',
        '  - check box "Remember me" [checked] [ref=e3]',
    ]


def test_an_open_menu_is_read_with_its_window_and_closed_menus_stay_out():
    bus = FakeBus(
        {
            ":1.4": (
                "Editor",
                [
                    el(
                        "frame",
                        "notes.txt — Editor",
                        el(
                            "menu bar",
                            "",
                            el(
                                "menu",
                                "File",
                                el("menu item", "Open…", off=True, interfaces={"Action"}),
                                states={"expandable", "expanded"},
                                interfaces={"Action"},
                            ),
                            el(
                                "menu",
                                "Edit",
                                el("menu item", "Undo", off=True, interfaces={"Action"}),
                                states={"expandable"},
                                interfaces={"Action"},
                            ),
                        ),
                        states={"active"},
                    ),
                    el(
                        "window",
                        "",
                        el(
                            "menu",
                            "File",
                            el("menu item", "New", interfaces={"Action"}),
                            el("menu item", "Save", interfaces={"Action"}, off=False),
                            el("separator"),
                            el(
                                "check menu item",
                                "Autosave",
                                states={"checkable", "checked"},
                                interfaces={"Action"},
                            ),
                            el("menu item", "Quit", interfaces={"Action"}, states=()),
                        ),
                    ),
                    el("tool tip", "Opens a file", off=True),
                ],
            )
        }
    )
    view, _ = view_of(bus)
    assert view["text"].splitlines() == [
        '- frame "notes.txt — Editor"',
        "  - menu bar",
        '    - menu "File" [expanded] [ref=e1]',
        '    - menu "Edit" [collapsed] [ref=e2]',
        "- window",
        '  - menu "File"',
        '    - menu item "New" [ref=e3]',
        '    - menu item "Save" [ref=e4]',
        '    - check menu item "Autosave" [checked] [ref=e5]',
        '    - menu item "Quit" [ref=e6]',
    ]
    assert "Open…" not in view["text"] and "Undo" not in view["text"]
    assert view["other_windows"] == []  # the popup belongs to the focused window


def test_a_chinese_window_and_a_disabled_control_keep_their_own_words():
    bus = FakeBus(
        {
            ":1.5": (
                "设置",
                [
                    el(
                        "frame",
                        "首选项 — 设置",
                        el("page tab list", "", el("page tab", "常规", states={"selected"})),
                        el("check box", "记住我", states={"checkable"}),
                        el(
                            "spin button",
                            "字号",
                            interfaces={"Value", "EditableText", "Text"},
                            value=12.5,
                        ),
                        el("push button", "确定", interfaces={"Action"}),
                        el(
                            "push button",
                            "应用",
                            interfaces={"Action"},
                            states={"showing", "visible"},
                            off=True,
                        ),
                        states={"active"},
                    )
                ],
            )
        }
    )
    lines = view_of(bus)[0]["text"].splitlines()
    assert lines == [
        '- frame "首选项 — 设置"',
        "  - page tab list",
        '    - page tab "常规" [selected]',
        '  - check box "记住我" [ref=e1]',
        '  - spin button "字号" [ref=e2]: 12.5',
        '  - push button "确定" [ref=e3]',
        '  - push button "应用" [disabled] [ref=e4]',
    ]


def test_no_focused_window_says_so_and_lists_the_windows_that_describe_themselves():
    bus = FakeBus({":1.2": ("Files", [el("frame", "Downloads")])})
    view, state = view_of(bus)
    assert view["focused_window"] is None
    assert "No focused window describes its elements" in view["text"]
    assert view["other_windows"] == ['frame "Downloads" (Files)']
    assert view["complete"] is False and state["refs"] == {}


def test_a_large_window_is_bounded_with_a_clear_stopped_line():
    rows = [el("list item", f"Αρχείο {i}", states={"selectable"}) for i in range(1200)]
    bus = FakeBus(
        {
            ":1.6": (
                "Files",
                [
                    el(
                        "frame",
                        "Big folder",
                        el("list", "Items", *rows[:350]),
                        el("list", "More", *rows[350:700]),
                        el("list", "Rest", *rows[700:1050]),
                        el("table", "Sheet", states={"manages descendants"}, child_count=10**6),
                        states={"active"},
                    )
                ],
            )
        }
    )
    reading = atspi.read(reader(bus))
    assert reading["stopped"] == "elements" and len(reading["nodes"]) == atspi.MAX_ELEMENTS
    view = desktop_elements.render(reading, desktop_elements.new_state())
    assert len(view["text"]) < desktop_elements.ELEMENT_VIEW_CHARS + 600
    last = view["text"].splitlines()[-1]
    assert last.startswith("[Stopped at") and "observe again" in last
    assert view["complete"] is False
    # A container that manages its own children is not enumerated: one line says so.
    bus.objects[(":1.6", bus.find("Items")["path"])]["refs"] = []
    reading = atspi.read(reader(bus))
    assert any(node.get("children_not_listed") == 10**6 for node in reading["nodes"])
    assert not any(
        call[2] == "GetChildren" and call[1] == bus.find("Sheet")["path"] for call in bus.calls
    )


def test_an_application_that_stops_answering_is_left_out_not_waited_on():
    bus = save_dialog()
    bus.slow.add(":1.9")
    reading = atspi.read(reader(bus))
    assert reading["unresponsive"] is True and reading["nodes"]
    text = desktop_elements.render(reading, desktop_elements.new_state())["text"]
    assert text.splitlines()[-1].startswith("[An application did not answer")


def test_refs_stay_with_their_element_and_never_name_another():
    bus = save_dialog()
    first, state = view_of(bus)
    assert "[ref=e7]" in first["text"]  # Αποθήκευση (save)
    # The application adds a control and replaces the cancel button by another kind of element.
    dialog = bus.find("Αποθήκευση ως")
    cancel = bus.find("Άκυρο")
    cancel["role"] = "toggle button"
    bus.counter += 1
    path = f"/org/a11y/atspi/accessible/{bus.counter}"
    bus._add(":1.7", path, el("push button", "Νέος φάκελος", interfaces={"Action"}), None)
    bus.objects[(":1.7", path)]["parent"] = (":1.7", dialog["path"])
    dialog["refs"].insert(0, (":1.7", path))
    second, _ = view_of(bus, state)
    assert '- push button "Αποθήκευση" [default] [ref=e7]' in second["text"]
    assert '- push button "Νέος φάκελος" [ref=e8]' in second["text"]
    assert '- toggle button "Άκυρο" [ref=e9]' in second["text"]
    assert desktop_elements.target(state, 6)["role"] == "push button"  # e6 still the old one
    with pytest.raises(desktop_elements.ElementRefusal, match="e42 is not a ref"):
        desktop_elements.target(state, 42)


def act(bus, name, action, **extra):
    role = {"Όνομα αρχείου:": "text"}.get(name)
    return atspi.act(reader(bus), {"element": bus.element(name, role), "action": action, **extra})


def test_activate_uses_the_default_action_and_falls_back_to_the_element_centre():
    bus = save_dialog()
    assert act(bus, "Αποθήκευση", "activate") == {"sent": "accessibility", "confirmed": True}
    assert bus.find("Αποθήκευση")["done"] == [0]
    bus.find("Αποθήκευση")["action_result"] = False
    assert act(bus, "Αποθήκευση", "activate") == {"pointer": {"x": 545, "y": 415}}
    bus.find("Άκυρο")["actions"] = 0
    assert act(bus, "Άκυρο", "activate") == {"refused": "no_position"}


def test_set_text_replaces_the_text_reads_it_back_and_types_where_it_cannot():
    bus = save_dialog()
    reply = act(bus, "Όνομα αρχείου:", "set_text", text="Σύνοψη 总结.odt")
    assert reply == {"sent": "accessibility", "confirmed": True, "value_matches": True}
    field = bus.find("Όνομα αρχείου:", "text")
    assert field["text"] == "Σύνοψη 总结.odt"
    # A password is set but never read back.
    reply = act(bus, "Κωδικός", "set_text", text="νέος")
    assert reply == {"sent": "accessibility", "confirmed": True}
    # A field that refuses direct setting is focused, and the worker types at its cursor.
    field["settable"] = False
    field["interfaces"] = {"EditableText", "Component"}
    assert act(bus, "Όνομα αρχείου:", "set_text", text="x") == {"focused": True, "then": "paste"}
    field["focusable"] = False
    assert act(bus, "Όνομα αρχείου:", "set_text", text="x") == {
        "pointer": {"x": 370, "y": 152},
        "then": "paste",
    }
    field["states"].add("read only")
    assert act(bus, "Όνομα αρχείου:", "set_text", text="x") == {"refused": "read_only"}


def test_toggle_and_select_reach_the_wanted_state_and_say_so():
    bus = save_dialog()
    assert act(bus, "Αποθήκευση με κωδικό", "toggle", checked=True) == {
        "sent": "accessibility",
        "confirmed": True,
        "checked": True,
    }
    assert act(bus, "Αποθήκευση με κωδικό", "toggle", checked=True) == {
        "unchanged": True,
        "checked": True,
    }
    assert act(bus, "Αποθήκευση", "toggle", checked=True) == {"refused": "not_toggle"}
    assert act(bus, "Word 2007–365", "select") == {
        "sent": "accessibility",
        "confirmed": True,
        "selected": True,
    }
    assert "selected" not in bus.find("ODF")["states"]
    assert act(bus, "Word 2007–365", "select") == {"unchanged": True, "selected": True}
    radio = FakeBus(
        {":1.1": ("A", [el("frame", "W", el("radio button", "On", states={"checked"}))])}
    )
    assert act(radio, "On", "toggle", checked=False) == {"refused": "radio"}


@pytest.mark.parametrize(
    "change,refusal",
    [
        (lambda r: r["states"].discard("showing"), "hidden"),
        (lambda r: r["states"].difference_update({"enabled", "sensitive"}), "disabled"),
        (lambda r: r.update(role="label"), "changed"),
        (lambda r: r["states"].add("defunct"), "gone"),
    ],
)
def test_an_element_that_changed_is_refused_before_anything_is_sent(change, refusal):
    bus = save_dialog()
    element = bus.element("Αποθήκευση")
    change(bus.find("Αποθήκευση"))
    assert atspi.act(reader(bus), {"element": element, "action": "activate"}) == {
        "refused": refusal
    }
    assert not bus.find("Αποθήκευση").get("done")
    del bus.objects[(element["bus"], element["path"])]
    assert atspi.run("act", {"element": element, "action": "activate"}, bus) == {"refused": "gone"}


def test_invalid_element_names_are_refused_before_any_call():
    bus = save_dialog()
    for element in (
        {"bus": "not a bus", "path": "/a", "role": "x"},
        {"bus": ":1.7", "path": "relative/path", "role": "x"},
        {"bus": ":1.7", "path": "/a/../b", "role": "x"},
        None,
    ):
        with pytest.raises(ValueError):
            atspi.act(reader(bus), {"element": element, "action": "activate"})
    assert bus.calls == []


def test_the_reader_child_replies_one_json_object_without_exception_text(monkeypatch, capsys):
    monkeypatch.setattr(atspi, "GioBus", lambda: (_ for _ in ()).throw(OSError("bus /secret")))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"{}")))
    atspi.main(["reader", "read"])
    assert json.loads(capsys.readouterr().out) == {"unavailable": "bus"}
    atspi.main(["reader", "format_disk"])
    assert json.loads(capsys.readouterr().out) == {"unavailable": "operation"}


# --- The host: offering, observing and acting through DesktopComputers ----------------------


def png():
    image = io.BytesIO()
    Image.new("RGB", (2, 2), "teal").save(image, "PNG")
    return image.getvalue()


class ElementExecutor:
    """A running owl computer; its desktop reads the scripted bus when it declares elements."""

    def __init__(self, owner, workspace, *, cancel, features=("elements",)):
        self.owner, self.workspace = owner, workspace
        self.closed = False
        self.calls = []
        self.desktop_features = frozenset(features)
        self.bus = save_dialog()
        self.png = png()

    def alive(self):
        return not self.closed

    def capture(self):
        return self.png

    def close(self):
        self.closed = True

    def call(self, owner, operation, args, *, cancel):
        assert owner == self.owner
        self.calls.append((operation, args))
        if operation == "observe":
            return self.observe(args)
        if operation == "element":
            request = {k: args[k] for k in ("element", "action", "text", "checked") if k in args}
            reply = atspi.act(reader(self.bus), request)
            if "refused" in reply:
                return {"dispatched": False, "refused": reply["refused"]}
            kept = ("confirmed", "checked", "selected", "value_matches", "unchanged")
            return {
                "dispatched": not reply.get("unchanged"),
                "verification_required": True,
                **{key: reply[key] for key in kept if key in reply},
                "observation": self.observe(args),
            }
        if operation in {"click", "type", "key", "scroll"}:
            return {
                "dispatched": True,
                "verification_required": True,
                "observation": self.observe(args),
            }
        return {"exit_code": 0, "output": "fixture result"}

    def observe(self, args):
        observation = {
            "frame_id": uuid4().hex,
            "width": 2,
            "height": 2,
            "png": base64.b64encode(self.png).decode(),
        }
        if args.get("elements") is True:
            observation["elements"] = atspi.read(reader(self.bus))
        if args.get("image") is False:
            observation.pop("png")
        return observation


@pytest.fixture
def computers():
    made = []

    def build(features=("elements",)):
        def factory(*args, **kwargs):
            executor = ElementExecutor(*args, **kwargs, features=features)
            made.append(executor)
            return executor

        return DesktopComputers("owner", ComputerViews(), factory=factory), made

    managers = []

    def make(features=("elements",)):
        manager, _ = build(features)
        managers.append(manager)
        return manager

    yield make, made
    for manager in managers:
        manager.close()


def task():
    return {"id": "task", "bot_id": "bot", "config": {"files_enabled": False}}


def token():
    return EventCancellationToken(threading.Event())


def offered(manager, image_input):
    schemas = manager.schemas(task(), True, approved=True, image_input=image_input)
    return {
        s["function"]["name"] for s in manager.offered(task(), schemas, image_input=image_input)
    }


def call(manager, name, args=None, *, image_input, review=None):
    result = manager.call(
        task(),
        name,
        args or {},
        token(),
        approved=True,
        image_input=image_input,
        review_action=review,
    )
    return result


def test_an_older_computer_image_keeps_todays_tools_and_requests(computers):
    make, made = computers
    manager = make(features=())
    for image_input in (False, True):
        assert "computer_element" not in offered(manager, image_input)
    assert "computer_observe" not in offered(manager, False)
    call(manager, "computer_run", {"command": "true"}, image_input=False)
    # The computer runs and declares nothing: still today's tools, and no new request fields.
    assert offered(manager, False) == {
        "computer_run",
        "computer_launch",
        "computer_process",
        "computer_stop_process",
    }
    assert "computer_element" not in offered(manager, True)
    with pytest.raises(DesktopError, match="unavailable"):
        call(manager, "computer_observe", image_input=False)
    observed = call(manager, "computer_observe", image_input=True)
    assert isinstance(observed, BrowserImageResult)
    assert "element_view" not in observed.observation
    assert made[0].calls[-1] == ("observe", {})
    with pytest.raises(DesktopError, match="cannot read its windows' elements"):
        call(
            manager,
            "computer_element",
            {"source_id": observed.observation["source_id"], "ref": 1, "action": "activate"},
            image_input=True,
        )


def test_a_computer_that_reads_elements_offers_them_beside_the_image(computers):
    make, made = computers
    manager = make()
    assert "computer_element" not in offered(manager, True)  # nothing runs yet
    observed = call(manager, "computer_observe", image_input=True)
    assert isinstance(observed, BrowserImageResult)
    view = observed.observation
    assert view["frame"]["coordinate_space"] == "image_pixels"
    assert '- push button "Αποθήκευση" [default] [ref=e7]' in view["element_view"]
    assert view["focused_window"] == 'dialog "Αποθήκευση ως" (Γραφείο)'
    assert made[0].calls[-1] == ("observe", {"elements": True})
    assert "computer_element" in offered(manager, True)
    # Pixel input stays as fast as today: no element reading after a click.
    clicked = call(
        manager,
        "computer_click",
        {"source_id": view["source_id"], "x": 1, "y": 1},
        image_input=True,
    )
    assert made[0].calls[-1][1] == {"frame_id": view["source_id"], "x": 1, "y": 1, "button": 1}
    assert "element_view" not in clicked.observation


def test_a_model_without_images_gets_the_element_view_alone(computers):
    make, made = computers
    manager = make()
    call(manager, "computer_run", {"command": "libreoffice"}, image_input=False)
    assert {"computer_observe", "computer_key", "computer_element"} <= offered(manager, False)
    assert not {"computer_click", "computer_type", "computer_scroll"} & offered(manager, False)
    observed = call(manager, "computer_observe", image_input=False)
    assert not isinstance(observed, BrowserImageResult)
    assert "frame" not in observed and "png" not in json.dumps(observed)
    assert observed["element_view"].startswith('- dialog "Αποθήκευση ως" [modal]')
    assert made[0].calls[-1] == ("observe", {"elements": True, "image": False})
    pressed = call(
        manager,
        "computer_key",
        {"source_id": observed["source_id"], "keys": ["Tab"]},
        image_input=False,
    )
    assert pressed["dispatched"] and "element_view" in pressed
    assert made[0].calls[-1][1]["elements"] is True and made[0].calls[-1][1]["image"] is False
    with pytest.raises(DesktopError, match="unavailable"):
        call(
            manager,
            "computer_click",
            {"source_id": pressed["source_id"], "x": 1, "y": 1},
            image_input=False,
        )


def test_element_actions_resolve_refs_pass_the_review_and_refuse_typed(computers):
    make, made = computers
    manager = make()
    reviews = []
    call(manager, "computer_launch", {"command": "soffice"}, image_input=False)
    observed = call(manager, "computer_observe", image_input=False)
    source = observed["source_id"]
    acted = call(
        manager,
        "computer_element",
        {"source_id": source, "ref": 3, "action": "toggle", "checked": True},
        image_input=False,
        review=lambda *args, **kwargs: reviews.append((args, kwargs)),
    )
    assert acted["dispatched"] and acted["checked"] is True and acted["confirmed"] is True
    assert '- check box "Αποθήκευση με κωδικό" [checked] [ref=e3]' in acted["element_view"]
    sent = made[0].calls[-1]
    assert sent[0] == "element" and sent[1]["action"] == "toggle" and sent[1]["checked"] is True
    assert sent[1]["element"] == made[0].bus.element("Αποθήκευση με κωδικό")
    (name, arguments, context), options = reviews[0]
    assert name == "computer_element" and options == {"private_computer": True}
    assert context["element"] == {
        "role": "check box",
        "name": "Αποθήκευση με κωδικό",
        "window": 'dialog "Αποθήκευση ως" (Γραφείο)',
    }
    # A ref outlives the observation it came from: e3 again, with the newer source.
    again = call(
        manager,
        "computer_element",
        {"source_id": acted["source_id"], "ref": 3, "action": "toggle", "checked": True},
        image_input=False,
    )
    assert again["dispatched"] is False and "already showed that state" in again["note"]
    # The application closes the button: typed refusal, nothing sent, observe again.
    made[0].bus.find("Αποθήκευση")["states"].add("defunct")
    with pytest.raises(desktop_elements.ElementRefusal, match="no longer on the screen"):
        call(
            manager,
            "computer_element",
            {"source_id": again["source_id"], "ref": 7, "action": "activate"},
            image_input=False,
        )
    with pytest.raises(desktop_elements.ElementRefusal, match="e99 is not a ref"):
        call(
            manager,
            "computer_element",
            {"source_id": again["source_id"], "ref": 99, "action": "activate"},
            image_input=False,
        )
    with pytest.raises(DesktopError, match="expired"):
        call(
            manager,
            "computer_element",
            {"source_id": "not-an-observation", "ref": 3, "action": "activate"},
            image_input=False,
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {"ref": 1, "action": "set_text"},
        {"ref": 1, "action": "activate", "text": "x"},
        {"ref": 1, "action": "toggle"},
        {"ref": 1, "action": "select", "checked": True},
        {"ref": 0, "action": "activate"},
        {"ref": "e1", "action": "activate"},
        {"ref": 1, "action": "double_click"},
        {"ref": 1, "action": "set_text", "text": "a\0b"},
    ],
)
def test_unclear_element_requests_are_refused_before_the_computer(computers, arguments):
    make, made = computers
    manager = make()
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        call(manager, "computer_element", {"source_id": "s", **arguments}, image_input=True)
    assert made == []


# --- The guest worker: declaring, reading and acting inside the computer --------------------


@pytest.fixture
def worker():
    from alysis_code.bots.desktop_worker import Computer

    worker = Computer.__new__(Computer)
    worker.shared_display = False
    worker.features = ("elements",)
    worker.frame = ("frame", time.monotonic(), b"pixels")
    worker.capture = lambda: b"pixels"
    worker.calls = []
    worker.xdo = lambda *args, **kwargs: worker.calls.append(args)
    worker.move_pointer = lambda x, y: worker.calls.append(("move", x, y))
    worker.paste_text = lambda text: worker.calls.append(("paste", text))
    worker.observe = lambda: {"frame_id": "new", "png": "image"}
    worker.replies = []
    worker.requests = []

    def accessibility(operation, request=None, **kwargs):
        worker.requests.append((operation, request))
        return worker.replies.pop(0)

    worker.accessibility = accessibility
    return worker


ELEMENT = {"bus": ":1.7", "path": "/org/a11y/atspi/accessible/12", "role": "push button"}


def test_ping_declares_elements_only_when_the_desktop_reads_them(worker):
    assert worker.dispatch("ping", {})["features"] == ["elements"]
    worker.features = ()
    assert worker.dispatch("ping", {})["features"] == []


def test_observing_adds_the_reading_and_leaves_out_the_image_for_text_models(worker):
    reading = {"focused": None, "windows": [], "nodes": [], "stopped": None}
    worker.replies = [reading]
    observed = worker.dispatch("observe", {"elements": True, "image": False})
    assert observed == {"frame_id": "new", "elements": reading}
    worker.replies = [{"unavailable": "bus"}]
    assert worker.dispatch("observe", {"elements": True})["elements_unavailable"] == "bus"
    assert worker.dispatch("observe", {}) == {"frame_id": "new", "png": "image"}
    worker.features = ()
    assert worker.dispatch("observe", {"elements": True})["elements_unavailable"] == "unsupported"


def test_a_pointer_fallback_clicks_the_element_centre_then_types_or_verifies(worker):
    reading = {"focused": None, "windows": [], "nodes": [], "stopped": None}
    worker.replies = [{"pointer": {"x": 545, "y": 415}}, reading]
    result = worker.dispatch("element", {"element": ELEMENT, "action": "activate"})
    assert worker.calls == [("move", 545, 415), ("mousedown", "1"), ("mouseup", "1")]
    assert result["dispatched"] and result["method"] == "pointer"
    assert result["observation"]["elements"] == reading and worker.frame is None
    worker.calls.clear()
    field = {**ELEMENT, "role": "text"}
    worker.replies = [{"pointer": {"x": 10, "y": 20}, "then": "paste"}, reading]
    result = worker.dispatch("element", {"element": field, "action": "set_text", "text": "Ωμέγα"})
    assert worker.calls[-1] == ("paste", "Ωμέγα") and result["typed_at_cursor"] is True
    worker.calls.clear()
    box = {**ELEMENT, "role": "check box"}
    worker.replies = [{"pointer": {"x": 10, "y": 20}, "then": "verify"}, {"checked": True}, reading]
    result = worker.dispatch("element", {"element": box, "action": "toggle", "checked": True})
    assert result["checked"] is True
    assert worker.requests[-2] == ("state", {"element": box})


@pytest.mark.parametrize(
    "reply,expected",
    [
        ({"refused": "gone"}, {"dispatched": False, "refused": "gone"}),
        ({"refused": "made-up"}, {"dispatched": False, "refused": "unavailable"}),
        ({"unavailable": "bus"}, {"dispatched": False, "refused": "unavailable"}),
        ({"pointer": {"x": 4000, "y": 20}}, {"dispatched": False, "refused": "no_position"}),
    ],
)
def test_a_refused_element_action_sends_no_input(worker, reply, expected):
    worker.replies = [reply]
    assert worker.dispatch("element", {"element": ELEMENT, "action": "activate"}) == expected
    assert worker.calls == [] and worker.frame is not None


def test_a_reader_stopped_part_way_reports_an_uncertain_action(worker):
    reading = {"focused": None, "windows": [], "nodes": [], "stopped": None}
    worker.replies = [{"unavailable": "timeout"}, reading]
    result = worker.dispatch("element", {"element": ELEMENT, "action": "activate"})
    assert result["uncertain"] is True and result["dispatched"] is None
    assert "observation" in result


@pytest.mark.parametrize(
    "arguments",
    [
        {"element": {**ELEMENT, "bus": "nobody"}, "action": "activate"},
        {"element": {**ELEMENT, "role": ""}, "action": "activate"},
        {"element": ELEMENT, "action": "drag"},
        {"element": ELEMENT, "action": "set_text", "text": "x" * 8001},
        {"element": ELEMENT, "action": "toggle"},
    ],
)
def test_invalid_element_requests_are_rejected_in_the_computer(worker, arguments):
    from alysis_code.bots.desktop_worker import Rejected

    with pytest.raises(Rejected):
        worker.dispatch("element", arguments)
    assert worker.requests == [] and worker.calls == []
    worker.features = ()
    with pytest.raises(Rejected, match="cannot act on window elements"):
        worker.dispatch("element", {"element": ELEMENT, "action": "activate"})


def test_a_reader_child_is_bounded_in_time_and_size():
    from alysis_code.bots import desktop_worker

    def start(code):
        return subprocess.Popen(
            [sys.executable, "-I", "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    echo = "import sys,json; print(json.dumps({'got': json.loads(sys.stdin.read() or '{}')}))"
    assert desktop_worker.child_reply(start(echo), '{"a": "Ω"}'.encode(), 10) == {"got": {"a": "Ω"}}
    # Without a request (the startup probe) the child still reaches the end of its input.
    began = time.monotonic()
    assert desktop_worker.child_reply(start(echo), None, 10) == {"got": {}}
    assert time.monotonic() - began < 5
    slow = start("import time; time.sleep(30)")
    began = time.monotonic()
    assert desktop_worker.child_reply(slow, b"{}", 0.5) == {"unavailable": "timeout"}
    assert time.monotonic() - began < 5 and slow.poll() is not None
    flood = start("import sys; sys.stdout.write('x' * 2_000_000)")
    assert desktop_worker.child_reply(flood, None, 10) == {"unavailable": "size"}
    assert desktop_worker.child_reply(start("print('[1]')"), None, 10) == {"unavailable": "invalid"}


# --- The handshake on each kind of computer --------------------------------------------------


@pytest.mark.parametrize(
    "ping,declared",
    [
        ({"ready": True, "network": "offline"}, frozenset()),
        ({"ready": True, "features": ["elements"]}, frozenset({"elements"})),
        ({"ready": True, "features": "elements"}, frozenset()),
        ({"ready": True, "features": ["elements", 3, "x" * 65]}, frozenset({"elements"})),
    ],
)
def test_a_local_desktop_records_what_its_worker_declares(ping, declared):
    import tempfile
    from pathlib import Path

    from alysis_code.bots.desktop_linux import OfflineLinuxDesktop

    script = (
        "import sys,json\n"
        "request=json.loads(sys.stdin.readline())\n"
        f"print(json.dumps({{'id': request['id'], 'result': {ping!r}}}), flush=True)\n"
        "sys.stdin.readline()\n"
    )
    computer = OfflineLinuxDesktop.__new__(OfflineLinuxDesktop)
    computer.owner, computer._closed, computer._cleaned = "owner", False, False
    computer._stop, computer._lock = threading.Event(), threading.RLock()
    computer._buffer, computer.session_id = b"", uuid4().hex
    computer._directory = tempfile.TemporaryDirectory()
    computer.desktop_features = frozenset()
    computer.workspace, computer.shared_display = Path(computer._directory.name), None
    computer.packages = None
    computer._process = subprocess.Popen(
        [sys.executable, "-I", "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        bufsize=0,
    )
    try:
        computer._handshake(lambda: False)
        assert computer.desktop_features == declared
    finally:
        computer.close()
    argv = OfflineLinuxDesktop.build_argv(computer, Path("/tmp/root"))
    assert argv[argv.index("/opt/desktop_accessibility.py") - 2] == "--ro-bind"


def test_a_cloud_computer_declares_elements_with_its_replies_and_an_older_image_none(tmp_path):
    from test_bots_cloud_desktop import Provider

    from alysis_code.bots.cloud_desktop import CloudDesktopFactory
    from alysis_code.bots.compute import CommandReceipt, ComputerSpec

    class Declaring(Provider):
        features = None

        def run(self, ref, argv, *, timeout):
            receipt = super().run(ref, argv, timeout=timeout)
            if argv[-2] == "start" or self.features is None:
                return receipt
            reply = json.loads(receipt.stdout)
            reply["desktop_features"] = self.features
            return CommandReceipt(0, json.dumps(reply), "")

    provider = Declaring()
    factory = CloudDesktopFactory(
        "owner", provider, ComputerSpec("prepared-image"), tmp_path / "ledger"
    )
    computer = factory("bot", tmp_path, cancel=lambda: False)
    try:
        assert computer.desktop_features == frozenset()
        computer.call("bot", "run", {"command": "true"})
        assert computer.desktop_features == frozenset()  # an older image declares nothing
        provider.features = ["elements", 7]
        computer.call("bot", "run", {"command": "true"})
        assert computer.desktop_features == frozenset({"elements"})
        computer.call("bot", "element", {"element": ELEMENT, "action": "activate"})
        sent = json.loads(base64.b64decode(provider.actions[-1][-1]))
        assert sent["operation"] == "element" and sent["arguments"]["element"] == ELEMENT
    finally:
        computer.close()


# --- A whole owl turn: a model without image input works through the element view ------------


def test_a_text_only_owl_reads_and_acts_by_element_once_its_computer_declares_them(tmp_path):
    from test_bots_browser_runtime import ClientFixture, advertised_names, response
    from test_bots_desktop_runtime import run_task

    from alysis_code.bots.models import BotInput, TaskInput
    from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore
    from alysis_code.bots.runtime import BotRuntime
    from alysis_code.bots.store import Store

    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Mira", purpose="Office work"))
    chat = store.create_chat("owner", bot["id"], "Conversation")
    store.enqueue(
        "owner", chat["id"], TaskInput(message="Save the report", request_id="save-the-report")
    )
    first = store.claim("owner")
    proposals = ProposalStore(store, "owner")
    proposal = proposals.propose(
        first,
        ActionProposal(
            question="Use the computer?", instructions="Save the report.", offline_computer=True
        ),
    )
    store.finish("owner", first["id"], "completed", content="Please review the scope.")
    proposals.decide(
        bot["id"],
        proposal["id"],
        ActionDecision(action="accept", expected_revision=1, offline_computer=True),
    )
    task = store.claim("owner")
    made = []

    def factory(*args, **kwargs):
        made.append(ElementExecutor(*args, **kwargs))
        return made[-1]

    manager = DesktopComputers("owner", ComputerViews(), factory=factory)

    def press_save(request):
        receipt = next(
            json.loads(m["content"])
            for m in reversed(request["messages"])
            if m["role"] == "tool" and "element_view" in m["content"]
        )
        ref = int(receipt["element_view"].split('"Αποθήκευση" [default] [ref=e')[1].split("]")[0])
        return response(
            name="computer_element",
            arguments={"source_id": receipt["source_id"], "ref": ref, "action": "activate"},
        )

    class Scripted(ClientFixture):
        def chat(self, **kwargs):
            reply = super().chat(**kwargs)
            return reply(self.requests[-1]) if callable(reply) else reply

    client = Scripted(
        [
            response(name="computer_launch", arguments={"command": "soffice --writer"}),
            response(name="computer_observe"),
            press_save,
            response("Saved the report."),
        ]
    )
    client._alysis_browser_image_input = False
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    try:
        run_task(runtime, task)
    finally:
        manager.close()
    assert store.task("owner", task["id"])["status"] == "completed"
    before, after = advertised_names(client.requests[0]), advertised_names(client.requests[1])
    # Nothing runs yet: today's tools. Its computer declared elements: observing and acting.
    assert not {"computer_observe", "computer_element", "computer_key"} & before
    assert {"computer_observe", "computer_element", "computer_key"} <= after
    assert not {"computer_click", "computer_type", "computer_scroll"} & after
    assert made[0].bus.find("Αποθήκευση")["done"] == [0]
    assert [c[0] for c in made[0].calls] == ["launch", "observe", "element"]
    messages = client.requests[-1]["messages"]
    assert not any(isinstance(m.get("content"), list) for m in messages)  # no image anywhere


def test_the_accessibility_bus_starts_only_within_the_startup_budget(monkeypatch):
    import os

    from alysis_code.bots import desktop_worker as module

    worker = module.Computer.__new__(module.Computer)
    worker.env, worker.services = {"DISPLAY": ":5", "HOME": "/tmp/home"}, []
    monkeypatch.setattr(module, "accessibility_installed", lambda: True)
    started, killed, address = [], [], [b"unix:path=/tmp/dbus-Q1,guid=ab12\n"]

    def popen(argv, **kwargs):
        started.append(argv)
        if "--print-address=1" in argv:
            read, write = os.pipe()
            os.write(write, address[0])
            os.close(write)
            return SimpleNamespace(
                stdout=os.fdopen(read, "rb", buffering=0), kill=lambda: killed.append(argv)
            )
        return SimpleNamespace(probe=argv)

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    # A computer that took its whole budget to start keeps today's desktop.
    assert worker._start_accessibility(time.monotonic() - 1) is None and started == []
    probe = worker._start_accessibility(time.monotonic() + 5)
    bus = started[0]
    assert bus[0] == "/usr/bin/bwrap" and bus[bus.index("--") + 1] == "/usr/bin/dbus-daemon"
    assert probe.probe[-2:] == [str(module.ACCESSIBILITY), "probe"]
    assert probe.probe[0] == "/usr/bin/bwrap"  # the reader runs like any application
    assert worker.env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/tmp/dbus-Q1,guid=ab12"
    assert worker.env["QT_LINUX_ACCESSIBILITY_ALWAYS_ON"] == "1"
    # Anything but a local socket address is refused, and that bus is ended.
    worker.env, started[:] = {"DISPLAY": ":5"}, []
    address[0] = b"tcp:host=example.invalid,port=1\n"
    assert worker._start_accessibility(time.monotonic() + 5) is None
    assert killed and "DBUS_SESSION_BUS_ADDRESS" not in worker.env and len(started) == 1
