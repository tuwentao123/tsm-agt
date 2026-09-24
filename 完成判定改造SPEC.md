# tsm-agt 完成判定改造 SPEC

## 1. 问题陈述

Runtime 已经正确记录命令业务失败，但完成判定层拿不到这个事实，导致 Task 被误判成功。

已核实的断点链路：

```text
ProcessResult.succeeded = false        ← 事实正确记录
  → ToolResult.ok = true               ← 调用层成功，掩盖业务失败
  → last_tool_error 未赋值             ← kernel.py:11111 仅判 not result.ok
  → CompletionReadinessProbe 无失败输入 ← ports/completion_readiness.py:128
  → gap_count = 0
  → action = COMPLETE
  → Task SUCCEEDED
```

复现任务：`task-b907f9c866f64f9f912676b448b77bff`，`git push` 退出码 128，Task 仍标记成功。

### 1.2 反向缺陷：成功被误判未完成

同一职责错位还有对称的另一面：Runtime 以模型的临时笔记覆盖了自己记录的真实事实。

```text
mutation 已提交
pytest exit=0 succeeded=true
  → WorkingMemory plan 未回写，步骤仍 PENDING
  → PLAN_STEP gap required=True 且 recoverable=False
  → REPORT_BLOCKED 且指令禁止调用工具
  → 唯一出路（core.working_memory_update）被封
  → 纠正配额耗尽 → 强制 COMPLETE
  → FinalAcceptance INCOMPLETE_REQUIRED_PLAN → BLOCK
```

复现任务：`task-a1ca6115134a42759ad2428eeee3e6dd`，代码与测试均已完成，Task 仍判定 blocked。详见 5.5。

两个缺陷方向相反、根因相同：完成判定没有以 Runtime 自身记录的执行事实为准。因此同批修复。

## 2. 设计原则

1. **不新增持久化实体**。事实由现有不可变记录派生，避免双账本。
2. **不新增完成判定体系**。`CompletionReadinessPolicy` 承担 Goal Reviewer 角色。
3. **不按业务分派**。判定只依赖 `ToolEffect` 与 `ToolResultAuthority`，不认识 git/部署/HTTP。
4. **保留 Protocol 与 Goal 分层**。确定性阻断先于语义完成判断。
5. **默认值兼容**。新增字段一律有默认值，历史快照与事件可直接回放。

## 3. 现状核实

| 组件 | 位置 | 状态 |
|---|---|---|
| `ToolExecutionRecord` | `core/execution.py:25` | 有完整生命周期，**无** `effect` / `result_authority` |
| `ToolCommitState` | `core/execution.py:14` | `COMMITTED/FAILED/CANCELLED/UNKNOWN_OUTCOME` |
| `ToolEffect` | `ports/tool.py:31` | 7 值封闭枚举 |
| `ToolResultAuthority` | `ports/tool.py:49` | 9 值封闭枚举 |
| `ProcessResult.succeeded/failure_code` | `ports/process.py` | 已实施 |
| `CompletionReadinessProbe` | `ports/completion_readiness.py:128` | 10 字段，**无失败动作输入** |
| `CompletionReadinessAction` | `ports/completion_readiness.py:15` | 4 值，已够用 |
| `_evaluate_completion_readiness` | `core/kernel.py:7139` | **已持有 `task.tool_executions`** |
| `_latest_recoverable_tool_batch` | `core/kernel.py:9346` | **已批次范围、已通用、并行就绪** |
| `ToolBatchSnapshot` | `core/agent_loop.py:193` | 批次协议边界，为并行预留 |
| 取消能力 | `CancelTaskInput` / `TaskState.CANCELLED` | 已完整 |
| Outcome 死代码 | `kernel.py:6953` `for outcome in ()` | 恒空，不可达 |

## 4. 目标流程

