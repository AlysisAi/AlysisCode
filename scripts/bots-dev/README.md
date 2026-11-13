# Local Alysis Bots development

This is the first Bots product slice: guided setup in Alysis, durable conversations,
model-driven text tasks, reviewed personal memory, versioned working notes, recoverable history, saved text files, and durable schedules. The runtime runs on
Linux through Docker Desktop. It does not provision Azure.

## Start

For the current integrated Bots branches, use the internal engine checkout
`alysis-engine-bots-main-sync` and app checkout `Alysis-Code-App-bots-main-sync`.
The installed release retains its published CLI pin. The development launcher
selects this internal engine through the isolated app profile's
`customBinaryPath`; setting only `ALYSIS_APP_CLI` is insufficient because main
rebuilds runtime environments from that saved selection. The normal capability
gate stays enabled.

Use a Python environment with the engine dependencies installed. Build the desktop
checkout once with `pnpm install --frozen-lockfile` and `pnpm run build`.
From the engine checkout:

```sh
python scripts/bots-dev/dev.py --build --profile opencode \
  --model glm-5.3-flash --reasoning-effort low \
  --protocol openai_compat \
  --max-calls 0 --app ../Alysis-Code-App-bots-main-sync
```

This command opens the saved prototype with provider calls disabled. It is useful
for inspecting conversations, files, memory and schedules without spending calls.
Sending a new task in this mode fails at the allowance; it does not contact a model.
For a deliberately bounded live session, first run the same launcher with `--check`
to read the durable count, then set `--max-calls` to that count plus the intended
small allowance. The count does not reset on restart; do not reuse an old total
from an earlier report.

The script reads the existing Alysis profile and saved key. It never creates or
replaces credentials and has no fallback provider. The current development
choice is the saved OpenCode Go profile, using GLM 5.3 Flash with low reasoning.
Refresh rejected credentials through Alysis before live testing; do not substitute
a paid provider. The launcher defaults to the `zai-coding-plan` profile but takes
its selected model unless `--model` is explicit. Earlier Haiku evidence remains
in the verification report as historical testing, not the current default.

Choose **Bots → Create bot**. The setup guide asks for purpose, instructions and
capabilities, then shows a review. Add a text file in **Files**, ask for a useful
document, and review any proposed memory in **Memory**. Each bot has one continuous
chat. Continue messaging to check that its context and approved memories carry over. **Working notes** shows project decisions,
open work and corrections; edit a note or inspect its saved revisions and source
conversation. A conflicting edit preserves your draft so you can review the
newer revision before saving. Preference corrections show what they replace and
require approval; approving one retires the conflicting original.

Ctrl+C stops the development service and leaves its database intact. The separate
desktop profile is stored under the engine checkout's ignored `.dev/bots-desktop`
directory. Closing that demo app also stops its launcher/service. This is a dev
launcher convenience, not the behavior of a future cloud bot.

## Native desktop bridge qualification

Build a separate preview in the **app** checkout:

```sh
pnpm build:bots-preview
```

Then run from the **internal engine** checkout using its own environment:

```sh
.venv/bin/python scripts/qa/bots_desktop_connection_native.py \
  --app ../Alysis-Code-App-bots-main-sync
```

The app checkout is required explicitly. The launcher saves the internal CLI
selection before opening the real Electron app; it does not use a fixture CLI
or disable compatibility checks. The default model is scripted for transport
and UI testing. `--live` uses only the saved Go endpoint and GLM 5.3 Flash low,
with no provider fallback. Credentials travel through memory/stdin only.

If Docker is unavailable, `--local-service` runs the same service source on
Mac loopback HTTP. This can qualify native integration, commands, transfers,
settings and recovery. It does **not** qualify Linux isolation or HTTPS transport.
The normal VM mode still uses verified HTTPS and publishes only to loopback.
The preview output is `.dev/bots-host-preview` in the selected app checkout;
the launcher's printed control path supports restart, Stop and revocation checks.
Closing the test with `quit` stops its owned app/service and preserves evidence.

## State and credentials

- Docker context must be the local Unix-socket `desktop-linux` context.
- API host port: **127.0.0.1:7080**, with a fresh random bearer token per launch.
- Provider credentials travel to the container once through standard input.
  They are not placed in Docker environment metadata, command arguments, files,
  renderer state, or logs. The desktop receives only the local service token.
- Durable data: Docker volume `alysis-bots-dev_bots-state`, database
  `/var/lib/alysis/bots/bots.sqlite3` inside the container.
