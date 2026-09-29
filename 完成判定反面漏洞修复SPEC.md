# 完成判定反面漏洞（inverse flaw）通用修复 SPEC

> 上游：`验收判定改造实施SPEC.md`、`上下文工程通用修复实施SPEC.md`。
> 本文件处理的是同一族缺陷的**反方向**：
> 之前修的是"把没做完的说成做完会被放行"（over-claim，防不住就死锁）；
> 这次是"runtime 自己停下时把'未完成'表达成 COMPLETE，再交给验证层判 FAILED"。

**状态**：设计完成，待实施。
**触发实例**：`task-9578272fb5bb49b986e363f036ae3c37`（FAILED）。

---

## 1. 证据链（全部来自持久事件）

| seq | 事件 | 关键内容 |
|---|---|---|
| 12 | `task_spec.revised` | 1 个 `WORKSPACE_DELIVERY` outcome，`required_effects: ["mutate"]`；5 条 UI 判据 + Runtime 生成的 `required-effect-delivery` |
| 63 | `tool.failed` | `core.apply_patch` `INVALID_PARAM`：`edits[3].old_text was not found` |
| 69 | `completion.readiness_evaluated` | `CONTINUE` / `required_capability_is_available`；gaps = `task-spec:required-effect-delivery`(mutate) + `execution-failure:…`(mutate) |
| 95 | `tool.failed` | 第二次尝试 `INVALID_PARAM`：`after_context must be 0..20` |
| 104 | `completion.readiness_evaluated` | **`REPORT_INCOMPLETE_RECOVERABLE`** / `required_work_is_recoverable_but_turn_capacity_is_exhausted` |
| 107 | `completion.incomplete_recoverable_requested` | 同一 reason；此后 `runtime_instruction` = "…no remaining capacity… **Do not call tools.** Report it as incomplete and resumable…"，且 `allow_tool_calls=False` |
| 112 | `llm.completed` | **模型完全遵命**：说"还没有完成，但可以继续恢复"，列出剩余工作，"还缺少一次成功的源码变更记录" |
| 111 | `completion.readiness_evaluated` | **`COMPLETE`** / `bounded_completion_corrections_exhausted`，**两个 required gap 仍开着** |
| 113 | `checkpoint.saved` | `model_calls: 6`，`model_budget_total_granted: 0`，`model_budget_renewal_count: 0` ⇒ **一次续量都没发** |
| 114→125→127 | `VERIFYING` → `verify.completed status=failed` → **`FAILED`** | `required-effect-delivery` = FAILED（`no durable record exists`），`mutation_journal: []` |

**矛盾点**：runtime 先命令模型"别调工具、报未完成"，随后把这次自我终止表达为 `COMPLETE`，并让验证层以"必需副作用缺失"判 `FAILED`。

### 1.1 调用时序（核验后，全部在同一个 turn `turn-0d32234e…` 内）

| model call | 允许工具？ | 模型做了什么 | 结果 |
|---|---|---|---|
| 1 | 是 | `core.read_file`（app.py 280-460 行） | ✓ |
| 2 | 是 | `core.apply_patch`（5 处 edits） | ✗ `old_text` 不匹配（分类 `terminal`） |
| 3 | 是 | 只输出文本（提出收尾） | readiness `CONTINUE` → 注入 correction |
| 4 | 是 | **`core.search_text`**（`query=compo…, after_context=25`）——**在重新定位真实文本以便重写 patch** | ✗ `after_context must be 0..20`（schema 从未声明该上限） |
| 5 | 是 | 只输出文本 | readiness `REPORT_INCOMPLETE_RECOVERABLE` → 注入 correction |
| 6 | **否**（`allow_tool_calls=False`） | 按指令"诚实汇报未完成" | readiness `COMPLETE` → 返回 `AgentTurnResult`（无 `turn.completed`） |

**重要更正**：模型并非一开始就被禁止调工具；它在第 2、4 次模型调用里**确实**在换方式重试（patch → search），只有第 6 次被强制为纯文本。所以"禁止调用工具"只发生了**一次**，但恰好发生在最后一步，使收尾必然带着未满足的契约。

---

## 2. 缺陷归类（四层，全部是通用缺陷）

### L1 动作词表不完备：逃生舱复用了成功动作

`CompletionReadinessAction` 只有四个值（`ports/completion_readiness.py:22-26`）：

```python
COMPLETE / CONTINUE / REPORT_INCOMPLETE_RECOVERABLE / REPORT_BLOCKED
```

`RuleBasedCompletionReadinessPolicy.evaluate` 的**兜底出口**（`policy.py:140-143`）：

```python
return self._decision(
    CompletionReadinessAction.COMPLETE,          # ← 与"无 required gap"同一个动作
    "bounded_completion_corrections_exhausted", state, probe,
)
```

于是 `COMPLETE` 同时表示两件语义相反的事：

1. `core_goal_has_no_known_gaps`（第 63 行）——**真的做完了**；
2. `bounded_completion_corrections_exhausted`（第 140 行）——**必需项还开着，运行时放弃纠正**。

消费者只能靠 `reason` 字符串区分，而 `reason` 是文本，动作才是契约。

### L2 补偿守卫被路径条件禁用

kernel 里**本来有**针对这个的守卫（`kernel.py:13477-13500`）：

```python
unmet_acceptance_criteria = legacy_completion_gate and any(
    gap.gap_id.startswith("task-spec:") and gap.required for gap in readiness.gaps
)
if (legacy_completion_gate and unmet_acceptance_criteria
        and readiness.action is CompletionReadinessAction.COMPLETE
        and not disclosure_only):          # ← 恰好在自己叫停模型的路径上被禁用
    readiness = CompletionReadinessDecision(REPORT_BLOCKED, "acceptance_criteria_unmet", ...)
```

实例中 seq 104 已经设置了 `last_action = REPORT_INCOMPLETE_RECOVERABLE`，于是 `disclosure_only = True`（`kernel.py:12732-12737`）——**守卫失效**。这个条件的存在说明作者已经意识到风险，却把它挂在了最不该生效的那条路径上。

### L3 大结构缺陷：`COMPLETE` 是唯一"结束这一轮"的动作，且它不要求 gap 为空

核验后的真实控制流（一轮 = `Kernel._continue_agent_turn`）：

