# 按 Hermes 重构：kernel.py → facade 的映射与顺序

> 目的：回答"现在能不能按 Hermes 重构框架"，并把答案压成可执行的清单。
> 全部数字为实测：`wc -l`、AST 遍历、`ls`。
> Hermes 源码在 `/tmp/hermes-src`（`NousResearch/hermes-agent`，`--depth 1`）。
> 规则引自 `AGENTS.md:232-267`（facade + siblings、"过 2000 行/300 行/CC30 就拆"、
> 禁止 `if/elif` ≥4 分支、禁止 re-export shim）。

---

## 1. 结论

**能。** 而且"按 Hermes 重构"这件事的**剩余范围已经被量化**：不是重做架构，是
**把 `core/kernel.py` 从 15,990 行的 god file 变成 `run_agent.py` 那样的门面**。

轮循环那一段**已经做完了**——它现在的规模已经在 Hermes 自己同一件事的规模之内或之下（§2）。

---

## 2. 已完成：轮循环已经是 Hermes 形状

Hermes 承担"一个 Turn 从进入到出结果"这件事的模块：

| Hermes 文件 | 行数 |
|---|---|
| `agent/conversation_loop.py`（含 `_LoopState`、`_run_phase`） | 1,788 |
| `agent/tool_executor.py` | 1,926 |
| `agent/session_persistence.py` | 533 |
| `agent/turn_failure_copy.py` | 459 |
| **小计** | **4,706** |

tsm-agt 现在承担同一件事的模块：

| tsm-agt 文件 | 行数 |
|---|---|
| `core/turn_phases.py` | 1,083 |
| `core/turn_tool_batch.py` | 1,146 |
| `core/agent_loop.py`（`TurnState` + `PhaseVerdict`） | 705 |
| `core/verification_criteria.py` | 117 |
| **小计** | **3,051** |

四个机制也已对齐：

| Hermes | tsm-agt |
|---|---|
| `_LoopState`（`conversation_loop.py:1385-1420`，字段几乎全是 `Any`） | `TurnState`（`core/agent_loop.py`） |
| `action ∈ {ok, continue, break, return}`（`:1641-1678`） | `PhaseAction` + `PhaseVerdict` |
| `_run_phase`（`:1480-1498`）按签名取参、按名回写 | 四相位方法，闭包断言 `(kernel, state)` |
| facade + late-import 兄弟（`AGENTS.md:243-244`） | `kernel.py` 5 行转发 → `turn_phases` / `turn_tool_batch` |
| `_EPHEMERAL_SCAFFOLDING_FLAGS`（`session_persistence.py:36-44`） | `SCAFFOLDING_KINDS`（`ports/model.py:180`） |

**⇒ 轮循环不需要再"按 Hermes 重构"。** 它已经在这个形状里了。

---

## 3. 剩余范围：`kernel.py` 本身

### 3.1 实测

```
kernel.py              15,990 行
  Kernel 成员          14,270 行 / 273 个方法
  模块级函数              444 行 / 23 个
  类：KernelDependencies + 8 个 _*Control/_Path* 小类
```

`Kernel` 里只有 4 个方法过 300 行（Hermes 的拆分信号线）：

```
411  verify_task_acceptance
348  _execute_authorized_tool
325  resolve_session_input
318  _completion_readiness_gaps
```

### 3.2 Hermes 的对照物

| | 行数 |
|---|---|
| `run_agent.py`（门面） | **1,582** |
| `AIAgent` 类本身 | **1,176 行 / 71 个方法** |
| `agent/` 目录 | **250 文件 / 114,134 行**（平均 456 行） |

**目标不是"每个文件 <2000 行"。** Hermes 自己 >2000 行的 `.py` 有 **50 个**，最大
8,255 行（`agent/auxiliary_client.py`）。`AGENTS.md:246-248` 的规则是"**过 2000 就按
`<stem>_<topic>` 拆，且单独一个 commit**"——是拆分**信号**，不是"必须小于"。

### 3.2.1 门面的形状是 **Mixin**，不是"搬到模块级函数"

这是 `run_agent.py:137-150` 读出来的、也是整个重构的关键：

