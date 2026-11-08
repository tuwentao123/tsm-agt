"""Provider-neutral MCP envelope; execution remains owned by the Kernel."""
from __future__ import annotations

import json
from dataclasses import replace

from tsm_agt.ports import ToolCall, ToolEffect, ToolRisk, ToolSpec
from .configuration import canonical_hash
from .tool import InvalidToolArguments

MCP_CALL_TOOL = "core.mcp_call"


def mcp_call_spec() -> ToolSpec:
    return ToolSpec(
        name=MCP_CALL_TOOL,
        description=(
            "Invoke an MCP tool found through core.mcp_discover. Use its exact name "
            "and encode its arguments as a JSON object string. The Runtime validates "
            "the actual tool's schema and applies its approval and recovery policy. "
            "If a definition changed, discover it again before correcting the call."
        ),
        parameters={
            "type": "object", "properties": {
                "tool_name": {"type": "string", "pattern": r"^mcp\.[^.]+\..+$"},
                "arguments_json": {"type": "string", "description": "JSON object matching the discovered input schema."},
                "schema_hash": {"type": "string", "description": "Optional definition hash returned by discovery."},
            },
            "required": ["tool_name", "arguments_json"], "additionalProperties": False,
        },
        # A conservative scheduling barrier, not the target authorization policy.
        risk=ToolRisk.R3, effect=ToolEffect.EXECUTE,
    )


def resolve_mcp_call(call: ToolCall) -> ToolCall:
    try:
        arguments = json.loads(call.arguments["arguments_json"])
    except (ValueError, TypeError, KeyError, RecursionError) as error:
        raise InvalidToolArguments("core.mcp_call arguments_json must encode a JSON object") from error
    if not isinstance(arguments, dict):
        raise InvalidToolArguments("core.mcp_call arguments_json must encode a JSON object")
    name = call.arguments.get("tool_name")
    if not isinstance(name, str) or not name.startswith("mcp.") or len(name.split(".", 2)) != 3:
        raise InvalidToolArguments("core.mcp_call target must be a native mcp.<server>.<tool> name")
    return replace(call, name=name, arguments=arguments)


def schema_hash(spec: ToolSpec) -> str:
    return canonical_hash({"name": spec.name, "parameters": dict(spec.parameters)})