```text
User Goal
    ↓
Goal View（现有 goal / TaskSpec / Policy 的只读视图）
    ↓
Task Runtime Loop（现有）
    ↓
Model → ToolCall → ToolResult
    ↓
现有执行记录
    ├── ToolExecutionRecord + ToolCommitState
    ├── ProcessResult.succeeded / failure_code
    ├── Mutation Journal
    └── SemanticAction
    ↓
ExecutionFactProjector（新增，纯函数）
    ↓
ExecutionFact（只读派生）
    ├── invocation_status
    ├── effect_status    succeeded | failed | unknown
    └── resolution       unresolved | retried | superseded
    ↓
模型提出最终回复
    ↓
Protocol Gate（确定性）
    ├── 未闭合批次 / 活跃进程
    ├── 等待审批 / 等待用户
    ├── 已请求取消
    └── unresolved effect failure
    ↓
CompletionReadinessPolicy（现有，承担 Goal Reviewer）
    ↓
COMPLETE | CONTINUE | REPORT_INCOMPLETE_RECOVERABLE | REPORT_BLOCKED
    ↓
FinalAcceptancePolicy + verify_task_acceptance（现有 fail-closed）
```

## 5. 首批改动（修复缺陷所必需）

### 5.1 新增投影模块

**新文件**：`src/tsm_agt/core/execution_facts.py`

```python
class EffectStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class FactResolution(StrEnum):
    UNRESOLVED = "unresolved"
    RETRIED = "retried"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class ExecutionFact:
    execution_id: str
    tool_name: str
    effect: ToolEffect
    invocation_status: ToolCommitState
    effect_status: EffectStatus
    resolution: FactResolution
    failure_code: str = ""
    detail: str = ""
```

判定表，按 `result_authority` 分派，不按工具名：

| 条件 | effect_status |
|---|---|
| `effect in {OBSERVE, INTERNAL}` | 不产出 fact |
| `result is None` | 不产出 fact（未终态） |
| `result.ok is False` | FAILED |
| `state is CANCELLED` | UNKNOWN |
| `state is UNKNOWN_OUTCOME` | UNKNOWN |
| `authority is PROCESS_FACT` 且 `data.status == "cancelled"` | UNKNOWN |
| `authority is PROCESS_FACT` 且 `data.succeeded is False` | FAILED |
| `authority is MUTATION_FACT` 且无对应 mutation 记录 | UNKNOWN |
| 其他且 `result.ok is True` | SUCCEEDED |

`resolution` 跨记录比对，仅用 `call.name + payload_hash` 等值签名：

- 后续存在同签名且 `SUCCEEDED` → `RETRIED`
- 后续存在同 `effect` 且 `SUCCEEDED` 的其他动作 → `SUPERSEDED`
- 否则 `UNRESOLVED`

约束：本模块纯函数、无 IO、无 await，可独立单测。

### 5.2 恢复链接入

**文件**：`core/kernel.py`

新增与 `_latest_recoverable_tool_batch` 并列的方法，不修改 `ToolResult` 语义：

```python
def _unresolved_failure_facts(
    self, executions, visible_tools,
) -> tuple[ExecutionFact, ...]:
    """Effect-level failures that no later action resolved."""
```

`structured_recovery` 的判定从单一来源扩展为二者取并：

```python
# 现状 kernel.py:11258
recovery_batch = self._latest_recoverable_tool_batch(messages)
recovery_result = recovery_batch[-1] if recovery_batch else None
structured_recovery = recovery_result is not None

# 改造后
failure_facts = self._unresolved_failure_facts(...)
structured_recovery = recovery_result is not None or bool(failure_facts)
```

`last_tool_error`（kernel.py:11111）同步改为：

```python
fact = project_execution_fact(record, result, spec)
if fact is not None and fact.effect_status is EffectStatus.FAILED:
    last_tool_error = f"{call.name}: {fact.failure_code}"
```

