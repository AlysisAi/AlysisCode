# Bots development guide

Bots (Owls in the desktop app) is shared engine code. App integration uses its own
reviewed engine pin; an engine merge does not update an installed app or publish a
release. See [desktop engine contracts](desktop-engine.md).

## Product and execution boundaries

- Each bot has one continuous conversation. Bots are peers. A person can choose one
  owl in each runtime to coordinate assignments using the same permission boundaries.
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

### Owl assignments

Services with the `owl_assignments` capability let the person select one existing
coordinator for their owner on this runtime with
`PUT /v1/bots/{bot_id}/coordinator` (`{"enabled":true}`). The coordinator can propose one
assignment per direct request with a same-owner recipient, brief, and expected result.
`GET /v1/bots/{bot_id}/assignments` lists the owl's coordinator status and newest outgoing
assignments. Follow `next_before` with `POST /v1/bots/{bot_id}/assignments/page`
(`{"before":"<cursor>"}`) to read older items. A proposal is ready after the originating turn
finishes. The person can
approve or decline it with `PATCH /v1/bots/{bot_id}/assignments/{assignment_id}`
(`{"decision":"approve"}` or `{"decision":"decline"}`). Repeating approval returns
the same task. Changing the coordinator cancels its pending proposals and keeps
already approved work and results.

An assignment runs as a separate task for the recipient, with its own usage and
stop state. It reads the scoped brief and expected result. Text assignments run
without tools. It does not
inherit conversations, attached files, connected apps, computers, or authority to
delegate. The coordinator's assignment list shows the result and the verification
verdict when available. If the service stops after recording the result and before
finishing verification, the saved result remains visible with no verification verdict.
The existing task cancel endpoint stops the assigned task. Deleting either owl
removes the assignment, after active assigned work has stopped.

### Assist

Services with `owl_assistant` expose `GET /v1/assistant` and
`PUT /v1/assistant` (`{"enabled":true}` or `{"enabled":false}`). Both return the
enabled state and the built-in owl, or `bot: null` before its first creation.
Assist starts off. The first enable creates one Alysis Assistant for the owner
on the current runtime and makes it the coordinator in the same transaction.
Enabling does not queue a first message or any other work. Re-enabling uses the
same owl and history. Alysis Assistant has its own slot alongside the 30
ordinary owls.

The built-in owl is marked `managed_assistant: true` and carries its current
`assistant_enabled` state in the bot view. Its name, purpose and instructions
come from the runtime. The person can change its look and memory or file
preferences. New direct messages require Assist to be on. Assignments must use
another owl as the recipient. While Assist owns the role, assignment responses
add `assistant_coordinator: true`, and a manual coordinator cannot replace it.

Assist can group related questions in a clarification card and ask a follow-up in
the same chat. It can retain confirmed project decisions in editable working notes
and use them to brief another owl. The person reviews the resulting assignment or
new-owl draft before work starts.

Desktop clients that support `owl_assistant_screen_requests` can send
`assistant_screen_requests: true` on an Assistant message. When screen context
would help, the model can attach an `assistant_screen_request` with a short goal
to the completed task. The desktop presents the request for approval before it
takes a screenshot. The request itself never captures or uploads an image.

Turning Assist off restores the previous manual coordinator if it still
exists. The same role transition cancels unapproved drafts and pauses active
assignment routines for the outgoing coordinator. That coordinator's already
approved work and saved history are kept. Turning Assist back on does not resume
paused routines. Deleting the built-in owl through the usual owl deletion
flow also releases the role and removes the assistant record.

Services with `owl_assistant_create_owl` let the person approve a new owl proposed by Assist.
Completed turns with a new-owl draft include `assistant_owl_draft` in the conversation response.
`POST /v1/assistant/{bot_id}/create-owl` takes the reviewed `name`, `purpose`, `instructions`,
`brief`, `expected_result`, `start_task` and `request_id`. It returns `{bot, assignment}`.
Creating the owl and optionally queuing its first assignment commit together. Repeating
the approved request returns the same owl and assignment. The result is available through
`GET /v1/bots/{bot_id}/assignments/{assignment_id}` on the coordinating Assistant.

