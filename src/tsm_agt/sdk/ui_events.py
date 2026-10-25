"""Allow-listed durable progress projection shared by live UI and history."""
from __future__ import annotations

import asyncio
from contextlib import aclosing
from collections.abc import Mapping


def installation_records(client, task_id: str, session_id: str | None) -> list[dict]:
    for provider in client.application.kernel.dependencies.tools:
        receipts = getattr(getattr(provider, "store", None), "installations", None)
        if receipts is not None:
            return [row for row in receipts.list() if row.get("task_id") == task_id
                    or (session_id and row.get("session_id") == session_id)]
    return []


def installation_view(job: dict, packet: dict) -> dict:
    if job["status"] in {"ready", "blocked", "failed"}:
        return dict(job)
    status = packet.get("status", "running")
    mapped = {"completed": "unverified", "awaiting_input": "unverified"}.get(status, status)
    summaries = {"awaiting_approval": "等待审批，请打开安装会话处理。",
                 "awaiting_user": "等待补充信息，请打开安装会话继续。",
                 "unverified": "会话已停止，尚未报告准备完成；请打开会话继续验证。",
                 "failed": "安装任务失败，请打开会话查看错误或重试准备。",
                 "interrupted": "安装任务已中断，请打开会话继续或重试准备。"}
    return {**job, "status": mapped, "summary": summaries.get(mapped, job["summary"])}


async def subscribe_installation(client, receipts, identity: str, *, heartbeat: float = 15):
    """Observe dispatch and preparation without a second polling loop."""
    store = client._store()
    cursor = 0
    while True:
        job = receipts.get(identity)
        waiting_scope = "task" if job.get("task_id") else "session"
        waiting_id = job.get("task_id") or job.get("session_id") or identity
        with store.watch(waiting_id, scope=waiting_scope) as signal:
            changed = signal.changed
            job = receipts.get(identity)
            settled = job["status"] in {"pending", "ready", "blocked", "failed", "interrupted", "unverified", "needs_review"}
            yield {"installation": job, "watching": not settled}
            if settled:
                return
            if job.get("task_id"):
                async with aclosing(subscribe_task_ui(client, job["task_id"], after=cursor, heartbeat=heartbeat)) as packets:
                    async for packet in packets:
                        if packet["type"] == "stream.heartbeat":
                            yield packet
                        cursor = max(cursor, packet.get("sequence", 0))
                        if packet["type"] != "task.snapshot":
                            continue
                        job = installation_view(receipts.get(identity), packet)
                        settled = job["status"] in {"ready", "blocked", "failed", "interrupted", "unverified", "needs_review"}
                        yield {"installation": job, "watching": not settled}
                        if settled:
                            return
            while not changed.is_set():
                try:
                    await asyncio.wait_for(changed.wait(), heartbeat)
                except TimeoutError:
                    yield {"type": "stream.heartbeat"}


async def ui_snapshot(client, task_id: str):
    """Read a version-consistent Task/Session view on the Runtime's own loop."""
    store = client._store()
    cache = getattr(client, "_ui_snapshots", None)
    if cache is None:
        cache = client._ui_snapshots = {}
    for _ in range(10):
        task = await client.application.kernel.get_task(task_id)
        before = await store.load_task(task_id)
        session = await store.load_session(task.session_id) if task.session_id else None
        key = (before.version, session.version if session else 0)
        if task_id in cache and cache[task_id][0] == key:
            return dict(cache[task_id][1])
        result = await client.get_task_result(task_id)
        events = await store.read_events(task_id)
        latest = next((event for event in reversed(events) if event.event_type == "turn.started"), None)
        turn_id = latest.turn_id or latest.payload.get("turn_id") if latest else None
        data = result.to_data()
        # An archived answer from an earlier turn is never this turn's output.
        data["assistant_text"] = None
        if task.session_id:
            for event in reversed(await store.read_session_events(task.session_id)):
                if (event.event_type == "session.task_result_recorded"
                    and event.payload.get("task_id") == task_id
                    and event.payload.get("turn_id") == turn_id):
                    message = event.payload.get("assistant_message") or {}
                    data["assistant_text"] = "\n".join(str(block.get("text", "")) for block in message.get("content", []) if block.get("type") == "text").strip() or None
                    break
        after = await store.load_task(task_id)
        session_after = await store.load_session(task.session_id) if task.session_id else None
        if before.version != after.version or (session.version if session else 0) != (session_after.version if session_after else 0):
            continue
        waiting = None
        if result.approval:
            waiting = {"kind": "APPROVAL", "approval": dict(result.approval)}
        elif result.status == "awaiting_user":
            waiting = {"kind": "INPUT", **dict(result.clarification or {})}
        failure = None
        for event in reversed(events):
            if event.event_type == "turn.started":
                break
            if event.event_type == "checkpoint.conflict":
                failure = _checkpoint_conflict_reason(event.payload.get("conflicts") or [])
                break
            if event.event_type == "turn.incomplete_boundary":
                failure = _incomplete_reason(event.payload)
                break
            if event.event_type.endswith(".failed") or event.event_type == "turn.interrupted":
                failure = event.payload.get("message") or failure
                if failure:
                    break
        data.update(schema_version=1, type="task.snapshot", session_id=task.session_id,
                    turn_id=turn_id, active_turn_id=turn_id, active_turn_sequence=latest.sequence if latest else 0, sequence=result.cursor,
                    waiting=waiting, failure_reason=failure,
                    final=result.phase1_state in {"INTERRUPTED", "DONE", "FAILED", "CANCELLED", "NEEDS_REVIEW"})
        cache[task_id] = (key, data)
        return dict(data)
    raise RuntimeError("Task changed while reading its UI snapshot; reconnect to retry")


