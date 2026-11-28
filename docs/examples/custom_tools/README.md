# Custom Tool Examples

This directory contains examples for Alysis Code custom tools. Files here are
documentation aids; they are not discovered from this directory at runtime.

## Files

- `workspace_manifest.py` shows the manifest shape for a workspace-local custom
  tool.

## Notes

Workspace custom tools are discovered from `<workspace>/.alysis/tools/*.py`.
After adapting an example into that location, review the source before trusting
it with `alysis tool trust`.

Project-tool trust is tied to the workspace, relative path, and file hash.

## See Also

- [Custom tools](../../custom_tools.md)
- [Security model](../../security_model.md)
