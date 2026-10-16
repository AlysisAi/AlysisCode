# Architecture

Alysis Code is a local CLI coding agent. It binds to a workspace, builds a
runtime session, exposes a controlled set of tools, sends turns to the selected
model provider, and stores local artifacts so work can be inspected after the
session ends.

This page gives a high-level view of the system. Detailed command behavior,
configuration keys, and subsystem contracts live in the feature-specific docs.

## Design Principles

- Local-first operation: source code, runtime state, and logs stay on the local
  machine unless the user configures a model provider, MCP server, web search
  backend, or other networked extension.
- Explicit workspace binding: file, shell, git, and runtime artifacts are scoped
  to a resolved workspace before a session starts.
- Host-owned policy: execution mode, sandbox settings, tool availability,
  approvals, and extension trust are enforced by Alysis Code, not by model prose.
- Progressive context: the model receives bounded workspace context and can ask
  for more through tools instead of receiving the entire repository upfront.
- Inspectable runs: session logs, verification output, Forge artifacts, and
  feedback bundles are written as local records for review and debugging.

## Runtime Flow

The High-level flow is:

1. The CLI resolves configuration, credentials, model/profile settings,
   execution mode, optional one-shot run deadline, diagnostic-log path, and the
   requested workspace path.
2. Workspace binding determines the workspace root and focus directory. Git
   repositories bind to their repository root; plain directories bind to the
   requested directory.
   The resulting `workspace_root` and `active_workdir` are carried through the
   session and tool layer.
3. Alysis Code creates a session with prompt context, workspace metadata, allowed
   tools, verification settings, and any enabled extension catalogs.
4. Every text turn goes straight to the main model with the full per-mode
   agent surface. No pre-turn classification runs: execution posture derives
   from the execution mode (`readonly` stays advisory; other modes carry the
   full execution contract), and explicit commands (`/ask` for one read-only
   turn, `/chat` for a minimal no-tools reply) replace inferred intent.
5. The agent loop sends the current turn to the model provider. If the model
   requests tool calls, Alysis Code runs allowed calls through host-owned
   permission, execution-mode, and sandbox checks — authorization follows the
   actual proposed action, never a prediction about the user's words.
6. Tool results are appended back to the session, and the autonomous loop
   continues until the task completes, the user cancels it, a genuine blocker
   or fatal error stops it, or an explicitly configured step/deadline safety
   limit is exhausted.
7. Logs and artifacts remain available locally for status views, resumes,
   feedback exports, and Forge execution reports.

## Main Components

### CLI And Session Runtime

The Typer CLI exposes `alysis`, `alysis chat`, `alysis run`, Forge
commands, setup commands, and supporting inspection commands.

The session runtime owns:

- workspace binding
- prompt assembly
- tool assembly
- autonomous continuation, optional safety limits, run-deadline checks, and
  final response handling
- session logging and local artifacts

`alysis chat` is interactive and supports commands such as `/status`,
`/permissions`, `/persona`, `/pwd`, `/subagents`, and `/forge`.

`alysis run` is the one-shot entrypoint. It is best for focused tasks that
can be completed from a single instruction. For exploratory or highly iterative
work, interactive chat or Forge is usually a better fit.

Both entrypoints share the same unified turn path. Execution mode, sandboxing,
and approvals decide whether a proposed action may run; the model's prose never
grants or removes capability.

One-shot runs may carry a single monotonic `ExecutionDeadline`. The same
absolute deadline is propagated through the session, tool assembly, verification
commands, shell commands, forced-summary handling, and child subagents. Where an
operation supports a timeout, the runtime clamps that timeout to the safe
remaining duration and restores client timeout state after the call. Provider or
tool operations that cannot be preempted are checked before launch and again
after return so the next expensive operation is not started.

### Model Provider Layer

Alysis Code talks to model providers through configured API profiles. The default
transport is chat completions, with provider-specific request normalization
where needed.

Model choice, base URL, API key source, timeout, reasoning options, and role
overrides are resolved before a session starts. Provider credentials are never
embedded in project files by Alysis Code.

Provider calls use typed transient retry classification. Cause chains are
inspected for provider throttling, remote-protocol stream truncation,
incomplete chunked reads, peer-closed transports, and related network resets;
authentication failures and permanent client errors are not retried. Retry
sleeps consult the shared `ExecutionDeadline`, and the soft finalization phase
allows at most one bounded provider restart. Streaming output is buffered per
attempt until a complete stream is accepted, which keeps partial tool calls and
duplicate deltas out of the authoritative turn state after a retry.

### Built-In Tools

