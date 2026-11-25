"""A tool's inputs as a model reads them: its JSON schema, without what only takes room.

Every tool an owl is offered is sent with every model call, so its schema is paid for on each
step and shares the context window with the work. Pydantic labels every model and field with a
"title" that repeats a name the schema already has (``"after_seconds": {"title": "After
Seconds", ...}``), and gives a tagged union an OpenAPI "discriminator" that maps each tag to the
variant whose own "kind" already says it; a model learns nothing from either, so both are left
out. Descriptions, types, limits and defaults are kept as they are.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

# Keys whose values map names to schemas: a "title" key inside them names a parameter (a
# document's "title" input, say) and is kept.
_NAMED = frozenset({"properties", "$defs", "definitions", "patternProperties"})
# Keys whose values are data, not schemas: kept exactly as they are.
_DATA = frozenset({"default", "const", "enum", "examples"})


def _labels_only(key: str, value: Any) -> bool:
    return (key == "title" and isinstance(value, str)) or (
        key == "discriminator" and isinstance(value, dict) and "propertyName" in value
    )


def without_titles(node: Any) -> Any:
    """A JSON schema without its "title" labels and OpenAPI discriminators; parameters called
    "title" or "discriminator" stay."""
    if isinstance(node, list):
        return [without_titles(item) for item in node]
    if not isinstance(node, dict):
        return node
    return {
        key: (
            value
            if key in _DATA
            else {name: without_titles(schema) for name, schema in value.items()}
            if key in _NAMED and isinstance(value, dict)
            else without_titles(value)
        )
        for key, value in node.items()
        if not _labels_only(key, value)
    }


def tool_parameters(model: type[BaseModel]) -> dict:
    """The JSON schema of a tool's input model, as it is offered to a model."""
    return without_titles(model.model_json_schema())
