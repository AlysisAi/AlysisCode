# Connector result references and completion evidence

MCP results remain fenced as untrusted data. URL provenance extraction decodes the
host-owned text envelope and bounded nested JSON before registering references.
It preserves complete structured URL values, including query strings and legal
trailing punctuation. It does not execute result text, follow arbitrary references,
change account/target bindings, or grant write authority. URL credential rejection,
fetch provenance checks, DNS/address checks and redirect validation still apply.
Backslashes are rejected in structured URLs rather than silently repaired.

Decoding is bounded by node count, depth, text length, URL length and result count.
Over-deep data is not scanned through a recursion-error fallback. Malformed or
truncated data does not gain permission to repair an escaped URL by guessing.

## API facts versus the requested result

A successful API operation establishes only the returned fact. A repository or
source-file link, resource creation, accepted job or configuration response does
not prove that a requested published deliverable works. All workflow prompt
profiles carry the same evidence guidance. The agent should select suitable
authorized read tools, inspect status/content, compare with the user's original
request and report incomplete or unavailable verification honestly.

Ordinary agent turns track exact links in registered MCP host wrappers. If a final
candidate contains such a link without a subsequent successful, nonempty page
fetch in this turn, one bounded continuation asks the model to verify the result
or qualify its report. It does not execute a tool itself, retry a write, change
permissions, create a task-specific workflow or classify user wording by regex.
If the link remains unchecked, the final reply retains a visible unverified
notice and the durable task outcome cannot be verified success. A fresh connector
response invalidates an older fetch of the same reference; another turn starts
with fresh completion observations. Stored URL provenance remains available for
ordinary authorized reads across turns.

The automatic continuation has a host-enforced capability boundary: it exposes
only registered built-in `web_fetch` and `web_search` tools and admits only those
exact handlers at dispatch. Other proposals receive a structured
`connector_verification_read_only` error before hooks, approvals or handlers.
Parallel subagent prelaunch is disabled in this phase, and the shared invocation
entry point also checks the bound tool identity. Connector read-only annotations,
custom tools with web-like names, fresh call IDs and mixed batches cannot grant
authority. The user's permission mode is unchanged; an explicit subsequent user
turn starts with the normal tool set and authorization policy. Reads unavailable
through these web tools remain unverified; this phase cannot run arbitrary shell,
connector, delegation or native tools to obtain additional evidence.

Independent review rejected the original continuation because it retained full
dispatch authority and repeated a synthetic completed write. The corrected
boundary is enforced in code, with negative production-loop coverage, rather
than relying on the verification prompt to stop a mutation.

`fetched_links` means only a later 2xx response with nonempty content was observed.
It is not a semantic completion certificate: a login page or wrong artifact can
return 200. `proves_requested_outcome` remains false; matching content to the goal
is still the agent's responsibility. Other verification methods, including native
browser observations, are not mechanically certified by this narrow link check.
The existing Computer Use completion protocol remains separate and unchanged.

## Controlled verification and remaining recovery work

The regression fixtures pass synthetic nested connector responses through real
provenance and fetch gates, and use scripted model replies through the real agent
loop. They cover source links, configuration201, no verification, failed404,
successful content fetch, stale/unrelated/empty checks, credentials and bounded
decoding. Further production-loop cases include a fresh-ID duplicate mutation,
mixed read/write batches for GitHub v1/v2, shell/edit/delegation/custom dispatch,
parallel subagent admission before versus during the phase, unavailable checks,
and restored normal execution on a subsequent user turn.
No fixture result establishes live GitHub, provider, installed-app or
published-site acceptance.

The separate provider `invalid_tool_linkage` stop remains outside this repair.
The shared response canonicalizer also runs for negotiated ephemeral tools. It
validates tool names, unique/non-reused protocol IDs and provider replay metadata
before dispatch. The incident recorded only a generic validation failure, not the
rejected call. Core must own any bounded regeneration/recovery change and safe
structured diagnostic reason reporting. Preserve rejection, do not infer the
missing payload, and never dispatch or automatically replay an invalid/uncertain
write. This change does not modify that validator or the unified approval policy.
