# 移除证明层（对齐 Hermes / Codex 语义）实施方案

> 决定：**不保留证明层**。目标语义与 Hermes/Codex 一致：
> 模型不再调用工具 ⇒ 回合结束 ⇒ 任务完成。唯一的"不成功"是异常或中止。
>
> 本文件只回答"怎么安全地删"。不再论证要不要删。

---

## 0. `SUCCEEDED` 由谁写 —— 已查清（原为未决问题）

删掉 `finalize_acceptance` 之后必须有人写终态。查 `core/task.py:103-190` 与
`core/kernel_task.py:740-772`，今天写 `SUCCEEDED` 前面有**三道闸**：

| # | 闸门 | 位置 | 内容 |
|---|---|---|---|
| 1 | 状态机 | `core/task.py:103-190` | `EXECUTING` 的合法后继里**没有** `FINALIZING`；今天唯一路径是 `EXECUTING → VERIFYING → FINALIZING → SUCCEEDED` |
| 2 | 终态权威 | `core/kernel_task.py:740-744` | 进终态需要 `TERMINAL_AUTHORITY` 允许的 authority（**待清点**：`SUCCEEDED` 允许哪些） |
| 3 | **通过的验证** | `core/kernel_task.py:757-772` | 若 `dependencies.final_acceptance_policy is not None`，必须存在 `verify.completed{status: passed}`，否则 `InvalidTurnState` |

**闸 3 的关键点：它是 `is not None` 条件——`final_acceptance_policy = None` 就把它关掉了。**
这是一个**依赖值**，不是代码分支。

### 结论：选 **b**（我原先推荐 a，现按证据改为 b）

| 选项 | 需要改什么 | 评价 |
|---|---|---|
| **b（推荐）** | **零状态机改动**：`final_acceptance_policy = None` 关掉闸 3；SDK/CLI 收到结果后走 `EXECUTING → VERIFYING → FINALIZING → SUCCEEDED`，但**不调用** `verify_task_acceptance` / `finalize_acceptance` | `VERIFYING`/`FINALIZING` 退化为记账跳；人工复核 API 不动 |
| a | 给 `LEGAL_TRANSITIONS` 加一条 `EXECUTING → FINALIZING` **并且** 还要过闸 2、闸 3 | 比 b 多两处改动，收益只是省一跳 |
| c | 删 `VERIFYING`/`FINALIZING`/`NEEDS_REVIEW` 三个状态 | 最大；`NEEDS_REVIEW` 还挂着 accept/cancel/return-for-revision |

**所以 S1 的实际大小比原方案小得多**：一个依赖值 + 两个调用点（`sdk/runtime.py`、`cli.py`）。

### 闸 2 已查清 → S1 的精确步骤

```python
# core/kernel_task.py:76-79
TERMINAL_AUTHORITY = {SUCCEEDED: {"runtime"}, FAILED: {"runtime"}, CANCELLED: {"user","runtime"}}
# core/kernel_task.py:733-736
async def transition_task(self, task_id, target, reason, *, authority: str = "runtime", ...)
```

`SUCCEEDED` 要求的 authority 是 `"runtime"`，而 `transition_task` 的默认值就是 `"runtime"`
⇒ **闸 2 不需要任何改动。**

**S1 = 三件事，全部精确：**

1. **关闸 3**：把 `dependencies.final_acceptance_policy` 置 `None`（即不再注册该适配器）。
   这是关掉"必须有 `verify.completed{passed}`"的那个 `is not None` 条件。
2. **改终态路径**（两处）：
   * `sdk/runtime.py:1321-1337`：`AgentTurnResult` 分支改为
     `VERIFYING`（记账）→ `FINALIZING` → `SUCCEEDED`，**不调用**
     `verify_task_acceptance` / `finalize_acceptance`。
   * `cli.py:588-645`：同样合一（`_finalize_standalone_agent_result`）。
3. **闸 1 不动**：`LEGAL_TRANSITIONS` 保持原样（走 `EXECUTING → VERIFYING → FINALIZING → SUCCEEDED`
   这条已有的合法路径）。

**S1 的验收**：一次"猜错路径"的分析任务**正常结束且为 `SUCCEEDED`**，不再挂起、不再
`NEEDS_REVIEW`；`verify.completed` 事件不再产生；全量绿。

