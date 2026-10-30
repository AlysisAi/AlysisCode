"""Native boundary probes. Host targets contain only disposable synthetic data."""

from __future__ import annotations

import ctypes
import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid

import pytest

from alysis_code.sandbox_runner import SeatbeltShellRunner

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="real macOS Seatbelt required")


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    root = (tmp_path / "project").resolve()
    root.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", "[]")
    monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", "[]")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    return root


def probe(root, source):
    result = SeatbeltShellRunner().run(
        root=root,
        cwd=root,
        cmd=shlex.join([sys.executable, "-I", "-B", "-c", source]),
        timeout_s=10,
    )
    assert result.returncode == 0, (result.returncode, result.stderr)
    return json.loads(result.stdout)


@pytest.mark.parametrize("flavor", [38, 49])  # KERN_PROCARGS / KERN_PROCARGS2
def test_other_process_environment_is_not_readable(boundary, flavor):
    marker = "synthetic-" + uuid.uuid4().hex
    # Never inspect the test runner or engine: their environment may be real.
    helper = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", "import time; time.sleep(20)"],
        env={"SYNTHETIC_BOUNDARY_CANARY": marker},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        result = probe(
            boundary,
            f"""import ctypes,json
lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
mib=(ctypes.c_int*3)(1,{flavor},{helper.pid})
buffer=ctypes.create_string_buffer(262144); size=ctypes.c_size_t(len(buffer))
rc=lib.sysctl(mib,3,buffer,ctypes.byref(size),None,0)
print(json.dumps({{'rc':rc,'errno':ctypes.get_errno(),'canary_found':{marker.encode()!r} in buffer.raw}}))
""",
        )
        assert result["rc"] != 0 and not result["canary_found"], result
    finally:
        helper.terminate()
        helper.wait(timeout=5)


def test_host_posix_shared_memory_is_not_readable(boundary):
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    name = ("/alysis-" + uuid.uuid4().hex[:20]).encode()
    fd = lib.shm_open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    assert fd >= 0, ctypes.get_errno()
    try:
        os.ftruncate(fd, 64)
        result = probe(
            boundary,
            f"""import ctypes,json,os
lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
fd=lib.shm_open({name!r},os.O_RDONLY,0)
print(json.dumps({{'opened':fd>=0,'errno':ctypes.get_errno()}}))
if fd>=0: os.close(fd)
""",
        )
        assert not result["opened"], result
    finally:
        os.close(fd)
        assert lib.shm_unlink(name) == 0


def test_host_sysv_shared_memory_is_not_attachable(boundary):
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    identifier = lib.shmget(0, 4096, 0o1000 | 0o600)  # IPC_PRIVATE, IPC_CREAT
    assert identifier >= 0, ctypes.get_errno()
    try:
        result = probe(
            boundary,
            f"""import ctypes,json
lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
lib.shmat.restype=ctypes.c_void_p
address=lib.shmat({identifier},None,0)
attached=address!=ctypes.c_void_p(-1).value
if attached: lib.shmdt(ctypes.c_void_p(address))
print(json.dumps({{'attached':attached}}))
""",
        )
        assert not result["attached"], result
    finally:
        assert lib.shmctl(identifier, 0, None) == 0  # IPC_RMID


def test_host_named_semaphore_cannot_be_opened_or_unlinked(boundary):
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    lib.sem_open.restype = ctypes.c_void_p
    # Darwin arm64 passes variadic arguments differently. Declare the fixed
    # arguments so the probe exercises Seatbelt, not an invalid native call.
    lib.sem_open.argtypes = [ctypes.c_char_p, ctypes.c_int]
    name = ("/alysis-" + uuid.uuid4().hex[:20]).encode()
    handle = lib.sem_open(name, os.O_CREAT | os.O_EXCL, 0o600, 1)
    assert handle != ctypes.c_void_p(-1).value, ctypes.get_errno()
    try:
        result = probe(
            boundary,
            f"""import ctypes,json
lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
lib.sem_open.restype=ctypes.c_void_p
lib.sem_open.argtypes=[ctypes.c_char_p,ctypes.c_int]
handle=lib.sem_open({name!r},0)
opened=handle!=ctypes.c_void_p(-1).value
if opened: lib.sem_close(ctypes.c_void_p(handle))
print(json.dumps({{'opened':opened,'unlinked':lib.sem_unlink({name!r})==0}}))
""",
        )
        assert result == {"opened": False, "unlinked": False}
    finally:
        assert lib.sem_close(ctypes.c_void_p(handle)) == 0
        assert lib.sem_unlink(name) == 0


