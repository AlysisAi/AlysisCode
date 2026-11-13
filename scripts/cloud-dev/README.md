# Local Linux development for cloud bots

This lab runs on your own machine using Docker. It provides a real Linux browser,
desktop, and AlysisCode execution server for developing the cloud-only Bots product.
It does not create Azure resources or use model APIs. Building the image downloads
public packages; normal container operation has no public internet route.

## Start and check

Use Docker Desktop with Linux containers and Docker Compose v2 or newer. The image
uses native Debian Chromium packages for ARM64 and AMD64. Apple Silicon runs ARM64
without x86 emulation. Allow at least 4 GiB of memory in Docker; this container is
limited to 2 CPUs, 3 GiB RAM, and 512 processes. The image/build cache also needs
several GiB of disk space. A separate desktop-viewer process is limited to 0.5 CPU
and 128 MiB RAM and shares the same image.

From the repository root:

```bash
scripts/cloud-dev/dev up
scripts/cloud-dev/dev check
scripts/cloud-dev/dev persistence
```

Open the [Linux desktop](http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale).
This is a development desktop, not the future Bots interface. `check` temporarily
controls Chromium to exercise keyboard and clipboard input, so avoid interacting
with the desktop during that command.

The wrapper explicitly selects the local `desktop-linux` Docker context, ignoring
an active remote context. On Linux, select a local Unix-socket context:

```bash
ALYSIS_DEV_DOCKER_CONTEXT=default scripts/cloud-dev/dev up
```

The wrapper rejects TCP/SSH contexts, does not load a `.env` file, and never mounts
the host Docker socket, repository checkout, home directory, or credentials.

| Command | Effect |
| --- | --- |
| `up` | Build from this checkout, start, and wait for health checks |
| `check` | Run real infrastructure checks without a model |
| `persistence` | Recreate this lab container and verify saved workspace data |
| `shell` | Open a Linux shell in the persistent workspace |
| `logs` / `status` | Inspect the lab |
| `stop` | Stop the lab to release CPU and memory |
| `down` | Remove this lab's containers/network; preserve its data volume |

Run `up` after source changes; code is built into the image rather than mounted
from the Mac. Docker caches unchanged layers. None of these commands deletes the
named data volume. Avoid storing valuable files here without a separate backup.

## What the checks prove

- The real AlysisCode HTTP server creates a workspace. Its actual `JobRunner`
  queues a Python process inside bubblewrap, which writes a file and cannot read
  a marker outside the workspace or access a network route. This checks the
  coding execution infrastructure; it does not exercise model-driven coding.
- The existing managed-browser service launches sandboxed Chromium. Default
  navigation rejects loopback; an explicitly registered owned preview serves a
  local test page. The service enters Unicode text, clicks, reads page text, and
  captures a PNG through its actual CDP transport. Both headless and visible
  managed sessions are checked; the visible session is the actual automated
  browser shown by the desktop viewer, not a second lookalike page.
- A separate visible Chromium runs on Xvfb/Openbox. X11 input, clipboard, pointer
  movement, and screen capture work. The visible managed-browser check also sends desktop input into its controlled
  page and verifies the combined result. Ownership rejection and cancellation
  before input are checked. A bot-facing desktop tool adapter and coordinated
  human takeover are still to be implemented.
- `persistence` verifies a server workspace and file survive container replacement
  while a temporary marker disappears. This is filesystem persistence, not a
  semantic memory system or recovery of in-flight jobs.

Screenshots and run data live in the Docker volume `alysis-cloud-dev_state`, under
`/var/lib/alysis/checks` and `/var/lib/alysis/server`. The interactive workspace is
`/var/lib/alysis/workspace`. Browser profiles are temporary and reset on recreation.

