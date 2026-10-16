# IDE Protocol v1

Alysis Code exposes a versioned IDE protocol for editor integrations. The first transport is newline
delimited JSON over stdio:

```bash
alysis ide-bridge --stdio
```

This protocol is used by the initial VS Code extension scaffold and is intended for production IDE
hosts. It is not a terminal output scraping interface. IDE clients must consume structured JSON
responses and events.

## Health

```bash
alysis ide-bridge health
```

The health command prints a single JSON object with:

- `protocol_version`
- installed Alysis Code version
- supported methods
- supported surface event types
- supported modes
- approval, cancellation, and replay capability flags
- run/chat option parity flags and `features.management.methods` metadata, including per-method
  `mutates`, `trust_required`, `terminal_output_scraping: false`, and
  `secret_values_in_params: false`
- session-method semantics under `features.run_chat_options.session_method_capabilities`:
  `session.history` reports backend redaction and bounds, `session.context` reports approximate
  `tokenizer_estimate` accounting or explicit unavailability, `session.compact` reports
  `real_compaction_supported: true` when the live Python session has a conversation compactor, and
  `session.resume` reports bounded retained-session log replay, `session.modelInfo` reports
  redacted model/provider metadata, and `session.subagents.status` /
  `session.subagents.setEnabled` expose status/toggle-only subagent control,
  `session.trace.status`, `session.trace.setLevel`, `session.trace.listEvents`,
  `session.trace.readArtifact`, and `session.trace.clear` expose bounded backend-redacted trace
  state/events/artifacts, and `session.terminals.list`, `session.terminals.show`,
  `session.terminals.kill`, and `session.terminals.clear` expose typed existing-terminal lifecycle
  operations without arbitrary shell execution or PTY streaming. The
  `session.images.*` methods advertise workspace-scoped path validation, paste-image fallback
  support, and `binary_jsonl: false`.
- Forge and diff capability flags. Protocol v1 currently implements synchronous compatibility
  `forge.plan`, async `forge.plan.start`/`forge.plan.result`, durable
  `forge.list`/`forge.open`/`forge.resume`, `forge.status`, `forge.show`,
  typed plan-edit methods `forge.plan.getState`, `forge.plan.setAssistant`,
  `forge.plan.setGoal`, `forge.plan.updateTask`, `forge.plan.validate`, and
  `forge.plan.regenerate`, `forge.review`, `forge.attach`, safe `forge.assets.*` methods, non-mutating `forge.executePreview`,
  review-mode `forge.execute`, `diff.list`, and `diff.get`; `forge.cancel` is advertised only when
  cooperative checkpoint cancellation is available through `features.forge.cancel.supported: true`.
- `features.managed_browser` advertises the owned-browser security boundary: loopback-only CDP,
  private profiles outside workspaces, persistent interception across child targets, and a
  validating loopback egress proxy that pins DNS answers to numeric connects with no direct network
  fallback, loopback bypass, or non-proxied UDP. Public destinations remain the default; local
  access is confirmation-gated. Snapshots and diagnostics are bounded, screenshot artifacts are
  chunked, and session cleanup is exact. Top-level IDE agent sessions receive capability-gated
  built-in tools backed by this same service; native Cockpit browser controls remain a separate
  frontend task.
- `features.host_actions` advertises the capability-negotiated VS Code host-action protocol. Host
  actions require a trusted workspace, an immutable session capability set, a per-session workspace
  fence, bounded arguments/results, and explicit rejection of late or duplicate responses.

The approval capability is advertised as:

```json
{
  "round_trip": true,
  "default_deny": true,
  "session_scoped_allow": true,
  "supported_kinds_for_session_allow": [
    "shell_run",
    "shell_background",
    "custom_tool_run",
    "mcp_tool_run",
    "fs_write",
    "fs_edit",
    "fs_move",
    "fs_copy",
    "fs_delete",
    "fs_mkdir",
    "git_apply_patch",
    "verify_run"
  ],
  "timeout_seconds_default": 300
}
```

IDE clients should separate baseline protocol viability from feature availability. For the VS Code
extension, baseline Cockpit compatibility requires only `initialize`, `health`, and
`getCapabilities`. Chat/run, Forge Plan, Forge Execute Preview/Review, diffs, and artifacts each
have their own required method sets and should degrade independently when an older CLI omits a
feature method. A missing `ide-bridge` command is an incompatible CLI, not a trusted healthy CLI.
Clients must not forward API keys or start a credentialed bridge after detecting an incompatible or
broken CLI.

## Method Contract Fixture

The machine-readable method contract lives in
[`docs/generated/ide_protocol_methods.json`](generated/ide_protocol_methods.json). It records each
advertised IDE method, required and optional params, forbidden secret field names, mutation and
Workspace Trust metadata, workspace requirements, and selected result redaction expectations.
CLI-to-IDE parity is governed separately by
[`docs/generated/ide_cli_parity_matrix.json`](generated/ide_cli_parity_matrix.json), with the
generated burn-down in
[`docs/generated/ide_cli_parity_burndown.md`](generated/ide_cli_parity_burndown.md).

The fixture is validated against `health.py`, `management_protocol.py`, protocol docs, and the VS
Code backend action registry. TypeScript action tests collect deterministic params for every
registered action and validate them against this fixture, so route/schema drift such as renamed
params or contradictory options fails in CI instead of reaching users.

VS Code slash aliases stay on typed protocol methods: `/clear` maps to `session.clear`, `/context`
(with `/ctx` as an alias) maps to
`session.context`, `/status` to `session.status`, `/feedback` to `report.create`, `/model-info`
to `session.modelInfo`, and the IDE-only `/subagents status|on|off` aliases to
`session.subagents.*`; those bridge methods survive independently of the terminal
chat command. `/trace` maps to
`session.trace.*`, `/terminals` to `session.terminals.*`, and `/paste-image` to the same path-only
`session.images.add` flow as `/image`. Terminal start/interactive PTY streaming and raw unredacted
trace disclosure remain blocked until lifecycle and disclosure models exist.

## Request Format

Each request is one JSON object on one line:

```json
{"protocol_version":"1","id":"req-1","method":"initialize","params":{}}
```

Fields:

- `protocol_version`: must be `"1"`
- `id`: string, integer, or null
- `method`: request method
- `params`: method-specific object

Malformed JSON, oversized requests, missing methods, missing method fields, unsupported protocol
versions, invalid modes, guarded workspace paths, invalid ids, and inline secrets fail closed with an
error response.

## Response Format

Successful response:

```json
{"protocol_version":"1","id":"req-1","ok":true,"result":{"protocol_version":"1"}}
```

Error response:

```json
{"protocol_version":"1","id":"req-2","ok":false,"error":{"code":"invalid_mode","message":"Unsupported mode: fullaccess."}}
```

The bridge never prints API keys, bearer tokens, or authorization header values in responses or
events. It rejects inline secret fields such as `api_key`, `token`, `secret`, `password`, and
`credential` on every request; credentials must come from existing Alysis Code configuration or
environment sources. Secret-looking values in neutral config params and URL userinfo are also
rejected. Provider extra header values are not accepted through `profile.add`; the bridge returns a
structured `requires_secret_storage` action instead.

Management responses and errors pass through the same central redaction path before they are
serialized. This includes nested dict/list values, authorization headers, bearer tokens, environment
secret values, common secret assignments, and URLs with userinfo. High-volume management reads are
also bounded: retained session events and rendered conventions include cap and truncation metadata
so IDE clients do not present partial data as complete logs.

## Event Format

The bridge emits structured event envelopes derived from the existing `surface.events` model:

```json
{
  "protocol_version": "1",
  "session_id": "20260519T100000Z_abcd1234",
  "run_id": null,
  "job_id": "job_20260519T100001Z_1234abcd",
  "sequence": 1,
  "timestamp": "2026-05-19T10:00:01.123Z",
  "type": "message_delta",
  "payload": {"text": "Hello", "worker_id": null, "role": null}
}
```

Supported event types are the existing surface event types, including `message_delta`,
`message_end`, `tool_call_started`, `tool_call_completed`, `status_update`,
`plan_node_updated`, `swarm_worker_state_changed`, `subagent_state_changed`, `verify_gate_result`,
`review_gate_decision`, `approval_reviewed`, `error_raised`, `prompt_for_input`,
`config_form_request`, and the opt-in reasoning events below.

`reasoning_started {block_id}`, `reasoning_delta {block_id, text}` and `reasoning_ended
{block_id}` stream the provider's reasoning display text: a safe summary (OpenAI Responses,
Anthropic, Gemini, OpenRouter) or the provider-returned reasoning text (DeepSeek, Qwen/DashScope,
Kimi/Moonshot, Mistral, NVIDIA, GLM and other compatible routes), the same text the terminal shows
as `thinking…`. One block covers one provider call; a turn can have several. Encrypted reasoning
state and signatures are never sent, payloads pass the usual secret redaction, and there is no
reasoning record in the session log. The events are sent only for sessions created with
`reasoning_trace: true` and only while the session's
trace level is not `off`. At `off` the engine attaches no reasoning sink, so routes that need a
request flag are not asked for summaries. Clients advertise support by checking
`features.reasoning_trace` in `health`/`getCapabilities`. A block may end without
`reasoning_ended` when a turn is cancelled, so clients should also close an open block on
`message_end` or when the job ends.

`approval_reviewed` reports an approval that the reviewer answered in `auto` ("Approve for me")
instead of a person: `outcome` is `allow`, `deny` or `ask_person` (a normal approval
`prompt_for_input` follows), with `subject` (the command or action), `reason`, `risk`
(`low`/`medium`/`high`) and `approval_kind`. The decision is already final when the event
arrives; it is for display, not for answering.

Sequence numbers are monotonically increasing per bridge session surface. A new session may start at
sequence `1` even if an older session previously reached a higher sequence, so clients must track
replay cursors per `session_id` or reset sequence state on session replacement. Clients should treat
event payloads as protocol data, not rendered terminal text.

IDE clients with multiple surfaces, such as chat and Forge views, should route events by explicit
owned `session_id` and `job_id`. A controller must not attach to an arbitrary event stream just
because it has no current session, and unowned approval events must not be auto-approved or denied
by the wrong surface.

The bridge retains a bounded in-memory replay buffer for recent events. Event replay is intended for
short reconnect gaps and UI recovery, not durable history.

The VS Code extension uses this replay window when a chat panel is reopened. Clients should surface
the `truncated` flag visibly and must not imply that replay is complete when older events have been
dropped by the bridge.

## Methods

### initialize

Returns the same shape as `health` and establishes protocol compatibility.

```json
{"protocol_version":"1","id":"init","method":"initialize","params":{}}
```

IDE clients must run `initialize` against the live stdio bridge process before creating a session.
The standalone `alysis ide-bridge health` command is useful for discovery, but it is not a
substitute for validating the process that will handle subsequent `session.create`, `chat.send`,
and `approval.respond` requests.

### health

Returns bridge health and capabilities.

```json
{"protocol_version":"1","id":"health","method":"health","params":{}}
```

### getCapabilities

Returns supported methods, event types, modes, and feature flags.

```json
{"protocol_version":"1","id":"caps","method":"getCapabilities","params":{}}
```

### bridge.shutdown

Requests an orderly bridge stop. The response is flushed before shutdown begins; the bridge then
cancels active work, denies pending approvals, closes every session, and terminates managed
background processes. A client should wait for process exit after receiving the response and use an
exact process-tree kill only if the bounded graceful attempt fails.

```json
{"protocol_version":"1","id":"shutdown","method":"bridge.shutdown","params":{}}
```

### session.create

Optional `sandbox_profile` accepts `default`, `strict`, `warn` or `off`. It applies to the new
session's shell and verification configuration; runtime environment policy takes precedence.
The response includes `sandbox_mode` with the resolved shell mode. Existing sessions keep their
policy until a new session is created.

Creates an Alysis Code session bound to a workspace. Client-provided `session_id` values are optional;
the bridge rejects duplicate active ids instead of replacing an existing session. Client-provided ids
must be 1-128 characters and use only letters, digits, `_`, `-`, `.`, or `:`.

```json
{
  "protocol_version": "1",
  "id": "create-1",
  "method": "session.create",
  "params": {
    "workspace": "/path/to/project",
    "mode": "review",
    "model": "gpt-4o-mini"
  }
}
```

Supported modes are `readonly`, `review`, and `auto`. `fullaccess` is intentionally not exposed by
IDE protocol v1. Workspace binding uses the same fail-closed startup policy as the CLI in
non-interactive mode; broad or blocked paths are rejected instead of prompting.

`base_url`, when provided, must be an absolute HTTP(S) URL and must not include username, password,
or other userinfo.

