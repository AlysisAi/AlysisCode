"""Atomic human gestures on an executor-owned X11 display.

Standalone for the offline namespace. Text travels over stdin, never argv, files,
logs or tool history. No shell, display discovery or listener. Every gesture
releases pressed input. Clipboard ownership lasts only within the human handoff.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path


def integer(value, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError("Invalid input coordinate or count.")
    return value


def validate(event):
    if not isinstance(event, dict):
        raise ValueError("Invalid human input.")
    kind = event.get("kind")
    fields = {
        "release": {"kind"},
        "click": {"kind", "x", "y", "button"},
        "drag": {"kind", "x", "y", "to_x", "to_y", "button"},
        "scroll": {"kind", "x", "y", "ticks"},
        "text": {"kind", "text"},
        "key": {"kind", "keys"},
    }
    if kind not in fields or set(event) != fields[kind]:
        raise ValueError("Invalid human input shape.")
    for key in ("x", "y", "to_x", "to_y"):
        if key in event:
            integer(event[key], 0, 2559 if key.endswith("x") else 1599)
    if "button" in event:
        integer(event["button"], 1, 3)
    if "ticks" in event:
        integer(event["ticks"], -10, 10)
    if kind == "text":
        value = event["text"]
        if not isinstance(value, str) or not 1 <= len(value) <= 8000 or "\0" in value:
            raise ValueError("Invalid input text.")
    if kind == "key":
        keys = event["keys"]
        if not isinstance(keys, list) or not 1 <= len(keys) <= 4:
            raise ValueError("Invalid key combination.")
        xlib = ctypes.CDLL("libX11.so.6")
        xlib.XStringToKeysym.argtypes = [ctypes.c_char_p]
        xlib.XStringToKeysym.restype = ctypes.c_ulong
        for key in keys:
            if (
                not isinstance(key, str)
                or not re.fullmatch(r"[A-Za-z0-9_]{1,40}", key)
                or not xlib.XStringToKeysym(key.encode())
            ):
                raise ValueError("Invalid X11 key name.")


def release(display_name):
    # This protocol emits only core buttons 1–5; it never admits arbitrary devices.
    x11, xtst = ctypes.CDLL("libX11.so.6"), ctypes.CDLL("libXtst.so.6")
    pointer = ctypes.c_void_p
    x11.XOpenDisplay.argtypes, x11.XOpenDisplay.restype = [ctypes.c_char_p], pointer
    x11.XDefaultRootWindow.argtypes, x11.XDefaultRootWindow.restype = [pointer], ctypes.c_ulong
    x11.XQueryKeymap.argtypes = [pointer, pointer]
    x11.XQueryPointer.argtypes = [pointer, ctypes.c_ulong] + [pointer] * 7
    x11.XSync.argtypes, x11.XCloseDisplay.argtypes = [pointer, ctypes.c_int], [pointer]
    xtst.XTestFakeKeyEvent.argtypes = [pointer, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    xtst.XTestFakeButtonEvent.argtypes = [pointer, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    display = x11.XOpenDisplay(display_name.encode())
    if not display:
        raise ValueError("Private display unavailable.")

    def pressed():
        keys = (ctypes.c_ubyte * 32)()
        root, child = ctypes.c_ulong(), ctypes.c_ulong()
        x, y, wx, wy = (ctypes.c_int() for _ in range(4))
        mask = ctypes.c_uint()
        if not x11.XQueryKeymap(display, keys) or not x11.XQueryPointer(
            display,
            x11.XDefaultRootWindow(display),
            ctypes.byref(root),
            ctypes.byref(child),
            ctypes.byref(x),
            ctypes.byref(y),
            ctypes.byref(wx),
            ctypes.byref(wy),
            ctypes.byref(mask),
        ):
            raise ValueError("Input state unavailable.")
        return [k for k in range(8, 256) if keys[k // 8] & (1 << (k % 8))], [
            b for b in range(1, 6) if mask.value & (1 << (7 + b))
        ]

    try:
        keys, buttons = pressed()
        for key in keys:
            if not xtst.XTestFakeKeyEvent(display, key, 0, 0):
                raise ValueError("Key release failed.")
        for button in buttons:
            if not xtst.XTestFakeButtonEvent(display, button, 0, 0):
                raise ValueError("Button release failed.")
        x11.XSync(display, 0)
        if any(pressed()):
            raise ValueError("Input release is unconfirmed.")
    finally:
        x11.XCloseDisplay(display)


def paste(display_name, text, env, xdo, *, persistent=False):
    """Serve UTF-8 through the executor's private X11 selection.

    TARGETS requests are negotiation, not evidence that text was consumed. Wait
    for the receiver to delete the delivered property before acknowledging input.
    Persistent ownership ends when HumanInput replaces or releases this helper.
    """
    ptr, ulong = ctypes.c_void_p, ctypes.c_ulong
    common = [
        ("type", ctypes.c_int),
        ("serial", ulong),
        ("send_event", ctypes.c_int),
        ("display", ptr),
    ]

    class Request(ctypes.Structure):
        _fields_ = common + [
            (name, ulong)
            for name in ("owner", "requestor", "selection", "target", "property", "time")
        ]

    class Selection(ctypes.Structure):
        _fields_ = common + [
            (name, ulong) for name in ("requestor", "selection", "target", "property", "time")
        ]

    class Property(ctypes.Structure):
        _fields_ = (
            common
            + [(name, ulong) for name in ("window", "atom", "time")]
            + [("state", ctypes.c_int)]
        )

    class Event(ctypes.Union):
        _fields_ = [
            ("type", ctypes.c_int),
            ("request", Request),
            ("selection", Selection),
            ("property", Property),
            ("pad", ctypes.c_long * 24),
        ]

    x11 = ctypes.CDLL("libX11.so.6")
    x11.XOpenDisplay.argtypes, x11.XOpenDisplay.restype = [ctypes.c_char_p], ptr
    x11.XDefaultRootWindow.argtypes, x11.XDefaultRootWindow.restype = [ptr], ulong
    x11.XCreateSimpleWindow.argtypes = [
        ptr,
        ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_uint,
        ulong,
        ulong,
    ]
    x11.XCreateSimpleWindow.restype = ulong
    x11.XInternAtom.argtypes, x11.XInternAtom.restype = [ptr, ctypes.c_char_p, ctypes.c_int], ulong
    x11.XSetSelectionOwner.argtypes = [ptr, ulong, ulong, ulong]
    x11.XGetSelectionOwner.argtypes, x11.XGetSelectionOwner.restype = [ptr, ulong], ulong
    x11.XSync.argtypes = [ptr, ctypes.c_int]
    x11.XFlush.argtypes = [ptr]
    x11.XPending.argtypes = [ptr]
    x11.XNextEvent.argtypes = [ptr, ptr]
    x11.XSelectInput.argtypes = [ptr, ulong, ctypes.c_long]
    x11.XChangeProperty.argtypes = [
        ptr,
        ulong,
        ulong,
        ulong,
        ctypes.c_int,
        ctypes.c_int,
        ptr,
        ctypes.c_int,
    ]
    x11.XSendEvent.argtypes = [ptr, ulong, ctypes.c_int, ctypes.c_long, ptr]
    x11.XDestroyWindow.argtypes = [ptr, ulong]
    x11.XCloseDisplay.argtypes = [ptr]
    display = x11.XOpenDisplay(display_name.encode())
    if not display:
        raise ValueError("Private clipboard unavailable.")
    window = None
    try:

        def atom(name):
            return x11.XInternAtom(display, name, 0)

        clipboard, targets, utf8, plain, string = [
            atom(name)
            for name in (
                b"CLIPBOARD",
                b"TARGETS",
                b"UTF8_STRING",
                b"text/plain;charset=utf-8",
                b"STRING",
            )
        ]
        window = x11.XCreateSimpleWindow(
            display, x11.XDefaultRootWindow(display), 0, 0, 1, 1, 0, 0, 0
        )
        x11.XSetSelectionOwner(display, clipboard, window, 0)
        x11.XSync(display, 0)
        if x11.XGetSelectionOwner(display, clipboard) != window:
            raise ValueError("Private clipboard ownership unavailable.")
        xdo("key", "--clearmodifiers", "Control_L+v")
        deadline, delivered, acknowledged = time.monotonic() + 3, None, False
        while acknowledged or time.monotonic() < deadline:
            if not x11.XPending(display):
                time.sleep(0.005)
                continue
            event = Event()
            x11.XNextEvent(display, ctypes.byref(event))
            if (
                event.type == 28
                and delivered == (event.property.window, event.property.atom)
                and event.property.state == 1
            ):
                if not persistent:
                    return
                if not acknowledged:
                    print("consumed", flush=True)
                    acknowledged = True
                delivered = None
                continue
            if event.type != 30:
                continue
            request = event.request
            prop = request.property or request.target
            notify = Event()
            notify.selection = Selection(
                31,
                0,
                1,
                display,
                request.requestor,
                request.selection,
                request.target,
                0,
                request.time,
            )
            if request.selection == clipboard and request.target == targets:
                supported = (ulong * 4)(targets, utf8, plain, string)
                x11.XChangeProperty(display, request.requestor, prop, 4, 32, 0, supported, 4)
                notify.selection.property = prop
            elif request.selection == clipboard and request.target in (utf8, plain, string):
                payload = text.encode(
                    "latin-1" if request.target == string else "utf-8", errors="strict"
                )
                x11.XSelectInput(display, request.requestor, 1 << 22)
                x11.XChangeProperty(
                    display,
                    request.requestor,
                    prop,
                    request.target,
                    8,
                    0,
                    ctypes.c_char_p(payload),
                    len(payload),
                )
                notify.selection.property = prop
                delivered = (request.requestor, prop)
            x11.XSendEvent(display, request.requestor, 0, 0, ctypes.byref(notify))
            x11.XFlush(display)
        raise ValueError("Paste consumption was not confirmed.")
    finally:
        if window:
            x11.XDestroyWindow(display, window)
        x11.XCloseDisplay(display)


def dispatch(display, event, *, persistent=False):
    validate(event)
    env = {"PATH": "/usr/bin:/bin", "DISPLAY": display, "LANG": "C.UTF-8"}

    def xdo(*args, text=None):
        subprocess.run(
            ["/usr/bin/xdotool", *args],
            input=text,
            text=True,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=5,
        )

    kind = event["kind"]
    try:
        if kind in {"click", "drag", "scroll"}:
            xdo("mousemove", str(event["x"]), str(event["y"]), "getmouselocation", "--shell")
        if kind == "click":
            xdo("click", str(event["button"]))
        elif kind == "drag":
            xdo("mousedown", str(event["button"]))
            xdo("mousemove", str(event["to_x"]), str(event["to_y"]), "getmouselocation", "--shell")
            xdo("mouseup", str(event["button"]))
        elif kind == "scroll" and event["ticks"]:
            xdo(
                "click",
                "--repeat",
                str(abs(event["ticks"])),
                "--delay",
                "20",
                "5" if event["ticks"] > 0 else "4",
            )
        elif kind == "key":
            xdo("key", "--clearmodifiers", "+".join(event["keys"]))
        elif kind == "text":
            paste(display, event["text"], env, xdo, persistent=persistent)
    finally:
        release(display)


class HumanInput:
    def __init__(self, display):
        if not re.fullmatch(r":[0-9]{1,5}(?:\.[0-9]{1,3})?", display):
            raise ValueError("An owned private display is required.")
        self.display = display
        self._clipboard = None

    def close(self):
        child, self._clipboard = self._clipboard, None
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=2)
            if child.stdout:
                child.stdout.close()

    def __call__(self, event):
        # Keep the selection owner alive for applications that request it more
        # than once. Its contents are removed on replacement or relinquishment.
        if event.get("kind") in {"text", "release"}:
            self.close()
        if event.get("kind") == "text":
            child = subprocess.Popen(
                [sys.executable, "-I", str(Path(__file__)), self.display, "--paste"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={"PATH": os.defpath},
                start_new_session=True,
            )
            self._clipboard = child
            try:
                child.stdin.write(json.dumps(event, ensure_ascii=False).encode() + b"\n")
                child.stdin.close()
                if (
                    not select.select([child.stdout], [], [], 5)[0]
                    or child.stdout.readline(32) != b"consumed\n"
                ):
                    raise ValueError("Human paste was not confirmed.")
                return {"dispatched": True}
            except BaseException:
                self.close()
                self({"kind": "release"})
                raise
        # Validate inside the credential-free helper; no input is echoed on error.
        child = subprocess.Popen(
            [sys.executable, "-I", str(Path(__file__)), self.display],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": os.defpath},
            start_new_session=True,
        )
        try:
            child.communicate(json.dumps(event, ensure_ascii=False).encode(), timeout=8)
            if child.returncode:
                raise ValueError("Human input was not confirmed.")
        except BaseException:
            # Revoke the whole owned helper group before trying cleanup. A late
            # xdotool child must not inject input after ownership is returned.
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            child.wait(timeout=2)
            subprocess.run(
                [sys.executable, "-I", str(Path(__file__)), self.display, "--release"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={"PATH": os.defpath},
                timeout=3,
                check=True,
            )
            raise
        return {"dispatched": True}


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[2] == "--release":
            release(sys.argv[1])
        elif len(sys.argv) == 3 and sys.argv[2] == "--paste":
            dispatch(sys.argv[1], json.loads(sys.stdin.buffer.readline(65537)), persistent=True)
        elif len(sys.argv) == 2:
            dispatch(sys.argv[1], json.loads(sys.stdin.buffer.read(65537)))
        else:
            raise ValueError("Invalid invocation.")
    except Exception:
        raise SystemExit(1) from None