- The existing offline desktop lab on port 6080 is independent and unchanged.
- The service is non-root, has a read-only root filesystem and no capabilities,
  and receives no host directory or Docker socket mounts.
- This service has model-provider network access. The default bot has no shell,
  arbitrary HTTP, or native desktop-input tool. The optional browser lab below
  adds guarded public-page reading.

## Optional Linux browser computer

Add `--browser-computer --build` to the launcher to enable the browser lab. It
selects `browser.compose.yaml`: the existing Linux lab namespace/seccomp settings,
1 GiB memory, 512 PIDs and a 256 MiB temporary filesystem. Chromium's own sandbox
stays enabled. Docker Compose must support `!override` (2.24.4 or later).
This remains a local development profile, not production tenant isolation.

An ordinary bot can navigate a public URL, inspect a compact DOM observation,
and retrieve its retained original in chunks. Private network destinations remain
blocked. Sessions and displays are assigned by the executor, never by model
arguments. **Bot options → View computer** shows that same browser.
Explicit reading tasks can continue in the background with their existing browser
and observations. Another message can receive a reply without taking that browser
over. Routine/event turns and coding continuations do not acquire this capability.
No browser is allocated for plain chat or just opening the viewer. Idle computers
close after three minutes; there are at most two per development service. Watching
does not extend their lifetime. Shutdown and cancellation retire owned processes.
The normal opt-in application now keeps account/bot-scoped profiles in its state
volume. Persistent cookies and localStorage survive verified clean shutdown.
Reopening starts a blank page; tabs, unsaved page state and session-only cookies
are not promised to survive. Dirty profiles are retained but refused until explicit
recovery; there is no recovery/reset UI yet. This is browser process termination
with retained disk data, not persistent VM sleep/resume. Trusted QA executors that
omit the profile store continue to be ephemeral.

The provider-free `scripts/qa/bots_browser_persistence.py` study has `seed`,
`restore`, `crash` and `reject-dirty` phases. Run them in disposable, offline Linux
containers sharing only a dedicated study volume. `crash` intentionally terminates
that disposable container without clean shutdown.

The default browser tools are read-only. For clicks or typing, the bot proposes
the task and exact website origin in the existing chat approval card. Selecting
**Do it** grants interaction only to that new task on that origin; the permission
ends when the task stops or finishes. Another task or bot cannot inherit it.
The desktop must echo the displayed origin, so older clients cannot approve a
permission they never showed. Background work retains only its own approved
permission and current observations. Network restrictions still apply: approving
a local address does not make it reachable unless the executor already owns it
as a preview. Embedded documents, sign-ins and arbitrary OS input remain unsupported.
The trusted QA preview option remains available for isolated studies.

Input dispatch does not prove that a website saved anything. Clicks allow a bounded
wait for an asynchronous page update, then return a dispatch receipt and the real
observation. The model must check the result and must not repeat an uncertain
submission. This is not an external site's idempotency guarantee.

To reproduce the real desktop/Linux study without touching the saved preview:

```sh
python scripts/qa/bots_browser_runtime.py --execute-live \
  --app ../Alysis-Code-App-bots-dev
```

This uses the saved Z.ai Coding Plan with `glm-5.3-flash`, low reasoning and a
maximum of eight model calls. Its local synthetic form permits preview interaction.
Add `--public-read` to exercise the normal read-only application wiring with a
maximum of three calls. Alternatively, `--background-read` runs a Linux API study
with up to eight calls: retain an observation across handoff, read a delayed page,
and answer two messages in the same chat while that read is still pending. It
requires no native desktop interaction and finishes by checking real Linux
cancellation/idle cleanup without additional model calls. These studies do not
create canned model responses. If Docker's
default network pool is exhausted, supply `--subnet` with a verified unused private
subnet. Ctrl+C closes only the study's desktop/container/network.

The prototype has a single configured owner. Store-level ownership checks and
per-bot scoping are tested, but this is not cloud authentication or a production
multi-tenant sandbox.

## Optional offline Linux computer

Use `--desktop-computer` instead of `--browser-computer` for the offline computer
backend. The options are mutually exclusive. It uses the same local nested
namespace/container profile, but the actual computer child has no network,
provider credentials or service database. This is still a development lab.

