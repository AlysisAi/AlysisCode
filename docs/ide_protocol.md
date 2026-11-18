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

`provider.auth.status` is an optional read method for native subscription connections. It takes
`provider_id` and returns a fresh provider-owned connection status, including `authenticated`,
`verified`, and the non-secret `route_scope` fingerprint from that same validated snapshot.
The bridge returns no tokens or account label. Each request reads the credential store again;
there is no cached successful identity. A host can reuse its settings engine for these reads
and fall back to `auth status <provider_id> --json` when an older engine lacks the method.

`session.create` also takes `task_output_directory` when the engine advertises the
`session_task_output_directory` feature: the host's default output folder for that session, the
value a host may otherwise set at launch as `ALYSIS_DESKTOP_TASK_OUTPUT_DIRECTORY` (the session's
value wins). Given per session, one engine started ahead of time can serve any chat. It is checked
against the task when used, exactly as the environment value is; a location, never a grant.

`provider.auth.selectModel` (`provider_id`, `model`, optional `reasoning_effort`) and
`provider.auth.reasoning` (`model`, `profile`) are `auth select-model` and `auth reasoning` without
a new process, one shared implementation each. The first is a Workspace Trust write: it asks the
provider for the account and its models outside the settings lock, then activates the subscription
and its model in one locked write. The second returns the model's effort choices as `models`,
saving nothing. A host falls back to the CLI commands when an older engine lacks them.

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
`getCapabilities`. Chat/run, management domains, and artifacts each
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
`subagent_state_changed`, `verify_gate_result`, `approval_reviewed`, `error_raised`, `prompt_for_input`,
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

IDE clients with multiple surfaces, such as chat and code review views, should route events by explicit
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

Optional `task_relation: "new_task"` binds this queued prompt's current canonical user
instruction as the turn objective. Only unpinned, nondelegated ordinary interactive
chat supports it; present null or any other value is invalid. Absence retains legacy
behavior. The intent is durable with its original prompt and participates in payload
idempotency; it does not restore an old objective or reset workspace evidence.
Sensitive sessions, `run.start`, and runtimes without an explicit consumer refuse it.


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
(such as code review) it fails with `session_busy`.

A turn can end before it reads a message. The bridge then holds the message for the session, so
the next turn (for example a queued `chat.send`) never reads it. `chat.steer.take` (`session_id`)
returns held messages, oldest first, so the host can send them as a turn of their own, as the TUI
does. A running turn's own messages stay with it. `session.clear` discards held messages.

### feedback.prepare, feedback.send

The private "Send to Alysis" report of the TUI's `/report`, for hosts. `feedback.prepare`
(`feedback`, optional `session_id`, `workspace`, `include_diagnostics`) builds the report and sends
nothing: the redacted description (1 to 8,000 characters), the engine version and, only when
`include_diagnostics` is true, OS, Python version and the last 20 messages of the live session
(text and tool calls; images, provider state and secrets removed; at most 180 KB). A session that
is not open in this bridge is read from its retained log, the way `session.resume` rebuilds it, and
nothing is written to it; that needs `workspace` and, when the host keeps logs elsewhere,
`session_dir` (as in `session.create`). Without `session_id` it needs `workspace` and includes no
conversation. The result has `report_id`,
`report` (the payload as an object for the user to review, after the bridge's redaction; the bytes
`feedback.send` sends are redacted the same way), `include_diagnostics` and `signed_in`. Sensitive (Computer Use) sessions refuse it.
Invalid text fails with `feedback_invalid` and the engine's message.

`feedback.send` (`report_id`) sends exactly the reviewed bytes. Signed in to Alysis
(`alysis login`), the report is tied to the account; otherwise it is sent without credentials and
the server admits it anonymously, rate-limited. Another provider's key is never sent. The result
has `report_id`, `created_at` and `anonymous`. A refusal has its own code: `feedback_rate_limited`
(429), `feedback_sign_in_required` (401 without a sign-in), `feedback_sign_in_expired` (401 with
one), `feedback_conflict` (409), `feedback_too_large` (413), `feedback_unavailable` (404); anything
else is `feedback_delivery_failed`. Messages never contain server text. Retrying the same
`report_id` is safe and never stores the report twice. The bridge keeps the 16 most recent prepared
reports; an older or unknown ID fails with `feedback_not_prepared`. The call runs on the request
loop; each network step waits at most 15 seconds. Advertised as `features.feedback_delivery`.

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

