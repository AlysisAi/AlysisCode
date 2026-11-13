"""Container entry: accept credentials once over stdin, never Docker environment."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import uvicorn

from .app import Settings, create_app


def main() -> None:
    try:
        raw = sys.stdin.buffer.readline(16385)
        if len(raw) > 16384:
            raise ValueError("Bootstrap request too large")
        values = json.loads(raw)
        if not isinstance(values, dict):
            raise ValueError("Invalid bootstrap request")
        settings = Settings(database=Path("/var/lib/alysis/bots/bots.sqlite3"), **values)
    except (ValueError, TypeError):
        raise SystemExit("Invalid Bots bootstrap. Use scripts/bots-dev/dev.py.") from None
    uvicorn.run(create_app(settings), host="0.0.0.0", port=7080, access_log=False)


if __name__ == "__main__":
    main()
