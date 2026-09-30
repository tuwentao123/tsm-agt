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
| `agent/` 目录 | **250 文件 / 114,134 行**（平均 456 行） |

**目标不是"每个文件 <2000 行"。** Hermes 自己 >2000 行的 `.py` 有 **50 个**，最大
8,255 行（`agent/auxiliary_client.py`）。`AGENTS.md:246-248` 的规则是"**过 2000 就按
`<stem>_<topic>` 拆，且单独一个 commit**"——是拆分**信号**，不是"必须小于"。

⇒ 真正的目标是：**`kernel.py` 变成 ~1,500 行的门面（公开入口 + 别处 import 的名字），
其余按 topic 变成 `core/*` 兄弟**，就像 `run_agent.py` 之于 `agent/turn_*.py`。

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

## 5. 建议顺序（每刀一个 commit）

排序依据：被引用少、依赖单向、Hermes 有对应 topic（好照抄）。

| # | 域 | 行数 | 目标文件（`core/` 下） | 依据 |
|---|---|---:|---|---|
| 1 | session | 2,173 | `session_persistence.py`, `session_prompts.py` | Hermes 同名文件存在；`session_context.py` 已在 |
| 2 | tool | 1,769 | `tool_invocation.py`, `tool_guardrails.py` | 对应 `agent/tool_executor.py` |
| 3 | projection / flow | 758 | `*_projection.py` | 纯投影，无副作用，最安全 |
| 4 | workspace / mutation + process | 834 + 379 | `workspace_mutation.py`, `process_control.py` | 对应 `file_safety.py` 等 |
| 5 | context / prompt | 503 | `turn_context.py`, `prompt_assembly.py` | 对应 `turn_context.py` |
| 6 | approval / policy + memory | 378 + 194 | `approval.py`（已在 core）, `memory.py`（已在 core） | 小 |
| 7 | task-lifecycle + task-spec | 544 + 351 | `task_lifecycle.py` | **本项目新增域**，形状自定 |
| 8 | verify / proof | 1,715 + §4.4 的 8 个文件 | 先定性再拆 | **本项目新增域**，最后做 |
| 9 | 剩余 turn/agent-loop + "未归类" | — | `turn_checkpoint.py`, `completion_readiness.py` … | 收尾 |

预期终态：`kernel.py` **~1,200–1,800 行门面** + 20 个左右兄弟。

---

## 6. 每刀必须走的清单（从四次真实失败里固化的）

1. **跨边界分析**：本刀绑定的名字 ∩ 后续阶段的读取 → 该进 `TurnState`（或本刀自己的
   slots dataclass）的字段，**先加字段再搬代码**。
2. **AST 定位 + 断言唯一性**。四次失败全是"用第一个像的当目标"：
   保护带只取首行（漏多行 `replace(...)` 续行）、`next()` 命中另一个 1 行赋值而目标是
   26 行、`"\nclass Kernel"` 子串命中非预期位置。**不许用第一个 substring 匹配。**
3. **搬完立刻断言闭包** `== ['self', 'state']`（或本刀的等价物），**在写入前**。
4. **先跑定向测试（~0.6 s）再跑全量（~23 s）**。前两次失败用全量当第一道验证，
   超时只报"挂了"不报"哪挂"。
5. **禁止 source-text 测试**（`AGENTS.md:487-502` banned outright）。移动符号后若
   有测试挂了，先看它是不是在读源码——是就改成行为测试。
6. **不给内部迁移留 re-export shim**（`AGENTS.md:250`）。

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