### S1 实测修订（落地后回写，覆盖上文三处不准确）

**1. 生产调用点是三个，不是两个。** 漏掉的是**交互式循环** `cli.py` 里
`interactive verifier started` 那段（原 `:2127-2161`，在 `_chat` 回合结束分支内）。
它与 `_finalize_standalone_agent_result`、`sdk/runtime.py` 是同一决策的三份拷贝。
三个调用点现在全部收敛到一个新 API：`Kernel.complete_task(task_id, reason)`
（`core/kernel.py`，紧邻 `finalize_acceptance`），内部就是
`VERIFYING → FINALIZING → SUCCEEDED` 三跳，**不读任何裁决**。

**2. "证明层仍然计算"是错的。** `final_acceptance_policy` 不只是闸 3 的开关，它同时是
**准则的构造者**：`kernel.py:3138` `_evaluate_final_acceptance` 在 `policy is None` 时
直接 `return None`，于是 `final-evidence-integrity` 这一类准则**从 `verify_task_acceptance`
的输出里消失**。所以 S1 不是"层变惰性"，而是"层的产出被削掉一半"。这没有害处（S1 之后
没有任何生产路径消费裁决），但必须知道——否则会以为 S1 是纯行为改动。

**3. 删除面比预估小。** 初测 15 条失败里 **14 条只是注入 kwarg 被删掉的 `TypeError`**
（`final_acceptance_policy_adapter` 不再存在），并非语义冲突。逐条处置：

| 测试 | 处置 | 依据 |
|---|---|---|
| `test_final_acceptance.py`（整文件 9 条） | **删除** | 全部断言 `final-evidence-integrity` 等准则；准则由被删的 policy 构造，已无主语 |
| `test_clarification.py::test_legacy_interaction_question_does_not_block_verification` | **删除** | 整条测试只为验证"老事件不阻塞验证" |
| `test_clarification.py::test_answered_interaction_is_not_final_evidence` | **改写** 为 `test_answered_interaction_binds_no_evidence_question` | 保留交互语义断言（回答不产生 `evidence.question_bound`），去掉裁决断言 |
| `test_terminal_state_mapping.py::test_entry_points_delegate_to_the_shared_finalizer` | **改写** 为 `..._shared_completion` | 反向守卫：入口必须调 `complete_task`，且 `cli.py`/`runtime.py` **不得**再出现 `verify_task_acceptance(` |
| `test_sdk_runtime.py::test_non_legacy_mode_reports_verification_without_finalizing` | **改写** 为 `test_readiness_mode_does_not_gate_the_terminal_path` | 旧断言"非 LEGACY 模式不终结"已被推翻；新模式只调 nudge，不调终态 |
| `test_terminal_state_mapping.py::FinalizeAcceptanceTest`（6 条） | **保留到批 3** | `finalize_acceptance`/`terminal_state_for` 在 S1 后仍然存在，其规格随批 3 一起删 |

结论：**S1 与"删规格"不是同一个原子动作**——只有 1 个测试文件整体删除，其余是同批改写。
上文"S1 不能独立变绿"的说法作废。

**4. ✅ S1 的验收条件（修复后达成，实现在 S2a）。** 落地 S1 后重跑事故复现测试
`tests/test_completion_readiness_modes.py::test_a_blocked_question_is_unclosable_by_construction`
**仍然通过**——LEGACY_GATE 下"猜错路径"的任务**依然停在 `AWAITING_USER`**。

原因：那次事故的堵点**不在终态决策**，而在**回合循环内部**。`turn_phases.py` 的
`incomplete_recovery_boundary` 在模型停止调用工具时，只要还有 **required gap**，就直接
`_suspend_incomplete_recoverable` → 根本走不到 `AgentTurnResult`，于是也走不到
`complete_task`。S1 删掉的是"回合结束之后"的权威；"回合能不能结束"由 S2 的范围决定。

**S2a 已把这个堵点删掉**（见 §1 S2a）。现在同一个测试断言的是修复后的契约：
`AgentTurnResult` + 经生产入口闭合为 `SUCCEEDED`（exit code 0）+ 那条不可闭合的 gap
**仍然被如实记录**、blocker disclosure **仍然只发一次**。

