"""One stable identity for observed exploration calls and relation lookup."""
import hashlib
import json

from tsm_agt.ports import ToolCall


def call_fingerprint(call: ToolCall) -> str:
    arguments = {str(k): v for k, v in sorted(call.arguments.items(), key=lambda row: str(row[0]))
                 if v not in (None, "", (), [], {})}
    payload = {"tool_name": call.name, "arguments": arguments}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