async def subscribe_task_ui(client, task_id: str, *, after: int = 0, heartbeat: float = 15):
    """Replay durable events, then sleep until commits/progress wake this task.

    No raw audit payloads cross this boundary. Heartbeats do not read the DB.
    Ephemeral fragments carry no SSE id and cannot advance a durable cursor.
    """
    if after < 0:
        raise ValueError("after must be non-negative")
    store = client._store()
    cursor = after
    live_cursor = client.latest_progress_sequence(task_id)
    with store.watch(task_id) as signal:
        changed = signal.changed
        snapshot = await ui_snapshot(client, task_id)
        task = await client.application.kernel.get_task(task_id)
        names = {item.call.call_id: item.call.name for item in task.tool_executions.values()}
        approval_turns = {item.approval_request_id: item.turn_id for item in task.tool_executions.values() if item.approval_request_id}
        if cursor > snapshot["cursor"]:
            yield {"type": "stream.reset_required", "task_id": task_id, "sequence": 0}
            cursor = 0
        yield {"type": "stream.open", "task_id": task_id, "session_id": snapshot["session_id"], "active_turn_id": snapshot["active_turn_id"], "active_turn_sequence": snapshot["active_turn_sequence"]}
        initial = True
        while True:
            visible = False
            candidate = None
            while True:
                events = await store.read_events(task_id, cursor, limit=500)
                for event in events:
                    cursor = event.sequence
                    if event.event_type == "tool.requested":
                        call = event.payload.get("call") or {}
                        names[call.get("call_id")] = call.get("name")
                    if event.event_type == "approval.requested":
                        request = event.payload.get("request") or {}
                        approval_turns[request.get("request_id") or event.payload.get("request_id")] = event.turn_id or event.payload.get("turn_id")
                    progress = _turn_progress_item(event, fallback_turn_id=approval_turns.get(event.payload.get("request_id")))
                    if progress:
                        if progress["kind"] == "tool_completed":
                            progress["tool_name"] = names.get(progress.get("tool_call_id"), "")
                        visible = True
                        yield {"schema_version": 1, "type": event.event_type, "task_id": task_id,
                               "session_id": event.session_id or snapshot["session_id"], "turn_id": event.turn_id or progress.get("turn_id"),
                               "sequence": event.sequence, "progress": progress}
                    if event.event_type in {"task.state_changed", "working_memory.updated", "steering.queued", "turn.failed", "turn.incomplete_boundary"}:
                        visible = True
                if len(events) < 500:
                    if visible or initial:
                        candidate = await ui_snapshot(client, task_id)
                        # Writes may happen while the consumer sends a yielded
                        # frame. Never acknowledge a newer snapshot cursor until
                        # every event through that cursor has been delivered.
                        if candidate["cursor"] > cursor:
                            continue
                    break
            # Only live-only content comes from the transient callback channel.
            for live in client.read_progress(task_id, after=live_cursor, limit=1000):
                live_cursor = live.sequence
                if live.progress.kind.value in {"reasoning_delta", "focus", "exploration"}:
                    yield {"type": "progress.transient", "task_id": task_id,
                           "turn_id": snapshot["active_turn_id"], "progress": live.progress.to_data()}
            if visible or initial:
                snapshot = candidate
                snapshot["installations"] = installation_records(client, task_id, snapshot["session_id"])
                yield snapshot
                initial = False
                if snapshot["final"] or snapshot["waiting"] or snapshot["status"] == "awaiting_input":
                    return
            elif cursor:
                yield {"type": "stream.cursor", "task_id": task_id, "sequence": cursor}
            try:
                await asyncio.wait_for(changed.wait(), timeout=heartbeat)
            except TimeoutError:
                yield {"type": "stream.heartbeat"}
                # No database read on a timer: resume waiting on the same hint.
                while not changed.is_set():
                    try:
                        await asyncio.wait_for(changed.wait(), timeout=heartbeat)
                    except TimeoutError:
                        yield {"type": "stream.heartbeat"}
            changed = signal.changed

