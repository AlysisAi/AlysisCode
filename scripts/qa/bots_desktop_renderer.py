"""Opt-in real React/HTTP/Linux/GLM integration, without native Mac UI access.

Reuses the private background-work service (12-call ceiling). The actual desktop
renderer and production HTTP client run in headless Chromium, with an in-memory
bridge in place of Electron IPC. No synthetic API replies or model outputs.
"""

import argparse
import json
import subprocess
from pathlib import Path
from uuid import uuid4


def exercise(app, client, bot, other, private, name):
    expected = "Café — Αθήνα — 東京 " + uuid4().hex[:8].upper()
    message = (
        "This is a synthetic UI and cancellation check. First propose this exact task with "
        "offline computer permission and wait for approval. After approval, first continue in "
        "background. Open a native xmessage window saying 'Alysis local study' with computer_launch. "
        "Use computer_run to create view-check.txt containing exactly "
        + json.dumps(expected, ensure_ascii=False)
        + " with no trailing newline. Save the real workspace file to the library using "
        "computer_save_file. Only after saving, use a separate computer_run command to create "
        "started.txt, wait 25 seconds, and then create late.txt (timeout 30 seconds). "
        "The user will stop that delayed command through the app. Do not repeat or restart "
        "anything after Stop. Keep the original saved file."
    )
    print(json.dumps({"ready": True, "container": name, "evidence": str(private)}), flush=True)
    config = {
        "execute_live": True,
        "url": str(client.base_url),
        "token": client.headers["Authorization"].removeprefix("Bearer "),
        "bot": bot,
        "other": other,
        "expected": expected,
        "message": message,
        "evidence": str(private),
    }
    subprocess.run(
        ["node", str(app / "scripts/qa/bots-linux-service-study.mjs")],
        input=json.dumps(config).encode(),
        cwd=app,
        check=True,
        timeout=600,
    )


if __name__ == "__main__":
    from functools import partial

    from bots_computer_view import host

    from alysis_code.config import load_config, resolve_profile_api_key
    from alysis_code.profiles import get_profile

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-live", action="store_true", required=True)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--port", type=int, default=7091)
    parser.add_argument("--image", default="bots-linux-night-20261019:actions")
    parser.add_argument("--existing-network", required=True)
    args = parser.parse_args()
    app = args.app.resolve()
    config = load_config()
    profile = get_profile(config, "zai-coding-plan")
    key = resolve_profile_api_key(config, "zai-coding-plan").key
    if profile is None or not key or profile.protocol != "openai_compat":
        raise RuntimeError("Saved Coding Plan unavailable; no fallback.")
    host(
        app,
        None,
        study_script=Path(__file__).with_name("bots_desktop_background.py"),
        bootstrap={"key": key, "base_url": profile.base_url, "single_scenario": True},
        lazy_browser=True,
        exercise=partial(exercise, app),
        port=args.port,
        image=args.image,
        name_prefix="bots-linux-night-20261019-renderer-",
        existing_network=args.existing_network,
    )
