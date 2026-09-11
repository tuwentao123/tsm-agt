"""Bootstrap diagnostics and deterministic task, turn, and tool demos."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import signal
import sys
from collections import deque
from collections.abc import Callable
from uuid import uuid4

from tsm_agt.bootstrap import (
    compose_fixture_agent_application,
    compose_fixture_application,
    compose_local_flow_query_application,
    compose_local_project_control_application,
    compose_openai_compatible_engineering_application_from_env,
    compose_readonly_application,
)
from tsm_agt.bootstrap.model_configuration import ModelConfigurationError
from tsm_agt.cli_input import platform_line_input
from tsm_agt.cli_setup import initialize_workspace, run_doctor
from tsm_agt import __version__
from tsm_agt.local_api import LocalEventApiServer
from tsm_agt.core import (
    AgentLoopLimitExceeded,
    AgentProgress,
    AgentProgressKind,
    AgentTurnResult,
    AgentTurnSuspended,
    AgentClarificationSuspended,
    AgentContinuationSuspended,
    ApprovalDecision,
    FlowNode,
    FlowNodeDiagnostic,
    FlowNodeKind,
    FlowNodeStatus,
    FlowProjection,
    FlowProjectionView,
    FlowReplayBoundary,
    FlowReplayIndex,
    FlowReplayPlayback,
    FlowReplaySnapshot,
    FlowReplaySpeed,
    FollowUpMode,
    ProjectTrustLevel,
    ProjectOnboardingSnapshot,
    RuntimeInputIntent,
    SessionContinuationMode,
    SessionResumeSafety,
    SessionInputAction,
    SessionChoiceAction,
    ModelInvocationFailed,
    SteeringKind,
    TaskState,
    build_flow_export_document,
)


def _render_evidence_level(assessment) -> str:
    labels = {
        "none": "无可用证据",
        "indirect": "间接证据",
        "direct": "直接证据",
        "verified": "已验证",
        "blocked": "受阻",
    }
    return (
        f"evidence: {assessment.level.value} "
        f"({labels.get(assessment.level.value, assessment.level.value)}; "
        f"items={assessment.evidence_count}; "
        f"verified_criteria={assessment.verified_criteria})"
    )


def _render_investigation_status(status) -> tuple[str, ...]:
    category_labels = {
        "new_paths": "路径", "new_symbols": "符号",
        "new_relations": "关系", "new_facts": "事实",
        "new_exclusions": "排除项", "resolved_questions": "已解问题",
        "new_verification": "验证",
    }
    value_labels = {"useful": "有效", "low": "低收益", "ignored": "不计分"}
    decision_labels = {
        "CONTINUE": "继续当前路线", "READ_HITS": "先读候选",
        "NARROW_SCOPE": "缩小范围", "CHANGE_METHOD": "换一种方法",
        "ASK_USER": "询问用户",
        "SUMMARIZE_WITH_EVIDENCE": "基于证据收尾",
        "STOP_NO_PROGRESS": "停止无进展探索",
    }
    question = f"Q#{status.question_ref}" if status.question_ref else "暂无"
    evidence_parts = [
        f"{category_labels[key]} {value}"
        for key, value in status.evidence_counts.items() if value > 0
    ]
    lines = [
        f"investigation: question={question}; evidence={status.evidence_total}; "
        f"zero_delta_streak={status.consecutive_zero_delta}",
        "evidence detail: " + ("; ".join(evidence_parts) if evidence_parts else "暂无"),
    ]
    score = str(status.budget_score) if status.budget_score is not None else "暂无"
    value = value_labels.get(status.value_band, status.value_band or "暂无")
    lines.append(
        f"exploration budget: score={score} ({value}); "
        f"scored_actions={status.scored_actions}; "
        f"low_value_streak={status.low_value_streak}; "
        f"tool_time_ms={status.cumulative_tool_milliseconds}"
    )
    decision = decision_labels.get(
        status.latest_decision, status.latest_decision or "暂无"
    )
    lines.append(f"route: {decision}")
    return tuple(lines)


async def _print_chat_status(application, session, task_id, output_fn) -> None:
    task = await application.kernel.get_task(task_id)
    steering = await application.kernel.get_steering(task_id)
    checkpoint = await application.kernel.get_session_active_checkpoint(
        session.session_id
    )
    output_fn(
        f"status: session={session.session_id} task={task.task_id} "
        f"state={task.state.value}"
    )
    try:
        investigation = await application.kernel.get_investigation_status(task_id)
    except LookupError:
        output_fn("investigation: unavailable in this Runtime composition")
    else:
        for line in _render_investigation_status(investigation):
            output_fn(line)
    output_fn(
        "activity: "
        f"model_calls={checkpoint.model_calls if checkpoint else 0}"
        f"/{checkpoint.max_model_calls if checkpoint else 0} "
        f"tool_calls={checkpoint.tool_calls if checkpoint else 0}"
        f"/{checkpoint.max_tool_calls if checkpoint else 0} "
        f"pending_input={len(steering.pending)}"
    )
    if checkpoint is not None:
        output_fn("continuation: " + checkpoint.continuation)
        if checkpoint.pending_tools:
            output_fn("pending tools: " + ", ".join(checkpoint.pending_tools))
    if task.pending_approval is not None:
        output_fn("waiting: explicit approval (ordinary text cannot approve)")
    elif task.pending_clarification is not None:
        output_fn("waiting: clarification answer")
    elif (
        checkpoint is not None
        and checkpoint.pending_user_action is not None
        and checkpoint.pending_user_action.get("kind") == "CONTINUATION"
    ):
        output_fn(
            "waiting: completed unit continuation (ordinary input is "
            "semantically routed; it grants no approval)"
        )


async def _print_chat_plan(application, task_id, output_fn) -> None:
    effective = await application.kernel.get_effective_working_memory(task_id)
    memory = effective.snapshot
    output_fn(f"goal: {memory.goal}")
    if not memory.plan:
        output_fn("plan: not published yet")
    for index, step in enumerate(memory.plan, 1):
        output_fn(
            f"{index}. [{step.status.value}] {step.description} "
            f"(done when: {step.completion_criteria})"
        )
    if effective.remaining_work:
        output_fn("remaining: " + "; ".join(effective.remaining_work))


async def _print_chat_spec(application, task_id, output_fn) -> None:
    spec = await application.kernel.get_task_spec(task_id)
    output_fn(f"Task SPEC r{spec.revision}: {spec.goal}")
    if spec.scope:
        output_fn("scope: " + "; ".join(spec.scope))
    if spec.constraints:
        output_fn("constraints: " + "; ".join(spec.constraints))
    output_fn(f"continuation: {spec.continuation_mode.value}")
    if not spec.outcomes:
        output_fn("outcomes: legacy Task (not planned yet)")
    for index, outcome in enumerate(spec.outcomes, 1):
        effects = ",".join(
            effect.value for effect in outcome.required_effects
        ) or "conversation"
        output_fn(
            f"outcome {index}: [{outcome.status.value}] "
            f"{outcome.description} ({outcome.kind.value}; "
            f"effects={effects}; proofs={len(outcome.fulfillment_refs)})"
        )
    for index, criterion in enumerate(spec.acceptance_criteria, 1):
        reference = (
            f" [{criterion.evidence_reference}]"
            if criterion.evidence_reference else ""
        )
        output_fn(
            f"acceptance {index}: {criterion.description} "
            f"({criterion.verification_kind.value}){reference}"
        )


async def _print_chat_diff(application, task_id, output_fn) -> None:
    changes = await application.kernel.get_workspace_changes(task_id)
    output_fn(
        f"diff: +{len(changes.added)} ~{len(changes.modified)} "
        f"-{len(changes.deleted)}"
    )
    for marker, paths in (
        ("+", changes.added), ("~", changes.modified),
        ("-", changes.deleted),
    ):
        for path in paths:
            output_fn(f"{marker} {path}")
    if changes.scan_truncated or changes.skipped_files:
        output_fn(
            f"scan: truncated={changes.scan_truncated} "
            f"skipped={changes.skipped_files}"
        )


async def _print_chat_permissions(application, root, output_fn) -> None:
    trust = await application.kernel.get_project_trust(root)
    output_fn(f"project trust: {trust.level.value}")
    output_fn("workspace read/search: allowed by built-in read tools")
    output_fn("workspace writes: Policy + Sandbox + mutation journal")
    output_fn(
        "build/test execution: " + (
            "eligible (each command is still checked)"
            if trust.level.allows_project_execution
            else "blocked until TRUSTED_BUILD"
        )
    )
    output_fn("approval: exact high-risk action must be approved explicitly")


async def _handle_active_chat_command(
    command, application, session, task_id, root, output_fn,
) -> bool:
    if command == "/status":
        await _print_chat_status(application, session, task_id, output_fn)
    elif command == "/plan":
        await _print_chat_plan(application, task_id, output_fn)
    elif command == "/diff":
        await _print_chat_diff(application, task_id, output_fn)
    elif command == "/permissions":
        await _print_chat_permissions(application, root, output_fn)
    elif command == "/spec":
        await _print_chat_spec(application, task_id, output_fn)
    else:
        return False
    return True


def _chat_error_guidance(error: Exception, root: Path) -> tuple[str, ...]:
    """Turn common Runtime failures into short, actionable recovery text."""
    if (
        isinstance(error, ModelInvocationFailed)
        and error.failure_kind == "tool_protocol"
    ):
        return (
            "recovery: the model connection succeeded, but its tool plan did not "
            "match the current Task contract after one automatic correction; "
            "use /flow to inspect the rejected tool and outcome mapping",
        )
    if (
        isinstance(error, ModelInvocationFailed)
        and error.recovery_action
        is ModelRecoveryAction.PRESERVE_AND_INTERRUPT
    ):
        return (
            "recovery: automatic model recovery was exhausted or unsafe; the "
            "safe checkpoint and collected evidence were preserved, so use "
            "/resume or enter a continuation request to continue from that point",
        )
    if isinstance(error, ModelInvocationFailed):
        if error.failure_category is ModelFailureCategory.AUTHENTICATION:
            return (
                "recovery: model authentication or authorization failed; review "
                "the private model credentials and endpoint permissions",
            )
        if error.failure_category is ModelFailureCategory.CONFIGURATION:
            return (
                "recovery: the model request or Provider configuration is invalid; "
                "review the private model endpoint and compatibility settings",
            )
        if error.failure_category is ModelFailureCategory.CONTEXT_LIMIT:
            return (
                "recovery: the model context limit was reached; inspect /flow and "
                "reduce or compact the active context before resuming",
            )
    message = str(error).lower()
    if not isinstance(error, ModelInvocationFailed) and any(word in message for word in (
        "connection", "connect", "timeout",
    )):
        return (
            "recovery: check the model service and run "
            f"tsm-agt doctor --model-check --workspace {root}",
        )
    if any(word in message for word in (
        "permission_denied", "project trust", "untrusted",
    )):
        return (
            "recovery: review the command, then enable build/test execution "
            "with tsm-agt trust set TRUSTED_BUILD --workspace " + str(root),
        )
    if "approval" in message:
        return (
            "recovery: inspect the exact pending action and approve or reject it; "
            "ordinary chat text never grants approval",
        )
    if "clarification" in message:
        return (
            "recovery: answer the pending question in Chat or with tsm-agt answer",
        )
    return (
        "recovery: use /status and /flow to inspect the last safe state; "
        "the Session and Task remain persisted",
    )


async def _resolve_session_input_with_progress(
    application, session_id: str, prompt: str, root: Path, output_fn,
    *, heartbeat_seconds: float = 5.0,
):
    """Run optional semantic routing with bounded, visible waiting."""
    output_fn("[会话] 正在识别这条输入与未完成任务的关系……")
    pending = asyncio.create_task(
        application.kernel.resolve_session_input(session_id, prompt, root)
    )
    elapsed = 0
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=heartbeat_seconds)
            if done:
                return pending.result()
            elapsed += heartbeat_seconds
            output_fn(
                f"[会话] 语义识别仍在进行（{elapsed:g} 秒）；"
                "可按 Ctrl+C 取消，不会修改或恢复任何 Task。"
            )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        raise
from tsm_agt.ports import (
    FlowArtifactExportPort, ReplayCursor, ReplayCursorStorePort,
    RuntimeStorePort, ToolCall, ModelFailureCategory, ModelRecoveryAction,
)


async def _task_demo(goal: str, workspace: Path) -> int:
    application = compose_fixture_application()
    await application.registry.start_all()
    try:
        task = await application.kernel.create_task(goal, workspace)
        route = (
            (TaskState.INTAKE, "normalize user request"),
            (TaskState.RESOLVING_PROJECT, "inspect workspace identity"),
            (TaskState.SELECTING_EXTENSIONS, "select available capabilities"),
            (TaskState.ROUTING, "choose deterministic execution path"),
            (TaskState.PLANNING, "build task plan"),
            (TaskState.EXECUTING, "execute planned work"),
            (TaskState.VERIFYING, "verify acceptance criteria"),
            (TaskState.FINALIZING, "prepare final result"),
            (TaskState.SUCCEEDED, "all criteria passed"),
        )
        for target, reason in route:
            task = await application.kernel.transition_task(task.task_id, target, reason)

        store = application.registry.require(RuntimeStorePort)
        print(f"task {task.task_id}: {task.state.value}")
        for event in await store.read_events(task.task_id):
            if event.event_type == "task.created":
                detail = event.payload["goal"]
            elif event.event_type == "config.snapshot":
                detail = (
                    f"revision {event.payload['revision']}: "
                    f"{event.payload['effective_config_hash']}"
                )
            else:
                detail = (
                    f"{event.payload['previous_state']} -> "
                    f"{event.payload['next_state']}: {event.payload['reason']}"
                )
            print(f"{event.sequence:02d} {event.event_type}: {detail}")
    finally:
        await application.registry.stop_all()
    return 0


async def _turn_demo(prompt: str, workspace: Path) -> int:
    application = compose_fixture_application()
    await application.registry.start_all()
    try:
        task = await application.kernel.create_task("single text turn", workspace)
        for target in (
            TaskState.INTAKE,
            TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS,
            TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await application.kernel.transition_task(
                task.task_id, target, f"prepare {target.value.lower()}"
            )
        result = await application.kernel.run_text_turn(task.task_id, prompt)
        print(f"assistant: {result.assistant_message.text}")
        print(
            f"usage: input={result.usage.input_tokens}, "
            f"output={result.usage.output_tokens}"
        )
        store = application.registry.require(RuntimeStorePort)
        for event in await store.read_events(task.task_id):
            print(f"{event.sequence:02d} {event.event_type}")
    finally:
        await application.registry.stop_all()
    return 0


async def _tool_demo(text: str, workspace: Path) -> int:
    application = compose_fixture_application()
    await application.registry.start_all()
    try:
        task = await application.kernel.create_task("single tool call", workspace)
        for target in (
            TaskState.INTAKE,
            TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS,
            TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await application.kernel.transition_task(
                task.task_id, target, f"prepare {target.value.lower()}"
            )
        tools = await application.kernel.list_tools()
        print("tools: " + ", ".join(tool.name for tool in tools))
        result = await application.kernel.invoke_tool(
            task.task_id,
            "turn-tool-demo",
            ToolCall("call-1", "fixture.echo", {"text": text}),
        )
        print(f"result: {result.data}")
        store = application.registry.require(RuntimeStorePort)
        for event in await store.read_events(task.task_id):
            print(f"{event.sequence:02d} {event.event_type}")
    finally:
        await application.registry.stop_all()
    return 0


async def _agent_demo(prompt: str, workspace: Path) -> int:
    application = compose_fixture_agent_application()
    await application.registry.start_all()
    try:
        task = await application.kernel.create_task("bounded Agent loop", workspace)
        for target in (
            TaskState.INTAKE,
            TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS,
            TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await application.kernel.transition_task(
                task.task_id, target, f"prepare {target.value.lower()}"
            )
        result = await application.kernel.run_agent_turn(
            task.task_id, prompt, on_progress=_print_standalone_agent_progress
        )
        _print_agent_result(result)
        store = application.registry.require(RuntimeStorePort)
        for event in await store.read_events(task.task_id):
            print(f"{event.sequence:02d} {event.event_type}")
    finally:
        await application.registry.stop_all()
    return 0


async def _files_demo(path: str, workspace: Path, recursive: bool) -> int:
    application = compose_readonly_application()
    await application.registry.start_all()
    try:
        task = await application.kernel.create_task("inspect workspace files", workspace)
        for target in (
            TaskState.INTAKE,
            TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS,
            TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await application.kernel.transition_task(
                task.task_id, target, f"prepare {target.value.lower()}"
            )
        result = await application.kernel.invoke_tool(
            task.task_id,
            "turn-files-demo",
            ToolCall(
                "call-files-demo",
                "core.list_files",
                {"path": path, "recursive": recursive, "limit": 200},
            ),
        )
        if not result.ok:
            print(f"error: {result.error_code}: {result.message}")
            return 1
        for entry in result.data["entries"]:
            print(f"{entry['type']:<9} {entry['size']:>8} {entry['path']}")
        print(
            f"listed={len(result.data['entries'])}, "
            f"omitted_sensitive={result.data['omitted_sensitive']}, "
            f"truncated={result.truncated}"
        )
    finally:
        await application.registry.stop_all()
    return 0


async def _agent(
    prompt: str, workspace: Path, *, verbose_progress: bool = False,
) -> int:
    root = workspace.expanduser().resolve()
    application = compose_openai_compatible_engineering_application_from_env(
        env_file=root / ".env",
        database_path=root / ".agent" / "runtime.db",
    )
    await application.registry.start_all()
    try:
        task = await application.kernel.create_task(prompt, root)
        for target in (
            TaskState.INTAKE,
            TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS,
            TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await application.kernel.transition_task(
                task.task_id, target, f"prepare {target.value.lower()}"
            )
        result = await application.kernel.run_agent_turn(
            task.task_id, prompt,
            on_progress=lambda item: _print_standalone_agent_progress(
                item, verbose=verbose_progress
            ),
        )
        _print_agent_result(result)
        return await _finalize_standalone_agent_result(
            application, task.task_id, result
        )
    finally:
        await application.registry.stop_all()


async def _finalize_standalone_agent_result(
    application, task_id: str, result,
    output_fn: Callable[[str], None] = print,
) -> int:
    """Give every one-shot continuation the same trusted Task ending."""
    if not isinstance(result, AgentTurnResult):
        return 0
    task = await application.kernel.get_task(task_id)
    task = await application.kernel.transition_task(
        task.task_id, TaskState.VERIFYING, "standalone verifier started"
    )
    verification = await application.kernel.verify_task_acceptance(task.task_id)
    if verification.evidence_level is not None:
        output_fn(_render_evidence_level(verification.evidence_level))
    for criterion in verification.criteria:
        output_fn(
            f"verification criterion: {criterion.criterion_id} "
            f"{criterion.status.value}"
        )
    if verification.passed:
        for target in (TaskState.FINALIZING, TaskState.SUCCEEDED):
            task = await application.kernel.transition_task(
                task.task_id, target, f"standalone {target.value.lower()}",
            )
        output_fn(
            f"verification: passed ({len(verification.criteria)} criteria)"
        )
        return 0
    await application.kernel.transition_task(
        task.task_id, TaskState.FAILED,
        f"trusted verifier {verification.status.value}",
    )
    output_fn(
        f"verification: {verification.status.value}; "
        "Task was not marked successful"
    )
    return 1


def _render_exploration_progress(progress: AgentProgress) -> tuple[str, ...]:
    """Render one completed investigation step, not every lifecycle event.

    TOOL_STARTED/TOOL_COMPLETED already show execution lifecycle.  The
    EXPLORATION projection is useful after completion because it adds evidence
    gain, scope movement and the next decision; planned/decision projections
    would otherwise repeat the same goal, question and arguments.
    """
    if progress.phase != "tool_result":
        return ()
    activities = {
        "inspect_structure": "查看项目结构",
        "search_concept": "搜索相关概念",
        "search_definition": "查找定义",
        "search_references": "查找引用",
        "read_artifact": "读取候选文件",
        "inspect_configuration": "检查配置",
        "mutate_workspace": "修改工作区",
        "verify": "执行验证",
        "run_command": "执行命令",
        "control_process": "管理后台进程",
        "ask_user": "请求用户补充信息",
        "manage_state": "更新任务状态",
        "use_tool": "使用工具",
    }
    scopes = {
        "workspace": "整个工作区", "directory": "目录", "file": "文件",
        "symbol": "代码符号", "external": "外部资源",
        "internal": "Agent 内部状态", "unknown": "未分类",
    }
    changes = {
        "first": "首次定位", "same": "范围不变", "narrowed": "已收窄",
        "expanded": "已扩大", "lateral": "横向切换", "unknown": "未分类",
    }
    actions = {
        "CONTINUE": "继续当前路线", "READ_HITS": "先读取已命中的候选",
        "NARROW_SCOPE": "保持或缩小搜索范围",
        "CHANGE_METHOD": "当前路线收益低，换一种方法",
        "ASK_USER": "向用户确认缺失信息",
        "SUMMARIZE_WITH_EVIDENCE": "基于现有证据整理结论",
        "STOP_NO_PROGRESS": "停止无进展探索并说明缺口",
    }
    parts = [
        f"动作：{activities.get(progress.activity, '使用工具')}",
        (
            f"范围：{progress.scope_target}"
            if progress.scope_target else
            f"范围：{scopes.get(progress.scope, '未分类')}"
        ) + f"（{changes.get(progress.scope_change, '未分类')}）",
    ]
    if progress.question:
        parts.insert(0, f"当前要确认：{progress.question}")
    elif progress.question_ref:
        parts.insert(0, f"问题：Q#{progress.question_ref}")
    lines = ["[调查结果] " + "；".join(parts)]
    if progress.scope_change_reason:
        lines.append(
            f"[调查] 调整范围的原因：{progress.scope_change_reason}"
        )
    result_parts = []
    if progress.evidence_delta is not None:
        result_parts.append(f"新增证据 {progress.evidence_delta} 项")
        if progress.evidence_delta == 0:
            result_parts.append(f"连续零增量 {progress.consecutive_zero_delta} 次")
    if progress.budget_score is not None:
        value = {"useful": "有效", "low": "低收益", "ignored": "不计分"}.get(
            progress.value_band, progress.value_band or "未分类"
        )
        result_parts.append(f"价值评分 {progress.budget_score}（{value}）")
    if result_parts:
        lines.append("[调查结果] " + "；".join(result_parts))
    budget_parts = []
    budget_near_limit = any((
        progress.max_exploration_actions > 0
        and progress.exploration_actions >= 0.8 * progress.max_exploration_actions,
        progress.max_exploration_tool_calls > 0
        and progress.exploration_tool_calls >= 0.8 * progress.max_exploration_tool_calls,
        progress.max_exploration_elapsed_seconds > 0
        and progress.exploration_elapsed_seconds
        >= 0.8 * progress.max_exploration_elapsed_seconds,
        progress.max_low_value_streak > 0
        and progress.low_value_streak > 0,
    ))
    if budget_near_limit:
        if progress.max_exploration_actions:
            budget_parts.append(
                f"探索动作 {progress.exploration_actions}/"
                f"{progress.max_exploration_actions}"
            )
        if progress.max_exploration_tool_calls:
            budget_parts.append(
                f"探索工具调用 {progress.exploration_tool_calls}/"
                f"{progress.max_exploration_tool_calls}"
            )
        if progress.max_exploration_elapsed_seconds:
            budget_parts.append(
                f"探索工具耗时 {progress.exploration_elapsed_seconds:.1f}/"
                f"{progress.max_exploration_elapsed_seconds:.0f} 秒"
            )
        if progress.max_low_value_streak:
            budget_parts.append(
                f"连续低收益 {progress.low_value_streak}/"
                f"{progress.max_low_value_streak}"
            )
        lines.append("[调查进度] " + "；".join(budget_parts))
    if progress.next_action and progress.next_action != "CONTINUE":
        lines.append(
            "[调查结果] 下一步："
            + actions.get(progress.next_action, progress.next_action)
        )
    return tuple(lines)


def _compact_tool_presentation(progress: AgentProgress) -> str:
    """Keep ordinary tool arguments readable on one terminal line.

    Patch previews are intentionally left intact because they are part of the
    write-approval decision. Other tools only need a concise description in
    the default view; verbose progress retains the full presentation.
    """
    rendered = progress.operation_presentation
    if not rendered and progress.operation_arguments:
        rendered = json.dumps(
            dict(progress.operation_arguments), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )
    if progress.tool_name in {"core.apply_patch", "core.apply_patches"}:
        return rendered
    compact = " ".join(rendered.split())
    return compact if len(compact) <= 360 else compact[:359] + "…"


def _render_agent_progress_lines(
    progress: AgentProgress, *, verbose: bool = False,
) -> tuple[str, ...]:
    """Render one live event without repeating lifecycle details."""
    if progress.kind is AgentProgressKind.MODEL_STARTED:
        counter = (
            f"{progress.model_call}/{progress.max_model_calls}"
            if verbose else str(progress.model_call)
        )
        return (f"[思考 {counter}] 模型正在分析…",)
    if progress.kind is AgentProgressKind.TOOL_STARTED:
        counter = (
            f"{progress.tool_call}/{progress.max_tool_calls}"
            if verbose else str(progress.tool_call)
        )
        heading = f"[操作 {counter}] {progress.tool_name}"
        rendered = (
            progress.operation_presentation
            if verbose else _compact_tool_presentation(progress)
        )
        if not rendered and progress.operation_arguments:
            rendered = json.dumps(
                dict(progress.operation_arguments), ensure_ascii=False,
                sort_keys=True, separators=(",", ":"),
            )
        return (heading + (f"：{rendered}" if rendered else ""),)
    if progress.kind is AgentProgressKind.TOOL_COMPLETED:
        status = "完成" if progress.ok else (
            f"失败 {progress.error_code or 'TOOL_ERROR'}"
        )
        details = [f"{progress.elapsed_seconds:.1f} 秒"]
        if progress.evidence_delta is not None:
            if progress.evidence_delta:
                details.append(f"新增证据 {progress.evidence_delta} 项")
            else:
                details.append("没有新增证据")
                if verbose:
                    details.append(
                        f"连续 {progress.consecutive_zero_delta} 次零增量"
                    )
        return (
            f"[完成 {progress.tool_call}] {progress.tool_name}：{status}（"
            + "；".join(details) + "）",
        )
    if progress.kind is AgentProgressKind.EXPLORATION:
        if verbose:
            return _render_exploration_progress(progress)
        if progress.phase != "tool_result":
            return ()
        actions = {
            "READ_HITS": "先读取已经找到的候选文件",
            "NARROW_SCOPE": "缩小搜索范围",
            "CHANGE_METHOD": "当前路线收益低，换一种查法",
            "ASK_USER": "需要向用户确认缺失信息",
            "SUMMARIZE_WITH_EVIDENCE": "证据已足够，开始整理结论",
            "STOP_NO_PROGRESS": "停止重复探索并说明证据缺口",
        }
        decision = actions.get(progress.next_action)
        if decision:
            return (f"[路线调整] {decision}。",)
        if progress.scope_change in {"expanded", "lateral"}:
            target = progress.scope_target or progress.scope
            reason = (
                f"；原因：{progress.scope_change_reason}"
                if progress.scope_change_reason else ""
            )
            return (f"[范围调整] 转到 {target}{reason}。",)
        return ()
    if progress.kind is AgentProgressKind.WRAP_UP:
        reasons = {
            "consecutive_low_value": "连续低收益达到上限",
            "total_tool_action_limit": "探索工具调用达到上限",
            "exploration_action_limit": "探索动作达到上限",
            "cumulative_tool_time": "探索工具累计耗时达到上限",
            "tool_budget_reserve": "需要预留工具调用给验证和收尾",
            "model_budget_reserve": "需要预留最后一次模型调用来回答",
            "plan_no_progress": "重复操作没有带来新进展",
            "execution_budget_nearly_exhausted": (
                "模型或工具总调用接近本次 Task 上限"
            ),
            "exploration_budget": "探索策略要求基于现有证据收尾",
        }
        code = progress.budget_reason or progress.reason
        lines = [
            "[收尾] " + reasons.get(code, code or "已满足收尾条件") + "。"
        ]
        if verbose:
            lines.append(
                "[预算] "
                f"模型 {max(0, progress.model_call - 1)}/{progress.max_model_calls}；"
                f"工具 {progress.tool_call}/{progress.max_tool_calls}；"
                f"探索动作 {progress.exploration_actions}/"
                f"{progress.max_exploration_actions or '?'}；"
                f"连续低收益 {progress.low_value_streak}/"
                f"{progress.max_low_value_streak or '?'}"
            )
        return tuple(lines)
    if progress.kind is AgentProgressKind.MODEL_RETRY:
        if progress.reason.startswith("stream_fallback:"):
            return (
                "[兼容] 模型流式响应没有完整收尾，正在自动改用"
                "非流式完整请求。已完成的工具结果不会重跑。",
            )
        detail = (
            f"（网络尝试 {progress.transport_attempt}/"
            f"{progress.max_transport_attempts}；{progress.reason}"
            + (
                f"；{progress.retry_delay_seconds:g} 秒后重试"
                if progress.retry_delay_seconds else ""
            ) + "）"
        )
        return (
            "[重试] 模型连接临时异常，正在自动重试"
            + detail + "。已完成的工具结果不会重跑。",
        )
    if progress.kind is AgentProgressKind.FOCUS:
        lines = [
            "[聚焦] 停止全仓宽泛搜索，继续读取已定位文件和小范围证据。"
        ]
        if verbose:
            lines.append(f"[聚焦] 触发原因：{progress.reason}。")
        return tuple(lines)
    return ()


def _print_standalone_agent_progress(
    progress: AgentProgress, output_fn: Callable[[str], None] = print,
    *, verbose: bool = False,
) -> None:
    """Render concise progress for one-shot Agent continuation commands."""
    for line in _render_agent_progress_lines(progress, verbose=verbose):
        output_fn(line)


async def _chat(
    workspace: Path, session_id: str | None = None, title: str | None = None,
    *, input_fn: Callable[[str], str] | None = None,
    output_fn: Callable[[str], None] = print, application_factory=None,
    progress_renderer: Callable[[AgentProgress], tuple[str, ...]] | None = None,
    verbose_progress: bool = False,
) -> int:
    """Run a durable multi-turn terminal conversation over isolated Tasks."""
    render_progress = progress_renderer or _render_exploration_progress
    interactive_terminal = input_fn is None
    terminal_input = platform_line_input() if interactive_terminal else None
    live_input_supported = bool(
        terminal_input is not None
        and getattr(terminal_input, "supports_live_input", False)
    )

    async def read_input(prompt: str) -> str:
        if terminal_input is not None:
            return await terminal_input.read(prompt)
        assert input_fn is not None
        return input_fn(prompt)
    previous_sigint = None
    if interactive_terminal:
        try:
            previous_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, signal.default_int_handler)
        except (AttributeError, ValueError):
            # Signal handlers can only be changed by the main thread. Injected
            # input used by tests and embedded callers does not require this path.
            previous_sigint = None
    root = workspace.expanduser().resolve()
    factory = application_factory or (lambda: (
        compose_openai_compatible_engineering_application_from_env(
            env_file=root / ".env",
            database_path=root / ".agent" / "runtime.db",
        )
    ))
    application = factory()
    await application.registry.start_all()
    output_context = (
        terminal_input.output_context()
        if terminal_input is not None
        and hasattr(terminal_input, "output_context")
        else None
    )
    if output_context is not None:
        output_context.__enter__()
    try:
        if session_id is None:
            session = await application.kernel.create_session(
                title or "Interactive engineering chat"
            )
        else:
            session = await application.kernel.get_session(session_id)
            if session.state.value != "ACTIVE":
                raise ValueError(
                    f"Session is {session.state.value}; only ACTIVE Sessions can chat"
                )
        output_fn(f"session: {session.session_id}")
        output_fn(
            "commands: /status, /plan, /spec, /diff, /permissions, /session, "
            "/sessions, /use, /tasks, /resume, /new, /flow, /followup, /queued, /exit"
        )
        output_fn(
            "follow-up mode: AUTO "
            "(ordinary messages are resolved semantically; /followup changes it)"
        )
        trust = await application.kernel.get_project_trust(root)
        visible_tool_names = {tool.name for tool in await application.kernel.list_tools()}
        if "core.run_command" in visible_tool_names:
            output_fn(f"workspace trust: {trust.level.value}")
            if not trust.level.allows_project_execution:
                output_fn(
                    "read/search: enabled; project build/test commands: disabled"
                )
                output_fn(
                    "Trust is separate from file ownership: it authorizes executing "
                    "code from this workspace."
                )
                try:
                    answer = (await read_input(
                        "trust this workspace for build/test commands? [y/N] "
                    )).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    answer = ""
                if answer in {"y", "yes"}:
                    trust = await application.kernel.set_project_trust(
                        root, ProjectTrustLevel.TRUSTED_BUILD
                    )
                    output_fn(
                        "workspace trust: TRUSTED_BUILD; each concrete command "
                        "still passes Policy, approval and Sandbox checks"
                    )
                else:
                    output_fn(
                        "continuing read-only; enable later with: tsm-agt trust set "
                        f"TRUSTED_BUILD --workspace {root}"
                    )
        deferred_inputs: deque[str] = deque()
        follow_up_mode = FollowUpMode.AUTO
        while True:
            queued_follow_up = None
            try:
                ready_follow_ups = (
                    await application.kernel.list_queued_session_follow_ups(
                        session.session_id, ready_only=True
                    )
                )
                if ready_follow_ups:
                    queued_follow_up = ready_follow_ups[0]
                    raw = queued_follow_up.text
                    output_fn(
                        f"[队列] 开始处理后续消息 "
                        f"{queued_follow_up.inbound_sequence}：{raw}"
                    )
                else:
                    raw = (
                        deferred_inputs.popleft() if deferred_inputs
                        else await read_input("you> ")
                    )
            except (EOFError, KeyboardInterrupt):
                raw = "/exit"
            prompt = raw.strip()
            if not prompt:
                continue
            explicit_resume_task: str | None = None
            explicit_new_task = False
            explicit_runtime_intent: RuntimeInputIntent | None = None
            invalid_runtime_command = False
            if prompt in {"/exit", "/quit"}:
                output_fn(
                    f"session saved: {session.session_id} "
                    f"(resume with: tsm-agt chat --session {session.session_id})"
                )
                return 0
            if prompt == "/help":
                output_fn(
                    "Enter a request to run one Agent Task. While it runs, type "
                    "ordinary language using AUTO semantic routing, or use "
                    "/steer, /queue, /redirect and /interrupt. /followup shows or "
                    "changes the mode. /status, /plan, /diff and "
                    "/spec, /permissions are read-only; /sessions and /use switch "
                    "durable conversations; /exit saves and leaves."
                )
                continue
            if prompt == "/followup":
                output_fn(f"follow-up mode: {follow_up_mode.value}")
                continue
            if prompt in {"/followup auto", "/followup steer", "/followup queue"}:
                follow_up_mode = (
                    FollowUpMode.AUTO if prompt.endswith("auto") else (
                        FollowUpMode.STEER
                        if prompt.endswith("steer") else FollowUpMode.QUEUE
                    )
                )
                output_fn(
                    f"follow-up mode: {follow_up_mode.value}; "
                    + (
                        "ordinary messages will be resolved from their meaning"
                        if follow_up_mode is FollowUpMode.AUTO else (
                            "new messages will adjust the running Task at a safe point"
                            if follow_up_mode is FollowUpMode.STEER else
                            "new messages will wait for the running Task to finish"
                        )
                    )
                )
                continue
            if prompt == "/queued":
                pending = await application.kernel.list_queued_session_follow_ups(
                    session.session_id
                )
                if not pending:
                    output_fn("follow-up queue is empty")
                for item in pending:
                    output_fn(
                        f"{item.input_id} [#{item.inbound_sequence}] "
                        f"after={item.after_task_id} {item.text}"
                    )
                continue
            if prompt.startswith("/unqueue "):
                input_id = prompt.removeprefix("/unqueue ").strip()
                cancelled = await application.kernel.cancel_session_follow_up(
                    session.session_id, input_id
                )
                output_fn(
                    "queued follow-up cancelled" if cancelled
                    else "queued follow-up not found or already dispatched"
                )
                continue
            if prompt == "/session":
                session = await application.kernel.get_session(session.session_id)
                output_fn(
                    f"{session.session_id} [{session.state.value}] "
                    f"tasks={len(session.task_ids)} title={session.title}"
                )
                continue
            if prompt == "/tasks":
                tasks = await application.kernel.list_session_tasks(session.session_id)
                if not tasks:
                    output_fn("no tasks")
                for task in tasks:
                    output_fn(f"{task.task_id} [{task.state.value}] {task.goal}")
                continue
            if prompt == "/resume":
                candidates = (
                    await application.kernel.list_session_resume_candidates(
                        session.session_id, root
                    )
                )
                if not candidates:
                    output_fn("no suspended tasks in this session")
                interaction = (
                    await application.kernel.request_session_task_choice(
                        session.session_id, candidates,
                        "请选择要恢复的 Task。", source="resume-command",
                    )
                    if candidates else None
                )
                for index, item in enumerate(candidates, start=1):
                    output_fn(
                        f"{index}. {item.task_id} [{item.task_state}] "
                        f"resume={item.safety.value} {item.goal}"
                    )
                if candidates:
                    assert interaction is not None
                    output_fn(
                        "输入编号、完整 Task ID，或 /resume <task_id>；"
                        "编号选择不调用模型。"
                    )
                continue
            if prompt.startswith("/resume "):
                explicit_resume_task = prompt.removeprefix("/resume " ).strip()
                if not explicit_resume_task:
                    output_fn("usage: /resume <task_id>")
                    continue
            if prompt.startswith("/new "):
                prompt = prompt.removeprefix("/new " ).strip()
                if not prompt:
                    output_fn("usage: /new <goal>")
                    continue
                explicit_new_task = True
            for prefix, intent in (
                ("/steer ", RuntimeInputIntent.STEER),
                ("/redirect ", RuntimeInputIntent.REPLACE),
                ("/replace ", RuntimeInputIntent.REPLACE),
            ):
                if prompt.startswith(prefix):
                    prompt = prompt[len(prefix):].strip()
                    if not prompt:
                        output_fn(f"usage: {prefix.strip()} <instruction>")
                        invalid_runtime_command = True
                        break
                    if session.active_task_id is None:
                        output_fn("no active task in this session")
                        invalid_runtime_command = True
                        break
                    explicit_runtime_intent = intent
                    explicit_resume_task = session.active_task_id
                    break
            if invalid_runtime_command:
                continue
            if prompt == "/sessions":
                sessions = await application.kernel.list_sessions()
                for item in sessions:
                    marker = "*" if item.session_id == session.session_id else "-"
                    output_fn(
                        f"{marker} {item.session_id} [{item.state.value}] "
                        f"tasks={len(item.task_ids)} title={item.title}"
                    )
                continue
            if prompt.startswith("/use "):
                target_id = prompt.removeprefix("/use " ).strip()
                target = await application.kernel.get_session(target_id)
                if target.state.value != "ACTIVE":
                    output_fn(f"cannot use {target_id}: state={target.state.value}")
                else:
                    session = target
                    output_fn(f"using session: {session.session_id}")
                continue
            if prompt == "/flow":
                flow = await application.kernel.get_session_flow(session.session_id)
                output_fn(
                    f"session [{flow['state']}] cursor={flow['cursor']} "
                    f"active={flow['active_task_id'] or '-'}"
                )
                for item in flow["tasks"]:
                    marker = "*" if item["active"] else "-"
                    output_fn(f"{marker} {item['task_id']} [{item['state']}]")
                continue
            if prompt in {
                "/status", "/plan", "/spec", "/diff", "/permissions"
            }:
                if prompt == "/permissions":
                    await _print_chat_permissions(application, root, output_fn)
                elif session.active_task_id is None:
                    output_fn("no active task in this session")
                else:
                    await _handle_active_chat_command(
                        prompt, application, session, session.active_task_id,
                        root, output_fn,
                    )
                continue
            if (
                prompt.startswith("/")
                and explicit_resume_task is None
                and not explicit_new_task
            ):
                output_fn(f"unknown command: {prompt}; use /help")
                continue

            resume_mode = False
            continuation_resume = False
            pending_approval_resolution = None
            session_input = None
            if explicit_resume_task is None and not explicit_new_task:
                local_choice = (
                    await application.kernel.resolve_pending_session_choice(
                        session.session_id, prompt
                    )
                )
                if local_choice.action is SessionChoiceAction.SELECT:
                    explicit_resume_task = local_choice.target_id
                    output_fn(
                        "[会话] 已按刚才展示的候选列表选择 "
                        f"{local_choice.option_id}；正在校验恢复安全性。"
                    )
                else:
                    session_input = await _resolve_session_input_with_progress(
                        application, session.session_id, prompt, root, output_fn
                    )
            if (
                session_input is not None
                and session_input.action is SessionInputAction.CLARIFY
            ):
                clarification = (session_input.clarification or
                    "请明确说明要处理的新目标或选择未完成任务。")
                interaction = await application.kernel.request_session_task_choice(
                    session.session_id, session_input.candidates, clarification
                )
                output_fn("[会话] " + clarification)
                for option, item in zip(
                    interaction.options, session_input.candidates, strict=True
                ):
                    output_fn(
                        f"{option.ordinal}. {item.task_id} "
                        f"[{item.task_state}] {item.goal}"
                    )
                output_fn(
                    "请输入编号、完整 Task ID，或 /resume <task_id>；"
                    "编号选择不调用模型。"
                )
                continue
            if (
                session_input is not None
                and session_input.action is SessionInputAction.NEW_TASK
            ):
                await application.kernel.clear_pending_session_interaction(
                    session.session_id, "conversation_moved_to_new_task"
                )
            selected_resume_task = (
                explicit_resume_task
                if explicit_resume_task is not None else (
                    session_input.task_id
                    if session_input is not None
                    and session_input.action is SessionInputAction.RESUME_TASK
                    else None
                )
            )
            if selected_resume_task is not None:
                await application.kernel.clear_pending_session_interaction(
                    session.session_id, "task_selected_for_continuation"
                )
            continuation = (
                await application.kernel.resolve_session_continuation(
                    session.session_id, root, task_id=selected_resume_task
                )
                if selected_resume_task is not None
                else None
            )
            if (
                continuation is not None
                and continuation.mode is SessionContinuationMode.RECOVER_TASK
            ):
                assert continuation.task_id is not None
                task = await application.kernel.get_task(continuation.task_id)
                session = await application.kernel.select_session_task(
                    session.session_id, task.task_id
                )
                resume_mode = True
                checkpoint = dict(task.active_agent_checkpoint or {})
                output_fn(f"task: {task.task_id}")
                output_fn(f"[恢复] 正在继续上次意外中断的任务：{task.goal}")
                output_fn(
                    "[恢复] 已保留原目标、已完成工作和证据；"
                    f"恢复方式={continuation.resume_safety.value if continuation.resume_safety else '-'}，"
                    f"从模型回合 {int(checkpoint.get('model_calls', 0)) + 1} 继续。"
                )
                if continuation.resume_safety is SessionResumeSafety.REBASE_REQUIRED:
                    selected_candidate = next((
                        item for item in continuation.candidates
                        if item.task_id == task.task_id
                    ), None)
                    reasons = (
                        ", ".join(selected_candidate.rebase_reasons)
                        if selected_candidate is not None else "runtime changed"
                    )
                    output_fn(
                        "[恢复] 检测到 Runtime/Adapter 升级，将使用当前版本"
                        "重新组装上下文；不会重放旧工具调用。"
                    )
                    output_fn(f"[恢复] 升级差异：{reasons}")
                if explicit_resume_task is None:
                    await application.kernel.queue_steering(
                        task.task_id, SteeringKind.STEER,
                        prompt, f"input-{uuid4().hex}",
                    )
                    output_fn(
                        "[恢复] 已将本轮用户原文加入任务上下文。"
                    )
            elif (
                continuation is not None
                and continuation.mode
                is SessionContinuationMode.RESUME_CONTINUATION
            ):
                assert continuation.task_id is not None
                task = await application.kernel.get_task(continuation.task_id)
                session = await application.kernel.select_session_task(
                    session.session_id, task.task_id
                )
                resume_mode = True
                continuation_resume = True
                output_fn(f"task: {task.task_id}")
                output_fn("[续接] 正在根据本轮输入继续尚未完成的交付项。")
            elif (
                continuation is not None
                and continuation.mode is SessionContinuationMode.MULTIPLE_CANDIDATES
            ):
                prompt_text = (
                    "找到多个未完成任务，无法安全猜测你要继续哪一个。"
                )
                interaction = await application.kernel.request_session_task_choice(
                    session.session_id, continuation.candidates, prompt_text,
                    source="continuation-safety-resolution",
                )
                output_fn("[恢复] " + prompt_text)
                for option, item in zip(
                    interaction.options, continuation.candidates, strict=True
                ):
                    output_fn(
                        f"{option.ordinal}. {item.task_id} [{item.task_state}] "
                        f"resume={item.safety.value} {item.goal}"
                    )
                output_fn(
                    "请输入编号、完整 Task ID，或 /resume <task_id>；"
                    "编号选择不调用模型。"
                )
                continue
            elif (
                continuation is not None
                and continuation.mode is SessionContinuationMode.AWAIT_USER_ACTION
            ):
                assert continuation.task_id is not None
                task = await application.kernel.get_task(continuation.task_id)
                if task.pending_approval is not None:
                    route = await application.kernel.route_runtime_input(
                        task.task_id, prompt, f"input-{uuid4().hex}",
                        explicit_intent=explicit_runtime_intent,
                    )
                    if route.intent is RuntimeInputIntent.STATUS_QUERY:
                        await _print_chat_status(
                            application, session, task.task_id, output_fn
                        )
                        continue
                    elif (
                        route.intent
                        is RuntimeInputIntent.REVIEW_PENDING_ACTION
                    ):
                        request = task.pending_approval
                        assert request is not None
                        checkpoint = dict(task.active_agent_checkpoint or {})
                        output_fn(f"task: {task.task_id}")
                        _print_chat_approval(AgentTurnSuspended(
                            task_id=task.task_id, turn_id=request.turn_id,
                            revision=int(checkpoint.get("revision", 0)),
                            approval_request_id=request.request_id,
                            payload_hash=request.payload_hash,
                            action=request.action, target=request.target,
                            preview=request.preview, risk=request.risk.value,
                            network_access=request.network_access,
                            data_transmission=request.data_transmission,
                            rollback=request.rollback,
                            approval_kind=request.kind.value,
                        ), output_fn)
                        try:
                            answer = (await read_input(
                                "这是当前尚未执行的操作。是否明确批准？ "
                                "[y/N/leave] "
                            )).strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            answer = "leave"
                        if answer in {"leave", "l", "exit", "q"}:
                            output_fn("审批保持等待；没有执行任何操作。")
                            continue
                        decision = (
                            ApprovalDecision.APPROVE
                            if answer in {"y", "yes", "approve"}
                            else ApprovalDecision.DENY
                        )
                        reason = (
                            "approved after reviewing restored pending action"
                            if decision is ApprovalDecision.APPROVE else
                            "rejected after reviewing restored pending action"
                        )
                        pending_approval_resolution = (
                            request.request_id, decision, reason
                        )
                        session = await application.kernel.select_session_task(
                            session.session_id, task.task_id
                        )
                        resume_mode = True
                        output_fn(
                            "[续接] 已记录明确的审批决定，正在继续同一 Task。"
                        )
                    elif route.intent is RuntimeInputIntent.NEW_TASK_AFTER_CURRENT:
                        follow_up = await application.kernel.queue_session_follow_up(
                            session.session_id, task.task_id, prompt,
                            f"input-{uuid4().hex}",
                        )
                        output_fn(
                            "[队列] 当前审批保持不变；已保存为完成后的第 "
                            f"{follow_up.inbound_sequence} 条消息"
                        )
                        continue
                    elif route.applied:
                        session = await application.kernel.select_session_task(
                            session.session_id, task.task_id
                        )
                        resume_mode = True
                        output_fn(f"task: {task.task_id}")
                        output_fn(
                            "[续接] 已取消尚未执行的旧审批动作；它没有被批准，"
                            "也不会在后台执行。"
                        )
                        output_fn(
                            "[续接] 已将本轮输入加入同一 Task，正在从安全断点"
                            "重新规划。若仍需风险操作，会重新请求授权。"
                        )
                    else:
                        output_fn(
                            "[续接] 当前 Task 正在等待审批。普通文字不会被"
                            "当作批准；本轮输入也未能安全判定为修改任务方向。"
                        )
                        output_fn(
                            "[续接] 可审批/拒绝原动作，或使用 /steer <补充要求>、"
                            "/replace <新目标>、/new <独立任务>。"
                        )
                        output_fn(
                            f"[续接] task={task.task_id} state={task.state.value} "
                            f"reason={route.reason_code}"
                        )
                        continue
                else:
                    output_fn(
                        "[续接] 当前任务正在等待明确的澄清答案；"
                        "请先回答已显示的问题。"
                    )
                    output_fn(
                        f"[续接] task={continuation.task_id or '-'} "
                        f"state={continuation.task_state or '-'} "
                        f"reason={continuation.reason_code}"
                    )
                    continue
            elif (
                continuation is not None
                and continuation.mode is SessionContinuationMode.BLOCKED
            ):
                output_fn(
                    "[恢复] 选中的任务仍未正常结束，但当前没有可安全恢复的"
                    "断点；为避免重复执行未知结果的写入或命令，本次不会"
                    "自动新建任务。"
                )
                output_fn(
                    f"[恢复] task={continuation.task_id or '-'} "
                    f"state={continuation.task_state or '-'} "
                    f"reason={continuation.reason_code}"
                )
                selected_candidate = next((
                    item for item in continuation.candidates
                    if item.task_id == continuation.task_id
                ), None)
                if selected_candidate and selected_candidate.conflict_reasons:
                    output_fn(
                        "[恢复] 冲突："
                        + ", ".join(selected_candidate.conflict_reasons)
                    )
                output_fn(
                    "[恢复] 可使用 /new <目标> 新建任务，或修复冲突后再恢复。"
                )
                continue
            else:
                task = await application.kernel.create_task(
                    prompt, root, session_id=session.session_id,
                    command_id=(
                        f"follow-up:{queued_follow_up.input_id}"
                        if queued_follow_up is not None else None
                    ),
                )
                if queued_follow_up is not None:
                    await application.kernel.mark_session_follow_up_dispatched(
                        queued_follow_up, task.task_id
                    )
                output_fn(f"task: {task.task_id}")
                output_fn("[进度] 正在识别项目、加载工具并准备上下文…")
            try:
                if not resume_mode:
                    for target in (
                        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                        TaskState.EXECUTING,
                    ):
                        task = await application.kernel.transition_task(
                            task.task_id, target,
                            f"interactive {target.value.lower()}"
                        )
                streamed: list[str] = []
                live_terminal = output_fn is print
                prompt_safe_streaming = live_terminal and live_input_supported
                pending_display = ""
                displayed_agent_line = False
                activity_heartbeat: asyncio.Task[None] | None = None

                def stop_heartbeat() -> None:
                    nonlocal activity_heartbeat
                    if activity_heartbeat is not None:
                        activity_heartbeat.cancel()
                        activity_heartbeat = None

                def start_heartbeat(label: str) -> None:
                    nonlocal activity_heartbeat
                    stop_heartbeat()

                    async def report_wait() -> None:
                        elapsed = 0
                        try:
                            while True:
                                await asyncio.sleep(5)
                                elapsed += 5
                                output_fn(
                                    f"[进度] {label}仍在进行（{elapsed} 秒，"
                                    "可按 Ctrl+C 安全中断）"
                                )
                        except asyncio.CancelledError:
                            return

                    activity_heartbeat = asyncio.create_task(report_wait())

                def on_text_delta(text: str) -> None:
                    nonlocal pending_display, displayed_agent_line
                    stop_heartbeat()
                    if prompt_safe_streaming:
                        # Prompt Toolkit can safely redraw complete lines above
                        # an active ``control>`` prompt.  A raw token fragment,
                        # however, is an incomplete terminal line and can be
                        # split or overwritten by the next prompt redraw.  Keep
                        # only that unfinished line buffered; complete lines
                        # still appear progressively during long answers.
                        pending_display += text
                        while "\n" in pending_display:
                            line, pending_display = pending_display.split(
                                "\n", 1
                            )
                            prefix = "agent> " if not displayed_agent_line else ""
                            print(prefix + line, flush=True)
                            displayed_agent_line = True
                    elif live_terminal:
                        if not streamed:
                            print("agent> ", end="", flush=True)
                        print(text, end="", flush=True)
                    streamed.append(text)

                def finish_stream(result=None) -> None:
                    nonlocal pending_display, displayed_agent_line
                    if streamed:
                        if prompt_safe_streaming:
                            if pending_display or not displayed_agent_line:
                                prefix = (
                                    "agent> " if not displayed_agent_line else ""
                                )
                                print(prefix + pending_display, flush=True)
                            pending_display = ""
                            displayed_agent_line = False
                        elif live_terminal:
                            print(flush=True)
                        else:
                            output_fn(f"agent> {''.join(streamed)}")
                        streamed.clear()
                    elif result is not None:
                        output_fn(f"agent> {result.assistant_message.text}")

                def on_progress(progress: AgentProgress) -> None:
                    if progress.kind is AgentProgressKind.MODEL_COMPLETED:
                        stop_heartbeat()
                        return
                    if progress.kind in {
                        AgentProgressKind.MODEL_RETRY,
                        AgentProgressKind.FOCUS,
                        AgentProgressKind.TOOL_COMPLETED,
                    }:
                        stop_heartbeat()
                    if progress.kind is AgentProgressKind.TOOL_STARTED:
                        finish_stream()
                    elif progress.kind is AgentProgressKind.MODEL_RETRY:
                        # A genuine recovery decision is a separate status
                        # line, never part of an assistant text stream.
                        finish_stream()
                    lines = (
                        render_progress(progress)
                        if (
                            progress.kind is AgentProgressKind.EXPLORATION
                            and progress_renderer is not None
                        )
                        else _render_agent_progress_lines(
                            progress, verbose=verbose_progress
                        )
                    )
                    for line in lines:
                        output_fn(line)
                    if progress.kind is AgentProgressKind.MODEL_STARTED:
                        start_heartbeat("模型响应")
                    elif progress.kind is AgentProgressKind.MODEL_RETRY:
                        start_heartbeat("模型重试")
                    elif progress.kind is AgentProgressKind.TOOL_STARTED:
                        start_heartbeat(f"工具 {progress.tool_name}")
                    elif (
                        progress.kind is AgentProgressKind.TOOL_COMPLETED
                        and progress.tool_name == "core.run_command"
                        and progress.error_code == "PERMISSION_DENIED"
                    ):
                        output_fn(
                            "[权限] 如果是 Project Trust 限制，请退出 Chat 后执行："
                        )
                        output_fn(
                            "tsm-agt trust set TRUSTED_BUILD --workspace "
                            f"{root}"
                        )

                async def run_current_agent():
                    if pending_approval_resolution is not None:
                        request_id, decision, reason = pending_approval_resolution
                        return await application.kernel.resolve_agent_approval(
                            request_id, decision, reason,
                            on_text_delta=on_text_delta,
                            on_progress=on_progress,
                        )
                    if continuation_resume:
                        return await application.kernel.resume_agent_continuation(
                            task.task_id, prompt, input_id=f"input-{uuid4().hex}",
                            on_text_delta=on_text_delta,
                            on_progress=on_progress,
                        )
                    if resume_mode:
                        return await application.kernel.resume_checkpointed_agent_turn(
                            task.task_id, on_text_delta=on_text_delta,
                            on_progress=on_progress,
                        )
                    return await application.kernel.run_agent_turn(
                        task.task_id, prompt, on_text_delta=on_text_delta,
                        on_progress=on_progress,
                    )

                try:
                    if not live_input_supported:
                        result = await run_current_agent()
                    else:
                        agent_future = asyncio.create_task(run_current_agent())
                        input_future = asyncio.create_task(read_input("control> "))
                        while True:
                            done, _ = await asyncio.wait(
                                {agent_future, input_future},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if agent_future in done:
                                input_future.cancel()
                                await asyncio.gather(
                                    input_future, return_exceptions=True
                                )
                                result = await agent_future
                                break
                            try:
                                live_text = input_future.result().strip()
                            except EOFError:
                                live_text = ""
                            if live_text:
                                if live_text in {"/exit", "/quit"}:
                                    await application.kernel.interrupt_agent_turn(
                                        task.task_id, "interactive chat exit"
                                    )
                                    agent_future.cancel()
                                    await asyncio.gather(
                                        agent_future, return_exceptions=True
                                    )
                                    output_fn(
                                        f"session saved: {session.session_id} "
                                        f"(resume with: tsm-agt chat --session "
                                        f"{session.session_id})"
                                    )
                                    return 0
                                if live_text in {
                                    "/followup", "/followup auto", "/followup steer",
                                    "/followup queue",
                                }:
                                    if live_text != "/followup":
                                        follow_up_mode = (
                                            FollowUpMode.AUTO
                                            if live_text.endswith("auto") else (
                                                FollowUpMode.STEER
                                                if live_text.endswith("steer")
                                                else FollowUpMode.QUEUE
                                            )
                                        )
                                    output_fn(
                                        f"follow-up mode: {follow_up_mode.value}"
                                    )
                                    input_future = asyncio.create_task(
                                        read_input("control> ")
                                    )
                                    continue
                                if live_text == "/queued":
                                    pending = (
                                        await application.kernel
                                        .list_queued_session_follow_ups(
                                            session.session_id
                                        )
                                    )
                                    if not pending:
                                        output_fn("follow-up queue is empty")
                                    for item in pending:
                                        output_fn(
                                            f"{item.input_id} "
                                            f"[#{item.inbound_sequence}] "
                                            f"after={item.after_task_id} {item.text}"
                                        )
                                    input_future = asyncio.create_task(
                                        read_input("control> ")
                                    )
                                    continue
                                if live_text.startswith("/unqueue "):
                                    input_id = live_text.removeprefix(
                                        "/unqueue "
                                    ).strip()
                                    cancelled = (
                                        await application.kernel
                                        .cancel_session_follow_up(
                                            session.session_id, input_id
                                        )
                                    )
                                    output_fn(
                                        "queued follow-up cancelled"
                                        if cancelled else
                                        "queued follow-up not found or already "
                                        "dispatched"
                                    )
                                    input_future = asyncio.create_task(
                                        read_input("control> ")
                                    )
                                    continue
                                if await _handle_active_chat_command(
                                    live_text, application, session, task.task_id,
                                    root, output_fn,
                                ):
                                    pass
                                elif live_text == "/interrupt":
                                    await application.kernel.interrupt_agent_turn(
                                        task.task_id, "interactive user interrupt"
                                    )
                                    agent_future.cancel()
                                    await asyncio.gather(
                                        agent_future, return_exceptions=True
                                    )
                                    output_fn(
                                        "interrupted safely; resume with: "
                                        f"tsm-agt resume {task.task_id} "
                                        f"--workspace {root}"
                                    )
                                    return 130
                                else:
                                    explicit = None
                                    routed_text = live_text
                                    for prefix, intent in (
                                        ("/steer ", RuntimeInputIntent.STEER),
                                        ("/redirect ", RuntimeInputIntent.REPLACE),
                                        ("/replace ", RuntimeInputIntent.REPLACE),
                                        ("/queue ", RuntimeInputIntent.NEW_TASK_AFTER_CURRENT),
                                        ("/after ", RuntimeInputIntent.NEW_TASK_AFTER_CURRENT),
                                    ):
                                        if live_text.startswith(prefix):
                                            explicit = intent
                                            routed_text = live_text[len(prefix):].strip()
                                            break
                                    if (
                                        explicit is None
                                        and follow_up_mode is FollowUpMode.QUEUE
                                    ):
                                        explicit = RuntimeInputIntent.NEW_TASK_AFTER_CURRENT
                                    if explicit is RuntimeInputIntent.NEW_TASK_AFTER_CURRENT:
                                        follow_up = (
                                            await application.kernel.queue_session_follow_up(
                                                session.session_id, task.task_id,
                                                routed_text, f"input-{uuid4().hex}",
                                            )
                                        )
                                        output_fn(
                                            "[队列] 已保存为当前 Task 完成后的第 "
                                            f"{follow_up.inbound_sequence} 条消息"
                                        )
                                        input_future = asyncio.create_task(
                                            read_input("control> ")
                                        )
                                        continue
                                    route = await application.kernel.route_runtime_input(
                                        task.task_id, routed_text,
                                        f"input-{uuid4().hex}",
                                        explicit_intent=explicit,
                                        fallback_intent=(
                                            RuntimeInputIntent.STEER
                                            if follow_up_mode is FollowUpMode.STEER
                                            else None
                                        ),
                                    )
                                    if route.intent is RuntimeInputIntent.STATUS_QUERY:
                                        await _print_chat_status(
                                            application, session, task.task_id, output_fn
                                        )
                                    elif route.intent is RuntimeInputIntent.NEW_TASK_AFTER_CURRENT:
                                        follow_up = (
                                            await application.kernel.queue_session_follow_up(
                                                session.session_id, task.task_id,
                                                routed_text, f"input-{uuid4().hex}",
                                            )
                                        )
                                        output_fn(
                                            "[队列] 已保存为当前 Task 完成后的第 "
                                            f"{follow_up.inbound_sequence} 条消息"
                                        )
                                    elif route.requires_confirmation:
                                        output_fn(
                                            "I cannot safely tell how this changes the "
                                            "running Task. Use /steer <补充要求>, "
                                            "/redirect <新目标>, or /queue <后续任务>."
                                        )
                                    elif route.applied:
                                        label = (
                                            "redirected goal; committed work is kept and "
                                            "unstarted tool calls will be cancelled"
                                            if route.intent is RuntimeInputIntent.REPLACE
                                            else "additional instruction"
                                        )
                                        output_fn(f"accepted {label}; applying at a safe point")
                            input_future = asyncio.create_task(
                                read_input("control> ")
                            )
                except (KeyboardInterrupt, asyncio.CancelledError):
                    stop_heartbeat()
                    finish_stream()
                    await application.kernel.interrupt_agent_turn(task.task_id)
                    output_fn(
                        "interrupted safely; resume with: "
                        f"tsm-agt resume {task.task_id} --workspace {root}"
                    )
                    return 130
                stop_heartbeat()
                finish_stream(
                    result if isinstance(result, AgentTurnResult) else None
                )
                if isinstance(result, AgentContinuationSuspended):
                    output_fn(f"agent> {result.assistant_message.text}")
                    output_fn(
                        "[续接] 本阶段已完成；后续必需交付项仍保留在同一 "
                        "Task。下一条普通输入会由会话语义路由判断是继续、"
                        "转向还是新任务。"
                    )
                    session = await application.kernel.get_session(
                        session.session_id
                    )
                    continue
                while isinstance(
                    result, (AgentTurnSuspended, AgentClarificationSuspended)
                ):
                    if isinstance(result, AgentClarificationSuspended):
                        output_fn(
                            "result reconciliation required"
                            if result.kind == "OUTCOME_RECONCILIATION"
                            else "input required"
                        )
                        output_fn(f"question: {result.question}")
                        output_fn(f"reason: {result.reason}")
                        selected_choice = None
                        answer = None
                        if result.choices:
                            for index, (_value, label) in enumerate(
                                result.choices, start=1
                            ):
                                output_fn(f"{index}. {label}")
                            try:
                                selection = (
                                    await read_input("choice> " )
                                ).strip()
                            except (EOFError, KeyboardInterrupt):
                                selection = ""
                            values = {value for value, _label in result.choices}
                            if selection.isdigit():
                                ordinal = int(selection)
                                if 1 <= ordinal <= len(result.choices):
                                    selected_choice = result.choices[ordinal - 1][0]
                            elif selection in values:
                                # Script/API-friendly stable value fallback.
                                selected_choice = selection
                            if selection and selected_choice is None:
                                output_fn(
                                    "invalid choice; enter a displayed number"
                                )
                                continue
                        else:
                            try:
                                answer = (
                                    await read_input("answer> " )
                                ).strip()
                            except (EOFError, KeyboardInterrupt):
                                answer = ""
                        if not selected_choice and not answer:
                            output_fn(
                                "clarification left pending; continue with: "
                                f"tsm-agt answer {result.request_id} "
                                f"--token {result.resume_token} "
                                + (
                                    "--choice '<value>' "
                                    if result.choices else "--answer '<text>' "
                                )
                                +
                                f"--workspace {root}"
                            )
                            return 0
                        result = await application.kernel.resolve_agent_clarification(
                            result.request_id, result.resume_token, answer,
                            selected_choice=selected_choice,
                            on_text_delta=on_text_delta, on_progress=on_progress,
                        )
                        if (
                            selected_choice == "KEEP_BLOCKED"
                            and isinstance(result, AgentClarificationSuspended)
                        ):
                            output_fn(
                                "[安全停点] 结果仍未确认；Task 保持阻塞，"
                                "后续不会自动重试或执行依赖操作。"
                            )
                            return 0
                        stop_heartbeat()
                        finish_stream(
                            result if isinstance(result, AgentTurnResult) else None
                        )
                        continue
                    _print_chat_approval(result, output_fn)
                    try:
                        answer = (await read_input(
                            "授权后才会执行上述完整操作，是否批准？ [y/N/leave] "
                        )).strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        answer = "leave"
                    if answer in {"leave", "l", "exit", "q"}:
                        output_fn(
                            "approval left pending; use tsm-agt approve/reject "
                            f"{result.approval_request_id} to continue"
                        )
                        return 0
                    decision = (
                        ApprovalDecision.APPROVE
                        if answer in {"y", "yes", "approve"}
                        else ApprovalDecision.DENY
                    )
                    reason = (
                        "approved interactively after reviewing the exact action"
                        if decision is ApprovalDecision.APPROVE else
                        "rejected interactively by the user"
                    )
                    try:
                        result = await application.kernel.resolve_agent_approval(
                            result.approval_request_id, decision, reason,
                            on_text_delta=on_text_delta,
                            on_progress=on_progress,
                        )
                    except (KeyboardInterrupt, asyncio.CancelledError):
                        stop_heartbeat()
                        finish_stream()
                        await application.kernel.interrupt_agent_turn(task.task_id)
                        output_fn(
                            "interrupted safely; resume with: "
                            f"tsm-agt resume {task.task_id} --workspace {root}"
                        )
                        return 130
                    stop_heartbeat()
                    finish_stream(
                        result if isinstance(result, AgentTurnResult) else None
                    )
                task = await application.kernel.get_task(task.task_id)
                task = await application.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING,
                    "interactive verifier started",
                )
                verification = await application.kernel.verify_task_acceptance(
                    task.task_id
                )
                if verification.evidence_level is not None:
                    output_fn(_render_evidence_level(verification.evidence_level))
                for criterion in verification.criteria:
                    output_fn(
                        f"verification criterion: {criterion.criterion_id} "
                        f"{criterion.status.value}"
                    )
                if verification.passed:
                    for target in (TaskState.FINALIZING, TaskState.SUCCEEDED):
                        task = await application.kernel.transition_task(
                            task.task_id, target,
                            f"interactive {target.value.lower()}",
                        )
                    output_fn(
                        f"verification: passed ({len(verification.criteria)} criteria)"
                    )
                else:
                    task = await application.kernel.transition_task(
                        task.task_id, TaskState.FAILED,
                        f"trusted verifier {verification.status.value}",
                    )
                    output_fn(
                        f"verification: {verification.status.value}; "
                        "Task was not marked successful"
                    )
                session = await application.kernel.get_session(session.session_id)
            except AgentLoopLimitExceeded as error:
                stop_heartbeat()
                output_fn(
                    "任务未能在调用预算内完成："
                    f"模型回合 {error.model_calls}/"
                    f"{error.max_model_calls or '?'}，"
                    f"工具调用 {error.tool_calls}/"
                    f"{error.max_tool_calls or '?'}。"
                )
                if error.last_tool_error:
                    output_fn(f"最后一个工具问题：{error.last_tool_error}")
                    if "PERMISSION_DENIED" in error.last_tool_error:
                        output_fn(
                            "说明：该动作被权限或 Project Trust 策略拒绝，"
                            "并非网络请求失败。"
                        )
                output_fn(
                    "这不是网络自动重试；通常表示模型持续查资料但没有及时收尾。"
                    f"可用 /flow 查看过程，Task 为 {task.task_id}。"
                )
            except Exception as error:
                stop_heartbeat()
                current = await application.kernel.get_task(task.task_id)
                if (
                    current.state is TaskState.EXECUTING
                    and current.active_agent_checkpoint is not None
                ):
                    try:
                        current = await application.kernel.interrupt_agent_turn(
                            task.task_id,
                            f"interactive execution stopped: {type(error).__name__}",
                        )
                    except (RuntimeError, ValueError):
                        pass
                elif not current.state.is_terminal and current.state not in {
                    TaskState.INTERRUPTED, TaskState.CONFLICT,
                }:
                    try:
                        await application.kernel.transition_task(
                            task.task_id, TaskState.FAILED,
                            "interactive Agent request failed without a safe checkpoint",
                        )
                    except (RuntimeError, ValueError):
                        pass
                output_fn(f"error: {type(error).__name__}: {error}")
                for line in _chat_error_guidance(error, root):
                    output_fn(line)
                current = await application.kernel.get_task(task.task_id)
                if current.state is TaskState.INTERRUPTED:
                    output_fn(
                        "[恢复] 任务已在安全断点停止。保持当前 Session，"
                        "修复外部问题后描述你的恢复意图，或使用 /resume 明确选择。"
                    )
    finally:
        try:
            await application.registry.stop_all()
        finally:
            if output_context is not None:
                output_context.__exit__(*sys.exc_info())
            if previous_sigint is not None:
                signal.signal(signal.SIGINT, previous_sigint)


def _print_chat_approval(
    result: AgentTurnSuspended, output_fn: Callable[[str], None],
) -> None:
    if result.approval_kind == "workspace_read":
        output_fn("需要额外目录访问权限")
        output_fn(f"目录：{result.target}")
        output_fn("权限：只读")
        output_fn("范围：当前 Task（任务结束后自动失效）")
        output_fn(f"用途：{result.action}")
        output_fn("仍然禁止：写文件、运行命令、读取 .env/.ssh/凭据和密钥")
        return
    output_fn("approval required")
    output_fn(f"risk: {result.risk}")
    output_fn(f"action: {result.action}")
    output_fn(f"target: {result.target}")
    output_fn(f"preview: {result.preview}")
    output_fn(f"full payload hash: {result.payload_hash}")
    output_fn("文件尚未修改；批准后才会执行与该 hash 绑定的完整操作。")
    output_fn(f"network: {result.network_access}")
    output_fn(f"data transmission: {result.data_transmission}")
    output_fn(f"rollback: {result.rollback}")


async def _resolve_agent_approval(
    request_id: str, decision: ApprovalDecision, reason: str, workspace: Path,
) -> int:
    root = workspace.expanduser().resolve()
    application = compose_openai_compatible_engineering_application_from_env(
        env_file=root / ".env",
        database_path=root / ".agent" / "runtime.db"
    )
    await application.registry.start_all()
    try:
        result = await application.kernel.resolve_agent_approval(
            request_id, decision, reason,
            on_progress=_print_standalone_agent_progress,
        )
        _print_agent_result(result)
        return await _finalize_standalone_agent_result(
            application, result.task_id, result
        )
    finally:
        await application.registry.stop_all()


async def _resolve_agent_clarification(
    request_id: str, resume_token: str, answer: str | None, workspace: Path,
    *, selected_choice: str | None = None,
) -> int:
    root = workspace.expanduser().resolve()
    application = compose_openai_compatible_engineering_application_from_env(
        env_file=root / ".env",
        database_path=root / ".agent" / "runtime.db"
    )
    await application.registry.start_all()
    try:
        result = await application.kernel.resolve_agent_clarification(
            request_id, resume_token, answer,
            selected_choice=selected_choice,
            on_progress=_print_standalone_agent_progress,
        )
        _print_agent_result(result)
        return await _finalize_standalone_agent_result(
            application, result.task_id, result
        )
    finally:
        await application.registry.stop_all()


async def _resume_agent_task(task_id: str, workspace: Path) -> int:
    root = workspace.expanduser().resolve()
    application = compose_openai_compatible_engineering_application_from_env(
        env_file=root / ".env",
        database_path=root / ".agent" / "runtime.db"
    )
    await application.registry.start_all()
    try:
        result = await application.kernel.resume_checkpointed_agent_turn(
            task_id, on_progress=_print_standalone_agent_progress
        )
        _print_agent_result(result)
        return await _finalize_standalone_agent_result(
            application, task_id, result
        )
    finally:
        await application.registry.stop_all()


async def _project_trust_get(workspace: Path) -> int:
    root = workspace.expanduser().resolve()
    application = compose_local_project_control_application(root)
    await application.registry.start_all()
    try:
        trust = await application.kernel.get_project_trust(root)
        print(f"workspace: {trust.workspace}")
        print(f"level: {trust.level.value}")
        print(f"fingerprint: {trust.fingerprint}")
        print(f"subject: {trust.subject}")
    finally:
        await application.registry.stop_all()
    return 0


async def _project_trust_set(workspace: Path, level: ProjectTrustLevel) -> int:
    root = workspace.expanduser().resolve()
    application = compose_local_project_control_application(root)
    await application.registry.start_all()
    try:
        trust = await application.kernel.set_project_trust(root, level)
        print(f"workspace: {trust.workspace}")
        print(f"level: {trust.level.value}")
        print(f"fingerprint: {trust.fingerprint}")
        print("Project trust updated; command approval and Sandbox checks still apply.")
    finally:
        await application.registry.stop_all()
    return 0


async def _memory_list(
    task_id: str, workspace: Path, include_stale: bool, as_json: bool,
) -> int:
    root = workspace.expanduser().resolve()
    application = compose_local_project_control_application(root)
    await application.registry.start_all()
    try:
        memories = await application.kernel.list_task_memories(
            task_id, include_stale=include_stale
        )
        if as_json:
            print(json.dumps(
                [memory.to_data() for memory in memories], ensure_ascii=False,
                indent=2, sort_keys=True,
            ))
            return 0
        if not memories:
            print("no visible memories")
            return 0
        for view in memories:
            record = view.record
            status = "STALE" if view.stale else "CURRENT"
            print(f"{record.memory_id} [{record.scope.value}/{status}]")
            print(f"  fact: {record.content}")
            print(
                f"  source: {record.source_kind.value} "
                f"{record.source_reference}"
            )
            print(f"  verified: {record.last_verified_at.isoformat()}")
            if view.stale_reason:
                print(f"  stale_reason: {view.stale_reason}")
        return 0
    finally:
        await application.registry.stop_all()


async def _working_memory_show(
    task_id: str, workspace: Path, as_json: bool,
) -> int:
    root = workspace.expanduser().resolve()
    application = compose_local_project_control_application(root)
    await application.registry.start_all()
    try:
        effective = await application.kernel.get_effective_working_memory(task_id)
        snapshot = effective.snapshot
        if as_json:
            print(json.dumps(
                effective.to_data(), ensure_ascii=False, indent=2, sort_keys=True
            ))
            return 0
        print(
            f"working memory task={snapshot.task_id} revision={snapshot.revision} "
            f"hash={snapshot.content_hash}"
        )
        print(f"goal: {snapshot.goal}")
        for label, values in (
            ("constraints", snapshot.constraints), ("facts", snapshot.facts),
            ("decisions", snapshot.decisions),
            ("hypotheses", snapshot.hypotheses),
            ("open_questions", snapshot.open_questions),
            ("completed_work", snapshot.completed_work),
            ("remaining_work", effective.remaining_work),
        ):
            print(f"{label}:")
            for value in values:
                print(f"- {value}")
            if not values:
                print("- none")
        print("plan:")
        for step in snapshot.plan:
            print(
                f"- {step.step_id} [{step.status.value}] {step.description}; "
                f"done when: {step.completion_criteria}"
            )
        if not snapshot.plan:
            print("- none")
        print("evidence:")
        for evidence in effective.evidence:
            print(
                f"- {evidence.evidence_id} ({evidence.kind}) "
                f"{evidence.reference}: {evidence.summary}"
            )
        if not snapshot.evidence:
            print("- none")
        return 0
    finally:
        await application.registry.stop_all()


async def _session_command(args: argparse.Namespace) -> int:
    root = args.workspace.expanduser().resolve()
    application = compose_local_project_control_application(root)
    await application.registry.start_all()
    try:
        if args.session_command == "create":
            session = await application.kernel.create_session(args.title)
            print(session.session_id)
            return 0
        if args.session_command == "list":
            sessions = await application.kernel.list_sessions(
                include_archived=args.include_archived
            )
            if args.json:
                print(json.dumps(
                    [session.to_data() for session in sessions],
                    ensure_ascii=False, indent=2, sort_keys=True,
                ))
            else:
                for session in sessions:
                    print(
                        f"{session.session_id} [{session.state.value}] "
                        f"active={session.active_task_id or '-'} tasks={len(session.task_ids)} "
                        f"{session.title}"
                    )
            return 0
        if args.session_command == "show":
            session = await application.kernel.get_session(args.session_id)
            data = session.to_data()
            print(
                json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
                if args.json else
                f"{session.session_id} [{session.state.value}]\n"
                f"title: {session.title}\nactive_task: {session.active_task_id or '-'}\n"
                f"tasks: {', '.join(session.task_ids) or '-'}\n"
                f"context_revision: {session.context_revision}"
            )
            return 0
        if args.session_command == "select-task":
            session = await application.kernel.select_session_task(
                args.session_id, args.task_id, expected_version=args.expected_version
            )
            print(f"active task: {session.active_task_id}")
            return 0
        if args.session_command == "close":
            session = await application.kernel.close_session(
                args.session_id, args.reason, expected_version=args.expected_version
            )
            print(f"{session.session_id} [{session.state.value}]")
            return 0
        if args.session_command == "archive":
            session = await application.kernel.archive_session(
                args.session_id, expected_version=args.expected_version
            )
            print(f"{session.session_id} [{session.state.value}]")
            return 0
        projection = await application.kernel.get_session_flow(
            args.session_id, after_sequence=args.after
        )
        if args.json:
            print(json.dumps(projection, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(
                f"session {projection['session_id']} [{projection['state']}] "
                f"cursor={projection['cursor']} active={projection['active_task_id'] or '-'}"
            )
            for task in projection["tasks"]:
                marker = "*" if task["active"] else "-"
                print(f"{marker} {task['task_id']} [{task['state']}]")
            for event in projection["events"]:
                print(f"  {event['sequence']:04d} {event['type']}")
        return 0
    finally:
        await application.registry.stop_all()


async def _task_create(
    goal: str, workspace: Path, session_id: str | None, command_id: str | None,
) -> int:
    root = workspace.expanduser().resolve()
    application = compose_local_project_control_application(root)
    await application.registry.start_all()
    try:
        task = await application.kernel.create_task(
            goal, root, session_id=session_id, command_id=command_id
        )
        print(f"{task.task_id} session={task.session_id} [{task.state.value}]")
        return 0
    finally:
        await application.registry.stop_all()


async def _steering_command(args: argparse.Namespace) -> int:
    root = args.workspace.expanduser().resolve()
    application = compose_local_project_control_application(root)
    await application.registry.start_all()
    try:
        if args.steering_command == "show":
            projection = await application.kernel.get_steering(args.task_id)
            data = {
                "task_id": projection.task_id,
                "revision": projection.revision,
                "latest_inbound_sequence": projection.latest_inbound_sequence,
                "latest_goal_revision": projection.latest_goal_revision,
                "pending": [item.to_data() for item in projection.pending],
                "applied_ids": list(projection.applied_ids),
                "content_hash": projection.content_hash,
            }
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    f"{projection.task_id} steering-r{projection.revision} "
                    f"goal-r{projection.latest_goal_revision} "
                    f"pending={len(projection.pending)}"
                )
                for item in projection.pending:
                    print(
                        f"{item.inbound_sequence:04d} {item.kind.value}: {item.text}"
                    )
            return 0
        kind = (
            SteeringKind.STEER if args.steering_command == "steer"
            else SteeringKind.REPLACE
        )
        projection = await application.kernel.queue_steering(
            args.task_id, kind, args.text, args.command_id
        )
        print(
            f"queued {kind.value} inbound={projection.latest_inbound_sequence} "
            f"pending={len(projection.pending)}"
        )
        return 0
    finally:
        await application.registry.stop_all()


async def _onboarding_run(task_id: str, workspace: Path, as_json: bool) -> int:
    root = workspace.expanduser().resolve()
    application = compose_local_project_control_application(root)
    await application.registry.start_all()
    try:
        result = await application.kernel.run_project_onboarding(task_id)
        if not isinstance(result, ProjectOnboardingSnapshot):
            raise RuntimeError("onboarding stopped with an incomplete checkpoint")
        _print_onboarding(result, False, as_json)
        return 0
    finally:
        await application.registry.stop_all()


async def _onboarding_show(
    task_id: str, workspace: Path, include_stale: bool, as_json: bool,
) -> int:
    root = workspace.expanduser().resolve()
    application = compose_local_project_control_application(root)
    await application.registry.start_all()
    try:
        snapshot, stale = await application.kernel.get_project_onboarding(
            task_id, include_stale=include_stale
        )
        _print_onboarding(snapshot, stale, as_json)
        return 0
    finally:
        await application.registry.stop_all()


def _print_onboarding(
    snapshot: ProjectOnboardingSnapshot, stale: bool, as_json: bool,
) -> None:
    if as_json:
        print(json.dumps(
            {**snapshot.to_data(), "stale": stale}, ensure_ascii=False,
            indent=2, sort_keys=True,
        ))
        return
    print(
        f"onboarding revision={snapshot.revision} "
        f"status={'STALE' if stale else 'CURRENT'} facts={len(snapshot.facts)}"
    )
    print(f"trusted_rules_read: {str(snapshot.trusted_rules_read).lower()}")
    for fact in snapshot.facts:
        sources = ", ".join(source.path for source in fact.sources)
        print(f"- {fact.category}: {fact.value} [{sources}]")


def render_flow_tree(projection: FlowProjection) -> str:
    """Render a compact, payload-free view of one persisted flow projection."""

    children: dict[str, list[FlowNode]] = {}
    root: FlowNode | None = None
    for node in projection.nodes:
        if node.parent_id is None:
            root = node
        else:
            children.setdefault(node.parent_id, []).append(node)

    if root is None:
        raise ValueError(f"flow projection has no task root: {projection.task_id}")

    lines = [
        f"task {projection.task_id} [{root.status.value}] "
        f"cursor={projection.cursor}"
    ]

    def append_children(parent_id: str, depth: int) -> None:
        for node in children.get(parent_id, []):
            event_end = (
                str(node.event_seq_end)
                if node.event_seq_end is not None
                else "..."
            )
            lines.append(
                f"{'  ' * depth}{node.kind.value} {node.label} "
                f"[{node.status.value}] "
                f"events={node.event_seq_start}..{event_end}"
            )
            append_children(node.node_id, depth + 1)

    append_children(root.node_id, 1)
    failure = projection.first_actionable_failure
    lines.append(
        "first_actionable_failure: "
        + (
            "none"
            if failure is None
            else f"{failure.node_id} events={failure.event_seq_start}.."
            f"{failure.event_seq_end or '...'}"
        )
    )
    return "\n".join(lines)


async def _flow(task_id: str, workspace: Path, as_json: bool) -> int:
    return await _flow_query(
        task_id, workspace, as_json, live=False, node_id=None,
        kinds=(), statuses=(), export_path=None, timeline=False, tui=False,
        failure=False,
    )


async def _flow_query(
    task_id: str,
    workspace: Path,
    as_json: bool,
    *,
    live: bool,
    node_id: str | None = None,
    kinds: tuple[FlowNodeKind, ...] = (),
    statuses: tuple[FlowNodeStatus, ...] = (),
    export_path: Path | None = None,
    timeline: bool = False,
    tui: bool = False,
    failure: bool = False,
    poll_interval: float = 0.5,
) -> int:
    if poll_interval <= 0:
        raise ValueError("flow poll interval must be positive")
    if node_id is not None and failure:
        raise ValueError("--node cannot be combined with --failure")
    if (node_id is not None or failure) and (kinds or statuses or timeline or tui):
        raise ValueError(
            "--node/--failure cannot be combined with filters, --timeline, or --tui"
        )
    if failure and live:
        raise ValueError("--failure cannot be combined with --live")
    if tui and as_json:
        raise ValueError("--tui cannot be combined with --json")
    if (timeline or tui) and (kinds or statuses):
        raise ValueError("--timeline/--tui cannot be combined with filters")
    if tui and live and not sys.stdout.isatty():
        raise ValueError("--tui --live requires an interactive terminal")
    if export_path is not None and (
        live or node_id is not None or failure or as_json or tui
    ):
        raise ValueError(
            "--export cannot be combined with --live, --node, --json, or --tui"
        )
    root = workspace.expanduser().resolve()
    application = compose_local_flow_query_application(root)
    await application.registry.start_all()
    try:
        projection: FlowProjection | None = None
        while True:
            updated = await application.kernel.get_flow_projection(
                task_id, projection
            )
            if projection is None or updated.cursor != projection.cursor:
                if export_path is not None:
                    view = (
                        updated.filter_nodes(kinds=kinds, statuses=statuses)
                        if kinds or statuses else None
                    )
                    exporter = application.registry.require(FlowArtifactExportPort)
                    document = build_flow_export_document(updated, view)
                    suffix = export_path.suffix.lower()
                    if suffix == ".json":
                        result = exporter.export_json(root, str(export_path), document)
                    elif suffix == ".jsonl":
                        result = exporter.export_jsonl(root, str(export_path), document)
                    elif suffix == ".html":
                        result = exporter.export_html(root, str(export_path), document)
                    else:
                        raise ValueError(
                            "Flow export path must use the .json suffix, "
                            "the .jsonl suffix, or the .html suffix"
                        )
                    print(f"exported: {result.relative_path}")
                    print(f"bytes: {result.size_bytes}")
                    print(f"sha256: {result.sha256}")
                elif tui:
                    if live:
                        print("\x1b[2J\x1b[H", end="")
                    print(render_flow_tui(updated), flush=live)
                elif failure:
                    failed = updated.first_actionable_failure
                    if failed is None:
                        raise LookupError(
                            f"flow has no actionable failure: {updated.task_id}"
                        )
                    _print_flow_node_diagnostic(
                        updated.inspect_node(failed.node_id), as_json,
                        live=False, cursor=updated.cursor,
                    )
                elif node_id is None:
                    _print_flow_projection(
                        updated, as_json, live=live, kinds=kinds,
                        statuses=statuses, timeline=timeline,
                    )
                else:
                    _print_flow_node_diagnostic(
                        updated.inspect_node(node_id), as_json, live=live,
                        cursor=updated.cursor,
                    )
            projection = updated
            if export_path is not None or not live or _flow_is_terminal(projection):
                return 0
            await asyncio.sleep(poll_interval)
    finally:
        await application.registry.stop_all()


async def _inspect_effective_configuration(
    task_id: str, workspace: Path, revision: int | None, as_json: bool,
) -> int:
    root = workspace.expanduser().resolve()
    application = compose_local_flow_query_application(root)
    await application.registry.start_all()
    try:
        configuration = await application.kernel.get_effective_configuration(
            task_id, revision
        )
        if as_json:
            print(json.dumps(
                configuration.to_data(), ensure_ascii=False, sort_keys=True
            ))
            return 0
        model = configuration.model
        print(f"effective configuration revision: {configuration.revision}")
        print(f"captured at: {configuration.captured_at.isoformat()}")
        print(
            f"model: {model.get('provider', 'unknown')}/"
            f"{model.get('model', 'unknown')}"
        )
        if model.get("endpoint_origin"):
            print(f"endpoint origin: {model['endpoint_origin']}")
        print(
            "credentials: "
            + ("configured" if model.get("credentials_configured") else "not configured")
        )
        print("provider capabilities:")
        for name, value in sorted(configuration.provider_capabilities.items()):
            print(f"- {name}: {value}")
        print("tools:")
        for tool in configuration.tools:
            print(
                f"- {tool['name']}: risk={tool['risk']}, "
                f"idempotency={tool['idempotency']}, "
                f"parameters_hash={tool['parameters_hash']}"
            )
        print("adapters:")
        for adapter in configuration.adapters:
            print(
                f"- {adapter['adapter_id']}@{adapter['adapter_version']}: "
                f"port={adapter['port_name']}@{adapter['port_version']}, "
                f"health={adapter['health']}"
            )
        print(
            f"project: trust={configuration.project.get('trust')}, "
            f"sandbox={configuration.project.get('sandbox_adapter_id')}"
        )
        manifest = configuration.prompt_manifest
        print(
            f"prompt manifest: status={manifest.get('status')}, "
            f"id={manifest.get('manifest_id')}, revision={manifest.get('revision')}"
        )
        static_segments = manifest.get("static_segments", [])
        if isinstance(static_segments, list):
            for segment in static_segments:
                if isinstance(segment, dict):
                    print(
                        f"- {segment.get('segment_id')}: "
                        f"source={segment.get('source')}@{segment.get('version')}, "
                        f"hash={segment.get('content_hash')}, "
                        f"tokens={segment.get('token_count_source')}"
                    )
        print("sources:")
        for name, value in sorted(configuration.sources.items()):
            print(f"- {name}: {value}")
        print("hashes:")
        for name in (
            "effective_config_hash", "toolset_hash", "adapter_lock_hash",
            "policy_hash", "provider_capabilities_hash",
            "prompt_manifest_hash",
        ):
            print(f"- {name}: {getattr(configuration, name)}")
        return 0
    finally:
        await application.registry.stop_all()


def _print_flow_projection(
    projection: FlowProjection, as_json: bool, *, live: bool,
    kinds: tuple[FlowNodeKind, ...] = (),
    statuses: tuple[FlowNodeStatus, ...] = (),
    timeline: bool = False,
) -> None:
    view = (
        projection.filter_nodes(kinds=kinds, statuses=statuses)
        if kinds or statuses else None
    )
    if as_json:
        print(
            json.dumps(
                (
                    projection.timeline().to_data()
                    if timeline else view.to_data()
                    if view is not None else projection.to_data()
                ),
                ensure_ascii=False,
                indent=None if live else 2,
                separators=(",", ":") if live else None,
                sort_keys=True,
            ),
            flush=live,
        )
        return
    if live:
        print(f"--- flow update cursor={projection.cursor} ---")
    rendered = (
        render_flow_timeline(projection)
        if timeline else render_flow_projection_view(view)
        if view is not None else render_flow_tree(projection)
    )
    print(rendered, flush=live)


def render_flow_timeline(projection: FlowProjection) -> str:
    timeline = projection.timeline()
    lines = [
        f"timeline {timeline.task_id} cursor={timeline.cursor} "
        f"lanes={','.join(lane.value for lane in timeline.lanes)}"
    ]
    for item in timeline.items:
        end = item.event_seq_end if item.event_seq_end is not None else "..."
        duration = (
            f"{item.duration_ms}ms" if item.duration_ms is not None else "open"
        )
        lines.append(
            f"{item.event_seq_start:04d}..{str(end):>4} "
            f"{item.lane.value:<9} {item.kind.value:<10} "
            f"[{item.status.value:<16}] {item.label} ({duration})"
        )
    return "\n".join(lines)


def render_flow_tui(projection: FlowProjection) -> str:
    """Render one dependency-free terminal dashboard frame."""

    root = next(node for node in projection.nodes if node.parent_id is None)
    failure = projection.first_actionable_failure
    header = (
        f"TSM-AGT FLOW  task={projection.task_id}  "
        f"status={root.status.value}  cursor={projection.cursor}\n"
        f"failure={failure.node_id if failure else 'none'}  "
        "(Ctrl+C stops watching; it does not stop the task)"
    )
    return header + "\n" + ("=" * 78) + "\n" + render_flow_timeline(projection)


def render_flow_projection_view(view: FlowProjectionView) -> str:
    by_parent: dict[str, list[FlowNode]] = {}
    root: FlowNode | None = None
    for node in view.nodes:
        if node.parent_id is None:
            root = node
        else:
            by_parent.setdefault(node.parent_id, []).append(node)
    filters = []
    if view.kinds:
        filters.append("kind=" + ",".join(kind.value for kind in view.kinds))
    if view.statuses:
        filters.append(
            "status=" + ",".join(status.value for status in view.statuses)
        )
    lines = [
        f"filtered flow {view.task_id} cursor={view.cursor} "
        f"filters={' '.join(filters)} matches={len(view.matched_node_ids)}"
    ]
    if root is None:
        lines.append("no matching nodes")
        return "\n".join(lines)
    matched = set(view.matched_node_ids)

    def append(node: FlowNode, depth: int) -> None:
        marker = "match" if node.node_id in matched else "context"
        event_end = node.event_seq_end if node.event_seq_end is not None else "..."
        lines.append(
            f"{'  ' * depth}{marker} {node.kind.value} {node.label} "
            f"[{node.status.value}] events={node.event_seq_start}..{event_end}"
        )
        for child in by_parent.get(node.node_id, []):
            append(child, depth + 1)

    append(root, 0)
    return "\n".join(lines)


def render_flow_node_diagnostic(diagnostic: FlowNodeDiagnostic, cursor: int) -> str:
    node = diagnostic.node
    duration = (
        f"{diagnostic.duration_ms}ms"
        if diagnostic.duration_ms is not None else "in progress/unknown"
    )
    lines = [
        f"node {node.node_id}",
        f"task: {node.task_id}",
        f"cursor: {cursor}",
        f"what: {node.kind.value} {node.label}",
        f"status: {node.status.value}",
        f"duration: {duration}",
        "event_anchors: " + ", ".join(map(str, diagnostic.event_anchors)),
        f"parent: {diagnostic.parent.node_id if diagnostic.parent else 'none'}",
        "children: " + (
            ", ".join(child.node_id for child in diagnostic.children) or "none"
        ),
        f"wait_reason: {node.wait_reason or 'none'}",
        "first_actionable_failure: "
        + ("yes" if diagnostic.is_first_actionable_failure else "no"),
        "failure_attribution: " + (
            "not applicable"
            if diagnostic.failure_attribution is None
            else (
                f"{diagnostic.failure_attribution.code or 'unclassified'} "
                f"[{diagnostic.failure_attribution.confidence}] "
                f"{diagnostic.failure_attribution.category}; "
                f"basis={diagnostic.failure_attribution.basis}"
            )
        ),
        "observable_facts:",
    ]
    lines.extend(
        f"  event={fact.event_sequence} {fact.category.value} {fact.code} "
        + ", ".join(f"{key}={value}" for key, value in fact.values)
        for fact in diagnostic.facts
    )
    if not diagnostic.facts:
        lines.append("  none")
    lines.append("downstream_impact:")
    lines.extend(
        f"  {child.node_id} [{child.status.value}]"
        for child in diagnostic.downstream_nodes
    )
    if not diagnostic.downstream_nodes:
        lines.append("  none")
    lines.extend([
        "incoming:",
    ])
    lines.extend(
        f"  {edge.relation.value}: {edge.from_node} -> {edge.to_node}"
        for edge in diagnostic.incoming_edges
    )
    if not diagnostic.incoming_edges:
        lines.append("  none")
    lines.append("outgoing:")
    lines.extend(
        f"  {edge.relation.value}: {edge.from_node} -> {edge.to_node}"
        for edge in diagnostic.outgoing_edges
    )
    if not diagnostic.outgoing_edges:
        lines.append("  none")
    return "\n".join(lines)


def _print_flow_node_diagnostic(
    diagnostic: FlowNodeDiagnostic, as_json: bool, *, live: bool, cursor: int
) -> None:
    if as_json:
        data = {"cursor": cursor, **diagnostic.to_data()}
        print(
            json.dumps(
                data, ensure_ascii=False, indent=None if live else 2,
                separators=(",", ":") if live else None, sort_keys=True,
            ),
            flush=live,
        )
        return
    if live:
        print(f"--- flow node update cursor={cursor} ---")
    print(render_flow_node_diagnostic(diagnostic, cursor), flush=live)


def _flow_is_terminal(projection: FlowProjection) -> bool:
    root = next(
        (node for node in projection.nodes if node.parent_id is None), None
    )
    if root is None:
        raise ValueError(f"flow projection has no task root: {projection.task_id}")
    return root.status.is_terminal


async def _replay(
    task_id: str, workspace: Path, *, list_frames: bool,
    at_sequence: int | None, turn_id: str | None = None,
    node_id: str | None = None,
    boundary: FlowReplayBoundary = FlowReplayBoundary.END,
    play: bool = False,
    speed: FlowReplaySpeed = FlowReplaySpeed.REALTIME,
    max_wait_seconds: float = 2.0,
    from_sequence: int | None = None,
    to_sequence: int | None = None,
    step: bool = False,
    save_cursor: Path | None = None,
    resume_cursor: Path | None = None,
    as_json: bool,
) -> int:
    if (turn_id is None and node_id is None) and boundary is not FlowReplayBoundary.END:
        raise ValueError("--boundary is only valid with --turn or --node")
    if not play and speed is not FlowReplaySpeed.REALTIME:
        raise ValueError("--speed is only valid with --play")
    if not play and max_wait_seconds != 2.0:
        raise ValueError("--max-wait-seconds is only valid with --play")
    if not play and (
        from_sequence is not None or to_sequence is not None or step
        or save_cursor is not None or resume_cursor is not None
    ):
        raise ValueError(
            "--from, --to, --step, and replay cursors require --play"
        )
    if step and as_json:
        raise ValueError("--step cannot be combined with --json")
    root = workspace.expanduser().resolve()
    application = compose_local_flow_query_application(root)
    await application.registry.start_all()
    try:
        if play:
            cursor_store = application.registry.require(ReplayCursorStorePort)
            if resume_cursor is not None:
                saved = cursor_store.load(root, str(resume_cursor))
                if saved.task_id != task_id:
                    raise ValueError(
                        "Replay cursor belongs to a different task: "
                        f"{saved.task_id}"
                    )
                resumed_from = saved.sequence + 1
                if from_sequence is not None and from_sequence != resumed_from:
                    raise ValueError(
                        "--from conflicts with the resumed replay cursor"
                    )
                from_sequence = resumed_from
            playback = await application.kernel.get_flow_replay_playback(
                task_id, speed, max_wait_seconds, from_sequence, to_sequence,
            )
            await _play_replay(
                playback, as_json, step=step,
                cursor_store=cursor_store if save_cursor is not None else None,
                workspace=root, cursor_path=save_cursor,
            )
        elif list_frames:
            index = await application.kernel.get_flow_replay_index(task_id)
            _print_replay_index(index, as_json)
        elif turn_id is not None:
            snapshot = await application.kernel.get_flow_replay_turn_snapshot(
                task_id, turn_id, boundary
            )
            _print_replay_snapshot(snapshot, as_json)
        elif node_id is not None:
            snapshot = await application.kernel.get_flow_replay_node_snapshot(
                task_id, node_id, boundary
            )
            _print_replay_snapshot(snapshot, as_json)
        else:
            snapshot = await application.kernel.get_flow_replay_snapshot(
                task_id, at_sequence
            )
            _print_replay_snapshot(snapshot, as_json)
    finally:
        await application.registry.stop_all()
    return 0


async def _play_replay(
    playback: FlowReplayPlayback, as_json: bool, *, sleep=asyncio.sleep,
    step: bool = False, input_fn=input,
    cursor_store: ReplayCursorStorePort | None = None,
    workspace: Path | None = None, cursor_path: Path | None = None,
) -> None:
    if not as_json:
        print(
            f"replay playback {playback.task_id} speed={playback.speed.value} "
            f"frames={len(playback.frames)} range={playback.from_sequence}.."
            f"{playback.to_sequence} max_wait={playback.max_wait_ms}ms"
        )
    for playback_frame, snapshot in playback.iter_snapshots():
        if step:
            command = input_fn(
                f"step {snapshot.cursor}/{snapshot.latest_cursor} "
                "[Enter=next, q=quit] "
            ).strip().lower()
            if command in {"q", "quit"}:
                break
            if command not in {"", "n", "next"}:
                raise ValueError("Replay step accepts Enter/next or q/quit")
        if playback_frame.wait_ms and not step:
            await sleep(playback_frame.wait_ms / 1000)
        if as_json:
            print(json.dumps(
                playback_frame.to_data(snapshot), ensure_ascii=False,
                separators=(",", ":"), sort_keys=True,
            ), flush=True)
        else:
            timing_flags = []
            if playback_frame.gap_capped:
                timing_flags.append("gap-capped")
            if playback_frame.timestamp_regressed:
                timing_flags.append("timestamp-regressed")
            flags = f" flags={','.join(timing_flags)}" if timing_flags else ""
            print(
                f"--- replay frame={snapshot.cursor}/{snapshot.latest_cursor} "
                f"event={snapshot.frame.event_type} "
                f"recorded_gap={playback_frame.recorded_gap_ms}ms "
                f"wait={playback_frame.wait_ms}ms{flags} ---",
                flush=True,
            )
            print(render_flow_tree(snapshot.projection), flush=True)
        if cursor_store is not None:
            assert workspace is not None and cursor_path is not None
            await cursor_store.save(
                workspace, str(cursor_path),
                ReplayCursor(playback.task_id, snapshot.cursor),
            )


def _print_replay_index(index: FlowReplayIndex, as_json: bool) -> None:
    if as_json:
        print(json.dumps(
            index.to_data(), ensure_ascii=False, indent=2, sort_keys=True
        ))
        return
    print(
        f"replay {index.task_id} latest={index.latest_cursor} "
        f"terminal={'yes' if index.terminal else 'no'}"
    )
    for frame in index.frames:
        print(
            f"{frame.sequence:04d} {frame.event_type} "
            f"{frame.occurred_at.isoformat()}"
        )


def _print_replay_snapshot(snapshot: FlowReplaySnapshot, as_json: bool) -> None:
    if as_json:
        print(json.dumps(
            snapshot.to_data(), ensure_ascii=False, indent=2, sort_keys=True
        ))
        return
    target = (
        f" target={snapshot.target.node_id}@{snapshot.target.boundary.value}"
        if snapshot.target is not None else ""
    )
    print(
        f"replay {snapshot.task_id} frame={snapshot.cursor}/"
        f"{snapshot.latest_cursor} event={snapshot.frame.event_type}{target}"
    )
    print(render_flow_tree(snapshot.projection))


def _print_agent_result(
    result: (
        AgentTurnResult | AgentTurnSuspended | AgentClarificationSuspended
        | AgentContinuationSuspended
    ),
) -> None:
    if isinstance(result, AgentClarificationSuspended):
        print(
            "result reconciliation required"
            if result.kind == "OUTCOME_RECONCILIATION" else "input required"
        )
        print(f"request_id: {result.request_id}")
        print(f"task_id: {result.task_id}")
        print(f"question: {result.question}")
        print(f"reason: {result.reason}")
        print(f"kind: {result.kind}")
        for value, label in result.choices:
            print(f"- {value}: {label}")
        print(
            f"continue with: tsm-agt answer {result.request_id} "
            f"--token {result.resume_token} --answer '<text>'"
        )
        return
    if isinstance(result, AgentTurnSuspended):
        if result.approval_kind == "workspace_read":
            print("需要额外目录访问权限")
            print(f"request_id: {result.approval_request_id}")
            print(f"task_id: {result.task_id}")
            print(f"目录：{result.target}")
            print("权限：只读")
            print("范围：当前 Task（任务结束后自动失效）")
            print(f"用途：{result.action}")
            print("仍然禁止：写文件、运行命令、读取 .env/.ssh/凭据和密钥")
            print(
                "continue with: tsm-agt approve "
                f"{result.approval_request_id} --reason 'allow Task read access'"
            )
            print(
                "or reject with: tsm-agt reject "
                f"{result.approval_request_id} --reason 'not approved'"
            )
            return
        print("approval required")
        print(f"request_id: {result.approval_request_id}")
        print(f"task_id: {result.task_id}")
        print(f"risk: {result.risk}")
        print(f"action: {result.action}")
        print(f"target: {result.target}")
        print(f"preview: {result.preview}")
        print(f"full payload hash: {result.payload_hash}")
        print("files are unchanged; approval executes the full hash-bound action")
        print(f"network: {result.network_access}")
        print(f"data transmission: {result.data_transmission}")
        print(f"rollback: {result.rollback}")
        print(
            "continue with: tsm-agt approve "
            f"{result.approval_request_id} --reason 'reviewed exact action'"
        )
        print(
            "or reject with: tsm-agt reject "
            f"{result.approval_request_id} --reason 'not approved'"
        )
        return
    if isinstance(result, AgentContinuationSuspended):
        print(result.assistant_message.text)
        print(
            "continuation required: completed="
            + ",".join(result.completed_outcome_ids)
            + " remaining=" + ",".join(result.remaining_outcome_ids)
        )
        print(
            "continue this Session with ordinary input; Runtime will route it "
            "without treating the text as approval"
        )
        return
    print(result.assistant_message.text)
    print(
        f"calls: model={result.model_calls}, tool={result.tool_calls}; "
        f"usage: input={result.usage.input_tokens}, "
        f"output={result.usage.output_tokens}"
    )


def _serve_local_api(workspace: Path, host: str, port: int) -> int:
    server = LocalEventApiServer(workspace, host=host, port=port)
    server.start()
    actual_host, actual_port = server.address
    print(f"local Event API: http://{actual_host}:{actual_port}")
    print(f"Bearer token (shown once): {server.token}")
    print("events expose redacted metadata only; press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("local Event API stopped")
    finally:
        server.stop()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="tsm-agt",
        description="A local, pluggable engineering agent harness",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser(
        "init", help="create a private first-run model configuration template"
    )
    init.add_argument("--workspace", type=Path, default=Path.cwd())
    init.add_argument(
        "--print", action="store_true", dest="print_only",
        help="print the template without writing a file",
    )
    doctor = subparsers.add_parser(
        "doctor", help="diagnose installation, workspace, adapters, and model"
    )
    doctor.add_argument("--workspace", type=Path, default=Path.cwd())
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument(
        "--model-check", action="store_true",
        help="send one fixed, project-free connectivity request",
    )
    api = subparsers.add_parser(
        "api", help="run the authenticated local Runtime Event API"
    )
    api_subparsers = api.add_subparsers(dest="api_command", required=True)
    api_serve = api_subparsers.add_parser(
        "serve", help="bind an authenticated HTTP/SSE API to loopback only"
    )
    api_serve.add_argument("--workspace", type=Path, default=Path.cwd())
    api_serve.add_argument("--host", default="127.0.0.1")
    api_serve.add_argument("--port", type=int, default=8765)
    demo = subparsers.add_parser("task-demo", help="persist a legal task state trace")
    demo.add_argument("goal")
    demo.add_argument("--workspace", type=Path, default=Path.cwd())
    turn = subparsers.add_parser("turn-demo", help="run one provider-neutral text turn")
    turn.add_argument("prompt")
    turn.add_argument("--workspace", type=Path, default=Path.cwd())
    tool = subparsers.add_parser("tool-demo", help="run one fixture tool call")
    tool.add_argument("text")
    tool.add_argument("--workspace", type=Path, default=Path.cwd())
    agent = subparsers.add_parser(
        "agent-demo", help="run one bounded fixture Agent loop"
    )
    agent.add_argument("prompt")
    agent.add_argument("--workspace", type=Path, default=Path.cwd())
    files = subparsers.add_parser(
        "files-demo", help="list workspace files through the core tool boundary"
    )
    files.add_argument("path", nargs="?", default=".")
    files.add_argument("--recursive", action="store_true")
    files.add_argument("--workspace", type=Path, default=Path.cwd())
    real_agent = subparsers.add_parser(
        "agent", help="run one real OpenAI-compatible engineering Agent task"
    )
    real_agent.add_argument("prompt")
    real_agent.add_argument("--workspace", type=Path, default=Path.cwd())
    real_agent.add_argument(
        "--verbose-progress", action="store_true",
        help="show full tool arguments, policy projections, and budget counters",
    )
    chat = subparsers.add_parser(
        "chat", help="start or resume an interactive engineering Session"
    )
    chat.add_argument("--session", dest="session_id")
    chat.add_argument("--title")
    chat.add_argument("--workspace", type=Path, default=Path.cwd())
    chat.add_argument(
        "--verbose-progress", action="store_true",
        help="show full tool arguments, policy projections, and budget counters",
    )
    resume = subparsers.add_parser(
        "resume", help="resume one interrupted Agent turn from its checkpoint"
    )
    resume.add_argument("task_id")
    resume.add_argument("--workspace", type=Path, default=Path.cwd())
    approve = subparsers.add_parser(
        "approve", help="approve a pending Agent action and resume its turn"
    )
    approve.add_argument("approval_id")
    approve.add_argument("--reason", required=True)
    approve.add_argument("--workspace", type=Path, default=Path.cwd())
    reject = subparsers.add_parser(
        "reject", help="reject a pending Agent action and resume its turn"
    )
    reject.add_argument("approval_id")
    reject.add_argument("--reason", required=True)
    reject.add_argument("--workspace", type=Path, default=Path.cwd())
    answer = subparsers.add_parser(
        "answer", help="answer a pending clarification and resume its turn"
    )
    answer.add_argument("request_id")
    answer.add_argument("--token", required=True)
    answer_value = answer.add_mutually_exclusive_group(required=True)
    answer_value.add_argument("--answer", dest="answer_text")
    answer_value.add_argument("--choice", dest="selected_choice")
    answer.add_argument("--workspace", type=Path, default=Path.cwd())
    trust = subparsers.add_parser(
        "trust", help="inspect or set trust for one canonical workspace"
    )
    trust_subparsers = trust.add_subparsers(dest="trust_command", required=True)
    trust_get = trust_subparsers.add_parser("get", help="show effective project trust")
    trust_get.add_argument("--workspace", type=Path, default=Path.cwd())
    trust_set = trust_subparsers.add_parser("set", help="set project trust explicitly")
    trust_set.add_argument(
        "level", choices=[level.value for level in ProjectTrustLevel]
    )
    trust_set.add_argument("--workspace", type=Path, default=Path.cwd())
    memory = subparsers.add_parser(
        "memory", help="inspect durable memories visible to one Task"
    )
    memory_subparsers = memory.add_subparsers(
        dest="memory_command", required=True
    )
    memory_list = memory_subparsers.add_parser(
        "list", help="list current sourced facts without changing them"
    )
    memory_list.add_argument("task_id")
    memory_list.add_argument("--include-stale", action="store_true")
    memory_list.add_argument("--workspace", type=Path, default=Path.cwd())
    memory_list.add_argument("--json", action="store_true")
    working_memory = subparsers.add_parser(
        "working-memory", help="inspect temporary goal, plan, progress, and evidence"
    )
    working_memory_subparsers = working_memory.add_subparsers(
        dest="working_memory_command", required=True
    )
    working_memory_show = working_memory_subparsers.add_parser(
        "show", help="show one Task scratchpad without changing it"
    )
    working_memory_show.add_argument("task_id")
    working_memory_show.add_argument("--workspace", type=Path, default=Path.cwd())
    working_memory_show.add_argument("--json", action="store_true")
    session = subparsers.add_parser(
        "session", help="create and inspect durable multi-Task Sessions"
    )
    session_subparsers = session.add_subparsers(
        dest="session_command", required=True
    )
    session_create = session_subparsers.add_parser("create")
    session_create.add_argument("title")
    session_list = session_subparsers.add_parser("list")
    session_list.add_argument("--include-archived", action="store_true")
    session_list.add_argument("--json", action="store_true")
    session_show = session_subparsers.add_parser("show")
    session_show.add_argument("session_id")
    session_show.add_argument("--json", action="store_true")
    session_close = session_subparsers.add_parser("close")
    session_close.add_argument("session_id")
    session_close.add_argument("--reason", required=True)
    session_archive = session_subparsers.add_parser("archive")
    session_archive.add_argument("session_id")
    session_select = session_subparsers.add_parser("select-task")
    session_select.add_argument("session_id")
    session_select.add_argument("task_id")
    session_flow = session_subparsers.add_parser("flow")
    session_flow.add_argument("session_id")
    session_flow.add_argument("--after", type=int, default=0)
    session_flow.add_argument("--json", action="store_true")
    for session_parser in (
        session_create, session_list, session_show, session_close,
        session_archive, session_select, session_flow,
    ):
        session_parser.add_argument(
            "--workspace", type=Path, default=Path.cwd()
        )
    for session_parser in (session_close, session_archive, session_select):
        session_parser.add_argument("--expected-version", type=int)
    task = subparsers.add_parser("task", help="create a Task in a Session")
    task_subparsers = task.add_subparsers(dest="task_command", required=True)
    task_create = task_subparsers.add_parser("create")
    task_create.add_argument("goal")
    task_create.add_argument("--session", dest="session_id")
    task_create.add_argument("--command-id")
    task_create.add_argument("--workspace", type=Path, default=Path.cwd())
    steering = subparsers.add_parser(
        "steering", help="queue or inspect runtime steering for an active Task"
    )
    steering_subparsers = steering.add_subparsers(
        dest="steering_command", required=True
    )
    for name in ("steer", "replace"):
        control = steering_subparsers.add_parser(name)
        control.add_argument("task_id")
        control.add_argument("text")
        control.add_argument("--command-id", required=True)
        control.add_argument("--workspace", type=Path, default=Path.cwd())
    steering_show = steering_subparsers.add_parser("show")
    steering_show.add_argument("task_id")
    steering_show.add_argument("--workspace", type=Path, default=Path.cwd())
    steering_show.add_argument("--json", action="store_true")
    onboarding = subparsers.add_parser(
        "onboarding", help="run or inspect read-only project discovery"
    )
    onboarding_subparsers = onboarding.add_subparsers(
        dest="onboarding_command", required=True
    )
    onboarding_run = onboarding_subparsers.add_parser(
        "run", help="run/resume static discovery without executing project code"
    )
    onboarding_run.add_argument("task_id")
    onboarding_run.add_argument("--workspace", type=Path, default=Path.cwd())
    onboarding_run.add_argument("--json", action="store_true")
    onboarding_show = onboarding_subparsers.add_parser(
        "show", help="show sourced facts from the last completed discovery"
    )
    onboarding_show.add_argument("task_id")
    onboarding_show.add_argument("--include-stale", action="store_true")
    onboarding_show.add_argument("--workspace", type=Path, default=Path.cwd())
    onboarding_show.add_argument("--json", action="store_true")
    flow = subparsers.add_parser(
        "flow", help="show a persisted task flow projection"
    )
    flow.add_argument("task_id")
    flow.add_argument("--workspace", type=Path, default=Path.cwd())
    flow.add_argument("--json", action="store_true")
    flow.add_argument(
        "--node", metavar="NODE_ID",
        help="show payload-free diagnostics for one projected node",
    )
    flow.add_argument(
        "--failure", action="store_true",
        help="show diagnostics for the first actionable failure",
    )
    flow.add_argument(
        "--kind", action="append", type=FlowNodeKind,
        choices=list(FlowNodeKind), metavar="KIND",
        help="filter by node kind; repeat for OR matching",
    )
    flow.add_argument(
        "--status", action="append", type=FlowNodeStatus,
        choices=list(FlowNodeStatus), metavar="STATUS",
        help="filter by node status; repeat for OR matching",
    )
    flow.add_argument(
        "--export", type=Path, metavar="PATH",
        help="write a redacted .json, .jsonl, or offline .html artifact",
    )
    flow.add_argument(
        "--timeline", action="store_true",
        help="show a payload-free chronological swimlane projection",
    )
    flow.add_argument(
        "--tui", action="store_true",
        help="show a dependency-free terminal dashboard (combine with --live)",
    )
    flow.add_argument(
        "--live", action="store_true",
        help="refresh when persisted events advance; stop on terminal task or Ctrl+C",
    )
    flow.add_argument(
        "--poll-interval", type=float, default=0.5, metavar="SECONDS",
        help=argparse.SUPPRESS,
    )
    inspect = subparsers.add_parser(
        "inspect", help="inspect an immutable persisted task snapshot"
    )
    inspect.add_argument("task_id")
    inspect.add_argument(
        "--effective-config", action="store_true", required=True,
        help="show the redacted Effective Configuration snapshot",
    )
    inspect.add_argument("--revision", type=int)
    inspect.add_argument("--workspace", type=Path, default=Path.cwd())
    inspect.add_argument("--json", action="store_true")
    replay = subparsers.add_parser(
        "replay", help="rebuild a historical Flow snapshot without rerunning work"
    )
    replay.add_argument("task_id")
    replay.add_argument("--workspace", type=Path, default=Path.cwd())
    replay_mode = replay.add_mutually_exclusive_group()
    replay_mode.add_argument(
        "--list", action="store_true", help="list payload-free Event frames"
    )
    replay_mode.add_argument(
        "--at", type=int, metavar="SEQ",
        help="show the Flow state immediately after Event SEQ",
    )
    replay_mode.add_argument(
        "--turn", metavar="TURN_ID",
        help="jump to a Turn node boundary; accepts ID with or without turn: prefix",
    )
    replay_mode.add_argument(
        "--node", metavar="NODE_ID",
        help="jump to a projected node boundary",
    )
    replay_mode.add_argument(
        "--play", action="store_true",
        help="play all validated Event frames in recorded order",
    )
    replay.add_argument(
        "--boundary", type=FlowReplayBoundary,
        choices=list(FlowReplayBoundary), default=FlowReplayBoundary.END,
        help="node/Turn boundary to show (default: end)",
    )
    replay.add_argument(
        "--speed", type=FlowReplaySpeed, choices=list(FlowReplaySpeed),
        default=FlowReplaySpeed.REALTIME,
        help="playback speed for --play (default: 1x)",
    )
    replay.add_argument(
        "--max-wait-seconds", type=float, default=2.0, metavar="SECONDS",
        help="cap one playback wait and mark compressed gaps (default: 2)",
    )
    replay.add_argument(
        "--from", dest="from_sequence", type=int, metavar="SEQ",
        help="start --play at Event SEQ",
    )
    replay.add_argument(
        "--to", dest="to_sequence", type=int, metavar="SEQ",
        help="stop --play after Event SEQ",
    )
    replay.add_argument(
        "--step", action="store_true",
        help="with --play, wait for Enter before each frame; q stops viewing",
    )
    replay.add_argument(
        "--save-cursor", type=Path, metavar="PATH",
        help="atomically save the last displayed Event cursor inside workspace",
    )
    replay.add_argument(
        "--resume-cursor", type=Path, metavar="PATH",
        help="resume after a saved Event cursor inside workspace",
    )
    replay.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.command == "init":
        try:
            return initialize_workspace(args.workspace, print_only=args.print_only)
        except (OSError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    if args.command == "doctor":
        return asyncio.run(run_doctor(
            args.workspace, as_json=args.json, model_check=args.model_check
        ))
    if args.command == "api":
        try:
            return _serve_local_api(args.workspace, args.host, args.port)
        except (OSError, RuntimeError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    if args.command == "task-demo":
        return asyncio.run(_task_demo(args.goal, args.workspace))
    if args.command == "turn-demo":
        return asyncio.run(_turn_demo(args.prompt, args.workspace))
    if args.command == "tool-demo":
        return asyncio.run(_tool_demo(args.text, args.workspace))
    if args.command == "agent-demo":
        return asyncio.run(_agent_demo(args.prompt, args.workspace))
    if args.command == "files-demo":
        return asyncio.run(_files_demo(args.path, args.workspace, args.recursive))
    if args.command == "agent":
        try:
            return asyncio.run(_agent(
                args.prompt, args.workspace,
                verbose_progress=args.verbose_progress,
            ))
        except ModelConfigurationError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        except (AgentLoopLimitExceeded, ModelInvocationFailed) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        except ValueError as error:
            parser.error(str(error))
    if args.command == "chat":
        try:
            return asyncio.run(_chat(
                args.workspace, session_id=args.session_id, title=args.title,
                verbose_progress=args.verbose_progress,
            ))
        except KeyboardInterrupt:
            # A second Ctrl+C may arrive while asyncio is closing adapters. Keep the
            # terminal clean; the first signal already requested safe interruption.
            print("\nchat interrupted")
            return 130
        except ModelConfigurationError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        except (LookupError, PermissionError, RuntimeError, ValueError) as error:
            parser.error(str(error))
    if args.command == "resume":
        try:
            return asyncio.run(_resume_agent_task(args.task_id, args.workspace))
        except (LookupError, RuntimeError, ValueError) as error:
            parser.error(str(error))
    if args.command in {"approve", "reject"}:
        decision = (
            ApprovalDecision.APPROVE
            if args.command == "approve"
            else ApprovalDecision.DENY
        )
        try:
            return asyncio.run(
                _resolve_agent_approval(
                    args.approval_id, decision, args.reason, args.workspace
                )
            )
        except (ValueError, LookupError) as error:
            parser.error(str(error))
    if args.command == "answer":
        try:
            return asyncio.run(_resolve_agent_clarification(
                args.request_id, args.token, args.answer_text, args.workspace,
                selected_choice=args.selected_choice,
            ))
        except (ValueError, LookupError, PermissionError) as error:
            parser.error(str(error))
    if args.command == "trust":
        try:
            if args.trust_command == "get":
                return asyncio.run(_project_trust_get(args.workspace))
            return asyncio.run(
                _project_trust_set(
                    args.workspace, ProjectTrustLevel(args.level)
                )
            )
        except (TypeError, ValueError) as error:
            parser.error(str(error))
    if args.command == "memory":
        try:
            return asyncio.run(_memory_list(
                args.task_id, args.workspace, args.include_stale, args.json
            ))
        except (LookupError, RuntimeError, ValueError) as error:
            parser.error(str(error))
    if args.command == "working-memory":
        try:
            return asyncio.run(_working_memory_show(
                args.task_id, args.workspace, args.json
            ))
        except (LookupError, RuntimeError, ValueError) as error:
            parser.error(str(error))
    if args.command == "session":
        try:
            return asyncio.run(_session_command(args))
        except (LookupError, PermissionError, RuntimeError, ValueError) as error:
            parser.error(str(error))
    if args.command == "task":
        try:
            return asyncio.run(_task_create(
                args.goal, args.workspace, args.session_id, args.command_id
            ))
        except (LookupError, PermissionError, RuntimeError, ValueError) as error:
            parser.error(str(error))
    if args.command == "steering":
        try:
            return asyncio.run(_steering_command(args))
        except (LookupError, PermissionError, RuntimeError, ValueError) as error:
            parser.error(str(error))
    if args.command == "onboarding":
        try:
            if args.onboarding_command == "run":
                return asyncio.run(_onboarding_run(
                    args.task_id, args.workspace, args.json
                ))
            return asyncio.run(_onboarding_show(
                args.task_id, args.workspace, args.include_stale, args.json
            ))
        except (LookupError, RuntimeError, ValueError) as error:
            parser.error(str(error))
    if args.command == "flow":
        try:
            return asyncio.run(_flow_query(
                args.task_id, args.workspace, args.json, live=args.live,
                node_id=args.node, kinds=tuple(args.kind or ()),
                statuses=tuple(args.status or ()),
                export_path=args.export,
                timeline=args.timeline, tui=args.tui,
                failure=args.failure,
                poll_interval=args.poll_interval,
            ))
        except KeyboardInterrupt:
            print("flow watch stopped")
            return 130
        except (FileNotFoundError, LookupError, RuntimeError, ValueError) as error:
            parser.error(str(error))
    if args.command == "inspect":
        try:
            return asyncio.run(_inspect_effective_configuration(
                args.task_id, args.workspace, args.revision, args.json
            ))
        except (FileNotFoundError, LookupError, RuntimeError, ValueError) as error:
            parser.error(str(error))
    if args.command == "replay":
        try:
            return asyncio.run(_replay(
                args.task_id, args.workspace, list_frames=args.list,
                at_sequence=args.at, turn_id=args.turn, node_id=args.node,
                boundary=args.boundary, play=args.play, speed=args.speed,
                max_wait_seconds=args.max_wait_seconds, as_json=args.json,
                from_sequence=args.from_sequence, to_sequence=args.to_sequence,
                step=args.step, save_cursor=args.save_cursor,
                resume_cursor=args.resume_cursor,
            ))
        except KeyboardInterrupt:
            print("replay playback stopped; original task was not changed")
            return 130
        except (FileNotFoundError, LookupError, RuntimeError, ValueError) as error:
            parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
