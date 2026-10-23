# Changelog

## 0.3.0 — pre-release preparation

- Bundle a signed, platform-specific CLI runtime; validate its signature and integrity before
  starting the bridge. The final source tag selects the bundled CLI version.
- Allow local providers without an API key, including switching back to a configured local server.
- Forward custom-endpoint keys through the provider-specific credential channel even when the
  provider has no named environment variable.
- Wait for initial runtime validation before Runtime Details, Check Setup, and Bridge Health;
  show preparation progress while validation is pending.
- Package only the intended runtime and presentation assets, excluding local QA output and other
  unexpected files from the VSIX.
- Update the development dependency source-map-js to 1.2.2 to address its high-severity source-map
  denial-of-service advisory.
- Verify packaged installations on the minimum supported VS Code version and current Stable.

Marketplace publication is pending the signed candidate, provider/remote acceptance, and release
signoff. Development component VSIX files do not contain the production runtime.

## Marketplace preparation — 22 September 2026

- Remove saved API keys from the correct provider-specific SecretStorage entry. When multiple
  keys exist, the command lets the user choose one and preserves the other connections.
- Refresh an idle connection after removing credentials, and explain when active work prevents
  the running connection from dropping its previous credential yet. Offline removal remains
  available without starting the CLI.
- Allow the source-pinned release maintainer to approve signing, evidence, and publication alone,
  while retaining actual approval receipts, runtime signatures, and exact-candidate validation.
- Validate the bundled `dist/extension.js` in component release checks instead of requiring the
  development-only `out/src/extension.js` that is intentionally excluded from the package.
- Update the managed engine's locked AnyIO dependency to 4.14.2, addressing its reported
  subprocess, process-pool, and TLS hostname verification vulnerabilities.

## Bundled extension host and release-gate walk — 21 September 2026

- Bundle the extension host with esbuild into a single `dist/extension.js` (esbuild 0.25.12,
  devDependency only; runtime dependency count stays at zero). `npm run compile` now produces
  both the `tsc` tree in `out/` (tests, scripts) and the bundle; packaging replaces it with the
  minified build via `bundle:production`, also wired as `vscode:prepublish`. The dev VSIX went
  from 106 files / 635 KB carrying 82 JS modules to 25 files / 459 KB carrying one 670 KB entry.
  Measured with the committed perf-activation protocol (VS Code 1.90.0, 5 samples): cold
  activation median 1,430 → 1,280 ms, p95 1,504 → 1,468 ms; warm p95 1,556 → 1,522 ms; RSS
  growth p95 12.8 → 11.3 MB. Both builds are well inside the 5,000 / 3,500 ms budgets.
- `StartViewProvider` locates `media/startView.html` through `context.extensionUri` instead of
  `__dirname`, and `test/bundleWiring.test.ts` fails on any `__dirname`/`__filename` in `src/`.
- VSIX dogfood stages the compiled helper modules beside the staged integration suite, since the
  bundle ships no `out/src` tree for the suite's helper imports to resolve against.

Release-gate status for this branch, run locally on Windows against the repo's `.venv` and the
cached VS Code 1.90.0 unless noted:

- Extension: `npm run lint` clean; unit suite 1,175 pass / 0 fail including the real Python
  bridge smoke; `npm audit --audit-level=high` 0 vulnerabilities; 26/26 extension-host
  integration tests against the bundled VSIX; perf-activation within budget; component dogfood
  `status: passed`, `component_valid: true`, `release_valid: false`, `extension_under_test:
  packaged_vsix`, preflight passed. Manifest is lazy (`onStartupFinished` absent), every
  contributed command is registered, and no activation path reaches the network (the only
  `https.get` belongs to `ManagedCliRuntime.install()`, which nothing outside tests calls).
- CLI: console-encoding 9/9; Forge exec/swarm/review 181/181; IDE release-candidate bridge tests
  308 pass / 9 skipped; `check_ide_cli_parity.py` reports the matrix current; the new TLS,
  watchdog and MCP-env tests pass. Full suite (Windows, Python 3.11): 10,868 passed, 101
  skipped, 381 subtests passed in 47 min. Its single failure was `test_host_tls.py` writing a
  PEM fixture as text (CRLF on Windows); the run had started before the fix in this branch
  landed, and the file passes 6/6 on the same interpreter afterwards.
- Not runnable here, by design: signed managed-runtime packaging (`package-release.js` refuses
  without `resources/managed-cli/`), the six-target `managed-cli-vsix-release` workflow, the
  untrusted-workspace, WSL, Remote-SSH and installed-live-provider acceptance runs, and every
  manual real-provider smoke. Those require a tagged commit and the protected CI environments.

Observation for later: ~1.3 s to activate and run one command is high for an extension whose
manifest is lazy; bundling was not the bottleneck. Worth profiling the activation path itself.

## Competitive gap analysis, tier 1 — 18 September 2026

Security fixes found during source inspection:

- Fence a host action whose adapter finishes after its deadline so a re-delivered
  `host_action_id` with a refreshed expiry can never run `tasks.run` or `debug.start` twice.
- Redact `Authorization` and `Proxy-Authorization` header values whatever their scheme before
  IDE context reaches the model; previously only `Bearer`/`Basic` were caught.
- Make `alysis.baseUrl` machine-scoped so a repository's `.vscode/settings.json` cannot redirect
  the stored API key, and accept plain `http://` only for loopback hosts (Ollama, LM Studio).

Enterprise plumbing for the spawned CLI:

- Forward VS Code's `http.proxy`, `http.noProxy` and `http.proxySupport` into the CLI environment
  (`HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`); `proxySupport: "off"` also clears ambient proxy
  variables. Invalid values are dropped with a one-line Output notice instead of failing launch.
- New `alysis.extraCaCerts` setting: a PEM bundle trusted in addition to the CLI's bundled roots
  (merged CLI-side, since `SSL_CERT_FILE` would replace them) and passed to Node MCP servers as
  `NODE_EXTRA_CA_CERTS`. Requires the updated CLI.
- Pin the extension host PID (`ALYSIS_PARENT_PID`) so the CLI exits if the host is hard-killed
  without reaching `deactivate()`. Requires the updated CLI.

Decisions recorded for the next reader:

- `http.proxyStrictSSL: false` is deliberately not honoured. Python has no environment switch
  for skipping certificate verification and the correct enterprise fix is the CA bundle above;
  the extension logs one notice saying so.
- The native side-by-side diff path (`alysis-diff` content provider, `vscode.diff`) is not
  reachable in production: the CLI's `diff.get` always returns `old_text`/`new_text` as `null`,
  so every Forge diff opens as a read-only unified-diff document. The analysis's cache-busting,
  `untitled:` and editable-pane suggestions for that path were skipped until the CLI sends
  snapshots; the unit tests that exercise it feed synthetic ones.

## 0.3.0 beta preparation — 16 September 2026

- Apply provider switches to the open conversation, including its endpoint, credentials and
  protocol. Preserve history, reject switches during a turn, and restore live state on failure.
- Apply model selections from the chat composer and Models surface to the current conversation
  as well as the saved default. Warn when the current conversation cannot accept the change.
- Prevent incorrect Git review results in repositories with content filters. Affected worktree
  reads require the configured command runner; unaffected paths and staged diffs remain available.
- Apply sandbox preferences to new chat, task and worktree sessions. Setup diagnostics use the
  same preference, preserve environment policy, and chat displays the reported effective mode.
- Pause actions that may change files when restore snapshots are unavailable. Reuse pending
  snapshot initialization on retry, respond to cancellation, and show a recovery card.
- Add an explicit beta channel across signed packaging, evidence, approvals and Marketplace
  publication. Keep stable channel protection and verify exact published bytes for either channel.
- Exclude generated local UI test output from lint discovery.
- Keep managed task requirements separate from generated setup guidance when checking completion.
  Recognize quoted output paths and permit the host's own lock heartbeat updates during long tasks.
- Count provider connection-drop retries in stream-restart diagnostics.

This is preparation for a signed beta, not a publication announcement. Native signing and
the six-target installed-provider and remote acceptance gates are still required.

## Local review and command setup — 15 September 2026