```
model call
 ├─ 有 tool calls → 执行 → 结果入 messages → 下一个 model call
 └─ 无 tool calls（模型提出收尾）
      readiness = policy.evaluate(gaps, state)
      [renewal 可能把 EXHAUSTED 改判 CONTINUE]                        kernel.py:13414-13465
      [守卫: COMPLETE ∧ 未满足 task-spec gap → REPORT_BLOCKED]        kernel.py:13477-13500
      final_response = COMPLETE ∧ ¬unmet                              kernel.py:13501-13505
      boundaries（含 incomplete_recovery_boundary）                    kernel.py:13506-13518
      _commit_model_response_checkpoint(final = final_response ∧ ¬boundary)
      if incomplete_recovery_boundary: 挂起并 return                   kernel.py:13539-13565
      elif action is not COMPLETE:     注入 correction → 继续循环       kernel.py:13566-13593
      else:                            return AgentTurnResult          kernel.py:13594-13612
```

**关键**：`13594` 的 `return AgentTurnResult` **既不检查 `final_response`，也不检查 `unmet_acceptance_criteria`**。
`final_response` 只决定 checkpoint 是否记 final（即是否写 `turn.completed`）。因此：

> **只要 readiness 给出 `COMPLETE`，这一轮就以"正常结果"结束**——哪怕 required gap 全开着、哪怕 `final_response` 为 False、哪怕根本没有 `turn.completed` 事件。

**持久证据**：本任务 `turn.completed` 计数 = **0**，但 `checkpoint.saved(model-response-recorded, model_calls=6)` 之后 SDK 直接 `EXECUTING -> VERIFYING`。这只能由"走 `COMPLETE` 分支返回、`final=False`"产生。

因此唯一能阻止它的就是 `incomplete_recovery_boundary` 挂起，而它依赖的守卫恰好被 `disclosure_only` 关闭（L2）。**缺陷是结构性的：`COMPLETE` 这个动作本身不携带"gap 必须为空"的约束，而它是唯一的收尾动作。**

### L4（反面，有意设计，不能放宽）

`_required_effect_delivery_result`（`kernel.py:714-734`）把"必需副作用没有持久记录"判为 **FAILED**：

```python
AcceptanceStatus.PASSED if not missing else AcceptanceStatus.FAILED
```

`tests/test_completion_claim_gate.py` 明确锁定了它："A contract that promises work cannot be closed by prose"。**这是正确的反谎报闸门，必须保留。** 缺陷不在它，而在于 runtime 自我终止的场景**没有在它之前被分流**。

> 结论：正确的区分维度是 **"谁结束了这一轮"**，不是"工作是否缺失"。
> 模型自己收尾 + 必需工作缺失 ⇒ FAILED（反谎报，保持）。
> runtime 主动停下 + 必需工作缺失 ⇒ NEEDS_REVIEW（不是产品失败）。

---

## 3. 通用不变量

| 编号 | 不变量 |
|---|---|
| **INV-13** | **完成由 gap 集合决定（completion is gap-determined）**：`action == COMPLETE ⇒ 无 required gap`。计数器（continue / disclosure / stalled / renewal）只能在"继续 / 可恢复挂起 / 交人复核"之间选择，**永远不能产生"成功"**。 |
| **INV-14** | **终止权决定终态（stop authority decides the terminal state）**：模型自行收尾且必需工作缺失 ⇒ `FAILED`；runtime 主动停下且必需工作缺失 ⇒ `NEEDS_REVIEW`（三值语义下的"未决"，不是产品失败）。 |
| **INV-15** | **动作→结果全覆盖（total action→outcome mapping）**：每个 readiness action 必须恰好映射到一个出口 —— final answer / continue / resumable suspend / blocked suspend / human review。不存在"落到循环尾部"的动作。 |
| **INV-16** | **验收层不得是"未完成"的第一个发现者（acceptance is never the first detector of open work）**：只要还有 required 工作未完成，就必须由**就绪层/目标层**先记录并给出可恢复状态；验收层（PASSED/FAILED 词表）只应看到"自称已完成"的任务。若验收层成为第一个发现者，说明状态分层失效。 |

**两轴模型（Codex 与 Hermes 都独立收敛到这个形状）**：把"这一轮执行得怎么样"与"目标有没有达成"分成两条正交的轴，
**只有目标轴承载"未完成但可续"**。

| 轴 | Codex | Hermes | 我们（目标） |
|---|---|---|---|
| 执行/回合轴 | `TurnStatus`（工作未完成也可以是 `Completed`） | `completed/failed/interrupted` booleans | `AgentTurnResult` / suspension |
| **目标轴（承载"未完成但可续"）** | `ThreadGoalStatus::BudgetLimited`（terminal-but-resumable，自动续跑只认 `Active`） | goal `paused`（预算耗尽的未完成 goal **永不 `done`**） | `TaskState.AWAITING_USER`（可续）→ stall cap → `NEEDS_REVIEW` |
| "完成"的判据 | prompt 级审计 + 工具参数校验（**无断言**） | `GoalContract` + 确定性质量门（**无断言**） | **required gap 集合（有断言，比两者都强）** |

我们的缺陷正是：**执行轴有了结论（`COMPLETE`），目标轴却什么都没记**（`TaskState` 仍是 `EXECUTING`，
`readiness.gaps` 没被转成任何目标级状态），于是"未完成"只能由**验收轴**发现——违反了 INV-16。

INV-13 是 L1+L2 的通用形式，INV-14 是 L4 的边界，INV-15 是 L3 的通用形式。

**外部同构表述（Hermes #122173 的根因用词）**：
> *"the presence of a final-phase message as completion evidence … where that evidence is insufficient"*

即 **完成必须由正向证据"挣得"（earned by positive evidence），不能由标记"声明"（declared by a marker）**。
映射到我们：`readiness.action == COMPLETE` 是一个**标记**；"required gap 为空 + 必需副作用有持久记录"才是**证据**。
INV-13 就是把"证据"补回这个动作的定义里。

**配套不变式（Hermes 在 HTTP 层强制的"状态对不矛盾"）**：
> *"…so `completed: true` never rides next to `partial: true`"*（`api_server_runs.py:181-200`）

对应到我们：**`COMPLETE` 永远不得与任何 required gap 并存**。这条可以做成一个纯断言测试（见 §6）。

---

## 4. 外部参考（Hermes 已核验；Codex 进行中）

### 4.1 Hermes Agent（`NousResearch/hermes-agent` @ `a6686cc3`，2026-09-27）