Services with `owl_assistant_assign_existing` also accept
`POST /v1/assistant/{bot_id}/assign` with the reviewed `recipient_bot_id`,
`brief`, `expected_result` and `request_id`. The recipient must be an existing
ordinary owl on the same owner and runtime. The response contains the selected
owl and its queued assignment. A retry with the same reviewed text recovers
the same task. Read its status through the same endpoint as a new-owl handoff.
Approved Assistant handoffs appear in the recipient's conversation. The task
includes the approved brief, expected result and any verification verdict in
`assistant_handoff`. Its reply follows the recipient's normal unread behavior.

The optional `work_scope` on assignment and new-owl requests defaults to `text`.
The approval for a proposed assignment repeats its scope. `web_research` allows
search and reading public pages (`owl_assignment_web_research`). `workbook`
creates a new .xlsx (`owl_assignment_workbook`) from the approved
brief if the assigned owl has Library access. The completed assignment includes its
`artifact_id` for download from the recipient's files. Both scopes are confined to
the approved task and share the existing task model-call limits.

### Assignment routines

A service with `owl_assignment_routines` lets the current coordinator's owner
schedule a completed original assignment from Owl assignments.
`POST /v1/bots/{bot_id}/assignments/{assignment_id}/schedule` takes a routine name,
timing and request ID. This saves a paused routine. The person turns it on in
Routines. The routine keeps the original recipient, scope, exact brief and expected
result, so a regular prompt edit cannot change what was approved.

Each occurrence queues one real recipient task in the same transaction that
records the routine run. The worker gets the current run's time with the approved
text and uses the same approved tools and result check as a one-time
assignment. Run status appears in Routines and the draft and review appear in Owl
assignments. The routine pauses when it loses its coordinator or recipient, or
when a completed draft needs review. An interrupted run preserves any saved draft
and follows the existing scheduler's missed-run policy.

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

Owls work numbers out of Library tables with `query_tables` (`owl_tables.py`), one
read-only SQL query over the files named by id, instead of adding up rows they read.
Each file becomes a table by its content, never its name: an Excel workbook (a table
per sheet; a single sheet is named after its file), a JSON list of objects, or
delimited text whose delimiter is sniffed. Row 1 is the header. Names are made simple
to write (NFKC, lower case, letters and digits of any script kept, everything else
`_`), and each result lists the tables with their original headers. A column is
numeric only when every filled value reads as a plain whole or decimal number, so
values such as `1.234,56` stay text for the query to convert. The database lives in
memory for the call; an authorizer allows only reading (SELECT, READ, functions and
recursion), so ATTACH, PRAGMA and writes are refused. A query stops after 20 seconds
or at the task's Stop; at most 500 rows (100 by default) and about 40,000 characters
are shown, the rest counted. Every table of one call together holds at most
2,000,000 values.

### Memory after the chat

Besides `propose_memory` during a turn, an owl keeps what the person states in passing once its
own chat has been quiet for two minutes after a turn of the person's that finished with a checked
reply (capability `memory_capture`, schema 50, `memory_capture.py`). The scheduler wakes a memory
lane that reads only when nothing of the owl is queued, working or waiting: one model call
without tools, on the owl's model, recorded as a provider call of the newest turn read and made
only when the workspace allowance keeps two calls for the person. It reads the person's own
messages, a message they sent to a running turn as part of that turn (never replies, tool results,
web pages, files, routine or engine-made turns such as check-backs, rooms, or turns that ran with
memory off) since the last read: the newest 8 turns, 2,000 characters each,
8,000 in all, with a bounded selection of saved memories. Of at most five facts it keeps one only
when its quote occurs in one of those messages (Unicode-normalised, case-folded and
whitespace-collapsed, without accents only when nothing matches exactly) and nothing saved or
offered already says it; `updates_memory_id` replaces that memory through the ordinary replace
path. Kept facts behave like an owl's own saves (Undo, under the reply they quote) and carry
`captured: true`. A new message, archiving, deleting, memory off or a service stop cancels a read
and nothing is saved. The cursor is an event seq, so a moved owl never reads the turns it brought
again, and an owl starts at its first qualifying turn. Once a day an idle owl's memories with the
same normalised content are merged: the copy is superseded by the one kept (the person's own
first, then the oldest), never deleted, and Undo of the one kept brings it back. Contradictions
and expiry are left to the person's own updates, which the read catches.

### Playbooks