Built-in tools are host-owned Python implementations. They cover local
filesystem operations, search, symbol lookup, shell execution, git history, web
fetch/search, session history, verification, and related workflow helpers.

A tool being implemented does not mean it is always visible to the model. The
actual tool surface depends on:

- execution mode
- runtime kind
- workspace binding
- sandbox readiness
- feature configuration
- extension trust and filtering

Web access is optional. `web_fetch` retrieves a specific URL. `web_search` is a
host-owned discovery tool that appears only when a supported search runtime is
configured. The active model decides when to call it through the normal tool
loop; the host validates the request, dispatches the configured provider-hosted
or external adapter, and returns bounded results as untrusted external data.
Backend availability remains independent of the selected model because direct
providers without hosted search can use the shared external adapter. See
[Web Search](web-search.md) for policy and provider coverage.

Tool dispatch is fail-closed for unknown names. Missing tools produce a
structured correction payload with available names and nearest suggestions. The
runtime may transparently execute only explicit schema-compatible aliases; broad
or ambiguous aliases remain no-ops and feed the existing repeated-call guard.

Web fetch provenance is session-owned state. User-supplied URLs, search results,
observed canonical redirects, bounded same-origin/search-mediated recovery
URLs, and source-linked URLs discovered inside trusted fetched pages, local file
reads, and registered tool or shell output are recorded with normalized keys and
bounded event metadata. The provenance layer never bypasses safe HTTP
validation, and finalization suppresses optional web-search recovery.

### Workspace And Safety

Alysis Code resolves a workspace before exposing local tools. Relative
file/search/shell paths are interpreted inside that workspace. Broad paths such
as a home directory require explicit confirmation or override, and the
filesystem root is blocked as a workspace root.

Execution modes define the default approval posture:

- `readonly`: inspect only
- `review`: ask before writes and sensitive commands
- `auto`: allow routine edits with fewer prompts
- `fullaccess`: remove mode-level prompts for trusted workspaces

Shell and verification commands can run through sandbox backends such as Docker
or Bubblewrap when configured. Network and URL handling use host-side safety
checks before requests are made.

### Verification

Verification is treated as part of the runtime contract, not as a free-form
model convention. Alysis Code can infer likely commands from the workspace,
accept explicit `--verify-cmd` values, and expose a `verify_run` tool when
verification is enabled for the session.

Forge workflows can make verification authoritative for execution gates. Normal
chat and one-shot sessions use verification as task evidence and completion
support.

Verification evidence is classified before it affects a gate. Authoritative
managed or effective commands are strongest, repo-native project commands can
satisfy ordinary task verification, and task-specific acceptance checks are
valid only when no known verification contract takes precedence. The classifier
rejects non-executing observation commands and verification runs that mutate
verification-relevant material files.

Verification availability is distinct from verification success. A managed host
can provide explicit verifier commands, report that no host verifier is
available, or explicitly disable verification through the existing disabled
session path. Host-verifier absence never becomes a successful command and is
not treated as disabled verification; repo-native discovery and task-derived
acceptance checks still run when verification is enabled.

Command selection is backed by `verification_command_analysis.py` and
`verification_contract.py`. The analyzer normalizes each command once, unwraps
approved runners and shell `-c`/`-lc` forms, identifies a primary command
family, records shell-control and pipeline policy, finds checker entrypoints,
and assigns an evidentiary capability. The contract layer serializes that into
typed specs with stable IDs, execution mode, provenance, trust level,
requirement level, working directory, timeout policy, mapped criterion IDs, and
validation diagnostics. Trusted provenance controls whether a command is
authorized to execute; it does not prove that the command checks anything.
Vacuous, non-assertive, or failure-masking commands such as `true`, `echo ok`,
`python -c 'pass'`, plain `curl -s`, and `pytest -q || true` are invalid or
inconclusive verification specs. `ARGV` commands are ordinary single-command
shapes. The only trusted shell expression accepted by default is a safely
analyzed wrapper such as `cd <path> && <single assertive command>` or
`bash -lc '<single assertive command>'`. Pipelines are rejected as authoritative
verification unless a controlled pipefail-capable runner can prove upstream
failure propagation consistently across host, bwrap, and Docker. Interpreter
snippets are accepted only when the prompt identifies executable validation and
the language/syntax can be preflighted. Invalid inferred fragments remain
diagnostic and cannot become hard executable requirements.

