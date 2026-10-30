# Official GitHub MCP desktop plugin

The installed `alysis.github` plugin contributes the HTTP server
`alysis.github/github` at `https://api.githubcopilot.com/mcp/` with
`host_managed = true`. The endpoint and its behavior are documented by
[GitHub](https://github.com/github/github-mcp-server/blob/85598ba6e1256f7ebf4867b95d63b833c4549264/docs/remote-server.md).
Alysis supplies the integration descriptor; GitHub supplies the remote server.
The bundled descriptor's SHA-256 is the content pin, while `upstream_commit` is
source provenance. Neither is a claim that GitHub's rolling hosted deployment is
immutable or that its full tool surface is enabled.

## Installation and trust

Use existing JSON methods `ext.search`, `ext.list`, `ext.info`, `ext.install`,
`ext.enable`, `ext.disable`, and `ext.uninstall`. For desktop installation:

1. Send `ext.install` with `workspace`, `workspace_trusted: true`,
   `source: "alysis.github"`, and `project: false`.
2. Display `trust_request` and `action.required_approval` from
   `action.kind: "requires_extension_trust_review"`.
3. After the user accepts, replay the same request with `yes: true` and
   `trust_approval` equal to that required approval. The engine rechecks the
   source, scope, content hash and approval fingerprint before installing.

The package contains only its descriptor. Its host-managed MCP component is
recorded in plugin state; no Python MCP server configuration or OAuth tokens are
created. Plugin records include `source_kind: "bundled"`, `package_sha256`,
`manifest_sha256`, `upstream_commit` and
`source_url: "bundled://alysis.github/sha256/<package_sha256>"`.
The legacy `commit` property is `sha256:<package_sha256>`; label it as a package
content pin rather than a Git commit in user-facing UI.

## Session negotiation v1

The CLI reports `features["github-mcp-plugin-v1"] = true`. The live IDE handshake
reports `features.github_mcp_plugin.version = 1`, with plugin/server IDs, the
action name, session parameter name and catalog limits.

The host authenticates the selected account, initializes the official server, calls
`tools/list`, applies its fixed tool allowlist and removes repository target
fields from each exposed schema. The host owns the read/write policy; remote tool
annotations must not decide approvals. Then include:

```json
{
  "workspace_trusted": true,
  "host_capabilities": {"protocol_version": "1", "actions": ["mcp.github.call"]},
  "host_mcp": {
    "protocol_version": 1,
    "plugin_id": "alysis.github",
    "server_id": "alysis.github/github",
    "tools": [{
      "name": "list_pull_requests",
      "description": "List pull requests for the selected repository.",
      "inputSchema": {"type": "object", "properties": {}},
      "readOnly": true
    }]
  }
}
```

Catalog limits are 32 tools, 120,000 total JSON bytes and 24,000 bytes per schema.
Each tool has exactly the fields shown. Model aliases are
`mcp__github__<official_tool_name>`. The engine validates and hashes the complete
catalog plus installed plugin provenance into the existing capability fingerprint.
`session.create.host_mcp` in the response contains `protocol_version`,
`plugin_id`, `server_id`, `manifest_sha256`, `package_sha256`, `source_kind`,
`scope` and accepted `tool_names`. Readonly sessions exclude write tools.
Sensitive Computer Use and untrusted sessions receive no MCP catalog.

A plugin turned off, removed or changed after the host read its state fails
`session.create` by default. A host that sees the `host_plugin_drop` feature
(version 1) can send `drop_inactive_host_plugins: true` instead: the session then
starts without that plugin's catalog and its host action (`mcp.github.call` and
the discovery actions; `mcp.google.call` once no Google catalog is left), and the
response lists each one in `host_plugins_dropped` as `plugin_id`, `code` and
`message`. A malformed catalog still fails the session.

## On-demand discovery v2

The existing `github_mcp_plugin.version: 1` feature and all v1 behavior remain.
New engines advertise `protocol_versions: [1, 2]` and a `discovery` feature with
version 1, actions `mcp.github.discover` and `mcp.github.describe`, and bounded
page/load limits. Hosts must detect this support before negotiating v2.
Google's catalog and call contract are unchanged.

V2 negotiates discover, describe and call together. Its `host_mcp` parameter is
exactly this bootstrap, with no provider catalog dump:

```json
{
  "protocol_version": 2,
  "plugin_id": "alysis.github",
  "server_id": "alysis.github/github",
  "discovery": {"version": 1}
}
```