**设计取舍**：另一种做法是让 process tool adapter 在业务失败时直接设置 `ToolRecoveryKind`，可以复用现有屏障不改 kernel。否决原因是那会把「调用成功但效果失败」混入 `ToolResult` 的恢复语义，使 `ok` 的含义变模糊，且每个 adapter 都要重复这个判断。投影层集中判断更可控。

### 5.3 完成判定接入

**文件**：`ports/completion_readiness.py`

```python
@dataclass(frozen=True, slots=True)
class CompletionReadinessProbe:
    ...
    unresolved_failures: tuple[ExecutionFact, ...] = ()   # 新增，默认空
```

**文件**：`core/kernel.py` `_completion_readiness_gaps()`

为每条未解决失败产出一条 gap，只新增一个 kind：

```python
CompletionGap(
    gap_id=f"execution-failure:{fact.execution_id}",
    kind="UNRESOLVED_EFFECT_FAILURE",
    description=f"{fact.tool_name} reported {fact.failure_code}",
    status=fact.effect_status.value,
    required=True,
    required_effects=(fact.effect,),
    observed=fact.detail,
)
```

**文件**：`core/kernel.py:7185` `successful_tool_calls`

现状口径需同步修正，否则同一 probe 内两个字段对「成功」的定义不一致：

```python
# 现状
execution.state is ToolCommitState.COMMITTED
and execution.result is not None and execution.result.ok

# 改造后：排除 effect_status is FAILED 的执行
```

不新增 `CompletionReadinessAction`。现有 rule-based policy 依据新 gap 返回 `CONTINUE` 或 `REPORT_INCOMPLETE_RECOVERABLE`。

### 5.4 上下文摘要保真

**文件**：`core/session_context.py` 及 kernel 的结果摘要构建处

非 OBSERVE 工具的结果摘要必须保留 `data.succeeded` 与 `data.failure_code`。当前只保留 `ok` / `error_code`，模型下一轮看不到真实业务结果。

### 5.5 降级 scratchpad 硬门（原 6.3，已提升为首批）

**文件**：`core/kernel.py` `_completion_readiness_gaps()`、`_evaluate_final_acceptance()`

**提升原因**：该缺陷会让任何忘记回写 scratchpad 的 Task 无法正常收尾，与业务无关，且已在真实任务中造成死锁。它不是「为后续迭代准备」，而是与 5.2/5.3 同级的通用阻断缺陷。

#### 5.5.1 实测死锁证据

复现任务：`task-a1ca6115134a42759ad2428eeee3e6dd`，共 679 条事件。

真实执行事实完整：

```text
[327] working_memory.updated   revision 1 → 2   仅此一次，写下 S1–S4
[384] MUTATION src/tsm_agt/core/session_context.py
[385] MUTATION src/tsm_agt/core/kernel.py
[386] MUTATION tests/test_kernel_tasks.py
[452] PROCESS exit=1 succeeded=False
[531] MUTATION tests/test_kernel_tasks.py
[562] PROCESS exit=1 succeeded=False
[618] MUTATION tests/test_kernel_tasks.py
[649] PROCESS exit=0 succeeded=True           ← 验证命令真实通过
```

23 次工具调用中仅 `#1 core.working_memory_read` 与 `#12 core.working_memory_update` 触及 scratchpad，其后再无更新。因此 S1 始终 `IN_PROGRESS`、S2–S4 始终 `PENDING`，`completed_work` 始终为空。

首次完成判定的实际 probe：

```text
action:  REPORT_BLOCKED
reason:  required_work_is_blocked_or_correction_limit_reached
forced_wrap_up: False
remaining_model_calls: 15
remaining_tool_calls:  97
gaps: plan-step:S1..S4
      kind=PLAN_STEP  required=True  recoverable=False
      required_effects=[]  candidate_tools=[]
```

最终结果：

```text
[658] READINESS REPORT_BLOCKED
[661] completion.blocker_disclosure_requested
[665] READINESS COMPLETE  reason=bounded_completion_corrections_exhausted
[669] verify.final_evidence_evaluated BLOCK
      INCOMPLETE_REQUIRED_PLAN × 4
[677] verify.completed status=blocked
```

