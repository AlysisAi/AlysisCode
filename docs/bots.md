# Bots development guide

Bots (Owls in the desktop app) is shared engine code. App integration uses its own
reviewed engine pin; an engine merge does not update an installed app or publish a
release. See [desktop engine contracts](desktop-engine.md).

## Product and execution boundaries

- Each bot has one continuous conversation. Bots are peers; the product does not
  require a privileged main assistant or a separate user-facing coding agent.
- Longer work runs in the background while the conversation remains responsive.
  Coding uses the engine through a scoped internal worker and retains task
  ownership, accounting, cancellation, and result verification.
- Peer requests stay within the owner's authorization. A message from another bot
  does not grant credentials or permission for an external action.
- Agent computer access and human viewer control have separate permissions.
  Pause, takeover, resume, and Stop must preserve those boundaries.
- Conversations, schedules, approved memories, files, and execution receipts
  belong in durable storage. Disposable computers must not become their only copy.
- Connected apps and credentials retain their existing authorization boundaries;
  do not inherit operator credentials into workers or disposable computers.

Historical test counts, deployment addresses and time-limited pilot approvals in older
records describe those runs only.

## Live desktop viewer

Cloud and local Windows Owls share the Linux desktop viewer implementation. Local
Windows computers use the packaged Hyper-V VM; Docker is a development/test tool,
not an end-user runtime dependency.

The app uses noVNC to decode incremental RFB updates. Its ordered byte channel
passes through the existing authenticated app API; credentials stay in Electron's
main process. `/computer/live` checks the bot owner and current display generation
before and after each read. `/computer/status` supplies control/lifecycle metadata
without capturing another screenshot. Older images retain the screenshot fallback.

The guest starts x11vnc in read-only `-inetd` mode over a private socketpair. It opens
no VNC listening port and disables selection/clipboard transfer. The private screen
channel is independent of serialized agent commands. Abandoned streams expire;
out-of-order exchanges close rather than replaying bytes.

Human input still goes through `/computer/control`, the existing pause/checkpoint
handoff, and the sequence-encrypted input channel. Continuous pointer events carry
coordinates and the DOM button mask; only unsent motion with the same button state
may be combined. Disconnect, lease expiry, pointer cancellation and focus loss
release held input. Watching does not grant control or explicitly wake a sleeping
computer.

Qualify both the app protocol and the actual guest image. `scripts/bots-cloud/check_image.py`
checks the isolated RFB path while an agent command is running, in addition to
Chrome, files, terminal and shared-display checks. A Linux viewer test does not
qualify the native Windows VM transport or replace its image/release checks.

## Source and local development

| Area | Entry points |
| --- | --- |
| API and service | [app.py](../src/alysis_code/bots/app.py), [local_service.py](../src/alysis_code/bots/local_service.py), [serving.py](../src/alysis_code/bots/serving.py) |
| Conversation and execution | [runtime.py](../src/alysis_code/bots/runtime.py), [execution.py](../src/alysis_code/bots/execution.py), [background.py](../src/alysis_code/bots/background.py) |
| Coding integration | [coding_api.py](../src/alysis_code/bots/coding_api.py), [coding_worker.py](../src/alysis_code/bots/coding_worker.py) |
| Durable state | [store.py](../src/alysis_code/bots/store.py), [postgres.py](../src/alysis_code/bots/postgres.py), [object_storage.py](../src/alysis_code/bots/object_storage.py) |
| Computers and control | [local_computers.py](../src/alysis_code/bots/local_computers.py), [cloud_desktop.py](../src/alysis_code/bots/cloud_desktop.py), [computer_control.py](../src/alysis_code/bots/computer_control.py) |

Use the [development scripts guide](../scripts/bots-dev/README.md) for local
setup, deterministic checks, and opt-in live studies. Its saved-fixture commands
require the named fixtures; missing data is not permission to make provider calls.
Local Docker development and private browser profiles are not evidence of hostile
tenant isolation. Qualify the actual backend and its trust boundary.

## Feature contracts

- Action review and approval rules: external effects and scoped authority.
- Parallel execution: task ownership, resource limits, and concurrent work.
- Portable storage: durable data and storage-provider boundaries.
- App and cloud integration: app/service connection and cloud qualification.
- Local computer integration: lifecycle, private VM boundaries, and native-app checks.

### Memory and Library

