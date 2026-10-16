# Agent Runtime

This package contains the core runtime for interactive chat, one-shot runs, and
managed execution flows.

## Contents

- [session.py](session.py) builds session state from configuration, workspace context, and
  runtime kind.
- [tools_assembly.py](tools_assembly.py) builds the concrete tool surface for a session.
- [turn/core.py](turn/core.py) runs the turn loop and tool-call iteration.
- [turn_path.py](turn_path.py) defines execution posture and reply-language directives.
- [prompt_context.py](prompt_context.py) prepares workspace, convention, skill, plugin, and
  verification context.
- `task_evidence.py` retains workspace verification baselines across turns and resumes.
  It does not store or pin a conversation objective.
- `verification.py` and `verification_commands.py` support verification flows.

## Scope

Tool exposure is not implied by imports. It depends on runtime kind, execution
mode, workspace policy, configuration, and readiness checks.

Repository files, web content, MCP output, OCR text, custom tool output, and
model output should remain treated as untrusted unless a higher layer has
explicitly established otherwise.

## Development

Prompt shape, tool ordering, and result summaries can affect behavior. Keep
changes narrow and test the runtime path being changed.

## See Also

- [Architecture](../../../docs/architecture.md)
- [Security model](../../../docs/security_model.md)
- [Forge](../../../docs/forge.md)