```python
from agent.client_lifecycle import ClientLifecycleMixin
from agent.stream_delivery import StreamDeliveryMixin
...
from agent.session_persistence import SessionPersistenceMixin
from agent.turn_facade import TurnFacadeMixin
...                                    # 共 13 个

class AIAgent(
    ClientLifecycleMixin, StreamDeliveryMixin, StatusOutputMixin, ApiRequestHooksMixin,
    ApiErrorSummaryMixin, InterruptControlMixin, TurnExplainersMixin, ActivityTrackingMixin,
    RateLimitCreditsMixin, SessionPersistenceMixin, CompressionFacadeMixin, TurnFacadeMixin,
    VisionMessagePrepMixin, ReasoningParamsMixin,
):
```

Mixin 文件自身（`agent/session_persistence.py:413`）就是裸类：

```python
class SessionPersistenceMixin:
```

**这意味着搬迁是"方法体一字不改"**——`self._dependencies`、`self._append_events`、
`Kernel` 自己的类属性全部照旧解析，因为实例的 `self.__class__` 仍是 `Kernel`，
属性查找从 `Kernel.__mro__` 开始。**没有重命名、没有签名改动、没有跨边界分析。**
这正是 Hermes 能连拆 15 个 god file 而不炸的原因，也正是本项目前四刀反复踩坑
（AST 改名、闭包断言、多行 `replace` 续行）的根因：**那些坑是自己造出来的。**

`agent/lazy_forward.py` 是配套的转发器（`forward(module, name)` 在**每次调用时**
解析目标），存在的理由是两条：`patch("<module>.<name>")` 仍能拦截，以及重模块
不进 `run_agent` 的 import 路径。

Hermes 同时保留**模块级函数**形式（`conversation_loop.py`、`turn_stop_gates.py`
的 `apply_stop_gates(agent, ...)`）。两者分工：**属于 `AIAgent` 自身的内聚方法组
→ Mixin；相位/策略助手 → 模块级函数**。本项目已抽的
`turn_phases.py` / `turn_tool_batch.py` 属后者，形状正确，不必改。

⇒ 真正的目标：**`kernel.py` 变成 ~1,200–1,800 行的门面（`class Kernel(<诸 Mixin>)`），
其余按 topic 变成 `core/kernel_<topic>.py` 的 `<Topic>Mixin`。**


### 3.3 分域清单（AST 聚类，27 个方法被多域命中时归首个匹配）

| 域 | 行数 | 方法数 | Hermes 对应 topic（可参考形状） |
|---|---:|---:|---|
| turn / agent-loop | 2,529 | 33 | `turn_facade.py`, `turn_recovery*.py`, `turn_liveness.py`, `turn_context.py` |
| session | 2,173 | 40 | `session_persistence.py`, `session_activity.py`, `title_generator.py` |
| （未归类） | 2,143 | 53 | 见 §3.4 |
| tool | 1,769 | 28 | `tool_executor.py`, `tool_dispatch_helpers.py`, `tool_guardrails.py` |
| verify / proof | 1,715 | 27 | `verification_evidence.py`, `verification_stop.py`, `verify_hooks.py` |
| workspace / mutation | 834 | 15 | `file_safety.py`, `runtime_cwd.py` |
| projection / flow | 758 | 19 | `trajectory.py`, `activity_tracking.py`, `display.py` |
| task-lifecycle | 544 | 12 | *(无直接对应，见 §4)* |
| context / prompt | 503 | 11 | `turn_context.py`, `turn_context_compaction.py`, `prompt_builder.py` |
| process | 379 | 13 | `process_bootstrap.py`, `terminal_env_provider.py` |
| approval / policy | 378 | 9 | `terminal_approval_batch.py`, `tool_guardrails.py`, `estop.py` |
| task-spec | 351 | 5 | *(无直接对应，见 §4)* |
| memory | 194 | 7 | `memory_manager.py`, `memory_provider.py` |
| **合计** | **14,270** | **273** | |

### 3.4 "未归类" 53 个方法是哪些

按行数排（前 20）：

