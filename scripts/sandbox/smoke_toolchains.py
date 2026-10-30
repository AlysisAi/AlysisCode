"""Exercise the shipped tools without network access or a writable image filesystem."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    variant = sys.argv[1]
    if variant not in {"base", "dev", "server"} or os.getuid() != 1000:
        raise SystemExit("Expected a supported sandbox variant running as UID 1000")
    with tempfile.TemporaryDirectory(prefix="alysis-smoke-") as directory:
        root = Path(directory)
        env = dict(
            os.environ, GOCACHE=str(root / "go-cache"), npm_config_cache=str(root / "npm-cache")
        )

        def run(*command: str) -> None:
            subprocess.run(command, cwd=root, env=env, check=True, timeout=120)

        for command in (("git", "--version"), ("rg", "--version"), ("bwrap", "--version")):
            run(*command)
        run(sys.executable, "-m", "venv", str(root / "venv"))
        run(str(root / "venv/bin/python"), "-m", "pip", "--version")
        (root / "probe.c").write_text("int main(void) { return 0; }\n")
        run("cc", "probe.c", "-o", "c-probe")
        run(str(root / "c-probe"))

        if variant != "base":
            (root / "package.json").write_text(
                json.dumps(
                    {
                        "name": "sandbox-smoke",
                        "version": "1.0.0",
                        "scripts": {"install": "node-gyp rebuild --nodedir=/usr/local"},
                    }
                )
            )
            (root / "binding.gyp").write_text(
                json.dumps({"targets": [{"target_name": "probe", "sources": ["addon.c"]}]})
            )
            (root / "addon.c").write_text(
                "#include <node_api.h>\n"
                "static napi_value init(napi_env env, napi_value exports) {\n"
                "  napi_value answer; napi_create_int32(env, 42, &answer);\n"
                '  napi_set_named_property(env, exports, "answer", answer); return exports;\n'
                "}\nNAPI_MODULE(NODE_GYP_MODULE_NAME, init)\n"
            )
            run("npm", "install", "--offline", "--no-audit", "--no-fund")
            run(
                "node",
                "-e",
                "require('node:assert/strict').equal(require('./build/Release/probe.node').answer,42)",
            )
            (root / "probe.go").write_text("package main\nfunc main() {}\n")
            run("go", "build", "-o", "go-probe", "probe.go")
            run(str(root / "go-probe"))
            (root / "probe.rs").write_text("fn main() { assert_eq!(3 * 7, 21); }\n")
            run("rustc", "probe.rs", "-o", "rust-probe")
            run(str(root / "rust-probe"))
        if variant == "server":
            run("alysis", "--version")
            run(
                sys.executable,
                "-c",
                "from alysis_code.forge_host_protocol import load_schema; "
                "assert load_schema()['$defs']['protocolVersion']['const'] == '2.0'",
            )
    print(f"{variant}: offline runtime and native toolchain smoke passed")


if __name__ == "__main__":
    main()
