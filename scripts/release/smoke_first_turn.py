"""Run the installed CLI's first turn with a local model-response fixture."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-root", type=Path, required=True)
    args = parser.parse_args()
    for name in list(os.environ):
        if name.startswith(("ALYSIS_", "SYLLIPTOR_")):
            del os.environ[name]
    os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
    with tempfile.TemporaryDirectory(prefix="alysis-first-turn-") as directory:
        root = Path(directory)
        os.environ["ALYSIS_CONFIG_DIR"] = str(root / "config")
        os.environ["ALYSIS_DATA_DIR"] = str(root / "data")
        workspace = root / "workspace"
        workspace.mkdir()

        from typer.testing import CliRunner

        import alysis_code
        from alysis_code.cli import app
        from alysis_code.llm.openai_compat import LLMResponse, OpenAICompatClient

        assert Path(alysis_code.__file__).resolve().is_relative_to(args.expected_root.resolve())
        reply = "The installed agent completed its first turn."
        # Substitute only the provider boundary: CLI admission, session creation,
        # artifact contracts and the agent turn all run from the installed wheel.
        with (
            patch.object(
                OpenAICompatClient,
                "chat",
                return_value=LLMResponse(content=reply, tool_calls=[], raw={}),
            ) as model,
            patch("socket.socket.connect", side_effect=AssertionError("Unexpected network access")),
        ):
            result = CliRunner().invoke(
                app,
                [
                    "run",
                    "Reply with a short greeting.",
                    "--path",
                    str(workspace),
                    "--mode",
                    "auto",
                    "--model",
                    "gpt-4o-mini",
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--api-key",
                    "synthetic-first-turn-fixture",
                    "--max-steps",
                    "2",
                    "--no-log",
                ],
            )
        assert result.exit_code == 0, (result.output, repr(result.exception))
        assert model.called, "The CLI never reached the model"
        assert reply in result.output, result.output
        print(json.dumps({"version": alysis_code.__version__, "first_turn": "passed"}))


if __name__ == "__main__":
    main()
