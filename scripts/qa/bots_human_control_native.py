"""Qualify the native human handoff on an isolated local Linux computer.

Run with PYTHONPATH=src in the engine's Python environment. Requires Docker,
the existing Bots QA Linux image and the app's installed Node dependencies.
Default is scripted; --live uses saved OpenCode Go GLM-5.3-flash/low only.
No Azure or paid fallback. Credentials travel through stdin, never saved files.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

import httpx


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--app-root", type=Path, default=root.parent / "Alysis-Code-App-bots-dev")
    parser.add_argument("--image", default="alysis-bots-memory-storage-20261021:qa")
    args = parser.parse_args()
    app = args.app_root.resolve()
    folder = root / ".dev/bots-control-20261023"
    folder.mkdir(parents=True, exist_ok=True)
    build = app / ".dev/bots-control-preview"
    build.mkdir(parents=True, exist_ok=True)
    (build / "vite.config.ts").write_text(
        "import config from '../../electron.vite.config';\n"
        "import { resolve } from 'node:path';\n"
        "const root=resolve(__dirname, 'out');\n"
        "export default {...config,\n"
        "main: {...config.main, build: {...config.main?.build, outDir: resolve(root,'main')}},\n"
        "preload: {...config.preload, build: {...config.preload?.build, outDir: resolve(root,'preload')}},\n"
        "renderer: {...config.renderer, build: {...config.renderer?.build, outDir: resolve(root,'renderer')}}};\n"
    )
    with (build / "build.log").open("w") as output:
        subprocess.run(
            [
                "node",
                "node_modules/electron-vite/bin/electron-vite.js",
                "build",
                "--config",
                str(build / "vite.config.ts"),
            ],
            cwd=app,
            stdout=output,
            stderr=subprocess.STDOUT,
            check=True,
        )
    token, key = secrets.token_urlsafe(40), ""
    payload = {"token": token}
    if args.live:
        from alysis_code.config import load_config, resolve_profile_api_key
        from alysis_code.profiles import get_profile

        cfg = load_config()
        profile = get_profile(cfg, "opencode")
        if not profile or profile.base_url != "https://opencode.ai/zen/go/v1":
            raise RuntimeError("Saved Go profile unavailable; no fallback")
        key = resolve_profile_api_key(cfg, profile.name).key
        if not key:
            raise RuntimeError("Saved Go credential unavailable")
        payload.update(api_key=key, base_url=profile.base_url, model="glm-5.3-flash")
    container = "alysis-bots-control-native-" + uuid4().hex[:12]
    volume, port = "alysis-bots-control-20261023-state", 7096
    cli = folder / "engine"
    cli.write_text(
        "#!/bin/sh\nexport PYTHONPATH="
        + shlex.quote(str(root / "src"))
        + "\nexec "
        + shlex.quote(sys.executable)
        + ' -m alysis_code "$@"\n'
    )
    cli.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix="alysis-handoff-") as temporary:
        cid = Path(temporary) / "container-id"
        command = [
            "docker",
            "run",
            "--rm",
            "--init",
            "-i",
            "--name",
            container,
            "--cidfile",
            str(cid),
            "--read-only",
            "--user",
            "1000:1000",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--security-opt",
            "seccomp=" + str(root / "scripts/cloud-dev/seccomp.json"),
            "--security-opt",
            "systempaths=unconfined",
            "--cpus",
            "2",
            "--memory",
            "2g",
            "--pids-limit",
            "512",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=512m,mode=1777",
            "-p",
            f"127.0.0.1:{port}:7080",
            "-v",
            volume + ":/var/lib/alysis",
            "-v",
            str(root / "src") + ":/latest-src:ro",
            "-v",
            str(root / "scripts/qa") + ":/qa:ro",
            "-e",
            "PYTHONPATH=/latest-src",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            args.image,
            "python",
            "/qa/bots_human_control.py",
            *(["--live"] if args.live else []),
        ]
        service = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        def log():
            with (folder / ("live-service.log" if args.live else "scripted-service.log")).open(
                "w"
            ) as output:
                for line in service.stdout:
                    clean = line.replace(token, "[redacted]")
                    if key:
                        clean = clean.replace(key, "[redacted]")
                    output.write(clean)
                    output.flush()

        logger = threading.Thread(target=log, daemon=True)
        logger.start()
        try:
            service.stdin.write(json.dumps(payload) + "\n")
            service.stdin.close()
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                headers={"Authorization": "Bearer " + token},
                timeout=5,
            ) as client:
                for _ in range(120):
                    if service.poll() is not None:
                        raise RuntimeError("QA service exited; inspect sanitized service log")
                    try:
                        client.get("/v1/status").raise_for_status()
                        break
                    except httpx.HTTPError:
                        time.sleep(0.5)
                else:
                    raise RuntimeError("QA service did not become ready")
                subprocess.run(
                    ["node", str(app / "scripts/qa/bots-human-control-native.mjs")],
                    input=json.dumps(
                        {
                            "token": token,
                            "url": f"http://127.0.0.1:{port}",
                            "engine_cli": str(cli),
                            "engine_source": str(root / "src"),
                            "live": args.live,
                            "container": container,
                        }
                    ),
                    text=True,
                    cwd=app,
                    check=True,
                )
        finally:
            if cid.exists():
                subprocess.run(
                    ["docker", "stop", "--timeout", "10", cid.read_text().strip()],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            service.wait(timeout=20)
            logger.join(timeout=2)
            print(
                json.dumps({"temporary_compute_stopped": container, "data_preserved": volume}),
                flush=True,
            )


if __name__ == "__main__":
    main()
