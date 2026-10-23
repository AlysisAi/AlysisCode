from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

# Runs in its own process so the test runner's stdio is never rewired. It claims the
# protocol streams exactly as the bridge does, parks a reader on protocol stdin (where
# the bridge spends its life), then starts a child that leaves stdin and stdout
# unspecified, as git and the search tools do.
_PROBE = """
import os
import subprocess
import sys
import threading

from alysis_code.ide.stdio_bridge import claim_protocol_streams

protocol_in, protocol_out = claim_protocol_streams()
for stream in (protocol_in, protocol_out):
    assert stream.fileno() not in (0, 1, 2)
    assert not os.get_inheritable(stream.fileno())
received = []
reader = threading.Thread(target=lambda: received.append(protocol_in.readline()), daemon=True)
reader.start()
child = subprocess.run(
    [sys.executable, "-c", "import sys; print('child-noise'); sys.stderr.write(repr(sys.stdin.read()))"],
    stderr=subprocess.PIPE,
    close_fds=False,
    text=True,
    timeout=20,
)
protocol_out.write("child " + child.stderr + "\\n")
protocol_out.flush()
reader.join(timeout=20)
protocol_out.write("received " + repr(received) + "\\n")
protocol_out.flush()
"""

# Force the parent wrappers to use the Windows legacy code page even on macOS.
# Exercise the real IDE request parser/writer and Forge Host v2's unescaped JSON
# encoder through the claimed transport, without a provider or user session.
_UTF8_PROBE = """
import json
import sys
from pathlib import Path

from alysis_code.forge_host_protocol.codec import encode_envelope, validate_envelope
from alysis_code.ide.stdio_bridge import StdioBridge, claim_protocol_streams

assert sys.stdin.encoding.lower() == "cp1252"
assert sys.stdout.encoding.lower() == "cp1252"
protocol_in, protocol_out = claim_protocol_streams()
assert protocol_in.encoding.lower() == protocol_out.encoding.lower() == "utf-8"
assert protocol_in.errors == protocol_out.errors == "strict"
line = protocol_in.readline()
request = json.loads(line)
bridge = StdioBridge(stdin=protocol_in, stdout=protocol_out)
try:
    bridge.process_line(line)
finally:
    bridge.close()

fixture = Path(sys.argv[1]) / "schemas/forge-host-v2/fixtures/valid/planner-input-request.json"
envelope = json.loads(fixture.read_text(encoding="utf-8"))
envelope["payload"]["input"]["text"] = request["id"]
encoded = encode_envelope(validate_envelope(envelope))
assert request["id"].encode("utf-8") in encoded
protocol_out.write(encoded.decode("utf-8") + "\\n")
protocol_out.flush()
"""


def _probe_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([str(SRC_DIR), existing]) if existing else str(SRC_DIR)
    return env


def _readline(process: subprocess.Popen[str], timeout: float = 30.0) -> str:
    stream = process.stdout
    assert stream is not None
    line: list[str] = []
    reader = threading.Thread(target=lambda: line.append(stream.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        process.kill()
        raise AssertionError("the bridge probe stopped answering")
    return line[0]


def test_child_processes_never_inherit_the_protocol_pipes() -> None:
    probe = subprocess.Popen(
        [sys.executable, "-c", _PROBE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=_probe_env(),
    )
    try:
        # The child ran while the bridge reader was parked on the protocol pipe. It must
        # have seen an empty stdin at once, neither hanging on the pipe nor consuming a
        # request, and its stdout must not have reached the protocol stream.
        assert _readline(probe) == "child ''\n"
        assert probe.stdin is not None
        probe.stdin.write('{"jsonrpc": "2.0", "id": 1}\n')
        probe.stdin.flush()
        assert _readline(probe) == 'received [\'{"jsonrpc": "2.0", "id": 1}\\n\']\n'
        probe.communicate(timeout=30)
        assert probe.returncode == 0
    finally:
        if probe.poll() is None:
            probe.kill()
            probe.wait(timeout=30)


def test_private_protocol_preserves_unicode_with_cp1252_parent_stdio(tmp_path: Path) -> None:
    text = "Ελληνικά 中文 😀"
    request = {
        "protocol_version": "1",
        "id": text,
        "method": "getCapabilities",
        "params": {},
    }
    env = _probe_env()
    env.update(
        PYTHONIOENCODING="cp1252",
        ALYSIS_CONFIG_DIR=str(tmp_path / "config"),
        ALYSIS_DATA_DIR=str(tmp_path / "data"),
        PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring",
    )
    probe = subprocess.run(
        [sys.executable, "-c", _UTF8_PROBE, str(REPO_ROOT)],
        input=(json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"),
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )

    assert probe.returncode == 0, probe.stderr.decode("utf-8", errors="replace")
    # Strict decoding rejects corrupted bytes; exact values also reject mojibake
    # and lone surrogates from decoding UTF-8 through a Windows code page.
    lines = probe.stdout.decode("utf-8", errors="strict").splitlines()
    assert len(lines) == 2
    ide_response, forge_response = map(json.loads, lines)
    assert ide_response["id"] == text
    assert "computer.respond" in ide_response["result"]["methods"]
    assert "browser_start.respond" in ide_response["result"]["methods"]
    assert "task_budget.respond" in ide_response["result"]["methods"]
    assert forge_response["payload"]["input"]["text"] == text
    assert text.encode("utf-8") in probe.stdout


def test_private_protocol_rejects_invalid_utf8_instead_of_creating_surrogates() -> None:
    env = _probe_env()
    env["PYTHONIOENCODING"] = "cp1252:surrogateescape"
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "from alysis_code.ide.stdio_bridge import claim_protocol_streams; "
            "protocol_in, protocol_out = claim_protocol_streams(); protocol_in.readline()",
        ],
        input=b'{"text":"\xff"}\n',
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )

    assert probe.returncode != 0
    assert b"UnicodeDecodeError" in probe.stderr
    assert probe.stdout == b""
