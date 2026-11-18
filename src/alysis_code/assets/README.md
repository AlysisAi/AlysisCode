# Assets

This package holds the agent's local file viewer and files bundled with the engine.

## Contents

- `local_tools.py` lets the agent view local files (text, images, documents) within its
  workspace.
- `untrusted_content.py` wraps extracted file text so it is never read as instructions.
- `models.py` defines `AssetError`.
- `windows-git/`, `windows-mxc/`, `git-sign/` and `owl/` are bundled files that ship with
  the engine.

## Scope

File content can come from repository files or user uploads. Keep it bounded and
provenance-aware before exposing it to prompts.

## See Also

- [Security model](../../../docs/security_model.md)