```
318  _completion_readiness_gaps          → 本项目新增（完成判定，见 §4）
259  route_runtime_input                 → 对应 runtime_input 域
109  _commit_final_agent_result          → turn_final_response.py
104  _evaluate_completion_readiness      → 本项目新增
103  resolve_needs_review                → 本项目新增（AG-16）
 78  _demote_unresolvable_criteria       → 本项目新增
 69  _normalize_and_validate_model_boundary → turn_request_assembly.py
 61  _evaluate_exploration_before        → 本项目新增（探索预算）
 58  _decide_stop_or_pivot               → 本项目新增
 58  _validate_agent_response            → turn_response_check.py
 55  _finalize_needs_review              → 本项目新增
 44  _demote_background_leaks            → 本项目新增
 44  _evaluate_read_hits_before          → 本项目新增
 41  _evaluate_artifact_read_before      → 本项目新增
 40  _complete_agent_model_request       → turn_api_call.py
 40  _resource_records                   → 本项目新增
 36  _update_read_hits_after             → 本项目新增
 35  _response_diagnostics               → turn_api_error.py
 33  _artifact_read_probe                → 本项目新增
 31  _update_artifact_read_after         → 本项目新增
```

**⇒ "未归类"里大部分是本项目新增的策略域，不是 Hermes 概念的错位。** 这不影响拆分，
只影响"拆出来的文件该叫什么、该由谁负责"——它们是按 topic 可分的，只是 topic 名由本项目定。

---

## 4. 三处**不能**照抄 Hermes

必须在 Hermes 之外自己定形状，不能声称"参考 Hermes"：

### 4.1 证明栈

`verify / proof` 1,715 行，加上已在 `core/` 的 `proof_obligation.py` /
`proof_projection.py` / `evidence_coverage.py` / `artifact_graph.py`。

Hermes **有**三个相关的模块，但**都不是证明栈**：

| Hermes 文件 | 行 | 它实际做什么 | 自带的关键约束（引自 docstring） |
|---|---:|---|---|
| `agent/verification_evidence.py` | 590 | 记账：从 **shell 命令字符串** 正则识别 pytest/lint/typecheck/build，配 exit code，落一张 SQLite 表 | "**Deliberately passive — it never runs a suite, never blocks completion, and never upgrades targeted checks into 'repo green'**" |
| `agent/verification_stop.py` | 230 | 把账本变成一次有限的提醒 | "**Policy-only: it never runs checks itself**"；`verify_on_stop_enabled` 注释写明 **default OFF (opt-in)** |
| `agent/turn_stop_gates.py` | — | 三个 stop gate：verify-on-stop → `pre_verify` hook → kanban terminal guard | 都是 `_continue(nudge)`：追加一条 user 角色提醒行、把 turn 交回循环；候选答案存进 `pending_verification_response` 当**预算耗尽兜底** |

提醒上限 `DEFAULT_MAX_VERIFY_NUDGES = 3`（`agent/verify_hooks.py:16`）。

**⇒ Hermes 里"没有证据"永远不会变成 FAILED / BLOCKED 这个判定，只变成一句提示。**
佐证：`_EXIT_REASON_FAILURES`（`agent/turn_failure_copy.py:93-111`）14 条退出原因里
**没有一条与验证有关**；`local_processing_error` 还显式标 `fails_turn=False`，注释为
"a deterministic local bug is not a task failure"；连最硬的 kanban gate 也只是
"…**nudging** to finish"。

**而证明栈的核心概念，全仓 0 次匹配：**

```
grep -rI "proof_obligation|derive_evidence|evidence_coverage|required_effects|result_authority"
→ 0 个文件
```

**为什么没有——三层原因：**

1. **机械原因（最硬）：输入不在那里。** Hermes `register()` 只带 name / description /
   schema / handler，**不带 `effect`、不带 `result_authority`**。所以它**结构上无法**从
   工具推导"这次调用能证明什么"。`derive_evidence_capabilities` 是从
   `ToolSpec.effect + result_authority` 推的——Hermes 的 ToolSpec 里没这两个字段，
   没有输入就没有这条链。AG-1..AG-16 / `ProofEnforcementMode`（OBSERVE /
   ENFORCE_NEW_TASKS / ENFORCE_ALL）同理，无对应语义。
