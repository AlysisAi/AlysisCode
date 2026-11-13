"""Deliverable checks executed only inside the private computer."""

import hashlib
import io
import time
from pathlib import Path
from zipfile import ZipFile

from .models import FileInput

FORMATS = frozenset({".docx", ".xlsx", ".pptx", ".pdf"})


def check_file(root: Path, name: str) -> dict:
    from ..artifact_checks import ArtifactChecks
    from ..documents.__main__ import _path
    from ..office.__main__ import MAX_FILE, _read

    name = FileInput(name=name, content="").name
    report = ArtifactChecks(root).check(name)
    if Path(name).suffix.lower() != ".pdf" and not failed(report):
        from lxml import etree

        data = _read(_path(root, name), MAX_FILE)
        if hashlib.sha256(data).hexdigest() != report["documentSha256"]:
            raise ValueError(
                "The artifact changed while checking; check the current version again."
            )
        incompatible = []
        with ZipFile(io.BytesIO(data)) as package:
            for member in package.namelist():
                if not member.endswith(".xml"):
                    continue
                tree = etree.fromstring(
                    package.read(member), etree.XMLParser(resolve_entities=False, no_network=True)
                )
                for namespace in (
                    "http://schemas.openxmlformats.org/drawingml/2006/chart",
                    "http://purl.oclc.org/ooxml/drawingml/chart",
                ):
                    for element in tree.iter(f"{{{namespace}}}axId", f"{{{namespace}}}crossAx"):
                        try:
                            # MS-OI29500 2.1.1432: Office limits c:axId to 2147483647.
                            valid = 0 <= int(element.get("val", "")) <= 2147483647
                        except ValueError:
                            valid = False
                        if not valid:
                            incompatible.append(member)
                            break
        report["items"].append(
            {
                "id": "office_compatibility",
                "label": "Office chart axis compatibility",
                "basis": "observed",
                "status": "failed" if incompatible else "passed",
                "detail": (
                    "Chart axis IDs are outside Office's nonnegative 31-bit range in "
                    + ", ".join(sorted(set(incompatible)))
                    + ". Reassign each distinct axis ID to a unique value in that range and update every axId and crossAx reference consistently; preserve the chart relationships."
                    if incompatible
                    else "Chart axis IDs are within Office's documented range. This is not a full Office schema validation."
                ),
            }
        )
    report["next"] = (
        "Fix failed checks and check the new file again. Open the returned page images "
        "on this computer to inspect layout before claiming a visual review. "
        "These checks do not verify source accuracy or task requirements."
    )
    return report


def failed(report: dict) -> bool:
    return any(item["status"] == "failed" for item in report["items"])


def await_check(request) -> dict:
    """Keep each provider RPC bounded while the private checker finishes its work."""
    started = request("check_file_start")
    while True:
        result = request("check_file_result", check_id=started["check_id"])
        if not result["running"]:
            return result["report"]
        time.sleep(0.05)
