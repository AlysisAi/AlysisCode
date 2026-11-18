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

A person's message can attach up to 10 Library files: `POST /v1/chats/{id}/tasks`
takes optional, unique `file_ids`, each a file of that owl's Library (an owl with
file access off refuses them). The same `request_id` with other files is a conflict.
`/v1/status` lists `message_files` in `capabilities` so apps can check before
sending. Each user message in a conversation carries `files` (id, name, media type,
size as sent, and `available`); deleting a file later leaves the message readable.
The owl reads the list after the person's words, as data, and opens the files with
its file tools.

### Time zone

`PUT /v1/settings/time-zone` with `{"time_zone": "<IANA name>"}` saves the person's
time zone for their account (validated with `zoneinfo`; the host's `localtime` and
names that aren't places are refused). The app sends its computer's zone when it
connects and whenever the zone changes, after checking that `/v1/status` lists
`time_zone` in `capabilities`; `/v1/status` also returns the saved `time_zone`, so it
sends only when that differs. Every turn reads the setting when it runs: its task
state keeps `Task start UTC time` and adds the person's local weekday, date, time,
zone and UTC offset, so "today", "tomorrow" and "9 AM" follow the person, and the
next turn after travel or a daylight saving change uses the new local time. A run
of a daily or weekly routine reads dates on the routine's own zone, and owls
answering another owl get the same line. With no zone saved, turns say they are in
UTC and that the person's zone is not known; nothing guesses it from language or
locale. `create_schedule` and `propose_schedule` fill a daily or weekly timing's
missing `timezone` with the person's zone; `edit_schedule` keeps the routine's
current one. With no zone saved, a timing without one is refused with a request to
ask the person.

### About me

About me is what the person wrote about themselves, shared by all their owls: up to 30
one-line facts of up to 200 characters each, in the person's order. The person's app
keeps the copy that counts and writes it whole to each place their owls run, after
checking that `/v1/status` lists `about_me` in `capabilities`:
`PUT /v1/settings/about-me` with `{"facts": [...], "version": "<app's name for this
content>", "expected_revision": <revision it read>}`. `/v1/status` returns the stored
`about_me_version`, so the app writes only when it differs; a write from an outdated
read is refused (409). `GET /v1/settings/about-me` returns `facts`, `version`,
`revision`, `added`, `included` and `limits`. It is kept per owner and never reaches
another person's owls.

Every turn of the owner's owls reads it (chat, routine runs, first hellos, carrying on
after a skipped question, and owls answering another owl of the same person), beside
the owl's setup and labelled as the person's own words, apart from the owl's memories.
It is read within an 800-token budget: facts are taken in order until the next would not
fit, and the rest are left out and counted (`included` says how many fit).

Owls never add to it themselves. In a chat turn with the person (not routines, event
reviews, hellos or exchanges), once the app has written an About me, an owl may call
`suggest_about_me` with a lasting fact the person stated (at most three per reply; a fact
already in About me or suggested before is not offered again). The reply's task carries
`about_me_suggestions` (`id`, `fact`, `status` pending or accepted).
`PATCH /v1/bots/{id}/about-me/suggestions/{id}` with `{"decision": "accept"|"decline"}`
decides it: accepting adds the fact here at once (refused with 429 when About me is full),
clears `version` and lists the fact under `added` until the app writes its copy back;
declining adds nothing.

### Failed turns and safe retries

A failed or interrupted task carries `failure`: a closed `kind` (`sign_in`, `model`,
`plan_limit`, `computer`, `network`, `service`, `task_limit`, `unknown`) and, when the
model service answered, its HTTP `status` and closed error `code` (`failures.py`). The
kind is set where the turn fails, from exception types, provider status and codes and
whether the model is the person's Alysis account; never from error wording. The task's
`error` stays the technical text, and the terminal event repeats the failure. Apps check
`task_failure_kinds` in `capabilities`; tasks from older services have no failure.

An owl may carry `color` and `background` (capability `owl_looks`): the app's short names for the
look the person picked for its face (lowercase letters, digits and hyphens). The app draws the face
and treats a name it doesn't know, or null, as no choice: a colour from the owl's name on the soft
tile of that colour. Like the name, a look is a label: `PUT /v1/bots/{id}` changes it at once, even
while the owl works, and a `PUT` that leaves a part out keeps it. Needs you items carry
`bot_color` and `bot_background`.

`POST /v1/bots` takes an optional `request_id` (`bot_create_request_id` capability). The
same id from the same owner returns the owl that request made; the same id with other
details is a conflict. Only a hash of the details is kept, and deleting the owl erases it.

Each task in a conversation lists `decided_actions`: the actions a person approved or
declined, with their tool, arguments and observed context but no screenshot.

### Finding things