- Show one actionable setup card when shell commands or tests cannot reach Docker.
  Setup diagnostics keep a working CLI available for file reading and Git review.
- The accompanying CLI can page through full Git diffs, scope a literal path, and inspect
  staged changes. Continuation checks reject a diff that changed between pages.
- Basic file and Git reads skip checkpoint initialization. Edits, shell commands, and
  unknown tools still prepare checkpoints before execution.

The Git and checkpoint changes require the updated CLI as well as the extension.

## Stable release preparation — 12 September 2026

- Wire stable candidate packaging, protected Marketplace promotion, stable approval receipts,
  channel collision rejection and exact published-byte verification together; remove preview metadata.
- Verify each bundled runtime's actual signed artifact, compatibility, pinned release identity and
  exact bytes during packaging. Fix Windows packager startup and the runtime smoke's stdio command.
- Keep runtime-less CI packages explicitly separate from production candidates.
- Replace listing placeholders with five labeled demo screenshots, pin release image URLs to the
  source commit, clarify privacy/sandbox requirements and repair support links.
- Update vulnerable extension tooling and locked runtime dependencies.

Signing, clean production installs and real multi-platform/provider release evidence remain
required before publication; this entry is not a release announcement.

## Conversation recovery — 12 September 2026

- Retain completed assistant answers in provider history so follow-up requests remain separate turns.
- Preserve a bounded list of recent tasks, their titles and recoverable conversations across reloads.
- Restore user/assistant roles correctly and keep internal recovery context out of the visible chat.
- Explain interrupted tasks and require fresh review for approvals lost during a reload.
- Restore conversation history on fresh machines without Git when no file checkpoints exist;
  preserve the recovery requirement for existing checkpoints and report missing Git clearly.
- Distinguish Git ownership errors from missing commits when worktree actions are unavailable.

## Permission enforcement — 12 September 2026

- Preserve confirmed task permissions across reloads, bridge reconnects and worktree moves. Older saved tasks without a recorded mode resume read-only.
- Show permission changes only after host confirmation and block task starts while a change is pending.
- Apply permission changes to a retained conversation after reconnecting it, instead of changing only the default for new tasks.
- Keep sensitive and external paths behind one-time approval, respect explicit Ask rules over cached grants, and deny requests when policy evaluation fails.
- Prevent an approval waiter from restoring a session grant after it was revoked.

## Session worktree bar — 12 September 2026

- Add New Session, New Worktree, Move to Worktree, branch identity and live Git change counts.
- Create real sibling Git worktrees with a choice of committed files or copied local changes, preserving the source checkout and index.
- Continue conversations in a separate worktree window through a persisted handoff, with fresh workspace permissions and checkpoints.
- Keep resumed conversation history visible and durable through subsequent restarts.

## Provider connection recovery — 12 September 2026

- Serialize passive startup, provider upgrades and explicit restarts together so a background
  model/provider refresh cannot replace chat's authenticated bridge during a provider switch.
- Use the key saved in VS Code for its named provider even when the CLI has an older saved key.
  Keep credentials isolated when multiple profiles declare the same environment variable.
- Recognize exhausted credits and account limits, including quota failures reported with HTTP 429,
  and explain the billing/provider recovery instead of offering a connection check.
- Merge matching stream and job-status failures into one card without hiding failures in later tasks.

## Kilo UI adaptation — 8 September 2026

- Adapt Kilo Code's compact prompt layout and persistent sidebar navigation to Alysis personas,
  models, permissions, Forge, history and settings, with its MIT attribution included.
- Keep model and persona selectors beside Send/Stop; place context tools below the one-line input.
- Use quiet assistant labels, expandable tool rows, compact onboarding and bounded review evidence
  with native file links. Preserve streaming, draft recovery and keyboard access.
- Author the UI in the shared package and generate the editor's asset copies.

## Composer layout — 8 September 2026

- Start the input at one line, grow it with the draft, and reflow it when resizing the sidebar.
- Align Send/Stop with context tools and tighten the model, persona, and permission rows.
- Use consistent conversation/composer margins and anchored, scrollable picker layouts.

## Agent alignment — 7 September 2026

- Replace retired chat Plan/Act controls with the agent's Permissions and Persona workflows.
- Align slash and native chat commands, capability gates, settings availability, and Forge guidance.
- Use confirmed agent permissions and pending images across sends, task switches, and webview restores.

All notable changes to the Alysis Code VS Code extension are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Added a task header with live status and checkpoint access, a persistent review shortcut,
  searchable task history with a current-task filter, response copying, and expandable evidence
  for resolved approvals. Approval diffs now show added/removed line counts and keyboard scrolling.
- Reworked the sidebar around native VS Code theme colors, clearer conversation typography,
  a two-row composer, explicit approval choices, and model/persona controls that remain available
  in narrow sidebars. Added reduced-motion and high-contrast refinements.
- Added the catalog's suggested models to every existing provider connection. `profile.list`
  carries no model catalog, so an already-configured provider offered exactly one model plus
  "type a name"; connections now borrow the matching preset's models and one-line descriptions —
  matched by preset name first, then by wire protocol and endpoint host, so the CLI's own
  `default` profile lands on the OpenAI preset and a Responses-API profile on the Responses preset,
  never cross-matched to a same-host preset with another protocol. The configured model stays
  first, a custom endpoint keeps only what is configured, and the command-palette picker, the
  sidebar model select, and the `/model` route all read the same list.

### Fixed

- Refined thinking and tool progress into one compact, theme-aware activity row with a single
  subtle spinner and expandable work details. Live updates preserve the indicator and keyboard
  focus; empty answer events, pending approvals, and stopped tasks no longer leave misleading
  thinking states or duplicate progress rows.
- Kept model picker options and provider form focus stable during unrelated chat updates.
  IME confirmation no longer selects a slash suggestion, pending sends lock mode changes,
  and cancelled work stops animating even when the final tool event is missing.
- Fixed every first task failing against the real CLI with `unsupported_turn_option`. The
  extension creates the session first (so host capabilities are negotiated against a known
  session id) and then repeated `workspace`, `mode`, `model`, `base_url`, `workspace_trusted`, and
  `host_capabilities` on the `run.start` that joins it; the bridge accepts turn params only on an
  existing session. `run.start` now carries the instruction, idempotency key, and context blocks
  alone. The integration mock CLI accepted the old shape without complaint — it now enforces the
  same rule as the real bridge so this cannot regress unseen.
- Fixed approved tool executions vanishing from the transcript. With semantic activity advertised,
  every legacy `tool_call_*` event was dropped on the assumption that an `activity_update` would
  follow; CLI 0.14 emits activities for patches and prompts but delivered `verify_run` and
  `shell_run` as legacy events only, so the user saw an approval card, then silence, then a reply
  saying the tests never ran. Legacy events now always open a row, a semantic activity with the
  same id adopts that row, and late legacy events for an adopted row are ignored. (The CLI side is
  fixed too: the IDE event surface now emits the tool activity on its canonical path.)
- Fixed the composer wedging behind "Alysis Code is already starting a task" after a refused start.
  Several guarded flows awaited `showWarningMessage`/`showInformationMessage` toasts that carry no
  buttons; VS Code resolves those only when the toast is dismissed, and one that slides into the
  notification bell never is. A single "Save or revert unsaved workspace files" warning therefore
  held the start lock until the bell was cleared, and every later Send was refused. Button-less
  toasts are now fire-and-forget across the chat, Forge, provider, backend-action, and start-view
  controllers (a modal or a toast with choices is still awaited, because its answer is the point).
- Fixed the sidebar header reading "Provider" before the first task even though a provider was
  active; the name now comes from the models surface until a session exists.
- Fixed provider changes silently ignoring every Connect or Replace-key click after the first one.
  The "connected / key rejected" toast offers a Replace key button and can sit unanswered in the
  notification bell indefinitely; awaiting it held the single-writer mutation lock, so later
  provider changes were dropped without a word. Outcome reporting now runs outside the lock, a late
  Replace key acquires the lock like any other change, and every click blocked by an in-flight
  change says so instead of only the first — a silently ignored click is indistinguishable from a
  dead button.