**同一缺陷已被独立发现并归档。** issue **#122173**（open，P2）：
*"Empty final phase authorizes commentary fallback, and explicit output exhaustion can normalize to stop."*
根因被明确写成：**"final-phase PRESENCE treated as completion evidence"** ——
一个 `phase=final_answer` 标记的**存在**被当成"完成"的证据，即使它是空的；且该标记短路了
`status=incomplete`（`incomplete_details.reason=max_output_tokens|length`）分支，把"预算耗尽"规范化成 `stop`。
（`agent/codex_responses_adapter.py:1124-1131`、`:1219-1225`；边界处 `agent/turn_response_check.py:47-64` 本来返回 `incomplete`，两个模块互相矛盾。）
修复 PR **#122176** 的做法：**要求实质性的 final evidence，并把 output exhaustion 纳入 incomplete 条件**。

> 与我们 L1/L3 是同一个缺陷：**`COMPLETE` 这个标记的存在被当成完成证据**，而它不携带"gap 必须为空"的约束。
> 因此 INV-13 可以更准确地表述为：**完成必须由正向证据"挣得"（earned by evidence），不能由标记"声明"（declared by a marker）。**

**Hermes 的"第三种出口"——正是我们想要的形状。**

| 维度 | Hermes 做法 | 证据 |
|---|---|---|
| 迭代预算耗尽的 turn | `completed=False, failed=False, interrupted=False`，`turn_exit_reason="max_iterations_reached(N/N)"` | `agent/turn_finalizer.py:124-193`（`:138`） |
| 该状态的语义 | *"`completed` is False because the work did not finish in that turn, but the turn itself is **a resumable boundary — not a failure**"*；`exit_reason_failure()` 对它返回 `None` | `agent/turn_failure_copy.py:146-160`、`:93-111` |
| goal 层不变式 | 预算耗尽的**未完成** goal 变 `paused`，**永不 `done`**；`/goal resume` 复位 `turns_used=0` 续跑 | `hermes_cli/goals.py:1443-1448`、`:1527-1528`、`:1171-1179` |
| 状态对矛盾的不变式 | HTTP 层把该情形映射为 `failed`，*"so **completed: true never rides next to partial: true**"* | `gateway/platforms/api_server_runs.py:181-200` |
| 其它预算种类 | kanban worker → `outcome="timed_out"`；goal loop → `blocked_budget`；context 压缩耗尽 → `failed=True, compression_exhausted=True` | `agent/turn_finalizer.py:47-84`、`hermes_cli/goals.py:1691-1699`、`agent/conversation_loop.py:1097-1122` |
| 续跑持久化 | `GoalState`（`turns_used`/`paused_reason`/`last_verdict`）存 `SessionDB.state_meta` 键 `goal:<session_id>` | `hermes_cli/goals.py:613-654` |

**反向教训（不要照抄的地方）**：delegate 子运行在预算耗尽时**仍保留 `status="completed"`**，只额外加一个
`"truncated": exit_reason == "max_iterations"` 标志（`tools/delegate_tool_child_run.py:585-601`）。
Hermes 自己在这里也不一致。**结论：耗尽必须是"独立出口"，不能是成功之上的一个 flag**——这正是我们
F1（新增 `EXHAUSTED`）而非"给 COMPLETE 加一个 flag"的理由。

**它把这类问题当作一等缺陷类**（与我们的定位一致）：
#90049 *"make false success a first-class defect class with typed completion proofs"*、
#98940 *"honor run budgets and prevent false terminal success"*、
**#111770 / #112272（已合并）"interrupted and unfinished turns are no longer reported as completed"**。

### 4.2 Codex（`openai/codex` @ `41f9084b`，已核验）

**最重要的一条：Codex 明确接受"turn 报 Completed 而工作未完成"——因为它把"工作是否完成"放在另一个持久状态里。**

> *"「work remains」（open `update_plan` items、budget-limited goal、模型只是停下）is NOT an error and NOT an abort, so it **DOES map to `TurnStatus::Completed` / exit 0**. … the "work remains" signal lives in the **separate persisted `ThreadGoalStatus::{BudgetLimited,Blocked,UsageLimited}` layer**."*

即 Codex 用**两条正交的轴**：

| 轴 | Codex | 承载"未完成"的能力 |
|---|---|---|
| turn 执行状态 | `TurnStatus = Completed｜Interrupted｜Failed｜InProgress`（`app-server-protocol/.../v2/turn.rs:33`） | **不承载**（工作未完成也可以是 Completed） |
| 目标状态（持久） | `ThreadGoalStatus = Active｜Paused｜Blocked｜UsageLimited｜BudgetLimited｜Complete`（`state/src/model/thread_goal.rs:14`；`is_terminal() = BudgetLimited｜Complete`，`:39`） | **承载** —— `budget_limited` 是 terminal-but-**resumable**，自动续跑只认 `Active`（`ext/goal/src/runtime.rs:465-468`） |

**它的预算耗尽处理与我们完全同形**：goal 超预算 → 标记 `budget_limited` + 注入 steering item
（`ext/goal/src/extension.rs:523`，文案 `templates/goals/budget_limit.md`）：
> *"The system has marked the goal as `budget_limited`, so **do not start new substantive work** for this goal. Wrap up this turn soon: summarize useful progress, identify remaining work or blockers, and leave the user with a clear next step."*

→ **turn 不被 abort，正常结束并报 `Completed`**，而 **goal 停止自动续跑**。

**这就是我们实例的镜像，也解释了我们的错在哪**：

> 我们的 disclosure 轮（`allow_tool_calls=False` + "Do not call tools. Report it as incomplete…"）与 Codex 的
> `budget_limit.md` steering **是同一种做法——Codex 也禁止开始新的实质工作**。区别在于
> **Codex 同时改了目标级状态（`budget_limited`）**，所以 turn 报 `Completed` 无害；
> **我们只改了指令、没有改任何目标级状态**，于是"未完成"只能留给**事后验证层**去发现，而验证层的词表只有 PASS/FAIL。

**Codex 的其它相关事实**：

- **没有**任何不变式阻止 `Completed` 与未完成的 plan 项并存：plan 工具纯 advisory（`core/src/tools/handlers/plan.rs:87-98`，tool 回复就是 `"Plan updated"`），`core/src/session/turn.rs:564` 的 `needs_follow_up` 只看"模型有没有继续发 tool call"。唯一的硬门是 **Stop hooks**（`session/turn.rs:666-693`，可阻止 finalize 并强制继续）和 goal 模式的**提示词级**完成审计。→ 在"完成必须由证据挣得"这一点上，**我们的 gap 集合断言比 Codex 更强**。
- 硬性 session token 预算 → `SessionBudgetExceeded` → 终局 turn error → `TurnStatus::Failed`（`core/src/agent/control/budget.rs:12-17`）。这是"预算超限=错误"，与"work remains"不同。
- `TurnAbortReason::BudgetLimited` 在 wire 上存在但 **core 从不构造**（仅 TUI 本地合成）；`TurnAbortReason` 只有 `Interrupted`/`Replaced` 真的被构造。
- 续跑：`ThreadResumeParams`（by thread_id / history / path），daemon 恢复时补一个 `TurnAborted{Interrupted}` 闭合旧 turn，再起新 turn 并附指令
  *"Continue the unfinished work from the saved conversation. Check the current state before repeating actions that may already have completed."*（`daemon_continuation.rs:88-91`）。
