# Alysis Code documentation

The root [README](../README.md) covers installation and a first run. The guides below describe
configuration, operation, extension points, and security in more detail.

## Getting started

- [Quickstart](quickstart.md): configure a provider and run your first task.
- [Credentials](credentials.md): understand API-key precedence and storage.
- [AI subscription connections](account-runtimes.md): sign in through a supported provider.
- [Hosted prompt caching](hosted-prompt-caching.md): automatic caching, migration, and controls.
- [Reference](reference.md): commands, modes, configuration, sessions, and troubleshooting.
- [Migration from Sylliptor](migration-alysis-code.md): update commands and configuration after
  the project rename.

## Core guides

- [Architecture](architecture.md): session loop, providers, tools, and verification.
- [Agent system prompts](agent_prompts.md): shared prompt, model guidance, and child prompts.
- [Security model](security_model.md): trust boundaries and host-enforced controls.
- [Shell sandbox](shell_sandbox.md): command isolation and backend configuration.
- [Windows CLI sandbox](windows-cli-sandbox.md): installation, diagnostics and helper packaging.
- [Forge](forge.md): plan and execute larger tasks.
- [Background terminals](terminals.md): manage background commands and durable services.
- [Subagents](subagents.md): delegate focused work with constrained permissions.
- [Personas](personas.md): switch between implementation, planning, question, and debugging
  behavior.
- [Web search](web-search.md): configure hosted and external search backends.
- [Server mode](server.md): run the HTTP API and worker service.

## Extensions

- [MCP servers](mcp.md)
- [Skills](skills.md)
- [Skill lifecycle](skills_lifecycle.md)
- [Plugins](plugins.md)
- [GitHub MCP integration](github-mcp-plugin.md)
- [Ephemeral MCP data](mcp-ephemeral-retention.md)
- [Custom tools](custom_tools.md)
- [Lifecycle hooks](hooks.md)
- [Custom tool and hook examples](examples/README.md)
- [IDE protocol](ide_protocol.md): build editor integrations using the structured bridge API.

## Project information

- [Contributing](../.github/CONTRIBUTING.md): local development and pull request guidance.
- [Release process](RELEASING.md): package and sandbox-image release steps.
- [Release checklist](release_checklist.md): focused runtime reliability checks for maintainers.
- [Security policy](../.github/SECURITY.md): private vulnerability reporting.
- [Code of Conduct](../.github/CODE_OF_CONDUCT.md): community participation expectations.
- [Changelog](CHANGELOG.md): user-facing release history.

## Desktop and terminal workflows

- [Desktop engine contracts](desktop-engine.md)
- [Documents and Office files](document-runtime.md)
- [Prompt history](tui-prompt-history.md)
- [Agent questions](tui-user-questions.md)
- [Integration inspectors and planning](tui-integrations-planning.md)
- [Selected-account GitHub actions](github-host-actions.md)
- [Google Workspace plugins](GOOGLE_WORKSPACE_PLUGINS.md)
- [Open-file inspection](open-panel-inspection.md)