2. **设计原因：完成是"模型说的"，不是"推出来的"。** 见上表与 `_EXIT_REASON_FAILURES`。
3. **架构原因：Hermes 的信任放在别处——不让坏事发生，而不是发生后能证明。**
   `tool_guardrails.py`(660) / `terminal_approval_batch.py` / `estop.py` /
   `secret_scope.py` / `redact.py` / `runtime_self_protection.py` + 人工审批。
   `background_review.py`(1,342) 是**事后复盘存 skill/memory**，不是验收。
   Hermes 的会话持久对象是 `messages` / `sessions`，一轮的产物是**文本**；
   没有 Outcome，就不存在"某个 outcome 是否达成"这个问题。

**本项目为什么必须有：** 因为本项目把 `Task` / `TaskSpec` / `Outcome` /
`AcceptanceCriterion` 做成了**持久对象**。一旦把"这个 outcome 必须在 MUTATE 效果下
才算达成"写进磁盘，就必须有东西判定它——否则持久化的准则只是装饰。
**⇒ 证明栈不是过度设计，是"选了任务模型"之后欠下的债。**

**对拆分的含义：** `verification_evidence.py`（记账）/ `verification_stop.py`（策略）/
`turn_stop_gates.py`（门）这个**三层分工**可以借鉴；但**判定语义**必须自定，
照抄会把证明栈做丢。

### 4.2 `TaskSpec` 与完成判定

**先前版本此处写"Hermes 没有对应的领域对象"，是错的，已更正。**

Hermes **有**验收，但在 kanban 子系统里，且形状完全不同：

| Hermes | 行 | 内容 |
|---|---:|---|
| `hermes_cli/kanban_pr_acceptance.py` | 200 | `validate_contract()` / `prepare_acceptance()` / `record_acceptance()` / `_GateAuthError` |
| `hermes_cli/kanban_pr_acceptance_store.py` | 48 | 回执持久化 |

它接受的 `completion_contract` 只有三种取值：`local-only` / `OWNER/REPO` /
一个精确的 GitHub PR URL（`_REPO` / `_PR` 两个正则全匹配）。验收动作是
**用 `gh api` 去问 GitHub 精确 head 的 PR 状态**，并且只持久化状态码 + endpoint，
从不记 `gh` 的 stderr（"never gh's stderr (credentials/host details)"）。

**⇒ 差别不是"有没有验收"，而是验收的**权威来源**：**

| | 验收权威来自 |
|---|---|
| Hermes kanban | **外部系统**（GitHub 说 PR 合了没），契约压成一个字符串枚举 |
| 本项目 | **本地持久证据的推导**（effect + authority → 能力 → 义务 → 覆盖） |

两者不可互换。本项目 AG-12 / AG-16 的判定（`TaskVerificationResult`、
`CompletionReadinessAction` 5 个取值、`terminal_state_for()`）在 Hermes 里没有对应物；
Hermes 里 `max_iterations_reached` / `budget_exhausted` 甚至不在失败表里（`failed=False`），
那是 Hermes 的语义，不是本项目的。

**对拆分的含义：** `completion_readiness` / `final_acceptance` 这类域拆出来的文件
由本项目自己命名与定责。

### 4.3 工具边界
`max_result_tokens` 在 `ToolSpec` 里**声明了、校验了、进了配置哈希，但边界从不执行**
（已核：`src/` 内除 `configuration.py:129` 的投影外无消费者）。
Hermes 有三层（工具内 per-tool cap → 落盘 + 预览 → `enforce_turn_budget`），
本项目只有第一层的零散实现。**这是 Hermes 有而本项目没有的，属于真缺口，不是重构。**

### 4.4 那 8 个未跟踪的 `core/*.py` 必须先定性
`turn_finalizer.py` / `turn_outcome.py` / `semantic_review.py` / `artifact_graph.py` /
`evidence_coverage.py` / `proof_obligation.py` / `proof_projection.py` /
`verification_command.py`，共 8 个文件、未提交。

