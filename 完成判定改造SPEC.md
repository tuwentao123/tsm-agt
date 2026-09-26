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

> 行号以修订时的代码为准（原稿行号已漂移，已在括号中给出原值）。

| 组件 | 位置（原稿） | 状态 |
|---|---|---|
| `ToolExecutionRecord` | `core/execution.py:25` | 有完整生命周期，**无** `effect` / `result_authority`；已新增 `reconciled_outcome` / `reconciliation_ref` / `reconciled_at` 对账字段 |
| `ToolCommitState` | `core/execution.py:14` | `PREPARED/RUNNING/COMMITTED/FAILED/CANCELLED/UNKNOWN_OUTCOME`（新增 `PREPARED`、`RUNNING`） |
| `ToolEffect` | `ports/tool.py:31`（现 `~172` 使用） | 7 值封闭枚举；`ToolSpec` 已声明 `effect` / `result_authority` |
| `ToolResultAuthority` | `ports/tool.py:49` | 9 值封闭枚举 |
| `ProcessResult.succeeded/failure_code` | `ports/process.py` | 已实施；注意 `failure_code` 对 `CANCELLED` 也返回 `PROCESS_CANCELLED`，判定须读 `status` |
| `CompletionReadinessProbe` | `ports/completion_readiness.py:128` | 10 字段，**无失败动作输入** |
| `CompletionReadinessAction` | `ports/completion_readiness.py:15` | 4 值，已够用 |
| `CompletionReadinessMode` | `ports/completion_readiness.py:14` | **新增（本 SPEC 未覆盖）**：`LEGACY_GATE`（默认）/ `OBSERVE_ONLY` / `AGENT_DECIDES`；只有 `LEGACY_GATE` 的 readiness 是强制的 |
| `_evaluate_completion_readiness` | `core/kernel.py:7139`（现 `~7360`） | **已持有 `task.tool_executions`** |
| `_completion_readiness_gaps` | `core/kernel.py`（现 `7189`） | gap 种类：`REQUIRED_OUTCOME_UNSATISFIED` / `TASK_SPEC_*` / `EVIDENCE_QUESTION` / `PLAN_STEP` / `POST_MUTATION_VERIFICATION` |
| `_latest_recoverable_tool_batch` | `core/kernel.py:9346`（现 `9710`） | **已批次范围、已通用、并行就绪** |
| `ToolBatchSnapshot` | `core/agent_loop.py:193` | 批次协议边界，为并行预留（`calls` / `pending_call_ids` / `is_open`） |
| `ToolSpec.is_concurrency_safe` | `ports/tool.py:165` | 已有 per-tool 并发声明 |
| 取消能力 | `CancelTaskInput` / `TaskState.CANCELLED` | 已完整 |
| 命令失败已有部分补偿 | `kernel.py:7401` `POST_MUTATION_VERIFICATION`、`7339` `TASK_SPEC_POST_MUTATION_COMMAND` | 仅覆盖**有 mutation** 的任务；不覆盖无 mutation 的副作用命令 |
| 结构化结论 | `ports/conclusion.py`、`core/conclusion_reference_validator.py` | **新增（本 SPEC 未覆盖）**：`FactReference.expected_effect` / `FactLifecycle`；与 `FactResolution` 需划清概念 |
| Outcome 残迹 | `kernel.py:7240`、`8338` `for outcome in ()` | 仅该字面循环不可达；**Outcome 机制本身已活跃**（见 §6.4） |

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

### 6.4 Outcome 残迹清理（方向已修正）

**原稿判断「Outcome 死代码，恒空不可达，停止新写」在当前代码上已不成立。**

修订时核实：Outcome 机制已经活跃——