预算充足（15 次模型调用、97 次工具调用）却判定阻塞，唯一原因是 scratchpad 未回写。

#### 5.5.2 三重缺陷叠加形成死锁

1. **scratchpad 被当作硬 Gate。** `_completion_readiness_gaps()` 把 `PENDING/IN_PROGRESS` 计划步骤产出为 `required=True` 的 gap；`_evaluate_final_acceptance()` 再将其作为 `INCOMPLETE_REQUIRED_PLAN` 违规。Runtime 因此以模型的临时笔记覆盖了自己掌握的真实事实（mutation + 成功验证命令）。

2. **`PLAN_STEP` gap 结构上不可恢复。** 该 gap 的 `required_effects=[]`，而 `rule_based_completion_readiness/policy.py` 的 `recoverable` 判定要求 `gap.effective_required_effects` 非空且被 `available_effects` 覆盖。空集合永远不进入 `recoverable`，因此 `can_continue` 恒为 `False`，直接落入 `REPORT_BLOCKED`，无法获得 `CONTINUE`。

3. **`REPORT_BLOCKED` 的 runtime instruction 明确禁止工具调用。** 该分支下发 `Do not call tools. State the exact blocker`，而唯一能解开 gap 的动作恰恰是调用 `core.working_memory_update`。

叠加后形成闭环：

```text
唯一出路 = 调用 core.working_memory_update
Runtime 指令 = 不许调用任何工具
模型只能用散文解释「已完成但无法标记」
纠正配额耗尽 → 强制 COMPLETE
FinalAcceptance → INCOMPLETE_REQUIRED_PLAN → BLOCK
```

#### 5.5.3 职责边界

`core/working_memory_tools.py` 的 `core.working_memory_update` 为 `effect=INTERNAL`、`is_internal_state=True`，且工具自述为：

```text
This is inspectable temporary state, not durable user/project memory.
```

据此确认职责：

| 角色 | 职责 |
|---|---|
| 模型 | 决定 scratchpad 内容与步骤状态，显式调用 `core.working_memory_update` |
| Runtime | 校验 `expected_revision` / `operation_id`，持久化并投影 |
| Runtime | **不得**从 `pytest exit=0` 之类事实反推 `S4` 是否完成 |
| Runtime | **不得**以模型笔记覆盖自身记录的执行事实 |

Runtime 没有语义能力把 `pytest validation` 这类自然语言步骤映射到具体命令，因此既不能代替模型回写，也不应把回写结果当作验收条件。

#### 5.5.4 改动内容

`_completion_readiness_gaps()` 不再产出 `PLAN_STEP` 类型的 `required=True` gap。计划中未完成的步骤改为 runtime instruction 提示，例如：

```text
Working-memory plan still lists unfinished steps. If the underlying work is
already done, update the scratchpad with core.working_memory_update before
finishing; the plan itself is not a delivery contract.
```

`_evaluate_final_acceptance()` 的 `required_plan_steps` 不再作为 `INCOMPLETE_REQUIRED_PLAN` 违规来源。

完成判定改为依据 5.1–5.3 已有的真实事实：

```text
mutation fact
effect_status（含 PROCESS_FACT succeeded）
unresolved failure
evidence question 完整性
```

#### 5.5.5 为何不采用「让 PLAN_STEP 可恢复」的替代方案

替代方案是保留该 gap，但声明：

```python
required_effects=(ToolEffect.INTERNAL,)
candidate_tools=("core.working_memory_update",)
```

这样会走 `CONTINUE` 而非 `REPORT_BLOCKED`，模型可补一次回写。

否决理由：

1. 每个任务都会多出一轮纯记账用途的模型调用；
2. scratchpad 不是交付物，用它当验收条件属于职责错位；
3. 仍未解决「Runtime 以笔记覆盖事实」这一根本问题；
4. 模型仍可能再次忘记回写，缺陷只是概率降低。