**注意：Hermes 里确实有 `agent/turn_finalizer.py`、`turn_outcome` 相关的
`turn_final_response.py`、`verification_evidence.py`、`verification_stop.py`。**
所以这批文件的 **topic 命名不是空想的**；先前对它们"形状是编的"的判断需要
**逐个对 Hermes 对应文件重新核一遍**，而不是直接删。

**⇒ 在 verify 域动刀之前，必须先给这 8 个文件定性（留 / 改 / 删）。**

---

## 5. 实际进度与顺序

实际执行时**不照抄上面的排序**——`flow/replay` 只有 77 行，单独建文件不划算；
而 `completion-readiness`（731 行 / 7 方法）被抽取脚本**正确拒绝**：它引用
`_has_active_workspace_mutation` / `_missing_effect_deliveries` /
`_post_mutation_verification` / `_task_spec_requires_command_verification` 四个 helper，
这四个同时被 `verify_task_acceptance` 使用，不能随它搬。**completion 与 verify 确实
纠缠，必须一起在第 8 刀处理。**

| # | 域 | 选定行数/方法 | 目标 | 状态 |
|---|---|---:|---|---|
| 1 | session | 2,173 / 40 | `kernel_session.py` / `SessionMixin` | ✅ `653f5f4` |
| 2 | tool | 1,500 / 22 | `kernel_tool.py` / `ToolMixin` | ✅ `f3d3f18` |
| 3 | workspace / mutation | 1,075 / 16 | `kernel_workspace.py` / `WorkspaceMixin` | ✅ `1b42605` |
| 4 | process / background | 476 / 20 | `kernel_process.py` / `ProcessMixin` | ✅ `41aae86` |
| 5 | projection（flow/replay + investigation status + effective configuration） | 283 / 11 | `kernel_projection.py` / `ProjectionMixin` | ✅ `2db8ecb` |
| 6 | project-onboarding / 项目指令 / 信任 | 310 / 9 | `kernel_onboarding.py` / `OnboardingMixin` | ✅ `5d2f743` |
| 7 | 进度上报 + 渐进作用域探针 | 262 / 5 | `kernel_progress.py` / `ProgressMixin` | ✅ `6b8262e` |
| 8 | approval | 340 / 7 | `kernel_approval.py` / `ApprovalMixin` | ✅ `bf0acb9` |
| 9 | memory（任务记忆 + 工作记忆） | 337 / 11 | `kernel_memory.py` / `MemoryMixin` | ✅ `4dc45a5` |
| 10 | task 生命周期 + SPEC 规划 | 897 / 13 + 4 符号 | `kernel_task.py` / `TaskMixin` | ✅ `def49cb` |
| 11 | 策略 before/after 探针 + 资源目录 | 496 / 14 | `kernel_policy.py` / `PolicyMixin` | ✅ `7fe8c85` |
| 12 | model 响应校验 / 诊断 / 失败记录 | 346 / 8 + 2 表 | `kernel_model.py` / `ModelMixin` | ✅ `3081d54` |
| 13 | 输入路由（`dispatch_input_event` / `route_runtime_input` / …） | ~380 / 4 | `kernel_input.py` | 待做 |
| 14 | agent-loop / turn（`run_agent_turn`、resume/suspend/clarify、取消） | 2,724 / 37 | `kernel_turn.py` | 待做 |
| 15 | **verify / proof + completion（纠缠，一起做）** | 2,318 / 34 + 18 个模块级函数 | 先给 §4.4 的 8 个文件定性 | 待做 |
| 16 | 门面收尾（`__init__` / `dependencies` / `_append_events` / `_require_stored_task` / 存储键） | ~200 | 留在门面 | 待做 |

当前状态：

```
kernel.py            15,990 → 7,146 行（272 → 96 方法，−55%）
core/kernel_session.py     2,267 行 / 40 方法
core/kernel_tool.py        1,960 行 / 22 方法
core/kernel_workspace.py   1,150 行 / 16 方法
core/kernel_task.py        1,038 行 / 13 方法 + 4 符号
core/kernel_policy.py        564 行 / 14 方法
core/kernel_process.py       558 行 / 20 方法
core/kernel_memory.py        398 行 / 11 方法
core/kernel_approval.py      396 行 /  7 方法
core/kernel_model.py         396 行 /  8 方法
core/kernel_onboarding.py    373 行 /  9 方法
core/kernel_projection.py    342 行 / 11 方法
core/kernel_progress.py      303 行 /  5 方法
class Kernel(ModelMixin, PolicyMixin, TaskMixin, MemoryMixin, ApprovalMixin,
             ProgressMixin, OnboardingMixin, ProjectionMixin, ProcessMixin,
             WorkspaceMixin, ToolMixin, SessionMixin)
```