Returns the last provider-reported request input and output usage, including cache and reasoning
fields when reported. Missing usage stays unavailable rather than becoming zero. Interaction-wide
usage is labeled separately and is not presented as the size of one context window. Cached input
remains part of input, not an additional charge against context. Output reserve is capacity held
for generation, not tokens already used.

The detailed context inspection categories are tokenizer estimates of the current locally assembled
request, separate from the last measured provider request. `provider_usage.input_breakdown` is optional:
when the provider supplies a complete consistent partition, it contains measured `instructions`,
`tools`, and `conversation` counts. It is omitted otherwise; local estimates never fill missing
provider counts. The ChatGPT subscription route has supplied this attribution in live tests, but it
is not a guarantee for every provider or response.

```json
{"protocol_version":"1","id":"context-1","method":"session.context","params":{"session_id":"20260519T100000Z_abcd1234"}}
```

### session.artifactChecks

Returns `{session_id, reports}` for checks already observed by the active session's `artifact_check`
tool. Each report identifies the workspace-relative path, exact `documentSha256`, check results and
bounded preview metadata. The call is read-only and does not execute a checker. An empty list means
no checks have been recorded, not that files passed. Clients must match the artifact hash and validate
preview paths before showing results. Structure, formula and render checks do not prove business
requirements or visual quality; `visualReview` remains `not_performed` by the checker.

```json
{"protocol_version":"1","id":"checks-1","method":"session.artifactChecks","params":{"session_id":"20260519T100000Z_abcd1234"}}
```

### session.outputArtifacts

Returns `{session_id, job_id, task_id, paths}` for the exact completed file turn.
Both `session_id` and `job_id` are required; no other parameters are accepted.
The backend checks the completed job and current task identity before returning
declared, changed paths relative to the requested workspace. This read-only
query does not open files or grant access to them. Sensitive-media sessions,
unfinished or unsuccessful jobs, and stale task identities are refused with
`output_artifacts_unavailable`.

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
`independently_resumable: false`, and the `subagent_state_changed` lifecycle event. It does not
run a subagent.

```json
{"protocol_version":"1","id":"subagents-1","method":"session.subagents.status","params":{"session_id":"20260519T100000Z_abcd1234"}}
```

### session.subagents.setEnabled

Toggles the live session `subagents_enabled` setting for subsequent turns. It requires
`workspace_trusted: true` and returns audit metadata without secret values.

```json
{"protocol_version":"1","id":"subagents-toggle-1","method":"session.subagents.setEnabled","params":{"session_id":"20260519T100000Z_abcd1234","enabled":true,"workspace_trusted":true}}
```

When an enabled chat session lets the model delegate, the delegate remains synchronous child work
inside the owning chat job. Start and terminal state are emitted as correlated
`subagent_state_changed` events with a non-authoritative `subagent_run_id`, bounded description or
error text, mode, elapsed time, step count, and child session id when available. Nested tool events
carry `worker_id` and `role`. The existing parent `session.cancel` token stops these delegates, and
their approvals continue through the parent session's host-managed approval channel. The protocol
does not offer a misleading independent resume or cancel method for them.

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

A successful retained-log replay returns `task_state_recovery: "not_restored"` and
`task_id: null`: workspace verification evidence may be retained, but no conversation
objective is restored. Its workspace baseline identity must not be used as an
objective ID. Failures do not publish this successful result; old peers may omit it.


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
- `profile.selectModel`
- `profile.rename`
- `profile.presets`
- `profile.preset`
- `profile.convert`

`profile.add` accepts provider metadata such as `base_url`, `api_key_env`, default model, and notes,
but not raw `extra_headers` values. If header values are required, the result is `changed: false`
with an action like `{"kind":"requires_secret_storage","next_step":"open_configure_provider"}`.
`profile.preset` applies the same URL-userinfo rejection to custom `base_url` overrides.