- 相关 issue：#48677 *"ends unfinished long-running work after a small partial checkpoint"*、
  #36596 *#"repeatedly terminates active autonomous work despite explicit instructions to continue"*、
  **#48596 *"Intent-Preserving Task Continuity — explicit Stop semantics for long-running agents"*（主张把 user-intent state 与 execution state 分开，正好对应我们下面的两轴结论）**、
  #34215（`budget_limited` 后无用户可达的 resume 路径，是该模型的已知缺口）。

**由此对设计的修正（重要）**：

1. **根因不是"禁止工具"这个动作**——Codex 同样禁止，且这是有界预算的正常做法。我们的 `disclosure` 轮不是错误设计。
2. **根因是"runtime 自我终止"没有落进任何目标级状态**，于是唯一发现它的地方是**验收层**。这比"动作词表缺一个值"更准确地描述了 L1/L3。
3. 因此新增 **INV-16**（见 §3），并把 **F5 降级为可选调优**（见 §5）——因为 Codex 并不允许在预算耗尽后继续实质工作，所以"允许工具"不是正确性修复。

---

## 5. 修复设计（F1–F3 + F6 必做；F4 可选）

### 5.0.1 实施前自审（发现两处逻辑错误 + 一处通用性增益）

1. **F3.3 被 F3.1 吸收，应删除。** F3.1 的归一化条件是"存在**任意** required gap"，而原守卫只覆盖
   `task-spec:` 前缀的 gap。F3.1 严格更强，因此在 F3.1 存在时原守卫永远不可达。
   → **F3 从 4 项减为 3 项**；"删掉 `and not disclosure_only`"不再需要，改为**删除整个已被吸收的守卫**。
2. **F3.1 必须限定在 legacy gate 模式。** 非 legacy（per-turn）模式在
   `kernel.py:13468-13476` 会把每个"无 tool call"的响应**强制改写成 `COMPLETE` / `diagnostic_only`**，
   并有意把决定权留给调用方（"only an explicit caller decision may advance or fail the Task"）。
   若无条件归一化，per-turn 语义会被破坏。
   → 归一化加 `legacy_completion_gate` 条件，放在该 override **之后**。
3. **F3.1 的通用性高于 F2（增益）。** 除了内置 policy，`kernel.py:8239-8245` 在
   **未配置 policy** 时也会直接产出 `COMPLETE / policy_not_configured`（注释明说"Keep COMPLETE for
   compositions that intentionally omit it"）——这是**第二个 COMPLETE-with-gaps 产地**，
   policy 内的修复（F2）**无法触及**。F3.1 放在消费者侧，同时覆盖它与自定义 policy。
   → 这进一步证明"根因修复必须在消费者侧"，F2 只是让内置 policy 不再主动违规。

---

### 5.0 每项修复各解决什么问题（不重叠）

| 修复 | 解决的缺陷 | 具体机制 | 单独是否足够 | 不解决什么 |
|---|---|---|---|---|
| **F1** 新增 `EXHAUSTED` | **L1 词表不完备** | 给"runtime 自我终止但需求未开"一个**独立名字**（有界、非成功、非失败） | ❌ 单独无效：没人产出也没人消费它，只是词汇前提 | 不改变任何行为；不解决 L2/L3 |
| **F2** policy 不再兜底 `COMPLETE`（配合 F6 删计数器，6 分支→4） | **L1 的来源** | 消除 `policy.py:140-143` 这个"成功动作 + 未满足 gap"的产地 | ✅ 对**内置** policy 足够；❌ 对自定义/遗留 policy 无效 | 不解决消费者侧的结构空洞（L3）；不恢复被禁用的守卫（L2） |
| **F3.1** 返回点归一化：`COMPLETE ∧ required gap ⇒ EXHAUSTED` | **L3（根因）** | 把 INV-13 钉在**唯一的收尾动作**上，与策略实现无关 | ⚠️ 只阻止"带 gap 的 COMPLETE 收尾"；若没有 F3.2 会变成"无出口"，仍会掉出去 | 不给终止状态落点；不解决 L2 |
| **F3.2** `EXHAUSTED` 接回 `_suspend_incomplete_recoverable` 漏斗 | **L3 的出口 + INV-14/16** | 让"自我终止"落进**目标轴**的可恢复状态（→ stall cap → `NEEDS_REVIEW`），验收层不再是第一个发现者 | ❌ 依赖 F3.1 先产出 `EXHAUSTED` | 不改验收层词表（L4 保持） |
| **F3.3** 删守卫的 `and not disclosure_only` | **L2（近因）** | 恢复"未满足验收判据 ⇒ 不得收尾"的补偿检查，在它本来最该生效的路径上 | ✅ **单独就能修掉本次实例**（会转成可恢复挂起而非 FAILED） | 不解决策略侧产地（L1）；不解决自定义策略（L3） |
| **F3.4** 续量判定去 `reason` 字符串（改判 `action is EXHAUSTED`） | **L1 在消费者侧的残留** | 消费者不再靠解析 `reason` 文本区分"成功"与"放弃" | ✅ | 行为等价，属正确性/可维护性 |
| **F6** 删 in-turn 纠正计数器，改"能力上限" | **1 次悬崖（近因）** | 可关闭的 gap + 资源尚在 ⇒ 不设次数上限，模型可持续重试 | ✅ 独立 | 不改变"gap 未空不得收尾"（反谎报保持） |

**近因 vs 类因**（实施顺序依据）：

- 本次实例的**近因是 L2**（`disclosure_only` 关掉了唯一补偿守卫）→ **F3.3 单独即可避免这次 FAILED**。
- 但"任何路径下 `COMPLETE` 与 required gap 并存"是**类因（L3）**→ 需要 **F3.1 + F3.2 配对**：
  F3.1 阻止错误收尾，F3.2 给出正确落点；缺任何一个都会从"误判失败"变成"无出口"。