Validated on 2026-10-18 with Apple M5 / Docker Desktop 29.6.1, native Linux ARM64,
Python 3.12.14, Chromium 155.0.8059.79, and bubblewrap 0.12.0. All live checks and
container-replacement persistence passed, as did the host-to-VNC WebSocket check.
The targeted server/browser regression suite had 107 passes and 5 skips (the host
Python environment lacks FastAPI; the container's actual HTTP API was exercised).
AMD64, native Linux hosts, and Azure have not yet been qualified with this lab.

The HTTP server listens on **container loopback** port 7070 and retains its existing
localhost-only authentication behavior. Access it through `dev shell` for now.
The only published port is the desktop viewer on **host 127.0.0.1:6080**. A separate
viewer container forwards WebSocket connections to the Linux desktop's private
VNC port. Only that viewer has an external network, so Docker Desktop can publish
its port while the Linux runtime stays offline. The viewer has no bot data volume
and its proxy destination is fixed to the VNC service; it is not an outbound proxy.
It has no login because it is a trusted local lab; other local users/processes can access it.
Do not forward that port or deploy this Compose file to a public server.

## Browser command pause study

```bash
docker --context desktop-linux exec -i alysis-cloud-dev-linux-1 python - < scripts/cloud-dev/command_gate_study.py
```

After rebuilding with `dev up`, this provider-free study runs visible Chromium on
its own disposable X11 display, with no viewer listener. A delayed HTTP response
and barriers after real CDP acknowledgements exercise pauses during navigation,
compound input, and fresh observation. Reports and a screenshot are saved under
`/var/lib/alysis/checks/command-gate-*`, including partial reports on failure.

The owned-session service now exposes `pause_commands` and `resume_commands` to a
trusted coordinator. Pause immediately rejects new browser commands and waits
for the active operation to finish. Timeout/cancellation after the fence leaves
admission closed; it does not cancel or roll back that operation. Status exposes
`commands_paused`, `operation_active`, and `command_generation` separately.
Resume checks the expected pause generation, captures a fresh DOM, then opens a
new generation. New pauses and shutdown supersede an in-progress resume.

After the first pause, navigation/click/type require `expected_generation` from
the current observation. Old or missing generations fail before dispatch. Existing
unpaused callers remain compatible at generation zero. These methods are not yet
model tools, a Bots endpoint, or a takeover UI. The future coordinator must also
bind retained observations to their generation, independently verify viewer
revocation before resuming, and enforce authenticated ownership. The current
viewer control channel below is not reliable enough for that integration.

## Viewer control study

Run the provider-free study inside the existing lab:

```bash
docker --context desktop-linux exec -i alysis-cloud-dev-linux-1 python - < scripts/cloud-dev/viewer_control_study.py
```

It creates its own disposable X11 display, visible managed Chromium, local test
page and loopback-only x11vnc listener. It does not change the existing desktop or
viewer. Real RFB clients deliberately send keyboard, pointer and clipboard events
despite watch-only mode. Actual DOM values, X11 key/button state and RFB pixels
verify the results. Reports, logs and screenshots are saved under the returned
`/var/lib/alysis/checks/viewer-control-*` directory. Failed checks return nonzero
and preserve a partial report. No model or provider credential is used.

The configured study uses `-pointer_mode 1 -nowf -noscr`; adding
`--default-optimizations` after `python -` reproduces the default input-loop
comparison. That baseline timed out waiting for revocation while a button was
held. The configured study passed all nine checks once, but an identical repeat
also timed out at revocation. Disabling just wireframe/scroll optimizations, or
continuing framebuffer requests, did not resolve every observed timeout. This
control channel is not qualified for takeover. No default lab flags have been
changed by this research.

The study establishes server-side watch enforcement and individual viewer
permissions. It also exposes held input after revocation/disconnect, stale button
bookkeeping after direct X11 release, and acknowledgements for vanished client
IDs. It is not an implemented agent/viewer handoff. The detailed findings and
remaining work are in `docs/bots-verification.md`.

## Sandbox and image choices

The container runs as UID 1000, with all Linux capabilities dropped, a read-only
root filesystem, no-new-privileges, and a private shared-memory segment. Chromium's
sandbox stays enabled. Docker's default restrictions prevent nested bubblewrap
from creating namespaces and mounting `/proc`, so this local lab uses:

- A vendored seccomp profile allowing namespace creation and namespace-local
  mount operations. Kernel capability checks still apply.
- `systempaths=unconfined` so bubblewrap can mount its own `/proc`. This removes
  Docker's normal masked/read-only system-path restrictions **inside the container**.

Those exceptions are for trusted development workloads. They are not a reviewed
production boundary for untrusted tenants. The internal Docker network blocks
public egress; it is not a complete host-isolation or production egress policy.
The production runtime needs its own tenant isolation and approved outbound access.

The Python and uv base images are digest-pinned and Python dependencies use
`uv.lock`. Debian desktop packages follow the signed Debian repositories at build
time, so OS packages are not a bit-for-bit reproducible closure. Record and scan
the final image digest before a cloud pilot. This Debian desktop image is separate
from the existing pinned Wolfi coding sandbox in `scripts/sandbox/`; these checks
do not validate that production image or an Azure deployment.

If sandbox startup fails on another Linux host, inspect its user-namespace/AppArmor
policy. Do not resolve it by adding `--privileged`, `SYS_ADMIN`, `--no-sandbox`, or
silently disabling AlysisCode's strict sandbox mode.

## Product boundaries and next work

This lab establishes the local Linux environment. The separate `scripts/bots-dev`
service now supplies peer bots, guided setup, memory/notes, files, durable tasks,
schedules and unread receipts. The two services are not connected for computer use.
This lab itself reads no provider credentials and retains its offline network.

The trusted host can opt into a visible managed browser with
`ManagedBrowserConfig(linux_display=":99")`. The default remains headless. Only
local X11 display syntax is accepted; the display is not inherited from the
process environment or exposed as a model tool argument. The sandbox, owned
profile, egress proxy, loopback CDP and owner checks remain in force. This option
is qualified on this ARM64 Linux lab only. CJK fonts are installed so the tested
Japanese content is readable rather than missing glyphs.

The agreed product direction remains:

1. Users create peer bots through guided setup; a main assistant is optional.
2. Bots keep their own state, memory, files, permissions, and schedules in the cloud.
3. Coding requests go to separate AlysisCode cloud jobs and return results to the
   requesting bot. There is no special coder-bot type.
4. The same Linux runtime can be tested locally before a small Azure pilot.

Next, connect a scoped browser capability to the durable Bots runtime. General
OS actions, visible ownership/control state, cancellation of in-flight work and
human takeover still need an end-to-end product implementation. The coding server
queue is process-local; it is separate from the already durable Bots scheduler. Keep coding execution separate from bot
state when integrating the handoff. Provision the Azure pilot only after local
end-to-end checks, tenant isolation, usage metering, and an explicit spend limit.

## Third-party profile attribution

`seccomp.json` is derived from the Apache-2.0-licensed Moby project
[default profile at 6fe7deb1b9fb7c0397a4593480d7d22b9ee8caef](https://github.com/moby/profiles/blob/6fe7deb1b9fb7c0397a4593480d7d22b9ee8caef/seccomp/default.json).
It is reformatted and adds an unconditional rule for `clone`, `setns`, `unshare`,
`mount`, `umount2`, `pivot_root`, and `chroot` for nested bubblewrap and Chromium.
All other rules are unchanged. The additional rule is marked in the file.
See this repository's Apache 2.0
[license](../../LICENSE). Playwright's namespace approach is described in its
[Docker documentation](https://playwright.dev/docs/docker).


## Opt-in model/browser study

After `dev up`, from the repository root:

```sh
PYTHONPATH=src python scripts/qa/bots_browser_study.py --execute-live --max-calls 6
```

This deliberately makes bounded calls through the saved `zai-coding-plan` profile
using `glm-5.3-flash`, low reasoning and no retries/fallback. Model requests run on
the Mac; a JSON-lines bridge over local Docker exec invokes the real browser in
the offline container. No key enters that container. The fixture has randomized
field IDs and a local server that independently checks the exact submitted values
and submission count. Model-visible observations come from Chromium. The fixture
is test data, not production behavior or a keyword-based automation route.

Reports/transcripts go to ignored `reports/bots-browser-study-*`; screenshots stay
in the lab volume under `checks/browser-agent-*`. The study closes its browser and
fixture server. It is a research harness, not a Bots menu capability. The default
`--observation-mode full` preserves the original full-DOM baseline. Add
`--observation-mode compact` to compare a structurally decoded view while retaining
the entire message history. This view includes layout nodes and their ancestors,
exact field values/text up to explicit limits, and main-document light-DOM
selectors. Layout membership does not prove visibility. Frames, shadow roots and
pseudo-elements do not get potentially misleading action selectors.

The compact mode keeps the last four original observations in worker memory.
`browser_source` pages their original JSON by opaque source ID and character
offset. Reading does not refresh the page or extend retention; expired sources
fail. This is evidence retrieval, not durable bot memory. Reports contain paired
full/compact character counts from the same captures, plus provider-reported
token usage. A small form result is not a general-web benchmark.

The provider-free check exercises generated selectors, Unicode values, exact
source reconstruction, immutability after page changes, expiration, actual save
count and visible confirmation:

```sh
PYTHONPATH=src python scripts/qa/bots_browser_study.py --verify-observations --observation-mode compact
```

## In-flight cancellation study

After rebuilding with `dev up`, run the provider-free study from the repository:

```sh
docker --context desktop-linux exec -i alysis-cloud-dev-linux-1 python - < scripts/cloud-dev/cancellation_study.py
```

It cancels a real navigation only after the offline HTTP server receives the
request, verifies that the closed control connection reports `disconnected`, and
checks that further operations fail rather than appearing usable. A second check
sets cancellation after a real Backspace acknowledgement in a text replacement;
the deletion remains and the new text is not inserted. Its transport decorator
only controls the timing, never the CDP result. This establishes partial effects,
not rollback or human handoff. Browsers and the temporary HTTP server are closed
afterward. No provider key, internet access or model call is used.

An eventual takeover flow must fence new agent actions first, wait for current
input to finish safely, and gate the actual viewer input route. Resuming must reject
old pending agent commands and re-observe the page. The lab's existing loopback
noVNC viewer has no such shared control protocol, so it must not be presented as
an exclusive production takeover system.


## Coding-worker integration audit

```bash
docker --context desktop-linux exec -i alysis-cloud-dev-linux-1 python - < scripts/cloud-dev/bots_worker_boundary_audit.py
```

This provider-free audit uses actual bubblewrap Python processes in a temporary
workspace, then recreates the JobRunner over its store. It also constructs argv
with a synthetic noncredential marker, without executing those commands. It does
not restart the running HTTP daemon or modify the lab's existing workspaces.
Current evidence: duplicate submissions execute twice; terminal receipts survive
but runner lookup is not recovered; environment values appear in worker argv.
This is a checklist of concrete gaps before Bots can reuse the existing worker,
not a qualified production handoff. See the coding handoff plan and audit JSON.