- `kernel.py:2581 request_task_outcome_completion`、`2677 _task_outcome_completion_gaps` 是活跃执行路径；
- `ports/tool.py:88 OutcomeBindingMode`（`FULFILLMENT` / `SUPPORTING`）在用；
- `kernel.py:7285` 已有 `REQUIRED_OUTCOME_UNSATISFIED` gap；
- 实测事件分布：`task_outcome.binding_decided=1321`、`state_changed=1489`、`completion_requested=17`、`support_observed=1`。

因此本节调整为：

1. **不得停止 Outcome 写入**；它是当前完成判定的一部分。
2. 只清理字面空循环残迹 `for outcome in ()`（`kernel.py:7240`、`8338`）及其不可达分支。
3. 清理前仍需先完成一次事件分布统计（现已有数据），确认这两个循环所在路径与 `_task_outcome_completion_gaps` 不重叠、删除后无行为变化。

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

修订时全量套件实测基线 `1023 passed, 8 skipped`（另有 9 个既有失败：`test_cli_line_editing` PTY 5 个、`test_composition` 2 个、`test_dependencies` 1 个、`test_investigation_status` 1 个，均与本 SPEC 无关）。重点检查断言 `completion.readiness_evaluated` payload、快照结构、`successful_tool_calls`、`test_checkpoint_compatibility` 的既有测试。**新增默认字段不得改变该基线。**

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

---

# 11. 相对当前代码的差异与适配（修订时新增）

原稿写在更早的代码形态上。实施前必须按以下 5 条适配，否则会做错。

### 11.1 完成判定已分模式，新增 gap 必须 mode-aware

现在存在 `CompletionReadinessMode`（`LEGACY_GATE` 默认 / `OBSERVE_ONLY` / `AGENT_DECIDES`）。

- `completion.readiness_evaluated` 的 `enforced` 只有 `LEGACY_GATE` 为 true（`kernel.py:7503-7506`）；
- 非 `LEGACY_GATE` 下 `verify_task_acceptance` 走 `observation_mode`（`kernel.py:8304-8308`），gap 是**诊断不阻断**。

**适配**：`UNRESOLVED_EFFECT_FAILURE` 可以无条件产出（诊断价值独立），但"阻断"语义只在 `LEGACY_GATE` 成立。§7.2 的验收期望必须标注适用模式，其余模式只断言 gap 被产出。

### 11.2 Outcome 机制已活跃

见 §6.4。`REQUIRED_OUTCOME_UNSATISFIED` 是既有 gap；新增失败事实时要注意**不要与 outcome 未闭合重复计 gap**。

### 11.3 命令验证已有部分补偿，必须划边界

`POST_MUTATION_VERIFICATION`（`kernel.py:7401`）与 `TASK_SPEC_POST_MUTATION_COMMAND`（`kernel.py:7339`）已覆盖"有 mutation + 命令验证未通过"。

**适配**：新增 `UNRESOLVED_EFFECT_FAILURE` 前先判断该失败是否已被上述 gap 表达；若已被表达则**不重复产出**（或明确优先级：spec 级 > 通用 effect 级）。否则同一失败会被计两次，加速耗尽有界纠正配额。

### 11.4 判定须读 `ProcessResult.status`，不能只看 `failure_code`

`ports/process.py:100-110`：`failure_code` 对 `CANCELLED` 也返回 `PROCESS_CANCELLED`（非 `None`）。

**适配**：§5.1 决策表里"取消 → UNKNOWN（不作为失败阻断）"必须显式读 `status`，否则取消会被误判为 FAILED，违背 SPEC 原则。

### 11.5 结构化结论已存在，概念要划清

现在有 `ports/conclusion.py`（`FactReference.expected_effect`、`FactLifecycle`）与 `core/conclusion_reference_validator.py`。

**适配**：

- `FactResolution`（unresolved/retried/superseded）与 `FactLifecycle`（active/orphaned/expired/purged）不是同一维度，文档中必须写明，避免后续误合并；
- "未解决 effect 失败"将来可作为**引用校验的 INCOMPLETE 原因**（与证据完整性同构）。本批仍走 probe 字段，但实现时不要与 `ConclusionReferenceValidator` 的只读约束冲突（该校验器明确不做状态变更）。