---

## 1. 删除顺序（每步独立可回退、每步全量绿）

### S1 —— 切断权威（行为立即达成）✅ 已落地
* `sdk/runtime.py`、`cli.py`（**两处**：`_finalize_standalone_agent_result` 与交互式
  回合结束分支）三处全部并入新 API `Kernel.complete_task`。
* `bootstrap/composition.py`：注销 `FinalAcceptancePolicyPort`
  （删 `final_acceptance_policy_adapter` 形参、两处默认注册、
  `registry.all(FinalAcceptancePolicyPort)` 校验；`final_acceptance_policy=None`）。
* 效果：`verify_task_acceptance` / `finalize_acceptance` 不再被生产调用；
  `verify.completed` 不再由生产产生。
* **注意**：这不是"层变惰性"——policy 同时构造准则，置 `None` 会顺带削掉
  `final-evidence-integrity` 等准则的产出（详见 §0 实测修订第 2 条）。
* 验收：**已达成**（在 S2a 之后，见下）。一次"猜错路径"的任务经**生产入口**
  `cli._finalize_standalone_agent_result` 正常结束且为 `SUCCEEDED`，exit code 0；
  真实台账那种分析任务不再挂起。

### S2a —— 回合内的 suspension 出口（真正修复事故的那一步）✅ 已落地
* `turn_phases.py`：删掉"有 required gap 且停止调工具 ⇒ `_suspend_incomplete_recoverable`"
  那段（27 行）。**没删 nudge**：停止门仍然最多 nudge 一次（`blocker_disclosure_requested`），
  之后**让回合结束**，把模型自己那句话当作答案。判据改成
  `not incomplete_recovery_boundary and readiness.action is not COMPLETE`。
* `_commit_model_response_checkpoint(final=...)`：由 `final_response and not boundary`
  改为 `final_response or boundary`——既然回合结束，检查点就该标 final。
* **没有**删除 `_suspend_incomplete_recoverable` 本身：`unresolved_tool_batch`
  （`turn_phases.py` 工具批恢复失败那条）仍然用它，也就是 NEEDS_REVIEW / 人工复核
  这条链**依然活着**。
* 效果：Hermes/Codex 语义成立——停止门只做**有界 nudge**，永不把回合扣在
  `AWAITING_USER`。三种 readiness 模式**行为一致**。
* 实测删除面（8 条失败，全部是被删层的规格，不是连带损伤）：

| 测试 | 处置 | 依据 |
|---|---|---|
| `test_completion_readiness_modes::test_a_blocked_question_is_unclosable_by_construction` | **改写**为 `test_a_blocked_question_no_longer_strands_the_turn` | 原测试自称"NOT yet fixed"；现在断言经**生产入口**闭合为 `SUCCEEDED` + gap 仍被如实记录 + disclosure 仍只发一次 |
| `test_completion_readiness_modes::test_the_non_gating_modes_return_instead_of_stranding` | **改写**为 `test_every_mode_closes_the_same_way` | 三种模式现在同一条闭合路径 |
| `test_completion_readiness::test_unmet_acceptance_criterion_never_completes_agent_turn` | **改写**为 `..._ends_turn_honestly` | 名字就是旧不变量；新契约是"nudge 一次 → 回合结束" |
| `test_completion_readiness::test_continuation_at_a_spent_budget_regains_capacity` | **删除** | 整条以"在耗尽预算处 suspension 再 resume"为前提；`capacity_replenished` 仍由 `test_continue_is_bounded_then_reports_persistent_blocker` 覆盖 |
| `test_completion_stall_terminal` 的 `_suspend_once`（牵连 5 条） | **改夹子** | 不再用 readiness 造 suspension，改用**仍然活着**的 `unresolved_tool_batch` 夹子（`_recovery_app()`）；NEEDS_REVIEW 生命周期与人工复核 accept/return/cancel 的**主题一条没丢** |

* 注意：`_suspend_once` 现在搭在 `unresolved_tool_batch` 上。**那一批删除时（S2b/S4），
  这 5 条要一起改成直接构造 NEEDS_REVIEW 状态**，不能留着悬空。