`profile.selectModel` takes `name` and `model` and is `alysis profile select-model` without a new
process (one shared implementation): a single locked write that activates the API profile, keeps
`model` as its default and routes execution natively. A subscription profile or a hosted profile
without an API key is refused with `config_error`; nothing is written then.

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
commands, URLs, headers, environment values, or credentials. A server that did not start when the
session began reports `connection_state: "failed"` and `startup_failed: true`; enable and restart
refuse it because its tools are not in the session. `mcp.status` rows carry the redacted reason as
`startup_error` (null for a server that started), and the other servers load normally. `enable`, `disable`, and `restart`
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
- `ext.custom.save`
- `ext.uninstall`
- `ext.enable`
- `ext.disable`

`ext.search` accepts an optional string `query`. An omitted, empty or whitespace-only
query lists the full curated registry; non-string values are rejected.

`ext.install`, `ext.uninstall`, `ext.enable`, and `ext.disable` are mutating package operations and
therefore require Workspace Trust plus explicit confirmation. Search/list/info are structured
metadata reads and do not parse terminal output. `ext.install` also requires explicit package trust
review. A request without `trust_approval` returns `changed: false`, a redacted `trust_request`
containing plugin/source/commit/manifest/components/permissions metadata, and an
`action.required_approval` object. The client must present that review to the user and call
`ext.install` again with `yes: true` and the unchanged `trust_approval` object. `yes: true` alone
does not approve package code, hooks, tools, skills, or MCP components.
`ext.custom.save` saves the user's own online plugin (Plugins M2d): `plugin_id` is
`custom.<name>` and `descriptor` is the exact descriptor text the desktop app built (one HTTPS
`[[components.custom_connector]]`, the person's tools and "only reads" marks, optional plugin
skills; nothing that runs). It uses the same two-step review as `ext.install`: the first call
returns `action.method: "ext.custom.save"` and `required_approval`; the second carries `yes: true`
and the unchanged `trust_approval`. The engine pins the exact bytes (SHA-256), so any edit needs a
new review. Custom plugins are user scope only, and `custom.*` ids can't come from the registry
or git.
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

Clients with separate UI controllers must answer only approvals for sessions/jobs
they own. If the owning UI cannot present an approval, it should deny fail-closed with a clear
surface-specific reason. No approval should be granted silently.

`allow_for_session: true` is exactly scoped, never broad by approval kind. The bridge persists
allowances only in memory on the `BridgeSession`, and session close or bridge restart drops them.
Supported boundaries are:

- shell approvals (`shell_run`, `shell_background`): exact command hash
- custom and MCP-like approvals (`custom_tool_run:<name>`, `mcp_*`): exact backend-provided
  action string hash
- MCP tool approvals (`mcp_tool_run:<server_id>/<tool_name>`): exact hash of the tool alias, so the
  allowance covers that one tool for any arguments. The approval's `action.tool_identity` carries
  `mcp_server_id` and `mcp_tool_id`. Tools the server marks destructive never get a session
  allowance
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

### Desktop host responses: computer.respond, task_budget.respond, browser_start.respond

These methods answer host requests that the bridge emits only to the Alysis Code desktop
app, and only for sessions created with the matching negotiated `host_capabilities`
(`sensitive-host-context-v1` / `computer-use-host-v1`, task budgets, and the browser start
handoff; see the `computer_use_host`, `task_budget` and `browser_start_v1` capability
features). Editor integrations that do not negotiate these capabilities never receive the
corresponding `computer_use_request`, `task_budget_request` or `browser_start_request`
events.

- `computer.respond` resolves one pending Computer Use request. The response must repeat the
  request's exact binding: `session_id`, `request_id`, `response_nonce`, `operation_id`,
  `job_id`, `tool_call_id`, `tool_name`, `arguments_fingerprint` and `expires_at`, plus `ok`
  and either `result` or `error` (`code`, `message`).