预期终态：`kernel.py` **~1,200–1,800 行门面** + 16 个左右 `kernel_<topic>.py`。

**命名约定**：兄弟文件一律 `kernel_<topic>.py`。Hermes 用不带前缀的
`agent/session_persistence.py`，但本项目的 `core/` 已经有同名的**领域模块**
（`session.py` / `workspace.py` / `tool.py` / `process.py`），它们不是 Mixin；
加 `kernel_` 前缀让"Mixin 兄弟"与"领域模块"一眼可分。

**待收尾的一件事**：MRO 顺序是抽取顺序的副产物（新 Mixin 一律插到最前），不是设计
决定。语义上安全（跨贡献者重名由守卫测试挡住），但第 16 刀应排成一个有意图的顺序。

---

## 6. 每刀必须走的清单（全部来自真实失败）

**整条流程已固化为 `scripts/extract_kernel_mixin.py`**（`--dry-run` 先看依赖报告再执行）。
它把第 1–5 条写成断言，第 10 条写成报告。它是迁移工具，最后一刀完成后删除。

1. **选出内聚的方法组**，不做 AST 改名。Mixin 形状下方法体**一字不改**。
2. **装饰器感知的行区间**。`ast.FunctionDef.lineno` **不含装饰器**——装饰器在
   `decorator_list` 里各有自己的 `lineno`。第一刀正是漏了这一点：`_session_task_summary`
   的 `@staticmethod` 被留在原处，**粘到了下一个方法 `get_task` 上**，于是
   `self.get_task(task_id)` 变成静态调用、报 `missing 1 required positional argument`。
   搬运与删除都必须用 `min(decorator_list.lineno + [lineno])`。
   第二刀有 5 个方法带装饰器、第三刀 4 个、第四刀 3 个——这条不是偶发。
3. **断言方法集合守恒**：搬运前的 `Kernel` 成员名集合 == 搬运后的
   `Kernel` ∪ `<Topic>Mixin`。第 1–4 刀实测 272/232/210/194 全部 ==。
4. **断言 MRO 与大类冲突**：`<Topic>Mixin in Kernel.__mro__`，且
   `Kernel` 自身命名空间与各 Mixin **无同名方法**（类自身命名空间优先，重名会让另一个
   定义**静默**不可达）。已固化为 `tests/architecture/test_kernel_facade.py`，并做了
   注入缺陷的负向验证（确认它真的会失败）。
5. **断言依赖单向**：`kernel.py` 模块级导入兄弟，兄弟**不得**在模块级绑定门面
   （`AGENTS.md:243-244`）。注意 `sys.modules` 判据**无效**：`core/__init__.py:55`
   的 `from .kernel import Kernel` 让任何 `core/*` 导入都拖进门面；可判的是
   sibling 模块 globals 里有没有 `__module__ == 'tsm_agt.core.kernel'` 的对象。
6. **断言无未解析全局名**：用 `symtable`（解释器自己的作用域分析，别手写 walker）
   取搬移后方法体的 global 名，逐个查是否在模块 globals 或 builtins 里。
   这条补的是"`NameError` 只在那个分支第一次执行时才炸"的盲区。
   只查 `is_global()`；`is_free()` 是闭包引用（`self`、推导式变量），正常。
7. **棘轮表按搬迁重分布**：`tests/architecture/test_dependencies.py` 的
   `_TERMINAL_STATE_COUPLING_BASELINE` 量 `TaskState.<终止态>` 引用点，**只许降**。
   纯搬迁时该表要跟着改行，判据是**列求和守恒**（第 1 刀 56 == 56，`kernel.py` 34→32、
   新文件 2；第 10 刀 `TERMINAL_AUTHORITY` 随 `transition_task` 搬走，`kernel.py` 32→19、
   `kernel_task.py` +13，仍是 56）。其余刀该域无引用，表不动。