* 已知遗留（未在本步处理）：`readiness.action` 里 `REPORT_BLOCKED` 已无人产出；
  `PhaseAction.BREAK`、`REPORT_INCOMPLETE_RECOVERABLE` 同属死值。

### S2b —— 停止构造 readiness gap ✅ 已落地
* `kernel.py::_completion_readiness_gaps` —— **四个调用点共享的唯一缝隙**
  （`kernel._evaluate_completion_readiness`、`turn_phases` ×2、`kernel_task` ×1）——
  整体改为 `return ()`：删掉 315 行 gap 构造（准则/证据问题/未决副作用/变更后校验
  四条来源全在内）。
* 效果：readiness 策略永远看到空 gap 集合 ⇒ 永远 `COMPLETE` ⇒ **nudge 与 disclosure
  都不再产生**，模型一停就结束回合。这就是 Hermes/Codex 的语义。
* 关闭 `completion.invariant_violated`（INV-3 demotion）诊断：它随 gap 一起消失。

**实测删除面（16 条失败，全部是"预期有 gap，结果没有"）**：

| 类别 | 数量 | 处置 |
|---|---|---|
| 纯 gap 生产规格 | 13 | **删除**：`test_completion_claim_gate` ×2、`test_completion_readiness` ×8、`test_goal_alignment` ×1、`test_post_mutation_applicability` ×1、`test_proof_closure` ×1 |
| 以 gap 为触发、但主体另有其事 | 3 | **改写**：事故复现测试去掉 gap/disclosure 断言、保留"经生产入口闭合为 SUCCEEDED"；`unmet_acceptance_criterion` 同上；`reapplied_change` 保留 `_has_active_workspace_mutation`，去掉 gap 断言 |

* 保留未动：`_task_outcome_completion_gaps`（显式 outcome 交付声明，另一条路径）、
  `_unfinished_plan_instruction`（计划提醒，不是门）。
* 遗留债（下一步）：
  1. `_completion_readiness_gaps` 现在是个 8 行的空壳，其背后的 gap 机制
     （`CompletionGap`、INV-3 demotion、`_proof_coverage_gaps`、
     `_task_outcome_completion_gaps`）需要按附录 A 分批删除。
  2. **renewal 机制**（`turn_phases` 的 `DiminishingModelCallBudgetPolicy` 分支）
     原本由 gap 触发，现在已无人使用 → 可与 `DiminishingModelCallBudgetPolicy` 同批删。
  3. 被删测试留下的孤儿夹具/导入（如 `CriterionBlockedModel`、
     `_require_command_criterion` 的部分调用者）需在下一批清理。

### S2c —— 删掉已死的产出者 ✅ 已落地（部分）
* 用零引用扫描（对 `kernel.py` 全部私有方法 × 全仓引用数）确认**唯一**已死的方法是
  `_proof_coverage_gaps`（55 行，含一次 `ProofPlanCompiler.compile` +
  `derive_proof_events`）→ 删除。
* 四个调用点不再走空壳：`kernel._evaluate_completion_readiness`、`turn_phases` ×2、
  `kernel_task`（`return_for_revision`）改为直接 `()`。
* **保留** `_completion_readiness_gaps` 空壳：另有 6 条规格直接调它断言"没有 gap"
  （claim_gate / readiness / post_mutation / proof_closure）。它们与空壳同批删。

**S2c 未做（下一批，必须做）**：`_publish_proof_diagnostics` 仍活在
`_evaluate_completion_readiness` 末尾——**每一次 readiness 求值仍会跑
`ProofPlanCompiler.compile` + `derive_proof_events` + 落 `proof.*` 事件**。
证明层现在"只算不判"，但**还在算**，这正是"复杂且低效率"的来源。
删它会波及 `tests/test_proof_closure.py`（2 处直接调 `_emit_proof_diagnostics`）。

