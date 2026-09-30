# tsm-agt 轮循环相位图（`kernel.py::_continue_agent_turn`）

> 来源：`src/tsm_agt/core/kernel.py`，函数 `:12038-14170`，**2,133 行**。
> 抽取方式：AST 遍历顶层与 `while` 体顶层语句，逐条登记行号与注释（不是通读，**行号与注释为实测**）。
> 用途：重构方案 M1 的开工依据。对照物是 `hermes-架构梳理/02-轮循环与相位协议.md`。

---

## 1. 函数骨架

```
_continue_agent_turn                              :12038
├── 变量播种序言   26 条赋值                       :12051-12089   (39 行)
└── while True:                                   :12091
    └── 循环体 80 条顶层语句                       :12097-14170  (2,074 行)
```

**一个 39 行的序言 + 一个 2,074 行的无限循环体。** 没有相位函数，没有统一状态对象——状态是 26 个局部变量 + 一个反复 `replace()` 重建的 `checkpoint`。

序言播种的变量（`:12051-12089`）：`task_id` `turn_id` `live_spec` `closed_outcomes_at_entry` `checkpoint` `messages` `pending_tool_calls` `seen_call_ids` `model_call_count` `tool_call_count` `input_tokens` `output_tokens` `last_tool_error` `plan_guard` `no_progress_stop` `budget_wrap_up` `budget_wrap_up_decision` `protocol_recovery_attempts` `protocol_correction` `visible_tool_by_name`。

---

## 2. 五个宏观相位（实测行号）

| 相位 | 行范围 | 行数 | 内容 |
|---|---|---|---|
| **A · 循环头卫生 + 队列替换** | `:12097-12129` | 33 | 取消边界 → `_synchronize_tool_batch` → `_cancel_open_batch_for_queued_replace` → `_apply_pending_steering("before-tool-batch"\|"before-model")` → 若 replaced：清空 pending + `plan_task_spec` + 刷新 spec revision/hash |
| **B · 工具批处理** | `:12130-13189` | **1,060** | `while pending_tool_calls:` —— 逐个工具调用的冻结、outcome 绑定、审批、执行 |
| **C · 模型调用准备** | `:13190-13560` | 371 | 边界规范化 → 调用上限检查 → **`CompletionReadinessState.from_data`(`:13219`)** → `legacy_completion_gate`(`:13222`) → `_completion_readiness_gaps`(`:13242`) → `ExplorationBudgetState`(`:13277`) → **重建上下文(`:13446-13450`)** → 压缩(`:13464`) → `prompt_template.assemble`(`:13483`) → `ModelRequest`(`:13528`) |
| **D · 模型调用** | `:13561-13789` | 229 | `try:` 包裹的实际调用 + `record_transport` 回调 |
| **E · 响应处理 + 完成判定 + 出口** | `:13790-14170` | 381 | token 累计 → `messages.append(response.message)` → `SteeringProjector.project`(`:13808`) → **`CompletionReadinessDecision`(`:13862`)** → 完成门(`:13989`) → `final_response`(`:14017`) → `_commit_model_response_checkpoint`(`:14050`) → **出口分支(`:14055-14169`)** |

**相位 B 独占 1,060 行**，比 Hermes 的整个 `run_tool_round`(299) + `turn_tool_validation`(227) + `tool_executor` 的调度部分加起来还大。

---

## 3. 两个决定性发现

### 3.1 多轮根因的铁证：上下文靠"擦掉重建"（`:13437-13450`）

```python
# Dynamic context is persisted in checkpoints for crash recovery, but
# must be rebuilt before every model call. Keeping an old
# session-context message lets stale Task summaries outlive later
# user input and dominate resumed turns.
generated_prefixes = (
    "project-instructions-context-", "task-spec-context-",
    "project-onboarding-context-", "project-memory-context-",
    "session-context-", "document-references-context-", "working-memory-context-",
)
messages = [
    message for message in messages
    if not message.message_id.startswith(generated_prefixes)
]
messages.extend(await self._project_context_messages(task_id))
```

**每次模型调用前，7 类"生成的消息"被整体删除，然后用 `_project_context_messages` 重新投影。**

设计者的取舍写在注释里了：*"Keeping an old session-context message lets **stale Task summaries outlive later user input and dominate resumed turns**."*
→ 他宁可接受有损投影，也不要过期的 Task 摘要压过新用户输入。

**这正是"权威与上下文绑定"的代价**，也正是重构方案 M3 要解的那个结。

对照 Hermes 的解法（`01` 册 §2）：

| | tsm-agt | Hermes |
|---|---|---|
| 会话历史 | `session-context-*` 消息，**每次调用删掉重建** | `messages` 表里的真实行，**追加且逐字节重放** |
| 为什么不会"过期压过新输入" | 只能靠重建 | **因为它本来就是按时间追加的，不存在摘要** |
| 动态注入（prefetch/hooks） | 混在同一批 `*-context-` 消息里 | **只在 API 时注入，不进 `messages`**（`agent/turn_context.py:1241-1254`） |

### 3.2 完成判定在**轮内**（`:14097`），Hermes 在**轮外**

出口分支（`:14055-14169`）实际有四条：

