# Provider tool-contract recovery

Computer Use and ephemeral-tool turns validate a complete provider response before
dispatching any call. A rejected batch also rejects otherwise valid siblings.
Native call IDs and generated public IDs are committed to the turn's seen-ID set
only after metadata projection and canonical response construction succeed.
Unknown names, missing/duplicate/reused IDs and invalid provider-native linkage
remain errors. Existing native-signature and reasoning checks remain in force.

A validation failure permits one fresh model response per turn, provided the
existing step, cancellation and execution-deadline guards permit it. The rejected
response is never appended to conversation history or provider replay state. An
ephemeral controller prompt asks for a fresh valid response from the last accepted
task state and directs the model to inspect an uncertain write with a read.
Recovery does not reset budgets or alter approval policy. A second provider
rejection or insufficient budget stops with a safe error; cancellation and
deadline exhaustion use their existing stop outcomes.

## Enforced operation admission

The rejected first implementation, `56132175`, instructed the model not to repeat
writes but admitted a freshly proposed duplicate with a new valid call ID. The
successful/uncertain-write fixtures originally regenerated reads and missed this
boundary. Check independently reproduced both duplicate-write cases; that commit
and the original evidence remain preserved.

The repaired controller keeps a turn-local ledger of dispatched operations,
independent of provider/public IDs and results. Its identity is the bound tool
name plus canonical arguments; engine-owned built-in coercions and declared
defaults are normalized. Only hashes remain in the ledger, and they are never
persisted. An exception or uncertain result does not remove a reservation.

After automatic recovery starts, every subsequent batch is checked for previously
dispatched or same-batch repeated operations before hooks, approvals, read caches
and handlers. A blocked batch gets safe failure results for all siblings and
dispatches none. Final admission rechecks effective input at the handler boundary,
including hook changes. Recovery disables parallel prelaunch and admits calls
serially; normal prelaunch records submitted operations for later comparisons.
The guard stays active through the rest of the recovery turn, including after a
read, and resets on a new user turn. Normal Full Access permits requested repeats.

Only bound `ToolDef.metadata.read_only == True` or the existing engine-owned
built-in read contract exempts repeats. Computer observations/waits declare their
read capability at their definitions. Model arguments, provider metadata,
descriptions and MCP annotations cannot grant this exemption. Unknown operation
effects are conservatively treated as non-read for repeat admission. Fresh safe
reads and distinct operations remain available; a write from a previously
rejected batch has no dispatch reservation and may execute for the first time.
No tool/site-specific write rule or new approval prompt is introduced.

`tool_contract_recovery_batch_blocked` records only the step and closed reason
`recovery_operation_repeat`; blocked results give read-first guidance without
disclosing arguments, IDs or prior results. These are admission results for a
valid provider batch, not a second provider-contract regeneration attempt.

The compatible `computer_use_provider_contract_failure` event records only
`step`, a closed `reason` code, `scope` (`computer_use` or `ephemeral_tools`),
`attempt` and whether `retry` is allowed. `missing_tool_call_id` is now distinct.
Unknown error attributes fall back to `invalid_tool_linkage`. Rejected names, IDs,
arguments, provider metadata and exception prose are excluded from this event,
the recovery prompt and visible failure messages. Existing sensitive turns remain
non-streaming until validation and projection finish.

## Controlled evidence

`tests/test_provider_tool_contract_recovery.py` exercises real agent turns with
scripted providers and synthetic tools, plus transactional validator cases:

- Mixed batches never dispatch a valid write sibling after any contract failure.
- Fresh valid responses recover both ephemeral-tool and Computer Use turns.
- Fresh-ID resubmissions of successful/uncertain writes are blocked, including
  after an intervening read and in mixed batches before hooks.
- Previously rejected writes execute once; repeated bound reads and distinct
  writes remain useful; normal Full Access repeats work in the next user turn.
- Effective-input coercion/defaults and hook rewriting cannot hide a repetition.
- Closed diagnostic causes survive; rejected-payload canaries and forged error
  attributes do not enter logs, UI captures, history or subsequent requests.
- Repeated rejection, step exhaustion, cancellation and deadline exhaustion are
  bounded and prevent further dispatch.

All 32 owner tests and Check's four independent probes pass locally. The original
inherited-environment nine-suite run had 183 passes and six failures, independently
reproduced there on untouched c5. Check later reported 200 candidate/182 baseline
passes with those unchanged assertions in its clean environment; the precise
original environment cause remains unknown. A fresh isolated-config, clean
environment final run here passes 258 cases, including argument handling and
selected parallel cancellation/prelaunch checks. Original partial logs remain
preserved; they are not silently relabeled as green.

The incident `chat_20261009t135352z_792fe6e4` did not persist its rejected payload.
These fixtures reproduce the general contract defects, not that exact payload or
an inferred Computer Use action. This evidence does not qualify a live provider,
installed app, desktop action or combined integration. GitBridge's frozen
corrected `fab7f3b19c98db8fe5388955a9cb90c63b658cc1` delta passes a textual
apply check against this candidate; it was never applied. Check must combine and
qualify the shared `core.py`. Preserve GitBridge's narrowed verification schemas,
bound-tool dispatch check and refresh filtering. Recovery only narrows admission;
it must not replace those restrictions or restore broader tools. Preserve Settings'
approval authority and deadlines when combining both deltas.