The session receipt contains the same trusted plugin snapshot, version 2,
`discovery: {version: 1}` and empty `tool_names`. The agent receives three
generic tools, `github_discover`, `github_describe` and `github_call`, and chooses
how to compose them with its other tools. No request phrase or site workflow is
encoded in the engine.

Discover accepts optional `query` (256 characters), opaque `cursor` (256
printable ASCII characters), and `limit` (1–20, default 10). It returns
`catalog_id`, up to the requested number of descriptors, and optional
`next_cursor`. Each descriptor has exactly `name`, `description`, `readOnly`,
`scope`, and `schema_hash`. Scope is account, organization, repository or
workspace; schema_hash is a host-issued immutable 64-character lowercase hex
identity. Descriptions are at most 2,000 characters. Empty search results are
valid. The host selects policy; provider annotations grant no authority.

Describe accepts `catalog_id`, 1–4 unique discovered `names` and an explicit
`target`: account or workspace require only `scope`; organization also requires
`organization`; repository also requires `owner` and `repo`. Account always
means the host-selected account. The host verifies access, confirms target
changes when required, and binds/injects provider target fields. Descriptor scope
is descriptive; compatible target resolution is host-owned, including account
creation in an organization and repository operations in a workspace. The result
contains `catalog_id`, opaque `target_id` and identical descriptors with their
`inputSchema`. Large definitions must be loaded in smaller batches, never
silently truncated.

Call accepts exactly `catalog_id`, `target_id`, `schema_hash`, `name` and
`arguments`. The engine requires the exact previously loaded definition and
target in the owning broker job, validates its arguments, and independently
enforces readonly/persona and host-managed write approval. Provider root target,
account, credential, header and endpoint overrides are forbidden; schema-declared
nested operation data are allowed. The host owns transport, exact target/account
binding and exact approval. V1 calls cannot bypass a v2 session's references.

State is never persisted: at most 256 discovered summaries and 32 loaded
name/target pairs per job. Changed catalogs invalidate old definitions; changed
descriptor/schema identities or target rebinding fail closed. Plugin manifest
trust, broker fences, cancellation and deadlines remain active before dispatch
and after result delivery. Timed-out v2 writes are reported as uncertain and
non-retryable; inspect the outcome before another write.

Definitions use bounded object schemas (24,000 bytes each, 64 properties and
depth 12) and the 64 KiB result envelope. Supported local checks include declared
and required properties, bounded additionalProperties, enum/const, primitive
types, arrays, anyOf/oneOf/allOf and numeric/string/item bounds. References, regex
assertions and unsupported conditional schemas fail loading. The host additionally
validates against its stored provider schema. Discovery metadata, schemas and
results are wrapped as complete marked untrusted text; the existing generic
output offloader may provide an explicit artifact locator for large results.
This is distinct from silently truncating a schema or claiming model acceptance.

## Calls, approval and revocation

The existing `host_action_requested` / `host.action.respond` channel carries
`action: "mcp.github.call"` and arguments `{"name": "...", "arguments": {...}}`.
The host injects its selected `owner` and `repo` into the official `tools/call`
request, checks the action against its discovery snapshot and validates all
arguments. Model-supplied `owner`, `repo` or `repository` fields are forbidden.
No endpoint, credential or connection selector appears in model arguments.

The host obtains its exact existing operation/account/repository/tool/arguments
approval before writes. The engine requires host approval authority for write
tools and blocks them in readonly or restricted persona contexts. Every remote
write (`mcp.github.call` and `mcp.google.call` writes, `github.pr.create`,
`github.git.push`) asks a person, one call at a time, also in Full Access: the
approval is marked `always_ask_person` and `mandatory_explicit_approval` (no AI
reviewer, saved allow rule or allow-for-session), and the host executes it only with
a person-approved receipt for that exact preparation. Read tools
remain usable in readonly sessions. Cancellation, job identity, workspace fence,
capability fingerprint, deadlines and duplicate-response checks reuse the existing
broker. The host revalidates before network dispatch and after every async boundary.

Success carries a bounded standard MCP result with `content` (text items only),
optional boolean `isError`, and optional object `structuredContent`. Other content
types fail closed. Existing limits remain 8 KiB arguments and 64 KiB results;
host errors are at most 512 characters. The engine wraps result text and structured
content as untrusted MCP data before model inclusion.

Disable, uninstall, disconnect, account/workspace changes and cancellation must
revoke the host's generation and stop affected operations, including warmed sessions.
Engine dispatch rechecks installed content and activation both before calling
the host and before returning results. Existing local Git tools remain native;
repository/PR API built-ins cannot become a desktop fallback when MCP is absent.
