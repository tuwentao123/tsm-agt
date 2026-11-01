"""Runtime tool concurrency is independent of model wire capabilities."""

from collections.abc import Mapping


def load_max_parallel_tool_calls(document: Mapping[str, object]) -> int:
    execution = document.get("execution", {})
    if not isinstance(execution, Mapping):
        raise ValueError("execution must be a mapping")
    limit = execution.get("max_parallel_tool_calls", 8)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("execution.max_parallel_tool_calls must be a positive integer")
    return limit