- **L1** 是产地问题（策略用成功动作表达放弃）→ **F1 + F2 + F3.4** 一起解决，
  且它同时消除了"消费者靠 reason 文本判断"这一脆弱点。
- 三者是**两端加固**（producer: F1/F2；consumer: F3.1–F3.4），与 INV-9 的做法一致
  （结构字段 + 机械护栏），不是重复劳动。

### F1 补全动作词表（`ports/completion_readiness.py`）

```python
class CompletionReadinessAction(StrEnum):
    COMPLETE = "COMPLETE"                       # 仅当 required gaps == ∅
    CONTINUE = "CONTINUE"
    REPORT_INCOMPLETE_RECOVERABLE = "REPORT_INCOMPLETE_RECOVERABLE"
    REPORT_BLOCKED = "REPORT_BLOCKED"
    EXHAUSTED = "EXHAUSTED"                     # 新增：runtime 自我终止但需求未满足
```

`EXHAUSTED` 的语义：**有界、非成功、非失败**——"我（runtime）用完了纠正预算，必需项还开着"。

### F2 Policy 只在无 required gap 时 COMPLETE（`adapters/rule_based_completion_readiness/policy.py`）

- 兜底出口（第 140-143 行）改为 `EXHAUSTED` / `bounded_completion_corrections_exhausted`。
- 其余分支不变（它们本来就携带 `required`）。
- 加一条策略级断言式不变式：任何返回 `COMPLETE` 的分支必须 `not required`；否则抛错（fail-closed，暴露自定义策略的违约）。

### F3 消费者总映射：把"runtime 自我终止"接回既有的有界漏斗（`core/kernel.py`）

关键发现：**正确语义的漏斗已经存在** —— `_suspend_incomplete_recoverable`（`kernel.py:14010-14039`）：

```python
stalled = …  # 同一组 required gap 连续几轮无进展
if stalled >= self._dependencies.completion_max_stalled_continuations:
    return await self._finalize_needs_review(          # INV-4 有界逃生 → NEEDS_REVIEW
        checkpoint, gaps, reason="required_work_unverifiable_or_stalled", …)
… 否则挂成可恢复续跑（resumable）
```

也就是说 **"有界 + 可恢复 + 交人复核"三件事它一次做全**。缺陷不是缺机制，而是
`COMPLETE + 未满足 required gap` 这条路径**没有接到它上面**。因此修复是**补一条边**，不是新增终态逻辑：

1. **守卫去掉路径条件**：`unmet_acceptance_criteria → REPORT_BLOCKED` 的 `and not disclosure_only` 删除（`kernel.py:13483-13488`）。这一条单独就能修掉实例（`disclosure_only=True` 时不再漏）。
2. **`acceptance_incomplete_boundary` 覆盖 `EXHAUSTED`**（`kernel.py:13506-13510`）：

```python
acceptance_incomplete_boundary = bool(
    not tool_calls
    and required_gaps_remain
    and readiness.action in {
        CompletionReadinessAction.REPORT_BLOCKED,
        CompletionReadinessAction.EXHAUSTED,
    }
)
```

   于是 `EXHAUSTED` → `incomplete_recovery_boundary` → `_suspend_incomplete_recoverable` → 有界可恢复挂起 → 到 stall cap 由 INV-4 转 `NEEDS_REVIEW`（INV-14 成立，且不引入新终态）。
3. **续量判定去文本化**：`readiness.reason == "bounded_completion_corrections_exhausted"`（`kernel.py:13383-13387`）改为 `readiness.action is CompletionReadinessAction.EXHAUSTED`。
4. **尾部断言（对应 L3 的结构性约束）**：`COMPLETE` 分支返回前，若 `readiness.gaps` 仍有 required 项，一律先按 `EXHAUSTED` 归一化（第 13594 行的返回点不得存在"gap 未空即为成功"的路径）；若归一化后仍无出口可走，发 `completion.invariant_violated` 并改走 `_suspend_incomplete_recoverable`，绝不静默返回 `AgentTurnResult`。
5. **策略违约也要 fail-closed**：`evaluate` 若返回 `COMPLETE` 且携带 required gap（自定义策略可能违约），在做 1–4 之前先按 `EXHAUSTED` 处理并记录违约，保证 INV-13 不依赖具体策略实现。

### F4（可选，较大）判据与 gap 同源

目前同一事实有两个表示：readiness gap `REQUIRED_DELIVERY_UNSATISFIED`（本来导向 BLOCK）与验证判据 `required-effect-delivery`（FAILED）。可选做法是让验证层的 `required_effect` 结果**复用同一投影**，只在"有尝试被证伪"时 FAILED、"完全没发生"且"runtime 终止"时 BLOCKED。**风险**：会削弱 L4 的反谎报闸门，故本次默认不做，仅记录。

### F5（**已降级为可选调优**）disclosure 轮是否允许调用工具

原提案是"disclosure 轮不再 `allow_tool_calls=False`"。**Codex 核验后此提案降级**：Codex 在 goal 预算耗尽时同样要求
*"do not start new substantive work … Wrap up this turn soon"*（`ext/goal/src/extension.rs:523` +
`templates/goals/budget_limit.md`），即**它也不允许继续实质工作**。所以"禁止工具"本身不是缺陷，
有界预算下它是正常做法。真正的缺陷是没有配套的目标级状态（见 INV-16）。
→ 本项只作为**产品取舍**保留：若希望"失败即重试"成为默认（DeepSeek 式体验），可放宽；
但它**不是正确性修复**，不应与 F1–F3 混在一起做。

### F6（校准，建议做）把"次数上限"换成"能力上限"

`RuleBasedCompletionReadinessPolicy` 默认 `max_continue_attempts=1`、`disclosure_attempts` 一次性（`policy.py:32`）。
**核验外部实现后，这两个 in-turn 计数器应当删掉**：

- **Codex 完全没有 turn/iteration 次数上限**：core 里不存在 `max_iterations`；
  循环在模型持续发 tool call 时无界（`core/src/session/turn.rs:424` `loop`，`:741` continue）；
  完成只由 `needs_follow_up = model_needs_follow_up || has_pending_input`（`:564,647`）决定——
  **模型停下，turn 就结束，harness 不覆盖。**
- **Hermes 只有资源计数**：`IterationBudget`（`max_total` 次 API 调用，`agent/iteration_budget.py:25-56`）+
  goal `max_turns`（跨轮资源预算）；**没有"纠正尝试次数"这种计数器**。
