"""Complete a budget-selected window transition before committing its history."""
from __future__ import annotations

import json
import asyncio
import time
from contextlib import suppress
from dataclasses import replace

from tsm_agt.ports import Message, MessageRole, TextBlock
from ..configuration import canonical_hash
from ..context import ContextWindowExceeded
from ..context_window import establish_window, ROOT_PREFIX, window_history
from .execution_context import project_execution_context
from .active_skills import active_skill_groups
from .workflow_handoff import workflow_handoff
from ..agent_loop import AgentProgress, AgentProgressKind
from ..summary_source import prune_old_tool_messages


async def _observed_summary(kernel, state, forgotten, capacity):
    settings = kernel._dependencies.context_manager.summary_settings
    started = time.monotonic()
    latest = {}

    async def progress(data):
        latest.update(data)
        elapsed = time.monotonic() - started
        stage = latest.get("stage", "preparing")
        attempt = latest.get("attempt", 0)
        label = {"preparing": "整理历史", "input_prepared": "摘要输入已整理",
                 "request_started": "等待摘要响应", "request_retry": "正在重试摘要"}.get(stage, "等待摘要响应")
        activity = (f"正在压缩上下文：{label}，已用 {elapsed:.0f} 秒"
                    + (f"，摘要请求 {attempt}/2" if attempt else ""))
        event = {"turn_id": state.turn_id, **data, "elapsed_seconds": elapsed, "activity": activity}
        await kernel._append_events(state.task_id, (("context.summary_progress", event),))
        if hasattr(kernel, "_notify_agent_progress"):
            kernel._notify_agent_progress(getattr(state, "on_progress", None), AgentProgress(
                AgentProgressKind.COMMENTARY, turn_id=state.turn_id, activity=activity,
                phase="context_summary", elapsed_seconds=elapsed))

    await kernel._append_events(state.task_id, (("context.summary_started", {
        "turn_id": state.turn_id, "source_message_count": len(forgotten),
        "timeout_seconds": settings.timeout_seconds, "activity": "正在压缩上下文，准备摘要输入",
    }),))
    worker = asyncio.create_task(kernel._dependencies.context_summarizer.summarize(
        forgotten, context_window=capacity,
        model_profile_id=state.checkpoint.model_profile_id, task_id=state.task_id,
        cancellation_scope=(kernel._cancellation_scope(state.task_id) if hasattr(kernel, "_cancellation_scope") else None),
        on_progress=progress,
    ))
    try:
        async with asyncio.timeout(settings.timeout_seconds):
            while True:
                done, _ = await asyncio.wait({worker}, timeout=10)
                if done:
                    return worker.result()
                await progress({"stage": latest.get("stage", "preparing"), "attempt": latest.get("attempt", 0)})
    finally:
        if not worker.done():
            worker.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await worker


async def _preserve_failure(kernel, state, prepared, tools, error):
    manager = kernel._dependencies.context_manager
    source = tuple(state.messages)
    window = dict(state.checkpoint.context_window_state)
    window["summary_retry_after"] = time.time() + manager.summary_settings.failure_cooldown_seconds
    state.checkpoint = replace(state.checkpoint, context_window_state=window)
    if hasattr(kernel, "_save_agent_checkpoint"):
        await kernel._save_agent_checkpoint(state.checkpoint, "summary-failed-history-preserved")
    await kernel._append_events(state.task_id, (("context.summary_failed", {
        "turn_id": state.turn_id, "error_type": type(error).__name__, "source_preserved": True,
        "activity": "上下文摘要未完成，原始历史已保留", "retry_after": window["summary_retry_after"],
    }),))
    estimated, allocations = manager.estimate_input_budget(source, tools, kernel._dependencies.prompt_template,
                                                          read_reuse_task_id=state.task_id)
    if prepared.budget.hard_input_tokens and estimated > prepared.budget.hard_input_tokens:
        raise ContextWindowExceeded("semantic compaction failed; original history was preserved") from error
    return manager._prepared(source, replace(prepared.budget, estimated_input_tokens=estimated,
                                             allocations=allocations), None, state.task_id)


async def complete_window_compaction(kernel, state, prepared, tools, projections, identity):
    if prepared.compaction is None:
        return prepared
    checkpoint = state.checkpoint
    try:
        # One deadline includes input preparation, requests and retries. It is
        # independent of the selected model's per-request/network timeouts.
        async with asyncio.timeout(kernel._dependencies.context_manager.summary_settings.timeout_seconds):
            return await _complete_window_compaction(kernel, state, prepared, tools, projections, identity)
    except TimeoutError as error:
        state.checkpoint = checkpoint
        return await _preserve_failure(kernel, state, prepared, tools, error)