| # | 条件 | 行为 | 行 |
|---|---|---|---|
| 1 | `not tool_calls and has_late_steering` | 应用 steering → `continue` | `:14060-14067` |
| 2 | `incomplete_recovery_boundary` | `_suspend_incomplete_recoverable(...)` → **return 暂停** | `:14070-14096` |
| 3 | `readiness.action is not COMPLETE` | `messages.append(self._completion_correction_message(readiness))` → 继续循环 | `:14097-14099` |
| 4 | `readiness.action is COMPLETE` | 走最终答复 | `:14100-14147` |
| 5 | 有 `tool_calls` | `pending_tool_calls = list(tool_calls)` → 继续 | `:14169-14170` |

**「是否算完成」由 `CompletionReadinessAction`（分支 3/4）在轮内决定**，路径 3 直接往消息里塞一条纠正消息再转一圈。

对照 Hermes：**完全相反**。

| | tsm-agt | Hermes |
|---|---|---|
| 判定位置 | **轮内**，`:13862` 构造 `CompletionReadinessDecision`，`:14097` 消费 | **轮外**，`gateway/run_goals.py:296` 的 `evaluate_after_turn` |
| 判定的产物 | `CompletionReadinessAction`（一套独立词汇表） | `decision` dict：`should_continue` + `continuation_prompt` |
| 不满足时怎么办 | 往 messages 里 **append 一条纠正消息**，再转一圈 | **把 prompt 塞回输入队列**，成为下一"轮"的输入 |
| 轮的边界 | 模糊——"再转一圈"和"真正的下一轮"是同一条循环 | 清晰——一轮就是一个 `run_conversation` 调用 |

**这就是"不知道如何完成闭环"的机械原因**：tsm-agt 把"完成判定"和"轮内续跑"合在了一个循环里，于是没有单一的出口权威（56 个 terminal-state 站点就是这么长出来的）。

---

## 4. 现状相位 → Hermes 相位的映射（M1 的靶子）

| tsm-agt 现状 | Hermes 对应相位 | Hermes 出处 |
|---|---|---|
| A 循环头卫生（取消/同步/steering） | `begin_iteration` + 独立 `turn_stop_gates` | `conversation_loop.py:1637` |
| C 前置（准备模型调用） | `prepare_iteration` | `:1639` |
| C 的上下文重建 + 压缩 | `assemble_api_request`（含压缩压力） + `turn_context_compaction` | `:1640`；`turn_request_assembly.py:106` |
| C 的 `_completion_readiness_gaps` | `run_preflight_gate` | `:1641` |
| D 模型调用 | `_run_api_retry_loop`（guard → build → call → check） | `:1501-1530` |
| E 的响应处理 | `normalize_model_response` | `:1665` |
| B 工具批处理（1,060 行） | **`run_tool_round`(299) + `turn_tool_validation`(227)** | `:1670-1672`；`turn_tool_validation.py` |
| E 的 `readiness.action is COMPLETE` | **`finish_text_response`** | `:1671` |
| E 的"append 纠正消息再转圈" | **不存在**——对应物在轮外（`evaluate_after_turn` 的 `continuation_prompt`） | `gateway/run_goals.py:306` |
| 26 个局部变量 + `checkpoint` replace | **一个 `_LoopState`** | `conversation_loop.py:1385-1420` |
| 无 verdict；靠 `if/continue/return` 散落 | **每个相位返回 `action ∈ {ok,continue,break,return}`** | `:1641-1678` |

**M1 的核心动作**（按收益排序）：

1. **E 的完成判定外移**：把 `CompletionReadinessDecision` 的消费从 `:14097` 挪到轮之后，使它产出 `should_continue` + `continuation_prompt`（Hermes 形状），而不是 append 一条消息再转一圈。**这一条同时解决 M2（出口收敛）**。
2. **B 拆出去**：1,060 行的工具批 → `<stem>_<topic>` 兄弟文件（Hermes 的 `turn_tool_round` + `turn_tool_validation` 是 299+227）。
3. **26 个变量 → 一个 `_TurnState`**，相位按名取参/按名写回。
4. **A 的取消边界**保持（tsm-agt 独有，Hermes 靠 `_interrupt_requested` 检查，`agent/AGENTS.md:28`），但包成相位。

---

## 5. 未核实项

1. **相位 B 的 1,060 行内部结构**（`:12130-13189`）——最大的一块，本轮只登记了它的边界。
2. **相位 C 的 `disclosure_only` 大分支**（`:13280-13415`，约 135 行）与 `:13416 not legacy_completion_gate` 分支的实际语义。
3. **相位 E 的 `:14100-14147`** 最终答复路径（只读到 `:14099`）。
4. `CompletionReadinessState` / `CompletionReadinessAction` / `CompletionReadinessDecision` 的定义与全部取值。
5. `_project_context_messages` 的实现（多轮投影的实体）。
6. `_completion_correction_message` 生成的纠正消息长什么样——它决定了"转一圈"能否修好。
7. `_suspend_incomplete_recoverable`（分支 2）与 `_record_session_task_result`（`:14091`）的关系。
8. `context_manager.prepare`（`:13452`）的压缩策略——tsm-agt 的压缩是否也是"唯一合法的缓存破坏点"（Hermes 不变量）。