Ask the bot to do a computer task and review its **Do it** card. The task grant
allows bounded commands, launched apps and (with actual image-capable model
configuration) screenshot-based clicks, paste, keys and scroll. **View computer**
shows its real current screen while the task owns it. There is no manual takeover.
The model can continue the same task in background while chat stays responsive.
Stop and task completion close its apps and delete scratch files. The bot must
save useful output before replying; the computer is not a persistent VM session.

When Files is enabled, `computer_import_file` copies a library text file into
`/workspace`; `computer_save_file` saves an actual workspace text file back to
the bot's library with a source-task receipt. This avoids model transcription.
The first transfer format is regular UTF-8 files up to 100,000 bytes, with new
names only. Binary files, directories and symlinks are unsupported.

For an isolated zero-call startup check, select a new test ID and unused loopback
port. `ALYSIS_BOTS_IMAGE` can select a lane-specific image instead of replacing
the normal preview's tag. Build it with the existing cloud-dev Dockerfile first:

```sh
ALYSIS_BOTS_IMAGE=my-bots-study:local python scripts/bots-dev/dev.py \
  --desktop-computer --test-id my-desktop-study --port 7091 --check \
  --profile zai-coding-plan --model glm-5.3-flash --reasoning-effort low \
  --protocol openai_compat --provider zai_coding_plan
```

For interactive review, replace `--check` with `--max-calls 0 --app PATH`.
Enable live work only with an explicitly bounded allowance after reading the
study's durable count. Do not use a test ID/port/image owned by another lane.
The supplied model is image-capable in the qualified compatible-protocol route;
the service consults shared model capabilities rather than a model-name shortcut.

## Execution and recovery

Messages and tasks are saved together before the worker starts. A request ID
deduplicates a repeated send. Each bot has one foreground reply at a time; coding can wait durably in the background while later messages are answered. One coding job may write each workspace.
One reply worker executes queued messages across bots. A process lock prevents another
service from taking the same database.

Stop cancels the active provider request. A cancellation or broken connection may
still have incurred usage upstream, so missing cost is displayed as unknown.
Failed or interrupted turns retain their status and any already-saved files.
Tool receipts distinguish dispatch from verified results; a completed reply alone
does not prove every attempted action succeeded. On restart, queued tasks remain
queued; running foreground replies become interrupted and are not replayed.
Waiting jobs retain their identity and produce one completion report when their
actual receipt arrives.
Previously saved files and pending memory proposals remain visible for review.

There is a durable model-call allowance across restarts (launcher default 100;
the explicit preview command above uses zero), at most eight calls per task,
bounded response/context sizes, and no
automatic provider retry. This is a request allowance, **not a dollar cap**.
Increasing `--max-calls` is an explicit developer action. Failed/unknown calls also
consume the allowance because the provider might have started work.

The service uses recent complete turns and a bounded set of current note/memory
bodies, with an index and tools to read omitted bodies. History search uses literal
Unicode phrases and cursor browsing; it is not semantic search. Original source
messages survive note corrections. Memory off disables history retrieval and
working-note tools for new tasks. Pending personal memories are not injected;
memory edits/removal affect subsequent tasks. Removing a personal-memory record
does not erase the original conversation. Notes currently support up to 40 records
of 4,000 characters each; edit an existing note when that limit is reached. The
personal-memory limit remains 40 active/pending records; at the limit, correct an
existing preference through the editor or remove a record before proposing another. Existing files
cannot be overwritten by the bot; a new version needs a new name.

## Verify

```sh
pytest -q tests/test_bots_runtime.py tests/test_bots_app.py tests/test_bots_context.py tests/test_bots_process_recovery.py tests/test_bots_schedules.py
ruff check src/alysis_code/bots scripts/bots-dev/dev.py tests/test_bots_*.py
```

The API tests require the engine's `server` extra. Fixture tests are isolated from
the production service; there is no simulated-model mode in the shipped runtime.

From the desktop checkout:

```sh
node --experimental-strip-types --experimental-loader ./tests/ts-extension-loader.mjs \
  --test tests/bots-client.test.ts
pnpm run build
```

For the real Electron/Linux/model check, use the verified start command above with
`--test-live`. It creates its own bot/conversations and makes actual provider calls.
The unrelated Code-chat engine uses the repository fixture to protect existing
desktop chats; the Bots service and model are real. Screenshots and a usage report
are saved under the desktop checkout's `test-results/bots-live` directory.