#: The durable events that describe how one Turn progressed. Reading only these
#: keeps whole model messages and patch bodies out of the per-Turn view.
_TURN_PROGRESS_EVENT_TYPES = (
    "turn.started",
    "model.attempt_started",
    "model.attempt_failed",
    "model.recovery_decided",
    "conversation.model_response_recorded",
    "conversation.commentary",
    "tool.requested",
    "tool.completed",
    "tool.failed",
    "turn.completed",
    "turn.interrupted",
    "turn.incomplete_boundary",
    "llm.failed",
    "turn.completion_review_completed",
    "turn.completion_review_started",
    "turn.completion_review_failed",
    "turn.completion_review_exhausted",
    "turn.completion_review_skipped",
    "turn.completion_review_bypassed",
    "approval.requested",
    "approval.resolved",
    "approval.invalidated",
    "checkpoint.conflict",
)

#: Argument keys a progress line may name as the call's target. Same rule as
#: ``_TURN_CALL_KEYS``: navigation metadata only, never command or patch bodies.
_TURN_PROGRESS_TARGET_KEYS = (
    "path", "query", "pattern", "target", "file_glob", "url",
)

#: Bound for any single string copied into a progress line.
_TURN_PROGRESS_TEXT_LIMIT = 200


def _turn_progress_item(event, *, fallback_turn_id: str | None = None) -> dict | None:
    """Normalize one durable event into a Turn progress item.

    The shapes mirror the live ``AgentProgress`` items the page already renders,
    so one renderer covers both the in-flight Turn and its durable record.
    """

    payload = event.payload if isinstance(event.payload, Mapping) else {}
    turn_id = str(
        payload.get("turn_id") or getattr(event, "turn_id", "") or fallback_turn_id or ""
    )
    if not turn_id:
        return None
    kind = event.event_type
    base: dict[str, Any] = {"turn_id": turn_id, "sequence": event.sequence}
    if kind == "turn.incomplete_boundary":
        return {**base, "kind": "turn_interrupted", "detail": _incomplete_reason(payload),
                "diagnostic_code": _bounded_text(payload.get("reason"))}
    if kind in {"turn.completion_review_started", "turn.completion_review_failed"}:
        return {**base, "kind": "completion_review",
                "review_action": "STARTED" if kind.endswith("started") else "FAILED",
                "review_reason_code": _bounded_text(payload.get("reason_code") or payload.get("error_type"))}
    if kind == "checkpoint.conflict":
        conflicts = [item[:80] for item in (payload.get("conflicts") or []) if isinstance(item, str)][:12]
        return {**base, "kind": "turn_conflict", "conflicts": conflicts,
                "diagnostic_code": "checkpoint_identity_conflict",
                "detail": _checkpoint_conflict_reason(conflicts)}
    if kind in {"approval.resolved", "approval.invalidated"}:
        return {**base, "kind": kind.replace(".", "_"),
                "request_id": str(payload.get("request_id") or ""),
                "decision": payload.get("decision"), "reason": _bounded_text(payload.get("reason"))}
    if kind == "turn.started":
        return {
            **base, "kind": "turn_started",
            "input_characters": payload.get("input_character_count"),
            "tool_exposure_profile": payload.get("tool_exposure_profile"),
            "visible_tool_count": len(payload.get("visible_tools") or ()),
        }
    if kind in {"model.attempt_started", "model.attempt_failed",
                "model.recovery_decided"}:
        event_kind = {
            "model.attempt_started": "attempt_started",
            "model.attempt_failed": "attempt_failed",
            "model.recovery_decided": "recovery_decided",
        }[kind]
        return {
            **base, "kind": "model_transport",
            "transport_event": event_kind,
            "transport_attempt": payload.get("provider_attempt"),
            "max_transport_attempts": payload.get("max_provider_attempts"),
            "transport_mode": payload.get("transport_mode"),
            "diagnostic_code": payload.get("diagnostic_code"),
            "diagnostic_detail": _bounded_text(payload.get("diagnostic_detail")),
            "recovery_action": payload.get("recovery_action"),
            "retry_delay_seconds": payload.get("delay_seconds"),
        }
    if kind == "conversation.model_response_recorded":
        return {
            **base, "kind": "model_completed",
            "model_call": payload.get("model_call"),
            "tool_call_count": payload.get("tool_call_count"),
            "finish_reason": payload.get("finish_reason"),
        }
    if kind == "conversation.commentary":
        return {
            **base, "kind": "commentary",
            "model_call": payload.get("model_call"),
            "activity": str(payload.get("text") or ""),
            "phase": str(payload.get("source") or "model"),
        }
    if kind == "tool.requested":
        call = payload.get("call")
        call = call if isinstance(call, Mapping) else {}
        arguments = call.get("arguments")
        arguments = arguments if isinstance(arguments, Mapping) else {}
        target = next(
            (
                str(arguments[key]).strip()
                for key in _TURN_PROGRESS_TARGET_KEYS
                if isinstance(arguments.get(key), str)
                and str(arguments[key]).strip()
            ),
            "",
        )
        return {
            **base, "kind": "tool_started",
            "tool_name": str(call.get("name") or ""),
            "tool_call_id": str(call.get("call_id") or ""),
            "target": target[:_TURN_PROGRESS_TEXT_LIMIT],
        }
    if kind in {"tool.completed", "tool.failed"}:
        result = payload.get("result")
        result = result if isinstance(result, Mapping) else {}
        return {
            **base, "kind": "tool_completed",
            "tool_call_id": str(result.get("call_id") or ""),
            "ok": bool(result.get("ok")),
            "error_code": result.get("error_code"),
        }
    if kind == "turn.completed":
        return {
            **base, "kind": "turn_completed",
            "model_calls": payload.get("model_calls"),
            "tool_calls": payload.get("tool_calls"),
        }
    if kind == "turn.interrupted":
        return {
            **base, "kind": "turn_interrupted",
            "diagnostic_code": payload.get("failure_category"),
            "detail": _bounded_text(payload.get("message")),
        }
    if kind == "approval.requested":
        request = payload.get("request")
        request = request if isinstance(request, Mapping) else {}
        return {
            **base,
            "kind": "waiting",
            "waiting_kind": "approval",
            "action": _bounded_text(request.get("action") or payload.get("action")),
            "request_id": str(
                request.get("request_id") or payload.get("request_id") or ""
            ),
        }
    if kind == "turn.completion_review_completed":
        return {
            **base, "kind": "completion_review",
            "review_action": payload.get("action"),
            "review_reason_code": payload.get("reason_code"),
        }
    if kind == "turn.completion_review_exhausted":
        return {
            **base, "kind": "completion_review",
            "review_action": "EXHAUSTED",
            "review_reason_code": payload.get("reason_code"),
        }
    if kind == "turn.completion_review_skipped":
        return {
            **base, "kind": "completion_review",
            "review_action": "SKIPPED",
            "review_reason_code": payload.get("reason_code"),
        }
    if kind == "turn.completion_review_bypassed":
        return {
            **base, "kind": "completion_review",
            "review_action": "UNVERIFIED",
            "review_reason_code": payload.get("reason_code"),
        }
    if kind == "llm.failed":
        # A recoverable stop also writes ``turn.interrupted``; that one line is
        # the whole story, so the duplicate is dropped.
        if payload.get("recoverable"):
            return None
        return {
            **base, "kind": "turn_failed",
            "error_type": payload.get("error_type"),
            "diagnostic_code": payload.get("failure_category"),
            "detail": _bounded_text(payload.get("message")),
        }
    return None


def _bounded_text(value: Any) -> str:
    text = str(value).strip() if value is not None else ""
    return text[:_TURN_PROGRESS_TEXT_LIMIT]


def _incomplete_reason(payload) -> str:
    reason = str(payload.get("reason") or "unknown")
    if reason == "completion_reviewer_unavailable":
        return "完成复核失败，最终答复尚未发布；本轮保留为未完成，可继续重试复核。"
    return f"本轮未完成（{_bounded_text(reason)}），执行进度已保留。"



def _checkpoint_conflict_reason(conflicts) -> str:
    keys = ", ".join(item[:80] for item in conflicts[:12] if isinstance(item, str))
    return f"执行检查点冲突（{keys or 'checkpoint_identity_conflict'}），本轮已停止，不能继续使用旧检查点或旧审批。"