**S2c 未做（需要你定，不是技术问题）**：**renewal 机制已成孤儿**
（触发条件是 gap 非空，现在恒空）。它的删除面是：`core/budget_renewal.py`、
`turn_phases` 的 renewal 分支、4 个依赖旋钮 `model_call_renewal_*`、
`bootstrap/exploration_configuration.py` 的配置与 3 个环境变量
`TSM_AGT_AGENT_MODEL_CALL_RENEWAL_*`、`tests/test_budget_renewal.py`。
**代价**：删掉后单回合模型调用上限从"最高 58（基础 40 + 续借）"降回 **40**，
而 Hermes 是 **90**。要么先按 Hermes 把基础值提到 ~90 再删，要么保留 renewal。
`model_budget_renewal_count` / `model_budget_total_granted` 两个 checkpoint 字段
**必须保留**（旧台账里有，删了旧任务重放会炸）。

**S2c 收尾 ✅ 已落地**：删掉证明诊断流水线。
* `_evaluate_completion_readiness` 末尾的 `_publish_proof_diagnostics` 调用移除；
  删掉 `_publish_proof_diagnostics` + `_emit_proof_diagnostics`（46 行）。
* 四个随之失去引用的 import 一起删：`ProofPlanCompiler`、`ProofCompilation`、
  `derive_proof_events`、`replay_proof_projection`、`coverage_gaps`、
  `MAX_AUTHORED_ACCEPTANCE_CRITERIA`。
* 删除 `tests/test_proof_closure.py::KernelProofEmissionTest`（79 行，2 条）。

**验证（这是本步的要害）**：全仓扫描 `ProofPlanCompiler` / `derive_proof_events` /
`replay_proof_projection` / `coverage_gaps(` 的**调用点**，只剩 `core/__init__.py`
的 re-export，**零生产调用**。也就是说证明层现在四件事全不做：
不参与决定（S1）、不拦回合（S2a）、不产出 gap（S2b）、**也不算不记账（S2c）**。

* 仍待处理：`proof_enforcement_mode` / `ProofEnforcementMode` 已成死配置、
  `_completion_readiness_gaps` 空壳、renewal（见下）。

### B1 —— 预算对齐 Hermes ✅ 已落地
* `default_max_model_calls` 40 → **90**（Hermes 父回合 90 次迭代；Codex 无上限、靠
  compaction）。三处默认值同步：`core/kernel.py`、`bootstrap/composition.py`、
  `bootstrap/exploration_configuration.py`。
* `model_call_renewal_absolute_limit` 60 → **120**：校验要求它 ≥
  `agent_max_model_calls`，不改它连配置都构建不出来（39 条测试因此变红）。
* 规格更新：`test_exploration_budget_policy` 两条断言旧的 40/60 → 90/120
  （含 env 覆盖用的 70 → 150）。
* 单回合上限从"基础 40 + 续借最高 18 = 58"变为 **90**；续借仍是孤儿（B2 删）。

### B2 —— 删除 renewal（已成孤儿）✅ 已落地
* 删除：`core/budget_renewal.py`、`tests/test_budget_renewal.py`、
  `turn_phases` 的 renewal 分支（94 行）+ import + `renewal_events` 接线、
  `kernel.py` 的 import + 4 个依赖旋钮、`composition.py` 的 14 行旋钮读取、
  `exploration_configuration.py` 的 6 处（字段/校验/serialization/env 映射/
  increments 装载/返回 kwargs）。
* 规格：`test_exploration_budget_policy` 去掉 renewal 断言与
  `test_invalid_renewal_sequence_fails_at_startup`。
* **保留** checkpoint 字段 `model_budget_renewal_count` /
  `model_budget_total_granted`（`agent_loop.py`、`kernel_checkpoint.py`、
  `kernel_model.py`、`turn_tool_batch.py`）—— 旧台账里有，删了旧任务重放会炸。
* 教训（已计入流程）：这步我用"内容锚点裁剪"一次改了 6 个文件，其中一刀
  `**values, model_call_renewal_increments=renewal_increments,` **把 `**values` 一起删了**，
  导致所有 env/.env 预算值静默失效（4 条测试报"ValueError not raised"，很具迷惑性）。
  锚点裁剪必须连同**同一行的其他内容**一起看，不能只匹配"要删的那部分"。

### S2 —— 停止产出
* 不再编译 obligation（`ProofPlanCompiler`）、不再构造 gap（`_completion_gaps`）、
  不再注入 readiness 诊断与 nudge（`policy`、`turn_phases` 的相关分支）。
* **保留** `from_data` / `replay_*` 的读取路径，否则旧事件读不了。

