# Shell Sandbox

Alysis Code can run shell and verification commands inside a sandboxed environment. The sandbox is designed to reduce the blast radius of local command execution while keeping normal repository workflows usable.

The shell sandbox applies to `shell_run`, `shell_background`, and durable service launches through `shell_service_start`. `shell_output`, `shell_wait`, `shell_list`, and `shell_kill` operate on already tracked background processes and do not launch new sandbox containers or host commands. Verification commands and the `verify_run` tool use the verification sandbox path, which reuses the same backend settings unless configured otherwise.

Sandboxing is not a substitute for reviewing code, dependencies, or commands before running them.

## Quick Start

Diagnose the current machine:

```bash
alysis sandbox doctor --smoke
```

Run guided setup:

```bash
alysis sandbox setup
```

Pull default Docker images:

```bash
alysis sandbox pull
```

Useful commands:

| Goal | Command |
| --- | --- |
| Diagnose readiness | `alysis sandbox doctor [--smoke] [--env]` |
| Guided setup | `alysis sandbox setup [--no-pull]` |
| Pull images | `alysis sandbox pull [--no-server] [--image NAME]` |

## Backends

Alysis Code has portable backends and a bundled native Windows beta backend:

- `bwrap`: Bubblewrap-based isolation, recommended on Linux when available.
- `docker`: container isolation where a compatible Docker daemon is available.
- `seatbelt`: the native macOS sandbox, used for shell commands and contained file workers.
- `windows-mxc`: the native Windows beta backend built on Microsoft's upstream
  sandbox runtime. The standalone Windows x64 CLI bundles its signed helper and
  selects it by default. See [installation and limits](windows-cli-sandbox.md).

Backend selection defaults to `auto`, which selects `windows-mxc` on native
Windows, native Seatbelt on macOS, and Bubblewrap then Docker on Linux/WSL.
An unavailable Windows helper fails closed; it does not trigger a host fallback.

Native Windows and WSL are separate runtime environments. The Windows candidate
runs Windows programs directly; Bubblewrap requires Linux (including a suitable WSL 2 environment).
Check backend availability from the same environment that runs Alysis with
`alysis sandbox doctor --smoke`. Neither an installed Docker CLI nor a passing
CLI startup check proves sandbox readiness.

### Bubblewrap

The Bubblewrap backend mounts the workspace at `/workspace`, provides isolated temporary and process namespaces where supported, and can use a hardened profile that avoids broad host mounts.

Install Bubblewrap with your operating-system package manager, for example:

```bash
sudo apt install bubblewrap
```

Bubblewrap can run inside WSL 2 when its Linux namespace requirements are available.
When no backend is found, `alysis setup` offers to run the command above for you.
It is not a native PowerShell backend.

Bubblewrap finds commands through the launcher's `PATH`, without a list of known
language or package-manager names, using the same discovery logic as Seatbelt.
It mounts executable files, their canonical targets, and conventional adjacent
code directories (`lib`, `lib64`, `libexec`, `share`, `include`) read-only.
A `PATH` directory is not a recursive read grant: adjacent private files remain
unavailable. Relative and project-controlled PATH entries cannot promote external
symlink targets into runtime authority. Executable aliases preserve their identity,
so the requested custom installation does not silently fall back to a system one.
System tools and the engine's own Python runtime remain available. Aliases to
already mounted tools do not rescan or remount their system libraries. Links in
system PATH directories can also expose tools installed outside those mounts;
discovery admits their executable targets and code directories through the same
scope validation. Linked code directories include their canonical targets so
their dependencies remain readable, while private targets remain blocked. Nonstandard
support data needs explicit read-only installation scopes from the launcher via
`ALYSIS_DESKTOP_RUNTIME_ROOTS` JSON array (also usable when launching the CLI):

```bash
ALYSIS_DESKTOP_RUNTIME_ROOTS='["/opt/my-tool-installation"]' \
PATH="/opt/my-tool-installation/executables:$PATH" alysis
```

