# Release Checklist

This checklist covers the completion-gate reliability and deadline/diagnostic
runtime safety invariants that should stay true across releases.

## Recovering an unpublished Python version

If a tag's publishing run timed out after its exact source passed complete CI,
run `release.yml` manually from the public repository's default branch. Supply
the existing `release_tag`, its independently verified full `release_commit`,
and the successful complete `ci_run_id`. Do not move the tag or change the
package version to recover publication. Recovery verifies all four Python
test jobs, platform reliability jobs, workflow lint and package smoke from that
exact source; it then repeats release qualification on Windows, macOS and Linux.
The packages are built from the tag, validated and signed with provenance,
an explicit release-source binding and the dependency SBOM before the existing
`pypi` environment approval and trusted-publisher upload.

## One-Shot Completion Gate

- Safety invariant: a one-shot execute-intent run must not finalize
  successfully after only reading, exploring, planning, or making an
  unsupported claim. It needs material work plus applicable verification, or a
  concrete evidence-backed blocker accepted by policy.
- Liveness invariant: rejecting an invalid final answer should continue with a
  targeted nudge, unless the run reaches repeated no-progress stagnation, an
  explicit safety-limit exhaustion, deadline exhaustion, cancellation, or
  another explicit terminal condition.
- Gate decisions remain machine-readable as `ALLOW_FINAL`,
  `NUDGE_AND_CONTINUE`, `TERMINATE_STAGNANT`, and
  `TERMINATE_BUDGET_EXHAUSTED`.
- Stagnation is based on progress episodes, not a one-retry-per-stage counter.
  Material edits, deliverables, verification coverage, changed failures after a
  relevant edit, and real stage progress can renew an episode. Read-only calls,
  repeated prose, empty replies, and repeated identical verification failures
  without edits do not.
- Semantic progress must not be inferred from a changed episode hash alone.
  Required outputs being created, hard criteria moving to passed, verification
  coverage improving, missing commands shrinking, preservation repairs, and
  failure changes after a material-generation change are progress; cosmetic
  final text, benign cache files, arbitrary touched paths, and changed failure
  wording alone are not.
- The completion-gate controller is the only rejected-finalization stagnation
  authority. Duplicate nudge detection must remain telemetry only; it must not
  return a hidden `nudge_stall` final response before the controller terminates.
- The default unchanged-episode policy permits two targeted nudges and
  terminates on the third unchanged invalid finalization, with separate
  consecutive no-progress and explicit-limit/deadline exits.
- Empty assistant responses after tool results must record recovery telemetry,
  issue a tool-action directive, use only capability-gated forced tool choice,
  and never become the user-visible final answer.
- Execute-intent one-shot tasks must reject generic clarification-only answers
  when a safe best effort is possible, while still allowing clarification for
  safety-critical ambiguity, credentials, unavailable external inputs, or
  destructive choices.
- Interactive execute turns must share the same local-materialization,
  clarification, empty-response, and semantic-progress behavior as one-shot
  runs.
- Requests that save, write, create, export, move, or put an answer/result in a
  local path must route to repo execution, keep web/MCP tools available for
  web-assisted execution, and must not finish through `non_repo_completed`
  before the artifact is resolved.
- Semantic progress must come from material edits, required artifact creation,
  accepted verification, improved hard-criterion coverage, or concrete blocker
  evidence. Repeated read-only calls, unknown-tool loops, duplicate
  clarifications/nudges, empty replies, and cosmetic text must not reset
  stagnation.
- Forced-summary wording must distinguish completion-gate stagnation, explicit
  step-limit exhaustion, and invocation deadline exhaustion.

## Autonomous Execution And Optional Limits

- Normal chat, one-shot, Forge, conflict-resolution, and subagent loops use the
  `autonomous` policy and must continue beyond the persisted legacy caps until
  completion, cancellation, a genuine blocker, a fatal error, or an optional
  invocation deadline.
- `--max-steps N` and a subagent tool call's `max_steps` are explicit safety
  limits. They must still stop at exactly the requested ceiling and produce a
  truthful forced summary instead of silently switching back to autonomous.
- The opt-in `limited` policy uses persisted chat, task, and subagent caps.
  Legacy `adaptive` and `fixed` values must load canonically as `autonomous` and
  `limited`, respectively.