若后续确实需要强约束计划完整性，应通过显式 Spec/Task Runner 的正式任务清单表达，而不是通过模型的临时笔记。

## 6. 第二批改动（为后续迭代准备）

### 6.1 冻结执行语义

`ToolExecutionRecord` 增加三个带默认值的字段，`start()` 时从 `ToolSpec` 写入，终态事件 payload 同步：

```python
effect: ToolEffect = ToolEffect.UNSPECIFIED
result_authority: ToolResultAuthority = ToolResultAuthority.UNSPECIFIED
semantic_signature: str = ""
```

**必要性**：首批不需要，因为 `visible_tools` 在两个接线点均在作用域内。但以下场景必需：

- MCP 工具被卸载或改版后重放历史事件
- 工具 spec 变更后的历史任务审计
- 并行执行时避免重复查 registry

### 6.2 Protocol Gate 显式化

将当前分散在 `_continue_agent_turn` 各分支的确定性检查收敛为一个方法，插在 final-response 判定之前：

```python
async def _protocol_gate(task_id) -> ProtocolBlock | None:
    # 1. 未闭合 tool batch / 活跃前台调用
    # 2. AWAITING_APPROVAL / AWAITING_USER
    # 3. 已请求取消 → CANCELLED，不进 Goal Reviewer
    # 4. unresolved + relevant effect failure（带一次性配额）
```

约束：只读 Runtime 可验证状态，**不读 WorkingMemory plan**。

第 4 项的一次性配额复用 `CompletionReadinessState` 现有有界纠正计数，防止反复失败导致任务永不结束。

### 6.3 降级 scratchpad 硬门（已移至首批 5.5）

原第二批条目已提升为首批 5.5，理由与实测死锁证据见该节。本节保留编号以便对照历史版本。

### 6.4 Outcome 死代码清理

前置条件：先统计历史分布。

```sql
SELECT event_type, COUNT(*) FROM runtime_events
WHERE event_type LIKE 'task_outcome.%' GROUP BY event_type;
```

确认后删除 `_completion_readiness_gaps()` 与 `verify_task_acceptance()` 中的 `for outcome in ()` 及其不可达分支。保留 Outcome 的兼容读取，停止新写。

## 7. 测试要求

### 7.1 通用参数化单测（保证规则正确）

对 `execution_facts.py` 参数化覆盖 `effect × result_authority × 结果组合`，使用最简 fake tool，**不涉及任何真实命令**。

| 输入 | 期望 effect_status |
|---|---|
| OBSERVE + ok | 不产出 fact |
| EXECUTE + PROCESS_FACT + succeeded=true | SUCCEEDED |
| EXECUTE + PROCESS_FACT + succeeded=false | FAILED |
| EXECUTE + PROCESS_FACT + status=cancelled | UNKNOWN |
| MUTATE + MUTATION_FACT + 无 mutation | UNKNOWN |
| 任意 + ok=false | FAILED |
| 任意 + UNKNOWN_OUTCOME | UNKNOWN |

`resolution` 单测：同签名后续成功 → RETRIED；同 effect 其他动作成功 → SUPERSEDED；无后续 → UNRESOLVED。

### 7.2 集成场景（保证不过度阻断）

| 场景 | 期望 |
|---|---|
| 副作用动作失败后模型准备结束 | CONTINUE |
| 模型换方式达成同一效果 | COMPLETE |
| 纯读取工具无结果 | COMPLETE |
| 动作被取消 | 不作为失败阻断 |
| 用户取消任务 | CANCELLED |
| 同一失败重复出现 | 一次强制继续后 REPORT_INCOMPLETE_RECOVERABLE |

### 7.3 scratchpad 降级专项（5.5）

