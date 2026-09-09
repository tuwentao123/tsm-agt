"""Tool discovery, argument validation, and invocation failures."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tsm_agt.ports import ToolSpec


class ToolNotFound(LookupError):
    pass


class DuplicateToolName(ValueError):
    pass


class InvalidToolArguments(ValueError):
    pass


class InvalidToolResult(ValueError):
    pass


def validate_tool_arguments(spec: ToolSpec, arguments: Mapping[str, Any]) -> None:
    """Validate the dependency-free JSON Schema subset supported by the MVP."""

    schema = spec.parameters
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise InvalidToolArguments(f"{spec.name} has an invalid properties schema")
    required = schema.get("required", ())
    if not isinstance(required, (list, tuple)) or not all(
        isinstance(name, str) for name in required
    ):
        raise InvalidToolArguments(f"{spec.name} has an invalid required schema")

    missing = [name for name in required if name not in arguments]
    if missing:
        raise InvalidToolArguments(
            f"{spec.name} is missing required arguments: {', '.join(missing)}"
        )
    if spec.name == "core.read_file":
        supplied = sum(
            isinstance(arguments.get(name), str)
            and bool(str(arguments[name]).strip())
            for name in ("path", "resource_ref")
        )
        if supplied != 1:
            raise InvalidToolArguments(
                "core.read_file requires exactly one of path or resource_ref"
            )

    if schema.get("additionalProperties") is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise InvalidToolArguments(
                f"{spec.name} received unknown arguments: {', '.join(unknown)}"
            )

    type_checks = {
        "string": lambda value: isinstance(value, str),
        "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda value: isinstance(value, (int, float))
        and not isinstance(value, bool),
        "boolean": lambda value: isinstance(value, bool),
        "object": lambda value: isinstance(value, Mapping),
        "array": lambda value: isinstance(value, (list, tuple)),
        "null": lambda value: value is None,
    }
    for name, value in arguments.items():
        property_schema = properties.get(name)
        if not isinstance(property_schema, Mapping):
            continue
        expected = property_schema.get("type")
        check = type_checks.get(expected)
        if check is not None and not check(value):
            raise InvalidToolArguments(
                f"{spec.name} argument {name!r} must be {expected}"
            )