- Setup and `/config` must not ask users for execution-policy or step-budget
  choices. Status surfaces should report `unlimited` for autonomous execution
  and concrete values only for explicit or opt-in limited ceilings.
- Autonomous execution removes the loop-wide step ceiling, not safety or
  liveness controls. Cancellation, fatal provider/tool failures, completion
  stagnation, per-operation timeouts, and explicitly configured one-shot
  deadlines remain independent terminal conditions.

## Verification And Mutation Evidence

- Authoritative/effective verification commands take precedence over
  supplemental or task-specific checks.
- Repo-native verification commands can satisfy ordinary tasks when no stronger
  contract is active.
- Task-specific acceptance checks are valid only when they execute a concrete
  changed script, validation executable, or output comparison and no known
  verification contract needs precedence.
- Non-executing commands, help/list/collect-only forms, and commands with
  disallowed shell control flow do not satisfy verification.
- Verification command specs must be backed by the canonical command analyzer
  and include stable IDs, normalized primary command, execution mode,
  provenance, trust level, requirement status, timeout policy, criterion IDs,
  validation status, shell-control classification, pipeline policy,
  evidentiary capability, and rejection diagnostics while preserving legacy
  string command config and `verify_run` payload fields.
- `NON_ASSERTIVE` commands must be invalid as verifiers. `UNKNOWN` commands may
  be diagnostic, but they must remain inconclusive and cannot satisfy a
  verification contract by default. `ASSERTIVE` commands still need successful,
  fresh, host-observed real execution.
- Safe compound handling must stay narrow: leading environment assignments,
  approved runner prefixes, shell `-c`/`-lc` wrappers, and
  `cd <path> && <single assertive command>` are allowed only when the inner
  command is fully analyzed. `cd <path> && true`, arbitrary `&&`, `||`, `;`,
  and pipelines must not satisfy verification.
- Pipelines must fail closed unless every execution backend uses a controlled
  pipefail-capable shell and the analyzer validates every stage required for
  the check. Generic `/bin/sh` pipeline status from `cat`, `tail`, or `tee`
  must not become authoritative verification.
- `verify_run` must expose command `status` values (`passed`, `failed`,
  `inconclusive`, `not_executed`, `stale`) and derive `ok`/`all_passed` from
  those statuses. Exit code zero plus unknown real execution must not be
  model-facing `all_passed=True`.
- Interpreter snippets must be converted to interpreter invocations after
  language/syntax preflight; malformed backtick prose stays invalid or advisory
  and must not be sent to `/bin/sh`.
- Benign runtime artifacts such as `.pytest_cache/`, `__pycache__/`,
  `.ruff_cache/`, `.mypy_cache/`, `.coverage`, Alysis Code runtime directories,
  and grounded Rust `target/` output do not count as material work.
- Unknown new outputs and requested deliverables remain material by default.
- A verifier that mutates verification-relevant material paths cannot satisfy
  the gate until a later clean verification pass restores eligibility.
- Exact trusted commands run through `shell_run` can cover the verification
  contract directly, but only for successful real executions in the current
  generation that do not mutate verification-relevant material paths.
- Pre-existing checker entrypoints inside an agent-writable workspace must be
  fingerprinted by resolved path, regular-file state, size, and SHA-256 hash.
  Created, modified, replaced, deleted, or symlink-retargeted checkers must
  emit a mutable-authoritative-checker downgrade and remain supplemental until
  an independent acceptance path covers the task.

## Acceptance Contract And Provenance

- Execute-intent turns build an acceptance contract from trusted inputs without
  an extra LLM call: user instruction, task brief, planning constraints,
  pre-turn workspace scan, pre-existing checks, and host verification commands.
- Required criteria cover concrete artifacts, exact user or host commands,
  format/schema hints, service/protocol requirements, preservation rules,
  thresholds, and pre-existing repo check surfaces.
- Path references must retain type and role. Workspace-internal absolute paths
  normalize to workspace-relative paths, external absolute paths stay external,
  parent traversal is unresolved/rejected, preserved inputs must not become
  required outputs, and dotted filenames in preservation clauses must stay
  intact.
- Criteria must carry confidence and enforcement. Host-authoritative and exact
  explicit user requirements are hard; weak references, residual broad context,
  and extension-only format guesses are advisory. Only hard criteria can create
  completion-gate problems.