| 场景 | 期望 |
|---|---|
| plan 存在 `PENDING`/`IN_PROGRESS`，但 mutation 与成功验证命令齐备 | `COMPLETE`，且 FinalAcceptance 不产生 `INCOMPLETE_REQUIRED_PLAN` |
| plan 全部 `COMPLETED`，但存在 unresolved effect failure | 不允许 `COMPLETE`（5.2/5.3 仍生效） |
| plan 为空 | 行为与现状一致 |
| plan 未完成且无任何 mutation/验证事实 | 仍因 5.1–5.3 的事实缺失而不能 `COMPLETE` |
| plan 未完成 | runtime instruction 包含回写提示，但不产生 required gap |

关键反向断言：**降级后不能出现「无任何执行事实却判定 COMPLETE」**。计划不再是 gate，但事实依然是。

### 7.4 回归测试（锁定已知故障）

两个历史故障各锁一条：

```text
task-b907f9c866f64f9f912676b448b77bff
  git push exit 128
  断言 gap_count > 0 且 action != COMPLETE

task-a1ca6115134a42759ad2428eeee3e6dd
  plan 未回写但 mutation + pytest exit=0 齐备
  断言 action == COMPLETE
  断言 verify.completed.status != blocked
  断言无 INCOMPLETE_REQUIRED_PLAN 违规
```

两个用例允许包含具体命令，因为它们锁定的是历史缺陷形状，不参与通用规则验证。

### 7.5 回归范围

全量套件基线 `956 passed, 8 skipped`。重点检查断言 `completion.readiness_evaluated` payload、快照结构、`successful_tool_calls` 的既有测试。

## 8. 改动面汇总

| 类型 | 首批 | 第二批 |
|---|---|---|
| 新增文件 | `core/execution_facts.py` | — |
| 新增字段 | `CompletionReadinessProbe` 1 个 | `ToolExecutionRecord` 3 个 |
| 新增枚举 | `EffectStatus`、`FactResolution`、1 个 gap kind | — |
| 移除 gap | `PLAN_STEP` 不再作为 required gap（5.5） | — |
| 修改方法 | `last_tool_error` 判定、`structured_recovery`、`_completion_readiness_gaps`、`successful_tool_calls`、结果摘要、`_evaluate_final_acceptance` | Protocol Gate 收敛 |
| 不新增 | 持久化实体、事件类型、Port、Task 状态、Action 枚举 | 同 |
| 回退方式 | 投影恒返回 `None`；plan gap 可重新启用 | 同 |

首批两类缺陷互补：

```text
5.1–5.4  修「失败被谎报成功」
5.5      修「成功被误判未完成」
```

两者都源于同一职责错位——完成判定没有以 Runtime 自身记录的执行事实为准。

## 9. 对后续迭代的适配

| 迭代项 | 适配方式 |
|---|---|
| Skill / MCP 工具 | 声明 `effect` + `result_authority` 即自动被覆盖，判定代码零改动 |
| 工具并行执行 | 投影是对批次终态的纯函数计算，与完成顺序无关；`_latest_recoverable_tool_batch` 已是批次范围 |
| 任务取消 | 复用 `ProcessExitStatus.CANCELLED` 与 `TaskState.CANCELLED`，映射为 UNKNOWN 而非 FAILED |

## 10. 未核实事项

0. 5.5 降级后，`REPORT_BLOCKED` 分支的 `Do not call tools` 指令是否仍有其他必需 gap 会落入同一死角，尚未逐一排查。需确认所有 `required=True` 且 `required_effects=()` 的 gap 来源，避免换一种 gap 再次形成闭环。
1. `_continue_agent_turn` final-response 分支的完整结构尚未通读，Protocol Gate 的精确插入点需在实施前确认。
2. 并行 MUTATE 的路径级串行化现状未核实。与本 SPEC 无关，但并行执行落地前需单独评估。
3. `.agent/runtime.db` 中 legacy outcome 事件的真实分布未统计，6.4 的清理不得先于统计执行。
