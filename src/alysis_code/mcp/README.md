# MCP

This package implements Alysis Code's MCP host integration: configuration,
transports, OAuth, catalog snapshots, and runtime filtering.

## Contents

- `config.py` and `models.py` load and validate user/project MCP settings.
- `transport_stdio.py` and `transport_http.py` implement the supported
  transports.
- `manager.py` starts servers, snapshots catalogs, and builds runtime bindings.
- `oauth*.py` and `token_store.py` handle HTTP OAuth state.
- `resources.py`, `prompts.py`, and `roots.py` normalize optional MCP surfaces.

## Scope

Project MCP configuration may narrow user configuration, but it must not add
new servers, credentials, or broader exposure. Server-provided text is treated
as untrusted and should stay wrapped before entering prompts or tool results.

The user-facing MCP contract is documented in `docs/mcp.md`; keep code changes
aligned with that page.

## Development

Transport, OAuth, and exposure changes need tests at both the config layer and
the manager/runtime layer.

## See Also

- [MCP](../../../docs/mcp.md)
- [Security model](../../../docs/security_model.md)