Use absolute canonical installation directories for support data outside the
discovered code paths. An executable shim does not grant arbitrary adjacent data.
The whole explicitly approved directory is readable,
so choose installation folders rather than a home directory or a mixed private
data/cache tree. Private app state and writable workspace overlap are refused.
New non-system runtime grants require copy installation semantics, rather than hardlinks to
an external package store. Workspace symlinks, including npm executable links,
resolve within the mounted namespace; external ungranted targets stay unavailable.
Workspace hardlinks are rejected because they can alias unapproved host content.
Approved Git metadata writes remain available when runtime or extra workspace
mounts are present; ordinary commands retain metadata protection on both aliases.
Missing future runtime generations stay protected but
are not mounted. Changes apply to the next launch; existing processes retain
their launch-time mounts until stopped.

This replaces implicit Node/Rust/version-manager root guesses and synthesized
language-specific environment variables. A tool that needs configuration should
receive it in its reviewed command or launcher configuration, within the normal
sandbox policy. This does not add the macOS permission-profile workflow to WSL.
Native Windows MXC is unchanged. Seatbelt retains its existing discovery semantics.

One existing language-specific compatibility rule remains in
`sandbox_runner._hardened_java_config_ro_bind_paths`: it admits `/etc/java-*` and
`/etc/.java` read-only. That convention can miss custom JDK configuration layouts.
Explicit runtime scopes are the general alternative; this change does not expand
the legacy exception.

The opt-in native regression is
`ALYSIS_RUN_BWRAP_RUNTIME_TESTS=1 python -m pytest -q tests/test_bwrap_runtime_scopes.py`.
It exercises arbitrary executable names, Unicode installation paths, separate
shims/dependencies, scope removal, denied sibling/private reads and denied runtime
writes in both read-only and workspace-write modes. Runtime inspection checks
cancellation and the foreground command deadline; background preparation has a
30-second deadline. Replay on WSL before qualifying a Windows/WSL release.

### macOS Seatbelt

Seatbelt uses `/usr/bin/sandbox-exec`; Docker is not required. Shell commands,
background commands and contained file workers share the same filesystem policy.
Commands can read approved project folders and the system/toolchain/runtime files
needed to execute tools. Other file contents are denied. Workspace-write permits
edits in approved folders; read-only denies edits in every approved folder.

Each invocation gets its own temporary directory and HOME, with a sanitized
environment by default. The directory survives for a background process's lifetime
and is removed during cleanup. Other system temporary directories are not writable.
Private configuration, credentials and Desktop control state remain unreadable;
runtime files and protected repository metadata remain unwritable even when nested
inside a project. Ancestors of protected paths cannot be renamed out of their rules.

Seatbelt resolves symlinks at access time. Existing hardlinks require all aliases
to have the same approved access; unknown or mixed writable/read-only aliases
are rejected. New hardlinks are restricted to writable project sources.
Filesystem roots and the entire
home directory cannot be granted as a project. Read-only mode and multi-folder
requests never silently fall back to unsandboxed host execution.

With local-preview access disabled, network-off blocks loopback as well as external
IP traffic. An explicit local-preview grant permits loopback clients and listeners.
This is
filesystem/process containment, not a VM or complete isolation from host services.
Run `alysis sandbox doctor --smoke` to verify native readiness on the current Mac.

### Docker

The Docker backend runs each command in an ephemeral container, mounts the workspace at `/workspace`, disables network by default, drops Linux capabilities, and avoids forwarding the host environment unless explicitly configured.

Install Docker Desktop if you explicitly choose Docker on macOS or Windows. The
native Windows backend does not need it. On Linux, make sure the current user can
access the Docker daemon when selecting this backend.

Durable services use the same backend settings but are intentionally detached
from the agent session lifecycle. Bubblewrap durable services omit
`--die-with-parent`; Docker durable services keep a named container until
`shell_service_stop` cleans it up. Service metadata contains process/container
identity, readiness config, and runtime log paths, not raw commands or inherited
secret environment values.

## Default Settings

Current defaults:

| Setting | Default |
| --- | --- |
| `shell_sandbox.mode` | `strict` |
| `shell_sandbox.backend` | `auto` |
| `shell_sandbox.network` | `off` |
| `shell_sandbox.preview_access` | `auto` (resolves to `local` unless overridden) |
| `shell_sandbox.bwrap_profile` | `hardened` |
| `shell_sandbox.clear_env` | `true` |
| `shell_sandbox.protect_repo_meta` | `true` |
| `shell_sandbox.unavailable_fallback` | `refuse` |

In environment shorthand, the production default is:

- `network=off`

Mode behavior:

- `strict`: require a usable sandbox backend.
- `warn`: attempt sandbox execution and warn on setup problems; with the default fallback, it does
  not run on the host when a backend is unavailable.
- `off`: run on the host shell. This is an explicit unsafe opt-in.

The CLI defaults to refusing commands when no backend is available. A host that can display
per-command approvals may explicitly set `shell_sandbox.unavailable_fallback="ask"` (or
`ALYSIS_SHELL_SANDBOX_FALLBACK=ask`); each fallback command then needs approval to run on the host.
This fallback does not provide sandbox isolation.

To explicitly disable shell sandboxing:

```bash
export ALYSIS_SHELL_SANDBOX_MODE=off
```

or equivalent config:

```json
{
  "shell_sandbox": {
    "mode": "off"
  }
}
```

Use `off` only for trusted local work where you would run the same commands directly.

### Local workspace previews

Static HTML/CSS/JavaScript previews use the dedicated `workspace_preview_start` tool. The model
requests semantic access (`auto`, `local`, or `lan`) instead of choosing a raw bind address. The
runtime resolves an interface from the operating system and asks the OS for an available port
unless the user requested one. `auto` follows `shell_sandbox.preview_access` and currently falls
back to `local` when that setting is also `auto`.

LAN access requires approval unless the session already has explicit full-access consent. Each
LAN preview receives a random temporary credential; the returned `access_url` installs an
HTTP-only, same-site cookie and then removes the credential from the browser address. Tokens are
kept in a user-private service file, excluded from service metadata and request logs, and removed
when the preview stops.

The preview server disables directory listings, blocks hidden files, rejects symlink escapes
outside the selected workspace directory, and does not depend on the Docker sandbox image. Stop
it with `shell_service_stop` using the returned service id.

Arbitrary containerized development servers still use `shell_service_start`. For TCP readiness
with the Docker backend, sandbox networking must be explicitly enabled. Alysis Code resolves a
loopback interface from the operating system and publishes only the requested TCP port there; the
server inside the container must listen on its container-wide interface at that port. Docker's
`network=off` mode intentionally rejects this path instead of starting an unreachable service.

## Docker Images

Published GHCR images:

- `ghcr.io/alysisai/alysis-sandbox:base`: minimal shell sandbox image.
- `ghcr.io/alysisai/alysis-sandbox:dev`: default development image for shell and verification commands.
- `ghcr.io/alysisai/alysis-sandbox:server`: worker image for server mode.

Pull the default image:

```bash
docker pull ghcr.io/alysisai/alysis-sandbox:dev
```

Build locally:

```bash
docker build --build-arg VARIANT=dev -t alysis-sandbox:dev -f scripts/sandbox/Dockerfile .
```

Pin a production image by digest:

```bash
docker buildx imagetools inspect ghcr.io/alysisai/alysis-sandbox:dev
export ALYSIS_SHELL_SANDBOX_DOCKER_IMAGE=ghcr.io/alysisai/alysis-sandbox@sha256:<digest>
```

When available, verify signature and provenance with your release process before pinning a digest.

## Environment Configuration

Environment variables override config values:

```bash
export ALYSIS_SHELL_SANDBOX_MODE=strict
export ALYSIS_SHELL_SANDBOX_BACKEND=auto
export ALYSIS_SHELL_SANDBOX_NETWORK=off
export ALYSIS_SHELL_SANDBOX_PREVIEW_ACCESS=auto
export ALYSIS_SHELL_SANDBOX_DOCKER_IMAGE=ghcr.io/alysisai/alysis-sandbox:dev
export ALYSIS_SHELL_SANDBOX_CLEAR_ENV=1
```

Supported shell sandbox variables:

- `ALYSIS_SHELL_SANDBOX_MODE=off|warn|strict`
- `ALYSIS_SHELL_SANDBOX_BACKEND=auto|bwrap|docker|seatbelt`
- `ALYSIS_SHELL_SANDBOX_FALLBACK=refuse|ask`
- `ALYSIS_SHELL_SANDBOX_NETWORK=off|on`
- `ALYSIS_SHELL_SANDBOX_PREVIEW_ACCESS=auto|local|lan`
- `ALYSIS_SHELL_SANDBOX_BWRAP_PROFILE=compat|hardened`
- `ALYSIS_SHELL_SANDBOX_DOCKER_IMAGE=<image>`
- `ALYSIS_SHELL_SANDBOX_CLEAR_ENV=0|1`
- `ALYSIS_SHELL_SANDBOX_DOCKER_PIDS_LIMIT=<int>`
- `ALYSIS_SHELL_SANDBOX_DOCKER_MEMORY=<value>`
- `ALYSIS_SHELL_SANDBOX_DOCKER_CPUS=<value>`
- `ALYSIS_SHELL_SANDBOX_DOCKER_READ_ONLY=0|1`
- `ALYSIS_SHELL_SANDBOX_PROTECT_REPO_META=0|1`
- `ALYSIS_SHELL_SANDBOX_DOCKER_ENV_ALLOWLIST=VAR1,VAR2`