`POST /v1/chats/{id}/page` (capability `conversation_pages`) reads an owl's one
conversation by message id instead of whole. The latest page holds the newest `limit`
messages, every task they belong to, every task started since its first message and every
task still working, so the app can match a message it just sent to its task. `earlier`
names the message to read the page before from (`before`), or is null at the start;
`through_task` reaches back to a task's first message. A poll sends the `version` the last
latest page returned and the newest message it has (`after`): `continues` then means the
messages follow on and the tasks are only those that changed (a task fingerprint differs),
and `unchanged` means nothing did; otherwise the page replaces what the app had. Clients
without the capability keep reading `GET /v1/chats/{id}`.

`GET /v1/needs-you` (capability `needs_you`) lists what waits for the person across every
owl of the service in one read, newest first: pending questions and approvals, a suggested
next step under its reply, approval on this computer, help on an owl's computer, routines
that paused themselves (a `pause_reason`) or wait to be turned on (an owl's draft), and an
owl's newest turn when it failed or was interrupted and is unread. Routine runs show as
their paused routine. Items carry the owl, the task to open and the owl's own words where
there are some; apps word the rest in the person's language.

`POST /v1/bots/{id}/history/search` matches Unicode-normalised, case-folded text, newest
first. Only when nothing in the owl's history matches exactly does it also ignore combining
marks (accents, Japanese voicing marks), compatibility forms and spacing; `match` says
which. Over HTTP each result carries an `excerpt` (`before`, `match`, `after`) cut from the
message's own text; the owl's history tool reads previews without excerpts.

Routines (schedules) run once, every N minutes, daily, weekly, monthly (a day of the
month; a month without that day runs on its last day) or every N minutes (at least
15) inside a daily window on chosen weekdays. Times follow the routine's IANA time
zone: daily, weekly and window times the clocks skip are skipped, repeated ones run
once, and a monthly time the clocks skip runs once they have moved. A failed run
does not pause its routine at once. A busy or unreachable model service is retried
after 5, 15 and 45 minutes while that is before the next run, unless the run had
already taken a step; a plan or usage limit holds the routine's runs until the limit's
own reset time (or tries again after an hour, then longer); a failure only the
person can fix (sign-in, a refused setting or permission, an action it could not
confirm, an approval still waiting when the next run comes due), or three failed
runs in a row, pauses it with `pause_code` and a `pause_reason` saying what to do.
The failure kind comes from the run's status and the error's type, HTTP status and
provider code, never its wording. `GET /v1/bots/{id}/schedules` adds `upcoming_runs`
(the next three times), `active_run`, `retry` and `failed_runs`;
`POST /v1/bots/{id}/schedules/{id}/run` with a `request_id` runs one once now
without changing its schedule, and the same `request_id` never starts a second run.
`/v1/status` lists `schedule_run_now`, `schedule_next_runs`, `schedule_monthly`,
`schedule_windows` and `schedule_retries` in `capabilities`.

A routine may use the owl's offline computer, its online browser and named website origins
only as the person allowed them for that routine (capability `routine_access`). The owl
declares a routine's `needs` (`offline_computer`, `online_browser`, `sites`) when it
creates, suggests or edits it; a schedule lists them with `access`: `allowed`, `requested`
(what a run asked for beyond that) and `to_allow`. `PUT /v1/bots/{id}/schedules/{id}/access`
allows exactly `to_allow` at the schedule's `expected_revision`, with `turn_on` and a
`run_request_id` for Allow and run again; `DELETE` revokes. Each run keeps what was allowed
when it was queued (its `config.access`), so a grant or revoke applies from the next run.
A run that asks for more with `propose_action` gets no card: the request is recorded and the
routine pauses with `pause_code` `access`. A changed job or changed needs clears what was
allowed. Website actions that need review still ask; activations and peer reports still get
no computer or browser.

### Apps per owl

The desktop app's plugins reach an owl's service through Electron main
(`/v1/desktop-plugins/*`). `GET /v1/bots/{id}/plugins` (capability `owl_apps`) lists every
app the app's last catalog named, with `connected` and whether it is `enabled` for that owl;
`PUT` with `plugin_id`, `enabled` and the `expected_revision` shown changes one. Every app is
on until the person turns it off, for new owls and owls from before. An app off for an owl is
never offered to it (chat, routine and exchange turns alike); switching it off mid-task takes
effect from the next step, and a step already chosen is refused before anything is sent, with
the coded refusal `app_off`. A service with the capability takes `apps` in the catalog: every
app owls could use once connected. An owl asks for one that is off or not connected with
`need_app` (the app's id from an enum, never words in a message); the task's `app_needs` carry
the card and Needs you lists it (`kind: "app"`). `PATCH /v1/bots/{id}/app-needs/{id}` answers
it: `turn_on`, `connect` (carried on once the next catalog shows the app connected) or
`dismiss`. Turning it on or connecting it queues a turn with no new message (`config.app_ready`,
request id `app_ready_<id>`) when the request is still the newest in its chat.

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
