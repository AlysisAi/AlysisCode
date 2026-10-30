# Ephemeral MCP tool input and output

An MCP server can mark a tool's data as ephemeral in its `tools/list` response:

```json
{"_meta":{"ai.alysis/retention":{"version":1,"mode":"ephemeral"}}}
```

The manager validates the declaration before making the tool callable. An
unsupported explicit policy prevents the tool from being used; the engine does
not silently fall back to ordinary retention. The rule applies to any tool name.

## How the engine handles the data

The declaration becomes host-owned `ToolDef` metadata. Before usage capture,
history, tool-start events or hooks, the engine replaces private inputs, results
and errors with redacted versions for display and storage. The declared tool
still receives the original arguments unchanged.

The next provider request gets a temporary copy of the raw arguments and results,
including errors needed for recovery. The engine clears that copy in a `finally`
block, including after provider failure or cancellation. Provider-native signed
replay state stays in turn memory only when the provider protocol requires it;
it is removed from durable copies and retained request references. Mixed
private/public batches are checked against their shared replay envelope.

While private tools are exposed, responses do not stream partial text or reasoning
before redaction. Connecting a server alone does not enable this handling: it
starts when discovery exposes a private schema or an actual private call is about
to run, and lasts for the rest of the turn. Ordinary responses keep streaming up
to that point.

## Limits

Known private values are removed from later response channels and public-tool
arguments. The public-tool exfiltration guard remains active. Exact-value
redaction cannot detect every paraphrase of a private fact.

Images use the existing bounded, transient visual channel. Text still has the
MCP text limit, so servers should return complete bounded objects or pages.
Host-owned untrusted-text wrappers are preserved. A tool's retention policy does
not establish that its result satisfies the user's task.