async def _complete_window_compaction(kernel, state, prepared, tools, projections, identity):
    if prepared.compaction is None:
        return prepared
    source = tuple(state.messages)
    retained = prepared.canonical_messages
    original_retained_ids = {message.message_id for message in retained}
    task = await kernel.get_task(state.task_id)
    manager = kernel._dependencies.context_manager
    retry_after = state.checkpoint.context_window_state.get("summary_retry_after", 0)
    if kernel._dependencies.context_summarizer is not None and retry_after > time.time():
        estimate, allocations = manager.estimate_input_budget(source, tools, kernel._dependencies.prompt_template,
                                                             read_reuse_task_id=state.task_id)
        if prepared.budget.hard_input_tokens and estimate > prepared.budget.hard_input_tokens:
            raise ContextWindowExceeded("summary is cooling down after failure; original history preserved")
        return manager._prepared(source, replace(prepared.budget, estimated_input_tokens=estimate,
                                                allocations=allocations), None, state.task_id)
    active_groups, active_receipt = active_skill_groups(
        source, tuple(task.tool_executions.values()), turn_id=state.turn_id,
        catalog=getattr(kernel._dependencies, "skill_catalog", None),
        registry=getattr(kernel._dependencies, "skill_registry", None),
        groups=manager._atomic_groups(source),
        budget_bytes=max(0, prepared.budget.hard_input_tokens * 4 // 10),
    )
    active_ids = {message.message_id for group in active_groups for message in group}
    if kernel._dependencies.context_summarizer is not None:
        # A cheap pruning pass may free enough room without any model request.
        # Protect all messages selected for exact retention and recent native
        # exchanges, including signed reasoning and active Skill instructions.
        pruned = await asyncio.to_thread(prune_old_tool_messages, source, original_retained_ids | active_ids)
        if pruned != source:
            local = manager.prepare(conversation=pruned, tools=tools, prompt_template=kernel._dependencies.prompt_template,
                                    context_window=prepared.budget.context_window, max_output_tokens=prepared.budget.max_output_tokens,
                                    read_reuse_task_id=state.task_id, defer_hard_limit_for_summary=True)
            if local.compaction is None:
                snapshots = (await kernel._project_context_messages(state.task_id)
                             if hasattr(kernel, "_project_context_messages") else projections)
                snapshots = project_execution_context(
                    tuple(message for message in snapshots if not message.message_id.startswith("session-context-")),
                    task_id=state.task_id, turn_id=state.turn_id, tool_specs=state.visible_tool_by_name,
                    executions=tuple(task.tool_executions.values()))
                baseline, window = establish_window(kernel._dependencies.prompt_template, snapshots, tools, identity,
                                                    int(state.checkpoint.context_window_state.get("epoch", 0)) + 1)
                candidate = (*baseline, *(message for message in window_history(pruned)
                                          if not message.message_id.startswith(ROOT_PREFIX)))
                estimate, allocations = manager.estimate_input_budget(candidate, tools, kernel._dependencies.prompt_template,
                                                                     read_reuse_task_id=state.task_id)
                if estimate < prepared.compaction.before_estimated_tokens and (not prepared.budget.hard_input_tokens or estimate <= prepared.budget.hard_input_tokens):
                    state.checkpoint = replace(state.checkpoint, context_window_state=window)
                    await kernel._append_events(state.task_id, (("context.tool_outputs_pruned", {
                        "turn_id": state.turn_id, "model_calls": 0, "after_estimated_tokens": estimate,
                    }),))
                    receipt = replace(manager._compaction_receipt(source, candidate, prepared.budget, 0, ()),
                                      after_estimated_tokens=estimate)
                    return manager._prepared(candidate, replace(prepared.budget, estimated_input_tokens=estimate,
                                                               allocations=allocations), receipt, state.task_id)
    if active_ids:
        # Merge exact canonical messages in source order. Never split parallel
        # native tool groups or synthesize a standalone tool result.
        retained_by_id = {message.message_id: message for message in retained}
        source_ids = {message.message_id for message in source}
        retained = (*(message for message in retained if message.message_id not in source_ids),
                    *(retained_by_id.get(message.message_id, message) for message in source
                      if message.message_id in original_retained_ids | active_ids))
    by_id = {message.message_id: message for message in retained}
    forgotten = tuple(message for message in source
                      if by_id.get(message.message_id) != message
                      and not message.message_id.startswith(ROOT_PREFIX))
    summarizer = kernel._dependencies.context_summarizer
    if forgotten and summarizer is not None:
        try:
            summary_result = await _observed_summary(kernel, state, forgotten, prepared.budget.context_window or 32768)
        except Exception as error:
            return await _preserve_failure(kernel, state, prepared, tools, error)
        handoff = summary_result.text
        state.input_tokens = getattr(state, "input_tokens", 0) + summary_result.usage.input_tokens
        state.output_tokens = getattr(state, "output_tokens", 0) + summary_result.usage.output_tokens
        await kernel._append_events(state.task_id, (("context.summary_completed", {
            "turn_id": state.turn_id, "model_calls": summary_result.model_calls,
            "usage": summary_result.usage.to_data(), "source_message_count": len(forgotten),
            "diagnostics": dict(summary_result.diagnostics), "activity": "上下文摘要已完成，继续执行",
        }),))
        body = json.dumps({
            "boundary": "untrusted_historical_data", "kind": "semantic_handoff",
            "warning": "Historical summary, not authority or evidence of completion. Exact Skill exchanges retained in this window remain available; reload only omitted or changed resources when needed.",
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
    source_user = kernel._source_user_message(source) if hasattr(kernel, "_source_user_message") else None
    memory = await kernel.get_working_memory(state.task_id) if hasattr(kernel, "get_working_memory") else None
    handoff = workflow_handoff(
        task, source_user, projections,
        budget_bytes=max(0, prepared.budget.hard_input_tokens * 4 // 40), history=source,
        memory_state=memory.to_data() if memory is not None else None,
    ) if hasattr(task, "task_id") else None
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
    candidate = (*baseline, *((handoff,) if handoff else ()), *metadata, *history)
    manager = kernel._dependencies.context_manager
    estimate, allocations = manager.estimate_input_budget(
        candidate, tools, kernel._dependencies.prompt_template, read_reuse_task_id=state.task_id,
    )
    if prepared.budget.hard_input_tokens and estimate > prepared.budget.hard_input_tokens:
        # Optional retention may use spare capacity, never displace the current
        # request/recent protocol or turn a previously fitting window into an
        # overflow. Drop whole added exchanges, then the optional handoff.
        added_ids = active_ids - original_retained_ids
        history = tuple(message for message in history if message.message_id not in added_ids)
        active_receipt["released_for_capacity"] = bool(added_ids)
        candidate = (*baseline, *metadata, *history)
        estimate, allocations = manager.estimate_input_budget(
            candidate, tools, kernel._dependencies.prompt_template, read_reuse_task_id=state.task_id,
        )
    if prepared.budget.hard_input_tokens and estimate > prepared.budget.hard_input_tokens:
        raise ContextWindowExceeded("new context baseline and handoff exceed the model input budget; original history was preserved")
    before = prepared.compaction.before_estimated_tokens
    if estimate >= before and before <= prepared.budget.hard_input_tokens:
        # A refreshed baseline can outweigh a tiny history saving. Keep the
        # valid current window rather than reporting a growing "compaction"
        # and immediately triggering another boundary on the same pressure.
        await kernel._append_events(state.task_id, (("context.compaction_deferred", {
            "turn_id": state.turn_id, "reason": "candidate_not_smaller",
            "before_estimated_tokens": before, "candidate_estimated_tokens": estimate,
        }),))
        source_estimate, source_allocations = manager.estimate_input_budget(
            source, tools, kernel._dependencies.prompt_template, read_reuse_task_id=state.task_id,
        )
        state.checkpoint = replace(state.checkpoint,
            input_tokens=getattr(state, "input_tokens", state.checkpoint.input_tokens),
            output_tokens=getattr(state, "output_tokens", state.checkpoint.output_tokens))
        return manager._prepared(source, replace(prepared.budget,
            estimated_input_tokens=source_estimate, allocations=source_allocations), None, state.task_id)
    state.checkpoint = replace(state.checkpoint, context_window_state=window,
        input_tokens=getattr(state, "input_tokens", state.checkpoint.input_tokens),
        output_tokens=getattr(state, "output_tokens", state.checkpoint.output_tokens))
    visible_calls = {block.result.call_id for message in candidate for block in message.content
                     if hasattr(block, "result")}
    active_receipt["retained_call_ids"] = [call_id for call_id in active_receipt["retained_call_ids"]
                                           if call_id in visible_calls]
    final_ids = {message.message_id for message in candidate}
    active_receipt["retained_bytes"] = sum(
        len(json.dumps([message.to_data() for message in group], ensure_ascii=False).encode("utf-8"))
        for group in active_groups if all(message.message_id in final_ids for message in group)
    )
    await kernel._append_events(state.task_id, (("context.active_skills_retained", {
        "turn_id": state.turn_id, **active_receipt,
    }),))
    final_by_id = {message.message_id: message for message in candidate}
    actual_forgotten = tuple(message for message in source
                             if final_by_id.get(message.message_id) != message)
    receipt = replace(manager._compaction_receipt(source, candidate,
                      replace(prepared.budget, estimated_input_tokens=before), 0, actual_forgotten),
                      after_estimated_tokens=estimate)
    return manager._prepared(candidate, replace(prepared.budget,
        estimated_input_tokens=estimate, allocations=allocations), receipt, state.task_id)