8. **先跑定向测试（~0.6–5 s）再跑全量（~23–27 s）**。
9. **禁止 source-text 测试**（`AGENTS.md:487-502`）。移动符号后若有测试挂了，先看它
   是不是在读源码——是就改成行为测试。
10. **patch 缝（第 3 刀新增，唯一无法静态检查的一条）**：
    `AGENTS.md:244-245` —— *"Patch where production reads. … a patch on the defining
    module passes silently."*
    方法搬走后，它从**兄弟模块**的 globals 解析导入名；测试里
    `patch.object(kernel_module, "<name>")` 绑的是门面那份拷贝，**静默不再拦截**。
    第 3 刀就是这么挂的：`VERIFY: ok` 全过，全量挂 2 个（`commit_prepared_workspace_deletion`
    / `commit_prepared_workspace_mutation`），另有 1 个文件（`test_core_apply_patches.py`）
    因为补偿路径也搬走了而同样命中。
    **处理方式是把 patch 目标修到 production 真正读的模块**，不是"重新指向定义模块"。
    脚本每次抽取后打印 `seam_names()` 供人工核对；真正的兜底是全量测试。
11. **符号 import 跟定义走（第 10 刀新增）。** 它与第 10 条是同一件事的两半，但规则**相反**：
    `from tsm_agt.core.kernel import X` 里 X 搬走了 → **repoint 到兄弟模块**（定义处），
    因为它**响**（导入即 ImportError，不是静默）。
    已固化为脚本的 `_detached_symbol_imports()`：在子进程里对**真实模块**做 hasattr
    检查，不读源码文本（re-export 与条件定义骗不过它），并做过注入缺陷的负向验证。
    第 10 刀命中 2 个测试文件。**记住三者的区别：import 跟定义；patch 跟读取点；
    两者都不是"重新指向定义模块"就能了事。**
12. **跨域符号用 late-import（第 10 刀新增）**。某符号同时被搬移集与留守代码使用
    （`_required_effects_from_outcomes`：任务域 ↔ verify 域），它**不能**搬。
    Hermes 的正解是**在读到它的函数里 `from .kernel import X`**（`AGENTS.md:243-244`）。
    额外好处：它在**调用时**从门面命名空间解析，所以 `patch(<facade>.<name>)` 照样拦截。
    已固化为脚本的 late-import 注入。**覆盖两种位置**：搬走的**方法体**和搬走的
    **模块级函数**——只覆盖前者会漏掉真实读取者（第 10 刀正是如此）。搬走的是
    **class** 时脚本显式 REFUSING，因为需要往它自己的方法里注入。
13. **`Kernel.<static>(...)` 改写为 `self.<static>(...)`（第 12 刀新增）**。
    这是脚本**唯一**的非逐字改写，因此逐处报告。Mixin 形状下 `Kernel` 不是兄弟模块的
    裸名，不改写就得给门面做 late-import（能跑，但把门面拖回来、绕过 MRO）。
    等价性：任何 `Kernel.f(x)` 必是 staticmethod/classmethod（普通方法这样调本来就缺
    `self`），`self.f(x)` 对两者都成立。
    **但对 `@staticmethod`/`@classmethod` 必须禁止改写**——它没有 `self`，改了就把
    `self` 变成未定义全局名（第 12 刀当场被 symtable 检查抓到）。
14. **不给内部迁移留 re-export shim**（`AGENTS.md:250`）。


---

## 7. 规模感

| | 文件数 | 行数 |
|---|---:|---:|
| tsm-agt `src/` | 215 | 69,301 |
| tsm-agt `core/` | 44 | 36,171 |
| Hermes 全仓 `.py` | — | 891,338 |
| Hermes `agent/` | 250 | 114,134 |

tsm-agt 整体约为 Hermes 的 8%。`core/` 里除 `kernel.py` 外最大的文件是
`flow.py` 1,622 行——**已经全部在 Hermes 的常态范围内**。

⇒ 这次重构的**全部工作量集中在 `kernel.py` 一个文件上**，范围有界。
