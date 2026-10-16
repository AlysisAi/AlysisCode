# Forge Host Protocol v2

`forge-host-v2.schema.json` is the single distributed wire grammar for Desktop,
CLI, VS Code, and the engine. The Python package mirrors it with strict Pydantic
models and a stateful `ProtocolLedger`. Hosts exchange opaque workspace/provider
references and NFC workspace-relative display paths; they never exchange a real
workspace root, worktree path, credential, or provider secret.

## Identity, operations, and replay

Handshake and planning use `runId: null`; `run.start` is admitted against that
pre-run identity and the engine allocates `runId` once. Every correlated command
has an envelope `operationId`. Ordinary events carry the operation that caused
them. An `approval.required` event instead mints a fresh fingerprint-bound
approval operation. The approval decision has its own command `operationId` and
separately echoes the issuance event's `approvalOperationId`, so correlation
cannot be mistaken for approval authority.

A fresh Phase-0 `ProtocolLedger` rejects an initial non-null `runId`. It accepts a
run identity only through one `bind_run` call after an admitted `run.start`; the
same or a different run cannot be rebound by advancing revision out of band.
Planning is an exact single-operation state machine. A `planner.question` must
carry the current `planner.input` operation, Phase 0 permits only one outstanding
required question, and its globally unique `questionId` is consumed exactly once
by a matching answer. That answer's request operation becomes current. Unrelated
input and `plan.ready` are blocked while a question is outstanding; `plan.ready`
must carry the latest current input operation. Planning closes permanently when
that plan is admitted, so all later planner input/questions reject. Unsolicited,
optional, duplicate, reused, or stale-operation planning messages have no authority.
`state.snapshot` remains a reserved wire shape and is rejected by the Phase-0
ledger until a complete authenticated state projection can be checked against the
ledger; a host must derive visible lifecycle state from admitted events for now.

## Bounded stdio transport profile

CLI, Electron, and VS Code use the same engine-owned JSONL transport. Each input
and output frame is exactly one canonical Host-v2 envelope followed by LF; blank,
unterminated, oversized, non-request, duplicate-key, unsafe, or otherwise
uncorrelatable frames close the connection. The first request must be
`handshake.request`. A successful handshake is emitted before any command is
accepted, and every later command must have negotiated its required capability.
The engine emits the correlated response first and then the exact ordered event
bytes returned by the authoritative service.

The transport revalidates response and event bytes, request/operation correlation,
object/byte equality, and the service session/generation before forwarding them.
It does not interpret planner, approval, execution, evidence, terminal, or replay
semantics. Correlated operational failures use the fixed `protocol.error` text;
uncorrelatable/fatal failures expose only a stable transport summary and suppress
the underlying exception chain so stderr or a process runner cannot reflect raw
input, credentials, or private paths.

Protocol run IDs (`run_*`) are opaque host authority. During migration the engine
persists a private `nativeRunRef` mapping to native CLI IDs such as `001-vulcan`;
that internal reference is never serialized, displayed as protocol authority, or
accepted back from a host.

`replayCursor` and `replayWindow` reserve the durable replay shape, but Phase 0
fails closed: negotiation rejects every replay cursor and `ProtocolLedger` rejects
`afterSequence > 0`. A future implementation must restore a complete authenticated
checkpoint (run operation, plan, approvals, evidence, revisions, and terminal
finality) before it may advertise replay. A cursor alone is never replay authority.
Accordingly, the Phase-0 stdio server does not advertise a `forge.replay`
capability. Byte-identical retries of an already accepted operation remain a core
idempotency guarantee of the live generation and do not imply durable cursor replay.

## Plan and verification bindings

The Host-v2 approval projection is explicitly versioned as
`alysis.forge-host.plan/v2`. `hostPlanFingerprint` binds every field in the
transmitted `planDefinition`: goal, summary, requirements, planning constraints,
asset references, intent, task order/kind/branch/dependencies, semantic text,
acceptance, and scope. It is deliberately **not** the legacy App/pinned-CLI plan
fingerprint and must not reuse an existing stored approval.

