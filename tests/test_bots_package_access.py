"""Package installs from an owl computer's shell (package_access.py, desktop_worker.py).

The guard is the real proxy on a real Unix socket; DNS and dialing are injected so no test
reaches the internet. Its TLS checks read real ClientHellos made by Python's ssl module. The
desktop worker's part runs its real relay, installer settings and a real pip, against a local
HTTPS index reached only through the guard.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from alysis_code.bots import desktop_worker
from alysis_code.bots.package_access import (
    CONFIG,
    INDEXES,
    PACKAGES_IN_COMPUTER,
    SOCKET,
    PackageGuard,
    PackageIndexError,
    package_indexes,
    start_package_guard,
)

PUBLIC = "151.101.0.223"


class RecordingDialer:
    """Each dial is a socket pair: the guard gets one end, the test plays the server."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.servers: list[socket.socket] = []
        self.called = threading.Event()

    def __call__(self, address: str, port: int, timeout: float) -> socket.socket:
        local, server = socket.socketpair()
        server.settimeout(2.0)
        self.calls.append((address, port))
        self.servers.append(server)
        self.called.set()
        return local


@pytest.fixture
def short_dir():
    # Unix socket paths are short; pytest's own temporary paths can exceed the limit.
    directory = Path(tempfile.mkdtemp(prefix="pkg-", dir="/tmp"))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def guard(short_dir):
    resolutions: list[str] = []
    dialer = RecordingDialer()
    guard = PackageGuard(
        short_dir / "packages",
        package_indexes(),
        resolver=lambda host, port: resolutions.append(host) or (PUBLIC,),
        dialer=dialer,
    ).start()
    yield guard, dialer, resolutions
    guard.close()


def client_hello(server_name: str | None) -> bytes:
    """A real TLS ClientHello, as an installer's TLS library sends it."""
    context = ssl.create_default_context()
    if server_name is None:
        context.check_hostname = False
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = context.wrap_bio(incoming, outgoing, server_hostname=server_name)
    with pytest.raises(ssl.SSLWantReadError):
        tls.do_handshake()
    return outgoing.read()


def connect(guard: PackageGuard) -> socket.socket:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2.0)
    client.connect(str(guard.directory / SOCKET))
    return client