- 两者的"收尾/汇报"模式都由**资源耗尽**触发（Hermes `turn_finalizer.py:124-193`；
  Codex `budget_limit.md`），不是由"我们告诫了几次"触发。

**我们的计数器是"readiness gate 能覆盖模型停止决定"的产物**（Codex 不覆盖，所以不需要），
而不是资源管理的产物——而资源管理我们本来就有：`max_model_calls`(40)、`max_tool_calls`(120)、
renewal 上限；`can_continue` 里也**已经**要求 `remaining_model_calls > 0 and remaining_tool_calls > 0`。
因此 in-turn 次数上限是**冗余**的，而且**有害**：它制造了一个 1 次的悬崖——实例中还剩 34 次 model call、
117 次 tool call，却因"已纠正过一次"进入不可逆的 disclosure 轮。

**正确的上限是"能力上限"而不是"次数上限"**，而 `recoverable` 已经算出来了
（`gap.effective_required_effects ⊆ available_effects`）：

```
required gap == ∅                                  → COMPLETE
required gap 可关闭（能力可用）且资源尚在            → CONTINUE     ← 不设次数上限（模型可以一直迭代）
required gap 不可关闭（能力不可用）且资源尚在        → REPORT_BLOCKED（一次披露）→ EXHAUSTED
required gap 仍在 且资源耗尽                        → EXHAUSTED
```

判据变成：**"这件事还能不能被关闭"**，而不是"我们问过几次"。实例中的 gap 是 **recoverable**
（`mutate` 工具可用、`remaining_model_calls=35`），所以正确行为是**继续**——模型会一直重试，正是
"失败即可重试"的期望行为；只有真正不可关闭的要求才会被有界地披露并转入交人复核。

**反死锁的归属也随之清晰**：跨轮的 `stalled_continuations`（同一组 gap 连续几轮无进展 →
`_finalize_needs_review`，INV-4）负责"不要永远重试同一个无法满足的要求"；
in-turn 计数器把这一职责重复实现了一遍，还实现错了（它数的是"告诫次数"，不是"有无进展"）。

**影响面（已核）**：生产仅 `adapters/rule_based_completion_readiness/policy.py`、
`kernel.py:13425`（renewal 时重置计数器）、`ports/completion_readiness.py` 的状态 schema；
测试仅 `tests/test_completion_readiness.py`、`tests/test_agent_checkpoint_resume.py`
（外加本 SPEC 新增的红测试）。需要更新那些断言"CONTINUE→disclosure→COMPLETE 三段式"的用例。

**代价与取舍**：删除后模型可在 `max_model_calls` 内持续重试，成本上升但**有界**。
若担心成本，正确做法是调 `max_model_calls`，而不是保留一个隐藏的 1 次悬崖。
**我们的核心优势不变**：只要 required gap 未空，就不接受最终答案（反谎报），
只是不再人为地把"还能关闭的要求"提前判死。

---

## 6. 测试与验收

| 用例 | 断言 |
|---|---|
| `test_exhausted_requires_no_required_gap`（策略单测） | 构造 `disclosure_attempts=1` + required gap ⇒ 返回 `EXHAUSTED`，**不是** `COMPLETE` |
| `test_policy_complete_only_without_required_gap`（属性测试） | 遍历状态矩阵，任何 `COMPLETE` 必 `gaps 无 required` |
| `test_forced_complete_with_gap_never_final`（kernel） | `COMPLETE + required gap` 不得成为 final answer |
| `test_exhausted_finalizes_needs_review_not_failed`（kernel 端到端） | 纠正预算耗尽 + required gap ⇒ 走 `_suspend_incomplete_recoverable`；连续无进展到 stall cap ⇒ `NEEDS_REVIEW`，**不出现** `FAILED`；`mutation_journal` 为空 |
| `test_exhausted_still_eligible_for_renewal`（kernel） | `EXHAUSTED` + 可恢复必需工作 + 预算将尽 ⇒ `budget.renewed` 且不终结 |
| `test_disclosure_path_still_guards_unmet_acceptance`（kernel） | `disclosure_only=True` 时守卫**仍然**生效（L2 回归）——即本次实例的直接回归测试 |
| `test_complete_with_required_gap_never_escapes`（kernel） | 自定义策略返回 `COMPLETE` + required gap ⇒ 被改判并记 `completion.invariant_violated`（INV-15） |
| `test_model_self_conclusion_with_missing_effect_still_fails`（保持 L4） | 模型自行收尾 + 缺必需副作用 ⇒ 仍 `FAILED` |
| `test_no_contradictory_status_pair`（不变式断言） | 任意 `completion.readiness_evaluated` 事件中，`action=COMPLETE` ⇒ 该事件的 `gaps` 不含 required 项；等价于 Hermes 的 "`completed: true` never rides next to `partial: true`" |
| `test_exhaustion_is_a_distinct_outcome_not_a_flag` | `EXHAUSTED` 是独立动作；不得实现为"`COMPLETE` + 一个 flag"（Hermes 自己在 delegate 子运行处犯了这个错，`delegate_tool_child_run.py:585-601`） |
| `test_exhausted_event_names_the_budget_kind` | 事件记录是哪一种预算耗尽（continue/disclosure/stalled/model-call-renewal），对应 Hermes 按预算种类给不同可恢复语义 |

**验收**：全量测试失败集合与改动前一致（`10 failed / 1170 passed`：`test_cli_line_editing` ×5、`test_composition` ×2、`test_investigation_status` ×1、`test_local_workspace_sandbox` ×2 为既有环境失败）。

---

## 附录 A：`禁止工具` 的完整出现链（逐行 + 持久证据）

**结论：它不是任何一个组件的独立决定，而是 `disclosure_only` 这个派生布尔的副作用；`disclosure_only` 又来自上一轮 readiness 的 `last_action`。**