### S3 —— 停止持久化
* 新 Task 不再写 `acceptance_criteria`（含 `TaskSpecSnapshot.initial` 里那条
  `workspace-integrity`，`task_spec.py:1161-1173`）。
* **必须**让 `TaskSpecProjector` 继续容忍旧事件里的该字段（否则重放旧任务会炸）。

### S4 —— 删模块（分批提交）
15 个直接相关模块：
```
core/artifact_graph.py  core/evidence_coverage.py  core/evidence_question.py
core/proof_obligation.py  core/proof_projection.py  core/semantic_review.py
core/turn_finalizer.py  core/turn_outcome.py  core/verification.py
core/verification_command.py  core/verification_criteria.py
ports/completion_readiness.py  ports/evidence_delta.py
ports/evidence_guided_exploration.py  ports/evidence_level.py
```
外加其适配器（`adapters/rule_based_completion_readiness`、
`adapters/rule_based_evidence_level`、`adapters/structured_evidence` 等）与 `core/__init__.py`
的导出。**每一批之后跑全量。**

### S5 —— 删词汇
`AcceptanceStatus`、`CompletionReadiness*`、`ProofObligation`、`EvidenceQuestion`、
`terminal_state_for`、`ToolRecoveryKind`（收敛到 Codex 式 2 值）、
以及 `AGENTS.md` 里的 INV-3 / INV-13 / INV-16（撤下时写明原因与日期）。

### S6 —— 处理既有 ledger（**不可跳过**）
* `acceptance_criteria` 出现在 **668 / 687** 个任务的持久化事件里（2,344 个事件）。
* 要求：**旧任务仍能重放**（读路径容忍未知/多余字段），且新任务不再产出该字段。
* 如需彻底清洗，必须是一次显式的数据迁移，而不是"读的时候当作不存在"。

---

## 2. 风险清单（按大小）

| 风险 | 量级 | 处置 |
|---|---|---|
| 旧 ledger 重放 | 668/687 任务带该字段 | S3/S6：读路径保持容忍 |
| 测试面 | `test_proof_closure.py`（143 条）+ `test_completion_readiness*.py` + `test_terminal_state_mapping.py` + `test_completion_stall_terminal.py` + `test_evidence_questions.py` 等 | 每批同删；**逐条写明是"规格改写"还是"随层删除"** |
| 人工复核 API | `NEEDS_REVIEW` 的 accept / cancel / return-for-revision | 若选 §0-c 则一并删除，并处理其 CLI/Web 入口 |
| 文档同步 | `tsm-agt-完成判定机制.md`、`tsm-agt-完成度*`、`tsm-agt-对齐Hermes设计方案.md` | 最后一并改，注明本决定推翻了前者 |
| 回退成本 | — | 全部按步提交，任一步 `git checkout HEAD -- <paths>` 可退 |

---

## 3. 每步的纪律（本会话已付学费）

1. **禁用多行字符串拼接**：按行号切片 + 逐行内容断言（我曾因此误删 753 行）。
2. 改完先 `ast.parse`，再跑聚焦测试，最后跑全量；**注意超时**（曾挂死一次，基线 27s）。
3. 先读相关测试，判断"仍成立 / fixture 要重指 / 才是规格改写"，三种在提交信息里分列。
4. 一步一提交，绝不留半套。
5. **不在余量不足时开始批量删除。** 15 个模块的删除需要一整段连续上下文。

---

## 4. 我为什么此刻只写方案不动手

本轮我已经用完上下文预算，而 S4 是 15 个模块的批量删除——正是本会话三次翻车（一次挂死、
一次误删 753 行、一次掀掉 INV-4 链）的同类动作。**方案已落盘，下一段从 S1 开始。**

---

# 附录 A —— 符号级消费者清单（补齐 S4/S5/S6）

方法：AST 遍历 `src/` + `tests/` 全部 `.py`，解析相对导入、绝对导入（`tsm_agt.core.X`）
与门面导入（`core/__init__.py` / `ports/__init__.py` 的 `from .X import (...)` 块），
外加 `kernel.dependencies.<field>` / `Kernel.<method>` / `recovery_kind` 的 grep。
行号精确，**不按名字相似推断**。