def head(connection: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def tunnel(guard: PackageGuard, host: str, port: int = 443) -> tuple[socket.socket, bytes]:
    client = connect(guard)
    client.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    return client, head(client)


def closed(connection: socket.socket) -> bool:
    try:
        return connection.recv(1) == b""
    except ConnectionResetError:
        return True


# ---------------------------------------------------------------------------------------------
# The declared index list (configuration)


def test_the_shipped_index_list_declares_exact_https_package_hosts():
    indexes = {index.ecosystem: index for index in package_indexes()}
    # Python only: the npm registry takes publishes on the host it serves installs from.
    assert set(indexes) == {"python"}
    for index in indexes.values():
        assert index.index_url.startswith("https://")
        assert all("*" not in host and ":" not in host for host in index.hosts)
    # Each index names the host its installer downloads files from, not only the index.
    assert len(indexes["python"].hosts) == 2
    assert json.loads(CONFIG.read_text())["schema"] == 1


@pytest.mark.parametrize(
    "index",
    [
        {"index_url": "https://pypi.org/simple/", "hosts": ["151.101.0.223"]},
        {"index_url": "https://pypi.org/simple/", "hosts": ["pypi.org", "*.pythonhosted.org"]},
        {"index_url": "https://pypi.org/simple/", "hosts": ["**.pypi.org"]},
        {"index_url": "https://pypi.org/simple/", "hosts": ["pypi.org:8443"]},
        {"index_url": "https://pypi.org/simple/", "hosts": ["https://pypi.org/"]},
        {"index_url": "https://pypi.org/simple/", "hosts": ["PyPI.org"]},
        {"index_url": "https://pypi.org/simple/", "hosts": ["pypi.org", "pypi.org"]},
        {"index_url": "https://pypi.org/simple/", "hosts": []},
        {"index_url": "http://pypi.org/simple/", "hosts": ["pypi.org"]},
        {"index_url": "https://mirror.example.net/simple/", "hosts": ["pypi.org"]},
        {"index_url": "https://pypi.org:8443/simple/", "hosts": ["pypi.org"]},
        {"index_url": "https://user:secret@pypi.org/simple/", "hosts": ["pypi.org"]},
        {"index_url": "https://pypi.org/simple/", "hosts": ["pypi.org"], "extra": True},
    ],
)
def test_an_unsafe_index_entry_is_refused(tmp_path, index):
    path = tmp_path / "indexes.json"
    path.write_text(json.dumps({"schema": 1, "indexes": {"python": index}}))
    with pytest.raises(PackageIndexError):
        package_indexes(path)
    # The computer then offers no package index at all: its shell stays offline.
    assert start_package_guard(tmp_path / "packages", config=path) is None
    assert not (tmp_path / "packages").exists()


def test_an_empty_or_unreadable_index_list_starts_no_guard(short_dir):
    empty = short_dir / "empty.json"
    empty.write_text(json.dumps({"schema": 1, "indexes": {}}))
    assert start_package_guard(short_dir / "a", config=empty) is None
    assert start_package_guard(short_dir / "b", config=short_dir / "missing.json") is None
    assert not (short_dir / "a").exists() and not (short_dir / "b").exists()


def test_reachable_hosts_come_from_the_configuration_not_the_code(short_dir):
    path = short_dir / "indexes.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "indexes": {
                    "python": {
                        "index_url": "https://packages.example.org/simple/",
                        "hosts": ["packages.example.org", "files.example.org"],
                    }
                },
            }
        )
    )
    dialer = RecordingDialer()
    guard = PackageGuard(
        short_dir / "packages", package_indexes(path), resolver=lambda *_: (PUBLIC,), dialer=dialer
    ).start()
    try:
        refused, response = tunnel(guard, "pypi.org")
        assert response.startswith(b"HTTP/1.1 403") and not dialer.calls
        refused.close()
        client, response = tunnel(guard, "files.example.org")
        assert response.startswith(b"HTTP/1.1 200")
        client.sendall(client_hello("files.example.org"))
        assert dialer.called.wait(2.0)
        client.close()
        declared = json.loads((guard.directory / INDEXES).read_text())
        assert declared["indexes"]["python"]["hosts"] == [
            "packages.example.org",
            "files.example.org",
        ]
    finally:
        guard.close()


# ---------------------------------------------------------------------------------------------
# The guard


def test_guard_admits_a_declared_index_host_and_relays_its_tls_unchanged(guard):
    guard, dialer, resolutions = guard
    mode = os.stat(guard.directory / SOCKET).st_mode
    assert stat.S_ISSOCK(mode) and stat.S_IMODE(mode) == 0o600
    assert stat.S_IMODE(os.stat(guard.directory).st_mode) == 0o700
    hello = client_hello("files.pythonhosted.org")
    client, response = tunnel(guard, "files.pythonhosted.org")
    assert response == b"HTTP/1.1 200 Connection Established\r\n\r\n"
    # Sent in pieces, as TCP may deliver it.
    client.sendall(hello[:7])
    time.sleep(0.05)
    client.sendall(hello[7:])
    assert dialer.called.wait(2.0)
    server = dialer.servers[0]
    received = b""
    while len(received) < len(hello):
        received += server.recv(65536)
    assert received == hello
    server.sendall(b"server bytes")
    assert client.recv(64) == b"server bytes"
    client.sendall(b"more client bytes")
    assert server.recv(64) == b"more client bytes"
    client.close()
    # The guard resolved the name itself and dialed only the numeric address, on port 443.
    assert resolutions == ["files.pythonhosted.org"] and dialer.calls == [(PUBLIC, 443)]