During migration, `nativeApprovalDefinition` is the new closed
`alysis.forge-native-approval/v2` projection, not raw `plan.json`, the App's
denylist approval view, or the pinned CLI fingerprint input. It is recursively
strict and losslessly projects to the complete Host plan; any unknown semantic or
runtime field is rejected. The adapter must first normalize an authoritative plan
into explicit branches and typed scopes, construct both projections atomically,
prove their deterministic equivalence, and request fresh approval. Legacy globs,
blank/generated branches, path-bearing intent, MCP scope, asset briefing,
scheduling, or embedded verification fields are capability-unavailable until a
lossless mapping is defined. `nativeApprovalFingerprint` names this new projection
only and must never be presented as an existing App/pinned-CLI approval fingerprint.

The verification contract binds both plan fingerprints and includes workspace
authority, task/integration scopes, provenance, trust and required/advisory policy,
commands, requirements, and acceptance/verification check definitions.
`run.start` and `run.terminal` must echo all three admitted plan/contract fingerprints:

- `hostPlanFingerprint`;
- `nativeApprovalFingerprint`;
- `verificationContractFingerprint`.

In addition, `executionAdmission` is an independently fingerprinted, engine-owned
record binding `workspaceRef`, `providerBindingRef`, the exact sequential/parallel
strategy and integration policy, `perManagedTaskMaxSteps`, and the whole-run
`runTimeoutMs`. It also binds `workspacePathPolicy`: NFC normalization, exact
`case_sensitive` or `case_insensitive` comparison, and the
`resolve_beneath_workspace` symlink policy. `plan_execute` approval binds this
fingerprint. `run.start` echoes the complete record and `run.terminal` echoes
`executionAdmissionFingerprint`, so a host cannot swap provider, workspace path
semantics, strategy, parallelism, task budget, or timeout after approval.
`perManagedTaskMaxSteps` is deliberately not a whole-run step limit.

Both opaque references resolve only inside the engine and are immutable for their
lifetime. In particular, `workspaceRef` pins the resolved repository root, base
branch and base Git HEAD; `providerBindingRef` pins provider/model/auth/account.
An adapter must reject drift or a rebound reference rather than updating the
referent behind the same ID. Those private binding records are intentionally not
serialized because they contain host paths and account authority.

Before emitting `plan.ready`, the engine adapter must preflight the bound workspace
using the declared comparison policy: normalize names to NFC, reject path aliases
under case-insensitive comparison, and verify every existing exact path's actual
file versus directory kind with `lstat`. For a new output path, its declared kind
remains authoritative: the adapter must resolve and verify the closest existing
ancestor directory beneath the workspace, reject case/NFC aliases, and never
substitute another kind when creating it. It resolves every existing ancestor
symlink and proves every resolved path remains beneath the pinned workspace root;
constraint roots follow their explicit engine existence policy. A directory
contains a same-kind directory at the same path and either kind only at a strict
descendant; it never authorizes a same-path file substitution. The adapter must
never guess host case behavior or treat the wire policy as a substitute for
filesystem containment. Case-distinct `Src` and `src` remain legal under a
case-sensitive binding and alias under a case-insensitive one.

The adapter also runs the equivalent of `git check-ref-format --branch` for every
authored task branch, confirms branch/ref-store collision behavior against the
actual pinned repository, and rejects casefold aliases when the bound policy is
case-insensitive. Branches are unique in the admitted plan and must not be
materialized before this preflight and the exact plan approval complete.

`PlanTask.taskKind` uses the engine's canonical, lossless lifecycle vocabulary:
`analysis_only`, `implementation`, `test_only`, `verification_only`,
`documentation_only`, or `unknown`. Planning constraints carry the native
structured target/forbidden/decoy/unrelated root records, including reason and
evidence, rather than flattening them into display strings.

