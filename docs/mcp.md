# MCP

Alysis Code can connect to external Model Context Protocol (MCP) servers and expose selected MCP tools, resources, roots, and prompts to supported runtimes.

MCP integration is intentionally explicit. User configuration defines connection details, project configuration may only narrow exposure, and readonly sessions do not expose MCP tools.

## Supported Transports

Alysis Code supports:

- stdio MCP servers
- synchronous Streamable HTTP MCP servers
- `tools/list` and `tools/call`
- optional `roots/list` support for the current workspace root
- optional listed resource access through Alysis Code-managed `mcp_resources_list` and `mcp_resource_read`
- optional manual prompt listing and retrieval through `alysis mcp prompts ...`
- static HTTP headers with `${ENV_VAR}` expansion
- OAuth for configured HTTP MCP servers through manual `alysis mcp auth ...` commands

Alysis Code does not currently implement GET SSE listening, resumability, polling, HTTP+SSE fallback, sampling, elicitation, resource subscriptions, resource templates, prompt subscriptions, or broad server-initiated request flows beyond the supported `roots/list` path.

## Runtime Exposure

MCP tools are exposed only in write-capable top-level runtimes:

- `interactive_chat`
- `one_shot`
- `forge_exec`

MCP tools are not exposed in:

- `readonly` sessions, including the `ask` persona and `/ask` turns
- `swarm_worker`
- `subagent`
- `conflict_auto_resolve`

Forge execution can apply task-level MCP scope on top of the session catalog. A Forge task without MCP scope does not receive live MCP tools or generic MCP resource tools.

## Config Files

User configuration lives at:

```text
~/.config/alysis/mcp.json
```

Project configuration lives at:

```text
<workspace_root>/.alysis/mcp.json
```

Both files use `schema_version: 1`. User config is authoritative for server connection details. Project config is lower-trust and can only narrow or disable an existing user-defined server.

## User Config Example

```json
{
  "schema_version": 1,
  "servers": {
    "docs": {
      "transport": "stdio",
      "command": "uvx",
      "args": ["example-docs-mcp"],
      "trust": "explicit",
      "enabled": true,
      "enabled_in": ["interactive_chat", "one_shot"],
      "allowed_tools": ["search", "read"],
      "roots_mode": "workspace",
      "resources_mode": "listed_read_only",
      "prompts_mode": "listed_get_only"
    },
    "tickets": {
      "transport": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${TICKETS_MCP_TOKEN}"
      },
      "trust": "explicit",
      "enabled_in": ["interactive_chat"]
    }
  }
}
```

For stdio servers:

- `command` is required
- `args` is optional
- `env` may define per-server environment variables, including `${ENV_VAR}` references

For HTTP servers:

- `url` is required
- HTTPS is required except for loopback development hosts such as `localhost` or `127.0.0.1`
- `headers` may define static request headers with `${ENV_VAR}` references
- `oauth` may be used instead of an `Authorization` header

The only supported `trust` value is `explicit`.

## Parallel Search MCP example

