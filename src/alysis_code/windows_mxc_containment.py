"""What the MXC sandbox was measured to keep from a command, on a real machine.

A command whose effects cannot be read from its text used to need a person in
every mode. Inside this sandbox it follows the mode instead, as a sandboxed
command does in Codex: the sandbox is the boundary, and what a command needs
beyond it is requested and reviewed. This module records the measurement that
rests on (``docs/windows-cli-sandbox.md``, "Commands in the sandbox follow the
mode"). ``tests/test_windows_contained_probes.py`` repeats it against the real
sandbox, so the record cannot drift from the helper.
"""

from __future__ import annotations

# The helper security revision that was probed, for the engine's own build and
# for the desktop app's. A helper with another revision is not treated as
# measured until the probes are run against it and this number is moved.
PROBED_SECURITY_REVISION = 11

# What a command in that sandbox still reaches with networking off and no
# access granted. These are known limits, written down so that nobody has to
# rediscover them. They do not keep the person decision: Codex on Windows, the
# sandbox this one is held to, leaves each of them reachable too.
REACHABLE_WHEN_CONTAINED: tuple[str, ...] = (
    # Crash handlers of browser-based apps accept every app container and
    # upload what a client hands them.
    "host pipes that admit every app container",
    # Variables saved with setx, which often hold API keys.
    "the user's saved environment variables in the registry",
    # Decrypts any protected blob the command can read.
    "the user's DPAPI key",
    # Only exact paths can be write-protected, so the repository at the top of
    # the project is covered and a repository in a subfolder, or a new one a
    # command creates there, is not.
    "repository metadata below the project root",
)
