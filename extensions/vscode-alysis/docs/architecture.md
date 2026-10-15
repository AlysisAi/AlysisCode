# Architecture

The extension is a native IDE frontend, not a terminal wrapper. It talks to the Alysis Code runtime
through Alysis Code IDE Protocol v1 over stdio:

```bash
alysis ide-bridge health
alysis ide-bridge --stdio
```

The sidebar webview (`alysis.start`) is the product surface: one conversation, with plans,
diffs, artifacts, and diagnostics opening as focused details on the right when they are needed.

## Capability inventory

- Opens a native, capability-gated Managed Browser surface. Normal `public` sessions are shared
  with the top-level agent. Explicitly confirmed `public_loopback` sessions are direct-user-only and
  agent-isolated; they allow public plus loopback destinations while denying LAN and link-local.
  The surface supports click/type interaction, semantic/accessibility/DOM/text snapshots, verified
  chunked PNG screenshots, bounded console/network diagnostics, native Save As, and confirmed
  exact-session cleanup without exposing filesystem paths to the webview.
- Selects the CLI executable through the managed runtime, not PATH. Release builds bundle a
  signed, platform-specific runtime under `resources/managed-cli/` that the extension verifies
  (manifest schema, ECDSA P-256 release signature, SHA-256 and size, provenance pins, live
  `--version`/protocol probe), installs outside the workspace, activates atomically, and rolls back
  to the last-known-good install on failure. Production builds have no PATH fallback: a missing or
  unverifiable bundle leaves execution and credential forwarding disabled and says so in Runtime
  Details. A non-empty `alysis.cliPath` is a development override in every mode — it is used as-is,
  labelled non-production, and never release-verified. Only Development/Test extension hosts fall
  back to `alysis` (or the legacy `sylliptor` alias) on PATH. See
  [Managed runtime](#managed-runtime) below.
- Resolves every executable before process launch so workspace-local binaries cannot receive
  credentials in untrusted workspaces.
- Calls `alysis --version` and `alysis ide-bridge health`.
- Validates protocol version, transport, and baseline IDE bridge methods, then evaluates a formal
  feature compatibility contract before enabling chat, Forge Plan, Forge Execute Preview/Review,
  diffs, and artifacts independently. Old CLIs can open the sidebar when baseline methods exist,
  while unsupported workflows show upgrade/setup guidance instead of failing ambiguously.
- Starts `alysis ide-bridge --stdio` and exchanges newline-delimited JSON requests/events.
- Negotiates native host actions for VS Code Tasks and Debug. The backend can request exact
  workspace-scoped task/debug lifecycles through the extension host; starts require fresh approval
  and Workspace Trust, results are size-bounded and redacted, and only executions owned by the
  requesting session can be observed, stopped, or cleaned up. The protocol rejects stale,
  duplicate, cancelled, late, oversized, and cross-workspace replies and does not expose breakpoint
  mutation.
- Serializes bridge startup and the initialize handshake, fails process/stdio errors closed,
  rejects pending requests on bridge exit, and invalidates every controller when the shared bridge
  restarts. Chat restores bounded retained context into a new live session; Forge reopens the
  persisted plan and refreshes its artifacts/diffs. Late responses and poll results from an older
  session are ignored, so **New Session** and reconnects cannot be undone by a slow backend reply.
- Opens a secure sidebar conversation, creates a protocol session, sends user messages, and streams
  structured events. The webview uses a crypto-backed nonce, strict CSP, packaged local assets
  including the bundled mascot poses, and text-only rendering for all model/tool/workspace output.
- Uses a black-and-white theme with the TUI green (`#3fb950`) as its single identity accent. It
  reads consistently in dark and light VS Code themes; status is encoded by glyph/shape/weight, not
  hue. The Details surface shows a compact health row (mode · model · trust) with a disclosure for
  the Mode/Model/CLI Health/Bridge/Sandbox runtime rows, plus a clickable session-mode pill
  (readonly / review / auto; `fullaccess` reserved and gated unless the bridge advertises it and the
  workspace is trusted). The only composer stays pinned to the bottom of the sidebar. When a plan
  exists, the plan card folds in a "What Execute Plan will do" readiness panel (write scope vs
  estimated files, required vs runtime approvals, verification, trust/sandbox gates) and an
  "Execute Plan" action that stays review-only and writes only to your working tree (never commits).
  After execution a diff-review card opens each changed file in the native VS Code diff; Keep/Discard
  (commit/revert) is gated to VS Code Source Control until a bridge apply/discard capability exists.
  Idle states are welcoming (a "Welcome to Alysis Code" onboarding card and tone-glyph setup/recovery
  cards), and the surface is keyboard-accessible with ARIA roles (tablist mode switch, tabs,
  disclosures), visible focus rings in both mono themes, and a reduced-motion guard.
- Adds production slash commands routed by an extension-side command router to typed controllers and
  command-palette handlers instead of being sent to the model as chat text. See
  [slash-commands.md](slash-commands.md) for the full contract.
- Routes chat and Forge events by explicit `session_id`/`job_id` ownership. Chat does not attach to
  arbitrary bridge events when no chat session exists, and Forge owns the sessions/jobs returned by
  Forge Plan, List/Open, and Execute flows.
- Exposes typed backend run/chat parity methods for session status, usage, history, context,
  model info, safe subagent status/toggle, compaction, resume, image basket list/add/clear,
  mode/model/stream changes, active workdir changes, clear, and safe workspace-scoped image paths.
  These are protocol calls, not terminal-output scraping. Capability metadata marks
  `session.history` as backend-redacted and bounded, `session.context` token accounting as
  approximate `tokenizer_estimate` or explicitly unavailable, `session.modelInfo` as redacted and
  key-free, `session.subagents.*` as status/toggle only, `session.compact` as live compactor-backed
  when available, and `session.resume` as bounded retained-session log replay. `/paste-image` uses
  the same path-based `session.images.add` flow as `/image`; no image binary is serialized through
  JSONL.
- Carries explicit active, retained, closed, failed, and historical session row metadata. Show,
  Score, and Usage operate on the selected `session_id`; Compact and Clear are offered only for the
  active live session; Resume is offered only for retained/history rows and replays the selected
  retained session into an existing or newly created live IDE session without sending a model prompt
  by itself.
- Exposes grouped command-palette workflows through a central backend action registry:
  **Current Task Tools**, **Model and Provider Settings**, **Manage Tools and Skills**,
  **Manage MCP, Hooks, and Conventions**, **Manage Alysis Code Extensions**, **Troubleshooting**, and
  **Plan Files and References**. These routes cover safe config/profile/session/tool/skill/
  doctor/sandbox/update/report/MCP/hooks/conventions/extension-package surfaces plus
  `forge.show`/`forge.attach`/`forge.assets.*` without terminal scraping. Mutating calls carry
  `workspace_trusted`, advertise trust/mutation metadata, require Workspace Trust and confirmation
  where appropriate, and fail closed in the backend; config/profile payloads report secret status
  only and never return raw secret values. Retained session logs and rendered conventions are
  redacted and bounded before display. Provider header values route to SecretStorage/configuration
  actions, and extension package install requires explicit manifest/source/permission trust approval
  beyond `yes: true`. Skill installs validate workspace-scoped local sources or explicit confirmed
  HTTPS remotes; extension package installs accept only registry ids or pinned HTTPS git sources.
  MCP OAuth on local extension hosts uses flow-id-bound start/status/cancel/logout, state + S256
  PKCE, an owned loopback callback, bounded polling, encrypted token persistence, and shutdown
  fencing without returning authorization codes or raw tokens through JSONL. OAuth is unavailable in
  every remote VS Code extension host (including SSH, containers, WSL, and tunnels) until a
  remote-safe callback or device-code flow exists. `hooks.watch` and weak `hooks.watch.*`
  placeholders are not advertised until there is a subscription registry, bounded redacted event
  buffer, dropped-event accounting, watcher cancellation, and stop cleanup.
- Renders assistant deltas, status updates, collapsible tool calls, warnings, errors, approval
  prompts, replay/truncation notices, inline copy actions, native diff actions, and backend-scoped
  artifacts without unsafe HTML insertion.
- Renders correlated `subagent_state_changed` lifecycle as calm helper activity rather than raw
  protocol chatter. Model-managed delegates stay owned by the active chat job and stop with its
  cancellation token; independently resumable background work remains the Forge swarm surface,
  whose recovery cards expose revision, attempts, usage, explicit Resume, and Stop without reusing
  previous permission grants.
- Sends approval decisions through `approval.respond`; no approval is granted silently. Approval
  result events, timeout denial, and scoped allow-for-session decisions are rendered as terminal UI
  states. "Allow for session" appears only when the backend marks the approval as scoped and
  supported: shell/custom/MCP-like approvals require exact command scope and file approvals require
  an exact file set or equivalent backend-owned exact scope.
- Handles Forge approvals in the Forge controller through a modal prompt and persistent approval
  cards showing kind, reason, command/preview, files, session scope, and fail-closed status. Closing
  the chat panel cannot deny or consume Forge-owned approvals; unavailable Forge approval UI denies
  fail-closed.
- Uses `session.getEvents` for short-gap replay when reopening the panel. Event sequence tracking is
  per session, so a new session starting again at sequence `1` is not hidden by an older session's
  higher sequence number. The bridge bounds closed-session and terminal-job history, resets volatile
  replay/job state when a closed id is reused, and keeps incidental provider/library stdout away
  from the JSONL protocol transport.
- Calls `artifact.list` and `artifact.read` for backend-exposed artifacts only.
- Runs Forge Plan through a real command that prompts for an instruction, starts the bridge after
  trust/process validation, uses async `forge.plan.start` plus `job.status` and `forge.plan.result`
  when the bridge advertises them, falls back to planner-backed synchronous `forge.plan` for older
  compatible bridges, and renders progress and final plans. Planner/model/API-key failures return
  clear protocol errors instead of a fake shallow plan.
- Shows plan tasks with task id, title, objective, file scope, acceptance criteria, verification
  commands, dependencies, risk notes, incomplete-plan warnings, plan source, and status. Structured
  Forge events update the view without terminal-output parsing.
- **Browse Plans** and **Open a Plan** reopen persisted Forge plans through scoped
  `forge.list`/`forge.open` without reading SecretStorage. The backend rejects symlinked Forge
  registry components before exposing persisted plan artifacts.
- Adds typed backend bindings for safe Forge parity. `forge.attach` and Forge asset operations are
  reachable through **Plan Files and References** and `/assets` / `/asset ...` slash routes:
  `forge.assets.list`, `forge.assets.show`, `forge.assets.add`, `forge.assets.delete`,
  `forge.assets.edit`, `forge.assets.refresh`, `forge.assets.cancelPending`,
  `forge.assets.checkPlan`, and `forge.assets.pruneLegacy`. `forge.show` is exposed through the
  grouped command and `/forge show` / `/forge plan show` slash routes; `forge.review` is exposed through
  the same grouped command and `/forge review` / `/review` slash routes. Mutating asset/review
  operations require Workspace Trust, reject path traversal and symlink escapes, and never parse
  terminal output.
- Adds typed Forge plan-edit parity for `/assistant`, `/goal`, `/task`, `/forge plan state`,
  `/forge plan validate`, `/forge plan regenerate`, and `/forge plan regenerate`. Read routes use
  `forge.plan.getState`/`forge.plan.updateTask` without mutation. Edits call
  `forge.plan.setAssistant`, `forge.plan.setGoal`, `forge.plan.updateTask`, or
  `forge.plan.regenerate`, require Workspace Trust, persist to the plan store, and use optimistic
  plan revisions. Global `/forge plan <instruction>` still creates a new plan; Forge-context regeneration
  is explicit and typed.
- Runs Forge Execute Preview through `forge.executePreview` on a bridge start path that strips
  SecretStorage and inherited `ALYSIS_API_KEY`, rendering readiness, file scope, verification
  commands, sandbox/trust state, required approvals, blockers, and known risks. Preview does not
  mutate files, run shell commands, start workers, execute tools, auto-approve actions, or fake job
  state.
- Starts real `forge.execute` only after Preview reports `real_execution_supported` and
  `preview_ready`, the requested mode is `review`, and the user confirms explicitly. The first
  execution path is selected-task, review-mode-only, sequential, Workspace Trust gated,
  sandbox-aware, and approval-gated. `alysis.forgeExecuteMaxSteps` and
  `alysis.forgeExecuteNoLog` are forwarded as explicit protocol params; when the bridge must
  restart for credential-capable execution, the controller reopens the persisted plan and refreshes
  backend-scoped artifacts and diffs before starting the job.
- Dispatches the swarm console across parallel workers (disjoint write scopes by a scheduler
  invariant), surfacing a task grid, a non-modal task-attributed approvals inbox (workers run
  without `--yes` auto-approval), cooperative checkpoint cancellation, and a per-task review surface
  where you Keep (apply to the working tree, never committed) or Discard each task's diff. Merge is
  review-only, and broad auto/fullaccess swarm execution remains unavailable in the IDE.
- Uses `diff.list` and `diff.get` for backend-provided diff records. Old/new text snapshots open in
  VS Code's native diff editor through readonly virtual documents; unified diff previews are opened
  read-only when side-by-side content is not available. Truncated diffs are labeled clearly, and
  symlinked patch directories or diff files are not exposed as review artifacts.
- Routes **Cancel Current Run** to the active chat or Forge controller. Forge cancellation is
  attempted only when `features.forge.cancel.supported` is true; method presence alone is not
  enough. Protocol v1 uses cooperative checkpoint cancellation: `cancellation_requested` remains
  active until the backend reports terminal `cancelled`, `completed`, or `failed`. Deactivate denies
  pending chat and Forge approvals in their owning controllers, closes idle sessions, requests
  cooperative cancellation for advertised active jobs, and reports jobs as interrupted rather than
  cancelled when shutdown cannot reach backend terminal status.
- Runs `alysis sandbox doctor --smoke` with output redaction. `/doctor` and the command palette
  publish Doctor started/completed/failed events into the Timeline and Diagnostics with exit code
  plus redacted stdout/stderr previews.
- Adds recovery commands for missing or incompatible CLIs: **Copy Development CLI Install Command**,
  **Copy Development CLI Upgrade Command**, and **Open Setup Guide**. These copy or display the
  canonical `pipx install alysis-code` and `pipx upgrade alysis-code` commands; the
  extension never installs or upgrades the CLI automatically.
- Uses VS Code SecretStorage for provider API keys and passes them to the local bridge through the
  child process environment, not command-line arguments or plaintext settings. SecretStorage is not
  read for untrusted non-readonly starts or for workflows blocked by missing/incompatible required
  bridge methods.
- Applies Workspace Trust gates for Forge, CLI execution, workspace-scoped execution settings, and
  future mutating actions. See [security-model.md](security-model.md).
- Shows an Alysis Code status bar item and registers the Alysis Code Activity Bar container.
- Adds an optional native VS Code `@alysis` Chat Participant using the stable VS Code 1.90
  `vscode.chat.createChatParticipant` API. The participant is a thin router for `/help`,
  `/forge plan <task>`, `/execute`, and `/doctor`; it opens the sidebar and delegates to the same
  extension controllers instead of replacing it or depending on GitHub Copilot.

## Runtime status model

Runtime status chips intentionally separate executable trust from runtime health:

- Header: Mode, Model, CLI Health, Bridge, and Sandbox.
- Diagnostics detail: CLI Origin (`trusted`, `blocked`, or `unknown`), CLI Health (`ok`,
  `missing`, `incompatible`, `broken`, or `unknown`), Bridge (`stopped`, `starting`, `ready`,
  `active`, `approval needed`, or `error`), Protocol (`ok`, `incompatible`, `missing methods`,
  `error`, or `unknown`), Sandbox (`unknown`, `running`, `ok`, or `failed`), provider, and
  Workspace Trust.

The status bar, Bridge chip, CLI chips, Sandbox chip, Diagnostics panel, and Timeline are
synchronized through shared runtime state. A trusted executable origin is not displayed as overall
CLI health when `ide-bridge health` or `sandbox doctor` fails.

## Managed runtime

`src/runtime/` owns which `alysis` executable the extension runs and whether it is trusted.

- `BundledManagedCli` locates `resources/managed-cli/manifest.json` inside the extension, refusing
  symlink escapes and oversized manifests, and names the one executable the manifest declares for
  the current platform target.
- `ManagedCliRuntime` parses the closed schema-v3 manifest (unknown keys are fatal), checks the
  release tag against the CLI version, checks the extension/protocol/CLI compatibility ranges,
  verifies the artifact's release signature through `ManagedCliReleaseSecurity`, copies the
  executable to an install root outside any workspace, re-verifies size and SHA-256, runs a live
  `--version` and `ide-bridge health` probe, and only then activates it. Activation is atomic and
  the previous verified install stays available for rollback.
- `ManagedCliReleaseSecurity` verifies ECDSA P-256 / SHA-256 signatures over the canonical signed
  record under the frozen domain `sylliptor-managed-cli-release-v3`. The trust set is compiled into
  the extension (`resources/managed-cli-release-public.pem` is a mirror the release workflow
  diffs against, not a runtime input). There is no setting, environment variable, or file that
  admits another key; `requireSignature` is on in every extension mode.
- `ManagedRuntimeCoordinator` turns the outcome into the runtime state the rest of the extension
  consumes: `origin` (`managed`, `development-override`, `development-path`, or `unavailable`),
  `production`, `executionAllowed`, and `apiKeyForwardingAllowed`. A non-empty `alysis.cliPath`
  short-circuits validation and yields `development-override`; a missing or invalid bundle yields
  `development-path` (PATH fallback) only outside `ExtensionMode.Production` and `unavailable`
  otherwise.

`scripts/package-release.js` refuses to package a release VSIX unless a manifest and the
target's executable are staged and consistent; `npm run package:dev-vsix` bypasses that guard and
produces a runtime-less build for local development only. The runtime itself is produced and signed
by `.github/workflows/managed-cli-vsix-release.yml`; see
[`docs/managed-cli-signing-operations.md`](../../../docs/managed-cli-signing-operations.md).

## CLI compatibility and recovery

The extension requires a local Alysis Code CLI. This build is tested against `alysis-code` `0.15.0`
(`MIN_RECOMMENDED_ALYSIS_CLI_VERSION`, kept equal to the monorepo `pyproject.toml` version by a unit
test), with IDE Protocol v1 over stdio. The version is a soft gate; hard availability is decided per
feature by capability negotiation, so an older CLI that still advertises the required methods works
and gets an upgrade hint rather than a refusal.

Baseline compatibility requires only:

- `initialize`
- `health`
- `getCapabilities`

Feature availability is evaluated separately from baseline health:

- Chat/run requires `session.create`, `chat.send`, `run.start`, `session.cancel`, `approval.respond`,
  `job.status`, `session.list`, `session.getEvents`, `artifact.list`, and `artifact.read`.
- Run/chat backend parity requires `session.status`, `session.usage`, `session.history`,
  `session.context`, `session.modelInfo`, `session.subagents.status`,
  `session.subagents.setEnabled`, `session.compact`, `session.resume`, `session.images.list`,
  `session.images.add`, `session.images.clear`, `session.setMode`, `session.setModel`,
  `session.setStream`, `session.setActiveWorkdir`, `session.clear`, `session.trace.status`,
  `session.trace.setLevel`, `session.trace.listEvents`, `session.trace.readArtifact`,
  `session.trace.clear`, `session.terminals.list`, `session.terminals.show`,
  `session.terminals.kill`, and `session.terminals.clear`.
- Management backend parity requires `config.get`, `config.set`, `config.schema`,
  `config.validate`, `profile.list`, `profile.show`, `profile.add`, `profile.remove`,
  `profile.use`, `profile.rename`, `profile.presets`, `profile.preset`, `profile.convert`,
  `session.show`, `session.score`, `tools.catalog`, `tool.list`, `tool.info`, `tool.trust`,
  `tool.untrust`, `skill.list`, `skill.info`, `skill.init`, `skill.validate`, `skill.install`,
  `skill.enable`, `skill.disable`, `skill.remove`, `doctor.summary`, `doctor.providers`,
  `doctor.bundle`, `sandbox.doctor`, `sandbox.setup`, `sandbox.pull`, `update.check`,
  `report.create`, `mcp.status`, `mcp.prompts.list`, `mcp.prompts.get`, `mcp.auth.status`,
  `mcp.auth.login.start`, `mcp.auth.login.status`, `mcp.auth.login.cancel`, `mcp.auth.logout`,
  `hooks.list`, `hooks.doctor`, `hooks.trace`, `hooks.test`, `hooks.trust`, `hooks.untrust`,
  `hooks.init`, `hooks.effective`, `hooks.enable`, `hooks.disable`, `conventions.list`,
  `conventions.render`, `ext.search`, `ext.list`, `ext.info`, `ext.install`, `ext.uninstall`,
  `ext.enable`, and `ext.disable`.
  Package install is a two-step trust-review flow: first request structured manifest/source/
  permission metadata, then pass the reviewed `trust_approval` object with explicit confirmation.
  `update.check` defaults to cached/local status and is not used by passive activation or status
  refresh; live network checks require an explicit user-triggered backend action. `session.show`,
  `session.history`, `session.resume`, and `conventions.render` include redaction and truncation
  metadata.
- Trace and terminal parity is intentionally narrow: `/trace` exposes backend-redacted bounded
  status/events/artifacts with explicit confirmation for `full`, and `/terminals` can list/show
  existing managed background terminals plus Workspace-Trust/confirmation-gated kill/clear when the
  active session has a terminal manager. Arbitrary shell start, interactive PTY streaming, and raw
  unredacted trace output are not IDE v1 features.
- Forge Plan requires `forge.plan` or the async pair `forge.plan.start` and `forge.plan.result`.
- Persisted Forge plans require `forge.list`, `forge.open`, `forge.status`, and can use
  `forge.show`.
- Forge plan-edit parity requires `forge.plan.getState`, `forge.plan.setAssistant`,
  `forge.plan.setGoal`, `forge.plan.updateTask`, `forge.plan.validate`, and
  `forge.plan.regenerate`.
- Forge asset command/slash workflows use `forge.attach`, `forge.assets.list`, `forge.assets.show`,
  `forge.assets.add`, `forge.assets.delete`, `forge.assets.edit`, `forge.assets.refresh`,
  `forge.assets.cancelPending`, `forge.assets.checkPlan`, and `forge.assets.pruneLegacy`.
  `forge.review` is routed through the grouped Forge assets command and `/forge review` / `/review`
  slash actions. Compatibility uses `features.forge.assets.supported` when available and otherwise
  falls back only to the real `forge.assets.*` method set, not the old `assets.list` name.
- Forge Execute Preview requires `forge.executePreview`.
- Forge Execute Review requires `forge.execute`, host approvals, job/event replay, artifacts, and
  diff methods.

If `alysis ide-bridge health` is missing, the CLI is treated as incompatible and the extension
does not start the bridge or forward SecretStorage API keys. If `alysis sandbox doctor --smoke`
is missing or fails, the Sandbox chip and Diagnostics panel show the failure while CLI Health stays
separate from executable-origin trust. Missing optional feature methods disable only the affected
feature; the recovery surface collapses the whole gap into one root-cause card in Diagnostics (with
a single low-noise Timeline pointer) that names how many capabilities are affected and offers
install/upgrade, Set CLI Path, Setup Guide, and Doctor actions instead of one card per feature.

Canonical development setup commands (for the `alysis.cliPath` override; release builds use the
bundled managed runtime and never invoke these):

```bash
pipx install alysis-code
pipx upgrade alysis-code
```

## Parity contracts

Backend parity with the CLI is tracked in the repo-level
[`docs/generated/ide_cli_parity_matrix.json`](../../../docs/generated/ide_cli_parity_matrix.json)
contract. The generated final burn-down report lives in
[`docs/generated/ide_cli_parity_burndown.md`](../../../docs/generated/ide_cli_parity_burndown.md),
and the Forge-specific release-review projection lives in
[`docs/generated/ide_forge_parity_burndown.md`](../../../docs/generated/ide_forge_parity_burndown.md).
Run `python scripts/qa/check_ide_cli_parity.py` from the repository root after changing CLI
commands, IDE methods, package command registrations, or slash command registrations. The contract
documents which CLI features are implemented in the extension, planned for typed protocol support,
intentionally CLI-only, blocked until the security/runtime model is stronger, or not applicable to
an IDE. It also records route metadata for every contributed VS Code command, including
command-palette entries, hidden context routes, backend action group, backend action, and
documented handler routes. Planned entries must include owner, target milestone, and next step
metadata; blocked entries must include an explicit security or lifecycle reason. New package
commands must add both a parity classification and route metadata before the validator passes.

The IDE method parameter contract is tracked in
[`docs/generated/ide_protocol_methods.json`](../../../docs/generated/ide_protocol_methods.json).
Backend action tests load that fixture and validate every action's required methods, trust metadata,
slash aliases, and collected params. If a Python protocol method changes shape, the TypeScript
controller tests should fail before a route reaches users with stale params.

## Activation

The extension activates lazily when an Alysis Code command, view, or native chat participant is used.
It does not declare `onStartupFinished`, so installing it does not add work to an unrelated VS Code
startup. `alysis.autoStartBridge` remains opt-in and is evaluated only after that lazy
activation. Virtual workspaces are explicitly unsupported because the IDE bridge requires local
filesystem paths and a local executable CLI. Remote VS Code extension hosts remain compatible when
both the workspace and Alysis Code CLI are available in that remote host environment.