Task-aware selection prefers host and explicit user commands, then discovered
repo-native checks. Generic `pytest -q` is selected only when the pre-turn
workspace scan shows a trustworthy Python test surface; an agent-created test
file, Python-looking output, or plain directory does not retroactively create
authority. The same logic runs in one-shot and interactive execute turns.
Exact trusted commands run through `shell_run` can record equivalent
verification coverage, but only for successful, real, current-generation runs
that do not mutate verification-relevant material paths.
Verification result construction also separates process execution from contract
coverage. A command result carries `passed`, `failed`, `inconclusive`,
`not_executed`, or `stale`; legacy `ok` and `all_passed` are derived from that
status so exit code zero with unknown real execution cannot be presented as a
passing contract.

`agent/acceptance_contract.py` adds criterion-level coverage for execute-intent
turns. It builds a bounded contract from trusted inputs without a separate LLM
call: the user instruction, task brief, planning constraints, pre-turn
workspace scan, pre-existing tests/checkers, effective verification selection,
and host verification commands. Criteria cover required paths, formats,
explicit commands, protocol/service behavior, dependencies, preservation,
thresholds, host checks, and pre-existing repo check surfaces. Evidence origin
is tracked separately from the existing verification category, distinguishing
host-authoritative, user-explicit, pre-existing repo-native, pre-existing task
checker, direct black-box, self-authored, and ad hoc observations. This lets a
direct failed threshold or preservation check outrank unrelated passing tests
written during the same turn.
Checker integrity is host-owned at snapshot time. Pre-existing checker
entrypoints are fingerprinted by resolved path, regular-file state, size, and
SHA-256 content hash. If an agent creates, modifies, replaces, deletes, or
symlink-retargets such a checker, the evidence is downgraded to supplemental
mutable-authoritative-checker/self-authored evidence and must be backed by an
independent acceptance path.

The contract uses a typed path model instead of treating every path-like string
as a required output. Each reference records raw text, display text,
workspace-relative path when available, safe absolute path when applicable,
path kind, and role. Absolute paths under the workspace are normalized to
workspace-relative paths; external absolute paths and unresolved `..` paths are
represented distinctly and are not inspected as repository mutations. Criteria
also carry confidence and enforcement. Host-authoritative and exact explicit
requirements are hard; weak references, broad residual context, and
extension-only format guesses are advisory.

Persistent-service criteria are grounded in explicit process ownership. Ordinary
`shell_background` processes are session-owned and are terminated by
`AgentSession.close`; they can help inspect behavior but cannot prove service
persistence. `shell_service_start` creates a durable service with detached
stdout/stderr logs and sanitized metadata under the runtime sessions directory.
The acceptance contract records the resulting `service_id`, requires a live
ready status, and rechecks readiness during finalization before the completion
gate can pass.

### One-Shot Completion Gate

Execution-style `alysis run` turns cannot finalize successfully from
assistant claims alone. The runtime requires the currently applicable evidence:
a non-empty final response, material repository work unless a concrete
blocker is accepted by existing policy, and verification when the touched paths
or configured verification contract require it. Required acceptance criteria
must also be passed or represented by an accepted blocker; missing output
artifacts, explicit failed checks, unexpected material scope changes, and
insufficient threshold evidence are completion-gate problems.

`agent/completion_certificate.py` is the pure policy layer that turns the
current execution state into `SUFFICIENT`, `INSUFFICIENT`, or `CONTRADICTED`.
It evaluates material work, hard acceptance criteria, current-generation
verification coverage, stale evidence, explicit preservation failures, known
threshold misses, and blocker validity. Advisory criteria are reported in
payloads but do not block completion. A known hard failure cannot be overridden
by unrelated passing verification, and self-authored tests cannot alone prove
an unrelated hard criterion.

The completion gate is implemented by `agent/completion_gate.py`. It returns
one of four explicit decisions that also appear in telemetry:
`ALLOW_FINAL`, `NUDGE_AND_CONTINUE`, `TERMINATE_STAGNANT`, or
`TERMINATE_BUDGET_EXHAUSTED`. A rejected final answer normally produces a
targeted nudge and another model step; it does not by itself mean the whole
run must stop. Nudge events include the controller's machine-readable reason
so downstream telemetry can distinguish missing requirements, meaningful
progress rechecks, stagnant episodes, and budget exhaustion without parsing the
human nudge text.

Rejected-finalization stagnation has a single authority: the completion-gate
controller. Duplicate nudge detection records `nudge_stall_detected` telemetry,
but it does not bypass `NUDGE_AND_CONTINUE`. The default policy allows two
targeted repair nudges for an unchanged evidence episode and terminates on the
third unchanged invalid finalization, with a small consecutive no-progress cap
as a global backstop.