- Completion certificates must reduce finalization state to `SUFFICIENT`,
  `INSUFFICIENT`, or `CONTRADICTED`, with known hard failures taking precedence
  over unrelated green verification.
- Evidence origin remains separate from verification category:
  host-authoritative, user-explicit, pre-existing repo-native, pre-existing
  task checker, direct black-box, self-authored, or ad hoc observation.
- Self-authored tests are supplemental unless independent evidence covers the
  required criterion; they must not override direct failed thresholds,
  preservation failures, or failed exact checks.
- One-shot execute turns refine generic verification fallback just like
  interactive execute turns. Non-Python or artifact-only tasks must not inherit
  generic pytest when no trustworthy Python test surface exists, while explicit
  host commands and discovered repo-native checks remain authoritative.
- Generic `pytest -q` is authoritative only with a trustworthy pre-existing
  Python test surface. Python-looking outputs, plain directories, and
  agent-created tests are not enough to create that surface.
- Required output paths missing at finalization, unexpected material scope
  changes, missed thresholds, and durable-service evidence gaps must keep the
  completion gate closed or produce a truthful blocker.
- Persistent-service criteria must require `shell_service_start` ownership,
  live readiness on finalization recheck, and matching TCP readiness when a
  port is specified. `shell_background` remains session-owned and insufficient
  for durability.

## Deadlines, Diagnostics, And Compatibility

- `run_deadline_seconds`, `ALYSIS_RUN_DEADLINE_SECONDS`, and
  `--deadline-seconds` must resolve consistently for one-shot runs and report
  the correct source: explicit CLI, environment, config, inherited parent, or
  absent.
- Managed-host one-shot invocations must pass `--require-deadline` with a
  finite deadline. If asserted and absent, Alysis Code must fail before session
  creation, routing, or model calls and should record only sanitized diagnostic
  metadata when diagnostics are enabled.
- Benchmark adapters must derive the Alysis Code deadline from the final
  effective agent timeout exactly once, never from verifier or environment-build
  timeouts, and subtract a host-owned reserve for process collection and
  artifact flushing. This reserve is separate from Alysis Code's internal
  finalization reserve.
- The Terminal-Bench adapter must fail closed unless
  `managed_host_agent_timeout_sec` is supplied through normal agent kwargs. For
  current runners, release smoke commands should pair
  `--global-agent-timeout-sec N` with
  `--agent-kwarg managed_host_agent_timeout_sec=N`; the adapter must not apply
  `--global-timeout-multiplier` a second time.
- Terminal-Bench deadline diagnostics must stay sanitized. The
  `agent-logs/managed-host-deadline.json` artifact may include timeout source,
  effective timeout, elapsed pre-launch time, host reserve, computed Alysis Code
  deadline, requirement status, validation status, command timeout, host
  verifier status/source/count, and stable verifier command hashes only. It must
  not include raw verifier commands.
- Terminal-Bench must not synthesize a fake verifier. With no explicit
  `verify_cmd` or `verify_cmds`, the adapter must leave `ALYSIS_VERIFY_CMD`
  unset, clear managed-profile explicit verifier commands, pass no
  `--verify-cmd`, record host verifier unavailable diagnostics, and let
  Alysis Code resolve repo-native verification normally. Explicit vacuous or
  failure-masking verifiers must fail closed before setup.
- Terminal-Bench verifier input validation must reject simultaneous
  `verify_cmd`/`verify_cmds`, explicitly empty strings, empty sequence members,
  sets, and unordered iterables before setup, source copy, session creation, or
  model invocation. Ordered commands must preserve order and continue to share
  the managed-host deadline behavior.
- Host launch commands must pass arbitrary instructions after a positional
  `--` separator, preserving dash-leading, quoted, multiline, and
  shell-looking text as one instruction argument. Interactive `alysis chat`
  must continue to accept dash-leading prompt text without inheriting a
  managed-host wall-clock deadline.
- The invocation-wide `ExecutionDeadline` is monotonic and shared with child
  subagents; subagents must not receive a fresh duration that extends the
  parent run.
- The deadline finalization window must be preserved: one materialization
  directive, no new subagents or broad exploration, bounded finishing actions
  only, and a local truthful summary when there is not enough time for a final
  model call.
