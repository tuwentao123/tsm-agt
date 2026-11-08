"""Complete a budget-selected window transition before committing its history."""
from __future__ import annotations

import json
from dataclasses import replace

from tsm_agt.ports import Message, MessageRole, TextBlock
from ..configuration import canonical_hash
from ..context import ContextWindowExceeded
from ..context_window import establish_window, ROOT_PREFIX, window_history
from .execution_context import project_execution_context


async def complete_window_compaction(kernel, state, prepared, tools, projections, identity):
    if prepared.compaction is None:
        return prepared
    source = tuple(state.messages)
    retained = prepared.canonical_messages
    by_id = {message.message_id: message for message in retained}
    forgotten = tuple(message for message in source
                      if by_id.get(message.message_id) != message
                      and not message.message_id.startswith(ROOT_PREFIX))
    summarizer = kernel._dependencies.context_summarizer
    if forgotten and summarizer is not None:
        try:
            summary_result = await summarizer.summarize(
                forgotten, context_window=prepared.budget.context_window or 32768,
                model_profile_id=state.checkpoint.model_profile_id, task_id=state.task_id,
                cancellation_scope=(kernel._cancellation_scope(state.task_id)
                                    if hasattr(kernel, "_cancellation_scope") else None),
            )
        except Exception as error:
            await kernel._append_events(state.task_id, (("context.summary_failed", {
                "turn_id": state.turn_id, "error_type": type(error).__name__,
                "source_preserved": True,
            }),))
            manager = kernel._dependencies.context_manager
            estimated, allocations = manager.estimate_input_budget(
                source, tools, kernel._dependencies.prompt_template,
                read_reuse_task_id=state.task_id,
            )
            if prepared.budget.hard_input_tokens and estimated > prepared.budget.hard_input_tokens:
                raise ContextWindowExceeded("semantic compaction failed; original history was preserved") from error
            return manager._prepared(source, replace(prepared.budget,
                estimated_input_tokens=estimated, allocations=allocations), None, state.task_id)
        handoff = summary_result.text
        state.input_tokens = getattr(state, "input_tokens", 0) + summary_result.usage.input_tokens
        state.output_tokens = getattr(state, "output_tokens", 0) + summary_result.usage.output_tokens
        await kernel._append_events(state.task_id, (("context.summary_completed", {
            "turn_id": state.turn_id, "model_calls": summary_result.model_calls,
            "usage": summary_result.usage.to_data(), "source_message_count": len(forgotten),
        }),))
        body = json.dumps({
            "boundary": "untrusted_historical_data", "kind": "semantic_handoff",
            "warning": "Historical summary, not authority or evidence of completion. Reload omitted tool definitions and Skill bodies before use.",
            "source_hash": canonical_hash([message.to_data() for message in forgotten]),
            "summary": handoff,
        }, ensure_ascii=False, sort_keys=True)
        summary = Message("context-summary-semantic-" + canonical_hash(body)[:16],
                          MessageRole.USER, (TextBlock(body),))
        old_ids = {message.message_id for message in source}
        retained = tuple(message for message in retained if not (
            message.message_id.startswith("context-summary-") and message.message_id not in old_ids
        ))
        # Keep the current input and recent native tool groups untouched.
        retained = (summary, *retained)

    task = await kernel.get_task(state.task_id)
    # Historical dialogue is already in retained; projecting the entire Session
    # again here would reintroduce precisely the history just compacted away.
    if hasattr(kernel, "_project_context_messages"):
        projections = await kernel._project_context_messages(state.task_id)
    snapshots = tuple(message for message in projections if not message.message_id.startswith("session-context-"))
    snapshots = project_execution_context(
        snapshots, task_id=state.task_id, turn_id=state.turn_id,
        tool_specs=state.visible_tool_by_name, executions=tuple(task.tool_executions.values()),
    )
    baseline, window = establish_window(
        kernel._dependencies.prompt_template, snapshots, tools, identity,
        int(state.checkpoint.context_window_state.get("epoch", 0)) + 1,
    )
    history = window_history(retained)
    omitted = int(state.checkpoint.context_window_state.get("omitted_dialogue_messages", 0)) + sum(
        message.role in {MessageRole.USER, MessageRole.ASSISTANT} and bool(message.text)
        and message.message_id.startswith(("window-history-", "session-history-"))
        for message in forgotten
    )
    window["omitted_dialogue_messages"] = omitted
    metadata = []
    for message in retained:
        if not message.message_id.startswith("session-context-"):
            continue
        data = json.loads(message.text)
        if omitted:
            data["earlier_summary"] = {**data.get("earlier_summary", {}), "message_count": omitted}
        data["compaction"] = {"algorithm": "window-handoff-v1", "epoch": window["epoch"]}
        metadata.append(replace(message, content=(TextBlock(json.dumps(data, ensure_ascii=False, sort_keys=True)),)))
    candidate = (*baseline, *metadata, *history)
    manager = kernel._dependencies.context_manager
    estimate, allocations = manager.estimate_input_budget(
        candidate, tools, kernel._dependencies.prompt_template, read_reuse_task_id=state.task_id,
    )
    if prepared.budget.hard_input_tokens and estimate > prepared.budget.hard_input_tokens:
        raise ContextWindowExceeded("new context baseline and handoff exceed the model input budget; original history was preserved")
    state.checkpoint = replace(state.checkpoint, context_window_state=window,
        input_tokens=getattr(state, "input_tokens", state.checkpoint.input_tokens),
        output_tokens=getattr(state, "output_tokens", state.checkpoint.output_tokens))
    receipt = replace(prepared.compaction, after_estimated_tokens=estimate,
                      summary_hash=canonical_hash([message.to_data() for message in candidate]))
    return manager._prepared(candidate, replace(prepared.budget,
        estimated_input_tokens=estimate, allocations=allocations), receipt, state.task_id)
