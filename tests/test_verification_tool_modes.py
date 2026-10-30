import pytest
from jsonschema import Draft202012Validator

from alysis_code.llm.gemini_generate_content import _gemini_schema
from alysis_code.tools.registry import get_builtin_tool_metadata


@pytest.mark.parametrize(
    "convert", [lambda schema: schema, _gemini_schema], ids=["json-schema", "native-gemini"]
)
@pytest.mark.parametrize("mode", ["defaults", "commands", "consumer", "mixed"])
def test_verification_schema_expresses_the_runtime_modes_before_execution(convert, mode):
    schema = convert(get_builtin_tool_metadata("verify_run").parameters)
    command = {"commands": ["python -m unittest discover"]}
    consumer = {
        "consumer_profile": {
            "profile": "library",
            "artifacts": ["package.py"],
            "python_module": "package",
        }
    }
    arguments = {
        "defaults": {},
        "commands": command,
        "consumer": consumer,
        "mixed": {**command, **consumer},
    }[mode]
    assert Draft202012Validator(schema).is_valid(arguments) is (mode != "mixed")
