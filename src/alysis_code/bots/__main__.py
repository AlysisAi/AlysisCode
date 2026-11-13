from __future__ import annotations

import os

import uvicorn

from .app import Settings, create_app


def main() -> None:
    # Credentials come from process environment; never write a config or print
    # settings/tracebacks that could include request headers or model arguments.
    try:
        settings = Settings.from_environment()
    except (ValueError, KeyError):
        raise SystemExit("Invalid Bots environment. Check the development setup guide.") from None
    app = create_app(settings)
    uvicorn.run(
        app, host=os.environ.get("ALYSIS_BOTS_HOST", "127.0.0.1"), port=7080, access_log=False
    )


if __name__ == "__main__":
    main()
