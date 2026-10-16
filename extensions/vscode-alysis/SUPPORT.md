# Support

## Where to file a bug

Open an issue at <https://github.com/AlysisAi/AlysisCode/issues>.

Do not report security vulnerabilities in a public issue. Email
[products@alysisai.com](mailto:products@alysisai.com) instead, as described in
[SECURITY.md](https://github.com/AlysisAi/AlysisCode/blob/main/.github/SECURITY.md).

## What to include

1. Run **Alysis Code: Check Setup** from the Command Palette. It runs the sandbox doctor smoke check
   and reports the result in the Alysis Code view. Include what it says.
2. Run **Alysis Code: Check Connection** to validate the runtime and IDE bridge protocol, and include
   that result too.
3. Open the **Alysis Code** output channel (View > Output, then pick "Alysis Code" from the dropdown)
   and paste the relevant lines. Secrets are redacted there, but read before you paste.
4. Add your VS Code version, operating system, whether the window is local or remote (SSH,
   container, WSL, tunnel), and whether the workspace is trusted.
5. Add the exact command or slash command you ran and what you expected instead.

**Alysis Code: Troubleshooting** collects the same diagnostics in one place if you would rather start
there.

## Setup guide

**Alysis Code: Open Setup Guide** opens the *Get started with Alysis Code* walkthrough: validate the
runtime, trust the workspace, configure a provider, and open Alysis Code. The
[README](https://github.com/AlysisAi/AlysisCode/blob/main/extensions/vscode-alysis/README.md)
covers requirements and install.

## Common problems

### Alysis Code cannot find or launch the runtime

Release builds bundle a signed, platform-specific runtime that the extension validates and installs
outside your workspace. If validation fails, **Alysis Code: Check Connection** and the Alysis Code
output channel name the cause. `alysis.cliPath` and **Alysis Code: Use Development CLI Override**
exist only as development overrides and are labeled non-production in Runtime Details. In Restricted
Mode, workspace-defined CLI paths are ignored until Workspace Trust is granted.

### Incompatible bridge protocol

**Alysis Code: Check Connection** reports protocol mismatches. The extension requires IDE Protocol v1
over stdio and validates the live bridge with `initialize` before creating sessions. Features whose
methods are missing are disabled individually with an explicit reason rather than failing silently;
upgrade the Alysis Code runtime to clear them.

### Sandbox doctor failures

**Alysis Code: Check Setup** runs `alysis sandbox doctor --smoke`. Common causes are a missing
sandbox backend (bubblewrap on Linux, Docker elsewhere), Docker daemon or permission issues, or
platform-specific sandbox limits. The Sandbox chip in Runtime Details stays separate from CLI
health, so a failing sandbox is visible even when the runtime itself is fine.

### Untrusted workspace

Workspace Trust gates plan creation, write-capable flows, workspace-local CLI paths, and other
execution surfaces. Read-only connection checks, provider setup, and setup checks stay available. If
`alysis.defaultMode` is `review` or `auto` in an untrusted workspace, chat submission is blocked
before any process starts — switch to `readonly` or grant trust.

### Workspace-scoped settings ignored

In Restricted Mode the extension ignores workspace or workspace-folder values for
execution-sensitive settings, including `alysis.cliPath`, mode, model, provider URL, sandbox
profile, auto-start, and Forge enablement.

### API keys

Do not put API keys in settings. Use **Alysis Code: Configure Provider**, which stores the value in VS
Code SecretStorage. Keys are passed to the runtime only through the child process environment, never
on a command line, and are never forwarded to an untrusted executable path.

### Remote windows and path issues

Alysis Code must be available in the same environment as the extension host. For Remote WSL, use a
Linux-side runtime. MCP OAuth login is unavailable in remote extension hosts (SSH, containers, WSL,
tunnels) — open the folder in a local window to sign in. Avoid workspace-relative wrapper paths
until the workspace is trusted.

### Cancelling a run

**Alysis Code: Cancel Current Run** requests cooperative cancellation. IDE Protocol v1 has no hard
interrupt: the request is honored at the backend's next checkpoint, and a job is never shown as
cancelled until the backend reports a terminal state. Idle sessions close immediately.

## More

Known limitations, architecture notes, and the full slash command reference are in
[`docs/`](https://github.com/AlysisAi/AlysisCode/tree/main/extensions/vscode-alysis/docs) (not
shipped inside the VSIX).