A playbook is a short procedure an owl writes for a job that comes again (capability
`owl_playbooks`, schema 45): a name, when to use it, up to 12 steps, up to 6 pitfalls met and one
check that proves the job is done, plus the tools the run used, taken from its receipts.
`save_playbook` is offered only with memory on, in turns whose reply goes through the reply
checker (not a first hello or a room), and refuses a run that has completed no tool yet. What a
run writes is held with it and kept only when the run finishes completed with a reply that
passed the check as a reply (not a plain status), with no question or approval card of the run
still waiting; any other end drops it (event `playbook_dropped` with a reason code). The turn
that kept one carries `playbook` (`id`, `name`, `version`, `current`).

Later turns and routine runs see an index (name, when to use) in their task state and read one
with `read_playbook`. `replaces_playbook_id` saves a new version; Undo takes back the current
version and restores the one before (or forgets a first version). Per owl: at most 20 playbooks,
three earlier versions each, one playbook per run, and no two of one name (Unicode-normalised
and case-folded, without accents only when nothing matches exactly). `GET
/v1/bots/{id}/playbooks` lists them (`earlier_versions` says whether Undo restores one), `GET
.../playbooks/{id}` reads one with its versions, `POST .../playbooks/{id}/undo` (body `version`,
the one the person saw) takes back the latest, and `DELETE` removes one. Playbooks move with an
owl (record kind `playbook`, sent only to a place that lists the capability) and are erased
with it.

### Checking back

An owl that has to wait never waits inside its turn: `check_back` (capability `check_back`,
schema 47, `check_backs.py`) keeps a time 1 minute to 30 days ahead (`after_seconds`, counted by
the service, or `at` with its offset), a note for the future turn and an optional condition, and
the turn ends. Only the person's own message, or a turn a check-back started, is offered it and
may set one (never a routine run, file event, background work, carried-on work or a room); at
most 10 wait per chat. The service's scheduler checks conditions with no model call, backing off
(own records: 5 s doubling to 60 s; pages: 60 s doubling to 30 min): a public page shows
(`page_contains`) or no longer shows (`page_lacks`) a text, read through the owl's web lookup and
its address rules and matched Unicode-normalised, case-folded and whitespace-collapsed (without
accents only when nothing matches exactly; a page read only in part never counts as lacking it);
a Library file added since, by a name pattern (`*`, `?`); or the owl's background, coding or peer
work ending (`work_ends`). It then queues one turn in the same chat (request id
`check_back_<id>`, no message from the person) and saves `starting` with it in one transaction,
so a restart never starts it twice; a check-back due while the service was down starts once, late.
The turn's request is the app's note ("You asked to check back: ..."), saying whether the
condition held or the time ran out, and how late it is; it carries the person's request on but
qualifies for nothing only their own message gives (`Store.direct_request`), and the owl's limits
and approvals apply. An archived owl's check-backs rest until it comes back. The turn that set
one carries `check_backs`; the turn one started carries `checked_back` (`id`, `note`, `outcome`:
`time`, `met` or `deadline`). `GET /v1/chats/{id}/check-backs` lists those waiting and `POST
.../check-backs/{id}/cancel` cancels one. Waiting ones move with an owl (record kind `check_back`,
and a turn's `config.check_back`, sent only to a place that lists the capability; work it waited
for has ended by then) and are erased with it.

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

### Messages that wait, and reactions to replies

A message the person sends while their owl works waits as a queued task. Each message sent with
`POST /v1/chats/{id}/tasks` carries `queued_message` (`revision`, `editable`, `edited`,
`withdrawn`), and every task carries `started_at`, when the owl first started it (null while it
waits), so an app can show a waiting message where it belongs (capability
`queued_message_edits`). Until it starts, `PATCH /v1/tasks/{id}/queued` with
`{"action": "edit", "expected_revision": n, "message": "..."}` changes its words (its files stay;
the owl reads the new words), and `{"action": "cancel", "expected_revision": n}` cancels it: the
task ends as cancelled, its message leaves the conversation and the owl's context, and the chat's
activity stays as it was. The answer's `outcome` says what the message is now: `edited`,
`cancelled`, `started` (the owl started it first, so nothing changed) or `changed` (it changed
since `expected_revision`, so nothing changed). A change and the worker starting the task are
serialised write transactions, so they never both happen; the same change again gives the same
answer, and sending the original request again with its `request_id` is still that one message.
Answers to an owl's question, routine runs, activations, accepted suggestions and hellos have no
`queued_message` and are refused (409).