Set `ALYSIS_BOTS_E2E_MEMORY=1` with `--test-live` for the focused memory check.
It exercises real note creation/correction, cross-conversation recall with source
links, preference replacement, and stale user edits. Set both that variable and
`ALYSIS_BOTS_E2E_READONLY=1` to verify the saved report's bot after container
replacement without additional model calls. The memory run used a total database
call allowance of 45; the limit is durable, not reset at each launch.

Database schema v4 adds exact activity/read receipts on top of v3 schedules,
creation request IDs and run receipts, preserving earlier memory, notes and conversations. Use the current runtime after
migration; older versions reject unsupported schemas rather than resetting data.

## Schedules

Open **Bots → your bot → Details → Schedules**. Create a one-time, interval (at least five
minutes), daily or weekly task. Calendar schedules require an IANA timezone;
one-time fields show the desktop's local timezone and store an absolute instant.
Ask in chat to get a draft; review the actual prompt and time before enabling it.
The model can only create drafts, not enable or edit schedules. At most 20
schedules are stored per bot in this stage; edit an existing one at the limit.

The Linux service must stay running. Each run joins the bot's continuous chat
and uses its conversation, current capabilities and enabled context. Runs do not
add browser, email or coding powers. View runs for actual task state, original
instructions, skip reasons and a link to the result conversation. Pausing
prevents new dispatch; stop already queued/running work in its conversation.

Occurrences over 15 minutes late are missed. Missed backlogs are not replayed;
a delayed interval within the grace window queues only one occurrence. An active task for the same bot
or queue capacity skips an occurrence. Failures/interruption pause an unchanged
schedule; an exhausted allowance pauses without creating another task. Daily and
weekly times that do not exist during clock changes are skipped; repeated times
run once. Resuming an interval starts a new interval from the time of resumption.

These are Bots schedules; the existing top-level **Scheduled** page still uses
the desktop window and is not migrated to the Linux runtime.

Use `--test-schedules` instead of `--test-live` for UI and Linux allowance checks
without provider calls. It sets the service allowance to zero before startup, as does `--check`,
so neither mode can contact the provider even on a fresh database. Queued tasks
cannot execute in this mode and due schedules pause at the allowance. Use the
development volume, without pending work you want to execute. Test schedules
remain paused. Set
`ALYSIS_BOTS_E2E_SCHEDULE_LIVE=1` with `--test-live` for the opt-in GLM schedule
check: chat draft, UI activation, correction after drafting, actual scheduled
execution, artifact and run history. Reports go to `test-results/bots-live`.


Use `--check` for a startup check that makes no model call. Never run
`docker compose down -v` against the development volume unless its saved data is
intentionally disposable.

Set `ALYSIS_BOTS_E2E_SCHEDULE_PERSIST=1` with `--test-schedules` after a completed
live schedule check to verify its run, conversation and artifact after container
replacement. This starts with a zero provider-call allowance. The original
request accompanies model-drafted runs as provenance; manual schedule edits
supersede that source for future runs.

For incomplete-turn continuity, set `ALYSIS_BOTS_E2E_INCOMPLETE_SEED=1` with
`--test-schedules` to create an actual zero-allowance failed task through Electron.
Then restart with `ALYSIS_BOTS_E2E_INCOMPLETE_LIVE=1`, `--test-live` and a total
`--max-calls` at most three above the current durable ledger. This uses the saved
GLM Coding Plan to recall the exact request and status with memory disabled; it
must not resume work or create artifacts. Each seed admits only one live follow-up.
`ALYSIS_BOTS_E2E_INCOMPLETE_REVIEW=1` with `--test-schedules` checks the saved reply,
rendering and persistence with zero model calls. Unique raw reports retain errors;
the review never sends again. Markdown JSON code blocks are recorded distinctly
from strict raw JSON. See the verification document for the first test-wait race,
interrupted unknown-usage attempt, and separate successful content verification.


## Review the overnight prototype without new provider calls

Use the explicit zero-call preview command above to inspect the saved work. At
the final database review, eight schedules were paused and three had ended;
none were active. `--test-schedules` instead
runs the baseline automated UI check; it also forces zero allowance before startup.
The following optional environment flags select focused tests with that mode:

| Flag | Evidence |
| --- | --- |
| `ALYSIS_BOTS_E2E_PARTIAL_REVIEW=1` | Saved GLM partial artifact, exact file content and source navigation |
| `ALYSIS_BOTS_E2E_DETAIL_NAVIGATION=1` | Late result/source responses and correct file selection, reusing saved artifacts |
| `ALYSIS_BOTS_E2E_SOURCE=1` | Original source anchoring, unread status and unsent composer drafts |
| `ALYSIS_BOTS_E2E_RUN_HISTORY=1` | Real schedule-history pagination and delayed response isolation |
| `ALYSIS_BOTS_E2E_DETAIL_DRAFTS=1` | Detail save races and a real working-note revision conflict |

