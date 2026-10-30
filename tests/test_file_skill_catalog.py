"""Resource/selection coverage without a wheel build, installation, or provider run."""

from pathlib import Path

from hatchling.builders.sdist import SdistBuilder
from hatchling.builders.wheel import WheelBuilder

from alysis_code.skills import discover_skills, read_skill_bundle_file, validate_skill_bundle
from alysis_code.skills.prompting import build_skill_advertise_block

NAMES = {"image-pdf-files", "office-files", "local-files"}
BUNDLED = {
    "address-pr-comments",
    "alysis-docs",
    "code-review",
    "commit",
    "computer-use",
    "debug",
    "documents",
    "fix-ci",
    "image-pdf-files",
    "local-files",
    "office-files",
    "pdf",
    "presentations",
    "release-notes",
    "research",
    "security-review",
    "skill-creator",
    "spreadsheets",
}


def test_bundled_skills_and_references_are_selected_for_wheel_and_sdist():
    root = Path(__file__).resolve().parents[1]
    bundled = root / "src/alysis_code/skills/bundled"
    names = {p.name for p in bundled.iterdir() if (p / "SKILL.md").is_file()}
    assert names == BUNDLED and NAMES <= names
    for builder, prefix in ((WheelBuilder, ""), (SdistBuilder, "src/")):
        included = {
            f.distribution_path: f.path for f in builder(str(root)).recurse_included_files()
        }
        for name in names:
            validation = validate_skill_bundle(bundled / name)
            assert validation.valid and not validation.warnings
            if name in NAMES:
                assert len(validation.description) <= 140
                assert len(validation.instructions) < 3000
            for resource in (bundled / name).rglob("*.md"):
                key = prefix + str(resource.relative_to(root / "src"))
                assert Path(included[key]).read_bytes() == resource.read_bytes()
        assert prefix + "alysis_code/file_staging.py" in included


def test_crowded_skill_catalog_preserves_distinguishing_words(tmp_path, mac_computer_use_protocol):
    for number in range(4):
        folder = tmp_path / ".alysis_skills" / f"custom-{number}"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: custom-{number}\ndescription: "
            + "Extra context " * 16
            + "\n---\nInstructions.\n"
        )
    found = discover_skills(
        focus_path=tmp_path,
        workspace_root=tmp_path,
        user_config_dir=tmp_path / "config",
        home_dir=tmp_path / "home",
    )
    assert sorted(s.name for s in found.ordered) == sorted(
        BUNDLED | {f"custom-{n}" for n in range(4)}
    )
    catalog = build_skill_advertise_block(skills=found.ordered, skills_auto_invoke=True)
    assert len(catalog) <= 16000 and "...(truncated)" not in catalog
    computer = found.skills["computer-use"]
    assert len(computer.description) <= 140
    line = next(line for line in catalog.splitlines() if line.startswith("- computer-use |"))
    assert line.startswith("- computer-use | Observe/operate Mac app windows")
    assert computer.description in line
    for phrase in (
        "open preview",
        "in finder",
        "computer_discover before assuming a project preview",
    ):
        assert phrase in line
    words = {
        "image-pdf-files": "image and PDF",
        "office-files": "DOCX, XLSX, and PPTX",
        "local-files": "List, find, copy, rename, move",
    }
    for name, distinguishing in words.items():
        line = next(line for line in catalog.splitlines() if line.startswith(f"- {name} |"))
        assert distinguishing in line
        body = read_skill_bundle_file(found.skills[name], "references/staging.md")["content"]
        assert "50 MiB" in body and "readonly" in body