While the owl works, `{"action": "steer", "expected_revision": n}` on the same endpoint sends a
waiting message now to the turn running in the owl's own conversation, when that turn is the
person's own request (capability `queued_message_steer`; `bots/steering.py`). The turn reads it at
its next model round, never inside a batch of tool calls, as the person's own message (user role,
their exact words and the files they attached): a response the model was writing meanwhile is
neither run nor published (no tool from it ran yet), so a reply about to go out is held and the
turn goes on. The message is then part of that turn: its task ends as completed without a reply of
its own, later turns read it inside the turn it steered, before that turn's reply, and it is not a
turn of its own for activity, Needs you or regenerate. The reply checker gets its words as
`messages_sent_during_task`, and the action reviewer as later user messages. The answer's
`outcome` is `steering` (on its way), `steered` (already read) or `queued` with a `reason`
(`not_working`: no turn runs there; `not_steerable`: the running turn is a routine run, an
activation, a hello, carried-on work's report or a room's turn; `full`: that turn has 5 messages to
read already), in which case nothing changed and the message waits its turn; `started`,
`cancelled` and `changed` as for a change. The message's `queued_message` then carries `steer`
(`state` `pending`, `applied` or `queued`, and the steered turn's `task_id`), its `started_at` is
when the turn read it, and its events are `steer_received`, then `steer_applied`, or
`steer_queued` when the turn stopped running (published its reply, failed, was stopped or went on
in the background) before reading it: then the message runs as the next turn, as if never sent
now. It can be edited or cancelled until the turn reads it, which reads its words as they are
then. The steered turn gets `steer_received` and `steer_applied` (`message_task_id`) too.
Background work keeps its own corrections (`update_background_task`) and isn't steered. A move to
the person's other place carries a steered message as a turn of its own.

`PUT /v1/bots/{id}/messages/{id}/reaction` with `{"reaction": "up"|"down", "note": "..."}` (a note
of up to 300 characters, optional) reacts to one of the owl's replies, and `DELETE` takes the
reaction away (capability `reply_reactions`). The reply's task carries `reactions` (`message_id`,
`reaction`, `note`, `updated_at`). Every later turn of that owl reads its newest reactions in the
task state, newest first, each with the start of the reply and the note: at most 8 and 800
tokens, the last one shortened when it doesn't fit whole, with older ones counted. They are
labelled as the person's feedback, never as instructions that override the owl's guidance,
safety rules, permissions or the current request. Deleting the owl erases them.

### Rooms: several owls in one conversation