def test_unapproved_executable_cannot_be_used_as_a_read_bypass(boundary):
    outside = boundary.parent / "executable"
    shutil.copy("/bin/echo", outside)
    result = probe(
        boundary,
        f"""import json,subprocess
try:
 result=subprocess.run([{str(outside)!r},'synthetic'],capture_output=True)
 print(json.dumps({{'executed':result.returncode==0}}))
except PermissionError: print(json.dumps({{'executed':False}}))
""",
    )
    assert not result["executed"], result


@pytest.mark.parametrize(
    "service",
    [
        "com.apple.cfprefsd.xpc.daemon",
        "com.apple.securityd",
        "com.apple.coreservices.launchservicesd",
        "com.apple.pasteboard.1",
    ],
)
def test_host_mach_services_cannot_be_looked_up(boundary, service):
    # Lookup only. Never send requests to keychain, preferences or clipboard.
    result = probe(
        boundary,
        f"""import ctypes,json
lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib')
bootstrap=ctypes.c_uint.in_dll(lib,'bootstrap_port').value
port=ctypes.c_uint()
rc=lib.bootstrap_look_up(bootstrap,{service.encode()!r},ctypes.byref(port))
if rc==0: lib.mach_port_deallocate(lib.mach_task_self(),port.value)
print(json.dumps({{'lookup_succeeded':rc==0,'result':rc}}))
""",
    )
    assert not result["lookup_succeeded"], result


@pytest.mark.parametrize(
    "settings,allowed",
    [
        ({}, False),
        ({"preview_access": "local"}, False),
        ({"network": "on"}, False),
        ({"network": "on", "network_domains": ("example.com",), "preview_access": "local"}, False),
        ({"network": "on", "private_network_endpoints": ("127.0.0.1:54321",)}, False),
        ({"network": "on", "preview_access": "local"}, True),
    ],
)
def test_native_trust_daemon_requires_broad_and_loopback_network_authority(
    boundary, settings, allowed
):
    source = """import ctypes,json
lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib')
bootstrap=ctypes.c_uint.in_dll(lib,'bootstrap_port').value
result={}
for service in ('com.apple.trustd','com.apple.trustd.agent','com.apple.securityd'):
 port=ctypes.c_uint()
 rc=lib.bootstrap_look_up(bootstrap,service.encode(),ctypes.byref(port))
 if rc==0: lib.mach_port_deallocate(lib.mach_task_self(),port.value)
 result[service]=rc==0
print(json.dumps(result))
"""
    run = SeatbeltShellRunner(**settings).run(
        root=boundary,
        cwd=boundary,
        cmd=shlex.join([sys.executable, "-I", "-B", "-c", source]),
        timeout_s=10,
    )
    assert run.returncode == 0, run.stderr
    values = json.loads(run.stdout)
    assert values == {
        "com.apple.trustd": allowed,
        "com.apple.trustd.agent": allowed,
        "com.apple.securityd": False,
    }


def test_new_links_and_renames_cannot_import_unapproved_content(boundary):
    outside = boundary.parent / "outside"
    outside.mkdir()
    canary = outside / "canary"
    canary.write_text("synthetic")
    result = probe(
        boundary,
        f"""import json,os
from pathlib import Path
result={{}}
for name,action in [
 ('hardlink',lambda:os.link({str(canary)!r},'hardlink')),
 ('rename',lambda:os.rename({str(canary)!r},'renamed')),
 ('symlink_read',lambda:Path('symlink').read_text()),
]:
 if name=='symlink_read': os.symlink({str(canary)!r},'symlink')
 try: action(); result[name]='allowed'
 except PermissionError: result[name]='denied'
print(json.dumps(result))
""",
    )
    assert result == dict.fromkeys(("hardlink", "rename", "symlink_read"), "denied")
    assert canary.read_text() == "synthetic"


def test_nested_policy_cannot_weaken_the_filesystem_boundary(boundary):
    outside = boundary.parent / "canary"
    outside.write_text("synthetic")
    result = probe(
        boundary,
        f"""import ctypes,json,os
lib=ctypes.CDLL('/usr/lib/libsandbox.dylib',use_errno=True)
error=ctypes.c_char_p()
rc=lib.sandbox_init(b'(version 1)(allow default)',0,ctypes.byref(error))
if error: lib.sandbox_free_error(error)
print(json.dumps({{'reinitialization':rc,'outside_readable':os.access({str(outside)!r},os.R_OK)}}))
""",
    )
    assert not result["outside_readable"], result