@pytest.mark.parametrize(
    "request_bytes",
    [
        b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n",
        # IP literals, even ones the index itself uses.
        b"CONNECT 151.101.0.223:443 HTTP/1.1\r\nHost: 151.101.0.223:443\r\n\r\n",
        b"CONNECT [2a04:4e42::223]:443 HTTP/1.1\r\nHost: [2a04:4e42::223]:443\r\n\r\n",
        b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n",
        # Lookalikes: longer, prefixed, a subdomain, and a homograph (Cyrillic letters).
        b"CONNECT pypi.org.example.com:443 HTTP/1.1\r\nHost: pypi.org.example.com:443\r\n\r\n",
        b"CONNECT evilpypi.org:443 HTTP/1.1\r\nHost: evilpypi.org:443\r\n\r\n",
        b"CONNECT upload.pypi.org:443 HTTP/1.1\r\nHost: upload.pypi.org:443\r\n\r\n",
        "CONNECT {0}:443 HTTP/1.1\r\nHost: {0}:443\r\n\r\n".format(
            "руpi.org".encode("idna").decode()
        ).encode(),
        # A declared host on another port, or without TLS.
        b"CONNECT pypi.org:80 HTTP/1.1\r\nHost: pypi.org:80\r\n\r\n",
        b"CONNECT pypi.org:8443 HTTP/1.1\r\nHost: pypi.org:8443\r\n\r\n",
        b"GET http://pypi.org/simple/ HTTP/1.1\r\nHost: pypi.org\r\n\r\n",
    ],
)
def test_guard_refuses_every_other_destination_before_dns_or_dial(guard, request_bytes):
    guard, dialer, resolutions = guard
    with connect(guard) as client:
        client.sendall(request_bytes)
        assert head(client).startswith(b"HTTP/1.1 403")
    assert resolutions == [] and dialer.calls == []


def test_a_declared_host_that_resolves_to_a_private_address_is_refused(short_dir):
    dialer = RecordingDialer()
    guard = PackageGuard(
        short_dir / "packages",
        package_indexes(),
        resolver=lambda *_: ("10.0.0.7",),
        dialer=dialer,
    ).start()
    try:
        client, response = tunnel(guard, "pypi.org")
        assert response.startswith(b"HTTP/1.1 403") and not dialer.calls
        client.close()
    finally:
        guard.close()


