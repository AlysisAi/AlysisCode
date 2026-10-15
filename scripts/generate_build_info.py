#!/usr/bin/env python3
"""Stamp the current commit into ``src/alysis_code/_build_info.py``.

Run this immediately before building a wheel or an sdist::

    python3 scripts/generate_build_info.py
    python3 -m build            # or: uv build / hatch build

Why a script rather than a build-backend hook: the project pins its backend
(``hatchling==1.31.0``) and its build requirements exactly, and the wheel that
carries this stamp is the artifact whose provenance is in question. A generator
that runs inside the build environment would have to be correct in an
environment that is, by construction, the one nobody has inspected. A script
the release job invokes is inspectable, runnable in a bare interpreter, and
fails loudly in the job log rather than silently inside a backend.

The committed ``_build_info.py`` is a dev default reporting an unidentifiable
build. This command overwrites the output file; save and restore its existing
contents when stamping a working checkout. ``scripts/build_benchmark_wheel.sh``
handles restoration automatically. The dirty check deliberately ignores this
one file, so stamping a clean tree still reports ``dirty: no``.

The stamp also records the commit's subject line and the SHA ``origin/main``
resolved to at build time, the latter after a ``git fetch origin``. An offline
build records ``origin/main`` as ``unavailable`` and still stamps; pass
``--no-fetch`` to skip the network probe deliberately. What an unresolved
remote costs is the ability to *claim* to be latest main, which the benchmark
adapter's provenance guard checks.

Exit codes: ``0`` stamped, ``1`` refused (with ``--require-clean``, when the
tree is dirty or has no commit).
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_build_identity():
    """Import the checkout's stdlib-only build modules with package-relative imports.

    ``alysis_code.__init__`` only declares the version; neither it nor the build
    identity and command decoding modules need installed third-party packages.
    """
    sys.path.insert(0, str(_REPO_ROOT / "src"))
    return importlib.import_module("alysis_code.build_identity")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        default=str(_REPO_ROOT),
        help="Repository to interrogate (default: this checkout).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Where to write _build_info.py (default: next to build_identity.py).",
    )
    parser.add_argument(
        "--require-clean",
        action="store_true",
        help="Exit 1 instead of stamping when the tree is dirty or has no commit.",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Report what would be stamped without writing the file.",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help=(
            "Skip the `git fetch origin` before resolving origin/main. The "
            "resolved SHA is then stamped as 'unavailable', so the build can "
            "never satisfy a latest-main provenance check."
        ),
    )
    args = parser.parse_args(argv)

    build_identity = _load_build_identity()
    info = build_identity.generate_build_info(repo_root=args.repo_root, fetch=not args.no_fetch)

    if args.require_clean and not info.is_clean:
        print(f"refusing to stamp: {info.describe()}", file=sys.stderr)
        return 1

    if args.print_only:
        print(info.describe())
        return 0

    output = Path(args.output) if args.output else build_identity.default_build_info_path()
    written = build_identity.write_build_info(info, output)
    print(f"{written}: {info.describe()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