Approved memories remain per bot until explicitly corrected or deleted. Working
note consolidation retains its originals. Bounded retrieval limits what reaches
the model; it must not silently evict approved memories to make room.

The old 40-memory/40-note prototype caps were removed. Library files have a shared
account allowance, including reservations; memories and working notes are separate.
Recheck deployed configuration before describing a historical quota as live policy.

## Operations and qualification

Reconcile actual deployed source, resources, and configuration before acting;
an old pilot's schedule or authorization is not an active operating instruction.

### Cloud computer images

Build the Linux runtime and worker from the same reviewed checkout. The runtime
includes the desktop panel and wallpaper tools; the worker installs its own locked
engine package so isolated Python subprocesses cannot import an older base-image
version. Copying `src/` over an old image alone does not update installed packages.
Controllers must leave the image's desktop and browser implementation together.
Do not overlay bridge files from the controller. Deploy a qualified worker package
whose bridge and installed modules agree; mixed releases can break startup.

```sh
docker build --platform linux/amd64 -f scripts/cloud-dev/Dockerfile -t alysis-computer-runtime:reviewed .
docker build --platform linux/amd64 -f scripts/bots-cloud/Dockerfile \
  --build-arg RUNTIME_IMAGE=alysis-computer-runtime:reviewed \
  -t alysis-computer:reviewed .
docker run --name alysis-image-check --network none --user 1000:1000 --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --security-opt seccomp=./scripts/cloud-dev/seccomp.json \
  --security-opt systempaths=unconfined --pids-limit 512 --memory 2g \
  --entrypoint /opt/alysis/.venv/bin/python alysis-computer:reviewed \
  -I /opt/alysis/check_image.py --output /tmp/image-check
docker cp alysis-image-check:/tmp/image-check ./image-check
```

The check uses real private X displays and clicks the Files and Terminal dock
launchers. It verifies typed terminal input by reading back a file, captures the
application windows, and opens a browser page served only on loopback. It compares
the installed engine modules with the image source. It makes no model calls or cloud requests.
Retain its `desktop.png`, `files.png`, `terminal.png`, `browser.png`, `shared.png` and `result.json` from the disposable
container when qualifying a deployment, and inspect the images. Qualify the target
architecture and the actual imported provider image separately before activation.
Boot that image with the production CPU, memory, disk, and suspension settings.
An image committed from a larger builder can require a disk larger than the
production allocation's default, even when the files use little space.

Cloud computers use one private X display for Google Chrome, Files, LibreOffice
and Terminal. The viewer only has Take/Give back control; applications open from
the desktop dock. The Chrome launcher signals the managed browser rather than
starting an unguarded process. Google Chrome is required in the amd64 Cloud image,
installed from Google's signed repository. Local Linux browser discovery is unchanged.

Desktop applications still have a private mount/process/network namespace and do
not receive the service's files, credentials, control socket or another user's
computer. Their shared X display can interact with the online browser: this is a
browser-mediated network environment, not an offline security boundary. Browser
requests still pass through the managed network guard. Standalone local offline
desktops retain their separate display and network isolation. Opening a Cloud
computer initializes its managed Chrome session; polling an existing view does not.
Sleeping computers created before shared displays retain their advertised legacy
controls. Recreating one requires the normal explicit recovery flow; changing the
worker image never discards another Owl's saved memory automatically.

Changing the configured worker image affects new allocations. Hosted operators can set
`ALYSIS_BOTS_WORKER_IMAGE` to the qualified Azure image UUID in the service unit;
the bootstrap validates and applies this non-secret override without rotating
credentials. Remove the override to use the bootstrap's image again.

Memory-suspended computers retain their old filesystem and processes; restarting the controller
does not upgrade them. Preserve the existing image and workspace/browser keys.
Do not delete or force-recover a retained session to apply an image update without
confirming how its workspace and any unsaved application state will be preserved.

For each change, record the commit, backend, test command, result, and skipped or
unverified cases in the PR. Distinguish deterministic tests, real model
calls, hosted checks, and installed-app qualification. Keep failures and unresolved
security or release gates visible. A successful source test does not establish
native Windows behavior, production availability, or installed-app readiness.

Before an app pin or release, validate the actual candidate and its capability
contract, durable-state recovery, approvals, and backend-specific behavior. Keep
temporary task diaries and handoffs out of main; update these guides and preserve
only the evidence needed to assess the change.