- Empty-response anomaly recovery in the finalization window must be bounded to
  one compact follow-up call; further blank responses or unsafe deadlines must
  terminate with a local truthful summary.
- Provider retry sleeps must remain deadline-aware. Transient provider
  throttling, stream truncation, incomplete chunked reads, peer-closed
  connections, and wrapped transport errors may retry only when the deadline
  reserve can absorb the delay; permanent auth and client errors must not retry.
- Streaming retries must not expose partial tool calls or duplicate visible
  deltas. Restart telemetry may record counts and normalized reasons, but not
  raw model content.
- Deadline termination is reported as `deadline_exhausted`, not as step-budget
  exhaustion.
- Crash diagnostics remain opt-in, append-only JSONL. They may coexist with
  `--no-log`, but must not persist prompts, tool arguments, command output,
  source contents, environment dumps, API keys, or authorization headers.
- Public compatibility remains stable for plugin and test authors:
  `run_agent(...) -> int`, `AgentSession.run_turn(...) -> int`,
  `create_session(...)`, existing tool/subagent result keys,
  `TurnExecutionState.as_payload()` legacy keys, lifecycle event fields, and
  the `_patchable()` monkeypatch façade through `alysis_code.agent_loop`.

## Unified Turn Path

- No pre-turn router call exists. Every text turn reaches the main model with
  the full per-mode agent surface; execution posture derives from the
  execution mode, and controller code must not add keyword lists or
  language-specific regular expressions to recover intent.
- Legacy routing settings remain accepted for compatibility but do not restore
  the retired pre-turn router. Verify that disabling them cannot change the
  unified execution path.

## Tool Recovery And Web Provenance

- Unknown-tool recovery payloads must include the requested name, current
  available names, nearest suggestions, and correction guidance without leaking
  hidden schemas, custom-tool source, environment data, or secrets.
- Only explicit schema-compatible compatibility aliases may execute
  automatically. Ambiguous aliases must remain non-executing guidance and still
  count toward repeated-call protection.
- `web_fetch` must require user, `web_search`, observed redirect, bounded
  same-origin/search-mediated provenance, or a configured trusted-domain match
  before retrieval. The trusted-domain list bypasses only the provenance gate.
- URLs discovered in trusted fetched pages, trusted local file reads, and
  registered tool or shell output must be recorded as bounded source-linked
  provenance, without persisting source content or granting broad same-domain
  access.
- Web provenance canonicalization may cover scheme/host case, default ports,
  fragments, and harmless trailing slashes, but must not bypass SSRF, embedded
  credential, redirect, or byte-cap checks.
- Deadline finalization must not initiate optional web-search recovery for a
  missing fetch provenance record.
- `shell_wait` must wait only on existing background processes, clamp wait time
  to the active deadline/finalization window, and preserve `shell_output`
  compatibility for immediate polling.

## Focused Regression Files

- `tests/test_completion_gate_controller.py`
- `tests/test_acceptance_contract.py`
- `tests/test_completion_certificate.py`
- `tests/test_acceptance_contract_integration.py`
- `tests/test_unified_turn_path.py`
- `tests/test_turn_contract_v2.py`
- `tests/test_one_shot_prompt_contract.py`
- `tests/test_verification_evidence.py`
- `tests/test_mutation_classification.py`
- `tests/test_execution_deadline.py`
- `tests/test_managed_host_deadline_contract.py`
- `tests/test_cli_ux.py`
- `tests/test_crash_diagnostics.py`
- `tests/test_runtime_backward_compatibility.py`
- `tests/test_agent_loop_one_shot_follow_through.py`
- `tests/test_agent_loop_forced_summary.py`
- `tests/test_agent_loop_step_budget_runtime.py`
- `tests/test_provider_limits.py`
- `tests/test_openai_compat.py`
- `tests/test_provider_telemetry.py`
- `tests/test_unknown_tool_recovery.py`
- `tests/test_web_fetch_provenance_recovery.py`
- `tests/test_web_fetch_tool.py`
- `tests/test_web_search_tool.py`
- `tests/test_shell_wait.py`
- `tests/test_subagents.py`
- `tests/test_config.py`
- `tests/test_session_store.py`

Run those targeted regressions before release work that touches one-shot
completion, verification evidence, mutation classification, deadlines,
diagnostics, subagents, or compatibility exports. Broader release validation
may add the full project suite according to the release branch policy.