The acceptance-contract, completion-certificate, and semantic-progress logic
live on the shared turn path, so they apply to interactive execute turns as
well as one-shot execution. Runtime-specific policy remains limited to
one-shot-only concerns such as invocation-wide host deadlines.
Routing now also has a deterministic local-materialization layer ahead of
non-repo finalization. Requests to save, write, create, export, move, or put a
result in a local file/directory force the repo-capable loop with execute
posture, even if web or MCP tools are needed first. The repo loop keeps those
tools available, builds the normal acceptance contract, and prevents a
`non_repo_completed` final answer while high-confidence materialization is
unresolved. Non-execution opt-outs such as explain-only, plan-only, advice-only,
and no-modification keep precedence.

The one-shot run deadline is a progress-aware controller layered under the
completion gate. `ExecutionDeadline` tracks normal work, a soft finalization
window, and hard exhaustion from one monotonic clock. The finalization reserve is
deterministic: it combines a bounded minimum, observed LLM/tool/verification
latency, cleanup time, and a relative cap so it cannot consume the whole run.
When finalization begins, the turn receives one system directive to materialize
the best valid result and checkpoint requested output paths. New subagents,
broad exploration, background work, provider retry sleeps, and speculative
optimization are denied; edits, foreground shell work, local summaries, and
bounded high-value verification may still start when hard time remains.

Externally time-bounded hosts add one layer above this controller. A benchmark
adapter should compute the Alysis Code deadline from the host framework's final
effective agent timeout, after any host timeout multiplier has already been
applied, then subtract a host reserve for process collection and artifact
flushing. Verifier and environment-build timeouts are not valid sources for the
agent deadline. The resulting invocation should include `--deadline-seconds`,
`--require-deadline`, and a positional `--` before the complete instruction so
dash-leading or shell-looking instructions remain data rather than CLI options.
`--require-deadline` is intentionally opt-in and affects only managed one-shot
launches; interactive `alysis chat` does not receive a wall-clock run
deadline from this contract.

The Terminal-Bench adapter deliberately defaults to host verifier unavailable:
it does not export `ALYSIS_VERIFY_CMD`, does not write `true` into
`verify_commands`, and does not pass `--verify-cmd` unless an explicit
non-empty verifier is supplied. When unavailable, setup clears managed-profile
explicit verifier commands and marks the host verifier unavailable so
repo-native discovery can still run. Adapter diagnostics record sanitized
verifier status/source/count and stable command hashes, not raw verifier
commands; simultaneous `verify_cmd`/`verify_cmds`, empty entries, and unordered
sets fail before source copy or model invocation.

`agent/verification_evidence.py` owns the evidence hierarchy, while
`agent/mutation_classification.py` separates benign runtime artifacts from
material source/config and deliverable paths. This keeps coverage files and run
logs from looking like product work, while preserving fail-closed behavior when
a verifier changes source, config, or a requested output.

The one-shot prompt contract mirrors that controller behavior. Execute-intent
runs are autonomous and cannot pause on a standalone text-only plan or progress
update. After read-only exploration, the next model step should normally edit,
create the requested deliverable, run an implementation-producing command,
verify existing work, or report a concrete evidence-backed blocker. Explicit
plan-only, advice-only, review-only, and no-modification requests remain
non-execution requests.

Empty assistant responses after tool results are recovered as model-control
anomalies: the runtime records the anomaly, appends a tool-action directive, and
retries under a separate anomaly budget instead of emitting empty final text.
The directive names the missing next action: implement required output, edit a
relevant path, run required verification, or report a concrete blocker. If the
active provider explicitly advertises forced tool choice and a safe schema is
available, the next call may request that tool; providers without this
capability fall back to ordinary prompting. During the finalization window only
one compact anomaly recovery call is allowed, and repeated empty responses stop
with a local truthful summary instead of another LLM-generated forced summary.
Generic clarification-only answers are invalid for execute-intent one-shot and
interactive execute tasks when a safe best effort is possible; clarification can
still finalize for safety-critical ambiguity, credentials or unavailable
external inputs, destructive alternatives, or non-execution intents.

Stagnation is tracked by progress episodes rather than a raw one-repair-per
stage counter. Episode evidence includes the gate stage, material edit count
and touched paths, verification-relevant edit generation, verification
coverage and missing commands, normalized verification failure signatures,
acceptance status counts and failure signatures, and accepted blocker state.
Meaningful progress includes real edits, required non-runtime deliverables,
accepted verification, improved hard-criterion coverage, changed
missing-command state, a changed failure after an intervening edit, or concrete
blocker evidence. Repeated read-only calls, unknown-tool correction loops,
duplicate clarifications or nudges, repeated plan/progress prose, repeated
identical final claims, empty replies, and rerunning the same failing
verification without a relevant edit do not indefinitely reset the episode.