A room is one conversation the person has with two to six of their owls in one place
(capability `owl_rooms`, schema 44). `GET /v1/rooms` lists the person's rooms and
`GET /v1/rooms/{id}` reads one: `name`, `owls` (`bot_id`, `name`, `color`, `background`, in their
order), `open` (false with fewer than two owls), `activity_seq`, `unread`, `latest_activity`
(with the `bot_id` of the owl it is from), `active_tasks` and `asks_you`. `POST /v1/rooms` with
`{"name", "bot_ids"}` makes one, `PATCH /v1/rooms/{id}` with `name` and/or `bot_ids` (all of its
owls, in order) renames it or changes its owls (an owl still working there can't leave), and
`DELETE /v1/rooms/{id}` erases its conversation (refused while owls work in it); deleting a room
never deletes an owl, and what the owls kept (memories, notes, Library files) stays theirs.

A room's id is its chat's id: `POST /v1/chats/{id}/page` reads it a page at a time and
`POST /v1/chats/{id}/read` marks it read, as for an owl's chat; its tasks belong to several owls
(`bot_id`) and carry the cards they wait on the person with (`questions`, `action_reviews`,
`proposals`). The person writes with `POST /v1/rooms/{id}/messages`
(`{"message", "request_id", "to"}`), which answers with the turns it started (`to`: their owls,
in order); the room's chat refuses `POST /v1/chats/{id}/tasks`. Who answers: the owls @mentioned
in the message, in the order first mentioned; else `to`; else the owl that answered last there,
or the first owl. A mention is an @ that starts a word, then a member's name, then the end of a
word, matched Unicode-normalised and case-folded with whitespace collapsed in any script, the
longest name first, and without accents only when no name matches exactly; any other @word is
ordinary text. The message is kept once, with the first owl's turn; `room_turns` links every
turn to it. The turns start one at a time, in order (`Store.claim`).

Each answer is a normal turn of its owl (its memory, apps, approvals, model and computer), with
the room in place of the owl's own chat: the room's latest messages (up to 40, each and all
bounded) as reference data, each labelled `person`, `you` or `owl` with its name, then the
person's message. Other owls' words grant nothing, as in exchanges between owls. Room turns
don't ask other owls, set up routines, propose the owl's own setup or suggest About me facts, and
a reply can't be answered again yet. Owls never start each other's turns: only the person's
message, or their answer to a card (which carries on in the room), starts one. An owl's own chat,
history search and moves leave rooms out. An owl that is deleted, or moves to the other place
(its original is deleted), leaves its rooms: its turns there are erased, and a message it shared
with other owls stays with theirs. A room left with fewer than two owls stays and takes no
messages until one is added; rooms don't move. Needs you items of a room's turns carry `room_id`
and `room_name`.

### Failed turns and safe retries

A failed or interrupted task carries `failure`: a closed `kind` (`sign_in`, `model`,
`plan_limit`, `computer`, `network`, `service`, `task_limit`, `unknown`) and, when the
model service answered, its HTTP `status` and closed error `code` (`failures.py`). The
kind is set where the turn fails, from exception types, provider status and codes and
whether the model is the person's Alysis account; never from error wording. The task's
`error` stays the technical text, and the terminal event repeats the failure. Apps check
`task_failure_kinds` in `capabilities`; tasks from older services have no failure.

A turn a restart cut off (an update, a quit, a crash or sleep: `Store.recover` at start, or
`Store.restart_cut` while the runtime stops under it) starts again once (capability
`restart_reruns`) when its step receipts show nothing it did reached outside the owl's own
records (`step_effects.py`): a chat turn queues again and reads the conversation afresh, work
off the reply loop carries on from its checkpoint, and both are told they restarted, with the
steps they took. The task counts it in `restarts` (schema 48) and gets a `restarted` event.
A stopping runtime's workers claim nothing more, so a turn it queued again waits for the restart.
Otherwise it is interrupted as before, with failure kind `service`, `not_rerun` (`acted`: a
step changed something outside those records; `unconfirmed`: one may have; `already_rerun`: it
was started again once already) and `step`, the tool that stopped it. Routine and peer turns
follow the same rule, so one that acted is never replayed.

An owl may carry `color` and `background` (capability `owl_looks`): the app's short names for the
look the person picked for its face (lowercase letters, digits and hyphens). The app draws the face
and treats a name it doesn't know, or null, as no choice: a colour from the owl's name on the soft
tile of that colour. Like the name, a look is a label: `PUT /v1/bots/{id}` changes it at once, even
while the owl works, and a `PUT` that leaves a part out keeps it. Needs you items carry
`bot_color` and `bot_background`.

An owl may carry `model` (capability `owl_models`): the id of a model its place offers, or null
for the place's own. `GET /v1/models` lists them, the default first, each with `label`, `model`,
`default`, its declared `image_input`, `context_window` and `reasoning` (the model registry's
capacities for that model on that endpoint, never guessed from names), the `connection` it thinks
through with its `connection_label`, its `model_label`, and `available` with `unavailable` (why
not). A local service offers the person's saved model connections that have a key and an endpoint
the service accepts, read again on every turn. A managed Cloud Owl lists the models its owner's
authenticated Alysis gateway serves, and runs its turns through that connection with the owner's
plan and usage accounting. An operator service offers its own model and `Settings.models`. A local connection also offers the
models its provider lists for the person, each as `<connection>/<model>` after the connection's
own: on the account gateway the account's catalog (the plan's models, open where the gateway serves
them to its key, else the gateway's own listing; read at most once a minute, the last good read
standing while a read fails), otherwise the reviewed models of the provider preset it was made
from. Each model lists its supported `reasoning_efforts`. Services advertising `owl_reasoning_effort`
accept a per-Owl `reasoning_effort` in Owl settings. Null inherits the connection’s normal Owl
behavior. A model-only change clears its previous effort. Both apply on the next turn.
A catalog model closed to the person is listed with `available: false` and `unavailable`
`pending` (listed, not open to this account yet) or `unchecked` (couldn't be checked just now).
It thinks through its connection with only the model changed: the same key, endpoint, usage
accounting, plan limits and work strength. Every turn of the owl, and its answers to other owls,
uses that model with its own context and reply allowances. An owl whose connection the place no
longer offers fails the turn with failure kind `model` and a plain reason (a routine pauses with
`setting`); no other connection is used instead. A catalog model its catalog no longer lists, or
no longer opens, gives way to its connection's own model, and the turn says so with a
`model_fallback` event (`picked`, `used`, `reason` `pending` or `removed`, and a plain `message`);
one whose catalog can't be read just now is used as picked. The connection's id alone still means
its own model. Like a look, the model applies from the next turn, a `PUT` that leaves it out keeps
it, and a newly picked id the place doesn't offer, or doesn't open to the person now, is refused
(409).

A routine may carry `model` too (capability `routine_models`): one of the same ids, or null for its
owl's own. The person sets it with `PUT /v1/bots/{id}/schedules/{id}/model` (`model`,
`expected_revision`), which changes only the model: the routine keeps its state (a suggested
draft stays one), what it was allowed and its failures; `POST`/`PUT .../schedules` take it as
well, and a `PUT` that leaves it out keeps it. The owl may give one in `create_schedule`,
`edit_schedule` or `propose_schedule`, only an id the place offers the person now (its
`list_schedules` result lists them under `models`, with what each can do); the person still turns
the routine on. Each run thinks with the model its routine had when the run was queued (its
`config.model`), by the owl's rules: one no longer offered fails the run with kind `model` (the
routine pauses with `setting`), a catalog model no longer open gives way with `model_fallback`
(`routine: true`). Another owl's turn never uses it. A move carries it when the app asks
(`routine_models: true` on the export, between places that both list the capability); the copy
keeps it where the other place offers it (the same id, else its one offer of the same model, the
place's own by its id), otherwise the routine thinks with its owl's.

Owls make and change pictures (capability `owl_images`, `owl_images.py`) with `create_image` (a
description, a size from the engine's set and a Library file name) and `edit_image` (a Library
PNG, JPEG or WebP file id and instructions), through the engine's own image request
(`tools/image_generation.py`, the same as Work's `image_generate`: endpoint, sizes, limits and
checks of the returned bytes; an edit goes to the provider's `/images/edits`). They are offered
only while the owl's files are on and the model connection it thinks with (for a catalog model,
that model's connection) declares image generation, which `GET /v1/models` shows as
`image_generation`. On this computer a saved
connection declares it when the person's `image_generation` settings are on and the request can
reach it (an image `base_url` of its own, or the connection speaks `openai_compat`); the image
key and endpoint default to the connection's own. A connection to the account gateway declares
what the account's image service declares (`account_images.py`: its models for new and changed
pictures and the qualities it accepts, with the connection's own key and only that key), unless
the person has an image provider of their own. Any other connection, without image settings the
request can use, declares what the person's signed-in Alysis account declares, with the saved
account key and only that key (`image_generation.key_from = "account"`). A hosted service declares it with an
`image_generation` object in its settings. `create_image` and `edit_image` take a `quality` only
when the person asks for one; otherwise the person's `image_generation.quality` applies as the
request goes out, and a quality the connection does not accept is refused before any call is
counted. Each request is one of the task's own provider calls
in the call ledger (the workspace allowance and the task's ceiling, leaving two calls for the
owl's answer); the image provider charges and limits it for the account its key belongs to, and
an HTTP 402/429 answer or a quota or plan-limit code comes back to the owl as a plain refusal.
The result is saved as a new Library file or a new version, never over another, and shows as a
file card; Stop while the provider answers returns at once and keeps nothing. Steps carry
`file`, the picture's name.

An owl moves between the person's places (capability `owl_move`, `owl_move.py`) as an export
from one service and an import into the other, driven by the desktop app's main process. The
original is read a page at a time (`POST /v1/bots/{id}/move/export` with the last `cursor`), only
while it isn't working; `GET /v1/bots/{id}/move` says whether it works, how many files and bytes
it brings, and its `fingerprint`. Pages are a versioned stream of records (`owl`, `rule`, `app`,
`turn`, `memory`, `note`, `archive`, `routine`, `trigger`, `file`), in that order, each bounded.
The other service takes them in order into a hidden copy (`POST /v1/owl-moves` with the app's
`request_id`, then `POST /v1/owl-moves/{id}/pages`); a page sent again must match and changes
nothing, and every record is validated (kinds, fields, sizes, counts, ids, order). Library files
travel through the ordinary resumable uploads into the copy, each version after the one before,
and `GET /v1/owl-moves/{id}` lists which have arrived. `POST /v1/owl-moves/{id}/complete` checks
every file by name, version, size and SHA-256, keeps text files as text, links files back to
their turns, and shows the copy. Then `POST /v1/bots/{id}/move/finish` deletes the original
through the ordinary delete path, refusing if it worked or changed since the first page (its
fingerprint). Routines arrive paused (`pause_code` `moved`) and file events paused, with their
needs but not what was allowed. Computer state, sign-ins, run histories and cards waiting for an
answer stay behind. Between places that both list `owl_move_model`, the app asks the export for
the owl's model (`model: true`): the owl record then carries its id and the model it names there
(`model`, `model_name`), and the copy keeps that id where it is offered and open, else the one
offer there of the same model, else this place's own (null); otherwise the model stays behind.
`DELETE /v1/owl-moves/{id}` gives up a move and deletes its hidden copy; one left unfinished for a
day is given up the same way.

`POST /v1/bots` takes an optional `request_id` (`bot_create_request_id` capability). The
same id from the same owner returns the owl that request made; the same id with other
details is a conflict. Only a hash of the details is kept, and deleting the owl erases it.

Owls carry `pinned_at` and `archived_at` (capability `owl_organize`): when the person pinned
the owl to the top of their list, and when they archived it; null when not. `PATCH
/v1/bots/{id}/organize` takes `pinned` and/or `archived` (true or false); a part left out stays,
and a `PUT /v1/bots/{id}` never changes them. Neither changes the owl's `updated_at`, so
nothing approved against its setup goes out of date, and pinning again keeps the first time.
An archived owl keeps its chats, memories and files and can still be read, but it rests: its
routines and file events start nothing (file events waiting to start are dropped), and Needs
you leaves it out. An owl at work can't be archived (409). Work the person starts for it (a
message, an answer, an approval, Run now, an app turned on from its card) brings it back, as
`archived: false` does: its routines go on from their next time, without replaying runs or
retries that came due meanwhile. Archived owls count toward the 30 owls an account may have.

`POST /v1/bots/{id}/duplicate` (same capability) makes a new owl like this one in the same
service: `name` (the app words the copy in the person's language), a required `request_id`
that works as for creating an owl, and `memories` (default false) to copy its approved
memories. The copy gets the purpose, instructions, memory and files switches, look, the apps
it may use and its approval mode; never its chats, files, routines, sign-ins or list place.

Each task in a conversation lists `decided_actions`: the actions a person approved or
declined, with their tool, arguments and observed context but no screenshot.

`POST /v1/tasks/{id}/regenerate` (capability `regenerate_reply`, body `request_id`) asks for
another answer to the person's message that the owl's latest turn answered. It is refused while
any of the owl's work runs, for a turn that isn't the latest, for an unfinished reply, and for
replies to requests the engine made (routine runs, triggers, suggestions, questions). The new
task has no message of its own: its config's `regenerate` names the task holding the person's
message (`task_id`), the answer it replaces (`replaces`) and the turn it was asked from
(`source`); asked from an answer that didn't finish, it tries that one again. The turn reads the
person's message with its files and, as reference data, the answer it replaces. The chat keeps
every answer; later turns read only the newest answer that finished. The same `request_id`
returns the task already queued.

Conversations and their pages carry `read_at`: when the activity the person last read up to
happened, in the clock of `created_at`, or null before their first read. Messages created after
it arrived since the person last read the chat.

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

`POST /v1/tasks/{id}/events` (capability `live_task_events`) follows one working task without
polling. Its body is `after` (the `seq` of the last event the app has; 0 for all), `wait` (up to
10 seconds) and `limit` (up to 200). It answers as soon as the task has events after `after`, or
with none once `wait` is over, and at once for a task that has ended: `status`, `wait_reason`,
`updated_at`, `events` (as `GET /v1/tasks/{id}` lists them, in commit order), `after` to send
next and `more` when further events already wait. It is a long poll, so it passes every proxy and
client transport the API already uses. The store wakes a waiting request after the commit that
wrote its task's events; an event another process wrote to a shared database is read within five
seconds. Each owner may have 32 requests waiting; more are answered at once. Progress only: the
reply is never in the feed, and the reply checker's verdicts (which quote or judge the draft)
and suggested next messages come with `withheld: true` and empty `data`; the app reads the reply,
and what goes with it, once the task has ended. Another owner's task reads as missing (404).

`GET /v1/needs-you` (capability `needs_you`) lists what waits for the person across every
owl of the service in one read, newest first: pending questions and approvals, a suggested
next step under its reply, approval on this computer, help on an owl's computer, routines
that paused themselves (a `pause_reason`) or wait to be turned on (an owl's draft), and an
owl's newest turn when it failed or was interrupted and is unread. Routine runs show as
their paused routine. Items carry the owl, the task to open and the owl's own words where
there are some; apps word the rest in the person's language.

A pending approval (`GET /v1/bots/{id}/action-reviews`, a room's tasks and Needs you) carries
`effect`: what the step would do that made it wait, as the page's own structure settled it or
the action reviewer named it (`pay`, `sign_in`, `upload`, `personal_data`, `send`, `post`,
`book`, `delete`, `unreadable_frame`, or `none` when the reviewer saw none but still asked), or
null when nothing named one (a plugin change that always asks, a person who reviews every
action). A browser step's `observed_context.step_dialog` is `"accept"` when the step also
answers yes to a question it opens itself, and `frame_url` names another site's frame the step
or the dialog belongs to.

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

A routine's run that checked and found nothing to tell the person ends quietly (capability
`quiet_routine_runs`): only routine runs are offered `nothing_to_report` (a short private
reason, called alone), so the choice is the owl's structured step, never read from its words.
The run completes with no reply and no reply check, and a conversation the person had read up
to then stays read, so the app has nothing to notify; its task has a `nothing_new` event with
the reason, and conversations, the run in `POST /v1/bots/{id}/schedules/{id}/runs`, a
schedule's `last_run` and an owl's `latest_activity` (when that run is its newest) carry
`nothing_new_at`, when it checked. The step is refused, with what to do instead, when the run
needed something its routine doesn't allow or took a step that changed something outside the
owl's own records or may have (`step_effects.py`: only steps listed as reading or keeping the
owl's Library, notes, memory and playbooks count as harmless; a plugin step when its effect
label says it reads). A run's step can't pause its routine, so one whose goal is met says so.

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
the installed engine modules with the image source. It also starts the package-index guard,
checks that the shell declares Python installs and that the guard refuses an undeclared host
(the check has no network, so an actual install is qualified on a booted computer).
It makes no model calls or cloud requests.
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
desktops retain their separate display and network isolation.

Package installs: an owl computer's shell still has no network interface. Its bridge starts a
second guard (`bots/package_access.py`) on a private Unix socket, bound read-only into the
shell's namespace, where the desktop worker relays a loopback port to it. The guard admits only
CONNECT tunnels to port 443 of the hosts in `bots/package_indexes.json`, only when the TLS server
name is that host, and dials only public addresses. That file is the reviewed allowlist: each
package index with its index URL and every host its installer downloads from. It ships PyPI
only: an index that also takes uploads on the hosts it serves installs from (the npm registry
does) would let a package or a tricked command publish the computer's files, so it stays out.
pip installs into
a private environment layered over the image's packages (the image is never changed) and npm's
global prefix is the computer's private home, so installs stay in the owl's computer for its
session. Command receipts list `package_indexes` only when the guard and that installer are
present; older images and standalone desktops declare none and stay offline. Approvals treat an
install like any other command on the private computer. Opening a Cloud
computer initializes its managed Chrome session; polling an existing view does not.
Sleeping computers created before shared displays retain their advertised legacy
controls. Recreating one requires the normal explicit recovery flow; changing the
worker image never discards another Owl's saved memory automatically.

Changing the configured worker image affects new allocations. Hosted operators can set
`ALYSIS_BOTS_WORKER_IMAGE` to the qualified Azure image UUID in the service unit;
the bootstrap validates and applies this non-secret override without rotating
credentials. Remove the override to use the bootstrap's image again.

Managed Cloud is always metered. By default the controller meters Cloud computers
itself and refuses to start without `billing_url` and `billing_key` in its bootstrap.
A host whose Cloud computers are metered by a separate operator service sets
`ALYSIS_BOTS_CLOUD_METERING=external` in the service unit; the controller then
opens no billing connection, so nothing is debited twice. Switch a host to
controller metering only after its separate meter is stopped and the billing
connection is in the bootstrap.

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
