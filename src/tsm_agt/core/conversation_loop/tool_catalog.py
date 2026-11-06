"""Refresh a turn's callable tools after an explicit catalog observation."""
from __future__ import annotations

from typing import Any

from tsm_agt.ports import MessageRole, ToolResultBlock

from ..agent_loop import TurnState
from ..configuration import effective_toolset_hash
from ..execution import ToolCommitState, ToolExecutionRecord


async def refresh_observed_tool_catalog(kernel: Any, state: TurnState) -> None:
    """Read trusted providers after a successful catalog-refreshing call.

    Result bodies never install schemas. The executed ToolSpec declares this
    capability, and the durable execution proves the call actually succeeded.
    All results in the preceding batch participate, including approval resumes.
    """
    results = []
    for message in reversed(state.messages):
        if message.role is MessageRole.ASSISTANT:
            break
        if message.role is MessageRole.TOOL:
            results.extend(block.result for block in message.content if isinstance(block, ToolResultBlock))
    if not results or not any(tool.refreshes_tool_catalog for tool in state.visible_tools):
        return
    task = await kernel.get_task(state.task_id)
    triggers = []
    for result in results:
        execution = task.tool_executions.get(ToolExecutionRecord.identity(state.turn_id, result.call_id))
        if execution is None or execution.state is not ToolCommitState.COMMITTED:
            continue
        spec = state.visible_tool_by_name.get(execution.call.name)
        if result.ok and spec is not None and spec.refreshes_tool_catalog:
            triggers.append(result.call_id)
    if not triggers:
        return
    tools = await kernel.list_tools()
    old_names = set(state.visible_tool_by_name)
    new_hash = effective_toolset_hash(tools)
    state.visible_tools = tools
    state.visible_tool_by_name = {tool.name: tool for tool in tools}
    if new_hash == state.checkpoint.toolset_hash:
        return
    # Bind the effective configuration too: only changing the toolset hash
    # would leave the next approval/resume comparing against a stale snapshot.
    state.checkpoint = await kernel._bind_agent_checkpoint(
        task, state.checkpoint, tools, configuration_reason="tool_catalog_refresh",
    )
    await kernel._append_events(state.task_id, (("tools.catalog_refreshed", {
        "turn_id": state.turn_id, "trigger_call_ids": triggers,
        "added_tools": sorted(set(state.visible_tool_by_name) - old_names),
        "removed_tools": sorted(old_names - set(state.visible_tool_by_name)),
        "tool_count": len(tools), "toolset_hash": new_hash,
    }),))
    await kernel._save_agent_checkpoint(state.checkpoint, "conversation-loop-tool-catalog-refreshed")