- `task_budget.respond` acknowledges or refuses one private task-budget request with
  `session_id`, `operation_id`, `job_id`, `request_id`, `response_nonce`, `lease_id`, `ok`,
  and a refusal `code` or a renewed `task_budget` lease.
- `browser_start.respond` answers the owner-bound browser start handoff with `session_id`,
  `job_id`, `tool_call_id`, `operation_id`, `host_nonce`, `request_id`, `allow`, and an
  optional refusal `code`.

Each response is single use: a mismatched binding, an expired request or a second answer
fails closed (`duplicate_response`, `unknown_computer_request`, `task_budget_refused`), and no
response value is echoed into errors. Because the host response binding is checked first,
these three methods apply their credential-field guard after the binding match instead of
before dispatch; they still refuse a request that carries credential fields.

Prepared Computer Use actions carry `approval_preflight.expires_at`, a UTC deadline
at most 30 seconds ahead. Transport and review time count toward that deadline.
The engine checks wall-clock and monotonic time before execution; expiry or
cancellation does not replay an action or create a replacement approval.

Hosts can negotiate `computer_use_host.upload_objective_evidence_version: 1` and
supply an `upload_objective` binding with `version: 1`, `objective_digest` and
`task_scope_digest`. This requires the upload and verification tool families.
Only a matching `computer_verify` evaluation for an upload initiated in this
session can satisfy that objective. Upload status, model text and restored
history cannot supply completion evidence or recreate that binding.

### Turn-scoped user questions

`session.userQuestions.get` requires `session_id` and `job_id` and returns the matching
broker's `{session_id, job_id, questions}` snapshots. It can inspect an active job or
that session's last job. `session.userQuestions.answer` additionally requires
`request_id` and `answers` (one `{id, selected: string[], text}` object for each question).
`session.userQuestions.touch` requires the same identifiers and renews the pending
question's interaction lease. Neither answer nor touch accepts a finished, cancelled,
mismatched or expired turn. A preselected option is not an answer. The desktop presents
these questions; the VS Code client exposes typed methods but has no presenter yet.

`host.action.dispatched` is a host-only acknowledgement for negotiated
`approval_protocol: 1`. It requires exactly `session_id`, `host_action_id`,
`workspace_fence` and `capability_fingerprint`. A current, unresolved execute-phase
request with matching active job and fences is required. It marks the dispatch boundary
once and returns `{status: "dispatched", host_action_id}`. Duplicate or stale receipts
fail; the receipt grants no new execution authority. The existing VS Code host does not
yet negotiate this protocol; its typed client method alone does not enable it.
### Resolved Computer Use entry (withdrawn)

The global rollback keeps `computer_use_start_resolved_v1` unadvertised and rejects
production descriptors. The following describes the dormant contract for retained
fixtures; W11 lifecycle negotiation does not re-release it. In that contract,
`features.computer_use_start_resolved_v1 = {"supported":true,"version":1}` would advertise
an optional request-owned route. An eligible ordinary `session.create` would carry
`computer_start_resolved_host`; its immutable descriptor is echoed exactly only in the
owning private reply. The model receives `computer_start_resolved_v1`, with no initial
inventory and no requirement to discover first. Legacy entry remains unchanged.

The private `computer_start_resolved_request` event carries the full plan, derived
selection, and exact capture/request binding. `computer_start_resolved.respond` repeats
`version`, `session_id`, `job_id`, `tool_call_id`, `operation_id`, `host_nonce`,
`generation_nonce`, `inventory_nonce`, `request_id` and Boolean `allow`. A denial adds
its closed `code`; only `computer_use_target_unverified` also requires
`unverified_target_ids`. Success acknowledges `status: "accepted"` and `request_id`.

V lexical bounds and duplicate/integer-token checks precede generic frame decoding.
Responses match the pending binding before content validation. Foreign or late replies
cannot consume it; a malformed owned reply poisons it. Stop, retirement and the original
bounded exchange deadline cannot be extended by a response. Descriptor, plan, selectors
and transport values remain private and excluded from durable/display diagnostics.
This route does not change protected consent, authority or observation controls.

