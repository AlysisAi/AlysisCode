TOOL = {
    "manifest_version": 1,
    "name": "workspace_manifest",
    "description": "Inspect common manifest files at the workspace root.",
    "input_schema": {
        "type": "object",
        "properties": {
            "names": {
                "type": "array",
                "items": {"type": "string"},
            }
        },
        "required": [],
    },
    "timeout_s": 10,
    "enabled_in": ["interactive_chat", "one_shot"],
    "isolation": "subprocess",
    "capabilities": {
        "read_only": True,
        "destructive": False,
        "network_access": "none",
        "filesystem": {"read": "workspace", "write": "none"},
        "secret_refs": [],
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "workspace_root": {"type": "string"},
            "found": {"type": "array"},
        },
    },
}


def run(args):
    import os
    from pathlib import Path

    workspace_root = Path(os.environ["ALYSIS_WORKSPACE_ROOT"])
    names = args.get("names") or ["pyproject.toml", "package.json", "Cargo.toml"]
    found = []
    for name in names:
        path = workspace_root / str(name)
        if not path.is_file():
            continue
        found.append(
            {
                "name": str(name),
                "size_bytes": path.stat().st_size,
            }
        )
    return {
        "workspace_root": os.fspath(workspace_root),
        "found": found,
    }