- Corrected the extension's recommended CLI version from `0.1.4` to the monorepo's `0.14.1`.
  `MIN_RECOMMENDED_ALYSIS_CLI_VERSION` now tracks the CLI this build is tested against, a unit test
  fails if it drifts from `pyproject.toml` or `docs/architecture.md`, and the docs call it what it
  is: a soft "tested against" gate, with hard availability decided per feature by capability
  negotiation.

### Changed

- Added `scripts/qa/headless-host.js`, a headless extension host for QA: it activates the real
  compiled extension under a stubbed `vscode` module, resolves the sidebar with a fake webview,
  sends the same messages the UI sends (task submit, approvals, Forge preview/execute/diff), and
  logs every published state, every host prompt, every bridge event, and every `vscode` API the
  stub does not implement. Bridge, CLI, provider, and workspace are real; only the UI is
  simulated. It found both contract bugs above in its first run.
- Documented the managed runtime honestly. `docs/architecture.md` previously described PATH and
  `alysis.cliPath` discovery and never mentioned the signed bundled runtime; it now describes
  `BundledManagedCli` → `ManagedCliRuntime` → `ManagedRuntimeCoordinator`, the frozen signing
  domain, the absence of any dev-key escape hatch, and when PATH fallback applies (Development
  hosts only). `docs/development.md` now names `package:dev-vsix` as the one guard-bypassing script
  and explains running from a source checkout. `SUPPORT.md` links that pointed at files excluded
  from the VSIX now point at GitHub.

## [0.2.0] - 2026-08-16

### Added

- Added session personas. A picker in the composer (and `/persona`, with name completion) switches
  between the CLI's builtin code, architect, ask, and debug personas plus workspace-defined ones;
  every row shows the execution mode the CLI clamp would land on, the active persona is shown as a
  chip when it is not `code`, and the control appears only when the connected CLI advertises
  `session.personas.list` and `session.persona.set`. A persona can narrow what the agent may do,
  never widen it.
- Added a readiness gate above the composer. Alysis Code now blocks a task when the engine, workspace
  trust, or provider is not actually ready — previously the composer unlocked on provider selection
  alone, so a missing CLI looked healthy right up until the first task failed. Each blocker is one
  plain-language row per root cause, deduplicated and capped, with the fix as a button.
- Added a typed error taxonomy. Invalid keys, rate limits, quota exhaustion, unreachable networks,
  context overflow, missing runtimes, and version mismatches each render a titled card with a
  one-click remedy instead of a raw protocol string and error code. Rate limits show a countdown;
  cancellation is an outcome, not an error. Raw diagnostics still go to the output channel.
- Added real message rendering: per-code-block Copy, Insert at cursor, and Apply (via diff, never a
  blind write), dependency-free syntax highlighting, links, tables, ordered and nested lists,
  blockquotes, and clickable file mentions.
- Added image paste and drag-and-drop into the composer, composer history recall, a jump-to-latest
  affordance, and keyboard shortcuts for opening Alysis Code, adding a selection, and cancelling a run.
- Added a fail-closed release packaging guard. `npm run package:vsix` now refuses to build unless the
  signed managed CLI is staged for a declared target; the unguarded path is `package:dev-vsix`.

### Added (previously unreleased)

- Added capability-negotiated native VS Code Tasks and Debug host actions. The backend can list,
  start, observe, cancel, and clean up exact extension-owned executions without shell scraping or
  breakpoint mutation. Every request is bounded and correlated to the negotiated session,
  immutable workspace identity, and replay fence; mutating starts require a fresh approval and
  Workspace Trust, while stale, duplicate, late, oversized, or cross-workspace responses fail
  closed. Windows managed-runtime install-lock contention now handles transient `EPERM`/`EACCES`/
  `EBUSY` races without weakening symlink/directory safety.
- Added correlated, semantic helper lifecycle in the transcript through
  `subagent_state_changed`. Model-managed delegates now show one calm start-to-finish activity row,
  nested tool events retain worker attribution, and concurrent helpers with the same role cannot
  overwrite each other. The protocol remains explicit that these delegates are owned and cancelled
  by their parent chat job; independently resumable child work stays in Forge swarm, with durable
  revisions, refreshed permissions, cancellation, recovery, review, and usage.

### Fixed

- Fixed a crash that could terminate the VS Code extension host — and every other extension in the
  window — when the bridge pipe errored. Only `stdin` had an error handler; `stdout`, `stderr`, and
  the readline interface did not, so an EPIPE or ECONNRESET became an unhandled `error` event.
- Fixed Forge wedging permanently after a single transient status-poll failure. The active job is now
  always released, transient failures are tolerated before giving up, and Cancel Current Run is
  enabled during Forge runs (the context key previously reflected chat activity only).
- Fixed swarm Apply writing agent-authored changes from one unconfirmed click. Apply now confirms,
  and the sidebar shows the diff and the untracked-files warning that the state already carried.
- Fixed restored diff tabs rendering blank after a window reload. Diff URIs are deterministic instead
  of timestamped, and a cache miss re-fetches rather than silently showing "no changes".
- Fixed screen readers re-announcing the whole transcript on every streaming token, and keyboard
  focus and text selection being destroyed on every publish. The transcript now reconciles in place.
- Fixed the response feeling laggy: cockpit state was fully re-serialized and re-rendered twice per
  token. Publishes are coalesced to frame cadence and flushed immediately on terminal events.
- Fixed running turns force-scrolling to the bottom even when the reader had deliberately scrolled up.
- Fixed activation blocking on full managed-runtime validation, a recursive preview-directory delete,
  and an initial bridge check. Validation now runs in the background and no longer re-fires on every
  unrelated setting change or reconnect probe.
- Fixed Windows `.cmd`/`.bat` CLI shims failing with a bare `spawn EINVAL`, `taskkill.exe` and `git`
  being resolved by bare name (which searches the current directory first), and empty or quoted PATH
  entries resolving to the current directory.
- Fixed every long-running bridge request sharing one two-minute timeout, and error responses without
  a request id stranding their caller for the full timeout instead of failing fast.
- Fixed a newer CLI being told to upgrade itself when the extension was the outdated side.
- Fixed stray CLI stdout (for example a pip warning) surfacing as a parser error card in chat.

### Security

- `sylliptor.cliPath` is now `machine`-scoped. A workspace could previously nominate an executable
  that the extension spawned on activation once the folder was trusted, with credential forwarding
  enabled — bypassing the signed managed-runtime chain entirely.
- The managed browser now refuses loopback, RFC1918, link-local, and cloud metadata addresses on
  public sessions, and the unsafe-host check covers IPv6 unique-local and IPv4-mapped forms.
- Two mutating backend actions that reached their mutation through an unconfirmed alias now route
  through the same confirmation, and workspace-relative paths are validated before being forwarded.
- The webview cache-busting parameter no longer reuses the CSP script nonce, and the internal release
  checklist (which names protected environments and secrets) no longer ships inside the VSIX.

### Removed

- Removed the unreachable Details chat panel and its assets. Four tree views were permanently gated
  `when: "false"` and could never render; they and their menus are gone. The providers behind them
  survive as data sources for the sidebar.
- Removed 45 redundant `onCommand:` activation events; VS Code derives them from `contributes`.

### Changed

- Xiaomi MiMo is now a regular provider preset flowing through the generic catalog. The dedicated
  `sylliptor.xiaomiApiPlan` setting, the "Xiaomi MiMo (pay as you go)" branch in Configure
  Provider, and the base-URL rewrite that forced `https://api.xiaomimimo.com/v1` whenever the
  provider was "Xiaomi MiMo" are gone — a configured `sylliptor.baseUrl` is now honored as-is for
  every provider, and an empty one defers to the CLI preset's default endpoint. Existing
  connections keep working without migration: the old flow stored `provider`, `defaultModel`, and
  `baseUrl` as ordinary settings (the API key lives in SecretStorage), so those values are read
  exactly as before; a leftover `xiaomiApiPlan` entry in settings is simply ignored. Only a
  configuration that relied on the rewrite overriding a different manually-set base URL will now
  see that manual value used instead.
