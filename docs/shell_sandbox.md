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

Alysis Code supports two sandbox backends:

- `bwrap`: Bubblewrap-based isolation, recommended on Linux when available.
- `docker`: cross-platform container isolation, recommended on macOS and Windows.

Backend selection defaults to `auto`.

### Bubblewrap

The Bubblewrap backend mounts the workspace at `/workspace`, provides isolated temporary and process namespaces where supported, and can use a hardened profile that avoids broad host mounts.

Install Bubblewrap with your operating-system package manager, for example:

```bash
sudo apt install bubblewrap
```

### Docker

The Docker backend runs each command in an ephemeral container, mounts the workspace at `/workspace`, disables network by default, drops Linux capabilities, and avoids forwarding the host environment unless explicitly configured.

Install Docker Desktop on macOS or Windows. On Linux, make sure the current user can access the Docker daemon.

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

In environment shorthand, the production default is:

- `network=off`

Mode behavior:

- `strict`: require a usable sandbox backend.
- `warn`: attempt sandbox execution and warn on setup problems; it does not fall back to host shell.
- `off`: run on the host shell. This is an explicit unsafe opt-in.

Host shell execution requires:

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
- `ALYSIS_SHELL_SANDBOX_BACKEND=auto|bwrap|docker`
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

- The shell sandbox isolates shell and verification command execution, not every host-side orchestration step.
- Sandboxed commands can modify the mounted workspace. The Docker read-only option
  protects the image root filesystem; the workspace bind remains writable, with
  protected repository metadata mounted read-only for arbitrary shell commands.
- A sandbox reduces impact; it does not make untrusted code safe.
- Custom tools have their own subprocess execution and capability checks; they are not automatically routed through the shell sandbox.