These are real Electron/Linux tests, but do not call a model in this mode. Some
create synthetic records, paused schedules or zero-allowance failed tasks; they
are not all read-only. Saved-artifact checks require their recorded seed data in
`test-results/bots-live`. Missing seeds are a prerequisite error, not permission
to silently spend new calls. See the [Bots development guide](../../docs/bots.md) for source
entry points, contracts, and operating procedures.


## Continuous chat verification (18 October)

The current UI offers one continuous chat per bot. Old chat IDs still resolve,
but messages and tasks from those chats appear in one chronological timeline.
Creating a chat again returns the same ID. Source links retain the original task
and message IDs; no original conversation records are migrated or deleted.
Scheduled runs use that same chat and skip an occurrence if the bot is already busy.

`--test-schedules` now checks this layout and the actual Linux scheduler with
provider calls disabled. `ALYSIS_BOTS_E2E_SINGLE_CHAT=1` with `--test-live` checks
persisted legacy source evidence, two actual chat replies, and a scheduled recall.
It requires the earlier `source-seed.json` evidence and a database with legacy
side-chat messages, and accepts at most four remaining provider calls. Read the
current usage before choosing the total `--max-calls` allowance.

Older opt-in multi-conversation research scripts (memory, activity, source and
composer studies) describe the prior UI and are historical evidence, not current
single-chat acceptance checks. The current workspace, schedule and focused
single-conversation checks exercise the supported interface.

### Coding workspace preview

Add `--coding-workspace /absolute/path/to/project` and one or more
`--coding-verify 'command'` arguments to the launcher. Select that workspace in a
bot's **Details → Coding**. This grants edits/checks only in that directory; one
bot owns the workspace at a time. The launcher starts a trusted host AlysisCode
controller whose commands run in isolated Linux containers. Provider credentials
remain in the Linux Bots controller. No-check work is allowed but cannot claim
independent verification.

Coding receives five execution model calls plus one report call, within the same
parent/service allowance. Partial or unverified work is shown honestly; Stop and
restart do not automatically replay it. Full cloud execution, dependencies that
need network access, larger workspaces (over 20 MB/5,000 files), and arbitrary
background commands are outside this development slice.

Automated tests use a separate Compose project/volume and port 7081 by default;
`--test-id NAME` reuses only that isolated state. The user preview stays on 7080.
Do not use the user's preview to seed integration test bots. The test-only slow
command and crash check is `PYTHONPATH=src python scripts/qa/bots_coding_recovery.py`.

## Managed Linux workspaces

Use `--linux-worker --keep-service-on-close` to run the trusted coding controller
in a separate Linux container. Every bot without an explicitly bound workspace
gets an empty, persistent workspace in the `coding-state` volume. Existing
operator bindings are preserved; no host directory is selected implicitly.
Commands use hardened, networkless bubblewrap and never receive the model key,
controller capability, Docker socket, peer workspace or private session state.

The controller uses the same nested namespace seccomp/systempaths profile as the
existing local lab. This is a development configuration, not qualified production
tenant isolation. The model-facing service keeps its stricter container profile.
Closing the desktop with `--keep-service-on-close` keeps both Linux services alive;
Ctrl+C in the launcher stops them. The local Mac must remain awake. The development
launcher still supplies session credentials; this is not cloud account auth.

`--coding-workspace` remains available only for explicitly selected host fixtures
and cannot be combined with managed workspaces. It uses the old host controller
for regression tests. `scripts/qa/bots_linux.py` exercises the managed route with
a fixture provider, actual bwrap commands, local Git and Electron disconnect and
reconnect. It does not connect GitHub or spend provider calls.

## Follow-up suggestions

The bot can propose one concrete next action while replying or reporting work.
Its chat card shows the exact task before **Do it** enqueues it once. **Dismiss**
keeps it declined; **In an hour** defers the card. Pending suggestions consume no
model calls. New messages or changed bot settings invalidate old pending scope.
Approved schedules can produce these questions while the desktop is closed.
There are no external notifications or connected-app event triggers yet.

### Automatic activation on file additions