def test_discovered_tool_does_not_grant_neighboring_private_files(boundary, monkeypatch):
    tool_home = boundary.parent / "tool-install"
    (tool_home / "bin").mkdir(parents=True)
    (tool_home / "lib").mkdir()
    tool_name = "εργαλείο-" + uuid.uuid4().hex
    executable = tool_home / "bin" / tool_name
    executable.write_text("#!/bin/sh\nprintf 'tool-ok'\n")
    executable.chmod(0o755)
    private = tool_home / "credentials.toml"
    private.write_text("synthetic")
    library = tool_home / "lib" / "runtime.txt"
    library.write_text("runtime-ok")
    monkeypatch.setenv("PATH", str(executable.parent) + os.pathsep + os.defpath)
    result = probe(
        boundary,
        f"""import json,os,subprocess
from pathlib import Path
print(json.dumps({{'tool':subprocess.check_output([{tool_name!r}],text=True),
 'library':Path({str(library)!r}).read_text(),
 'credential_readable':os.access({str(private)!r},os.R_OK)}}))
""",
    )
    assert result == {"tool": "tool-ok", "library": "runtime-ok", "credential_readable": False}


def test_public_mime_catalog_without_adjacent_server_configuration(boundary):
    result = probe(
        boundary,
        """import json,mimetypes,os
print(json.dumps({'type':mimetypes.guess_type('archive.html')[0],
 'server_config':os.access('/private/etc/apache2/httpd.conf',os.R_OK)}))
""",
    )
    assert result == {"type": "text/html", "server_config": False}


def test_loopback_permission_cannot_borrow_another_commands_proxy(boundary):
    from alysis_code.ide.browser_egress_proxy import BrowserEgressProxy

    with BrowserEgressProxy(require_authentication=True) as proxy:
        source = f"""import socket,json
with socket.create_connection(('127.0.0.1',{proxy.port}),timeout=2) as client:
 client.sendall(b'CONNECT example.com:443 HTTP/1.1\\r\\nHost: example.com\\r\\n\\r\\n')
 print(json.dumps(client.recv(1024).split(b'\\r\\n')[0].decode()))
"""
        result = SeatbeltShellRunner(preview_access="local").run(
            root=boundary,
            cwd=boundary,
            cmd=shlex.join([sys.executable, "-I", "-B", "-c", source]),
            timeout_s=5,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout).startswith("HTTP/1.1 407")


@pytest.mark.parametrize("method", ["spawn", "forkserver", "fork"])
def test_python_process_pool_uses_only_its_sandbox_semaphores(boundary, method):
    (boundary / "pool.py").write_text(
        "from concurrent.futures import ProcessPoolExecutor\n"
        "import multiprocessing,json\n"
        "def square(x): return x*x\n"
        "if __name__=='__main__':\n"
        f" with ProcessPoolExecutor(2,mp_context=multiprocessing.get_context({method!r})) as pool:\n"
        "  print(json.dumps(list(pool.map(square,range(12)))))\n"
    )
    result = SeatbeltShellRunner().run(
        root=boundary,
        cwd=boundary,
        cmd=shlex.join([sys.executable, "-I", "-B", "pool.py"]),
        timeout_s=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [n * n for n in range(12)]
    assert "leaked semaphore" not in result.stderr


def test_other_command_semaphore_cannot_be_opened_or_unlinked(boundary):
    import time

    from alysis_code.background_runner import SeatbeltBackgroundRunner

    name = ("/alysis-" + uuid.uuid4().hex[:20]).encode()
    source = f"""import ctypes,time
from pathlib import Path
lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
lib.sem_open.restype=ctypes.c_void_p
lib.sem_open.argtypes=[ctypes.c_char_p,ctypes.c_int]
handle=lib.sem_open({name!r},{os.O_CREAT | os.O_EXCL},0o600,1)
assert handle!=ctypes.c_void_p(-1).value,ctypes.get_errno()
Path('semaphore-ready').touch()
time.sleep(20)
"""
    spawn = SeatbeltBackgroundRunner().start(
        root=boundary,
        cwd=boundary,
        cmd=shlex.join([sys.executable, "-I", "-B", "-c", source]),
    )
    try:
        deadline = time.monotonic() + 5
        while not (boundary / "semaphore-ready").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (boundary / "semaphore-ready").exists()
        result = probe(
            boundary,
            f"""import ctypes,json
lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
lib.sem_open.restype=ctypes.c_void_p
lib.sem_open.argtypes=[ctypes.c_char_p,ctypes.c_int]
handle=lib.sem_open({name!r},0)
opened=handle!=ctypes.c_void_p(-1).value
if opened:lib.sem_close(ctypes.c_void_p(handle))
print(json.dumps({{'opened':opened,'unlinked':lib.sem_unlink({name!r})==0}}))
""",
        )
        assert result == {"opened": False, "unlinked": False}
    finally:
        spawn.cleanup()
        spawn.popen.communicate(timeout=5)
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        lib.sem_unlink(name)