Fingerprints use the App-compatible canonical JSON profile: recursive NFC for keys
and values, Unicode-code-point key order, compact UTF-8, finite JavaScript-safe
integral numbers (`-0` becomes `0`), rejection of unpaired surrogates, and recursive
exclusion of the four computed fingerprint keys. The language-neutral golden vectors
in `fixtures/fingerprint-vectors.json` are mandatory for TypeScript and Python.

## Approval and terminal authority

`PlanReady.approvalPolicy` is a fingerprinted stable subject/policy; it does not
embed an ephemeral approval ID, action fingerprint, or expiry. Each separately
sequenced `approval.required` issuance binds schema, authority operation,
session/generation/run plus issued revision, fresh approval ID, structured action,
structured scope fingerprint, UTC whole-second expiry, risk, and the exact
human-visible summary. A decision separately echoes the issuance operation,
fingerprints, and expiry. The ledger rejects unsolicited, mismatched, concurrently
pending, reissued, expired, or already consumed decisions. `allow_once` and `deny`
are consumed once; after deny or expiry the engine may issue a fresh approval for
the identical immutable plan/policy. An exact same-operation retry is only an
idempotent replay. Plan execution authority admits one distinct `run.start`.

Every Phase-0 plan requires a fresh `plan_execute` approval, including read-only
plans, because provider use and test execution are authority-bearing even when no
file is written. `required: false` is rejected until a future host-minted composite
preauthorization capability is specified. Approval presentation is deterministic:
the scope label and summary are fixed protocol constants, and risk is `high` when
the plan writes or executes in parallel and otherwise `medium`. A UI must render
the exact structured resources, resolved provider display identity, strategy, and
approved command argv; it must never rely on summary/risk prose as the sole
authority display.

Evidence and artifact IDs are immutable across events. Acceptance checks carry no
`commandIds`; executable proof belongs to verification checks. Phase 0 admits only
the canonical repo-native whole-suite pytest argv `pytest` or `pytest -q`. The
Python boundary re-runs the public CLI verification-command analyzer and requires
its exact tokenization/family/capability result; it never trusts sender-supplied
validity, capability, trust, provenance, or requiredness labels. Shell control,
wrappers, focused targets, plugins, arbitrary pytest options, and destructive or
vacuous commands are rejected. Other languages must run the mandatory cases in
`fixtures/verification-command-vectors.json`. The engine executor must pass this
argv directly with `shell=false`, closed stdin, a controlled environment and PATH,
a cwd resolved beneath the bound workspace, and the admitted timeout. `pytest`
must resolve to the engine-pinned trusted runtime; the workspace and other
host-writable directories must not precede it on PATH. Legacy string-shell
verification execution is not this protocol capability.

Every verification check command is executable `ARGV`, `VALID`, `ASSERTIVE`,
`TRUSTED`, and `REQUIRED`, and covers its required requirements. One verification
`EvidenceRecord` proves exactly one command and carries that command's own
`realExecution`, `exitCode`, and `timedOut` facts. A passed fact requires real
execution, exit zero, no timeout, and available immutable artifact evidence. The
terminal must resolve distinct records whose command-ID union exactly equals every
approved command for the check; one exit fact cannot claim multiple commands. A
succeeded terminal must
have non-empty exclusively passed acceptance and verification checks, cover every
approved check, resolve passed evidence emitted earlier, match evidence role/task/
requirement/command authority, have exit code zero, remain within `runTimeoutMs`,
cover every managed task with model-step usage within `perManagedTaskMaxSteps`, and
settle every requested worker successfully. Progress `taskModelSteps` is an
authoritative per-task model-call count: it is monotonic, cannot exceed admission,
and terminal `taskUsage` must equal the last observed value. A succeeded run must
have emitted an authoritative count for every managed task; failed or cancelled
runs must report zero for never-started tasks and exactly match every observed
task. Terminal aggregate
`usage.modelCalls` cannot be less than the sum of managed task model steps.
Worker allocation is also admission-bound: sequential execution uses one worker,
while parallel execution cannot request more than either `maxParallelWorkers` or
the number of managed tasks.
Before terminal admission, expired issuances are retired and every unexpired
pending approval must be resolved, so a terminal cannot strand invisible live
authority. Terminal is final: no command or event may follow it.