- Made connecting a provider honest, safe to cancel, and self-verifying. The key is collected
  BEFORE anything is created or switched, so Escape at the key prompt is a clean cancel that keeps
  the previous working connection (the old flow switched first, stranded a cancel on a keyless
  provider, and still announced success). Reconnect confirmation now happens upfront, before the
  user types a key. After a key is stored, the extension restarts the idle bridge with the fresh
  credentials and runs one explicit-intent live check through the new `doctor.providers.live`
  bridge method (capability-gated; requires `allow_live=true`; never passive), reporting
  "connected and working", a classified failure with a one-click "Replace key" retry, or an
  unverified-but-saved outcome — never a fake success. The busy provider card narrates each phase
  ("VS Code is asking for your API key", "Checking your key...") so the native prompts opening at
  the top of the window are explained. The command-palette "Add or update API key" now stores the
  key for the active profile — the same store the sidebar reads — instead of a global secret the
  CLI ignores for non-default profiles.
- Reconciled the extension parity contract with the new History, Settings, Forge, and Add Selection
  commands and their actual menu routes.
- Tightened release engineering with fully lazy activation, explicit virtual-workspace exclusion,
  minimum/current VS Code Extension Host runs, strict VSIX content checks, temporary-profile cleanup,
  and an isolated install/list smoke for the packaged extension.
- Replaced the generic "release-valid" shortcut with a production artifact chain. Generic VSIX
  dogfood is now component-only and explicitly not-for-release. The six-target managed-runtime
  workflow builds from committed `uv.lock`, audits locked dependencies, applies and reverifies
  platform-native signing policy, binds a schema-v3 ECDSA manifest plus the exact schema-v2 native
  evidence digest to release/provenance/SBOM/key
  lifecycle metadata, emits and verifies executable plus final-VSIX provenance/SBOM attestations,
  and clean-installs each target VSIX in a real production Extension Host before it can report
  `release_valid: true`. Manual provider evidence is schema v5 and binds the exact target package
  plus its installed-production dogfood summary and immutable release/host identity.
- Rewrote the public-facing documentation. `README.md` is now a Marketplace product page
  (positioning, requirements, install, features, providers, user-relevant settings, and the
  known limitations) instead of internal engineering notes; `SUPPORT.md` was rewritten against
  the real contributed command titles (**Check Setup**, **Check Connection**, **Troubleshooting**);
  and the verification, dogfood, release-policy, architecture, security-model, slash-command, and
  limitations material moved into `docs/` with an index.

### Fixed

- Made the unified sidebar resilient under real user timing: first-message slash commands no longer
  start an agent run, session creation and task startup are single-flight, stale async responses are
  discarded, rapid duplicate submissions do not create orphan work, approvals are owned and
  de-duplicated by session, New Task clears the prior transcript, and retained session context is
  restored through `workspaceState` after reload or a safe bridge reconnect.
- Removed duplicate protocol lifecycle events at their source, including contradictory successful
  tool completions rendered as failures. Added a real VS Code-client-to-Python-bridge smoke test that
  runs a readonly tool-using turn against the deterministic mock provider and asserts one canonical
  start/progress/completion/message-end sequence with no credential leakage.
- Closed long-running Forge races: Plan and swarm startup are single-flight and stale-result guarded,
  global Stop covers swarm work as well as Chat and Forge jobs, and bridge listeners detach during
  controller shutdown.
- Hardened the shared IDE bridge and controller lifecycle: concurrent callers now share one process
  startup/initialize handshake and one session or persisted-plan recovery; process and stdin errors
  invalidate Chat and Forge immediately; retained chat context and persisted Forge plans recover
  after reconnect; and stale create/run/poll/artifact responses cannot overwrite a newer session.
- Hardened the Python stdio runtime for long-lived use: incidental provider/library stdout is routed
  away from JSONL, worker-thread launch failures become redacted terminal job failures, closed
  sessions and terminal jobs are bounded, and reusing a closed session id clears its replay/job
  state. Added adversarial regression coverage for each lifecycle path.

### Security

- Hardened credentials end to end: API-key rotation/removal now safely restarts an idle bridge using
  one-way launch fingerprints, diagnostic and no-secret processes receive no inherited credential
  variables, command/error/result surfaces redact secret-like values, and provider credentials stay
  in VS Code `SecretStorage` rather than settings or launch-profile state.
- Updated the VSCE packaging toolchain and pinned the patched `brace-expansion` implementation used
  by development-only glob consumers; `npm audit --audit-level=high` now reports zero findings.

> The entries below predate the Sylliptor → Alysis Code rename and keep the product and command
> names that shipped at the time. Legacy `sylliptor.*` settings migrate automatically on activation,
> and a `sylliptor` executable on PATH is still found by the development override picker.

## [0.1.1] - 2026-06-10

- Added: the cockpit **Run Swarm** console (SW6) — a capability-gated, Workspace-Trust-gated swarm
  workflow in the webview. `Sylliptor: Run Swarm` (and the in-cockpit control) dispatch the active
  Forge plan across parallel workers, rendering a task grid (status by glyph/weight, never hue), a
  non-modal task-attributed approvals inbox (workers run without `--yes` — dangerous actions pause
  the worker until you Allow/Deny, never auto-denied), a cooperative-cancel control with honest
  checkpoint copy, and a per-task review surface that Keeps (applies the diff to the working tree
  only, never commits) or Discards each task, prominently listing any untracked files that were not
  applied. Failed or interrupted tasks offer an editable, pre-filled regenerate-subtree instruction
  (one click sends — never silent). The Run Swarm control is method-gated: dimmed with a structured
  "needs a newer Sylliptor CLI" reason when the bridge lacks `forge.swarm`, a working control when
  it is present. Merge stays review-only and broad auto/fullaccess swarm execution remains
  unavailable in the IDE. Strict CSP/nonce, zero-innerHTML, and `isChatPanelMessage` validation hold
  for every new `swarm.*` message.

- Changed (IDE swarm safety semantics, backend): swarm workers no longer run with `--yes`-style
  auto-approval — dangerous actions raise task-attributed approvals through the existing session
  approval system (requesting worker pauses, siblings continue, timeouts re-emit
  `approval_pending` and never auto-deny); `forge.swarm.start`'s `approval_scope_grants` now
  records launch-time pre-grants using the allow-for-session scope shape. Merge became review:
  nothing merges — completed tasks surface as per-task harvested diffs through new
  `forge.swarm.review`/`forge.swarm.apply`/`forge.swarm.discard` methods (typed client:
  `forgeSwarmReview`/`forgeSwarmApply`/`forgeSwarmDiscard`); apply touches the working tree only,
  never commits, and surfaces "untracked files created" sidecars so nothing lands silently;
  failed/interrupted items carry a `regenerate_subtree` recovery offer. ALIGNMENT: `forge.execute`
  jobs now use the same data-outcome status model as swarm jobs — a finished run is a `completed`
  job whose result payload and exit code carry the task outcome; only engine exceptions mark the
  job `failed` (consumers should read the result payload, not infer failure from job status).

- Added: typed bridge-client coverage for the new Forge swarm job surface —
  `forge.swarm.start`/`status`/`result`/`cancel`/`reconcile` (`forgeSwarmStart`, `forgeSwarmStatus`,
  `forgeSwarmResult`, `forgeSwarmCancel`, `forgeSwarmReconcile`) and the async review pair
  `forge.review.start`/`forge.review.result` (`forgeReviewStart`, `forgeReviewResult`). Swarm jobs
  are Workspace-Trust gated, cooperatively cancellable (interrupted tasks keep preserved worktrees),
  stream task lifecycle through the existing `swarm_worker_state_changed` event with reconnect
  replay, and `forge.swarm.reconcile` reports post-crash per-task state read-only with explicit
  idempotent harvest/discard actions. `features.forge.swarm.supported` is now true;
  `approval_scope_grants` is a reserved empty-array placeholder until approval routing lands.
  Cockpit swarm UX is not wired yet (methods are bridge-only; adoption lands later in this track).