---

# 12. 面向并行与 Plan-and-Execute 的扩展点与预留决策（修订时新增）

### 12.1 预留判据

只在下面三条**同时**成立时才预留：

1. 写进**持久层或对外协议**（不是纯内存结构）；
2. 是**当时才知道、事后无法重建**的观测值；
3. 加上去**零行为变化**（默认值 + `from_data` 容错 + 不进兼容哈希）。

纯代码分支、纯内存 dataclass、可由事件推导的内容，一律**不预留**——晚加成本几乎为零，提前预留只会变成兼容负担或锁死错误 schema。

### 12.2 决定预留（仅两项）

1. **`ToolExecutionRecord` 增加 `effect` / `result_authority`（即 6.1 的前两个字段）。**
   - 满足判据 1：进入 `TaskSnapshot.to_data()` → `runtime_tasks.data_json`；
   - 满足判据 2：只有执行发生时 `ToolSpec` 在手才知道；晚加则历史记录永久缺失，而工具 spec 会变，无法回填；
   - 满足判据 3：记录不参与 checkpoint 兼容哈希（`test_checkpoint_compatibility` 只看 `adapter_lock_hash` / `prompt_manifest_hash` / `policy_hash` 与 `tool_execution_count`）；项目已有 `legacy_execution_focus_revision` 同类先例；
   - `semantic_signature` 不预留（可由 `call` 派生）。

2. **`FinalAcceptanceProbe.required_plan_step_refs` 保留字段，只换数据源。**
   - 5.5 实施时**删除数据来源**（WorkingMemory plan），但**不删除字段**；
   - 留给 Plan-and-Execute 把契约步骤从这里接入。

### 12.3 决定不预留

| 项 | 原因 |
|---|---|
| `ExecutionFact` 的 `batch_id` / `step_ref` | 纯投影、非持久化，后加零成本 |
| gap 的 batch 级聚合 | 纯代码，届时在 `_completion_readiness_gaps` 局部改 |
| Protocol Gate 抽象（6.2） | 行为重构，提前抽接口等于猜接口 |
| 并行调度 / 路径级锁 | 是功能不是预留 |
| 新的 Plan / Contract 实体 | 真正做 Plan-and-Execute 时才能定形；原则 1 现在不建是对的 |

### 12.4 并行落地前必须解决（本批不做，仅登记）

1. **gap 粒度 vs 纠正配额**：gap 现为 per-execution，配额为 per-turn；并行一批 N 个失败会产生 N 个 gap，误判"无进展"。需 batch 级聚合。
2. **`resolution` 定序**：retried/superseded 隐含全序，并行需确定性规则（batch 序 + 批内 index）。
3. **UNKNOWN × effect 类别**：OBSERVE 的 UNKNOWN 可放过；MUTATE/EXECUTE 的 `UNKNOWN_OUTCOME` 必须接入既有 `reconciled_*` 对账，不能静默放过。
4. **事实缺批次归属**：`ToolExecutionRecord` 无 `batch_id`（可用 `ToolBatchSnapshot.calls` join，但批次快照关闭后可能不再保留）。
5. **路径级串行化**：并行 MUTATE 需按路径串行，否则丢失更新（原 §10 未核实事项 2）。

### 12.5 Plan-and-Execute 落地前必须解决（本批不做，仅登记）

1. **事实缺步骤维度**：`ExecutionFact` 需要 `step_ref` 才能表达"某步骤的动作失败未解决"。
2. **契约 seam 迁移**：`required_plan_step_refs` 的数据源应从 WorkingMemory 迁到 TaskSpec/Plan。
3. **原则 1 限定适用范围**：`不新增持久化实体`应表述为"**本批**不新增"；runtime 拥有的 plan（步骤/依赖/per-step 验收/重试）未来需要持久化实体。