Equivalent `config.json` shape:

```json
{
  "shell_sandbox": {
    "mode": "strict",
    "backend": "auto",
    "network": "off",
    "preview_access": "auto",
    "bwrap_profile": "hardened",
    "docker_image": "ghcr.io/alysisai/alysis-sandbox:dev",
    "clear_env": true,
    "docker_pids_limit": 256,
    "docker_memory": "1g",
    "docker_cpus": "1.5",
    "docker_read_only": true,
    "protect_repo_meta": true,
    "docker_env_allowlist": ["LANG"]
  }
}
```

## Verification Sandbox

Verification commands use `ALYSIS_VERIFY_SANDBOX_MODE` or `verify_sandbox.mode`:

```bash
export ALYSIS_VERIFY_SANDBOX_MODE=strict
```

Supported values are `off`, `warn`, and `strict` (default `strict`).

Example config:

```json
{
  "verify_sandbox": {
    "mode": "strict"
  }
}
```

Verification commands default to strict sandboxing too. Verification reuses the
shell sandbox backend, image, network policy, and environment settings. Keep
network disabled unless a verification command genuinely needs outbound access.

The sandbox receives only commands that pass the verification contract
preflight. Ordinary verification commands are argv-compatible shapes. Exact
pipelines, redirections, or shell chaining are executed as trusted shell
expressions only when the expression came unchanged from host configuration, an
explicit user command, or a pre-existing repo/task checker. Model-inferred
shell chaining is rejected before sandbox launch. Interpreter snippets are
converted to explicit interpreter commands, such as `python -c`, when language
and syntax are clear; malformed prose is retained only as diagnostic/advisory
contract data.

Direct task-native evidence follows the same sandbox and mutation policy. If a
trusted command succeeds through `shell_run`, it can cover the verification
contract without a second `verify_run`. If that command changes
verification-relevant source, configuration, or requested outputs, the coverage
is stale until a later clean run succeeds.

## Production Profile

For production-style or shared environments:

```bash
export ALYSIS_SHELL_SANDBOX_MODE=strict
export ALYSIS_SHELL_SANDBOX_BACKEND=docker
export ALYSIS_SHELL_SANDBOX_NETWORK=off
export ALYSIS_SHELL_SANDBOX_DOCKER_READ_ONLY=1
export ALYSIS_SHELL_SANDBOX_PROTECT_REPO_META=1
export ALYSIS_SHELL_SANDBOX_DOCKER_PIDS_LIMIT=256
export ALYSIS_SHELL_SANDBOX_DOCKER_MEMORY=1g
export ALYSIS_SHELL_SANDBOX_DOCKER_CPUS=1.5
export ALYSIS_SHELL_SANDBOX_DOCKER_ENV_ALLOWLIST=LANG
```

Recommended practices:

- pin Docker images by digest
- keep network disabled by default
- use a custom image with preinstalled dependencies instead of downloading packages during verification
- keep repository metadata protection enabled
- use narrow environment allowlists

## Troubleshooting

Run:

```bash
alysis sandbox doctor --smoke --env
```

Common cases:

| Symptom | Likely cause | Next step |
| --- | --- | --- |
| No usable backend | Docker or Bubblewrap is missing or unavailable | Install/start the backend, then rerun doctor. |
| Docker daemon error | Docker Desktop is closed or permissions are missing | Start Docker or fix daemon access. |
| Image missing | Docker works but the sandbox image is not local | Run `alysis sandbox pull`. |
| Pull timeout | Registry, proxy, DNS, or auth issue | Retry with a longer timeout and inspect pull output. |
| `pytest` missing in `:base` | The base image is intentionally minimal | Use `:dev` or a custom image with project tools. |
| Slow WSL2 mounts | Repository is under `/mnt/c/...` | Prefer a Linux filesystem checkout under WSL. |
| `.git/` write denied | Repository metadata protection is active | Avoid writing repo metadata, or disable only for trusted local work. |