[Parallel Search MCP](https://docs.parallel.ai/integrations/mcp/search-mcp) provides
web search and page extraction without a Parallel API key. Its anonymous tier is
free for exploration and light use, with rate limits.

Merge the `parallel` server from
[`examples/parallel-search-mcp.json`](examples/parallel-search-mcp.json) into the
`servers` object in your **user** `~/.config/alysis/mcp.json`, retaining your other
servers. If you have no MCP config yet, you can use the example as the whole file:

```json
{
  "schema_version": 1,
  "servers": {
    "parallel": {
      "transport": "http",
      "url": "https://search.parallel.ai/mcp",
      "trust": "explicit",
      "enabled_in": ["interactive_chat", "one_shot"],
      "allowed_tools": ["web_search", "web_fetch"]
    }
  }
}
```

No `Authorization` header, OAuth login, or additional MCP package is needed.
Connection details belong in user config; project config cannot add this server.
This opt-in server leaves the built-in `web_search` and your model settings unchanged.

Check the connection from your workspace, then start a new session:

```bash
alysis-code mcp status
alysis-code chat
```

Status should show `parallel` with `loaded:2` tools. In a write-capable session,
ask: "Use `mcp__parallel__web_search` to find the official Python venv documentation,
then use `mcp__parallel__web_fetch` to read it and explain how to create a virtual
environment." These aliases distinguish the MCP tools from the built-in web tools.
The same server is available to `alysis-code run`; it is not exposed in readonly
sessions or subagents.

If status reports a startup warning, check network access to the endpoint and any
project overrides. Free-tier rate limits can also prevent calls; wait before
retrying according to the server response. Remove the `parallel` entry or set
`"enabled": false` to disable it.

## Project Overrides

Project config can narrow an existing user server:

```json
{
  "schema_version": 1,
  "servers": {
    "docs": {
      "enabled_in": ["interactive_chat"],
      "allowed_tools": ["search"],
      "resources_mode": "disabled"
    }
  }
}
```

Project config cannot introduce new servers, change transports, replace commands, change URLs, add headers, define OAuth, broaden runtime exposure, increase timeouts, or re-enable a server disabled by user config.

Allowed project override fields are:

- `enabled`
- `enabled_in`
- `allowed_tools`
- `denied_tools`
- `startup_timeout_s`
- `call_timeout_s`
- `roots_mode`
- `resources_mode`
- `prompts_mode`

For HTTP servers, `startup_timeout_s` applies to both the `initialize` request and
the HTTP acknowledgement of `notifications/initialized`. Each uses the configured
timeout; `call_timeout_s` governs subsequent requests. Stdio servers retain a short
post-notification observation window.

An MCP startup failure is reported as an MCP error. Chat exits cleanly instead of
retrying the same failing connection through the classic chat fallback.

## Tool Policy

Use `allowed_tools` and `denied_tools` to keep the MCP surface narrow:

```json
{
  "allowed_tools": ["search", "read"],
  "denied_tools": ["delete"]
}
```

If `allowed_tools` is non-empty, only those tool names may be exposed. `denied_tools` removes tools from the effective catalog. A tool cannot appear in both lists.

Alysis Code assigns local aliases for exposed MCP tools using the server id or configured `tool_prefix`, for example:

```text
mcp__docs__search
```

## Roots

Roots are disabled by default. Enable the current workspace root for a server with:

```json
{
  "roots_mode": "workspace"
}
```

When enabled in a supported runtime, Alysis Code answers `roots/list` with a single `file://` root for the bound workspace. Roots are not exposed in subagents, swarm workers, or conflict-resolution automation.

## Resources

Resources are disabled by default. Enable listed read-only resources with:

```json
{
  "resources_mode": "listed_read_only"
}
```

When enabled, Alysis Code lists resources for the session and exposes generic host tools:

- `mcp_resources_list`
- `mcp_resource_read`

Resource reads must target a `server_id` and `uri` that appeared in the listed session catalog. Arbitrary URI reads are blocked.

## Prompts

Prompts are disabled by default. Enable manual listed prompt access with:

```json
{
  "prompts_mode": "listed_get_only"
}
```

Prompt entries are not exposed as autonomous model tools. Users inspect them through:

```bash
alysis mcp prompts list
alysis mcp prompts get <server_id> <prompt_name>
alysis mcp status
```

Use `--refresh` on prompt list/get commands when you want to refresh the relevant prompt snapshot.

## HTTP OAuth

HTTP MCP servers may use an `oauth` block in user config:

```json
{
  "schema_version": 1,
  "servers": {
    "secure_docs": {
      "transport": "http",
      "url": "https://mcp.example.com/mcp",
      "oauth": {
        "client_id": "alysis",
        "scopes": ["docs.read"]
      },
      "trust": "explicit"
    }
  }
}
```

Supported OAuth fields are:

- `client_id`
- `redirect_host`
- `redirect_port`
- `scopes`
- `authorization_server_url`

Manage tokens with:

```bash
alysis mcp auth login <server_id>
alysis mcp auth status
alysis mcp auth logout <server_id>
```

OAuth tokens are stored in the user config scope, separate from `mcp.json`. Alysis Code does not write raw tokens into project config or session diagnostics.

OAuth currently requires an interactive authorization-code login. Device-code flow, dynamic client registration, token revocation, and browserless paste-back auth are not implemented.

## Environment And Secrets

Stdio servers receive a conservative baseline environment plus only the configured per-server `env` values. HTTP servers receive only configured request headers and OAuth bearer headers when applicable.

Resolved secret values are not written back into diagnostics, errors, or metadata. Missing environment variables fail clearly at config or runtime boundaries without printing the missing value.

## Diagnostics

Use:

```bash
alysis mcp status
```

`status` reports resolved server exposure, transport readiness, tool/resource/prompt stale state, and manual prompt availability without dumping secret values.

For a live IDE session, use the capability-negotiated `mcp.server.status`,
`mcp.server.enable`, `mcp.server.disable`, and `mcp.server.restart` protocol methods. These methods
are session- and server-bound and operate on the agent session's existing manager; they never create
a temporary manager. Status does not materialize a connection and reports a dead owned stdio
process as disconnected. One stable client lease per
server is shared by frozen tool, resource, and prompt bindings. Lifecycle mutations require
Workspace Trust and an idle session. Reconnect is atomic: the old connection remains usable if the
replacement fails, differs from any frozen catalog, or reports a list-change notification while the
catalog fence is running. A successful validated reconnect clears stale state inherited from the
closed transport. Disabling remains fail-closed even if it happens before first catalog
materialization. Disabling or closing a session shuts down every exact owned connection and leaves
existing bindings fail-closed. Server-controlled stderr and remote response diagnostics are never
echoed through the IDE protocol error payload.

For prompt-enabled servers:

```bash
alysis mcp prompts list --server <server_id>
alysis mcp prompts get <server_id> <prompt_name>
```

Targeted prompt commands only load the requested server. Unfiltered prompt listing may touch all prompt-enabled servers for the selected runtime.

## Security Notes

- Keep MCP servers narrow with `enabled_in`, `allowed_tools`, and `denied_tools`.
- Put connection details and secrets in user config, not project config.
- Treat MCP tool results, resources, and prompts as untrusted external text.
- Prefer readonly sessions for inspection when MCP access is not needed.
- Review project MCP overrides before trusting a workspace.