### W11 shared-engine integration status

This U1 source composition is unqualified. The existing `session.close` method and
its legacy receipt do not prove worker settlement or permit shared-engine CU.
`computer_use_session_isolation_v1` is not advertised until the complete W11-F1 N
wire/state/owner contract is implemented and qualified together with O provenance.
Sensitive session creation is refused in this intermediate composition. Ordinary
chat remains independent; no legacy separate protected-engine fallback qualifies CU.

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

### session.close

Optionally advertised idle-session retirement for clients that keep an engine process
running between operations. Accepts `session_id`. Active jobs are refused with
`session_busy`; cancel them and wait for their terminal result first. The method fences
new work, cancels queued follow-ups and waits for terminal workers under the existing shutdown grace, including
cleanup that runs after a terminal event. It then closes session resources and removes
the session's volatile authority, events and job ids. Successful response:
`{"session_id":"chat-one","status":"closed","worker_settled":true}`.

Clients must verify that exact session and both completion fields before pooling the
process. A timeout, cleanup failure or ambiguous response requires process retirement.
Sensitive computer-use sessions support this method, but remain single-operation
sessions: a subsequent operation must create a new session and obtain fresh host
authority. Runtimes without the method retain the process-per-operation fallback.

### session.cancel

An optional `job_id` qualifies cancellation to that exact job in this session. Only
**absence** selects the legacy session-wide behavior described below. A present
non-string/empty/invalid identifier is `invalid_field` (maximum 256 characters;
`[A-Za-z0-9][A-Za-z0-9._:@/-]*`). Unknown, pruned, foreign, non-current or absent-active
jobs return `job_not_found` / `Job was not found.` before any effect. A retained
terminal job returns its existing summary read-only, including when another job is
active or `close_when_idle` is supplied; it neither closes the session nor cancels
queued work. Qualified responses identify the requested `session_id` and `job_id`.

The session/job pair is resolved under the same lock as cancellation admission.
A private transition reservation prevents replacement admission/draining while
session-scoped cancellation callbacks and queue/close effects settle outside that
lock. Callback uncertainty retains the fence; it is not permission to operate on
a newer job. Existing sensitive-media redaction and approval behavior apply.
`session_busy` may refuse a concurrent reset/close/uncertain transition. A retained
terminal result is history, not proof of worker/resource cleanup or readiness.
Legacy cancellation of a terminal job whose original worker is still retiring
returns `session_busy`, with either close flag, before cleanup. An already admitted
close intent is serviced after original retirement or a positively known no-start
failure; an ambiguous start retains the fence. Close-observer retirement transfers
pending responsibility under the state lock. Failed cleanup is retained and does
not trigger an automatic observer retry loop. A clear/resume validation or retained-
path lookup refusal before any mutation releases deferred ordinary queue progress.
An error after checkpoint/queue/history effects begin retains the mutation fence;
it neither reports successful restoration nor admits the next queued turn.

Clients must qualify the actual launched implementation before sending `job_id`:
older peers can ignore it and cancel their current job. Version strings and method
presence alone are not semantic qualification. This adds no public capability or
new protocol version, and does not authorize a session-wide retry after uncertainty.

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

`browser.status` and `browser.list` report `running` only while the owned process,
egress proxy and CDP control connection remain available. `disconnected` means
the process is still alive but control has failed closed (for example, cancellation
or timeout during an in-flight CDP request); `crashed` means the process or egress
proxy is unavailable. Close the owned session and start a new one to recover from
disconnection. Cancellation before dispatch keeps a healthy session usable.
Cancellation is not rollback: a multi-command input action may already have
changed the page before its next checkpoint. Re-observe a healthy session before
deciding another action; do not infer successful human takeover from cancellation.

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

## Composer catalog and explicit skills

`commands.list` is a read-only management method. With `mode: "chat"` (default)
it returns `commands: [{name, usage, description}]` from the CLI slash command
catalog; `chat` is the only supported mode. Hosts must expose
only entries they can execute or map to a native UI action; this endpoint does
not execute terminal commands.