- Changed (backend groundwork for IDE swarm support, no extension behavior change yet): the Forge
  swarm engine now supports cooperative cancellation end-to-end (first Ctrl+C in the CLI requests a
  graceful stop at worker checkpoints, second hard-exits) with a new distinct task status
  `interrupted` — killed/cancelled runs recover stale tasks as `interrupted` instead of `failed`,
  interrupted worktrees are preserved for harvest, and interrupted tasks reschedule automatically.
  Swarm workers also now run behind a swarm-layer write-scope path guard at the tool dispatch
  boundary (realpath/symlink/traversal hardened, win32 case-insensitive) that fails a task closed
  on out-of-scope writes while leaving siblings unaffected. The cockpit may start seeing the new
  `interrupted` task status in Forge plan payloads; it renders through the existing generic status
  path. IDE-facing swarm job methods arrive in a later prompt of this track.

- Added: typed bridge-client coverage for the new async plan-regeneration pair
  `forge.plan.regenerate.start`/`forge.plan.regenerate.result` (`forgePlanRegenerateStart`,
  `forgePlanRegenerateResult`). The bridge now runs plan regeneration without holding its dispatch
  state lock across the planner, supports cooperative cancellation via `forge.cancel`, and fails
  closed with `stale_plan_revision` when a concurrent plan edit lands during regeneration. Cockpit
  routes still call the sync `forge.plan.regenerate`; adopting the async pair in the Forge UI is
  tracked in the Forge-swarm-in-IDE track. The bridge also now preserves `session.getEvents`
  reconnect replay across `session.trace.clear` (`replay_preserved_after_clear`).

- Fixed: the header model pill now uses the live `session.modelInfo` payload instead of the static
  default fallback, so it shows the active session model plus profile. Before a live session model is
  available, it prefers the active provider profile model over the static `default` config fallback.
  When `session.setModel` is advertised, the pill renders real session model choices that call the
  typed bridge method; otherwise it routes users to Configure Provider with explicit copy instead of
  claiming bridge-side model selection exists.

- Added: Extension Host integration coverage now exercises the async Forge Plan path end-to-end
  against the deterministic mock bridge: plan start/result, Execute Review, native diff open,
  capability gates, no detached-method `TypeError`, and the forced Forge Plan failure path with one
  canonical error record/no duplicate UI error.

- Fixed: Manage tree domains no longer stay stuck on "needs newer Sylliptor CLI" after a healthy
  bridge check. Runtime bridge health is now the single compatibility source for native trees and
  the Cockpit, restored webviews re-run the same health path, and `Sylliptor: Manage Refresh`
  re-probes bridge health before redrawing the tree.

- Fixed: Forge mode Plan no longer crashes before starting when the bridge advertises async
  `forge.plan.start`/`forge.plan.result`. The Forge controller now calls bridge RPC methods through
  the bridge receiver, and the bridge client binds its public RPC methods so detached method
  references cannot lose `this` and bypass job tracking.

- Changed: wired the extension-bundled mascot images into the Cockpit with idle, replying, working,
  and dimmed recovery poses, updated the webview CSP to allow only extension-packaged image
  resources, and switched the VSIX icon to the new rounded-square `resources/icon.png`.

- Added: production-readiness signoff is now explicit and test-enforced. Release managers must use
  `docs/vscode_extension_production_signoff_template.md` after the strict automated gate,
  release-valid Extension Host dogfood, completed manual real-provider evidence, OS smoke evidence,
  no P0/P1 blocker review, and rollback plan. Release notes must preserve the known IDE v1
  limitations: Forge swarm is review-only (per-task Keep/Discard and never auto-merged); broad Forge
  exec and Forge auto/fullaccess remain unavailable; active cancellation is cooperative
  checkpoint-only with no hard interrupt; MCP OAuth is unavailable on Remote-SSH extension hosts;
  `hooks.watch` unavailable; raw trace unavailable;
  arbitrary terminal start and interactive terminal streaming unavailable; setup/config terminal
  menus and inline API key setters unavailable. Manual real-provider evidence now uses the
  `completed-manual-report` schema and must cover normal chat, Run Task, Forge show/status, missing
  provider recovery, broken CLI recovery, and separate Windows, macOS, and Linux smoke evidence
  before public beta signoff.

- Changed: began the FE-15 cockpit refresh by porting the approved Black/White token ramp into the
  real webview theme layer, replacing the header wordmark with the square Sylliptor mark + mono `SYLLIPTOR`
  wordmark + live bridge connection dot, using the same square mark for assistant avatars, and
  upgrading the empty Timeline state to a v5-style brand mark plus 24 suggestion chips. The chips
  route only through existing `submit` / `slash.quickAction` messages, so older-CLI degradation,
  Workspace Trust gates, CSP, and text-only rendering remain unchanged.

- Fixed: FE-15 review round 1 cockpit layout now uses the v5 chat-first shell: header, collapsed
  runtime Details drawer, scrolling Timeline, and a bottom-pinned composer. The shell-level Composer
  and Timeline titles/cards are gone, the composer has a single mode-driven primary action
  (`Send`/`Plan`) with no persistent Execute Plan button, header chips are de-duplicated to mode +
  model + compact trust/health Details toggle, Config is capability-gated, and the empty state now
  shows five curated starters without numeric prefixes while preserving existing bridge messages.

- Fixed: FE-15 review round 2 tightens the cockpit composer to match the v5 visual balance: the
  Chat/Forge control is a compact segmented pill, More is a quiet ghost action, the bottom row has
  one filled `Send`/`Plan` primary with the mono hint beside it, the textarea auto-grows from a
  two-line minimum, and the empty state centers four curated starters in one balanced row.

