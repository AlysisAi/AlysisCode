"""P2's transferred catalog/lookup seam, using the actual approved custom descriptor."""

from alysis_code.extensions.plugin_skills import active_plugin_skills, find_plugin_skill
from tests.test_custom_plugins import _descriptor, _save, root  # noqa: F401


def test_custom_catalog_and_qualified_lookup(root):  # noqa: F811
    _save(
        root,
        _descriptor(
            extra='\n[[components.plugin_skill]]\nname="notes"\ntitle="Notes"\ndescription="Read notes."\ntext="Summarize the notes."\n'
        ),
    )
    skills = active_plugin_skills(root, connector_mcp=[{"plugin_id": "custom.tools"}])
    [skill] = skills
    assert skill.name == "custom_tools:notes"
    assert find_plugin_skill(skills, "custom_tools:notes") is skill
    assert find_plugin_skill(skills, "custom.tools:notes") is skill
    for bad in ["notes", "tools:notes", "CUSTOM_TOOLS:NOTES", " custom_tools:notes "]:
        assert find_plugin_skill(skills, bad) is None
    assert find_plugin_skill((skill, skill), skill.name) is None