## A.1 七个删除批次（依赖在前、基础在后；每批后 `import tsm_agt` 可用、测试可收集）

| 批 | 删除 | 必须同批编辑 | 必须同批处理的测试 |
|---|---|---|---|
| **1** | `core/turn_finalizer.py`、`core/turn_outcome.py` | 无（`core/__init__` 不导出它们） | 删 `test_proof_closure.py` 中 TurnOutcome/TurnFinalizer 两个类 |
| **2** | `core/verification_command.py`、`core/verification_criteria.py`、`core/evidence_question.py` | `core/__init__:157-161,464-469`；`kernel.py:292,373,376,857,1355,1716-3139,2752,2815`；`kernel_tool.py:101,953`；`task_spec.py:22,858`；`kernel_checkpoint:48`；`kernel_input:52,340-425`；`kernel_memory:42,72`；`kernel_steering:32,248-276`；`session_resources:16`；`turn_tool_batch:33`；`working_memory:19,301-326` | 删 `test_evidence_questions.py`；改 `test_working_memory.py`、`test_tool_scope_consistency.py:20,139` |
| **3** | `core/verification.py` | `core/__init__:153-156,458-462`；`kernel.py:369,761-3433,2646,2664`；`kernel_task.py:72,770,903` | 删 `test_terminal_state_mapping.py`；改 **11 个** import `AcceptanceStatus` 的测试（含 `test_clarification`、`test_coding_agent_e2e`、`test_completion_claim_gate`、`test_engineering_loop_over_http`、`test_final_acceptance`、`test_goal_alignment`、`test_goal_command_verification`、`test_post_mutation_applicability`、`test_production_journeys`、`test_real_provider_journey`、`test_task_spec`）+ `test_rubric_judge`、`test_task_spec_criteria_validation` |
| **4** | `core/artifact_graph.py`、`proof_obligation.py`、`evidence_coverage.py`、`proof_projection.py`、`semantic_review.py` | `core/__init__:162-190,196-243`；`kernel.py:301,305-311,428,650,699,706,1073,2264,2413-2515,2972,3464`；**`task_spec.py:15-20 与 ProofPlanCompiler ~875-1110`** | 删 `test_proof_closure.py`（若批 1 未删） |
| **5** | `ports/evidence_delta.py`、`ports/evidence_guided_exploration.py`（**必须同批**：后者 import 前者） | `ports/__init__:103-110,146-160,304-309,342-356`；`ports/exploration_budget_policy.py:11,141`（**改公共 API**）；6 个适配器；`exploration_coordinator:10,99-146`；`kernel.py:21,432-435,1051-1059,5195-5288`；`kernel_policy:38,259-323`；`kernel_projection:35,146-174`；`kernel_session:1849`；`turn_tool_batch:54`；`bootstrap/composition.py:102,348-355,444-451,583-590,634-639,735-745` | 删 `test_evidence_delta.py`、`test_evidence_guided_exploration.py`；改 8 个（`test_composition:26`、`test_exploration_budget_policy:22`、`test_investigation_status:77`、`test_agent_progress_projection:207`、`test_semantic_action_kernel:189`、`test_stop_or_pivot_policy:179`、`test_tool_scope_consistency:102`、`test_post_mutation_applicability:103`） |
| **6** | `ports/completion_readiness.py`、`ports/evidence_level.py` | `ports/__init__:128-134,161-165,229-235,362-365`；两个适配器；`kernel.py:21,416,422-424,438,1069-1074,2308-2560,4161-4318,5091-5112`；**`turn_phases.py:37,137-160,475,743-1045`（就绪门整段）**；`cli.py:359,593-637,2179-2203`；`sdk/runtime.py:32,1323-1337`；`composition.py:102,236-258,371-461,551-564(env),625-757` | 删 `test_completion_readiness.py`、`test_completion_readiness_exhausted.py`、`test_completion_readiness_modes.py`、`test_completion_stall_terminal.py`、`test_evidence_level.py`、`test_cli_standalone_finalization.py`；改 `test_sdk_runtime.py:12,260` |
| **7** | 收缩 `ToolRecoveryKind`（`ports/tool.py:155-166,511,524,534-542,557,585`） | `ports/__init__:189,402`；`kernel.py:118,1757,1809,2206,4161`；`kernel_tool.py:68,475-480,561,1485-1566`；`turn_tool_batch:55,737`；`context:703`；`execution_facts:74-79,123,214-266,381`；`flow:741-743`；`kernel_checkpoint:699`；`turn_phases:297,750`；`adapters/builtin/workspace_tools.py:12,470,485` | 删 `test_tool_recovery_contract.py`；改 `test_core_apply_patch.py:12,121,155,275` |

