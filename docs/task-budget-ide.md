# Durable task budget: engine caller

An optional `computer_use_host.task_budget` binds a newly created protected IDE
session to the app's original task envelope and current short lease. Existing
sessions and calls without that binding keep their prior behavior. The engine
does not issue user approval, spend policy, durable usage, verifier progress or
renewal authority. Main must supply the exact negotiated binding from its real
budget runtime; stored session IDs are not a way to attach it again.

`features.task_budget` advertises the version-1 app contract. Admission is limited
to an actual `AgentSession`, the five builtin provider client classes, the fixed
protected Computer Use tool surface, and its actual selected route. Custom
clients/transports/auth adapters, subagents, active conversation compaction and
explicit remote cache sessions refuse. The existing protected-session policy
already disables those broader surfaces. A future change enabling them must
qualify their caller lifetime before relaxing this admission check.

The five request implementations are `openai_responses`, `openai_compat`,
`anthropic_messages`, `gemini_generate_content` and `gemini_interactions`. Their
HTTP constructors now use `provider_httpx_client`. Without an envelope it returns
the same ordinary HTTPX client. Under an envelope every request uses the existing
joined async cancellation transport, including token counts and normally
synchronous auxiliary calls. Main requests, internal parameter/previous-response
fallbacks, outer retries, and auxiliary HTTP therefore pass through the same
prepared request gate. The timeout context labels main/compaction operations;
other operations default to auxiliary. The labels do not grant or charge usage.

The gate hashes the exact built method, URL, ordered headers and body using
length-delimited immutable bytes, then asks main for one durable `attempt` ACK.
No payload, URL, credential or header is sent to the budget channel. The attempt
ID and monotonic sequence are consumed before the exchange. Only that ACK permits
the prepared request to enter HTTPX's `send`; no redirects, auth callbacks,
environment proxy discovery or connection retries can create an inner send.
URL credentials refuse before admission, and an explicit fixed no-op HTTPX auth
flow prevents implicit authorization-header substitution after request hashing.
Any provider-level retry builds another request and pays another permit.
HTTPX's transport documentation describes the distinction between a request and
configured connection retries: [HTTPX transports](https://www.python-httpx.org/advanced/transports/).

The response body is buffered by the joined transport. `settled` is issued only
after the actual request has completed or its transport has ended. An unknown
ACK, cancellation, expired original deadline or lost settlement never restores a
permit or refunds the app's count. A short lease may expire while an already
admitted request is in flight; settlement is historical and can occur under the
remaining original deadline. It does not authorize a new request.

The original absolute deadline is converted once to a monotonic deadline and
clamped with any existing engine deadline. Wall-clock high-water checks reject
regression. The cancellation transport checks the original deadline during an
in-flight socket wait. Existing finalization reserves and per-attempt timeout
clamps remain in effect and may stop earlier. Final summaries and auxiliary
requests have no budget exemption. Autonomous `max_steps=None` remains unchanged;
it is constrained by the approved deadline, durable attempts and read/mutation
limits rather than an invented engine step count.

Before a new request or host tool dispatch, an expired short lease requests
`renew` with no progress fields. Main runs its independent verifier and either
returns a new exact lease or refuses. Task, envelope, authority generation and
original deadline must remain identical. The current lease ID is included in
every subsequent host tool request; no action is replayed on renewal. Main still
needs a genuine progress issuer. This engine change does not supply one.

A mutation-count boundary may be reached while that short lease's time is still
valid. An exact host `computer_use_lease_renewal_required` refusal now requests
the same private renewal after retiring the pending tool exchange. The refused
action and its approval are never replayed. Only the actual, still-current
renewal ACK arms a one-use turn-loop recovery receipt; a matching model/tool
error code alone cannot do so. The turn then requires a successful fresh
`computer_state` before a final answer. A provider that ignores forced tool
choice and answers immediately ends with an explicit unverified failure. The
original task, envelope, generation, deadline and durable totals stay unchanged.

The IDE channel is exact session/job/operation/lease/nonce, one pending request,
at most 32,768 exchanges and 10,000 attempts. The single turn consumes its
authority once. Responses wake the worker only after their actual stdio result
has been flushed, so an immediate provider response cannot emit a settlement
ahead of the previous ACK. These events bypass replay, session history and
ordinary event listeners. Shutdown, cancellation, job end or refusal closes the
channel permanently. Subsequent `chat.send` cannot reuse its envelope.

Budget-bound session construction is offline before any job/channel exists.
The actual ModelRegistry created there permanently omits remote subscription
catalog discovery, including later `session.modelInfo` and route checks outside
the worker context. Active budget contexts likewise omit remote catalogs.
Subscription auth HTTP and refresh refuse during setup and execution. Existing
static capacity metadata can still be read; it supplies neither account identity
nor approval. The real selected credential scope is still checked independently.
A still-expired token may be described locally during setup, but cannot refresh
or authorize a budgeted inference request. Non-budgeted catalog behavior is
unchanged.

The current selected provider object, model, base URL, raw API-key configuration,
extra headers, route identity and auth adapter are pinned during the turn. The
existing provider/account checks remain in place. A subscription token needing
refresh refuses before refresh HTTP; an ordinary non-budgeted reauthorization
flow must restore it. No alternative provider/account is chosen. Gemini
Interactions still has its existing limited tool support and does not become a
new Computer Use route through this capability.

## Verification

Run the focused Python suites with the existing project interpreter:

```sh
python -m pytest -q tests/test_task_budget_transport.py tests/test_task_budget_channel.py tests/test_task_budget_setup.py
```

The tests use actual builtin adapter request construction and an intercepted
HTTPX transport to check every adapter, the Responses inner fallback, auxiliary
count/search, refusal/lost ACK, cancellation, clock/deadline, exact nonce binding,
post-flush release and immutable renewal identity. The actual subscription
AgentSession/session.create and later modelInfo fixture proves zero catalog or
refresh HTTP with both fresh and expired generated credentials. A separate
regression reproduced the former uncharged catalog GET at the actual HTTPX
boundary before the offline guard was added. A source audit must continue
to reject direct HTTP constructors added outside the shared provider helper.

The cross-process test additionally needs the actual app caller worktree and
Python runtime; it uses generated loopback data only:

```sh
ALYSIS_TASK_BUDGET_APP=/path/to/app \
ALYSIS_APP_RUNTIME_PYTHON=/path/to/python \
PYTHONPATH=src:/path/to/existing/site-packages \
node --experimental-strip-types --test tests/task-budget-app-composition.test.mjs
```

It composes actual Python IDE/AgentSession/provider HTTP, actual app
IdeRuntimeBridge/controller and actual encrypted task store/journal/supervisor.
Each loopback request checks that its durable charge already exists. Browser
observations and user approval/progress are explicit app fixtures; this is not
an installed browser/provider/UX qualification. The fixture covers 66 actual
autonomous calls, real 429 retry, lost ACK, cancellation, original deadline and
an independently checked short-lease renewal. It also covers the actual app
mutation-count refusal, one private renewal, no replay of the refused action,
and refusal when the provider skips the required fresh page read. Main opt-in
and its genuine progress issuer are separate app changes; this engine commit
does not change its version or packaging.
