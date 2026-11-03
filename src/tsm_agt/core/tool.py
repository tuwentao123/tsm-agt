"""Tool discovery, argument validation, and invocation failures."""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
import json
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import Unresolvable

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
    """Enforce the advertised schema locally, without fetching external $refs.

    Provider strict mode is optional; nested constraints must hold regardless
    of the model or transport. JSON Schema defaults remain annotations, never
    implicit argument rewrites. Format is annotation-only unless a tool checks it.
    """

    try:
        validator = _argument_validator(json.dumps(dict(spec.parameters), sort_keys=True))
        error = next(validator.iter_errors(dict(arguments)), None)
    except (SchemaError, Unresolvable, TypeError, ValueError) as error:
        raise InvalidToolArguments(
            f"{spec.name} has an invalid or unresolved parameter schema; "
            "external schema references are not fetched"
        ) from error
    if error is not None:
        path = "arguments" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        )
        detail = _argument_error_detail(error)
        description = error.schema.get("description", "") if isinstance(error.schema, Mapping) else ""
        guidance = f" {description[:600]}" if isinstance(description, str) and description else ""
        raise InvalidToolArguments(f"{spec.name} {path}: {detail}.{guidance}")
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


@lru_cache(maxsize=256)
def _argument_validator(serialized_schema: str):
    schema = json.loads(serialized_schema)
    validator_type = validator_for(schema, default=Draft202012Validator)
    validator_type.check_schema(schema)
    return validator_type(schema, registry=Registry())


def _argument_error_detail(error: ValidationError) -> str:
    """Describe the violated rule without echoing potentially sensitive values."""
    rule, expected = error.validator, error.validator_value
    if rule == "required":
        missing = [name for name in expected if name not in error.instance]
        return "missing required arguments: " + ", ".join(missing)
    if rule == "type":
        return f"must be {expected}"
    if rule in {"enum", "const"}:
        return f"must match {rule} {json.dumps(expected, ensure_ascii=False)}"
    if rule in {"minLength", "maxLength"}:
        return f"must contain {'at least' if rule == 'minLength' else 'at most'} {expected} characters"
    if rule == "pattern":
        return f"must match pattern {expected}"
    if rule == "additionalProperties":
        return "contains unknown arguments not allowed by additionalProperties"
    return f"must satisfy {rule}={json.dumps(expected, ensure_ascii=False)}"
