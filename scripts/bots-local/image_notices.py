"""Third-party notices and source references for everything installed in an owl computer image.

Runs inside the image at build time (standard library only) and writes:

* ``/usr/share/alysis/THIRD-PARTY-NOTICES.txt``: every Debian package with its version and
  the full text of its copyright file (packages sharing a text are listed together), the
  common license texts those files refer to, and every Python package with its license
  metadata and license files.
* ``/usr/share/alysis/sources.json``: where the corresponding source of each package is
  permanently available (Debian's snapshot archive, PyPI).

Debian policy requires ``/usr/share/doc/<package>/copyright`` in every package, so a
package without one stops the build rather than shipping without its notice.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

FIRST_PARTY = {"alysis-code"}
LICENSE_FILE = re.compile(r"^(licen[cs]e|copying|notice|authors|copyright)", re.IGNORECASE)
DPKG_FORMAT = "${Package}\t${Version}\t${source:Package}\t${source:Version}\t${Architecture}\n"


def debian_packages(rows: list[str]) -> list[dict]:
    packages = []
    for row in rows:
        if not row.strip():
            continue
        name, version, source, source_version, architecture = row.split("\t")
        packages.append(
            {
                "name": name,
                "version": version,
                "source": source or name,
                "source_version": source_version or version,
                "architecture": architecture,
            }
        )
    return sorted(packages, key=lambda item: item["name"])


def dep5_licenses(text: str) -> list[str]:
    """Short license names from a machine-readable (DEP-5) copyright file, if it is one."""
    names = []
    for line in text.splitlines():
        match = re.match(r"^License:\s*(\S.*)$", line)
        if match and match.group(1).strip() not in names:
            names.append(match.group(1).strip())
    return names


def copyright_file(doc_root: Path, package: str) -> Path | None:
    for name in (package, package.split(":")[0]):
        path = doc_root / name / "copyright"
        if path.is_file():
            return path
    return None


def python_distributions(skip: set[str]) -> list[dict]:
    found = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get("Name") or ""
        if not name or name.lower() in skip or name.lower() in found:
            continue
        classifiers = [
            value.split("::")[-1].strip()
            for value in dist.metadata.get_all("Classifier") or []
            if value.startswith("License ::")
        ]
        declared = dist.metadata.get("License-Expression") or (dist.metadata.get("License") or "")
        texts = []
        for file in dist.files or []:
            parts = Path(str(file)).parts
            in_dist_info = any(part.endswith(".dist-info") for part in parts)
            if in_dist_info and ("licenses" in parts or LICENSE_FILE.match(parts[-1])):
                try:
                    texts.append(
                        (
                            str(file),
                            Path(dist.locate_file(file)).read_text(
                                encoding="utf-8", errors="replace"
                            ),
                        )
                    )
                except OSError:
                    continue
        found[name.lower()] = {
            "name": name,
            "version": dist.version,
            "license": declared.strip().splitlines()[0] if declared.strip() else "",
            "classifiers": classifiers,
            "files": sorted(texts),
        }
    return sorted(found.values(), key=lambda item: item["name"].lower())


def build(
    packages: list[dict],
    doc_root: Path,
    common_root: Path,
    python: list[dict],
) -> tuple[str, dict]:
    missing = [p["name"] for p in packages if copyright_file(doc_root, p["name"]) is None]
    if missing:
        raise SystemExit(
            "These packages ship no copyright file, so their notice cannot be included: "
            + ", ".join(missing)
        )
    groups: dict[str, dict] = {}
    for package in packages:
        text = copyright_file(doc_root, package["name"]).read_text(
            encoding="utf-8", errors="replace"
        )
        key = hashlib.sha256(text.encode()).hexdigest()
        group = groups.setdefault(key, {"text": text, "packages": []})
        group["packages"].append(package)
    referenced = (
        sorted(
            path.name
            for path in common_root.iterdir()
            if path.is_file() and any(path.name in group["text"] for group in groups.values())
        )
        if common_root.is_dir()
        else []
    )

    lines = [
        "Third-party software in the Alysis owl computer image",
        "=" * 54,
        "",
        f"Debian packages: {len(packages)}. Python packages: {len(python)}.",
        "Where to get the corresponding source of each package: sources.json.",
        "",
        "Debian packages",
        "---------------",
    ]
    for group in sorted(groups.values(), key=lambda g: g["packages"][0]["name"]):
        names = ", ".join(f"{p['name']} {p['version']}" for p in group["packages"])
        sources = sorted({f"{p['source']} {p['source_version']}" for p in group["packages"]})
        lines += ["", "#" * 72, f"Packages: {names}", f"Source: {'; '.join(sources)}"]
        licenses = dep5_licenses(group["text"])
        if licenses:
            lines.append(f"Licenses: {'; '.join(licenses)}")
        lines += ["", group["text"].rstrip()]
    if referenced:
        lines += ["", "", "Common license texts referenced above", "-" * 36]
        for name in referenced:
            text = (common_root / name).read_text(encoding="utf-8", errors="replace").rstrip()
            lines += ["", "#" * 72, f"/usr/share/common-licenses/{name}", "", text]
    lines += ["", "", "Python packages", "---------------"]
    for dist in python:
        declared = dist["license"] or "; ".join(dist["classifiers"]) or "not declared in metadata"
        lines += ["", "#" * 72, f"{dist['name']} {dist['version']}", f"License: {declared}"]
        if not dist["files"]:
            lines.append("No license file ships with this package; see its source on PyPI.")
        for path, text in dist["files"]:
            lines += ["", f"[{path}]", text.rstrip()]

    sources = {
        "schema": 1,
        "debian": [
            {
                "source": source,
                "version": version,
                "binaries": sorted(
                    p["name"]
                    for p in packages
                    if (p["source"], p["source_version"]) == (source, version)
                ),
                "url": f"https://snapshot.debian.org/package/{quote(source, safe='')}/{quote(version, safe='')}/",
            }
            for source, version in sorted({(p["source"], p["source_version"]) for p in packages})
        ],
        "python": [
            {
                "name": dist["name"],
                "version": dist["version"],
                "url": f"https://pypi.org/project/{quote(dist['name'], safe='')}/{quote(dist['version'], safe='')}/#files",
            }
            for dist in python
        ],
    }
    return "\n".join(lines) + "\n", sources


def main() -> None:
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "/usr/share/alysis")
    rows = subprocess.check_output(["dpkg-query", "-W", "-f=" + DPKG_FORMAT], text=True)
    notices, sources = build(
        debian_packages(rows.splitlines()),
        Path("/usr/share/doc"),
        Path("/usr/share/common-licenses"),
        python_distributions(FIRST_PARTY),
    )
    output.mkdir(parents=True, exist_ok=True)
    (output / "THIRD-PARTY-NOTICES.txt").write_text(notices, encoding="utf-8")
    (output / "sources.json").write_text(
        json.dumps(sources, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