Disable repository metadata protection only when the task is trusted and needs it:

```bash
export ALYSIS_SHELL_SANDBOX_PROTECT_REPO_META=0
```

## Limitations

### File tools in the Windows candidate

The `host.file-policy-v1` capability extends the shell boundary to model-facing
file tools. Direct reads validate opened file objects and refuse private state,
reparse paths and multiply linked files. Recursive readers and final mutations
run in fixed workers inside the selected OS sandbox after the usual approval and
stale-file checks. Workers receive only bounded tool arguments, never provider
credentials. Their network is disabled independently of shell network access.

The supported worker implementations are Windows MXC, the explicitly selected
legacy Windows helper, hardened Bubblewrap, and macOS Seatbelt. Unsupported backends refuse these
operations instead of falling back to host execution. Explicit Full access with
sandbox mode off retains host execution; Full access paired with strict sandbox
settings does not disable the protected-path policy.

Windows uses a retained directory-relative file handle for larger request packets,
so junction changes cannot redirect host packet writes or cleanup. Git patches use
a temporary index and object store; they preserve the real index and LF/CRLF file
endings. If image publication partly succeeds, successful images remain in place
and the error identifies them; cleanup never follows a newly substituted path.

Bubblewrap refuses primary workspaces overlapping private configuration/data or
trusted writable runtime paths, including a project containing the active engine
virtual environment. Place the engine environment outside that project. Linked
folders, OneDrive placeholders and hardlinked source trees can be refused rather
than silently granted access. Installed-runtime tests are separate from unit tests
that deliberately run with shell containment disabled.

Candidate verification uses `scripts/qualify_contained_file_tools.py` with the
installed Python and a new synthetic output directory. Standalone Windows uses its
bundled pinned helper; explicit development overrides still require both helper
path and digest. WSL requires Bubblewrap. Neither successful local tests nor
Windows Server CI replace clean supported Windows client, signing and remote-peer
network qualification.

### Additional approved folders

Use repeatable launcher options before the command, for example
`alysis --add-dir C:\projects\engine run --path C:\projects\desktop`.
Desktop supplies per-chat grants and per-turn project-folder snapshots selected
through its native folder picker. The project controls shared instructions and
memory; each chat keeps its working directory for relative paths, repository
discovery and change history. File tools and shell commands can read/edit additional folders
using absolute paths or use one as their `cwd`. Additional folders are disclosed
in the model's environment context; they do not inherit instructions automatically.

CLI grants are captured from the trusted launcher for the process lifetime.
Desktop project-folder snapshots are captured per turn and propagated to worker
threads/processes; later turns use the current snapshot after folders are detached.
Project configuration, command environment overrides and later environment edits
cannot expand a captured runner's scope. Approval mode, filesystem write mode and
network access remain independent. Every approved folder is read-only under a
read-only policy, and every writable folder retains instruction/repository metadata
protection. Effective engine settings, Desktop control state and trusted runtimes
cannot become writable workspace roots.

Native MXC and the explicitly selected legacy Windows backend support these grants.
Linux/WSL requires hardened Bubblewrap with clean environment and metadata protection.
Real paths and the primary `/workspace` alias receive the same protections, including
nested approved roots. macOS Seatbelt supports these folders under the policy above.
Docker refuses additional folders; the engine never silently drops the list or switches to
host execution. Full access remains an explicit choice to run without the sandbox.

This follows Codex's separation of approved folders, approval decisions and network
policy. It is not a claim of feature identity: secondary repository/history
selection, native protected Git writes, durable sandbox services and host-loopback
access are not provided by this candidate. MXC network-on permits outbound traffic;
inbound and host-loopback restrictions remain in place.

- The shell sandbox isolates shell and verification command execution, not every host-side orchestration step.
- Sandboxed commands can modify the mounted workspace. The Docker read-only option
  protects the image root filesystem; the workspace bind remains writable, with
  protected repository metadata mounted read-only for arbitrary shell commands.
- A sandbox reduces impact; it does not make untrusted code safe.
- Custom tools have their own subprocess execution and capability checks; they are not automatically routed through the shell sandbox.