- Changed: made the core loop calmer and more honest (behaviour-preserving polish — no capability,
  gate, or wire changed). The **Execute** tab now reads as a confirmation rather than a protocol log:
  readiness, selected tasks, mode, sandbox, scopes, prerequisites, risks, verification, and approvals
  stay at the top, while protocol internals (Real Execute support, sandbox diagnostic, active
  cancellation) move behind a single collapsed **Details** disclosure (the dead "Execute Button:
  enabled/disabled" row is gone — the Execute Plan button's own state conveys it). **Diffs** drop the
  dead per-row Keep/Discard (they never had a bridge backing) and keep only **View diff** per row plus
  ONE Source Control pointer ("…review or stage these changes in the VS Code Source Control panel —
  Forge writes to your working tree, never commits or reverts."). The gated teases — Open as PR, Run as
  swarm, and the non-selectable **full access** mode — are separated out of the live controls: PR/swarm
  move into a "Not available yet" group on the renamed **More** tab, and full access sits under a "Not
  available" divider in the mode menu with an honest "coming soon — not yet enabled in the extension"
  reason instead of a control that errors on click. A shared empty-state component (decorative glyph +
  one line + at most one CTA) now backs the empty Plan / Diffs / Artifacts / Task-detail surfaces; the
  empty Plan tab offers a "Run a plan" CTA that drops `/plan ` into the composer. The **Reserved** tab
  is renamed **More**, surfaces live Forge assets with a count badge, and drops the now-live "Providers
  & profiles" row (it is first-class in the header). The cockpit now restores on window reload via a
  `WebviewPanelSerializer` instead of leaving a blank editor group, and the composer placeholder /
  Execute Plan chip wording distinguishes the mutating **Execute Plan** (writes to your working tree,
  never commits) from the non-mutating **/execute preview** (preview only).
- Added: a first-class **Manage** activity-bar tree (under the Sylliptor container) that browses
  Tools, Skills, MCP servers, Hooks, Conventions, and Extensions. Each domain group is gated on its
  capability (an unsupported domain shows a "needs newer Sylliptor CLI" note, never a faked control)
  and lists its items from the read-only `*.list/.catalog/.status` methods (redacted). Per-item
  context-menu actions — Show Details, Trust/Untrust, Enable/Disable, Remove/Uninstall, MCP
  Login/Logout — route through the gated action pipeline so each runs the capability + Workspace-Trust
  + confirm gates and renders its result/errors as an in-cockpit card. The existing group Quick Picks
  remain as a fallback.
- Added: wired the capabilities that had a bridge method but no extension entry point — `config.set`
  (guarded, non-secret) and `mcp.auth.login.start` (MCP OAuth login) — each capability-gated with
  results rendered in the cockpit. The mutating ones (`config.set`, `mcp.auth.login.start`) require
  Workspace Trust + confirmation and redact values. The `update.check`, `sandbox.doctor/setup/pull`,
  `doctor.bundle`, and `doctor.providers` actions are exposed through the "Sylliptor: Health and
  Diagnostics" Quick Pick (`update.check` is user-initiated only, never auto-run, apply stays
  CLI-only).

- Added: provider/profile switching and session management are now first-class cockpit surfaces
  (no longer Quick-Pick-only with Output-channel results). The header carries a click-to-switch
  provider/profile pill that lists the configured profiles (active marked) and switches with one
  click via `profile.use` — keeping the Workspace-Trust gate, the confirm modal, and the in-cockpit
  result card. It is gated on the `providersProfiles` capability (informational/disabled with a
  reason when unsupported) and on Workspace Trust (no switch option when untrusted); credentials
  still go through the existing Configure Provider / SecretStorage flow. The Sessions view now offers
  per-session actions via the item context menu — Show details / Score on any session, and the
  active-session-only Usage / Compact / Clear / Resume on the live session — each routed through the
  gated action pipeline so the result (and any error) renders in the cockpit, redacted. Session
  compact/clear/resume now require Workspace Trust in addition to their confirm modal. The empty
  Sessions state still shows the getting-started welcome.

- Added: management action results now render inside the cockpit instead of only the Output channel.
  Every backend action (run via slash, the group Quick Picks, or a hidden command) shows a live
  card in the Timeline that transitions busy → result: a spinner-by-glyph "Running…" state, then the
  structured result rendered as real DOM — a table for list-style results (profiles, tools, sessions,
  hooks, MCP, assets…), a key/value block for objects, or monospace text for raw output — or a
  redacted error message on failure. Results are redacted field-by-field (a new structure-preserving
  `redactDeep`) before they ever reach the webview, so secrets are scrubbed at every depth while the
  table/kv structure is kept. The Output channel still carries the full detail as a secondary log.
  Backend actions have no cancel path, so the lifecycle presents them as non-cancellable (no fake
  cancel affordance). The result-rendering + lifecycle (`ActionResultStore` + the generic payload
  renderer) is generic and reused by later management surfaces.

- Changed: bridge recovery now diagnoses the right problem and recovers on its own. A CLI that can't
  be found or launched (not on `PATH`, a wrong `sylliptor.cliPath`, or a failed transport) is now a
  distinct **"Can't reach the Sylliptor CLI"** card whose primary action is **Locate Sylliptor CLI** —
  no longer mislabeled "outdated"/Upgrade. A single detection failure renders exactly one card: the
  implied bridge-process error is folded into it (no duplicate "bridge-unreachable" sibling) and the
  standalone "Bridge health failed" Timeline/Diagnostics item is no longer recorded beside the card
  (full logs stay available via the card's Open Output action). Added an affirmative
  `● Bridge healthy — <mode> · <model>` line in Diagnostics when connected. On a recoverable failure
  the extension re-checks health automatically with bounded backoff and clears the cards on its own
  (each attempt honors Workspace-Trust / executable-origin gating and never auto-spawns a blocked
  CLI); a manual Reconnect/Restart Bridge re-reads `sylliptor.cliPath` and re-runs discovery, so a
  path set via Locate CLI applies live without a window reload.
- Changed: the webview JS (`media/chatPanel.js` + the view-model and DOM helper modules) is now
  type-checked and linted in CI. It was previously invisible to the type-checker (`.js`) and
  ESLint-ignored; it now carries `// @ts-check`, is checked by a dedicated `tsconfig.webview.json`
  wired into `npm run lint`, and `media/**` is linted. Added a jsdom render behavior test that mounts
  the renderer against a representative cockpit state and asserts the header/timeline/plan/diffs/
  diagnostics render with gated controls disabled — real coverage beyond string matching. Tightened
  the CSP to `img-src 'none'` (the cockpit renders no images), documented the JSON-escaping rationale,
  and removed dead/duplicated code (the unused Reserved tab badge; folded `keepDiscardButton` into the
  gated-button path). No cockpit behavior change.
- Added: a Forge review gate and a Forge assets surface. When the bridge advertises `forge.review`,
  a "Review changes" action runs an automated review of the working-tree changes and renders the
  decision (approved / changes requested), confidence, blocking/non-blocking issue counts, and a
  redacted summary, with links to the review artifacts. Review reads and annotates the working tree —
  it never commits, merges, or pushes, and Keep/Discard stays gated. (`forge.review` is a single
  blocking request with no job id, so no cancel affordance is offered — it shows a busy state while it
  runs.) When `forge.assets.list`/`show`
  are advertised, the Reserved tab lists plan assets and opens their metadata/preview; asset
  mutations stay disabled behind their capability + Workspace Trust.
- Changed: per-domain capability gating. The all-or-nothing `management` feature (which required all
  62 management methods) is split into independent per-domain ids (manageConfig, manageProfiles,
  manageSessions, doctor, sandbox, tools, skills, hooks, conventions, mcp, ext, update, report) — a
  missing `doctor` no longer disables `config`. The reserved capability ids now resolve to the real
  bridge method names (forgeReviewGate → `forge.review`; assets → `forge.assets.list`/`show`;
  providersProfiles → `config.*` + `profile.*`). `forge.pr`, `forge.swarm`, fullaccess mode, and
  diff apply/discard remain permanently gated with clear reasons.
- Changed: incremental, keyed transcript rendering. The cockpit no longer rebuilds the whole
  transcript on every state publish (including each streaming delta) — unchanged item nodes are
  reused, the streaming item is patched in place (text selection survives), and stale items are
  removed. Streaming publishes are coalesced to frame cadence (terminal/error states render
  immediately), and chat history is capped at the 200 most-recent items so memory does not grow
  unbounded (with an "earlier messages trimmed" note). The viewport sticks to the bottom only when
  the reader is already there; scrolling up to read no longer yanks back.
- Added: keyboard-navigable tabs and clearer screen-reader announcements. The Chat | Forge and
  workspace tablists now use WAI-ARIA roving tabindex with Arrow/Home/End navigation and a visible
  black-&-white focus ring; a dedicated polite live region announces only newly finalized messages
  (not the whole transcript), and the timeline carries `aria-busy` while a response streams.
- Made the cockpit discoverable and smoothed first run. The status-bar item and a new view-title
  button open the cockpit (bridge health stays reachable from the status-bar tooltip); the Sessions
  view shows an "Open Sylliptor Cockpit" / "Set up Sylliptor" / "Locate Sylliptor CLI" welcome; and a
  "Get started with Sylliptor" walkthrough covers install, trust, provider, and opening the cockpit.
- Added the "Locate Sylliptor CLI" command. It auto-detects the CLI (PATH, the active virtualenv,
  the configured Python interpreter dir, and common install locations), lets you Browse or enter a
  path, validates the binary with `--version` + `ide-bridge health` (output redacted, never spawning
  an untrusted workspace-local binary), and saves the chosen path to `sylliptor.cliPath` (workspace
  or global) before reconnecting. Surfaced from the welcome view, the walkthrough, and the recovery
  card's "Set CLI Path…" action.
- Collapsed the cockpit recovery surface so an outdated or misconfigured CLI no longer stacks
  3-6 near-identical "feature disabled" cards. Recovery cards now group by root cause — one card
  per cause, deduplicated by a stable id, capped, and ordered most-severe-first. The version gap,
  missing bridge methods, and every individually disabled capability fold into a single "Your
  Sylliptor CLI is older than this extension" card that names how many capabilities are affected.
  Cards render only in Diagnostics; the Timeline shows one low-noise pointer ("N issues need
  attention — open Diagnostics") that focuses the Diagnostics tab. Cards offer Install/Upgrade CLI,
  a new Set CLI Path… action (opens the `sylliptor.cliPath` setting), Open Setup Guide, and Run
  Doctor (only when the management capability is advertised).
- Added an optional native VS Code `@sylliptor` Chat Participant. The participant uses the stable
  VS Code 1.90 chat API, contributes `/help`, `/plan`, `/execute`, and `/doctor`, opens the
  Sylliptor Cockpit, and routes through existing controllers without proposed APIs or a GitHub
  Copilot dependency.
- Refined the Sylliptor Cockpit into a cleaner chat-first UI. The composer now has one textarea,
  one Send button, primary Plan / Execute Preview / Execute Review actions, and a More menu for
  lower-frequency commands; the header keeps only high-level Mode, Model, CLI Health, Bridge, and
  Sandbox chips; the Forge workspace collapses when inactive and auto-opens for Plan/Preview; and
  recovery cards provide Retry, Restart Bridge, Doctor, setup, and Output actions
  without dumping raw stderr into the visible Timeline.
- Restyled the Sylliptor Cockpit in a monochrome (black-&-white) theme that reads consistently in
  dark and light VS Code themes, encoding status by glyph, border weight, and fill rather than hue,
  and inverting the primary action button. The header status grid now collapses into a compact
  health pill (mode · model · trust) with a "Details" disclosure that groups the Session, Runtime,
  and Safety runtime rows. High-contrast themes keep their accessible colors; no behavior, bridge,
  or message-protocol changes.
- Added a Chat | Forge composer mode switch. Chat mode keeps the conversational Send action; Forge
  mode swaps in a primary Plan action that runs the existing `/plan` flow. The toggle is client-side
  UI state reflected into cockpit state via a new `composerMode.set` webview message (no IDE bridge
  call), so the active mode is assertable from the extension test API.
- Folded an Execute Plan readiness summary into the Forge plan card. When a plan loads, the cockpit
  auto-requests the execute preview under the hood — non-interactively, covering the whole plan with
  no task picker (the standalone "Execute Preview" chip is gone)
  and renders a "What Execute Plan will do" panel: write scope vs estimated files, required
  approvals ("will need") vs runtime approvals ("may need at runtime"), verification commands,
  trust/sandbox gate glyphs, readiness, missing prerequisites, and warnings — plus the
  working-tree-only invariant ("never commits or merges").
- Relabeled the whole-plan execute action "Execute Plan" (the wire stays the existing executeReview
  message → bridge `forge.execute`). It is review-only and stays disabled until the preview reports
  real execution supported, `preview_ready`, `approval_scopes_safe`, and review mode.
- Reworked the Forge diffs view into a diff-review card. Each changed file shows status/size and a
  "View diff" action that opens the native VS Code diff editor (via the existing `forge.diff.open` →
  `ForgeDiffContentProvider`). Keep / Discard (commit / revert) is rendered in a gated state — the
  bridge has no commit/revert capability yet, so the cockpit points to the VS Code Source Control
  panel instead of wiring a fake commit/revert.
- Added capability feature-ids so the cockpit can gate reserved slots ahead of backend support (all
  default OFF with today's CLI): `forgePr` (forge.pr), `forgeReviewGate` (forge.review), `forgeSwarm`
  (forge.swarm), `assets` (assets.*), `providersProfiles` (profile.*), `diffApplyDiscard`
  (diff.apply/diff.discard), and `modesFullaccess` (capabilities.modes includes "fullaccess"). They
  flow to the webview through the existing runtime compatibility snapshot and light up automatically
  when the CLI advertises the matching method/mode.
- Added an agent session-mode pill to the cockpit header. It offers readonly / review / auto (writing
  `sylliptor.defaultMode` through a new `mode.set` webview message routed via the existing `/mode`
  flow), marks the active mode, and trust-gates non-readonly modes. `fullaccess` appears as a reserved
  option, gated unless the bridge advertises it (`modesFullaccess`) AND the workspace is trusted, with
  a risk note. `/mode` now also accepts `auto`. Forge execution stays review-only regardless of mode.
- Wired the capability flags into reserved cockpit slots that stay gated until the backend advertises
  support: a "Reserved" workspace tab (Swarm workers / Providers & profiles / Plugins·Skills·Hooks),
  plan-card "Open as PR" (`forgePr`) and "Run as swarm" (`forgeSwarm`), a diff-card review-gate
  (`forgeReviewGate`), and the diff Keep/Discard buttons tied to `diffApplyDiscard`. Gating is driven
  by a small, unit-tested viewModel helper (`featureGate`/`featureSupported`), which now also owns the
  Execute Plan gate (`canExecutePlan`). Gated slots dim and show a "needs newer Sylliptor CLI" lock
  with the missing methods in the tooltip; when a capability is advertised ahead of the extension
  wiring the slot switches to an inert "pending" state (never an enabled-but-dead control). The
  Reserved tab is reachable from the workspace tab strip.
- Phase 4 polish & accessibility: welcoming empty/idle states — a framed "Welcome to Sylliptor"
  onboarding card and tone-glyph setup/recovery cards (▲ warn / ■ error) that read in both dark and
  light mono with stronger left-border contrast. The composer Chat | Forge switch is now a proper
  tablist (`role="tablist"`/`role="tab"`/`aria-selected`), approval buttons gained aria-labels, focus
  rings cover tabs and disclosures, and a `prefers-reduced-motion` guard was added.
- Added ESLint to the extension toolchain. A lean flat config (`eslint.config.js`, `@typescript-eslint`)
  now runs in the `lint` script alongside `tsc --noEmit` and in `prepublish:check`. Wiring it surfaced
  and removed dead code (an unused status-bar reference and the legacy execute-preview markdown renderer
  that the cockpit's readiness panel superseded). `compile`, `test`, `lint`, and `package:vsix` all pass.
- Prepared the public-beta release workflow without publishing: the extension manifest now uses the
  `0.1.x` beta-candidate line, strict VSIX packaging no longer allows missing repository metadata,
  package scripts separate local VSIX, pre-release VSIX, prepublish checks, and non-publishing dry
  run validation, and a manual release-candidate workflow gates Windows/default-console encoding,
  Forge suites, Extension Host, release-valid dogfood, pre-release VSIX marker verification, and
  manual sign-off inputs before uploading VSIX/dogfood artifacts without running `vsce publish`.
- Documented internal VSIX dogfood, public beta/pre-release, and stable Marketplace release gates,
  including full Forge exec/swarm/review suites, Extension Host integration, release-valid dogfood,
  manual real-provider evidence, known limitations, and Marketplace publisher/PAT prerequisites.
- Hardened IDE management protocol routing for the extension backend: retained session logs and
  rendered conventions are redacted/bounded, passive status refresh does not run network update
  checks, and skill/extension install sources require explicit safe local or reviewed remote/package
  source shapes.

## [0.0.1]

- Added formal extension/CLI compatibility handling for beta users. The Cockpit can open with
  baseline IDE bridge methods, feature-specific missing methods disable only the affected
  chat/Forge/Execute/diff/artifact workflow, setup/recovery cards show install/upgrade commands,
  and SecretStorage API keys are not forwarded to missing, broken, or incompatible CLIs.
- Added the initial Sylliptor VS Code extension scaffold.
- Added local CLI detection, bridge health validation, SecretStorage helpers, Workspace Trust gates,
  status bar states, placeholder views, and scaffold tests.
- Added the first live chat/run flow over Sylliptor IDE Protocol v1, including stdio bridge
  request correlation, structured event streaming, approval round-trips, honest cancellation
  handling, bounded event replay, and backend-scoped artifact list/read.
- Hardened bridge lifecycle and chat UI state handling: startup failures and process errors reject
  pending requests, SecretStorage API keys are passed through the bridge environment, stale session
  state is cleared after idle cancel/bridge exit, approval timeout/result events render explicitly,
  webview messages are validated by method payload, and stale process events are ignored after
  restart.
- Hardened production approval and webview safety: allow-for-session approval is scoped by exact
  command hash, exact file set, exact verification command set, or explicit backend-safe kind; the
  chat UI only offers it when the backend marks it supported; the webview uses a crypto-backed nonce,
  packaged local assets, strict CSP, and text-only rendering.
- Hardened bridge lifecycle on deactivate: the extension awaits cleanup, denies pending approvals,
  closes idle sessions, reports active jobs as not runtime-cancellable, validates the live stdio
  process with `initialize`, and only force-kills the bridge after a bounded graceful attempt.
- Added production test and release infrastructure: Extension Host integration tests with
  `@vscode/test-electron`, a real Python stdio bridge smoke in the Node test suite, Marketplace-ready
  package metadata with a PNG icon, SUPPORT and release checklist docs, and CI coverage for
  compile/lint/test/package/integration without automatic Marketplace publishing.
- Hardened Restricted Mode handling: sensitive Sylliptor settings are declared as restricted
  configurations, workspace-scoped execution settings are ignored before process launch, workspace
  CLI paths require Workspace Trust, auto-start cannot be enabled by untrusted workspace settings,
  and API keys are not forwarded to untrusted executables.
- Fixed remaining release blockers: chat transcript event replay now tracks sequence numbers per
  session, untrusted non-readonly starts are blocked before bridge spawn or SecretStorage reads,
  PATH-resolved `sylliptor` origins are checked before API-key forwarding, the real bridge smoke
  covers safe `session.create`/`session.list`/`session.cancel`, and `npm audit --audit-level=high`
  passes with dev-only Mocha transitive overrides.
- Added TypeScript protocol/client support for the backend Forge/diff foundation:
  `forge.plan`, `forge.status`, fail-closed `forge.execute`, `diff.list`, and `diff.get`.
  Forge Execute initially remained unavailable until the backend reported safe execution support.
- Replaced the Forge Plan placeholder with a real structured UI flow over `forge.plan`, including
  task tree rendering, structured event updates, grouped artifacts, opaque diff listing/opening, and
  native readonly virtual diff documents when the backend supplies old/new text.
- Kept Forge Execute honest and fail-closed when the backend reports unsupported protocol behavior;
  no fake execution progress is shown.
- Tightened release hardening: `@types/vscode` is pinned to the declared minimum VS Code engine,
  docs match the current Forge Plan UI state, and diff opening uses session/plan-scoped `diff.get`
  requests only.
- Productionized Forge Plan support: planner-backed `forge.plan` results now render richer task
  fields and incomplete-plan warnings, while `Sylliptor: List Forge Plans` and `Sylliptor: Open
  Forge Plan` reopen persisted plans through durable protocol methods without reading
  SecretStorage. Later entries in this section add the review-mode Execute path.
- Added `Sylliptor: Forge Execute Preview`, backed by non-mutating `forge.executePreview`. The
  preview renders selected tasks, file scope, verification commands, sandbox/trust state, safe
  approval scopes, blockers, and unsupported real-execution status without reading SecretStorage or
  showing fake job progress.
- Hardened Forge Execute Preview so the no-secret bridge path strips inherited `SYLLIPTOR_API_KEY`,
  empty task selections stop before bridge startup, and mutating preview modes show unverified
  sandbox availability as a blocker instead of treating a supported profile label as available.
- Hardened persisted Forge plan/diff safety: reopened plan artifacts and diff records now come only
  from validated non-symlink Forge run roots, while symlinked registry and patch components are
  rejected by the backend.
- Added launch-profile-aware bridge startup and async Forge Plan support. Chat, Run Task, and
  Forge Plan now require a credential-capable bridge profile, no-secret list/open/preview flows stay
  no-secret when possible, idle no-secret bridges can restart for model-backed work, and Forge Plan
  uses `forge.plan.start` progress/jobs when supported while keeping synchronous `forge.plan`
  fallback.
- Added the first real Forge Execute flow for review mode only. The command still runs Preview
  first, requires explicit confirmation, restarts the bridge with credentials only when safe, starts
  a selected-task `forge.execute` job, and renders structured progress, verification/review events,
  diffs, and artifacts. Auto/fullaccess execution and active cancellation remain unsupported.
- Hardened Forge Execute rendering and preview details: blocked review decisions and failed verify
  gates now render as failures, and the preview shows dynamic runtime approval requirements for
  shell/custom/MCP-like tool requests instead of only preflight file/verify approvals.
- Hardened Forge Execute job finalization handling in the backend contract consumed by the
  extension: terminal Execute events now arrive only after `job.status`, `completed_at`, and
  `exit_code` are terminal, while active cancellation remains unsupported/fail-closed.
- Hardened extension ownership for real Forge Execute: chat and Forge route bridge events by owned
  session/job ids, Forge approvals are shown and answered by Forge UI, the chat panel no longer
  consumes Forge approvals, and `Sylliptor: Cancel Current Run` reports unsupported active Forge
  cancellation honestly.
- Hardened Forge backend routing consumed by the extension: Forge Execute now advertises and passes
  explicit `no_log` / `max_steps` params while keeping subagents, auto/fullaccess execution, and
  active runtime cancellation unsupported. `/asset refresh`, `/asset check`, `/forge review`, and
  `/review` now route through typed, trust-gated backend actions, and repeated unsupported Forge
  cancellation attempts are suppressed per running job.
- Fixed Forge approval edge cases: approval prompts now display the backend action kind through
  `approval_kind`, Forge approvals buffered during async plan startup are answered by the Forge
  controller, and simultaneous chat/Forge active jobs require an explicit cancel target.
- Labeled the command as `Sylliptor: Forge Execute Review`, kept preview/execute disabled outside
  review-mode readiness, and updated release docs to require full Forge exec/swarm/review suites,
  Extension Host coverage, manual VSIX dogfood, and a disposable-repo review-mode Execute smoke.
- Added local VSIX packaging support for development builds.
- Added production chat-panel slash commands. `/help`, `/mode`, `/plan`, `/forge plan`,
  `/execute preview`, `/execute plan`, `/plans`, `/open plan`, `/diffs`, `/artifacts`, `/cancel`,
  `/doctor`, and `/config` now route through typed extension controllers/commands instead of
  `chat.send`; unknown slash commands fail closed with help. The webview includes slash
  autocomplete, keyboard selection, quick actions, and strict `textContent` rendering.
- Upgraded the webview into the Sylliptor Cockpit. Runtime status, slash-aware chat, Forge Plan task
  cards, task detail, Execute Preview readiness, Execute Review controls, persistent Forge approval
  cards, native diff actions, grouped artifacts, diagnostics, and replay/truncation notices now
  share one secure VS Code-themed surface. Extension Host tests cover cockpit plan/preview,
  approval denial, diff open, and artifact open through the mock bridge.
- Hardened Cockpit UX/observability. Slash commands now add immediate Timeline lifecycle entries,
  `/plan` reports validation, executable-origin, bridge, planning, completion, and failure stages,
  `/doctor` records started/completed/failed events with exit code and redacted stdout/stderr
  previews, CLI Origin is distinct from CLI Health, status bar and cockpit Bridge/Protocol/Sandbox
  chips are synchronized, contributed command titles no longer double-prefix `Sylliptor:`, and
  slash autocomplete uses Enter for complete commands while Tab/Right Arrow accepts suggestions.
- Added deterministic Cockpit E2E coverage through the VS Code Extension Host mock bridge. The suite
  now exercises `/help`, `/plan` success and failure, `/doctor` failure, broken health/missing-method
  status chips, `/execute preview`, `/execute plan` review-mode verification/review events, Forge
  approval cards, native diff open, artifact open, unknown slash command fail-closed behavior, and
  bridge exit reporting without model calls or API keys. `VSCODE_TEST_EXECUTABLE_PATH` is supported
  for cached/offline VS Code integration runs.
- Added the production dogfood release gate for Cockpit and Forge Execute Review v1. The repo-level
  `scripts/qa/vscode_extension_dogfood.py` harness prepares a disposable Python fixture, validates
  bridge health and VSIX packaging, runs the mock Cockpit E2E path without provider credentials, and
  writes a manual real-provider checklist for pre-Marketplace signoff.
- Hardened dogfood release behavior: local `--extension-host skip` smoke reports are explicitly not
  release-valid, release dogfood requires Extension Host `run` or `require-cached`, cached VS Code
  executables can be supplied through `--vscode-executable` or `VSCODE_TEST_EXECUTABLE_PATH`, and
  missing/download-unavailable VS Code failures now produce clean reports with explicit
  `release_valid`, `extension_host`, and preflight status fields.