## A.2 行为调用点（必须**改接线**，不是删掉）

核心内：
`kernel.py` 的 `_completion_readiness_gaps`(def 1961)、`_evaluate_completion_readiness`(2308)、
`_publish_proof_diagnostics`(2469)、`_finalize_needs_review`(1015/called 4219,5128)、
`_apply_inv3_demotion`(706)、`_inject_agent_decides_completion_diagnostics`(2517)、
`verify_task_acceptance`(2664)、`finalize_acceptance`(2632)、
`1051-1059`（evidence_relation 接线）、`2206`（recovery 决策）、`2972`（rubric 判官）、`2752`（种子判据）。

适配器里**做决定的实现**：
`rule_based_completion_readiness/policy.py:85-160`、
`structured_evidence/evaluator.py:58-121`、
`evidence_guided/strategies.py:60-236`、
`rule_based_evidence_level/evaluator.py:36-75`、
`rule_based_investigation_flow/projector.py:91`、`rule_based_investigation_status/projector.py:49`、
`rule_based_exploration_budget/policy.py:102,180`、
`builtin/workspace_tools.py:470,485`（设置 `RETRY_AFTER_STATE_CHANGE` 的生产端）。

其他决策点：`exploration_coordinator.py:99-146`、`kernel_policy.py:259-323`、
`kernel_projection.py:146-174`、`kernel_session.py:1849`、`composition.py:551-564`（env）。

## A.3 零消费符号（删起来最便宜，且说明这层有死区）

`derive_edge_id`、`requirement_digest`、`version_free_selector`、`derive_obligation_id`、
`ProofProjection`、`SemanticReviewOutcome`、`classify_transition_reason`、
`PROOF_ENFORCEMENT_CHANGED`、`MIN_ENFORCEMENT_WINDOW`、`MIN_AUTO_ATTRIBUTION_RATE`、
`MAX_ADVISORY_OBLIGATION_RATE`、`EnforcementReadiness`、`artifact_id_for_locator`、
`PROOF_OBLIGATION_ADVISORY/REJECTED`、`ARTIFACT_RELATION_OBSERVED`、5 个 `*_version` 助手。

**`core/turn_finalizer.py` 零 src 消费者（只有测试）**；`web/` **零消费者**
（`web/app.py:1567` 的 `verification_status` 是无关的投影字符串）。

## A.4 未核实

* **删后未执行导入验证**（只读分析，未改文件）；批次安全性来自依赖图，不是运行结果。
  风险最高的两处编辑：`task_spec.py` 的编译器移除、`turn_phases.py` 的就绪门整段。
* `ports/exploration_budget_policy` 的 probe/state 内嵌 `EvidenceDelta`，删它会**改变该
  port 的公共 API** 并波及 `kernel_policy` / `rule_based_exploration_budget`（字段级未穷举）。

---

# 附录 B —— S1 的实测波及（原评估"独立可验证"是错的）

S1（`final_acceptance_policy=None` + SDK/CLI 改为不调 verify/finalize）实测：
**全量 9 条失败，全部是"被切掉的那个权威"的规格**：
`test_final_acceptance.py` ×5（含"BLOCKED 不得报成功"、"kernel 不能绕过验收"）、
`test_clarification.py` ×2（"交互回答不是最终证据"、"问题不该阻塞验证"）、
`test_terminal_state_mapping.py` ×1（"入口必须委派共享 finalizer"）、
`test_sdk_runtime.py` ×1（"非 legacy 模式只报告验证"）。

**结论：S1 不能独立变绿，它与删规格是同一个原子动作。** 所以正确的第一步是
「S1 + 上述 9 条规格的处置」，并且**逐条写明是随层删除还是规格改写**。
（本轮已回退 S1，工作区干净、全量 1437 passed。）