Phase 0 admits only the canonical pre-run `plan_execute` approval. Generic runtime
provider, process, network, filesystem, and Computer Use approval shapes are
reserved, but the ledger rejects them until a future engine-owned subject registry
can bind issuance to a concrete action lifecycle. Terminal admission nevertheless
fails if a restored/authenticated future checkpoint contains any live issuance.

Provider cost is never guessed. `usage.costKnown: false` requires
`totalCostMicros: null`; numeric zero is valid only when `costKnown: true` and the
provider explicitly reported a zero cost.

`timing.elapsedMs` is monotonic engine accounting and is the sole timeout authority.
Any terminal status whose elapsed time exceeds the admitted deadline must set
`timedOut: true`; a succeeded result cannot exceed the deadline at all.
`startedAt` and `finishedAt` are wall-clock presentation/audit timestamps; they must
be ordered but may diverge from elapsed time after host clock adjustments.

For the local journaled integration boundary, a successful `elapsedMs` is sampled at
`PUBLISH_READY`: after Git and native-state finalization and immediately before the
canonical terminal batch is built. The subsequent atomic `SEALED` journal write is
checked against the same monotonic deadline both before and after its fsync. If that
write crosses the deadline, the transaction stays reversible and the success batch is
discarded; its later sample is a gate only and cannot replace `elapsedMs` without
changing the event bytes that the journal binds. Thus reporting/encoding overhead may
make wall `finishedAt` later than `elapsedMs`, but it can never turn an over-deadline
run into success.

## Declarative versus stateful conformance

JSON Schema expresses field types, closed objects, discriminators, enums, bounds,
JavaScript-safe integer limits, relative-path syntax, and declarative success/
aggregate constraints. Standard Draft 2020-12 cannot express equality between two
fields, uniqueness by an object's semantic ID, NFC normalization, timestamp order,
worker arithmetic, graph/scope containment, historical reference resolution,
idempotency, approval consumption, or event finality.

Those rules are not claimed as Schema-only. They are split between typed validators
and `ProtocolLedger` and classified under `typedSemanticInvariants` and
`ledgerOnlyInvariants` in
`fixtures/conformance-manifest.json`. Every host implementation must run the same
schema/model cases, canonical vectors, and ordered ledger scenarios.

Verification argv semantics, NFC identity, cross-field fingerprints and workspace
comparison are typed semantic invariants, not claims about JSON Schema alone. The
checked Schema still mirrors every declarative restriction, and the corpus tests
validation in both directions. Invalid fixtures are single-fault cases: each
manifest entry names one repair, and applying only that repair must produce an
envelope accepted by both Schema and the Python typed boundary.

## Redaction boundary

Explicitly authored planner input/question and plan semantic prose is not
heuristically scanned, so ordinary text such as `Implement the /api route` is
valid. Every runtime-originated summary/reason/label is instead a redacted runtime
field: cancellation and terminal messages, progress/evidence/check summaries,
verification capability/rejection reasons, approval summary/policy summary, and
approval scope labels. These fields reject private Unix, home, Windows and UNC
paths even beside punctuation or quotes, plus bearer/API tokens, GitHub/Slack/AWS/
JWT credentials, query-string secrets, and private-key headers. The exact field
classification is language-neutral in `fixtures/conformance-manifest.json`.
Secret detection also applies to structured command fields, and every typed
path/root/directory field rejects absolute or traversal paths. Protocol errors use
a fixed safe summary for each code. Validation errors expose only stable codes and
generic summaries; raw invalid values and Pydantic diagnostics never cross the
wire.

Capability-specific preview and Computer Use event types remain a later slice.
Hosts must negotiate those capabilities as unavailable rather than inventing wire
shapes outside this contract.