```
① 模型第 5 次调用没有发起 tool call（提出收尾）
      ↓
② readiness 求值（seq 104）
   policy.py:96-102  can_continue 要求 continue_attempts < max_continue_attempts(默认 1)
                     —— 但 seq 69 的 CONTINUE 已把 continue_attempts 置为 1 ⇒ False
   policy.py:114-128 recoverable ∧ remaining_model_calls>0 ∧ disclosure_attempts==0 ⇒ True
                     ⇒ action = REPORT_INCOMPLETE_RECOVERABLE
                     ⇒ next_state.last_action = "REPORT_INCOMPLETE_RECOVERABLE"
                       next_state.disclosure_attempts = 1
   ★ 这一步的语义是「进入一次性 disclosure 轮」：runtime 决定不再给工作机会，只要一份诚实汇报
      ↓
③ 消费者：action ≠ COMPLETE ⇒ 注入 correction，并把 state 存进 checkpoint     kernel.py:13566-13590
   持久证据 seq 108：checkpoint.saved(reason=completion-readiness-correction, model_calls=5)
      ↓
④ 下一轮循环开头（模型第 6 次调用之前）读回 state
   kernel.py:12732-12737  disclosure_only = last_action ∈ {REPORT_BLOCKED, REPORT_INCOMPLETE_RECOVERABLE}
                                        = True                    ← 持久证据 seq 104 的 state.last_action
      ↓
⑤ 一个 flag 产生两个效果：
   (a) kernel.py:13035   allow_tool_calls = not (wrap_up or disclosure_only) = False
       → adapters/openai_compatible/model.py:982   payload["tool_choice"] = "none"
         （注意：35 个 tools 仍被 advertise，prompt_tool_count=35 全程不变；只是禁止调用）
   (b) kernel.py:12782-12799  runtime_instruction = "This Turn has no remaining capacity …
                              **Do not call tools.** Report it as incomplete and resumable …"
                              （被计入 prompt 的 runtime_instruction 桶）
      ↓
⑥ 模型第 6 次调用只能产出文本（seq 112），readiness 再求值（seq 111）
   can_continue False（continue_attempts=1；且 forced_wrap_up=True）
   disclosure 分支 False（disclosure_attempts=1）
   REPORT_BLOCKED 分支 False（同一条件）
   ⇒ 落到兜底 policy.py:140-143  action = COMPLETE / reason = bounded_completion_corrections_exhausted
      ↓
⑦ 消费者：守卫 COMPLETE→REPORT_BLOCKED 因 `and not disclosure_only` 被跳过   kernel.py:13483-13488
   final_response = False（unmet_acceptance_criteria=True）
   incomplete_recovery_boundary = False
   ⇒ 但 `action is COMPLETE` ⇒ kernel.py:13594 return AgentTurnResult（无 turn.completed 事件）
      ↓
⑧ SDK legacy gate：EXECUTING → VERIFYING → required-effect-delivery FAILED → FAILED
```

**两个被这条链暴露的附加缺陷**：

1. **`disclosure_attempts` 是一次性预算（默认 1），用完即无路可走**——所以"禁止工具"实际上是**不可逆点**：
   一旦进入 disclosure 轮，runtime 已经放弃了继续工作，剩下的唯一出口就是坏掉的 COMPLETE 兜底。
2. **reason 文案与实际不符**：seq 104 的 reason 是
   `required_work_is_recoverable_but_turn_capacity_is_exhausted`，
   但同一事件记录的 `remaining_model_calls = 35`、`remaining_tool_calls = 117`。
   真正耗尽的是**纠正计数器**（`continue_attempts=1`、`disclosure_attempts=1`），**不是预算**。
   这也解释了为什么续量从未发放（`model_budget_total_granted: 0`）：`DiminishingModelCallBudgetPolicy`
   只在 `remaining <= threshold(4)` 时才续量，而当时还剩 35 次 ⇒ 正常拒绝。
   → 建议随 F6 一并修正 reason 命名（区分 `correction_budget_exhausted` 与 `model_budget_exhausted`）。

---

## 7. 状态表

| 项 | 内容 | 不变量 | 状态 |
|---|---|---|---|
| F1 | 动作词表补 `EXHAUSTED` | INV-13 | ✅ 已实施 |
| F2 | Policy 改 gap-determined 四分支，删 in-turn 计数器 | INV-13 | ✅ 已实施 |
| F3.1 | 消费者侧归一化：`COMPLETE ∧ required gap ⇒ EXHAUSTED`（legacy 限定，越过 diagnostic-only） | INV-13/16 | ✅ 已实施 |
| F3.2 | `EXHAUSTED` 接回 `_suspend_incomplete_recoverable`（含一次 wrap-up 轮，由 `last_action` 派生且有预算前提） | INV-14/16 | ✅ 已实施 |
| F3.3 | ~~删守卫的 `not disclosure_only`~~ | — | ⛔ 已被 F3.1 吸收，不需要 |
| F3.4 | 续量判定去 `reason` 字符串，改判 `action is EXHAUSTED` | INV-15 | ✅ 已实施 |
| F3.5 | 工具的 `effective_recovery_kind` 穿进 `ExecutionFact` → `CompletionGap.recoverable`（并给 `UNRESOLVED_EFFECT_FAILURE` 加 INV-3 豁免） | INV-14 | ✅ 已实施 |
| F4 | 判据与 gap 同源（可选） | — | ☐ 暂不做 |
| F5 | disclosure 轮允许工具 | — | ☐ 已降级为产品取舍 |
| F6 | 删除 in-turn 纠正计数器 | INV-13/14 | ✅ 已实施 |
| T | `tests/test_completion_readiness.py` 旧契约用例迁移 | — | ✅ 完成（31 passed） |
| R | 外部参考 | — | ✅ 完成（Hermes #122173/#122176；Codex `TurnStatus` × `ThreadGoalStatus`） |

### 最终测试结果（收尾后复核）

- 全量：**`8 failed / 1198 passed / 8 skipped`**（175 subtests passed）。
- 基线口径：同一个 venv，在 HEAD `a2a9c1e` 的**干净 worktree**（`git worktree add --detach`
  并用 `PYTHONPATH=<worktree>/src` 压过 editable 安装）实测为
  **`10 failed / 1166 passed / 8 skipped`**（157 subtests passed）。因此当前工作树
  （含并行改动）相对 HEAD **净增 32 个通过用例、消除 2 个既有失败、零新增失败**。
- 消除的 2 个即 `test_composition` ×2；它们**不是**环境失败，而是期望工具表过期
  （缺 `core.grep_search`）叠加一处测试不密闭，详见 §8。
- 剩余 8 个与本次改动无关，且已在 HEAD 干净 worktree 上复现确认：
  `test_cli_line_editing` ×5（`OSError: out of pty devices`）、
  `test_local_workspace_sandbox` ×2（OS 隔离 / 信任缓存环境）、
  `test_investigation_status` ×1（`status.tool_calls` 实测 0、期望 1）。
- `tests/test_completion_readiness.py`：31 passed（含迁移后的旧契约用例）
- `tests/test_completion_readiness_exhausted.py`：7 passed（红转绿）
- `tests/test_completion_claim_gate.py`（反谎报闸门）：通过，未改动语义