Optional `reasoning_trace` (boolean, default `false`) asks for the reasoning events described in
[Event Format](#event-format). The response
echoes the effective value as `reasoning_trace`, and `session.trace.status` reports it as
`reasoning_events`. `session.trace.setLevel` with `off` stops the events for later provider calls;
`compact` or `full` resumes them.

### chat.send

Durably enqueues a user turn in an existing session and returns its stable prompt/job id
immediately. Runtime output is sent as structured events. A client-supplied `idempotency_key`
prevents duplicate submission after retries or reconnects. `context_blocks` accepts bounded,
validated IDE context (selection, visible/open files, diagnostics, terminal selection, Git diff,
symbols, or references); context is treated as untrusted data and workspace paths remain subject to
containment, ignore, and sensitive-file policy.

```json
{
  "protocol_version": "1",
  "id": "turn-1",
  "method": "chat.send",
  "params": {
    "session_id": "20260519T100000Z_abcd1234",
    "message": "Summarize this repository in readonly mode.",
    "idempotency_key": "6bbce0ab-8ff4-4ee0-b678-305485eeb6b8",
    "context_blocks": []
  }
}
```

Only one prompt runs per session. Additional sends remain durable and ordered until the active
turn finishes. `chat.queue.list` returns a bounded, redacted queue summary, `chat.queue.get` returns
one redacted prompt record, and `chat.queue.delete` cancels a pending prompt. Running prompts use
fenced leases so a stale bridge process cannot complete a prompt claimed by a replacement process.
Pending and expired-running prompts are recovered when a retained session is resumed.

### chat.steer, chat.steer.take

`chat.steer` (`session_id`, `message`) adds a user message to the turn that is running now, the
same way Enter does in the TUI. The turn reads it at its next step boundary, adds it to history as
`[Mid-turn message from the user] <text>`, and logs `steer_message_delivered` with the text in
`messages`; `session.resume` replays it at that position. The result reports `pending` (messages
waiting), `dropped` (older waiting messages this call pushed out; at most 16 wait) and `truncated`
(messages longer than 4000 characters are shortened). Without a running turn the call fails with
`session_idle`; send the message with `chat.send` instead. While a job that is not a chat turn runs
(Forge, code review) it fails with `session_busy`.

A turn can end before it reads a message. The bridge then holds the message for the session, so
the next turn (for example a queued `chat.send`) never reads it. `chat.steer.take` (`session_id`)
returns held messages, oldest first, so the host can send them as a turn of their own, as the TUI
does. A running turn's own messages stay with it. `session.clear` discards held messages.


### checkpoint.list, checkpoint.diff, checkpoint.revert, checkpoint.redo, checkpoint.branch

Audited file and native Git reads do not initialize snapshots. Before the first tool that may
change files, the bridge waits up to ten seconds for a restore baseline, with cancellation checks.
If setup is pending or failed, that action is not executed (`checkpoint_unavailable`); reads remain
available. The next request reuses a pending worker or retries failed storage. A background worker
never attaches a baseline after tool execution has begun. The extension shows a recovery card.

Successful real-session turns capture a bounded change checkpoint in Alysis Code-owned external
storage; the workspace's own Git repository and refs are never modified. Sensitive files and files
outside the configured size limits are excluded. `checkpoint.list` returns checkpoint metadata and
changed paths, while `checkpoint.diff` returns a bounded patch for one checkpoint.

`checkpoint.revert` and `checkpoint.redo` are explicit workspace mutations. They require a trusted
workspace, confirmation, an idle session, and an unchanged workspace snapshot; stale content fails
with `stale_workspace` instead of overwriting newer edits. Revert/redo are LIFO-safe and perform
atomic restoration. `checkpoint.branch` records an externally stored branch ref for the selected
checkpoint without touching project Git.

### session.tasks.get, session.tasks.replace

These methods expose a durable, session-scoped task ledger for native IDE progress UI. Each task has
a stable id, bounded title, and one of `pending`, `in_progress`, `completed`, or `blocked` statuses.
At most one task may be `in_progress`. Updates require Workspace Trust and an
`expected_revision`; a stale revision returns `updated: false`, `conflict: true`, and the current
revision without overwriting newer state. State is stored in Alysis Code-owned external storage rather
than the project tree and survives bridge restarts.

### session.questions.create, session.questions.get, session.questions.list, session.questions.answer, session.questions.cancel

Structured questions are durable, bounded decision requests with stable question and option ids.
Creation is idempotent, expires automatically, and requires Workspace Trust. Answer and cancel are
one-shot, Workspace-Trust-gated mutations. The bridge acquires and fences its internal resolution
lease before accepting an answer; the lease token is never exposed through the IDE protocol. An
expired lease is recoverable after a crash, while an answered, cancelled, or expired question set
cannot be resolved again. Responses and activity events contain no hidden lease credentials.

### permission.rules.list, permission.rules.grant, permission.rules.revoke, permission.evaluate

The persistent permission policy is an ordered Allow/Ask/Deny rule set over normalized tool,
workspace-path, and command selectors. `permission.evaluate` returns the effective decision, a
machine-readable reason, the matching rule id/source, and specificity. Sensitive resources and
paths outside the workspace have non-bypassable Ask/Deny safety overrides. Command selectors are
never returned by list responses. Grant and revoke mutations require `yes: true` or
`confirm: true`; policy persistence uses an atomic, process-locked user configuration file and
corrupt policy fails closed.

`permission.session.list` exposes privacy-safe ids for exact, session-only approval grants.
`permission.session.revoke` immediately removes one such grant. It does not reveal command text or
file-set contents, and it never converts a one-time sensitive-file approval into a reusable grant.
The approval executor rechecks sensitive/external paths before consulting session grants. Explicit
Ask rules require a fresh decision even if the same scope was previously approved for the session.
Mandatory one-time approvals never create reusable grants, including when a client requests
`allow_for_session: true`; the response reports the effective one-time decision. Policy evaluation
errors deny the operation, and a revoked grant cannot be recreated by a delayed approval waiter.

### run.start

Creates a session when needed and starts one user turn. It uses the same structured event stream as
`chat.send`.

```json
{
  "protocol_version": "1",
  "id": "run-1",
  "method": "run.start",
  "params": {
    "workspace": "/path/to/project",
    "mode": "readonly",
    "model": "gpt-4o-mini",
    "instruction": "List the top-level files."
  }
}
```

`session.create` and `run.start` accept structured run/chat options where supported by the bridge:
`temperature`, `stream`, `verify_cmd`, `verify_commands`, `model`, `base_url`, `max_steps`,
`no_log`, `yes`, `subagents_enabled`, `active_workdir`, `active_workdir_relpath`, `images`, and
`image_paths`. Omitted `subagents_enabled` means "use the loaded config default"; it does not
disable subagents. Image params are workspace-scoped paths only. The bridge rejects missing files,
symlinks, paths outside the resolved workspace, oversized images, and unsupported image MIME types.
Image binary data is not serialized through JSONL.

### session.status

Returns live session metadata for an active bridge session, including mode, model, base URL, stream
state, max step budget, active workdir, verification commands, active job, and pending approval
count.

```json
{"protocol_version":"1","id":"status-1","method":"session.status","params":{"session_id":"20260519T100000Z_abcd1234"}}
```

### session.usage

Returns usage totals for a selected session id. If the id belongs to a live bridge session, the
bridge reads live usage. If no live session exists with that id, the bridge falls back to retained
session logs and returns the same `by_model`, `totals`, and `call_count` shape for the retained JSONL
log.

```json
{"protocol_version":"1","id":"usage-1","method":"session.usage","params":{"session_id":"20260519T100000Z_abcd1234"}}
```

### session.history

Searches bounded live-session message history for a text pattern. This is a structured result, not
terminal history scraping. The bridge redacts every returned snippet before applying the text cap
and returns `redacted: true`, `secret_values_included: false`, `max_results`, `max_text_chars`, and
`truncated` metadata. Nested message content is normalized to structured JSON text before matching
so list/dict message payloads are handled consistently.

```json
{"protocol_version":"1","id":"history-1","method":"session.history","params":{"session_id":"20260519T100000Z_abcd1234","pattern":"pytest","max_results":25,"max_text_chars":500}}
```

### session.search

Searches retained conversations belonging to the same local account and canonical workspace as the
live session. Scanning, file bytes, result counts, and snippets are bounded; results and errors are
redacted. Every match includes a validated `past_session` context block which a client may attach to
a later `chat.send` without copying an unbounded session log into the prompt.

```json
{"protocol_version":"1","id":"search-1","method":"session.search","params":{"session_id":"20260519T100000Z_abcd1234","query":"migration failure","max_results":20}}
```

### session.context

Returns current live-session context metadata. When provider-reported context accounting is not
available, the bridge returns a tokenizer estimate with `source: "tokenizer_estimate"` and
`approximate: true`; clients must not treat a missing value as real zero-token usage. The result
includes `token_breakdown` when estimation succeeds and `null` for unavailable budget fields. If
the live context-left object has no usable token counts, the method reports `source:
"unavailable"` and `token_usage_available: false` rather than fabricating zero usage.
Health advertises
`features.run_chat_options.session_method_capabilities["session.context"].token_accounting` as
`"tokenizer_estimate"`.

```json
{"protocol_version":"1","id":"context-1","method":"session.context","params":{"session_id":"20260519T100000Z_abcd1234"}}
```

### session.modelInfo

Returns structured model/provider metadata for the active live session, or for an explicit `model`
parameter when supplied. The response includes `model`, `provider`, `profile`, a redacted
`base_url` when configured, context-window and support booleans when known, source metadata, and
`secret_values_included: false`.

```json
{"protocol_version":"1","id":"model-info-1","method":"session.modelInfo","params":{"session_id":"20260519T100000Z_abcd1234","model":"gpt-4o-mini"}}
```

### session.subagents.status

Returns status-only subagent metadata for the live session: whether subagents are enabled for
subsequent turns, available subagent names when the session exposes a registry, and an explicit
`explicit_execution_supported: false` policy. It also declares
`execution_lifecycle: in_turn_parent_owned`, `cancellation: parent_job`,
`independently_resumable: false`, the `subagent_state_changed` lifecycle event, and
`background_worker_surface: forge.swarm`. It does not run a subagent.

```json
{"protocol_version":"1","id":"subagents-1","method":"session.subagents.status","params":{"session_id":"20260519T100000Z_abcd1234"}}
```

### session.subagents.setEnabled

Toggles the live session `subagents_enabled` setting for subsequent turns. It requires
`workspace_trusted: true`, returns audit metadata without secret values, and does not change Forge
Execute behavior; IDE Forge Execute v1 still advertises subagents as unsupported.

```json
{"protocol_version":"1","id":"subagents-toggle-1","method":"session.subagents.setEnabled","params":{"session_id":"20260519T100000Z_abcd1234","enabled":true,"workspace_trusted":true}}
```

When an enabled chat session lets the model delegate, the delegate remains synchronous child work
inside the owning chat job. Start and terminal state are emitted as correlated
`subagent_state_changed` events with a non-authoritative `subagent_run_id`, bounded description or
error text, mode, elapsed time, step count, and child session id when available. Nested tool events
carry `worker_id` and `role`. The existing parent `session.cancel` token stops these delegates, and
their approvals continue through the parent session's host-managed approval channel. The protocol
does not offer a misleading independent resume or cancel method for them. Long-lived recoverable
parallel work uses `forge.swarm.*`, whose durable coordinator refreshes permission fingerprints on
resume and fences leases, revisions, cancellation, results, and usage.

### session.trace.status

Returns trace availability and the current active-session trace level. Supported levels are `off`,
`compact`, and `full`. Results include `redacted: true`, `secret_values_included: false`,
`max_events`, `max_bytes`, and `full_trace_requires_confirmation: true`.

```json
{"protocol_version":"1","id":"trace-status-1","method":"session.trace.status","params":{"session_id":"20260519T100000Z_abcd1234"}}
```

### session.trace.setLevel

Sets the active-session trace level for subsequent trace-aware runtime paths. `full` trace requires
`confirm: true` or `yes: true`; the backend still redacts and bounds trace output. This method does
not return raw provider headers, API keys, prompt-like secret values, or environment variables.

```json
{"protocol_version":"1","id":"trace-set-1","method":"session.trace.setLevel","params":{"session_id":"20260519T100000Z_abcd1234","level":"compact"}}
```

### session.trace.listEvents

Lists bounded structured protocol events retained for the live session. Params include
`after_sequence`, `max_events`, and `max_bytes`. The bridge redacts before returning events and
reports `truncated`, `truncated_by_event_count`, `truncated_by_bytes`, `lowest_retained_sequence`,
and `highest_retained_sequence`. This is structured event replay, not terminal output scraping.

```json
{"protocol_version":"1","id":"trace-events-1","method":"session.trace.listEvents","params":{"session_id":"20260519T100000Z_abcd1234","max_events":100,"max_bytes":32768}}
```

### session.trace.readArtifact

Reads a scoped artifact through the active session artifact store with a trace-specific byte cap and
backend redaction. Params are `session_id`, `artifact_id`, and optional `max_bytes`. Results include
`redacted: true`, `secret_values_included: false`, `truncated`, and `max_bytes`.

```json
{"protocol_version":"1","id":"trace-artifact-1","method":"session.trace.readArtifact","params":{"session_id":"20260519T100000Z_abcd1234","artifact_id":"session:trace.txt"}}
```

### session.trace.clear

Clears the session's *trace view* by advancing a per-session visibility floor: subsequent
`session.trace.listEvents`/`session.trace.status` calls only report events emitted after the clear.
The underlying bounded event buffer that backs `session.getEvents` reconnect replay is never
touched, so an IDE that reconnects after a trace clear still replays the full retained session
lifecycle (`replay_preserved: true` in the result, `replay_preserved_after_clear` in capabilities).
It does not delete workspace files or unredact historical artifacts.

```json
{"protocol_version":"1","id":"trace-clear-1","method":"session.trace.clear","params":{"session_id":"20260519T100000Z_abcd1234"}}
```

### session.terminals.list

Lists existing background terminals owned by the active agent session when a terminal manager is
present. If no manager exists, the method returns `supported: false`, `available: false`, and a
clear reason. It never starts a shell.

```json
{"protocol_version":"1","id":"terms-list-1","method":"session.terminals.list","params":{"session_id":"20260519T100000Z_abcd1234"}}
```

### session.terminals.show

Shows bounded, backend-redacted output for an existing managed background terminal. Params are
`session_id`, `process_id`, and optional `since`, `max_lines`, and `max_bytes`. Results include
`dropped_lines`, `truncated`, `truncated_by_line_count`, `truncated_by_bytes`, `redacted: true`,
`secret_values_included: false`, `arbitrary_shell_execution: false`, and
`interactive_pty_streaming: false`.

```json
{"protocol_version":"1","id":"terms-show-1","method":"session.terminals.show","params":{"session_id":"20260519T100000Z_abcd1234","process_id":"proc-1","max_lines":200}}
```

### session.terminals.kill

Kills an existing managed background terminal through the active session terminal manager. It
requires `workspace_trusted: true` and explicit `confirm: true` or `yes: true`. It does not accept a
command string and cannot start arbitrary processes.

```json
{"protocol_version":"1","id":"terms-kill-1","method":"session.terminals.kill","params":{"session_id":"20260519T100000Z_abcd1234","process_id":"proc-1","workspace_trusted":true,"confirm":true}}
```

### session.terminals.clear

Clears retained output for an existing managed background terminal only when the active terminal
manager exposes a safe clear operation. It requires `workspace_trusted: true` and explicit
confirmation. When the manager has no clear API, the bridge returns `supported: false` rather than
pretending output was cleared.

```json
{"protocol_version":"1","id":"terms-clear-1","method":"session.terminals.clear","params":{"session_id":"20260519T100000Z_abcd1234","process_id":"proc-1","workspace_trusted":true,"confirm":true}}
```

### session.compact

Runs the live Python session conversation compactor when it is enabled for that session. The result
sets `changed: true` only when the live message list changes and includes approximate
`tokens_before`, `tokens_after`, and `tokens_delta` values. If a session does not expose a
conversation compactor, the method returns `supported: false` for that session rather than
pretending compaction occurred. Provider/model failures are returned as structured errors.

```json
{"protocol_version":"1","id":"compact-1","method":"session.compact","params":{"session_id":"20260519T100000Z_abcd1234","focus":"recent failing tests"}}
```

### session.resume

The optional `emit_history: true` replays bounded user/assistant dialogue as `message_end` events for a cold IDE window. Resumed history is persisted in the destination log, so subsequent restarts keep the same conversation.

Replays bounded retained-session log history into a live IDE session using the existing CLI resume
helpers. `session_id` is the active live IDE session that receives the replay, and
`target_session_id` is the retained/historical session selected by the user. Creating the live IDE
session is a session lifecycle operation only; it does not send a model prompt by itself. The target
id is sanitized with the CLI resume policy, replayed messages are redacted, and `max_messages` is
capped by the bridge. The result reports `bounded`, `history_count`, `history_count_total`,
`resume_context_loaded`, `resume_context_skipped_reason`, and `source:
"retained_session_log_replay"`. When the retained history exceeds `max_messages`, the bridge skips
the auxiliary resume-context summary so it cannot reintroduce unbounded history into model context.
Workspace verification evidence is restored into a fresh live runtime. Saved objective
records are ignored. Clients must not replay terminal output to simulate a resume.

```json
{"protocol_version":"1","id":"resume-1","method":"session.resume","params":{"session_id":"20260519T100000Z_abcd1234","target_session_id":"20260518T090000Z_deadbeef"}}
```

### session.fork

Copies dialogue from a live `source_session_id` into a fresh `session_id`. Both sessions must be trusted, idle, and belong to different worktrees of the same Git repository. Returns `session_id`, `source_session_id`, and `history_count`. Transfer is limited to 200 text messages and 256,000 UTF-8 bytes; messages are redacted before persistence and emitted as `message_end` events. Destination startup instructions remain authoritative. Tool calls/results, approvals, queued jobs, browser ownership, and checkpoint lineage are not transferred. A nonempty or already-forked destination is rejected. The source conversation remains intact.

### session.images.list

Lists the pending image basket for the next `chat.send` turn. The response includes workspace
relative paths, MIME type, size, maximum accepted size, and `binary_jsonl: false`.

```json
{"protocol_version":"1","id":"images-1","method":"session.images.list","params":{"session_id":"20260519T100000Z_abcd1234"}}
```

### session.images.add

Adds workspace-scoped image paths to the pending image basket for the next `chat.send` turn. The
bridge uses the same image validation as `chat.send`: no symlinks, no workspace escapes, existing
regular files only, maximum size enforcement, and supported image MIME types only. Image binary data
is never serialized through JSONL.

```json
{"protocol_version":"1","id":"image-add-1","method":"session.images.add","params":{"session_id":"20260519T100000Z_abcd1234","images":["screenshots/failure.png"]}}
```

### session.images.clear

Clears the pending image basket without mutating workspace files.

```json
{"protocol_version":"1","id":"images-clear-1","method":"session.images.clear","params":{"session_id":"20260519T100000Z_abcd1234"}}
```

### session.setMode

Changes the live bridge session mode to `readonly`, `review`, or `auto` and returns
`session.status`.

```json
{"protocol_version":"1","id":"mode-1","method":"session.setMode","params":{"session_id":"20260519T100000Z_abcd1234","mode":"readonly"}}
```

### session.setProfile

Switch an idle conversation and the saved default to a configured provider. Requires
`session_id`, profile `name`, and `workspace_trusted=true`; optional `model` also
updates that profile's default model. The method replaces protocol-specific clients
and credentials while preserving history, workspace, and permissions. A running turn
returns `session_busy`. Configuration or persistence failures keep the prior live
provider. Clients must check method support before offering a live provider switch.

### session.setModel

Changes the live session model and optional `base_url` override. Secret-bearing URL userinfo is
rejected.

```json
{"protocol_version":"1","id":"model-1","method":"session.setModel","params":{"session_id":"20260519T100000Z_abcd1234","model":"gpt-4o-mini"}}
```

### session.fork

Copies user and assistant dialogue from `source_session_id` into the fresh target
`session_id`. Both sessions must be trusted, idle, and in different worktrees of
the same repository. Approvals, tool state, and checkpoints are not copied.

### session.setProfile

Switches an idle, trusted live session to the saved provider profile named by
`name`, with an optional `model` override. The request requires `session_id`.
It preserves the conversation, workspace, permissions, and budgets, rebuilds the
provider clients, persists the active profile, and returns session status. An
unknown profile or active task is rejected; a failed configuration update restores
the previous live clients.

### session.personas.list

Lists the personas available to a live session: the builtin code, architect, ask, and debug
personas followed by any workspace-defined personas (`.alysis_personas/*.md`), plus the active
persona and whether persona modes are enabled at all. When the persona kill switch is off the call
still succeeds with `enabled:false` and an empty list so hosts can render an honest disabled state.

```json
{"protocol_version":"1","id":"personas-1","method":"session.personas.list","params":{"session_id":"20260519T100000Z_abcd1234"}}
```

### session.persona.set

Switches the live session persona. The clamp rule applies: a persona may lower the effective
execution mode, never raise it, and a write-scoped persona runs at review at most. Refused while a
task is running (`persona_change_busy`), for unknown names (`invalid_persona`), and when persona
modes are disabled (`persona_modes_disabled`). A successful change emits `persona_changed`; setting
the already-active persona returns `changed:false` and emits nothing.

A new session starts on the persona `alysis chat` would start it on. When its log already records a
persona (a session created with the `session_id` of an existing chat), that persona is applied
again, as `/resume` does, and `session.personas.list` reports `active_source: "resume"`. Otherwise
the configured `default_persona` is applied. Either way the persona's mode, write scope and model
role are in effect before `session.create` answers, its `mode` is the narrowed mode, and a
`persona_changed` event may arrive before that answer. `session.resume` from another session
applies that session's persona the same way, unless a task is running.

```json
{"protocol_version":"1","id":"persona-1","method":"session.persona.set","params":{"session_id":"20260519T100000Z_abcd1234","persona":"architect"}}
```

### session.setStream

Changes live session streaming preference and returns `session.status`.

```json
{"protocol_version":"1","id":"stream-1","method":"session.setStream","params":{"session_id":"20260519T100000Z_abcd1234","stream":true}}
```

### session.setActiveWorkdir

Changes the active workdir within the session workspace using the same path confinement policy as
the CLI session helpers.

```json
{"protocol_version":"1","id":"workdir-1","method":"session.setActiveWorkdir","params":{"session_id":"20260519T100000Z_abcd1234","path":"src"}}
```

### session.clear

Clears live in-memory conversation messages when the session runtime exposes a mutable message
list.

```json
{"protocol_version":"1","id":"clear-1","method":"session.clear","params":{"session_id":"20260519T100000Z_abcd1234"}}
```

### Management Methods

Protocol v1 exposes typed management methods for CLI surfaces that the VS Code backend needs. These
methods reuse Python internals rather than shelling out to the CLI or parsing Rich/Typer terminal
output. Every mutating method advertises `mutates: true` and `trust_required: true` in
`features.management.methods`, and workspace-bound methods advertise `workspace_required: true`;
the backend also fails closed with `workspace_trust_required` unless `workspace_trusted: true` is
present. Inline `api_key`, `token`, `password`, `secret`,
`credential`, or `authorization` params are rejected for every method. Secret-looking neutral values,
URL userinfo, and raw provider extra header values are rejected or converted into structured next
steps. Provider key values are reported only as presence/source metadata; SecretStorage remains
extension-side.

Configuration:

- `config.get`
- `config.set`
- `config.schema`
- `config.validate`

Profiles:

- `profile.list`
- `profile.show`
- `profile.add`
- `profile.remove`
- `profile.use`
- `profile.rename`
- `profile.presets`
- `profile.preset`
- `profile.convert`

`profile.add` accepts provider metadata such as `base_url`, `api_key_env`, default model, and notes,
but not raw `extra_headers` values. If header values are required, the result is `changed: false`
with an action like `{"kind":"requires_secret_storage","next_step":"open_configure_provider"}`.
`profile.preset` applies the same URL-userinfo rejection to custom `base_url` overrides.

Retained sessions:

- `session.show`
- `session.score`

`session.show` returns a bounded, redacted retained JSONL view. Params include `max_events` and
`max_total_bytes`; results include `returned_event_count`, `truncated_by_events`,
`truncated_by_bytes`, `max_events`, `max_total_bytes`, `secret_values_included: false`, and
`redacted: true`.

`session.score` accepts either `session_id` for one selected retained session or `latest` for the N
newest retained sessions. Clients must not send both. The VS Code Sessions tree sends the selected
`session_id`; command-palette/group fallback sends `latest: 1`.

Tools and skills:

- `tools.catalog`
- `tool.list`
- `tool.info`
- `tool.trust`
- `tool.untrust`
- `skill.list`
- `skill.info`
- `skill.init`
- `skill.validate`
- `skill.install`
- `skill.enable`
- `skill.disable`
- `skill.remove`

`skill.install` accepts local directories or `.zip` archives only when the resolved source stays
inside the resolved workspace and is not a symlink. HTTPS remote skill sources require explicit
remote intent (`allow_remote: true`) plus confirmation (`yes: true` or `confirm: true`); URL
userinfo and non-HTTPS remote transports fail closed in Protocol v1.

Doctor, sandbox, update, and report:

- `doctor.summary`
- `doctor.providers`
- `doctor.providers.live` — one minimal live provider request against the active profile. Never
  passive: requires `allow_live=true`, which clients send only on a direct user action (Connect /
  Replace key / Test connection). Accepts a bounded `timeout_s` (default 15, max 60) and returns a
  redacted, classified `validation` result plus a top-level `ok` flag.
- `doctor.bundle`
- `sandbox.doctor`
- `sandbox.setup`
- `sandbox.pull`
- `update.check`
- `report.create`

`update.check` is passive-safe by default. Empty params and `cached: true` return cached/local
status and report `network_used: false`; clients must pass explicit user-triggered
`allow_network: true` or `force: true` to perform a live network check.

MCP:

- `mcp.status`
- `mcp.server.status`
- `mcp.server.enable`
- `mcp.server.disable`
- `mcp.server.restart`
- `mcp.prompts.list`
- `mcp.prompts.get`
- `mcp.auth.status`
- `mcp.auth.login.start`
- `mcp.auth.login.status`
- `mcp.auth.login.cancel`
- `mcp.auth.logout`

`mcp.status` remains configuration-oriented and may bootstrap a short-lived manager. The
`mcp.server.*` methods are different: they require `session_id` and `server_id` and operate only on
the exact live `McpManager` owned by that IDE session and workspace. Ownership mismatch, an absent
live manager, an inactive server, or a non-explicit server trust mode fails closed.

`mcp.server.status` is read-only and returns bounded, redacted connection/catalog metadata without
commands, URLs, headers, environment values, or credentials. `enable`, `disable`, and `restart`
require `workspace_trusted: true` and an idle session. Disable closes the owned connection and makes
all existing model-facing bindings reject calls. Enable/restart validate a replacement connection
against the session's frozen tool, resource, and prompt catalogs before atomically reconnecting the
stable bindings. If a catalog changed, the replacement is closed and the client must create a new
IDE session. Agent-session close, bridge shutdown, startup failure, and process exit continue to
close the same owned connections.

`mcp.auth.login.start` is a Workspace-Trust-gated, non-blocking user action. It performs bounded
OAuth metadata discovery, starts an owned one-shot `127.0.0.1` callback listener, persists a flow
record, and returns a `flow_id` plus `browser_url`. The bridge never opens that URL itself. The VS
Code extension opens only HTTPS browser URLs after explicit confirmation, polls
`mcp.auth.login.status`, and sends `mcp.auth.login.cancel` when the user cancels.

State and S256 PKCE are mandatory. Callback wait and token exchange run asynchronously; tokens are
written directly to the encrypted MCP credential store. Authorization codes, PKCE verifiers,
tokens, listener credentials, and completion leases are never returned by the protocol. Listener
cleanup occurs on completion, timeout, cancellation, logout, and bridge shutdown. Logout fences
concurrent completion so a late exchange cannot resurrect deleted credentials.

The current callback is IPv4 loopback only. Remote/SSH extension hosts require a future VS Code URI
handler or device-code flow. Confidential-client secrets are intentionally unsupported, and remote
provider revocation requires a provider-specific revoker; confirmed logout always deletes local
encrypted credentials.

Hooks:

- `hooks.list`
- `hooks.doctor`
- `hooks.trace`
- `hooks.test`
- `hooks.trust`
- `hooks.untrust`
- `hooks.init`
- `hooks.effective`
- `hooks.enable`
- `hooks.disable`

`hooks.watch` is not advertised as an IDE request/response method, and the bridge does not advertise
weak `hooks.watch.start`/`poll`/`stop`/`status` placeholders. It requires a subscription registry,
bounded redacted event buffers, dropped-event accounting, watcher cancellation, and disposable stop
cleanup before IDE support. It must require explicit user action and Workspace Trust when a future
typed lifecycle exists.

Conventions:

- `conventions.list`
- `conventions.render`

`conventions.render` redacts rendered convention content and bounds it with `max_chars` and
`max_bytes`. Results include `document_count`, `truncated_by_chars`, `truncated_by_bytes`,
`secret_values_included: false`, and `redacted: true`.

Alysis Code extension package management:

- `ext.search`
- `ext.list`
- `ext.info`
- `ext.install`
- `ext.uninstall`
- `ext.enable`
- `ext.disable`

`ext.install`, `ext.uninstall`, `ext.enable`, and `ext.disable` are mutating package operations and
therefore require Workspace Trust plus explicit confirmation. Search/list/info are structured
metadata reads and do not parse terminal output. `ext.install` also requires explicit package trust
review. A request without `trust_approval` returns `changed: false`, a redacted `trust_request`
containing plugin/source/commit/manifest/components/permissions metadata, and an
`action.required_approval` object. The client must present that review to the user and call
`ext.install` again with `yes: true` and the unchanged `trust_approval` object. `yes: true` alone
does not approve package code, hooks, tools, skills, or MCP components.
Install sources are limited to registry ids or pinned HTTPS git sources
(`git+https://...@<40-char-commit>` or `https://...#<40-char-commit>`). URL userinfo, local paths,
path traversal, unsupported schemes, and unpinned remote sources fail closed before package
installation is attempted.

Examples:

```json
{"protocol_version":"1","id":"config","method":"config.get","params":{}}
{"protocol_version":"1","id":"config-set","method":"config.set","params":{"workspace_trusted":true,"key":"default_model","value":"gpt-5"}}
{"protocol_version":"1","id":"profile","method":"profile.preset","params":{"workspace_trusted":true,"preset_key":"ollama","name":"local-ollama","yes":true}}
{"protocol_version":"1","id":"tools","method":"tool.list","params":{"workspace":"/path/to/project"}}
{"protocol_version":"1","id":"report","method":"report.create","params":{"workspace":"/path/to/project","workspace_trusted":true,"feedback":"IDE bridge issue","local_only":true}}
{"protocol_version":"1","id":"mcp","method":"mcp.status","params":{"workspace":"/path/to/project"}}
{"protocol_version":"1","id":"mcp-live","method":"mcp.server.status","params":{"session_id":"<session_id>","server_id":"tickets"}}
{"protocol_version":"1","id":"mcp-restart","method":"mcp.server.restart","params":{"session_id":"<session_id>","server_id":"tickets","workspace_trusted":true}}
{"protocol_version":"1","id":"hooks","method":"hooks.test","params":{"workspace":"/path/to/project","event":"PreToolUse","tool":"shell_run"}}
{"protocol_version":"1","id":"sandbox-setup","method":"sandbox.setup","params":{"workspace_trusted":true,"pull":false}}
{"protocol_version":"1","id":"sandbox-pull","method":"sandbox.pull","params":{"workspace_trusted":true,"images":["alysis-sandbox:latest"]}}
{"protocol_version":"1","id":"update","method":"update.check","params":{"allow_network":true}}
{"protocol_version":"1","id":"ext-review","method":"ext.install","params":{"workspace":"/path/to/project","workspace_trusted":true,"source":"publisher.plugin"}}
{"protocol_version":"1","id":"ext-install","method":"ext.install","params":{"workspace":"/path/to/project","workspace_trusted":true,"source":"publisher.plugin","yes":true,"trust_approval":{"approved":true,"plugin_id":"publisher.plugin","commit":"<reviewed commit>","manifest_sha256":"<reviewed manifest sha256>","approval_fingerprint":"<review fingerprint>"}}}
{"protocol_version":"1","id":"forge-show","method":"forge.show","params":{"session_id":"<session_id>","plan_id":"<plan_id>"}}
```

### approval.respond

Responds to a host-managed approval request emitted as a `prompt_for_input` event with
`kind: "approval"`.

Request params:

- `session_id`: string, required
- `approval_id`: string, required
- `allow`: boolean, required
- `allow_for_session`: boolean, required

Success result:

```json
{
  "session_id": "20260519T100000Z_abcd1234",
  "approval_id": "2f70ef1b68a34a0daf97a6a38db39fcb",
  "status": "applied",
  "allow": true,
  "allow_for_session": false,
  "allow_for_session_supported": true,
  "allow_for_session_scope": {
    "type": "exact_command_hash",
    "algorithm": "sha256",
    "kind": "shell_run",
    "command_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "allow_for_session_warning": null
}
```

`status` is one of `applied`, `already_resolved`, or `expired`. Duplicate responses already
applied by a previous `approval.respond` fail closed with `duplicate_response`; timed-out approvals
return `expired` if the bridge still retains the timeout record.

Errors:

- `session_not_found`: the session does not exist or is closed
- `unknown_approval`: no pending or recently expired approval exists with that id
- `duplicate_response`: a previous `approval.respond` already resolved that approval
- `invalid_request`: required fields are missing or have the wrong JSON type

All `approval.respond` params still pass through inline-secret rejection, and all responses still
flow through protocol secret redaction.

Approval request events use the existing event envelope with `type: "prompt_for_input"` and include:

- `approval_id`
- `kind` set to `"approval"`
- `approval_kind` (the backend action kind to display, such as `fs_write`, `shell_run`, or
  `verify_run`)
- `reason`
- `preview`
- `files`
- `command`
- `metadata`
- `expires_at`
- `scope` (the exact scope the approval covers, or `null`)
- `allow_for_session_supported`
- `allow_for_session_scope`
- `allow_for_session_warning`

Example event payload:

```json
{
  "prompt_id": "2f70ef1b68a34a0daf97a6a38db39fcb",
  "prompt_text": "write README.md",
  "kind": "approval",
  "approval_kind": "fs_write",
  "approval_id": "2f70ef1b68a34a0daf97a6a38db39fcb",
  "reason": "review mode requires confirmation for write operations",
  "preview": "write README.md",
  "files": ["README.md"],
  "command": null,
  "metadata": {
    "approval_kind": "fs_write"
  },
  "expires_at": "2026-05-19T10:02:01.000Z",
  "allow_for_session_supported": true,
  "allow_for_session_scope": {
    "type": "exact_file_set",
    "algorithm": "sha256",
    "kind": "fs_write",
    "operation": "fs_write",
    "files_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "file_count": 1,
    "files": ["README.md"]
  },
  "scope": {
    "type": "exact_file_set",
    "algorithm": "sha256",
    "kind": "fs_write",
    "operation": "fs_write",
    "files_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "file_count": 1,
    "files": ["README.md"]
  },
  "allow_for_session_warning": null
}
```

Request:

```json
{
  "protocol_version": "1",
  "id": "approval-1",
  "method": "approval.respond",
  "params": {
    "session_id": "20260519T100000Z_abcd1234",
    "approval_id": "approval_20260519T100001Z_1234abcd5678",
    "allow": true,
    "allow_for_session": false
  }
}
```

Error response example:

```json
{
  "protocol_version": "1",
  "id": "approval-2",
  "ok": false,
  "error": {
    "code": "unknown_approval",
    "message": "No pending approval exists with that id."
  }
}
```

When an approval is allowed, denied, or times out, the bridge also emits a `prompt_for_input` event
with `kind: "approval_result"` and the same `approval_id`. The result payload is intentionally
small:

```json
{
  "kind": "approval_result",
  "approval_id": "2f70ef1b68a34a0daf97a6a38db39fcb",
  "status": "expired",
  "allow": false,
  "allow_for_session": false,
  "allow_for_session_supported": true
}
```

The bridge emits this result event for every resolved path, including timeout and denial. It does
not include command text, file paths, reasons, previews, scope details, or warnings in the
`approval_result` payload.

Clients with separate chat and Forge UI controllers must answer only approvals for sessions/jobs
they own. If the owning UI cannot present an approval, it should deny fail-closed with a clear
surface-specific reason. No approval should be granted silently.

`allow_for_session: true` is exactly scoped, never broad by approval kind. The bridge persists
allowances only in memory on the `BridgeSession`, and session close or bridge restart drops them.
Supported boundaries are:

- shell approvals (`shell_run`, `shell_background`): exact command hash
- custom and MCP-like approvals (`custom_tool_run:<name>`, `mcp_*`, `mcp_tool_run:<name>`): exact
  backend-provided action string hash
- file approvals (`fs_write`, `fs_edit`, `fs_move`, `fs_copy`, `fs_delete`, `fs_mkdir`,
  `git_apply_patch`): exact sorted file path set; later file approvals may auto-allow only when
  their file set is a subset of the approved set for the same operation kind
- verification approvals (`verify_run`): exact verification command set hash and command count

If a client sends `allow_for_session: true` for an approval without an exact safe scope, the bridge
forces `allow_for_session: false` in the response and returns a non-empty
`allow_for_session_warning`. The action may still be allowed once when `allow: true`, but no broader
allowance is stored.

The default approval timeout is 300 seconds. Timeout is fail-closed: the agent receives
`ApprovalDecision(allow=False)`, the bridge emits `kind: "approval_result"` with
`"status": "expired"`, and the pending registry entry is cleaned up.

The bridge remains terminal-non-interactive. It does not call Rich/Typer prompt APIs for IDE
approvals.

### Capability-negotiated VS Code host actions

`session.create` and session-creating `run.start` requests may include:

```json
{
  "workspace_trusted": true,
  "host_capabilities": {
    "protocol_version": "1",
    "actions": [
      "tasks.list",
      "tasks.run",
      "tasks.status",
      "tasks.terminate",
      "debug.list",
      "debug.start",
      "debug.stop",
      "debug.status"
    ]
  }
}
```

The action allowlist is strict and immutable for the session. If `workspace_trusted` is absent or
false, the effective action list is empty and no task/debug agent tools are assembled. The host must
cancel and recreate the session when workspace trust or the supported capability set changes.
`session.create` returns `host_actions` with the effective actions, `workspace_fence`,
`capability_fingerprint`, event/method names (including `session_closed_event`), a 30-second default
request timeout, an 8 KiB argument cap, and a 64 KiB result cap. `run.start` retains its normal
job-start result and does not repeat this session-creation metadata.

An agent tool call emits `host_action_requested` in the normal event envelope. Its payload is:

```json
{
  "protocol_version": "1",
  "host_action_id": "ha_<32 lowercase hex chars>",
  "action": "tasks.list",
  "arguments": {},
  "workspace_root": "/canonical/workspace/root",
  "workspace_fence": "wf_<32 lowercase hex chars>",
  "capability_fingerprint": "<sha256>",
  "expires_at": "2026-07-30T12:00:30Z",
  "max_result_bytes": 65536,
  "job_id": "<owning job id>"
}
```

The host must recheck live Workspace Trust, keep all opaque task/configuration/execution/debug ids
inside the event's canonical workspace, and answer with `host.action.respond` before `expires_at`:

```json
{
  "protocol_version": "1",
  "id": "host-response-1",
  "method": "host.action.respond",
  "params": {
    "session_id": "<owning session id>",
    "host_action_id": "ha_<32 lowercase hex chars>",
    "workspace_fence": "wf_<32 lowercase hex chars>",
    "capability_fingerprint": "<sha256>",
    "ok": true,
    "result": {"tasks": [], "truncated": false}
  }
}
```

Exactly one of `result` or `error` is required. Errors use
`{"code":"bounded_code","message":"bounded message","retryable":false}`. A successful response
acknowledges `{status, session_id, host_action_id, action, outcome}`, where `status` is `applied` and
`outcome` is `result` or `error`. Wrong workspace/capability fences, unknown action ids, duplicate
responses, and responses after timeout/cancellation fail closed. Job/session/bridge cancellation or
deadline expiry emits one `host_action_cancelled` event containing `host_action_id`, `action`, both
fences, `reason`, and `protocol_version`; the host must abort the adapter operation and discard any
late completion.

After successful session cleanup, the bridge emits `session_closed` in the normal event envelope.
Its payload contains `protocol_version`, `workspace_fence`, and `capability_fingerprint`; the
envelope's `session_id` is the execution owner. The host must terminate every still-running task or
debug execution owned by that exact session and fence. A bridge process loss cannot guarantee this
event, so clients must also terminate all bridge-owned executions on reset, exit, or extension
deactivation.

`tasks.run` and `debug.start` are opaque execution boundaries: a task or launch configuration may
resolve to arbitrary workspace commands. In `review` and `auto`, the backend emits the existing
`prompt_for_input` approval event before `host_action_requested` and requires an explicit allow-once
decision. The approval advertises `allow_for_session_supported: false`; denial, timeout, a cached or
session-style grant, or a runtime without host-managed approvals prevents the host action entirely.
This one-time gate also applies in `fullaccess` because the backend cannot inspect the opaque command
before launch or apply its shell denylist. Status/list operations remain read-only, and
terminate/stop remain bounded cleanup operations.

Supported action arguments and bounded results:

- `tasks.list {task_type?}` -> `{tasks:[{id,label,type?,source?,detail?}],truncated}`
- `tasks.run {task_id}` -> `{execution_id,task_id,state,exit_code?,diagnostics_delta?}`. `state` is
  `started`, `completed`, or `failed`. A long-running task returns `started` before the host-action
  deadline; a task that ends inside the bounded wait returns its terminal state and exit code.
  `diagnostics_delta` contains non-negative `added`, `removed`, `changed`, and `total` counts,
  `truncated`, and at most 50 bounded `{uri,line,severity,message,source?}` items.
- `tasks.status {execution_id?}` ->
  `{executions:[{execution_id,task_id,state,exit_code?,diagnostics_delta?}],truncated}`, where
  `state` is `running`, `completed`, `failed`, or `terminated`. A terminal row may carry the final
  bounded diagnostic delta against the snapshot preserved when `tasks.run` started. Results contain
  only executions owned by the requesting session and its immutable workspace/capability fence;
  terminal records remain queryable for the session lifecycle.
- `tasks.terminate {execution_id}` -> `{execution_id,terminated,state}`, with `state` equal to
  `terminated`, `not_found`, or `already_ended`.
- `debug.list {}` ->
  `{configurations:[{id,name,type,request?,workspace_folder?}],truncated}`.
- `debug.start {configuration_id}` -> `{debug_session_id,configuration_id,state}`, with `state`
  equal to `started` or `failed`.
- `debug.stop {debug_session_id}` -> `{debug_session_id,stopped,state}`, with `state` equal to
  `stopped`, `not_found`, or `already_ended`.
- `debug.status {debug_session_id?}` ->
  `{sessions:[{id,name,type,state,workspace_folder?}],truncated}`, where state is `running` or
  `stopped`.

Task/debug lists are capped at 100 items, opaque ids at 256 characters, and display fields at 512
characters. Breakpoint and arbitrary Debug Adapter Protocol requests are outside this contract.

### session.cancel

Closes an idle session. When an active job is cancellable, `session.cancel` requests cooperative
checkpoint cancellation and returns `status: "cancellation_requested"` with a job summary. The job
remains `active_job` until the backend reaches a checkpoint, pending approval boundary, or finalizer
and then records a terminal `cancelled`, `completed`, or `failed` state in `last_job`.

This is not a hard interrupt. Provider calls or long-running work that cannot observe the
cooperative token immediately may remain in `cancellation_requested` until the next safe checkpoint.
Clients must not render a job as cancelled until `job.status`, `session.status`, or `session.list`
reports terminal `cancelled`.

Clients abandoning a session, such as **New Session**, may send `close_when_idle: true`. The bridge
atomically stops queued prompts, rejects new work for that session, requests cancellation of the
exact active job, and acknowledges `close_when_idle: true`. After that worker settles, the bridge
closes the agent session and all owned managed-browser processes and private artifacts. A client must
treat a non-closed response without that acknowledgement as an unconfirmed cleanup, not success.

On bridge shutdown or extension deactivate, clients should first deny visible pending approvals and
close idle sessions, then call `bridge.shutdown`. If running jobs remain, the bridge marks them
interrupted/failed instead of leaving stale active jobs. The stdio process may then be terminated as
an exact process tree after a bounded graceful attempt, with hard kill reserved as the last resort.

```json
{"protocol_version":"1","id":"cancel-1","method":"session.cancel","params":{"session_id":"20260519T100000Z_abcd1234"}}
{"protocol_version":"1","id":"abandon-1","method":"session.cancel","params":{"session_id":"20260519T100000Z_abcd1234","reason":"new_session_requested","close_when_idle":true}}
```

### code.review.start / code.review.result

Starts a generic, asynchronous review of the working tree, a branch against its merge base, one
commit, or an explicit revision range. Review collection is read-only, shell-free, bounded, and
excludes sensitive, binary, unsafe, and oversized files. Truncated or excluded reviews cannot return
an approval verdict. The request requires a trusted workspace because it invokes Git and a provider.

```json
{"protocol_version":"1","id":"review","method":"code.review.start","params":{"session_id":"20260519T100000Z_abcd1234","scope":"working_tree","workspace_trusted":true}}
```

For `branch`, provide `base` and optionally `head` (default `HEAD`); for `commit`, provide
`revision`; for `range`, provide `base` and `head`. Poll `code.review.result` with the returned
`job_id`. Completion returns bounded structured findings, a verdict summary, and safe diff metadata;
raw secret-bearing file contents are never returned.

```json
{"protocol_version":"1","id":"review-result","method":"code.review.result","params":{"job_id":"job_20260519T100001Z_1234abcd"}}
```

### job.status

Returns current status for a job.

```json
{"protocol_version":"1","id":"job-1","method":"job.status","params":{"job_id":"job_20260519T100001Z_1234abcd"}}
```

The result includes `job_id`, `session_id`, `status`, `created_at`, `started_at`, `completed_at`,
`exit_code`, and `error`.

### session.list

Lists bridge sessions currently known in memory.

```json
{"protocol_version":"1","id":"sessions","method":"session.list","params":{}}
```

Each item includes `session_id`, `workspace_root`, `mode`, `closed`, and an active job summary when
present.

### session.getEvents

Returns recent structured events for a session from the bounded replay buffer.

```json
{
  "protocol_version": "1",
  "id": "events",
  "method": "session.getEvents",
  "params": {
    "session_id": "20260519T100000Z_abcd1234",
    "after_sequence": 10,
    "max_events": 100
  }
}
```

The result includes `events`, `truncated`, `lowest_retained_sequence`,
`highest_retained_sequence`, and `max_events`. `after_sequence` is scoped to the requested
`session_id`; clients should send the last sequence observed for that same session, not a global
sequence from another session.

### artifact.list

Lists readable artifacts rooted under the session artifact directory. Protocol v1 does not expose
the whole workspace as an artifact root.

```json
{"protocol_version":"1","id":"artifacts","method":"artifact.list","params":{"session_id":"20260519T100000Z_abcd1234","max_items":500}}
```

The response includes `artifacts`, `truncated`, `max_items`, and `max_depth`. The bridge bounds
recursive listing to avoid unbounded filesystem walks.

### artifact.read

Reads an artifact by id with a byte cap.

```json
{
  "protocol_version": "1",
  "id": "artifact-read",
  "method": "artifact.read",
  "params": {
    "session_id": "20260519T100000Z_abcd1234",
    "artifact_id": "session:tool_logs/example.json",
    "max_bytes": 65536
  }
}
```

Artifact reads are path-confined to their declared artifact root and reject `..` escapes.
Reads are bounded to at most 1 MiB, stream only the requested byte window into memory, and return
`truncated: true` when content was capped.

### Managed browser lifecycle

The managed browser bridge exposes `browser.start`, `browser.navigate`, `browser.snapshot`,
`browser.screenshot`, `browser.artifact.read`, `browser.diagnostics`, `browser.click`,
`browser.type`, `browser.status`, `browser.list`, and `browser.close`. The native Browser Cockpit
and built-in agent tools share this typed lifecycle; neither surface exposes raw CDP or bypasses
its ownership, navigation, approval, and cleanup boundaries.

Every call is scoped twice. `session_id` selects a live IDE session and therefore the workspace
owner. Operations on one browser additionally require its opaque `browser_session_id`. A browser
session or screenshot id owned by another IDE session is rejected rather than disclosed. Callers
never supply a profile directory, artifact path, DevTools port, or WebSocket endpoint.

`browser.start` requires `workspace_trusted: true` and starts a supported Chrome, Edge, or Chromium
binary as an owned process group. The bridge uses a minimal child environment, disables inherited
proxy configuration, creates a private per-browser profile and artifact directory below the
Alysis Code user-data root (never below a declared workspace), and accepts only a literal-loopback
DevTools WebSocket endpoint. Every browser session also owns a validating loopback HTTP/CONNECT
proxy. Chrome is launched without direct network fallback or implicit loopback proxy bypass, with
direct host resolution disabled and non-proxied UDP disabled. The proxy itself resolves each
request, applies the destination policy to the complete bounded answer set, and connects only to a
captured numeric address. Its own endpoint and the session's DevTools endpoint are permanently
denied, including after explicit local-access confirmation. An optional absolute `executable_path`
may select the binary.

```json
{
  "protocol_version": "1",
  "id": "browser-start",
  "method": "browser.start",
  "params": {
    "session_id": "20260519T100000Z_abcd1234",
    "workspace_trusted": true
  }
}
```

Browser navigation is public-only by default. `browser.navigate` accepts only HTTP or HTTPS URLs,
rejects URL userinfo, normalizes the host through IDNA, resolves it with a bounded DNS lookup, and
rejects localhost, `.local`, loopback, private, link-local, multicast, reserved, unspecified, and
other non-public results. Persistent CDP Fetch interception applies the same authorization callback
to top-level, redirect, subresource, and recursively attached child-target requests. The validating
egress proxy independently repeats authorization and pins the accepted answer to the numeric socket
connect, closing the DNS validation/connect race even for requests triggered after navigation,
clicks, typing, workers, frames, or popups. Navigation, `browser.click`, and `browser.type` require
`workspace_trusted: true`; selectors and typed text are bounded, and the `browser.type` result
reports only success and character count, never the input text.

```json
{
  "protocol_version": "1",
  "id": "browser-navigate",
  "method": "browser.navigate",
  "params": {
    "session_id": "20260519T100000Z_abcd1234",
    "browser_session_id": "dUIpQNHJGFKN8Eq1pW69dQN7BiPEgA5C",
    "url": "https://example.com/",
    "workspace_trusted": true,
    "timeout_seconds": 20
  }
}
```

Local development is a persistent property of a newly started browser, not a per-navigation
escape hatch. New IDE clients request `network_scope: "public_loopback"` with
`workspace_trusted: true` and explicit `yes: true` or `confirm: true` on `browser.start`.
This separately confirmed Direct IDE scope permits public destinations plus literal or named
loopback only. Private LAN, link-local, multicast, reserved, mixed-DNS, proxy, and DevTools
destinations remain denied. `allow_local_destinations: true` is a compatibility-only alias for
this loopback scope and cannot be combined with `network_scope`. HTTP/HTTPS-only and no-userinfo
validation still apply, and a caller cannot broaden an already running public-only browser.

Direct IDE sessions are an actor boundary as well as a network boundary. The Cockpit can list and
operate them, but every model-controlled browser tool filters them from lists and rejects their
ids before status, navigation, observation, mutation, or cleanup. Public sessions remain shared
with the top-level IDE agent; nested subagents receive no browser tools.

Top-level IDE agent sessions expose the corresponding `browser_start`, `browser_navigate`,
`browser_snapshot`, `browser_screenshot`, `browser_artifact_read`, `browser_diagnostics`,
`browser_click`, `browser_type`, `browser_status`, `browser_list`, and `browser_close` built-ins only
when the managed-browser service is attached. Nested subagents do not receive them. Snapshot,
screenshot, artifact-read, diagnostics, status, and list are observational routes; start,
navigate, click, type, and close require a fresh one-time IDE host approval for every action in all
execution modes and fail closed when host-managed approvals are unavailable. The tool route is
always public-only: it has no parameter for local/private destination opt-in, no arbitrary
executable or profile path, and no delete-artifacts option. Approval previews omit URL query and
fragment data and omit typed text; results expose only bounded owner-scoped public data.

`browser.snapshot` accepts `kind: semantic|accessibility|dom|text` and returns a redacted bounded
payload. `browser.diagnostics` returns only allowlisted console and network event families, with a
bounded `max_events` (maximum 500 at the protocol boundary), per-event payload limits, and
`truncated` metadata. `timeout_seconds`, where accepted, must be between 0.05 and 300 seconds.

`browser.screenshot` validates the CDP response as a bounded PNG, stores it in the owning browser's
private artifact directory, and returns only `artifact_id`, media type, size, and SHA-256 metadata.
Bytes are retrieved with `browser.artifact.read`, never through a caller-selected path:

```json
{
  "protocol_version": "1",
  "id": "browser-artifact-read",
  "method": "browser.artifact.read",
  "params": {
    "session_id": "20260519T100000Z_abcd1234",
    "browser_session_id": "dUIpQNHJGFKN8Eq1pW69dQN7BiPEgA5C",
    "artifact_id": "browser:dUIpQNHJGFKN8Eq1pW69dQN7BiPEgA5C:screenshot-0001-a1b2c3d4.png",
    "offset": 0,
    "max_bytes": 262144
  }
}
```

The result uses `encoding: "base64"` and includes `offset`, `next_offset`, `size_bytes`, and
`truncated`. The default chunk is 256 KiB and the hard per-call maximum is 1 MiB. Screenshot files
are capped at 10 MiB by default (20 MiB hard configuration ceiling), while snapshot payloads are
2 MiB by default (8 MiB hard ceiling).

`browser.status` returns one owned browser summary; `browser.list` returns only browsers owned by
the requesting IDE session. `browser.close` remains available after Workspace Trust is revoked so
an exact owned browser can always be stopped. It requires explicit `yes` or `confirm`, closes the
CDP transport, terminates only the exact owned process tree after a bounded grace period, and
deletes both the private profile and ephemeral screenshots. `delete_artifacts: false` is rejected:
closed browser sessions have no owned artifact-read lifecycle, so the bridge never leaves
unreachable private screenshot directories behind. IDE session close and `bridge.shutdown` call
the same exact owned cleanup for every remaining browser, including partial launch failures; they
do not kill an arbitrary Chrome process, and cleanup failures remain visible and retryable.

### forge.plan

Creates or reuses an IDE bridge session and records a Forge plan artifact. If `session_id` is not
provided, the bridge first applies normal `session.create` validation and workspace binding rules.

```json
{
  "protocol_version": "1",
  "id": "forge-plan-1",
  "method": "forge.plan",
  "params": {
    "workspace": "/path/to/project",
    "instruction": "Prepare the diff review backend.",
    "mode": "readonly",
    "model": "gpt-4o-mini",
    "base_url": "http://127.0.0.1:9/v1",
    "max_steps": 4
  }
}
```

The result is a stable IDE schema:

- `plan_id`
- `session_id`
- `job_id` (`null` for the synchronous compatibility call; async planning returns a job id)
- `status`
- `source` (`active_memory` for a live plan, `loaded_persisted` when reopened)
- `created_session`
- `project_goal`
- `summary`
- `warnings`
- `incomplete`
- `tasks[]`
- `plan_artifact_id`
- `plan_markdown_artifact_id`

Each task includes `task_id`, `title`, `objective`, `file_scope`, `acceptance_criteria`,
`verification_commands`, `risk_notes`, `dependencies`, `order`, `scope_unknown_reason`, `warnings`,
and `status`. `forge.plan` uses the real Forge planner path. If the planner/model/API key cannot
run, the bridge returns a clear protocol error such as `forge_plan_failed` or
`forge_plan_incomplete`; it does not return a fake single-task shallow plan. Production plan quality
validation marks incomplete plans with warnings when execution-ready tasks lack acceptance criteria,
verification commands, or concrete file scope without an explicit unknown-scope reason.

The adapter maps host-owned Forge `plan.json` data into this schema and does not parse terminal
output. Current protocol planning persists a normal Forge run under `.alysis/runs/<plan_id>/`
and exposes `plan/plan.json` and `plan/PLAN.md` as protocol artifacts.

The bridge emits `status_update`, `plan_node_updated`, and `info_emitted` events while recording the
plan when those events are available from the structured event surface. New clients should prefer
the async methods below; `forge.plan` remains a backward-compatible method for older clients.

### forge.plan.start / forge.plan.result

`forge.plan.start` accepts the same params as `forge.plan` plus an optional client-generated
`idempotency_key`, creates or reuses the same scoped bridge session, and starts planning in a
background bridge job instead of blocking the request until the model-backed planner completes.
Production IDE clients should always send a stable, opaque key for one user submission and preserve
it across lost responses, webview reloads, extension-host restarts, and bridge restarts.

```json
{
  "protocol_version": "1",
  "id": "forge-plan-start",
  "method": "forge.plan.start",
  "params": {
    "workspace": "/path/to/project",
    "instruction": "Prepare the diff review backend.",
    "mode": "readonly",
    "idempotency_key": "forge-request-018f2d1f",
    "model": "gpt-4o-mini"
  }
}
```

The immediate response is:

```json
{"session_id":"20260519T100000Z_abcd1234","job_id":"job_20260519T100001Z_aaaabbbb","status":"started","durably_accepted":true,"duplicate":false}
```

The response is written only after a private SQLite request ledger outside the workspace has
committed acceptance. The ledger stores hashes of the idempotency key and semantic request payload,
not the key or instruction. Repeating the same key and payload returns the same stable `job_id` with
`duplicate: true`; reusing the key for a different payload fails with `idempotency_conflict` and
does not start a planner. The legacy synchronous `forge.plan` compatibility method does not provide
this exactly-once acceptance contract.

`job.status` reports `queued`, `running`, `cancellation_requested`, `cancelled`, `completed`, or
`failed` for that job. Forge Plan jobs include `kind: "forge_plan"` and, after completion, the
resulting `plan_id`. While the job runs, the bridge records and replays structured planning events
through `session.getEvents`; clients should use those events for progress instead of terminal output.

After `job.status` is `completed`, clients call:

```json
{"protocol_version":"1","id":"forge-plan-result","method":"forge.plan.result","params":{"job_id":"job_20260519T100001Z_aaaabbbb"}}
```

The result is the same stable Forge Plan schema returned by `forge.plan`, with `job_id` populated.
`job.status` and `forge.plan.result` consult the durable ledger when the in-memory job registry is
empty. A completed retry after a bridge restart reconstructs the result from the persisted Forge
plan and attaches it to a live inspection or caller session; clients do not need to start the
planner again.

Workers transition from a fenced dispatch lease to a renewable running lease before calling the
planner. A stale queued dispatch can be safely reclaimed because an expired worker cannot claim it.
An expired running lease is different: the durable state becomes indeterminate, protocol status is
terminal `failed`, and `forge.plan.result` returns `forge_plan_indeterminate`. The bridge never
automatically re-executes indeterminate work. Clients must ask the user to review workspace
artifacts before submitting a new request with a new idempotency key.

If the planner fails, `job.status` reports `failed` with a redacted error and `forge.plan.result`
returns `forge_plan_failed`. Active cancellation remains honest: `session.cancel` may return
`cancellation_requested` for cancellable Forge Plan work, but clients must keep rendering the job as
in progress until `job.status`, `session.status`, or `session.list` reports a terminal state. A
backend that cannot cancel the current work returns `non_cancellable` or a structured unsupported
error rather than a fake cancelled result.

### forge.list

Lists persisted Forge plans for a scoped workspace or active session. The registry scans only the
workspace's `.alysis/runs/<plan_id>/plan/plan.json` entries using opaque plan ids; it does not
accept arbitrary file paths. The bridge rejects symlinked `.alysis`, `runs`, run, `plan`,
`execution`, and `execution/patches` registry components before reading persisted plans or exposing
Forge artifact roots.

```json
{"protocol_version":"1","id":"forge-list","method":"forge.list","params":{"workspace":"/path/to/project","max_items":50}}
```

Each item includes `plan_id`, optional `session_id`, `workspace_root`, `status`, `source`,
`project_goal`, `summary`, `task_count`, timestamps, and plan artifact ids.

### forge.open / forge.resume

Opens a persisted Forge plan from the scoped workspace registry. If `session_id` is omitted, the
bridge creates a readonly inspection session so the plan and artifacts can be viewed after the
original session closed or after the bridge restarted.

```json
{"protocol_version":"1","id":"forge-open","method":"forge.open","params":{"workspace":"/path/to/project","plan_id":"20260519T100002Z_aaaabbbb"}}
```

`forge.resume` is an alias for `forge.open`. Invalid, unknown, path-like, or rejected registry paths
return stable protocol errors (`invalid_plan_id`, `forge_plan_not_found`, or
`forge_registry_rejected`). Opening a plan adds only a validated non-symlink Forge run root to the
inspection session.

### forge.status

Returns the current IDE schema for a recorded Forge plan. It normally takes `session_id` and
`plan_id`; for async planning clients may pass `session_id` and `job_id` to retrieve a running
status object or the completed plan result once available.

```json
{"protocol_version":"1","id":"forge-status","method":"forge.status","params":{"session_id":"20260519T100000Z_abcd1234","plan_id":"20260519T100002Z_aaaabbbb"}}
```

### forge.show

`forge.show` returns the same scoped plan schema as `forge.status` plus first-class asset entries,
legacy asset metadata, and an artifact count. It is read-only, uses `session_id` and `plan_id`, and
does not scrape terminal output.

### forge.plan.getState

Returns the active persisted Forge plan state with `ide_revision`, assistant instruction metadata,
goal, validation metadata, tasks, artifacts, and assets. It is read-only and is the typed route for
showing Forge-context `/assistant`, `/goal`, `/task`, and `/forge plan state` data.

### forge.plan.setAssistant

Updates the plan assistant/persona instruction. The VS Code `/assistant` route is dual-mode:
`/assistant show` reads through `forge.plan.getState`, while `/assistant <instruction>` calls this
mutating method. Updates require `workspace_trusted: true`, accept an optional
`expected_revision`, persist the plan, increment `ide_revision` on change, and return audit
metadata without secret values.

### forge.plan.setGoal

Updates the plan goal. The VS Code `/goal` route is dual-mode: `/goal show` reads through
`forge.plan.getState`, while `/goal <goal>` calls this mutating method. Updates require
`workspace_trusted: true`, reject empty goals, accept an optional `expected_revision`, mark
validation metadata stale with `goal_changed`, persist the plan, and return the same bounded
plan-state shape as `forge.plan.getState`.

### forge.plan.updateTask

Shows or updates one existing task by opaque `task_id`. Without `title`, `body`, or `status` it is
read-only. Updates require `workspace_trusted: true`, validate task ids as non-path identifiers,
allow only safe task statuses, accept an optional `expected_revision`, and persist changes to the
plan store without mutating workspace files. VS Code records `/task <id> show` as read-only and
records `/task <id> status|title|body ...` as mutating after parsing the params.

### forge.plan.validate

Returns backend validation metadata for a plan without mutating it. The method is used by the
extension to keep typed Forge plan-edit routes honest without terminal Forge chat scraping.

### forge.plan.regenerate

Regenerates the active persisted Forge plan through the typed planner adapter. It requires
`session_id`, `plan_id`, and `workspace_trusted: true`; accepts optional `expected_revision`,
`instruction`, and `focus`; and returns `plan_id`, `session_id`, `old_revision`, `new_revision`,
`changed`, redacted `summary`, `warnings`, validation metadata, `redacted: true`, and
`secret_values_included: false`. A stale `expected_revision` returns a structured
`stale_plan_revision` error instead of overwriting newer plan state. This is the IDE route for
Forge-context plan regeneration via `/forge plan regenerate`;
`/forge plan <instruction>` remains new plan creation. The method does not expose Forge swarm, broad
execution, auto/fullaccess execution, terminal scraping, or arbitrary shell execution.

The bridge runs the planner outside its dispatch state lock (snapshot → compute → commit): other
methods, event emission, and cancellation stay responsive while a regeneration is in flight. At
commit time the bridge re-checks the persisted plan revision; if a concurrent edit (for example
`forge.plan.setGoal` or `forge.plan.updateTask`) landed while the planner ran, the regeneration
fails closed with the same `stale_plan_revision` error and persists nothing.

### forge.plan.regenerate.start / forge.plan.regenerate.result

Async job form of `forge.plan.regenerate` on the shared bridge job registry, mirroring
`forge.plan.start`/`forge.plan.result`. `forge.plan.regenerate.start` takes the same params as the
sync method (Workspace Trust gated, optional `expected_revision` validated up front), returns
`{session_id, plan_id, job_id, status: "started"}`, and occupies the session's single job slot. The
job honors cooperative checkpoint cancellation around the planner provider call via `forge.cancel`
(or `session.cancel`); a cancelled regeneration commits nothing. `forge.plan.regenerate.result`
returns a progress payload while the job is active, a cancelled payload after cancellation, the
full sync-shaped regenerate result on success, and re-raises the structured failure code (for
example `stale_plan_revision` from the commit-time conflict check) on failure. The sync
`forge.plan.regenerate` remains supported for back-compat.

```json
{"protocol_version":"1","id":"regen-start","method":"forge.plan.regenerate.start","params":{"session_id":"20260519T100000Z_abcd1234","plan_id":"20260519T100002Z_run","workspace_trusted":true,"focus":"demo.py"}}
{"protocol_version":"1","id":"regen-result","method":"forge.plan.regenerate.result","params":{"job_id":"job_20260519T100003Z_ccccdddd"}}
```

### forge.review

`forge.review` runs the existing Forge review engine for one task and returns structured review
data. Params are `session_id`, `plan_id`, `task_id`, `workspace_trusted: true`, and optional safe
model overrides (`model`, `base_url`, `temperature`). Inline `api_key`, `token`, `password`,
`secret`, or credential params are rejected by the global protocol secret policy. The method writes
review artifacts under the validated Forge run, so it is Workspace Trust gated.

The result includes `approved`, `confidence`, `summary`, issue counts, redacted `review_json`,
`review_markdown`, artifact ids, and a structured `action` such as `review_needed` when approval is
not granted. It never prompts in the terminal.

### forge.review.start / forge.review.result

Async job form of `forge.review` on the shared bridge job registry. The review provider call runs
in a job thread, so it no longer blocks the stdio dispatch loop for its duration; the sync
`forge.review` remains for back-compat. `forge.review.start` takes the same params as the sync
method plus nothing new, occupies the session's single job slot, and returns
`{session_id, plan_id, task_id, job_id, status: "started"}`. Review jobs honor `forge.cancel`
(and `session.cancel`) with cooperative checkpoints before and after the provider call — the
review engine itself is a single blocking request, so a cancel that arrives mid-call takes effect
at the post-call checkpoint and the result is discarded. `forge.review.result` returns a progress
payload while active, a cancelled payload after cancellation, the sync-shaped review result on
success, and re-raises the structured failure code on failure.

### forge.attach

`forge.attach` is the IDE-safe attach equivalent. It accepts `session_id`, `plan_id`,
`workspace_trusted: true`, `source_path` or `source`, and optional `title`/`description`. The bridge
resolves the source inside the plan workspace, rejects symlinks and path escapes, requires a regular
file under the size limit, then stores it through the first-class asset surface. It returns the same
mutation shape as `forge.assets.add`.

### forge.assets.list

Lists first-class Forge assets for `session_id` and `plan_id`. Optional `include_deleted` controls
tombstones. Read-only.

### forge.assets.show

Shows one asset by opaque `asset_id`. Asset ids are validated as opaque identifiers; path-like ids
are rejected. Read-only.

### forge.assets.add

Adds a workspace-scoped asset using `source_path` or `source`, optional `title`, `description`,
`pinned`, `wait`, and `link`. Requires `workspace_trusted: true`. The bridge rejects symlinks,
workspace escapes, missing/non-regular files, unsupported file types, and sources larger than the
advertised limit. Binary payloads are never serialized through JSONL.

### forge.assets.delete

Deletes an asset tombstone by opaque `asset_id`. Requires `workspace_trusted: true` and explicit
`yes: true` or `confirm: true`.

### forge.assets.edit

Updates asset metadata with `title`, `description`, `pinned`, and optional synchronous
`refresh: true`. Requires `workspace_trusted: true`; empty edits are rejected.

### forge.assets.refresh

Refreshes asset comprehension synchronously for a single `asset_id`. Requires
`workspace_trusted: true`. Protocol v1 does not start unmanaged background refresh jobs.

### forge.assets.cancelPending

Requests cancellation of any in-process asset comprehension owned by the current bridge instance.
There is no persistent background CLI job to scrape; idle responses are structured.

### forge.assets.checkPlan

Checks plan asset references and returns `deleted_referenced`, `missing_referenced`, `pinned_added`,
and `ok`. Read-only.

### forge.assets.pruneLegacy

Prunes verified legacy asset files after migration. Requires `workspace_trusted: true`; when files
would be deleted, callers must pass `yes: true`. The bridge refuses unverified legacy files and
validates legacy paths under the Forge run before deletion.

### forge.executePreview

`forge.executePreview` is a non-mutating execution-readiness check. It validates the scoped
`session_id`, `plan_id`, optional `task_ids`, requested `mode`, Workspace Trust status, sandbox
profile label, plan completeness, verification commands, and approval scope safety. It must not
write files, run shell commands, start workers, execute tools, or auto-approve any action.

```json
{
  "protocol_version": "1",
  "id": "forge-execute-preview",
  "method": "forge.executePreview",
  "params": {
    "session_id": "20260519T100000Z_abcd1234",
    "plan_id": "20260519T100002Z_aaaabbbb",
    "task_ids": ["T01"],
    "mode": "review",
    "workspace_trusted": true,
    "sandbox_profile": "default"
  }
}
```

The result includes `selected_task_ids`, `execution_mode_requested`, Workspace Trust requirement and
status, estimated file scopes, verification commands, required approvals with exact safe session
scopes, runtime approval requirements for dynamic shell/custom/MCP-like tool requests, sandbox
profile `supported`/`available`/`diagnostic` fields, known risks, missing prerequisites,
`preview_ready`, `real_execution_supported`, `unsupported_reason`, active cancellation support, and
the parsed execution policy fields `max_steps`, `no_log`, `subagents_supported`,
`subagents_enabled`, `subagents_policy`, and `next_recommended_action`. Preview validates
`max_steps` and `no_log` the same way real execution does. `subagents_enabled` is not accepted for
IDE Forge Execute v1; callers must use `features.forge.execute.subagents_supported` instead of
probing with params. `mode: "readonly"` can preview without requiring Workspace Trust when the
executable origin is trusted by the host. `review` previews require Workspace Trust and a verified
sandbox profile before execution can start. `auto` previews remain non-mutating and report real
execution as unsupported. The preview itself does not write files, run tools, run verification
commands, or auto-approve anything.

### forge.execute

`forge.execute` validates `session_id`, `plan_id`, explicit `task_ids`, `mode`, Workspace Trust
status, sandbox readiness, plan completeness, optional `max_steps`, optional `no_log`, and exact
approval-scope readiness. When
`dry_run: true` is supplied it returns the same non-mutating preview schema as
`forge.executePreview`.

Real execution in protocol v1 is deliberately narrow:

- `mode` must be `review`; `auto` and `fullaccess` fail closed.
- `task_ids` are required and execution is limited to those selected tasks.
- Execution is sequential review execution for the selected tasks. Protocol v1 does not expose the
  full CLI `forge swarm` contract: parallel swarm workers, integration-gate merge batches,
  replanning, and merge/push orchestration are not part of IDE Forge Execute v1.
- Workspace Trust must be reported by the host.
- A strict sandbox profile must be available; `warn` and `off` are not accepted for mutating IDE
  execution because they can permit host fallback or no sandbox.
- Each selected task must have acceptance criteria, verification commands, and scoped files.
- `max_steps` is a positive integer override for the selected task execution budget. When omitted,
  the loaded CLI config budget is used. The VS Code extension sends this only from a positive
  `alysis.forgeExecuteMaxSteps` setting.
- `no_log: true` disables JSONL session logging for the managed execution runner. It does not
  suppress structured protocol events, bounded artifacts, or redacted errors. The VS Code
  extension forwards `alysis.forgeExecuteNoLog` explicitly as `no_log`.
- IDE Forge Execute v1 advertises `features.forge.execute.subagents_supported: false`, rejects
  `subagents_enabled` request params, and keeps subagents disabled until worker lifecycle, sandbox,
  approval, and cancellation semantics are strong enough.
- File writes, shell commands, custom/MCP tools, and verification commands remain host-approved
  through scoped approval requests. No action is auto-approved.
- Dependent selected tasks only run after their dependencies have actually completed. If a selected
  prerequisite fails verification or is blocked, downstream selected tasks are marked blocked and do
  not start.
- Execution is job-based. `job.status` becomes `completed` for successful review-mode execution and
  `failed` for verification failure, blocked dependencies, agent failure, out-of-scope changes, or
  protocol errors.
- Execution emits structured status, task, worker, verification, review, warning, info, and error
  events. Clients must not parse terminal output.
- The terminal `forge_execute_completed` or `forge_execute_failed` info event is emitted only after
  the bridge has recorded terminal `job.status`, `completed_at`, and `exit_code`; `session.list`
  observes the same terminal active-job summary after that event.
- Generated reports, context packs, verification artifacts, and patch diffs are exposed only through
  scoped artifact/diff APIs.
- Active runtime cancellation is cooperative checkpoint cancellation. Failed `forge.cancel` or
  `session.cancel` must not be rendered as successful cancellation, and `cancellation_requested`
  must not be rendered as terminal `cancelled`.

```json
{
  "protocol_version": "1",
  "id": "forge-execute",
  "method": "forge.execute",
  "params": {
    "session_id": "20260519T100000Z_abcd1234",
    "plan_id": "20260519T100002Z_aaaabbbb",
    "task_ids": ["T01"],
    "mode": "review",
    "workspace_trusted": true,
    "sandbox_profile": "strict",
    "max_steps": 3,
    "no_log": true
  }
}
```

### forge.cancel

`forge.cancel` requests cooperative checkpoint cancellation for the active Forge job matching the
given `session_id` and `plan_id`. It returns `status: "cancellation_requested"` when the request is
accepted, `status: "no_active_job"` when no matching Forge job is running, or `status:
"non_cancellable"` when the active job cannot observe cancellation. Pending Forge approvals are
resolved as cancelled so review-mode execution can stop without terminal prompts.

This method is capability-gated by `features.forge.cancel.supported`; method presence alone is not a
promise of active cancellation. It is not a hard interrupt. Covered job kinds are advertised in
`features.forge.cancel.covered_job_kinds` and include plan, plan-regeneration, review, execute, and
swarm jobs. Session/job status uses `active_job` only for queued,
running, or `cancellation_requested` jobs; completed, failed, and cancelled jobs move to `last_job`
so status views do not show stale active Forge jobs after terminal events.

### Durable Forge swarm lifecycle

Workspace-Trust-gated swarm execution is both a live bridge job and a durable, restart-safe job.
`forge.swarm.start` params: `session_id`, `plan_id`, `workspace_trusted: true`, optional bounded
`parallel` (1–8, default 2), optional `idempotency_key`, and `approval_scope_grants` — launch-time
pre-grants shaped as `[{kind, scope}]` using the exact
allow-for-session scope payloads the IDE already receives in approval prompts; they are recorded
into the session's approved scopes so matching dangerous actions never prompt. The job runs the
real swarm engine in strict write-scope mode with the swarm-layer dispatch guard and the scheduler
invariant that concurrent workers always hold disjoint (case-insensitively compared) write scopes.

Supplying an `idempotency_key` makes start retry-safe: repeating the same request returns the same
durable job instead of creating duplicate provider work. Reusing a key with a changed execution
specification or permission fingerprint fails closed. If the key is omitted, the bridge generates
one for that call. Durable records live outside the workspace but are scoped by bridge owner,
canonical workspace, and session. Execution specifications and results are bounded and redacted.
Worker lease tokens, permission fingerprints, and internal execution specifications are never
returned through the IDE protocol.

`forge.swarm.list` (required `session_id`, optional `limit`, default 100 and maximum 200) returns
the bounded durable job history for that live session. Before list/status/result operations, an
expired running lease is atomically recovered to `interrupted`; recovery never silently restarts
provider work. Public durable state includes `job_id`, `state`, `revision`, `attempts`,
`resume_count`, lifecycle timestamps, `result_available`, bounded error metadata, aggregate usage,
and `resumable`.

`forge.swarm.resume` is the only operation that restarts an interrupted or failed durable run. It
requires `session_id`, `plan_id`, `job_id`, and `workspace_trusted: true`; it accepts optional
fresh `approval_scope_grants`, `expected_revision`, bounded `parallel`, and normal model overrides.
Omitting grants represents a fresh empty scope. Resume fails closed unless that fresh permission-
scope fingerprint exactly matches the original start, and a stale revision is rejected rather
than overwriting newer state. A claimed
worker receives a private, renewable, generation-fenced lease. Expired, cancelled, or superseded
workers cannot publish usage or terminal results. Usage events are attempt-idempotent, so retries do
not double count calls or tokens.

**Approvals replace `--yes`:** IDE swarm workers never run with `--yes`-style auto-approval.
Dangerous actions (sensitive shell commands and every other gate the agent layer routes through
approvals) raise approvals through the existing per-session system with task attribution
(`forge_swarm_task_id`/`worker` in the prompt metadata, `swarm_approval_requested`/`_resolved`
info events, and a `swarm_worker_state_changed` `approval_pending` state). The requesting worker
pauses at that checkpoint — before the dangerous action runs — while sibling workers continue;
`approval.respond` resumes it. Swarm approvals are persistent: a timeout re-emits
`approval_pending` and leaves the task paused; it is never auto-denied and never killed
mid-mutation. Cancelling the run resolves pending approvals as cancelled and the workers stop
cooperatively. Terminal `forge swarm` keeps `--yes` as a terminal-only convenience.

**Merge is review:** under IDE swarm nothing merges. The engine runs the review merge strategy —
completed tasks stay `ready_for_merge` with preserved worktrees, and the bridge harvests one
review diff per task (`execution/harvest/<task>.diff` + an untracked-files sidecar). The run
result and `forge.swarm.result` carry a `review` payload listing the pending items.

`forge.swarm.cancel` (params `session_id`, `job_id`, optional `reason` and
`expected_revision`) first atomically marks the durable job cancelled and fences the active worker,
then sets the live job's cancellation event, which is the same cooperative signal the swarm engine
checkpoints on: running workers stop at their next checkpoint with task status `interrupted` and
preserved worktrees,
queued tasks never start, and nothing merges after the cancel. `forge.cancel` with the plan id
covers swarm jobs too. `forge.swarm.status` returns the job summary plus live
`task_status_counts` and durable status when available. `forge.swarm.result` accepts `job_id` and an
optional `session_id`; `session_id` is required after a bridge restart when the job is no longer in
the process-local registry. It returns a progress payload while active, a cancelled
payload (with the partial run summary) after cooperative cancellation, and otherwise the run
summary: `exit_code`, `run_status`, `clean`, `interrupted`, `interrupted_task_ids`,
`verification_status`, `reason_codes`, and `task_status_counts`.

`getCapabilities.features.resumable_swarm` is the compatibility gate for this lifecycle. It
advertises durable external storage, workspace/session scoping, idempotent start, explicit resume,
fresh permission fingerprints, fenced leases, restart recovery, atomic cancellation, exactly-once
usage events, and the six lifecycle methods. Clients must not infer these guarantees from the
presence of `forge.swarm.start` alone.

Swarm progress streams as typed, bounded, backend-redacted events through the session surface and
the bounded replay ring (`session.getEvents` replays them after reconnect): task lifecycle on
`swarm_worker_state_changed` (worker_id = task id; states `scheduled`, `started`, `progress`,
`interrupted`, `failed`, `merged`, with `approval_pending` reserved for approval routing),
scope-violation and worker warnings on `warning_emitted`, and run lifecycle
(`swarm_started`/`swarm_completed`/`swarm_cancelled`) on `info_emitted`/`warning_emitted`.

### forge.swarm.review / forge.swarm.apply / forge.swarm.discard

The per-task review surface for the review merge strategy (the same review semantics as the
Execute v1 surface: diffs land in the working tree only on explicit apply; nothing auto-applies;
nothing ever commits). `forge.swarm.review` (read-only) lists per-task items: state
(`ready_for_merge`, `applied`, `discarded`, `failed`, `interrupted`, ...), the harvested
`diff_artifact_id` (readable via `artifact.read`), and — mandatorily surfaced — the
untracked-files sidecar (`untracked_files` plus a human-readable `untracked_files_note`,
"untracked files created: …"), because untracked file contents are not part of the diff and must
never land silently. Failed/interrupted items carry a structured `recovery` offer
(`regenerate_subtree` via `forge.plan.regenerate.start`, with suggested instruction/focus;
interrupted items also point at `forge.swarm.reconcile` harvest).

`forge.swarm.apply` (Workspace Trust gated; explicit `task_ids`, batch-capable) applies each
task's harvested diff to the canonical working tree with `git apply --check` first — per-task
atomic, sequential in plan order, no staging, no commit (`working_tree_committed: false`); the
applied task is marked `done` and an apply marker makes the call idempotent. The response repeats
`untracked_files_not_applied` per task. `forge.swarm.discard` (Trust + `yes`/`confirm`; explicit
`task_ids`) drops a task's preserved worktree, marks it `candidate_rejected`, and keeps harvest
artifacts for audit; already-applied tasks refuse discard (revert through source control instead).
Both refuse while a swarm job is active on the session.

### forge.swarm.reconcile

Post-crash/reload reconciliation over a run's persisted artifacts and preserved task worktrees.
The default `action: "report"` is strictly read-only: it enumerates per-task state — `merged`,
`interrupted` (with `diff_available`), `unstarted`, or the raw task status — plus worktree
presence, patch/harvest artifact presence, and merge commit hashes, without touching the backend
worktree ensure/reuse lifecycle. Explicit actions: `harvest` (Workspace Trust gated) writes
per-task diff artifacts under `execution/harvest/` (working-tree diff against the base branch plus
an untracked-file listing sidecar) for later review; `discard` (Trust + `yes`/`confirm` gated)
drops preserved worktrees safely, refusing anything outside the run's `worktrees/` directory.
Both actions are idempotent — harvesting twice rewrites the same artifacts, discarding twice
reports `already_absent` — and nothing mutates without an explicit `action` param. Optional
`task_ids` filters the scope; unknown ids fail closed with `task_not_found`.

### diff.list

Lists protocol-known diff review artifacts for a session and optional plan. The bridge only exposes
diffs created under recorded Forge run artifact roots and returns opaque ids; callers cannot pass
file paths for listing or reading. Reopened persisted plans work after `forge.open` because the
bridge attaches that run's validated artifact root to the readonly inspection session. Symlinked
patch directories are rejected, and symlinked diff files are not exposed.

```json
{"protocol_version":"1","id":"diffs","method":"diff.list","params":{"session_id":"20260519T100000Z_abcd1234","plan_id":"20260519T100002Z_aaaabbbb"}}
```

Each item includes `diff_id`, `session_id`, `plan_id`, `job_id`, `file_path`, `status`,
`old_label`, `new_label`, and `size_bytes`. When the scoped plan has no diff artifacts yet, the
response returns an empty `diffs` array with `empty_reason`.

### diff.get

Reads a diff by opaque `diff_id` with a byte cap. `session_id` and `plan_id` are required so the
lookup is scoped to a single recorded Forge plan. The bridge resolves ids only from that plan's
validated diff registry; `diff_id` is not a path and cannot be used for arbitrary file reads.
Symlinked patch directories are rejected with `forge_registry_rejected`; symlinked or missing diff
files resolve as `diff_not_found` without exposing external paths.

```json
{"protocol_version":"1","id":"diff","method":"diff.get","params":{"session_id":"20260519T100000Z_abcd1234","plan_id":"20260519T100002Z_aaaabbbb","diff_id":"diff_abc123","max_bytes":65536}}
```

The result includes `diff_id`, `session_id`, `plan_id`, `job_id`, `file_path`, `old_text`,
`new_text`, `old_artifact_id`, `new_artifact_id`, `unified_diff`, `truncated`, `size_bytes`, and
`max_bytes`. Current Forge execution patch artifacts are returned as `unified_diff`; old/new file
snapshots are `null` until a runtime path records them as explicit artifacts.

Diff protocol payloads are review previews, so the bridge applies the same protocol secret
redaction used for other responses and events. The underlying Forge artifact on disk is not modified
by preview redaction.

## Composer catalog and explicit skills

`commands.list` is a read-only management method. With `mode: "chat"` (default)
it returns `commands: [{name, usage, description}]` from the CLI slash command
catalog. `mode: "forge"` additionally includes Forge commands. Hosts must expose
only entries they can execute or map to a native UI action; this endpoint does
not execute terminal commands.

`skill.list` and `skill.info` include `available` for each skill: lifecycle-enabled,
allowed by the effective plugin activation and existing workspace trust, and enabled
in configuration. Reading the catalog never grants trust. `enabled` continues to
describe the skill lifecycle setting.

`skill.list` additionally returns the effective configuration's `skills_enabled`
flag. Existing entry `enabled` fields continue to describe lifecycle state.

The capability `features.explicit_skill_invocation` advertises version 1,
field `skill`, and scope `turn`. Ordinary `chat.send` accepts:

```json
{"session_id":"example","message":"The prepared chat instruction","skill":{"name":"code-review","arguments":"Review these changes"}}
```

The engine resolves the name or alias against the session's effective,
plugin-filtered skill registry and uses the same bounded context builder as CLI
`/skill`. Arguments are the current user task, without host-added history or
attachments. Skill text is never accepted from the renderer. Missing, disabled,
and sensitive host-context selections fail before enqueueing. The normalized
selection is part of durable prompt identity and is resolved again at execution;
it contributes ephemeral context to that turn only. `skill_invoke` activity names
the selected skill. Hosts with kept sessions should refresh discovery before an
explicit invocation when local skills may have changed.


## Host integration additions in 0.15

These methods require a compatible host and negotiated capabilities. Their
presence does not mean that every editor exposes a control for them.

- `agents.list` returns the effective subagent and role model settings.
- `feedback.prepare` builds a redacted report for review. `feedback.send` sends
  the reviewed report ID; diagnostics are optional.
- `browser_start.respond`, `computer.respond`, and `task_budget.respond` return
  host decisions for the matching active operation. Nonces and expiry checks
  bind responses to their session and job.
- `host.action.dispatched` records dispatch using the host action ID, workspace
  fence, and capability fingerprint. Stale or duplicate receipts are rejected.
- `session.userQuestions.get` reads questions for a session and job.
  `session.userQuestions.answer` submits answers for a request ID, and
  `session.userQuestions.touch` keeps an active question request alive.
  Responses for an ended or cancelled job are rejected.

The VS Code typed client supports subagent settings queries and feedback calls.
Computer Use, browser-start consent, task budgets, dispatch receipts, and the
structured user-question UI require a host that implements those interfaces.