Ask a bot to review files whenever you add them, then use **Enable** on its chat
card. **Bot options → Automatic activation** lists rules and recent deliveries,
and lets you create or pause a rule directly. The automatic turn can read and
reply or offer a separately approved action. The current source is additions to
that bot's text-file store, not arbitrary filesystem changes or connected apps.

Event delivery is persisted with uploads, survives service restarts and requires
no model calls while idle. Files generated by a task do not emit this source.
Nearby uploads are grouped for five seconds, runs have a one-minute cooldown and
a development limit of 12/day/rule, and pending events expire after 24 hours.
Busy queues and exhausted model budgets preserve pending events until capacity
returns or expiry. Pausing cancels pending/queued reviews, signals running ones,
and invalidates unaccepted suggestions. Re-enabling starts with future events.

The isolated live check (saved Coding Plan only) is:

```sh
ALYSIS_BOTS_E2E_ACTIVATIONS=1 PYTHONPATH=src python scripts/bots-dev/dev.py \
  --profile zai-coding-plan --model glm-5.3-flash --provider zai_coding_plan \
  --protocol openai_compat --reasoning-effort low --max-calls 10 \
  --test-live --build --app ../Alysis-Code-App-bots-dev
```

This creates isolated test state and can consume up to ten provider calls. It
checks an event arriving with Electron closed, actual file reads, approval-only
writes, no feedback from bot outputs, and pause. It does not connect a plugin,
validate a public webhook or deploy a hosted service.


## Current routine and background-work interaction

Use **Bot options → Routines**, or ask directly in the continuous chat. Explicit
requests can create enabled schedules/file watches and edit/pause/resume existing
ones. Own-initiative suggestions remain drafts. New routine and Edit in chat fill
the ordinary composer; old schedule controls/run history remain secondary.

Long non-coding turns can continue on a separate worker with their current context,
while quick questions stay direct. This currently covers supplied text/files and
memory tools, plus read-only browsing when `--browser-computer` is enabled. It does
not add connector access. Schema9 preserves
queued background checkpoints; an interrupted in-flight turn is not replayed.
Model-call allowances are shared with the initial turn and remain durable.

`ALYSIS_BOTS_E2E_ROUTINES=1` with `--test-live --max-calls 20 --test-id NAME`
checks real model-chosen background work, concurrent reply, exact context, routine
creation/pause in chat and file-event activation with Electron closed.
`ALYSIS_BOTS_E2E_ROUTINES_UI=1` with `--test-schedules --max-calls 0 --test-id NAME`
checks schedule controls, chat editing and composer draft preservation without
model calls. Use a new isolated test ID for each check. Neither uses preview data.

### Shared cloud connections: current boundary

`alysis_code.cloud_connections` provides durable connection metadata and independent
bot/main-agent grants. `GitHubReader` accepts a trusted token callback; tokens are
never written to the connection store. Registration is a trusted server operation
after verified installation authorization, not an HTTP/model capability. The
normal development launcher does not configure this reader. Existing desktop
accounts are not automatically cloud connections. GitHub authorization/token
issuance and the shared Plugins UI remain to be implemented.

To verify the HTTP path without accounts, credentials or external network access,
rebuild the image and run:

```sh
docker --context desktop-linux run --rm -i --network none --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges:true --cpus 1 --memory 512m --pids-limit 64 \
  alysis-bots-dev:local python - < scripts/bots-dev/check-connections.py
```

This uses a real Linux Bots HTTP service and a separate loopback HTTP server with
synthetic GitHub responses; its model is a fixture. It checks shared access,
separate bot grants and in-flight revocation. It is not live GitHub/model evidence.

## Reliability and generated files (2026-11-03)

Native command approvals now offer **Allow once** or **Always allow for this
session** for the selected Bot. Session permission ends on Disconnect or app
quit/restart; **Ask for each command** clears it immediately. Other Bots and
file-transfer requests still ask.

The hosted private bootstrap
requires `hosted.browser_state_key`, a stable base64-encoded 32-byte key supplied
from trusted secret storage through stdin. Reuse it on restart; keep it separate
from backups and out of repository/configuration files. A missing or wrong key
must not silently replace an existing profile.

Build the disposable Linux image from `scripts/cloud-dev/Dockerfile`; then run
`scripts/qa/bots_reliability.py` with this checkout's Python. The study uses a
synthetic loopback sign-in site and actual Chromium/LibreOffice. It makes no model
calls and retires only its owned VMs. `--files-only` exercises file checks without
repeating browser continuity. This is local Linux qualification, not an Azure
deployment or Windows check.