### 实施中发现并修掉的三处逻辑问题

1. **F3.3 是多余项**：F3.1 的归一化条件（任意 required gap）严格强于原守卫（仅 `task-spec:` 前缀），
   因此原守卫不可达 → 直接删除整个守卫，F3 从 4 项减为 3 项。
2. **F3.1 必须限定 legacy gate**：非 legacy 模式在 `kernel.py:13468-13476` 会把每个"无 tool call"响应
   **强制改写成 `COMPLETE/diagnostic_only`** 并有意把决定权交给调用方；无条件归一化会破坏该语义。
   归一化因此加 `legacy_completion_gate` 且置于该 override 之后。
3. **wrap-up 轮需要预算前提**：`EXHAUSTED` 的第一次裁决会买一轮"诚实汇报"，但若 `remaining_model_calls <= 1`
   （该值仍含刚发生的那次调用）就不该尝试，否则空耗预算并触发 `AgentLoopLimitExceeded`。
   → 条件加 `remaining_model_calls > 1`。

### 需要记录的**行为变化**（非缺陷）

1. **F3.5 让"不可纠正的失败"不再空耗预算**：`recovery_kind ∈ {terminal, user_action_required, unknown_outcome}`
   的执行失败 gap 现在 `recoverable=False, required_effects=()`，门立即 `EXHAUSTED`（→ wrap-up → 可恢复挂起），
   而不是反复 `CONTINUE` 直到预算耗尽。未分类的失败仍保守地视为可纠正。
2. **INV-3 新增豁免 `UNRESOLVED_EFFECT_FAILURE`**：这类"不可关闭但必须阻断完成"的 gap 不再被降级成
   可选（降级会让"必需副作用从未发生"的任务报成功）；它的有界逃生是 `EXHAUSTED` → stall cap → `NEEDS_REVIEW`。
3. **INV-10 在 per-turn 门也生效**：无 judge 的组合遇到含 `ANSWER` 的 Task（自带 `goal_alignment` 判据）时
   现在会**挂起**而非跑完再在验收层判 BLOCK。因此 `test_production_journeys` 的
   `test_invalid_side_effect_binding_is_corrected_without_execution` 需要像其他 journey 一样装配
   `rubric_judge_adapter`（该用例关注副作用绑定，不是 judge 配置）。
4. **测试夹具迁移**：`BlockedReadTool` 显式声明 `USER_ACTION_REQUIRED`（权限拒绝不可由再次行动修复），
   `ReadinessSequenceModel` 增加 `EXHAUSTED` 分支，`BusinessFailureModel` 每轮生成唯一 `call_id`
   （工具调用 id 在轮内必须唯一）。

---

## 8. 收尾记录：全量门禁上的两个外部失败归零

§7 用"失败集合与改动前一致"当门禁。收尾时门禁上还挂着两个**不属于**完成判定改动的失败。
它们此前被记成"既有环境失败"，实际各有明确成因；一并定位修复，避免真实缺陷被长期豁免。

### 8.1 `test_composition::test_real_provider_engineering_composition_has_process_tools`

两个叠加的成因，都不是实现缺陷：

1. **测试不密闭（本次修复）**。该用例用 `patch.dict(os.environ, {...}, clear=True)` 只注入 3 个变量，
   却调用**不带 `env_file` 参数**的 `compose_openai_compatible_engineering_application_from_env()`，
   于是回落到开发者本机 `.env`。协议配置改成"选择器 + per-protocol 模型名"后，本机 `.env` 里的
   `TSM_AGT_OPEN_MODEL` 从文件层注入，与用例注入的 legacy 字面量 `TSM_AGT_MODEL=test-model` 组合，
   正好命中"含糊配置"守卫 → `ValueError: TSM_AGT_MODEL must be 'open' or 'anthropic'`。
   同文件其他用例都显式传 temp `env_file`，只有它没有。
   → 改为 `tempfile` + 显式 `env_file`（`TSM_AGT_MODEL=open` / `TSM_AGT_OPEN_MODEL=test-model`）。
   **没有**放宽守卫：守卫拦下的确实是"选择器是字面量 + 存在 per-protocol 模型名"的真实含糊配置。
2. **期望工具表过期（既有失败，非本次引入）**。修掉配置问题后，该用例与
   `test_readonly_application_exposes_builtin_workspace_tools` 都只剩同一个差异：
   期望列表缺 `core.grep_search`。该工具在 HEAD 就已注册
   （`adapters/builtin/core_tools.py` 的 `CoreReadOnlyToolProvider`，与 `core.search_text`
   共用 `_search_text` 实现），两处期望列表自新增该工具起一直没更新，各差 1 个元素
   （readonly 实测 5 项、expected 4 项；engineering 实测 35 项、expected 34 项）。
   → 两处期望列表补 `core.grep_search`。

### 8.2 Anthropic Messages 适配器的 `system` 线上形状 + 架构违规

- **线上形状**：适配器把内部 system 消息累积进 `system: list[str]`，但发出的
  `payload["system"]` 形状与该网关文档不一致。目标网关把 `system` 记为**字符串**
  （官方 API 两种都收），故统一为 `payload["system"] = "\n\n".join(system)`，
  并在累积处留注释说明"缓冲区是 `list[str]`，因为线上形状是拼接后的单串"。
  该形状由 `tests/test_anthropic_messages_model.py` 直接断言
  （`request["payload"]["system"] == "be precise"`）。
- **架构违规**：Anthropic 适配器原本 `import tsm_agt.adapters.openai_compatible` 复用 HTTP/SSE 管道。
  已抽出协议中立的 `tsm_agt.adapters.http_json`（`HttpJsonTransport` /
  `UrllibHttpJsonTransport` / `ModelProviderTransportError`），两个 provider 适配器各自依赖它；
  OpenAI 适配器保留 `OpenAICompatibleProviderError = ModelProviderTransportError` 兼容别名。
- **架构门禁补强**：`tests/architecture/test_dependencies.py` 增加 `SHARED_ADAPTER_MODULES`
  白名单（只允许中立模块），并新增两条用例：白名单成员不得是 provider 适配器、
  Anthropic 适配器不得 import OpenAI 适配器。

### 8.3 结论

全量失败集合 = HEAD 基线失败集合 **减去** 两项 composition，**零新增**。剩余 8 项与本次
全部改动无关，并已在 HEAD 干净 worktree 上逐项复现确认（PTY 不可用 ×5、OS 隔离/信任缓存 ×2、
`status.tool_calls` ×1）。
