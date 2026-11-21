"""A tool's inputs as a model reads them: its JSON schema, without what only takes room.

Every tool an owl is offered is sent with every model call, so its schema is paid for on each
step and shares the context window with the work. Pydantic labels every model and field with a
"title" that repeats a name the schema already has (``"after_seconds": {"title": "After
Seconds", ...}``); a model learns nothing from it, so it is left out. Descriptions, types,
limits and defaults are kept as they are.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

# Keys whose values map names to schemas: a "title" key inside them names a parameter (a
# document's "title" input, say) and is kept.
_NAMED = frozenset({"properties", "$defs", "definitions", "patternProperties"})
# Keys whose values are data, not schemas: kept exactly as they are.
_DATA = frozenset({"default", "const", "enum", "examples"})


def without_titles(node: Any) -> Any:
    """A JSON schema without its "title" labels; parameters called "title" stay."""
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
        if not (key == "title" and isinstance(value, str))
    }


def tool_parameters(model: type[BaseModel]) -> dict:
    """The JSON schema of a tool's input model, as it is offered to a model."""
    return without_titles(model.model_json_schema())