`agents.list` is a read-only management method for subagent model settings. With
`workspace` or `path`, it includes the project's `.alysis_agents/`; without, only
the built-ins and the user's `agents/` folder. It returns `subagents_enabled`,
`user_agents_dir`, `agents: [{name, builtin, description, mode,
allow_workspace_writes, model_role, model, profile, enabled, available, unavailable,
effective_model, effective_profile, problem, user_file, settable,
not_settable_reason}]`, `roles: [{role, description, model, effective_model}]` and `profiles: [{name, active, usable,
reason}]`: whether a subagent can run on each saved provider profile (another
profile needs its own base URL and key or account). `effective_model` and
`effective_profile` are what a run would use now, resolved as a run resolves them;
`problem` says why it could not start. Changing a model is `alysis agents set NAME
--model M --profile P` (or `--clear`): it rewrites only the `model` and `profile`
lines of the subagent's file in the user's `agents/` folder, and a built-in without
a file gets a metadata-only override that keeps its prompt and permissions.

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


### Active-turn questions and host dispatch receipts

`session.userQuestions.get` reads question snapshots for the bound `session_id` and
`job_id` (the active or most recently finished job). `session.userQuestions.touch`
and `session.userQuestions.answer` additionally require `request_id` and a live,
uncancelled matching job. The answer method accepts `answers`; invalid, expired,
or already-finished requests are refused by the question broker. These methods
return the current question snapshots and do not create a workspace trust grant.

`host.action.dispatched` acknowledges the execution phase of an approved host
action. It accepts exactly `session_id`, `host_action_id`, `workspace_fence`, and
`capability_fingerprint`. The pending action must use approval protocol 1, belong
to the current live job, be in its execution phase and be within its deadline.
Stale, duplicate or incorrectly fenced receipts are rejected. This receipt records
dispatch only; the result still arrives through `host.action.respond`.


### Desktop customization workbench

`skill.import.preview` accepts an explicitly chosen folder, ZIP, or HTTPS Git repository,
with optional `ref` and `subdir`, `workspace_trusted: true`, and `yes: true`. It returns an
opaque `draft_id`, source commit, file inventories, full entrypoints, validation issues,
and package compatibility information. Source acquisition does not execute package code.
Drafts are process-local, bound to the workspace, expire after 30 minutes, and are bounded
in count and bytes. A management-process restart requires a new preview.

`skill.import.describe` rereads a draft for host-owned confirmation.
`skill.import.install` accepts that draft, `selected` candidate IDs, `project`, and
`accept_partial` for explicitly importing skills separately from a package or retaining
unsupported metadata. It installs the snapshot bytes, not a fresh source download.
Unsupported behavioral metadata starts disabled. Per-skill results report partial failure;
a consumed draft cannot be replayed. `skill.import.discard` abandons a preview.

`skill.import.package` installs a validated native Alysis package from the same reviewed
snapshot, for the personal scope, through the existing plugin installer. Imported packages
retain an unverified `snapshot` source kind and cannot claim bundled/custom namespaces.
Replacing an existing native package is currently refused to preserve component edits.
Portable Agent Plugins, Codex and Cursor manifests expose skill imports and MCP configuration;
foreign rules, agents, commands and hooks are shown as requiring separate setup.

`skill.document.read` returns raw SKILL.md, an entire-bundle revision, supporting file names,
provenance, and edit/update availability. Protected-value redaction makes a document read-only.
`skill.document.save` validates a complete entrypoint and accepts either a new skill, a managed
`name` with matching `revision`, or `copy_from` with matching source revision. Supporting
files survive edits and copies. `skill.update.preview` accepts `expected_source` from the document provenance revision,
so a host can bind explicit source review before fetching. It checks the recorded source only when
the installed bundle matches its baseline; installation rechecks the baseline after review.

`hooks.document.read` and `hooks.document.save` use `layer: user | project | local`, with
revision checks and the engine's hook schema. Saving project hooks does not grant trust.
`hooks.trust` and `tool.trust` accept `expected_hash` to bind host review to exact content.
`hooks.test` continues to check matches without executing commands.