Completion-gate stagnation is distinct from exhaustion of an explicit step
limit. The forced-summary path carries a structured termination kind so
fallback summaries only say a step limit was exhausted when the user or a
special managed profile actually configured one. Autonomous execution has no
default step ceiling.

### Extensions

Alysis Code has several extension points. They are intentionally separated so
each has a clear trust model.

- MCP connects external Model Context Protocol servers. User configuration is
  higher trust; project configuration can only narrow exposure.
- Skills are local instruction bundles rooted at `SKILL.md`. They provide
  reusable workflow guidance and are loaded progressively.
- Plugins package trusted bundles that may contribute skills, custom tools, MCP
  servers, and hooks.
- Custom tools are trusted Python files with a manifest and `run(args)`
  entrypoint. Project tools require an explicit trust decision.
- Hooks run deterministic command-based policy or automation around lifecycle
  events and require trust for project-local configuration.
- Subagents are session-owned child runs used by normal chat and one-shot flows.
  They can run synchronously or in the background; shared read-only calls and
  isolated-worktree calls are eligible for bounded parallel scheduling.
- Isolated writers leave retained candidates for explicit host-checked apply or
  discard. Read-only children can pin an unreleased candidate for exact-view
  verification, and depth-1 writers can use bounded non-editing helpers in the
  same workspace. The registry also owns steering messages, dependency chains,
  persisted status/view data, and terminal-run resumption.
- Child schedulers and workspace providers are neutral agent modules. They do
  not depend on Forge execution or swarm workers, and those
  runtimes do not receive the top-level background orchestration surface.
- Every child uses the earlier of the parent run's absolute deadline and its
  finite configured fallback ceiling, so delegation cannot extend a one-shot
  run beyond its original time limit and cannot run unbounded without one.

See [MCP](mcp.md), [Skills](skills.md), [Plugins](plugins.md),
[Custom tools](custom_tools.md), [Lifecycle hooks](hooks.md), and
[Subagents](subagents.md) for the user-facing contracts.

Release work that touches completion-gate, verification-evidence,
deadline, diagnostic, or compatibility behavior should also follow the
[release checklist](release_checklist.md).

### Forge

Forge is the plan-driven workflow for larger tasks. It creates a structured
plan, executes scoped tasks, records verification and review evidence, and keeps
run artifacts under the workspace runtime directory.

Forge is stricter than normal chat. It expects scoped tasks, concrete write
paths, and clear verification commands for strict gates. Use it when a change
is large enough that planning, task boundaries, and review artifacts are useful.

See [Forge](forge.md) for commands and operational guidance.

### Server Mode

Server mode exposes Alysis Code through an HTTP API for managed runs and worker
jobs. It reuses the same workspace binding, execution modes, tool policy, and
artifact model as the CLI.

See [Server mode](server.md) for API and deployment details.

## Local State And Artifacts

Alysis Code writes local state for sessions, logs, verification output, Forge
runs, knowledge artifacts, tool-output artifacts, and feedback exports. Runtime
artifacts are kept separate from source files and are excluded from normal
project work whenever possible.

Crash diagnostics are a separate opt-in JSONL stream for minimal lifecycle
metadata. They are append-only and can remain enabled when normal session logs
are disabled with `--no-log`, but they intentionally omit prompts, tool
arguments, command output, source text, credentials, authorization headers, and
environment dumps.

The important rule is that artifacts are evidence, not hidden authority. The
host decides which artifacts are used for resuming, verification, planning,
Forge execution, and feedback export.

## Trust Boundaries

Alysis Code keeps trust-boundary decisions in the host runtime: external content
may inform a session, but it cannot override system instructions, user
instructions, execution mode, sandbox settings, workspace binding, or host-owned
policy checks.

See [Security model](security_model.md) for the full list of untrusted inputs
and the rules that apply to them.

## Where To Go Next

- [Quickstart](quickstart.md): install and run the first session.
- [Reference](reference.md): detailed runtime behavior and configuration.
- [Security model](security_model.md): trust boundaries and sandboxing.
- [Shell sandbox](shell_sandbox.md): Docker and Bubblewrap setup.
- [Forge](forge.md): plan-driven execution.
- [MCP](mcp.md): external server integration.
- [Skills](skills.md): reusable instruction bundles.
- [Plugins](plugins.md): extension packaging.
- [Custom tools](custom_tools.md): trusted Python tools.
- [Lifecycle hooks](hooks.md): command-based policy and automation.
- [Release process](RELEASING.md): maintainer release steps.
- [Release checklist](release_checklist.md): completion-gate regression and
  compatibility checks.
- [Server mode](server.md): HTTP API operation.