@pytest.mark.parametrize(
    "first_bytes",
    [
        pytest.param(lambda: client_hello("example.com"), id="tls-names-another-host"),
        pytest.param(lambda: client_hello("pypi.org.example.com"), id="tls-names-a-lookalike"),
        pytest.param(lambda: client_hello(None), id="tls-names-no-host"),
        pytest.param(lambda: b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n", id="not-tls"),
    ],
)
def test_a_tunnel_whose_tls_asks_for_another_host_never_reaches_the_server(guard, first_bytes):
    guard, dialer, _ = guard
    client, response = tunnel(guard, "pypi.org")
    assert response.startswith(b"HTTP/1.1 200")
    client.sendall(first_bytes())
    assert closed(client)
    client.close()
    for server in dialer.servers:
        assert closed(server)  # The server end saw no byte at all.


def test_a_redirect_to_an_unlisted_host_cannot_be_followed(guard):
    guard, dialer, resolutions = guard
    client, response = tunnel(guard, "pypi.org")
    assert response.startswith(b"HTTP/1.1 200")
    client.sendall(client_hello("pypi.org"))
    assert dialer.called.wait(2.0)
    # The index answers (inside TLS, opaque to the guard) with a redirect elsewhere.
    dialer.servers[0].sendall(
        b"HTTP/1.1 302 Found\r\nLocation: https://downloads.example.net/pkg.whl\r\n\r\n"
    )
    assert b"downloads.example.net" in client.recv(4096)
    client.close()
    # Following it needs a new tunnel, to a host that is not declared.
    follow, response = tunnel(guard, "downloads.example.net")
    assert response.startswith(b"HTTP/1.1 403")
    follow.close()
    assert resolutions == ["pypi.org"] and len(dialer.calls) == 1


def test_closing_the_guard_removes_its_socket(short_dir):
    guard = PackageGuard(short_dir / "packages", package_indexes()).start()
    assert guard.healthy
    guard.close()
    assert not guard.healthy and not (guard.directory / SOCKET).exists()


def test_the_guard_needs_a_new_private_directory(short_dir):
    (short_dir / "packages").mkdir()
    assert start_package_guard(short_dir / "packages") is None


# ---------------------------------------------------------------------------------------------
# Inside the computer: the relay, the installers' settings and the declaration


def unix_echo_server(path: Path):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(4)

    def serve():
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            with connection:
                while data := connection.recv(65536):
                    connection.sendall(b"guard:" + data)

    threading.Thread(target=serve, daemon=True).start()
    return listener


def declare(root: Path, indexes: dict) -> socket.socket:
    root.mkdir(mode=0o700)
    (root / INDEXES).write_text(json.dumps({"schema": 1, "indexes": indexes}))
    return unix_echo_server(root / SOCKET)


SHIPPED = {index.ecosystem: index.declaration() for index in package_indexes()}
# Both installers, to pin how each one is wired; only Python ships (package_indexes.json).
BOTH = {
    **SHIPPED,
    "node": {"index_url": "https://registry.npmjs.org/", "hosts": ["registry.npmjs.org"]},
}


def test_the_desktop_worker_binds_the_same_place_the_bridge_mounts():
    assert desktop_worker.PACKAGES == Path(PACKAGES_IN_COMPUTER)


def test_without_a_guard_the_shell_declares_nothing_and_its_settings_are_unchanged(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp/home"}
    assert desktop_worker.package_installs(env, root=tmp_path / "absent", home=tmp_path) == (
        (),
        None,
    )
    assert env == {"PATH": "/usr/bin:/bin", "HOME": "/tmp/home"}
    # An index list without the guard's socket is not a guard either.
    (tmp_path / "half").mkdir()
    (tmp_path / "half" / INDEXES).write_text(json.dumps({"schema": 1, "indexes": SHIPPED}))
    assert desktop_worker.package_installs(env, root=tmp_path / "half", home=tmp_path)[0] == ()
    computer = desktop_worker.Computer.__new__(desktop_worker.Computer)
    computer.shared_display = ":7"
    assert computer.network_receipt() == {"network": "browser-mediated"}


def test_the_relay_carries_a_loopback_connection_to_the_guard_socket(short_dir):
    listener = unix_echo_server(short_dir / "guard.sock")
    relay = desktop_worker.PackageRelay(short_dir / "guard.sock")
    try:
        assert relay.url.startswith("http://127.0.0.1:")
        port = int(relay.url.rsplit(":", 1)[1])
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            client.sendall(b"CONNECT pypi.org:443")
            assert client.recv(64) == b"guard:CONNECT pypi.org:443"
    finally:
        relay.close()
        listener.close()


@pytest.fixture
def computer_python_has_pip(monkeypatch):
    """An owl computer's Python has pip (its image installs it); a test interpreter may not.
    uv environments, for one, ship without it."""
    import importlib.machinery
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a: (
            importlib.machinery.ModuleSpec(name, None) if name == "pip" else real(name, *a)
        ),
    )


def test_declared_installers_point_at_the_relay_and_install_into_the_private_home(
    short_dir, monkeypatch, computer_python_has_pip
):
    listener = declare(short_dir / "packages", BOTH)
    tools = short_dir / "tools"
    tools.mkdir()
    (tools / "npm").write_text("#!/bin/sh\n")
    (tools / "npm").chmod(0o755)
    env = {"PATH": f"{tools}:/usr/bin:/bin", "HOME": str(short_dir)}
    home = short_dir / "home"
    declared, relay = desktop_worker.package_installs(env, root=short_dir / "packages", home=home)
    try:
        assert declared == ("node", "python")
        overlay = home / "python" / "bin"
        assert env["PATH"].split(os.pathsep)[:2] == [str(overlay), str(home / "node" / "bin")]
        assert env["PIP_PROXY"] == relay.url == env["npm_config_https_proxy"]
        assert env["npm_config_proxy"] == relay.url
        assert env["PIP_INDEX_URL"] == SHIPPED["python"]["index_url"]
        assert env["npm_config_registry"] == BOTH["node"]["index_url"]
        assert env["npm_config_prefix"] == str(home / "node")
        assert (overlay / "pip").is_file() and os.access(overlay / "pip", os.X_OK)
        # The private environment still sees every package this computer's Python has.
        probe = subprocess.run(
            [str(overlay / "python"), "-c", "import sys, pytest; print(sys.prefix)"],
            capture_output=True,
            text=True,
            env={"PATH": env["PATH"]},
            timeout=30,
        )
        assert probe.returncode == 0, probe.stderr
        assert probe.stdout.strip() == str(home / "python")
        computer = desktop_worker.Computer.__new__(desktop_worker.Computer)
        computer.shared_display = None
        computer.package_indexes = declared
        assert computer.network_receipt() == {
            "network": "offline",
            "package_indexes": ["node", "python"],
        }
    finally:
        relay.close()
        listener.close()


def test_an_ecosystem_is_declared_only_where_its_installer_exists(
    short_dir, monkeypatch, computer_python_has_pip
):
    import importlib.util

    listener = declare(short_dir / "packages", SHIPPED)
    try:
        env = {"PATH": "/nonexistent", "HOME": str(short_dir)}
        declared, relay = desktop_worker.package_installs(
            env, root=short_dir / "packages", home=short_dir / "home"
        )
        assert declared == ("python",)  # No npm here.
        relay.close()
        assert "npm_config_registry" not in env
        real = importlib.util.find_spec
        monkeypatch.setattr(
            importlib.util, "find_spec", lambda name, *a: None if name == "pip" else real(name, *a)
        )
        env = {"PATH": "/nonexistent", "HOME": str(short_dir)}
        assert desktop_worker.package_installs(
            env, root=short_dir / "packages", home=short_dir / "other"
        ) == ((), None)
        assert env == {"PATH": "/nonexistent", "HOME": str(short_dir)}
        # An ecosystem the computer has no installer for is never declared.
        unknown = {"other": {"index_url": "https://x.example/", "hosts": ["x.example"]}}
        (short_dir / "packages" / INDEXES).write_text(json.dumps({"indexes": unknown}))
        assert desktop_worker.package_installs(
            env, root=short_dir / "packages", home=short_dir / "third"
        ) == ((), None)
    finally:
        listener.close()


def test_command_receipts_carry_the_declaration(short_dir, monkeypatch):
    monkeypatch.setattr(desktop_worker, "application_argv", lambda argv: argv)
    popen = subprocess.Popen
    # The computer's /workspace is this test's own directory.
    monkeypatch.setattr(
        desktop_worker.subprocess,
        "Popen",
        lambda argv, **kwargs: popen(argv, **{**kwargs, "cwd": short_dir}),
    )
    computer = desktop_worker.Computer.__new__(desktop_worker.Computer)
    computer.env = {"PATH": "/usr/bin:/bin"}
    computer.shared_display = ":3"
    computer.jobs = {}
    offline = computer.run({"command": "echo ready"})
    assert offline["output"] == "ready\n" and "package_indexes" not in offline
    computer.package_indexes = ("python",)
    receipt = computer.run({"command": "echo ready"})
    assert receipt["network"] == "browser-mediated"
    assert receipt["package_indexes"] == ["python"]
    launched = computer.run({"command": "true"}, background=True)
    assert launched["package_indexes"] == ["python"]


# ---------------------------------------------------------------------------------------------
# End to end: real pip, through the relay and the guard, from a local HTTPS index


def make_wheel(directory: Path, name: str, version: str) -> Path:
    import base64
    import hashlib
    import zipfile

    module = name.replace("-", "_")
    dist = f"{module}-{version}.dist-info"
    files = {
        f"{module}/__init__.py": f"VERSION = {version!r}\n".encode(),
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode(),
        f"{dist}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = (
        "".join(
            f"{path},sha256={base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()},{len(data)}\n"
            for path, data in files.items()
        )
        + f"{dist}/RECORD,,\n"
    )
    files[f"{dist}/RECORD"] = record.encode()
    path = directory / f"{module}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for item, data in files.items():
            archive.writestr(item, data)
    return path


def https_index(directory: Path, wheels: dict[str, tuple[str, Path]]):
    """A local HTTPS server answering as the declared index and file hosts."""
    import datetime
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "package test")])
    now = datetime.datetime.now(datetime.UTC)
    hosts = ["pypi.org", "files.pythonhosted.org", "downloads.example.net"]
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in hosts]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    (directory / "cert.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (directory / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            for project, (url, wheel) in wheels.items():
                if self.path.rstrip("/") == f"/simple/{project}":
                    body, kind = f'<a href="{url}">{wheel.name}</a>'.encode(), "text/html"
                    break
                if self.path == "/files/" + wheel.name:
                    body, kind = wheel.read_bytes(), "application/octet-stream"
                    break
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(directory / "cert.pem", directory / "key.pem")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, directory / "cert.pem"


def test_pip_installs_from_the_declared_index_through_the_guard_and_nothing_else(short_dir):
    pytest.importorskip("pip")
    wheels_dir = short_dir / "wheels"
    wheels_dir.mkdir()
    allowed = make_wheel(wheels_dir, "alysis-probe", "1.0")
    elsewhere = make_wheel(wheels_dir, "alysis-elsewhere", "1.0")
    server, certificate = https_index(
        short_dir,
        {
            "alysis-probe": (f"https://files.pythonhosted.org/files/{allowed.name}", allowed),
            # The index links this file to a host that is not declared.
            "alysis-elsewhere": (
                f"https://downloads.example.net/files/{elsewhere.name}",
                elsewhere,
            ),
        },
    )
    port = server.server_address[1]
    dialed: list[tuple[str, int]] = []

    def dialer(address, _port, timeout):
        dialed.append((address, _port))
        return socket.create_connection(("127.0.0.1", port), timeout=timeout)

    guard = PackageGuard(
        short_dir / "packages",
        package_indexes(),
        resolver=lambda host, _port: (PUBLIC,),
        dialer=dialer,
    ).start()
    home = short_dir / "home"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8"}
    declared, relay = desktop_worker.package_installs(
        env, root=guard.directory, home=home, installers={"python": desktop_worker.python_installer}
    )
    try:
        assert declared == ("python",)
        # Only the test's certificate authority is added; everything else is the worker's.
        env["PIP_CERT"] = str(certificate)

        def pip(*args):
            return subprocess.run(
                ["pip", "install", "--retries", "0", "--timeout", "5", *args],
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
            )

        installed = pip("alysis-probe")
        assert installed.returncode == 0, installed.stdout + installed.stderr
        probe = subprocess.run(
            ["python3", "-c", "import alysis_probe, sys; print(alysis_probe.VERSION, sys.prefix)"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert probe.stdout.split() == ["1.0", str(home / "python")]
        # This test's own Python is untouched: the package went into the private home only.
        untouched = subprocess.run(
            [sys.executable, "-c", "import alysis_probe"], capture_output=True, timeout=30
        )
        assert untouched.returncode != 0
        refused = pip("alysis-elsewhere")
        assert refused.returncode != 0
        assert "403" in refused.stdout + refused.stderr
        assert set(dialed) == {(PUBLIC, 443)}
    finally:
        relay.close()
        guard.close()
        server.shutdown()


# ---------------------------------------------------------------------------------------------
# The bridge: the guard starts with the computer's desktop and is bound into its jail


class FakeProcess:
    pid = 0
    stdin = stdout = None

    def poll(self):
        return 0


def test_the_jail_binds_only_a_confirmed_guard_read_only(short_dir, monkeypatch):
    from alysis_code.bots import desktop_linux

    launched: list[list[str]] = []
    monkeypatch.setattr(
        desktop_linux.subprocess,
        "Popen",
        lambda argv, **kwargs: launched.append(argv) or FakeProcess(),
    )
    monkeypatch.setattr(desktop_linux.OfflineLinuxDesktop, "_request", lambda *a, **k: {})
    workspace = short_dir / "workspace"
    workspace.mkdir()
    guard = PackageGuard(short_dir / "packages", package_indexes()).start()
    try:
        desktop_linux.OfflineLinuxDesktop("owner", workspace, packages=guard.directory).close()
        argv = launched.pop()
        at = argv.index(PACKAGES_IN_COMPUTER)
        assert argv[at - 2 : at] == ["--ro-bind", str(guard.directory)]
        assert argv.count(PACKAGES_IN_COMPUTER) == 1 and "--unshare-all" in argv
        # Standalone desktops get no guard, as before.
        desktop_linux.OfflineLinuxDesktop("owner", workspace).close()
        assert PACKAGES_IN_COMPUTER not in launched.pop()
        # Anything the bridge did not make itself is refused before the jail starts.
        link = short_dir / "link"
        link.symlink_to(guard.directory)
        loose = short_dir / "loose"
        loose.mkdir(mode=0o755)
        (loose / INDEXES).write_text("{}")
        empty = short_dir / "empty"
        empty.mkdir(mode=0o700)
        (empty / INDEXES).write_text("{}")
        for candidate in (link, loose, empty, str(guard.directory)):
            with pytest.raises(ValueError, match="guard"):
                desktop_linux.OfflineLinuxDesktop("owner", workspace, packages=candidate)
        assert not launched
    finally:
        guard.close()


def test_the_bridge_starts_the_guard_with_the_desktop_and_closes_it_with_the_session(
    short_dir, monkeypatch
):
    from uuid import uuid4

    from alysis_code.bots import cloud_desktop_worker as bridge

    root = short_dir / "session"
    monkeypatch.setattr(bridge, "root_for", lambda session: root)
    seen = {}

    class Stop(BaseException):
        """Ends the serve loop, as a fatal error would, so the test can observe cleanup."""

    class Desktop:
        def __init__(self, session, workspace, **kwargs):
            seen.update(kwargs)
            packages = kwargs.get("packages")
            seen["socket_live"] = packages is not None and (packages / SOCKET).is_socket()

        def rfb(self, args):
            return {}

        def capture(self):
            return b""

        def call(self, session, operation, args):
            if operation == "stop":
                raise Stop
            return {"ran": operation}

        def alive(self):
            return True

        def close(self):
            seen["desktop_closed"] = True

    monkeypatch.setattr(bridge, "OfflineLinuxDesktop", Desktop)
    session = uuid4().hex

    def serve():
        try:
            bridge.serve(session)
        except Stop:
            seen["stopped"] = True

    serving = threading.Thread(target=serve, daemon=True)
    serving.start()
    deadline = time.monotonic() + 5
    while not (root / "control.sock").exists() and time.monotonic() < deadline:
        time.sleep(0.02)

    def send(operation):
        request = {"id": uuid4().hex, "session": session, "operation": operation, "arguments": {}}
        return bridge.exchange(session, request, timeout=5)

    assert send("run")["result"] == {"ran": "run"}
    assert seen["packages"] == root / "packages" and seen["socket_live"]
    with pytest.raises(Exception, match="closed"):
        send("stop")
    serving.join(timeout=5)
    assert not serving.is_alive() and seen["stopped"] and seen["desktop_closed"]
    assert not (root / "packages" / SOCKET).exists()


def test_the_owl_is_told_installs_work_only_where_its_computer_declares_them():
    from alysis_code.bots.desktop import DEFINITIONS
    from alysis_code.bots.execution import PROMPT

    run = DEFINITIONS["computer_run"][1]
    assert "Package installs work only when a receipt lists package_indexes" in run
    assert "Without it, report a missing package instead of installing." in run
    assert "package_indexes" in DEFINITIONS["computer_launch"][1]
    assert "No package installation from the internet" not in PROMPT
    assert "only when a computer receipt lists package_indexes" in PROMPT
