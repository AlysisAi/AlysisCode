# Custom Tools

Custom tools let you add trusted Python tools without changing Alysis itself.

They are intended for local or team-owned helpers such as issue lookups, repository metadata checks, private API reads, and repeatable project utilities.

Custom tools are trusted Python code. Review them the same way you would review checked-in automation scripts.

## Tool Roots

Alysis discovers custom tools from:

- project tools: `<workspace>/.alysis/tools/*.py`
- user-global tools: `<user-config-dir>/tools/*.py`

The user config directory follows the same platform config location used for Alysis config. `ALYSIS_CONFIG_DIR` can redirect that root for tests or local overrides.

Discovery is conservative:

- scans only single-file `*.py` tools
- rejects symlinked tool files
- rejects paths that escape the owning workspace or config directory
- requires UTF-8 source
- reports invalid tools as issues instead of crashing startup

Precedence:

- global tools load first
- project tools override global tools with the same name
- invalid project tools do not shadow valid global tools
- names are matched case-insensitively
- built-in tool names and names starting with `mcp__` are reserved

## Authoring Contract

Each tool file must define:

1. a top-level literal `TOOL = {...}` dictionary
2. a top-level `run(args)` function

Minimal example:

```python
TOOL = {
    "manifest_version": 1,
    "name": "jira_lookup",
    "description": "Read one Jira issue by key.",
    "input_schema": {
        "type": "object",
        "properties": {"issue_key": {"type": "string"}},
        "required": ["issue_key"],
    },
    "timeout_s": 15,
    "required_env": ["JIRA_BASE_URL", "JIRA_TOKEN"],
    "enabled_in": ["interactive_chat", "one_shot"],
    "isolation": "subprocess",
    "capabilities": {
        "read_only": True,
        "destructive": False,
        "network_access": "restricted",
        "network_hosts": ["jira.example.com"],
        "filesystem": {"read": "none", "write": "none"},
        "process_spawn": "none",
        "secret_refs": ["JIRA_TOKEN"],
    },
}


def run(args):
    issue_key = args["issue_key"]
    return {"issue_key": issue_key, "status": "ok"}
```

Required manifest keys:

- `name`
- `description`
- `input_schema`

Optional manifest keys:

- `manifest_version`
- `timeout_s`
- `required_env`
- `enabled_in`
- `isolation`
- `capabilities`
- `output_schema`

The `TOOL` dictionary is parsed without importing the file. `run(args)` is loaded only when the tool is invoked.

## Manifest Rules

Important rules:

- `manifest_version` currently supports `1`
- `input_schema` must be an object-root JSON schema
- `output_schema`, when present, must also be an object-root JSON schema
- `isolation` must be `subprocess`
- `run(args)` is the only supported entrypoint
- package-directory tools are not supported
- tools must not import private Alysis host APIs
- `inprocess` execution is rejected

Tools with missing required environment variables remain visible in CLI inspection output, but they are not exposed to the model until the variables are available.

## Capabilities

Capabilities describe what the tool is expected to do and support approval, telemetry, metrics, and worker-side policy checks.

Supported fields:

- `read_only`: `true` or `false`
- `destructive`: `true` or `false`
- `network_access`: `unspecified`, `none`, `local`, `restricted`, or `unrestricted`
- `network_hosts`: exact host or IP strings, required with `network_access: "restricted"`
- `filesystem.read`: `unspecified`, `none`, `tool_dir`, `workspace`, or `unrestricted`
- `filesystem.write`: `unspecified`, `none`, `tool_dir`, `workspace`, or `unrestricted`
- `process_spawn`: `unspecified`, `none`, or `unrestricted`
- `secret_refs`: environment-variable-style secret names

Capability enforcement is a subprocess worker policy for ordinary Python operations. It is not a hard sandbox against malicious native-code or interpreter escape techniques.

## Trust Model

User-global tools are trusted by location.

Project tools are untrusted by default. Trust is persistent and keyed by:

- workspace root
- relative tool path
- file hash

This means editing, moving, or copying a project tool invalidates trust.

Trust and untrust project tools with:

```bash
alysis tool trust TOOL_NAME --path .
alysis tool untrust TOOL_NAME --path .
```

Trust commands apply only to project tools.

## Runtime Exposure

Custom tools can be exposed in:

- `interactive_chat`
- `one_shot`

They are not exposed in:

- `readonly`
- `subagent`

Older manifests may still list `forge_exec`, `swarm_worker`, or `conflict_auto_resolve`.
Those runtimes no longer exist, so they are skipped; a manifest that lists only retired
runtimes is reported as invalid instead of being exposed everywhere.

Mode behavior:

- `review`: custom tool calls require approval
- `auto`: trusted tools may run without per-call approval
- `fullaccess`: trusted tools may run without per-call approval

## Execution Model

Default execution is subprocess-based. For each invocation, Alysis:

- validates arguments against `input_schema`
- verifies the source hash still matches discovery metadata
- reloads project-tool trust state
- executes a sealed temporary copy in a worker process
- injects a small `ALYSIS_*` environment contract
- passes only declared environment variables and declared secret references
- applies selected capability checks in the worker
- captures stdout/stderr previews and writes full logs to local artifacts
- returns a bounded JSON-serializable result

Injected variables:

- `ALYSIS_WORKSPACE_ROOT`
- `ALYSIS_SESSION_ID`
- `ALYSIS_TOOL_PATH`
- `ALYSIS_TOOL_SCOPE`
- `ALYSIS_TOOL_NAME`

Unrelated host secrets are not inherited unless the manifest declares them.

## CLI

List the effective catalog:

```bash
alysis tool list --path .
```

Inspect one tool:

```bash
alysis tool info TOOL_NAME --path .
```

Manage project-tool trust:

```bash
alysis tool trust TOOL_NAME --path .
alysis tool untrust TOOL_NAME --path .
```

`alysis tools` shows built-in tools. Custom tools use the separate `alysis tool ...` command group.

## Security Notes

- Treat custom tools as trusted local code.
- Keep manifests narrow and explicit.
- Declare only the environment variables a tool genuinely needs.
- Prefer read-only capabilities where possible.
- Avoid unrestricted filesystem, network, or process-spawn access unless the tool requires it.
- Review project tools again after every edit, because edits invalidate trust.

## Example

See `docs/examples/custom_tools/workspace_manifest.py` for a copyable tool that inspects common manifest files from the bound workspace root.
