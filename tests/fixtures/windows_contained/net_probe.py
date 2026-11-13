"""Runs inside the sandbox: every network and pipe route a command can try.

Prints one JSON object. Each entry is ``["OK", detail]`` when the attempt went
through and ``["ERR", detail]`` when the system refused it.
"""

import ctypes
import json
import socket
import sys
from ctypes import wintypes as w

spec = json.load(open(sys.argv[1], encoding="utf-8"))
out = {}


def attempt(name, action):
    try:
        out[name] = ["OK", action()]
    except BaseException as error:  # noqa: BLE001 - the refusal is the result
        code = getattr(error, "winerror", None) or getattr(error, "errno", None)
        out[name] = ["ERR", f"{type(error).__name__} {code}"]


def tcp(address, port):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as client:
        client.settimeout(4)
        client.connect((address, port))
        client.send(b"sandbox")
    return "connected"


def udp(address, port):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as client:
        client.settimeout(2)
        return f"sendto returned {client.sendto(b'sandbox', (address, port))}"


for name, (address, port) in spec["endpoints"].items():
    send = tcp if name.startswith("tcp") else udp
    attempt(f"host_{name}", lambda send=send, address=address, port=port: send(address, port))

# TEST-NET-1 is routed like any public address and answered by nobody.
attempt("external_tcp", lambda: tcp("192.0.2.1", 443))
attempt("dns_public_name", lambda: socket.getaddrinfo("example.com", 443)[0][4][0])


def raw_icmp():
    with socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP) as client:
        client.settimeout(2)
        client.sendto(b"\x08\x00\xe5\xca\x12\x34\x00\x01", ("127.0.0.1", 0))
        try:
            client.recvfrom(200)
        except TimeoutError:
            return "no reply"
        return "REPLY"


attempt("raw_icmp_loopback", raw_icmp)


def listen(address, suffix):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as server:
        server.bind((address, 0))
        server.listen(1)
        server.settimeout(4)
        with open(spec["listen_file"] + suffix, "w", encoding="ascii") as handle:
            handle.write(str(server.getsockname()[1]))
        try:
            server.accept()
        except TimeoutError:
            return "no connection arrived"
        return "ACCEPTED"


attempt("listen_loopback4", lambda: listen("127.0.0.1", "4"))
attempt("listen_loopback6", lambda: listen("::1", "6"))


def own_loopback():
    with socket.socket() as server, socket.socket() as client:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client.settimeout(3)
        client.connect(server.getsockname())
    return "connected"


attempt("own_loopback", own_loopback)

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.CreateFileW.restype = w.HANDLE
kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
kernel.WriteFile.argtypes = [w.HANDLE, w.LPCVOID, w.DWORD, ctypes.POINTER(w.DWORD), w.LPVOID]
kernel.CloseHandle.argtypes = [w.HANDLE]


def open_pipe(path):
    handle = kernel.CreateFileW(path, 0xC0000000, 0, None, 3, 0, None)
    if handle == w.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        written = w.DWORD()
        if not kernel.WriteFile(handle, b"sandbox", 7, ctypes.byref(written), None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)
    return "wrote to the host pipe"


for kind, path in spec["pipes"].items():
    attempt(f"pipe_{kind}", lambda path=path: open_pipe(path))

port = spec["endpoints"]["tcp4"][1]
for name, path in (
    ("unc_loopback_share", "\\\\127.0.0.1\\C$\\Windows\\win.ini"),
    ("unc_webdav_loopback", f"\\\\127.0.0.1@{port}\\DavWWWRoot\\x.txt"),
):
    attempt(name, lambda path=path: open(path, "rb").read(8) and "READ")

print("PROBE_JSON " + json.dumps(out))
