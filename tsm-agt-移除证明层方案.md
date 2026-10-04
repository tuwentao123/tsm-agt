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

### S3 —— 停止持久化 acceptance_criteria（选 A）✅ 已落地
**先查清再动**：`workspace-integrity` 是 `TaskSpecSnapshot.initial()` 给**每个**新任务
硬塞的准则。它的验证逻辑只是拿磁盘哈希和 mutation journal 比对，**空 journal 必然通过**
（`kernel.py:2398-2400` 明写 "empty journal trivially satisfies it"）；但同一 Runtime 的
`ProofPlanCompiler` 把它编译成 `EFFECT_OBSERVED` + **`required=True`** +
`required_capabilities=(MUTATE_ARTIFACT,)`（`task_spec.py:1024-1037`）—— 一条"没改动就该
通过"的检查变成了"必须有改动"的硬性义务。**这就是 task-8d1ec232 的死因。**
`kernel.py:585-596` 自己还写着"bare workspace_integrity does NOT imply a required change"，
与编译器直接矛盾。Hermes / Codex 都没有这种任务级完整性准则。

**关键取证（决定 A 是否安全）**：
* `TaskSpecSnapshot` 过去要求 **1-30** 条准则（`task_spec.py:1137`），所以空集建不出任务
  （初测 523 条失败全部是这个 `ValueError`，**不是**功能依赖）。
* `TaskSpecProposal` 只有**上界**（`task_spec.py:419`），且**要求 1-30 个 outcome**，不要求准则。
* 但凡是"把 planner 提案变成 TaskSpec"的路径，**都显式传
  `require_acceptance_criteria=True`**：`kernel_task.py:303`、
  `model_task_spec_planner/planner.py:170,184`。`TaskSpecSnapshot.from_proposal` 另外还有
  一条"proposal 无准则且未给 fallback 就拒绝"（`task_spec.py:1190`）。
  ⇒ **放宽快照下界不会产生"零准则的已规划任务"**，planner 的 ≥1 保证独立存在。

**改动**：
1. `task_spec.py:1137` 下界 1 → 0（消息改为 "supports at most 30"）。
2. `initial()` 不再写准则，返回空集。
3. 规格处置（3 条）：
   * 删 `test_task_spec.py::test_workspace_integrity_cannot_hide_protocol_as_final_answer`
     （主语已不存在）。
   * `test_completion_readiness`、`test_agent_loop` 各一处把 fallback
     `current.acceptance_criteria`（现在是空集）换成自己声明的准则。
* 实测 `TaskSpecSnapshot.initial(...).acceptance_criteria == ()`。

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

## A.1b 批 2 重扫（HEAD 实测，取代 A.1 的行号）

附录 A 行号生成于本会话早期；S1/S2b/S2c/S3 之后已全部偏移。开工前重跑 AST+import 扫描，
批 2 三个模块的**真实**消费者清单（file:line）：

**`verification_criteria.py`** —— 1 个符号 `seed_acceptance_criteria`，1 个 import 点：
`kernel.py:368`。机械可删。

**`verification_command.py`** —— 2 个符号，**5 个真实调用点**：
`kernel.py:852`(is_verification_request)、`kernel.py:1350`(is_verification_command)、
`kernel.py:2414`(is_verification_request)、`kernel_tool.py:953`、`task_spec.py:858`。

**`evidence_question.py`** —— 6 个符号，**10 个 import 点**（最重）：
`core/__init__:157`（门面）、`kernel.py:371`、`kernel_checkpoint:48`、`kernel_input:52`、
`kernel_memory:42`、`kernel_steering:32`、`session_resources:16`、`turn_tool_batch:33`、
`working_memory:19`、`tests/test_working_memory.py:18`。

### ⚠️ 发现：A.1 的分批顺序在批 2 处是错的

`verification_command` 那 5 个调用点全部在回答**"这条命令算不算真正的测试/构建验证"**：
`task_spec.py:858` 校验 planner 声明的 `POST_MUTATION_COMMAND` 命令是否合格，
`kernel.py:2414` / `kernel_tool.py:953` 判断一次调用是否算"验证请求"，
`kernel.py:852/1350` 判断它有没有解除那条义务。

这些都是**证明层的语义**，而且它们**只有**在 `POST_MUTATION_COMMAND` 准则、
`AcceptanceStatus`、`verify_task_acceptance` 还存在时才有意义 —— 也就是**批 3 的范围**。
生产路径已经不调用 `verify_task_acceptance`（S1），所以这些判据现在是装饰性的，
但**不能独立于批 3 删除**：先删它们会让 `task_spec.py` 放弃对声明命令的校验，
留下一个"planner 可以声明任意命令当验证"的中间状态。

再查一层，`verification_criteria` 也一样：`seed_acceptance_criteria` 的**唯一**调用点是
`kernel.py:2351`，而它在 `verify_task_acceptance`（def 于 `kernel.py:2263`）里面。
删掉它就要先拆 `verify_task_acceptance` —— 同样是**批 3** 的范围。

### 结论：批 2 实际只剩 `evidence_question.py`

| 模块 | 归属 | 依据 |
|---|---|---|
| `verification_criteria.py` | **并入批 3** | 唯一调用者在 `verify_task_acceptance` 内 |
| `verification_command.py` | **并入批 3** | 5 个调用点全在 POST_MUTATION_COMMAND / 验证语义上 |
| `evidence_question.py` | **批 2（保留）** | 10 个 import 点，多为读投影，可独立改接线 |

也就是说 A.1 把这三个模块放在同一批是**按名字相近**分的，不是按依赖分的。
下一批应当是"**批 2 = 只删 `evidence_question.py`**"，或者干脆按依赖重排：
先做批 3（`verification.py` + 两个 verification_*.py + `POST_MUTATION_COMMAND`）,
再回头收 `evidence_question.py`。

## A.1c 批 3 重扫（HEAD 实测）—— 这是最大的一批，必须再切片

**源码侧**（比想象小）：
* `core/__init__.py:153` 门面导出（AcceptanceResult / AcceptanceStatus / Evidence /
  TaskVerificationResult / TERMINAL_STATE_BY_VERDICT / terminal_state_for）
* `kernel.py:364`、`kernel_task.py:72`
* `verification_command`：`kernel.py:291`、`kernel_tool.py:101`、`task_spec.py:22`
* `verification_criteria`：`kernel.py:368`

**测试侧**（附录 A 的清单已过时 —— `test_final_acceptance`、`test_clarification`
在 S1 就删了，别再找）。真实清单是 **11 个文件** import `AcceptanceStatus`：
`test_coding_agent_e2e:19`、`test_completion_claim_gate:19`、`test_goal_alignment:16`、
`test_goal_command_verification:11`、`test_post_mutation_applicability:20`、
`test_production_journeys:25`、`test_proof_closure:30`、`test_real_provider_journey:17`、
`test_task_spec:11`、`test_terminal_state_mapping:16`（+ `test_rubric_judge`、
`test_task_spec_criteria_validation` 按附录 A 亦在此列）。

**真正的体量在 `kernel.py` 里 —— 18 个方法、1176 行**：

| 行数 | 位置 | 方法 |
|---|---|---|
| 418 | 2263 | `verify_task_acceptance` |
| 106 | 2717 | `_evaluate_final_acceptance` |
| 81 | 2961 | `_verify_answer_evidence_sufficiency` |
| 78 | 2825 | `_verify_goal_command_outcome` |
| 78 | 1319 | `_demote_unresolvable_criteria` |
| 60 | 3142 | `_verify_task_spec_reference` |
| 54 | 2905 | `_verify_answer_completeness` |
| 52 | 833 | `_post_mutation_verification` |
| 45 | 4884 | `_dispose_evidence_question` |
| 45 | 3043 | `_judge_rubric_criterion` |
| 37 | 4846 | `_observe_evidence_question` |
| 31 | 2231 | `finalize_acceptance` |
| +6 个小的 | | `_post_mutation_verification_observed/description` ×2、`_bind_evidence_question`、`_assistant_conclusion`、`get_evidence_questions`、`_task_spec_requires_command_verification` |

### A.1d 批 3b 重扫（HEAD 实测）—— 比附录 A 干净得多

逐个符号数调用点，结果：

| 符号 | 生产调用者 | 结论 |
|---|---|---|
| `verify_task_acceptance`（418 行） | **0**（23 处引用全在 tests + `verification_criteria.py:1` 的 docstring） | 整段只有测试可达 |
| `_evaluate_final_acceptance`(106) | 1 —— 在 `verify_task_acceptance` 内 | 随它一起死 |
| `_verify_answer_evidence_sufficiency`(81) | 1 —— 同上 | 随它一起死 |
| `_verify_goal_command_outcome`(78) | 1 —— 同上 | 随它一起死 |
| `_verify_answer_completeness`(54) | 1 —— 同上 | 随它一起死 |
| `_judge_rubric_criterion`(45) | 1 —— 同上 | 随它一起死 |
| `seed_acceptance_criteria` | import + 调用点都在 `verify_task_acceptance` 内 | `verification_criteria.py` 可整删 |

⇒ **删掉 `verify_task_acceptance` 就一次性清掉约 780 行 + `verification_criteria.py`。**

**但有 3 个辅助方法有"外部"调用者，必须单独处理，不能跟着盲删**：

| 符号 | 外部调用者 | 处理 |
|---|---|---|
| `_demote_unresolvable_criteria`(78) | `task_spec.py:849`、`kernel_task.py:313` | 要看这两处语义 |
| `_verify_task_spec_reference`(60) | `kernel.py:1376`（`verify_task_acceptance` 之外） | 要看 1376 的上下文 |
| `_task_spec_requires_command_verification`(6) | `kernel.py:2373` | 确认是否真在 `verify_task_acceptance` 内 |

**所以 3b 的正确切法**：
* **3b-1**：删 `verify_task_acceptance` + 5 个"1 ref"辅助 + `verification_criteria.py`，
  并把 `verify` 子命令（cli）与 11 个测试文件一起处理。这是主体。
* **3b-2**：单独处理上表 3 个有外部调用者的方法（它们牵连 `task_spec.py` /
  `kernel_task.py` 的 criterion 派生，与 3c 的 `POST_MUTATION_COMMAND` 语义相邻）。

### A.1e 批 3b-1 测试处置预案（实测：逐文件数"碰验收裁决的用例 / 总数"）

方法：AST 逐文件统计每个 `test_*` 的**方法体**是否出现
`verify_task_acceptance` / `AcceptanceStatus` / `TaskVerificationResult` / `AcceptanceResult`。
下表是实测值，不是猜的。

| 测试文件 | 碰裁决/总数 | 处置 |
|---|---|---|
| `test_goal_command_verification.py` | **4/4** | **整文件删**（整个文件就是 goal-command 裁决） |
| `test_real_provider_journey.py` | **1/1** | **整文件删**（唯一用例断言 `reaches_verified_answer`） |
| `test_coding_agent_e2e.py` | 4/5 | 删 4 个 `*_verify/*_verifier_*`；**保留** `test_verification_command_is_eligible_after_workspace_effects` |
| `test_completion_claim_gate.py` | 3/21 | 删整个 `DeliveryAcceptanceTest`（3）；保留其余 18 |
| `test_evidence_level.py` | 3/6 | 删 `EvidenceLevelKernelTest`（3）；保留 3（含 `test_cli_renderer_is_explicit`） |
| `test_goal_alignment.py` | 2/6 | 删 `test_unrelated_answer_fails_alignment`、`test_judged_criterion_without_judge_cannot_pass`；保留 4 |
| `test_post_mutation_applicability.py` | 2/5 | 删 `test_verifier_records_the_criterion_as_not_applicable`、`test_change_without_verification_is_blocked_in_verifier`；保留 3 |
| `test_production_journeys.py` | 2/5 | 删 `test_analysis_journey_survives_sqlite_restart`、`test_multi_tool_journey_recovers_without_replaying_tools`；保留 3 |
| `test_rubric_judge.py` | 1/8 | 删 `RubricAcceptanceTest.test_judge_error_is_fail_closed`；保留 7 |
| `test_task_spec.py` | 5/21 | 删 5 个 verifier 用例（`test_answer_completeness_accepts_user_facing_text`、`test_pending_structured_tool_call_is_not_a_final_answer`、`test_unverified_guess_after_failed_reads_is_blocked`、`test_successful_read_does_not_trigger_unverified_answer_gate`、`test_verifier_blocks_missing_reference_and_passes_real_event`）；保留 16 |
| `test_task_spec_criteria_validation.py` | 1/8 | 删 `CriteriaDemotionTest.test_demoted_rubric_without_judge_is_blocked_not_passed`；保留 7 |

合计：**整删 2 个文件 + 删 27 个用例**（保留 76 个）。

**每个保留用例的文件都要顺手摘掉已无用的 `AcceptanceStatus` 等 import**（11 个文件都 import 了它）。

**注意 `test_proof_closure.py` 是 0/120** —— 它跟 3b 无关，那是批 4 的目标；别顺手删。

**一个必须确认的点**：`test_unverified_guess_after_failed_reads_is_blocked`
就是本次事故的**旧规格**（断言"猜错后不许成功"）。它必须随 3b-1 删除 ——
新契约由 `test_completion_readiness_modes.py::test_a_blocked_question_no_longer_strands_the_turn`
覆盖（回合经生产入口闭合为 SUCCEEDED）。删它**不是**丢覆盖，是换规格。

### A.1f 批 3c 重扫 —— **不能独立删**：4 个调用点全是活的

| 符号 | 调用点 | 性质 |
|---|---|---|
| `is_verification_command` | `kernel.py:1335`、`task_spec.py:858` | 校验/派生**作者声明的** `POST_MUTATION_COMMAND` 准则的命令是否合格 |
| `is_verification_request` | `kernel.py:837`、`kernel_tool.py:953` | 判断一次调用/执行是否构成"验证请求" |
| `_post_mutation_verification` | `kernel.py:1290`（+ 3 个测试文件） | **活的**，在准则降级/派生路径里 |
| `_post_mutation_verification_observed` / `_description` | `kernel.py:1297` / `:1295` | 同上 |
| `_has_active_workspace_mutation` | `kernel.py:603`（+ `test_post_mutation_applicability`） | **活的**，工作区变更判定的真实逻辑 |
| `TaskCriterionKind.POST_MUTATION_COMMAND` | 14 处（含 `task_spec.py:57/855/1015`、3 个测试文件） | **planner 可见的 schema 词汇** |

**结论**：`verification_command.py` 的每一个调用点都挂在
`TaskCriterionKind.POST_MUTATION_COMMAND` 这个**仍然存在、且 planner 仍然可以声明**的
准则种类上。删这个模块**等价于删掉这个准则种类**，而那属于 **S5「删词汇」**，
不是 S4 的"删模块"。

⇒ **3c 撤销，并入 S5。** 3b-2 留下的两个活方法（`_demote_unresolvable_criteria`、
`_verify_task_spec_reference`）同理，随 `plan_task_spec` 的准则词汇一起走。

**真正的下一步是批 4**：`artifact_graph` / `proof_obligation` / `evidence_coverage` /
`proof_projection` / `semantic_review` 五个模块 —— 它们在 S2c 之后已经零生产调用
（`ProofPlanCompiler` / `derive_proof_events` / `replay_proof_projection` /
`coverage_gaps` 只剩 `core/__init__` 的 re-export），所以这一批是**纯删除 + 整删
`test_proof_closure.py`（120 条）**，风险最低。

### 批 3 的建议切片（按"能否独立变绿"）

* **3a ✅ 已落地**（`95ab0ee`）：删 `Kernel.finalize_acceptance`(32 行)、
  `terminal_state_for` + `TERMINAL_STATE_BY_VERDICT`（`verification.py` 106→71 行）、
  门面导出；`test_terminal_state_mapping.py` 只保留 `_exit_code_for` 那条
  （它是**仅存**的客户端映射，不能跟着删）。实测 `terminal_state_for` 的消费者
  只有 `finalize_acceptance` 与门面+该测试，所以这是干净的一刀。
* **3a'（原提议）**：`finalize_acceptance`(31) + `terminal_state_for` +
  `TERMINAL_STATE_BY_VERDICT` + 门面导出 + 删 `test_terminal_state_mapping.py`。
  `finalize_acceptance` 自 S1 起零生产调用者。
* **3b（主体）**：`verify_task_acceptance`(418) + 6 个 `_verify_*`/`_judge_*` 辅助
  + `verification_criteria.py` + 11 个换 `AcceptanceStatus` 的测试文件。
* **3c**：`verification_command.py` + `POST_MUTATION_COMMAND` 语义（3 个 import 点）
  + `_post_mutation_verification*`(52+23+23) + `_task_spec_requires_command_verification`(6)。
* **3d**：`evidence_question.py` + 4 个 `_*_evidence_question` 方法 + 10 个 import 点。

## A.1g 批 4 边界扫描（实测）—— 有界，可以一次做完

**5 个模块的源码侧消费者只剩 3 个文件**：

| 文件 | 引用 | 处置 |
|---|---|---|
| `core/__init__.py:161,167,173,177` | 5 个模块的全部门面 re-export | 删门面条目 |
| `kernel.py:304` | `evidence_coverage` → `ESCALATED_GAP_KINDS`、`ProofEnforcementMode` | 见下 |
| `kernel.py:306` | `semantic_review` → `normalize_judgement` | **已死**（唯一调用者 `_judge_rubric_criterion` 在 3b-1 删除）→ 删 import |
| `task_spec.py:15,18` | `evidence_coverage` → `ProofPlanSnapshot`；`proof_obligation` → 5 个符号 | 随内嵌编译器一起删 |

**`task_spec.py` 内嵌 `ProofPlanCompiler` 的边界：第 928–1114 行（187 行）**，
它用的 `ProofPlanSnapshot`(917,991) 与 `proof_obligation` 5 个符号只服务于它。

**三个跨模块符号的实测存活状态**：
* `normalize_judgement`（semantic_review）—— **死的**，删除 import 即可。
* `ESCALATED_GAP_KINDS`（evidence_coverage）—— **活的**，`kernel.py:630` 的
  `_EXEMPT_GAP_KINDS` 与 `:679` 的 `_apply_inv3_demotion` 在用。但这条链服务的是
  gap 生产（S2b 已停止产出），所以**与 `_apply_inv3_demotion` 同批删**。
* `ProofEnforcementMode` —— **只剩死配置**：`kernel.py:419` 的依赖字段 + `:1053` 的
  属性，已无人读。连同字段一起删。

**测试侧只有一个消费者：`test_proof_closure.py`**（2400 行 / 120 条，import 了这 5 个
模块的全部符号）。**整文件删**。

⇒ 批 4 = 删 5 模块 + 删 `task_spec.py:928-1114` 的编译器 + 删 `_apply_inv3_demotion`/
`_EXEMPT_GAP_KINDS` + 删 `proof_enforcement_mode` 字段与属性 + 清门面 + 整删
`test_proof_closure.py`。**无语义决策**（证明层已彻底不运行），是本轮风险最低的一批。

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

## A.1h 批 4 落地实测（回写，覆盖 A.1g 的三处不准）

**结果：1244 passed / 12 skipped / 0 failed，26.78s。净 -4486 行（删 4923 / 增 437）。**

删掉的源码模块（2272 行）：`artifact_graph.py` 339、`proof_obligation.py` 272、
`evidence_coverage.py` 717、`proof_projection.py` 718、`semantic_review.py` 226。

### 修正 1：`task_spec.py` 的编译器边界不是 928–1114

A.1g 只圈了 `class ProofPlanCompiler` 本身。实测**证明专属块从第 865 行就开始**：

| 行 | 内容 |
|---|---|
| 865 | `#: The reason a criterion could not be compiled at all.` 注释头 |
| 867 | `NOT_STRUCTURALLY_KNOWABLE` |
| 875 | `_REFERENCE_COMPILATION`（用 `ObligationPredicate`） |
| 887 | `_VERIFIED_REFERENCE_CAPABILITY` |
| 894–896 | `_PRIORITY_REQUIRED_EFFECT` / `_PRIORITY_POST_MUTATION_COMMAND` / `_PRIORITY_DEFAULT` |
| 900 | `class CompilationRejection` |
| 908 | `class ProofCompilation`（字段用 `ProofPlanSnapshot`/`ObligationTruncation`/`ProofObligation`） |
| 928 | `class ProofPlanCompiler` |

`_VERIFIED_REFERENCE_CAPABILITY` 是**零引用**符号，`_REFERENCE_COMPILATION` 与三个
`_PRIORITY_*` 只被编译器读。真界 = **865–1116**，删后 `TaskSpecSnapshot` 直接接在两行
空行后。实删 252 行（含 import 段 7 行 → `EvidenceCapability` 也随之从 import 移除）。
**教训（第四次同类）：`class X` 的起点从来不是删除起点；配套常量/辅助类在它上方。**

### 修正 2：`test_proof_closure.py` **不能整删**——6 个类测的是活代码

A.1g 说"整文件删"。实测 23 个类里，**6 个零死引用或混合，钉的是 `ports` 的活契约**：

| 类 | 钉什么 | 处置 |
|---|---|---|
| `EvidenceCapabilityTest` | `ToolSpec.evidence_capabilities` 派生表 + 两条 fail-closed 断言 | 迁入新文件 |
| `CapabilityDeclarationContractTest` | 只读 composition 的 `ENUMERATE_SCOPE`/`OBSERVE_RELATION` 声明 | 迁入新文件 |
| `EngineeringCapabilityContractTest` | 工程 composition 的派生能力负空间 | 迁入新文件 |
| `ReviewerModelConfigurationTest` | `TSM_AGT_REVIEW_MODEL` 只覆盖模型名 | 并入 `test_rubric_judge.py` |
| `ReviewerProviderWiringTest` | 无覆盖时复用同一 provider 对象 | 并入 `test_rubric_judge.py` |
| `ReadinessEventPayloadTest` | 活的 `_readiness_event_payload`（原用死模块构造 gap） | 改写后并入 `test_completion_readiness_modes.py` |

新文件 `tests/test_evidence_capabilities.py` = 11 tests / 26 subtests；
`test_rubric_judge.py` 7 → 11 tests；`test_completion_readiness_modes.py` +2 tests。

`ports/tool.py` 的 `EvidenceCapability` 本体**是活的**（`core_tools.py` 4 处、
`code_intelligence_tools.py` 5 处自己声明 `ENUMERATE_SCOPE`/`OBSERVE_RELATION`），
`derive_evidence_capabilities` 也是活的（`ToolSpec.evidence_capabilities` 属性在调）。
A.1g 未涉及；只改了它们的溯源文档串（第 3 条来源已随本批消失）。

### 修正 3：`_apply_inv3_demotion` 生产侧零调用

A.1g 说它"活的"，实测**只有 `test_proof_closure.py:1848` 调它**。因此
`_JUDGED_GAP_KINDS` / `_EXEMPT_GAP_KINDS` / `_gap_has_contract_source` /
`_apply_inv3_demotion` 四者整链删除（kernel -86 行）。`_readiness_event_payload` 里
唯一用 `ESCALATED_GAP_KINDS` 的 `coverage_gap_ids` 键**删除**（无生产读者）。

### 新发现（属下一批，未在本批动）

`rubric_judge` 通路**整体孤儿化**：`composition.py:945,1131` 仍强制要求注册
`RubricJudgePort`，`kernel.py:433` 仍有 `rubric_judge` 字段与 `rubric_judge_max_attempts`，
但 **core 里已无任何调用点**。本批只清掉它的三个死 import（`JudgeVerdict`、
`RubricEvidence`、`RubricJudgement`，均只剩 import 行）与 `ports/rubric_judge.py` 里
提到已删 `normalize_judgement` 的文档串。**裁剪这条通路需要动 composition 的强制注册
断言，属独立的语义决策。**

## A.1i S5「删词汇」落地实测 —— 选 A 墓碑法（用户决策）

**结果：1247 passed / 12 skipped / 0 failed，26.07s。**

A.1f 说 S5"不能独立删"。实测它不是"删词汇"，而是**一次模型可见契约的收缩**，
且撞上 hard replay 兼容。以下全部在真实 ledger（`.agent/runtime.db`，692 task /
115119 event）上量得。

### 测量：这个 kind 到底在哪儿

| 位置 | 值 |
|---|---|
| 当前 task 快照含该 kind | **0 / 692**（落库前已被降级，3 条 `task_spec.criteria_demoted` 为证）|
| `task_spec.revised` 事件含该 kind | **206 条 / 385 处**，路径 `snapshot.acceptance_criteria[].verification_kind` |
| 反序列化路径 | `TaskAcceptanceCriterion.from_data` → 裸 `TaskCriterionKind(str(...))` → **未知成员 ValueError** |
| 直接验证 | `TaskSpecSnapshot.from_data` 在 HEAD 可成功 replay 这些快照 |

⇒ 删成员 = 206 条事件 replay 失败。**A.1f 没说这一层**，是本批新增的证据。

### 落地（选 A：枚举留作墓碑，只删模型可见面）

1. **`task_spec.py`**：`POST_MUTATION_COMMAND` 保留，加"replay-only tombstone"注释；
   新增 `REPLAY_ONLY_CRITERION_KINDS = {POST_MUTATION_COMMAND}`；
   `TASK_SPEC_PROPOSAL_SCHEMA_V1` 的 `verification_kind.enum` 改为同时排除
   `RUNTIME_AUTHORED_CRITERION_KINDS` 与 `REPLAY_ONLY_CRITERION_KINDS`。
   **模型可见枚举从 4 个降为 3 个**：`workspace_integrity / evidence_reference / rubric`。
2. **`kernel.py`**：删 `_demote_unresolvable_criteria` 的 `POST_MUTATION_COMMAND` 分支
   （原 1250-1274）与对应文档 bullet；import 收窄为只 `is_verification_request`。
   **该函数保留**：它的第二个分支（`EVIDENCE_REFERENCE` 降级）是活的，且正是死锁事故
   （`task-2c8f8fbb...`）的防死锁守卫。`_verify_task_spec_reference` 因此保留。
3. **模型可见面文本**：`task_spec_tools.py` 工具描述、`planner.py` planner prompt 各删一句。
4. **证据等级**：删 `EvidenceLevelSignals.passed_post_mutation_commands` —— 它**从来没有
   写入点**（只被 `evaluator.py` 读），恒等于 +0，是纯死信号。
5. **`verification_command.py` 保留**：两个函数里 `is_verification_request` 本来就是活的
   （`kernel.py:761`、`kernel_tool.py:953`，工具资格判定）；`is_verification_command`
   也仍被 `validate_authored_criterion` 调用，不是死代码。**A.1f 说"删该模块"是错的。**

### 新增钉子（原测试一条都没覆盖这次改动）

`tests/test_task_spec.py::PostMutationCommandTombstoneTest`（3 条）：模型可见 schema 不含该
kind / 枚举成员仍在且可反序列化 / 含 `command` 的 criterion 往返不变。
**改动前后测试数 1244 → 1247，证明此前无人钉住这个契约。**

### 验证

* `py_compile` 全通过。
* 模型可见枚举实测 = `['workspace_integrity', 'evidence_reference', 'rubric']`。
* **老 ledger replay 实测：206/206 事件 OK。**

### 仍未处置（下一批候选）

* `tests/test_post_mutation_applicability.py` 的 gap 断言**已是空转**：`_completion_readiness_gaps`
  已 hollow（返回 `()`），所以 `assertNotIn(...)`/`assertEqual(list(...), [])` 永远不会失败。
  该文件真正还有效的只有 `_has_active_workspace_mutation` 那两条断言。它与
  `_completion_readiness_gaps` 这个 hollow 缝（及 6 个直接调它的 spec）同属一批。
* `rubric_judge` 通路整体孤儿化（见 A.1h）。
* 批 3d：`evidence_question.py` + 4 个 `_*_evidence_question` 方法 + 10 个 import 点。

## A.1j 批 3d 重扫 —— **撤销**：`evidence_question` 不是证明层，是探索循环的核心机制

A.1b 把 `evidence_question.py` 列为批 2 的残余，summary 又把"批 3d = `evidence_question.py`
+ 4 个 `_*_evidence_question` 方法 + 10 个 import 点"当作待删项。**两者都错。**

实测（HEAD）：

| 测量 | 值 |
|---|---|
| `require_evidence_questions` 在主编排工厂的默认值 | **`True`**（`composition.py:273`）|
| `turn_tool_batch.py` 调用 `_bind/_observe/_dispose_evidence_question` | **10 处** |
| 生产代码里 `get_evidence_questions` 调用点 | **11 处** |
| `evidence_question.py` 内的方法数（活的投影） | 14 |
| 依赖该词汇的 `src/` 文件 | **37** |
| 依赖该词汇的 `tests/` 文件 | **30** |

`EvidenceQuestion(question_id, question, expected_scope, scope_expansion_reason)` 描述的是
**"这次工具调用想回答什么问题"**。它绑在 `ToolCall` 上（`ports/tool.py:367`），由两个模型
适配器包进 `{evidence_question, tool_arguments}` 信封，由 `turn_tool_batch` 在绑定/观察/
处置时更新，再由 `EvidenceQuestionProjection` 投影成工作记忆、检查点、会话资源与流程视图。

**这跟"证明层"是两件事**：
* 证明层 = 验收裁决（`acceptance_criteria` → proof obligation → coverage → 终端裁定）—— 已删。
* 证据问题 = **探索循环的机制**（每次工具调用声明它要回答的问题）—— 是 tsm-agt 的核心特性，
  与 `acceptance_criteria` 无依赖关系。

⇒ **批 3d 撤销**。删它等于删功能，不是删证明层。这是本会话第二次因重扫而撤销批次
（第一次是 3c）。

### 教训（第三次复现同一模式）

附录 A 的分批是**按模块名相近**画的（`evidence_coverage` / `evidence_question` 名字像 →
被归到一起），而不是按**依赖**画的。`evidence_coverage` 已删、`evidence_question` 全活 ——
仅凭名字相邻推断归属，这次错了两次（3c、3d）。

## A.1k 批 5 —— 收掉 `_completion_readiness_gaps` hollow 缝 + 清理空转测试

**结果：1246 passed / 12 skipped / 0 failed。**（1247 → 1246：删 2 条空转测试，把 1 条
救回成 2 条 —— −1 −1 +1）

### 缝本身：**零 src 调用**

`Kernel._completion_readiness_gaps` 的调用点只有 3 个测试（`test_completion_claim_gate_
410`、`test_completion_readiness_507`、`test_post_mutation_applicability_71`），**生产代码
一个都没有**。删它不需要动任何 src 接线。

**注意别误删 `CompletionGap` 词汇**：真正活的生产者是 `_task_outcome_completion_gaps`
（`kernel.py:1100`，被 `request_task_outcome_completion` 调用），加上 `turn_phases.py:734`
的 recovery gap 与 `kernel_task.py:1014` 的消费端。缝只是**遗留的第二个生产者**。

### 三处空转测试的实测处置

| 位置 | 实测 | 处置 |
|---|---|---|
| `test_completion_claim_gate.py::DeliveryGapTest` | 只断言 `REQUIRED_DELIVERY_UNSATISFIED` —— 该 kind **在 src 里已无生产者**（只有 2 个测试提到）。同文件另有 15 条活测试（`DeliveryRecordTest`/`DeliveryCriterionAuthoringTest`）| 删该类 + 其 `_gaps` helper |
| `test_completion_readiness.py::test_mutation_only_delivery_does_not_invent_command_verification` | gap 断言空转，**但意图与活分支一一对应** | **救回**（见下）|
| `test_post_mutation_applicability.py` | 3 个测试里 `test_analysis_task...` 纯空转；另 2 个断言的是 `_has_active_workspace_mutation`（**全仓唯一覆盖**）| 修剪 + 改名（见下）|

### 救回：空转测试的意图落在活的 `MISSING_VERIFICATION` 分支上

活的生产者 `_task_outcome_completion_gaps` 的条件是
`ToolEffect.EXECUTE in outcome.required_effects and task.mutation_journal`
（`kernel.py:1206-1220`）—— 与被删测试"只欠 mutate 就不该要求命令验证"**是同一个不变量**。

改为直接驱动活生产者，并补上正例：

* `test_mutation_only_delivery_does_not_invent_command_verification` —— `mutate` → 无 gap
* `test_execute_owing_delivery_does_owe_command_verification` —— `mutate+execute` → 有 gap

**`MISSING_VERIFICATION` 此前零测试覆盖**，`_task_outcome_completion_gaps` /
`request_task_outcome_completion` 也**零测试覆盖** —— 这次救回顺带补上了唯一覆盖。

### 修剪 + 改名：`test_post_mutation_applicability.py` → `test_workspace_mutation_net_effect.py`

该文件删掉纯空转的 `test_analysis_task_does_not_owe_a_post_mutation_command`、
`_require_command_criterion`、`_persist_final_answer`、`_gaps` 之后，只剩
`_has_active_workspace_mutation` 的净效应断言。文件名已误导（`post_mutation` 现在是
replay-only 墓碑），故一并改名，并删掉因此不再使用的 `replace` /
`RuleBasedEvidenceLevelEvaluator` / `TaskAcceptanceCriterion` / `TaskCriterionKind` /
`ToolEffect` import。**注意：这个文件是 `_has_active_workspace_mutation` 的唯一覆盖，
不能整删。**

### 保留

`test_completion_readiness.py:419,444,1020,1072,1105` 与
`test_completion_readiness_exhausted.py:31` 里的 `POST_MUTATION_VERIFICATION` /
`REQUIRED_DELIVERY_UNSATISFIED` 是**直接构造 `CompletionGap` 喂活的 readiness policy**，
不经过被删的缝 —— 属正确写法，保留。

## A.1l 批 6 —— `rubric_judge` 通路（A-full）：摘依赖 + 删装配管线，保留适配器

**结果：1240 passed / 12 skipped / 0 failed。**（1246 → 1240 = 测试 −6，与预测一致。）
净 −319 行（31 增 / 350 删）。

### 先纠正我在上一轮报错的范围

我报给你的 (a) 只有 4 条，**漏了一整条连带链**，而且没量就动手了。实测：

`composition.py` 里两处 `reviewer = _reviewer_model_provider(...)` 的**唯一**消费者就是那个
judge 自动注册 —— 它上面那行注释自己写着 `# INV-10: a judged criterion must have a judge`。
所以摘掉兜底注册会一次杀死：

| 死掉的东西 | 位置 | 规模 |
|---|---|---|
| `reviewer` 局部变量 | `composition.py:932,1110` | 2 |
| `_reviewer_model_provider()` | `composition.py:830-868` | **39 行** |
| `review_model` 参数 + 透传 | `composition.py:834,878,1055,1244,1290` | 5 |
| `KernelDependencies` 4 个字段 | `kernel.py:428-436` | 9 行 |
| `ModelConfiguration.review_model` | `model_configuration.py:136` | — |
| `TSM_AGT_REVIEW_MODEL` 白名单 + 解析 | `model_configuration.py:44,266` | — |
| `ModelRubricJudge` import | `composition.py:19` | — |

`rubric_evidence_max_items`(435) 与 `rubric_evidence_excerpt_characters`(436) 也是孤儿 ——
**A.1h 那次扫描漏了后者**。

### 我犯的两个错，都记下来

1. **范围没量就动手。** 只删了 `KernelDependencies` 的字段，漏了 `composition.py:472` 的构造
   透传 → `KernelDependencies.__init__() got an unexpected keyword argument 'rubric_judge'`
   → **22 个失败**。已 `git checkout` 完整退回，没有留下半成品状态。
2. **重复了批 4 的教训。** 删 `test_goal_alignment.py` 的 `_RecordingJudge` 时，我用
   "class 行 → 下一个 `^class` 行"整段删，**吞掉了夹在中间的模块级 `async def _executing_task`**
   （HEAD 第 92 行）。它被 `GoalAlignmentCriterionTest` 调用 → 会 NameError。
   **同类错误第五次。规则应当固化为：删任何块之前，先列出该行区间内的所有 `^class` 与
   `^def`/`^async def`，确认没有夹在中间的模块级定义。**

### 落地

**删（管线）**：`review_model` 全链（含 `TSM_AGT_REVIEW_MODEL`）、`_reviewer_model_provider`、
`reviewer` 局部量、`KernelDependencies` 的
`rubric_judge`/`rubric_judge_max_attempts`/`rubric_evidence_max_items`/`rubric_evidence_excerpt_characters`、
`rubric_judge_adapter` 参数与注册、两处自动兜底注册、`ModelRubricJudge` import。

**保留（适配器）**：`ports/rubric_judge.py`、`adapters/model_rubric_judge/`、
`test_rubric_judge.py::RubricJudgeAdapterTest`（5 条，直接构造适配器，不经 composition）。
另外**有意保留** `composition.py` 的 `at most one RubricJudgePort` 单例检查 —— 该块是通用
port 完整性检查，里面 `SessionInputResolverPort` 同样从不被 composition 注册，保留才一致。

**修正文档串（用户要求）**：
* `task_spec.py` 的 `RUBRIC` 注释原写"advisory in the per-turn readiness gate and is evaluated
  by a bounded rubric judge before final acceptance" —— **两句都是假的**（gate 的 gap 生产者在
  批 5 收了，judge 在本批摘了）。改为"记录但从不评估"。
* `GOAL_ALIGNMENT` 注释原写"must be judged on whether it answered *this* request" —— 同样改真。
* 两处模型可见文本（`task_spec_tools.py` 工具描述、`planner.py` planner prompt）补上
  "recorded, not machine-evaluated"。

### 测试处置（−6）

| 文件 | 处置 |
|---|---|
| `test_rubric_judge.py::ReviewerProviderWiringTest`（2）| 删 —— 断言的 `judge._model is primary` 正是被摘的接线 |
| `test_rubric_judge.py::ReviewerModelConfigurationTest`（4）| 删 —— 断言的 `load_model_configuration().review_model` 已不存在 |
| `test_rubric_judge.py` 的两个**孤儿 helper**（`_executing_task`、`_set_rubric`）| 删 —— 它们拖死 `Path`/`TaskState`/`replace`/`TaskAcceptanceCriterion`/`TaskCriterionKind`/`compose_fixture_application`/`tempfile` 7 个 import。整文件重写为纯 adapter 契约测试（5 条）|
| `test_goal_alignment.py::GoalAlignmentJudgingTest`（0 条，40 行空壳）| 删；`_RecordingJudge` 删；5 个孤儿 import 删 |
| `test_production_journeys.py` | 删 `AlwaysSatisfiedJudge` + 2 处 `rubric_judge_adapter=`；3 条测试存活 |

### 验证

* `compileall` 全通过；A-full 相关符号在 `src/`+`tests/` **零残留**。
* 模型可见枚举仍为 `['workspace_integrity', 'evidence_reference', 'rubric']`（按你的选择保留 rubric）。
* **老 ledger replay 复验：206/206 OK。**

### 仍未处置

`test_post_mutation_applicability` 那条线在批 5 已处理。剩下的是本会话早先记录的
`PhaseAction.BREAK` / `ReportBlocked` / `ReportIncompleteRecoverable` 死枚举值，以及
比事故前基线仍少的测试用例（批 4/5/6 各按证据删过空转项，需重新建立基线）。

## A.1m 批 7 —— 收尾：`PhaseAction.BREAK` 死值，以及重建测试基线

**结果：1241 passed / 12 skipped / 0 failed，26.01s。**

### `PhaseAction.BREAK` —— 实测零消费

`PhaseAction` 定义在 `agent_loop.py:616`，成员四个。实测：

| 值 | 消费点 |
|---|---|
| `OK` | 默认值 + `turn_phases.py` 3 处产出；循环里**落空**（不分支） |
| `CONTINUE` | `kernel.py:3478,3486` 分派；`turn_phases.py:607,889,926` 产出 |
| `RETURN` | `kernel.py:3473,3480,3484` 分派；`turn_phases.py:88,774,939` 产出 |
| **`BREAK`** | **只有定义行本身** —— `src/` + `tests/` 全域零引用 |

`Kernel.run_agent_turn` 的 `while True`（`kernel.py:3466-3487`）只分支 `RETURN` 与
`CONTINUE`；`OK` 是落空。**`BREAK` 从来没有任何分支处理它**，是个静默 no-op。

**先确认它不持久化再删**：`PhaseAction`/`PhaseVerdict` 无 `to_data`/`from_data`/
checkpoint 路径，`"break"` 字符串在全域只出现在定义处 —— 纯内存控制流，**没有 S5 那种
replay 兼容风险**。

删除并在文档串里写明"每个成员都必须是循环真正分支的那个值，`BREAK` 曾以 no-op 的形态
坐在这里"。

**顺带撤回一条过期的待办**：summary 里的 `ReportBlocked` / `ReportIncompleteRecoverable`
两个"死枚举值"**在全域已不存在**（`src/` + `tests/` 零命中），那条待办本身是过期的。

**新增钉子** `tests/test_agent_loop.py::PhaseActionVocabularyTest`：断言成员集恰好是
`{ok, continue, return}`。这是唯一能防住"再加一个没有分支的成员"的检查 —— 也正是当初
`BREAK` 溜进来的方式。

### 重建测试基线

**⚠️ 先更正本批初稿里的一句错话。** 我最初写"'比事故前少 28 条'不可复现、载体已随事故消失"，
**这是错的** —— 我当时没读 `tsm-agt-待恢复文件清单.md`。那个 28 **有明确载体、有算式**：
该文件 §4 记录 `事故前 1469 收集 / 现在 1443（含我新增 2 条）` ⇒ `1469 − (1443 − 2) = 28`，
并列出受影响的 11 个测试文件；§8.4 用一张排查表（APFS 快照 / Time Machine / Cursor 与 VS Code
本地历史 / 1077 个 git 对象 / 18 个游离提交）证明**本机确实没有可恢复源**。

**所以准确说法是**：那 28 条**有据可查、但本机不可恢复**，只能靠仓库外的备份取回
（`scripts/restore_from_backup.sh` 就是那个取回工具，**故不应删除**）。我先前"测不出基线"
的说法只对了一半：`git log` 里确实没有基线（事故丢的是未提交改动），但**文档里有**。

**新基线（命令写死以便复现）**：

```
.venv/bin/python -m pytest tests/ -q -p no:randomly
→ 1241 passed, 12 skipped, 0 failed, 252 subtests, ~26s
```

* `--collect-only`：**1253** collected = 1241 passed + 12 skipped（自洽）
* 源码级 `def test_` 计数 1238（差额来自参数化用例）
* **逐文件审计：最小用例数是 1，没有任何"收集到 0 条"的空洞文件**
* 已知的单用例文件（都是有意截断的）：`test_terminal_state_mapping.py`(1)、
  `test_runtime_task_result.py`(1)、`test_coding_agent_e2e.py`(1) 等
* **12 个 skip 的构成**：5 个 pty（本沙箱禁 `/dev/ptmx`）+ 7 个 Windows 平台契约。
  事故文档记的是 13 个 skip，差的 1 个 = `tests/test_real_provider_journey.py` 那条
  被 `@unittest.skipUnless` 守卫的用例，随该文件在批 3b-1 删除而消失（已实测核对）。

**本会话测试数的净变化账**（可复算）：

| 批次 | 增 | 删 |
|---|---|---|
| 批 4 | +17（救回 6 个测活代码的类）| −120（`test_proof_closure.py` 整删）|
| S5 | +3（墓碑契约钉子）| — |
| 批 5 | +1（救回成 2 条）| −2（空转项）+1（原空转条）|
| 批 6 | — | −6（接线测试 2 + config 测试 4）|
| 批 7 | +1（`PhaseAction` 钉子）| — |

### 证明层已不在运行路径上（本批的收束结论）

不产出、不计算、不裁决、不阻断、不装配。剩下的 `ports/rubric_judge.py` 与
`adapters/model_rubric_judge/` 是**有意保留的适配器**（可一行重新注册接回），
`TaskCriterionKind.POST_MUTATION_COMMAND` 是**有意保留的 replay 墓碑**。
（后续批次见 A.1n / A.1o。）

## A.1n 批 8 —— 文档同步（方案风险表里的必做项）

**结果：1241 passed / 12 skipped / 0 failed。**

### 7 份文档，逐份按"陈旧程度"给处置，不是套一句通用横幅

| 文档 | 陈旧引用 | 处置 |
|---|---|---|
| `tsm-agt-完成判定机制.md` | 6 | **整篇主题即被删机制** → 横幅说明题目所指的 verdict→终态判定已不存在，并给出当前语义 |
| `tsm-agt-不变式术语表.md` | 2（原文多处）| **查阅型文档，泛横幅不够** → 横幅 + **逐条状态表**（已废 / 仍成立 / 部分成立 / 出处已失）|
| `tsm-agt-对齐Hermes设计方案.md` | 5 | 横幅：终态映射链已删；本文主张的 Hermes 形状**已落地** |
| `tsm-agt-按Hermes重构kernel映射.md` | 9 | 横幅：行数/行号/方法清单全部过期（kernel 又减约 1,400 行）|
| `tsm-agt-单轮多轮重构方案.md` | 4 | **直接改事实**：原文"5 套并行词汇表 / 56 个耦合点" → 已收敛 + 实测 51 |
| `tsm-agt-会话框架重设计.md` | 2 | 行内标记 `turn_finalizer.py` 已删 + 合计行标注过期 |
| `tsm-agt-多轮会话实测.md` | 0 | 新增 §10：删除后复测（见 A.1o）|

### 术语表的逐条判真（这是本批最需要证据的部分）

**仍成立 2 条**（实测）：`INV-4`（`_finalize_needs_review` 活于 `kernel.py:2758,3667`）、
`INV-15`（`PhaseAction` 恰好 `{OK, CONTINUE, RETURN}`，三者都有处理，新增测试钉住）。

**部分成立 4 条**（各自写明当前实际行为）：`AG-7`（NEEDS_REVIEW 仍可达，但覆盖层触发源消失）、
`AG-9`（`_apply_inv3_demotion` 已删；保留的是 `_demote_unresolvable_criteria` 的
`EVIDENCE_REFERENCE` 分支 —— 防死锁对策）、`AG-16`（`TERMINAL_STATE_BY_VERDICT` 已删故无从违反，
残留 `_exit_code_for`）、`INV-13/14`（readiness 门仍在但 gap 生产者已删）。

`AG-13` 单独核实后归入**已废**：枚举完整性校验随覆盖层消失，`stop_reason` / `skipped_count`
在全域只剩 `ports/tool.py` 一句注释。

### 顺带修掉一处**代码里的**过期断言

`tests/architecture/test_dependencies.py` 的 `_TERMINAL_STATE_AUTHORITY` 注释写
"the enum itself, **the verdict mapping**, and the outcome vocabulary"，并把
`core/verification.py` 列为终态权威。实测该文件（70 行）**零** `TaskState`/`terminal` 引用 ——
verdict 映射删掉后它不再命名任何终态，该条目是**没人在用的许可**。已从集合移除并改正注释。
架构测试 17 passed / 45 subtests 通过。

### 棘轮基线说明

`_TERMINAL_STATE_COUPLING_BASELINE` 合计已从 56 收紧到 **51**（`cli.py` 7→5、`sdk/runtime.py`
8→5，会话内已回写）。`tsm-agt-单轮多轮重构方案.md` 里仍写 56，本批改为实测值并列出各文件分解。

## A.1o 批 9 —— 多轮会话复测：删证明层**没有**打断多轮，且未引入回归

> 起因：用户最初三条抱怨的第 2 条是"多轮会话运行不了"。证明层删完后必须回答它，
> 而不是拿"78 个会话读路径重放零回归"充数 —— 那只证明"会话能恢复读取"，
> 不证明"能连续跑 N 轮"。

**方法**：复刻 `tsm-agt-多轮会话实测.md` 的场景 —— 单会话连续 14 轮、每轮
`submit_session_text` **之后** `run_task`（该文件 §7 的教训：只 submit 不 run 会让断言空转）、
`CONTEXTUAL` 路由、fixture 4096 窗口。**结论：14/14 轮 `SUCCEEDED`，每轮都真的发出了模型调用。**

**非回归对照**：在 `git worktree` 的 `88331b8`（删除前）上跑同一脚本、同一场景：

| | 删除前 | 删除后 |
|---|---|---|
| 终态 | 14 SUCCEEDED | 14 SUCCEEDED |
| 每轮模型调用 | 1 | 1 |
| `recent_messages` 上界 | 8（第 5 轮） | 8（第 5 轮） |
| `recent_task_summaries` | 3 | 3 |
| `earlier_summary` 序列 | omit 0→10 / mc 0→19 | **完全相同** |
| 投影字符（3/6/9/10/14 轮） | 5297/6983/7061/7090/7209 | 5281/6959/7037/7066/7185 |

过滤掉每次运行新生成的 UUID 后，**整个 session-context 载荷里唯一的内容差异**是
`verification_status`：`"passed"` → **`null`**。那 −24 字符完全由它构成。已逐点核实消费者安全：
`session_context.py:317`（声明即 `str | None`）、`session_handoff.py:92`（`or "unknown"`）、
`evaluator.py:44`（`in {"failed","blocked"}`）。CLI/Web 用的是**另一个派生状态**
（`task_runtime_projection.py:213`），它读 `verify.completed` —— 该事件仍在写，但只剩人工复核
accept 一条路径（`kernel_task.py:902`，`manual_verified: True`）。

**⇒ 现在唯一能把任务标记为"领域已验证"的，是显式人工接受。**

### 我自己在这次复测里犯的两个错，一并记下

1. **读错了键**：载荷里的键是 `recent_task_summaries`，我先按 `task_summaries` 读，
   得到"恒为 0"，一度当成回归。**测量伪像**，已改正并把真键名写回 `tsm-agt-多轮会话实测.md` §10.3。
2. **差点把对照实验当结论**：`recent_task_summaries` 稳定在 3（上限 4），我先怀疑是改动造成的；
   删除前对照同样是 3，**故与本次改动无关**，原因未查明，已如实记录。

### 遗留（未做，需决策）

CLI（`cli.py:612` "领域检查: {status}"）与 Web（`app.py:1567` "领域检查"）现在对**每个机器完成
的任务**都显示 `not_started`。该值**准确**（确实没跑检查），但它是被删层留下的**用户可见面**。
处置需产品决策：保留 / 改标签 / 删除。

## A.1p 批 10 —— 归档项**反转**：恢复清单不能归档；并更正我的一处错话

原计划的第 3 项是"归档 `tsm-agt-待恢复文件清单.md` + `scripts/restore_from_backup.sh`"。
动手前按纪律先把它读完 —— **读完就该反转**。

### 反转理由：它承载三件仍需仓库外动作的事

1. **§4 的 28 个用例可查但不可恢复。** 算式在文里：`事故前 1469 收集 − (1443 − 2 新增) = 28`，
   并列出受影响的 11 个测试文件；§8.4 用排查表穷尽本机恢复源（APFS 快照 / Time Machine /
   Cursor 1087 项与 VS Code 94 项本地历史 / 1077 个 git 对象 / 18 个游离提交），结论是本机无源。
   ⇒ 只有**仓库外备份**能取回，`scripts/restore_from_backup.sh` 就是那条路径的工具。
2. **§5 末尾**：`cli.py`（3,905 行）与 `web/app.py`（3,843 行）测试几乎没触达，若备份有差异
   应优先覆盖。
3. **§10**：5 个 pty 用例在本沙箱只能 skip，需在有伪终端的终端里跑一次。

⇒ 归档会把这三件**埋掉**。处置改为：加横幅明确"仍可行动、不要归档"，并标注过期计数。

### 更正我自己在 A.1m 里写的一句错话

A.1m 初稿写"'比事故前少 28 条'不可复现、载体已随事故消失"—— **错**，因为我当时没读这份文档。
准确说法：那 28 **有载体、有算式、有排查结论**；`git log` 里确实没有基线（事故丢的是未提交
改动），但**文档里有**。已在 A.1m 就地把这句改成正确表述。

### 顺带把 skip 13→12 的原因定位实

事故文档记 13 个 skip"与事故前完全一致"。当前是 **12**。差额 1 条 = 
`tests/test_real_provider_journey.py` 里那条被 `@unittest.skipUnless` 守卫的用例，随该文件在
**批 3b-1** 删除而消失。现存 12 = 5 个 pty + 7 个 Windows 平台契约。

### 读这份文档还得到一个交叉印证

它的 §8.3 说那 3 个 phantom `EXECUTING` 属于身份功能上线前（2026-08-31）的 legacy Session，
`legacy-local-subject` 是 store 在数据里没有 subject 时的兜底值。这与我本批做的
**692 任务 / 78 会话全量读路径重放**结果一致 —— 那 3 个会话正是 `PermissionError: session
belongs to another local subject` 的来源，属设计上的主体边界，不是缺陷。

### 仍然开着、需要你定的两件事（都不是本次重构引入的）

1. **28 个用例**：只有仓库外备份能取回。若你手头有那份备份，`restore_from_backup.sh` 是取回工具。
2. **CLI/Web 的"领域检查"面**：`cli.py:612` 与 `web/app.py:1567` 现在对每个机器完成的任务都显示
   `not_started`（值准确，但属被删层的遗留可见面）。保留 / 改标签 / 删除 —— 产品决策。

## A.1q 批 11 —— CLI/Web 的"领域检查"行改为**有值才显示**（用户决策 B）

**结果：1242 passed / 12 skipped / 0 failed**（+1 条新测试）。

### 先纠正这批的定性：它不是"残留垃圾面"

我原先把它描述成"被删层留下的用户可见面"，读代码后发现框架不对：

* 它是**被测试钉住的有意设计**：`test_cli_standalone_finalization.py:63,102`、
  `test_web_phase1_state.py:72` 三处断言。
* 它的**值是准确的**：`not_started` 就是"没跑过领域检查"。
* 投影字段本身**必须保留**：`task_runtime_projection.py:241` 的 `_phase()` 在用它，
  且人工 accept 会写 `passed`。

⇒ 真正的问题不是"垃圾"，而是**信息量塌缩**：证明层删除后，每个机器完成的任务都恒为
`not_started`，渲染出来会被读成"检查卡住没开始"。

### 落地（方案 B）

| 位置 | 改动 |
|---|---|
| `cli.py` | 仅当 `verification_status is not NOT_STARTED` 才 `output_fn("领域检查: …")`；新增 `TaskVerificationStatus` import |
| `web/app.py` | 同样的条件（`!== 'not_started'`），并从 `|| '未开始'` 兜底改为不渲染 |
| `cli.py` 该函数 docstring | "Print four independent persisted result categories" → 说明该行可有可无及原因 |
| `test_cli_standalone_finalization.py` ×2 | `assertTrue(该行存在)` → `assertFalse(该行存在)`（那两个场景无 `verify.completed`）|

### 补上 B 的另一半（否则只有"抑制"被覆盖）

新增 `test_domain_check_row_reappears_once_a_verdict_exists`：`complete_task` 后追加一条
`verify.completed{status: passed, manual_verified: true}`（即人工接受路径实际写的事件形状），
断言 `领域检查: passed` **重新出现**。**两个方向都被钉住。**

### 顺手记一个我自己的失误

第一次改那两处断言用了 `replace_all`，但两处**缩进不同**（第二处在 `try:` 内，深一层），
结果产生 `SyntaxError: expected 'except' or 'finally' block`。**教训：`replace_all` 只适合
缩进相同的同形行；否则要各自带上下文替换。**

## A.1r 真实场景端到端验证 —— **已跑通**（网络前提已变，此前"无法验证"作废）

**起因**：用户问"现在能真实场景使用了吗"。此前所有验证都基于 fixture/桩模型，而恢复文档把
"真实模型端点端到端"记为**唯一无法验证**项（理由是该域名被解析到 `198.18.0.71` 黑洞）。
动手前先复核这个前提 —— **前提已经不成立**。

### 网络前提复核（先查，别继承旧结论）

| 检查 | 结果 |
|---|---|
| DNS | `aibrain-large-model-engine-slow.hellobike.cn` → **`10.33.200.42`**（不再是黑洞）|
| TCP 443 | **通**，0.04s |
| TLS | **通**，证书 CN `*.hello-inc.com`（上海哈啰普惠科技有限公司）|
| `tsm-agt doctor` | **healthy**，14 adapters，模型配置完整（`OpenAI-GPT-5.1`，key 已配，0600）|

### 四条真实路径，全部跑通

**① 最小真实模型调用**
`compose_openai_compatible_engineering_application_from_env(./.env)` →
`provider.complete(...)` → 返回 `'PONG'`，6.17s，`finish_reason=stop`，
`usage: input=12/output=11`。

**② 真实单轮工具任务**（独立项目 `/tmp/tsm-real-1`，含 `README.md` + `src/calc.py`）
提示："Read every file … state whether any file contains a bug."
→ 3 次模型调用 / 3 次工具调用，50.8s，29,476 input / 764 output tokens。
回答**准确**：正确列出两个文件，正确指出 `divide(a, 0)` 会抛 `ZeroDivisionError`。
**终态 `SUCCEEDED`**，事件链完整：`completion.readiness_evaluated` → `turn.completed`
→ `task.state_changed(SUCCEEDED)`。

**③ 真实 3 轮会话**（同一 session，真实模型）

| 轮 | 路由 | 结果 |
|---|---|---|
| 1 | `CREATE_TASK`（`no_unfinished_session_task`）| `SUCCEEDED`；答"两个简单数学函数，一个加一个除" ✓ |
| 2 | 先 `ANSWER`(0.95) → **`session.route_self_corrected`**（"问的是本地文件 README.md，需要工作区访问，而我没有"）→ 改建任务（`task_relation: CONTEXTUAL`）| `SUCCEEDED`；答"tiny two-file demo project" ✓ |
| 3 | `ANSWER`（confidence 1.0，`catalog_task_ids`=前两个任务）| **答 "You first asked about the file `src/calc.py`."** ✓ **跨两个任务正确回忆第 1 轮** |

⇒ 第 2 条抱怨（"多轮会话运行不了"）在**真实模型**下得到正面回答；且观察到**路由自我纠正**
（`session.route_self_corrected`）在真实运行中救回一次错误路由。

**④ 真实写入路径（含审批）**（`/tmp/tsm-real-2`）
提示："Create NOTES.md containing exactly one line 'created by tsm-agt'."
→ planner 产出 `core.apply_patch`（R1）→ **停在审批**，磁盘未改，打印完整补丁预览 + 载荷
hash + 回滚说明 → `tsm-agt approve <request_id> --reason '…'` → 补丁落盘 →
**agent 自己再 `core.read_file` 回读确认** → 终态 **`SUCCEEDED`**。
`NOTES.md` 磁盘内容精确为 `created by tsm-agt`（18 字节，无多余换行）。

⇒ **planner → 工具调用 → 人工审批 → 真实落盘 → 回读自验 → 机械闭环** 全链在真实模型下成立。

### 行为 B 在真实输出里得到验证

真实运行的 CLI 输出**没有** `领域检查:` 行（因为状态是 `not_started`）——
这正是批 11 的行为 B 在真实场景的表现，与 fixture 测试一致。

### ⚠️ 真实场景暴露的一个缺陷（fixture 永远测不出）

`core.list_files(recursive=True)` **把 Runtime 自己的状态目录当成项目内容返回**。
实测第一个工具调用就返回了 `.agent`、`.agent/memory.db`、`.agent/runtime.db` 等条目，
agent 随后读入并在回答里把它们当成"项目文件"逐条点评。

根因（已定位到行）：

* `src/tsm_agt/core/workspace.py:18` 定义了 `_EXCLUDED_DIRECTORIES`
  = `{".git", ".agent", ".gradle", ".idea", ".venv", ".vscode", "__pycache__",
  "node_modules", "target"}`，**并被** `workspace.py:902`（工作区快照）与
  `onboarding.py:268` 使用；
* 但 `src/tsm_agt/adapters/builtin/core_tools.py:648` 的 `_walk()`（`core.list_files`
  递归路径）**只排序、不过滤**；调用处 `:342` 也只用 `_is_sensitive()` 过滤凭证文件。

⇒ **两层不一致**：工作区层排除这些目录，工具层不排除。后果是 token 浪费
（本例 29k 输入里有相当部分是本可跳过的二进制库）与回答被污染。

**这是本轮唯一发现的功能性缺陷，且只有真实运行才能暴露。** 是否修（把
`_EXCLUDED_DIRECTORIES` 接进 `_walk` / 或抽成共享常量）属行为变更，**待用户决定**。

## A.1s 真实用户报障：会话被"已死机制的待回动作"永久阻塞（A+B 修复）

**用户报告**：在旧会话 `task-8d1ec232a0e54910852a029dcf11b03f` 里输入"看下今天的nba新闻"，
得到 *"当前任务正在等待审批或回答，请通过对应通道回复，或使用显式命令（/new、/stop）。"*

### 诊断（基于真实 ledger，不是推测）

该任务的真实状态：

```
state: AWAITING_USER
pending_user_action: {kind: "CONTINUATION",
                      reason: "completion_exhausted_with_required_gaps",
                      gap_ids: ["evidence-question:249158bfa15d"], ...}
```

产生它的那两段代码（`completion.blocker_disclosure_requested` 与 gap 耗尽）**已随证明层删除**。
全仓扫描确认：`unmet_acceptance_criteria` 与 `completion_exhausted_with_required_gaps`
在 `src/` 里**零生产者**；而 `incomplete_recoverable` / `unresolved_tool_batch` 仍是活的
（`kernel.py:2775,3630` / `:2708`）。

**35 个 `AWAITING_*` 任务的实际构成**：

| 数量 | kind | reason | 生产者 |
|---|---|---|---|
| **16** | CONTINUATION | `unmet_acceptance_criteria` | **已删** |
| 8 | CONTINUATION | `incomplete_recoverable` | 活 |
| 3 | CONTINUATION | `unresolved_tool_batch` | 活 |
| **2** | CONTINUATION | `completion_exhausted_with_required_gaps` | **已删** |
| 3 | CLARIFICATION | — | 活 |
| 2 | APPROVAL | — | 活 |
| 1 | OUTCOME_RECONCILIATION | — | 活 |

⇒ **18 个任务在等一个已被删除的机制**，而规则 (3)（`sdk/runtime.py:572`）仍拿它拒绝新输入
→ 会话**永久阻塞**。这是本次重构的欠账：删了生产者，却把这些任务留在"等你回话"。

### Bug A：提示让人用一个**不存在的命令**

提示写"显式命令（/new、**/stop**）"。实测 `/stop` **只**存在于 Web 的 HTTP 路由
（`app.py:3189` `POST /tasks/{id}/stop`）与这句文案里；`cli.py` 的 chat 命令表**没有**它，
意图分类器也不识别该前缀，且 chat 循环会把所有未知 `/` 输入拦成 `unknown command`。

### 修复（用户选 A+B）

**A（文案说真话）**：提示改为
`"…或使用显式命令（/new 新建任务、/resume 恢复该任务）。"`

**B（路由容忍）**：新增
`RuntimeCommandService._ORPHANED_PENDING_ACTION_REASONS = {unmet_acceptance_criteria,
completion_exhausted_with_required_gaps}` 与 `_awaits_orphaned_action(task)`；
规则 (3) 的条件加上 `and not self._awaits_orphaned_action(active)`。
待回动作属于已死机制时**不再拒绝**，落到既有规则 (5) 交给会话路由；
**任务本身不动**（`/resume` 仍能列出并恢复它）。

**刻意保留的保守性**：用的是**显式死因集合**，不是"任何未知 reason 都放行"——
未知 reason 可能是真实且仍可回答的动作，对它放行会把普通文本静默改释为新话题，正是规则 (3)
要防的事。`AWAITING_APPROVAL` 与活 reason 的行为**不变**。

### 测试与对照（含一次我自己做错的对照）

新增 `tests/test_orphaned_running_tasks.py::OrphanedPendingActionInputTest` 4 条：
死因不再拒绝 / 活 reason 仍拒绝 / 无待回动作仍拒绝 / 文案含 `/new`、`/resume` 且不含 `/stop`。

**第一版对照是无效的，必须记下来**：我在 `git worktree` 的 HEAD 上跑新测试，结果"4 passed"，
看似测试空转。实际原因是本仓库有 **editable 安装**（`.venv/.../_editable_impl_tsm_agt.pth`
指向 repo 的 `src`），pytest 导入的是**修复后**的代码 —— 对照根本没换代码。

改为强制源码后对照才有效：

```bash
cd <worktree> && PYTHONPATH=<worktree>/src <repo>/.venv/bin/python -m pytest …
→ 2 failed, 2 passed      # 两条新测试失败，两条回归守卫通过
```

⇒ 新测试确实钉住了这两个 bug。

**顺带核实更早的两个对照是有效的**：`sys.path.insert(0, <worktree>/src)` 经实测**能压过**
editable 安装（A.1h 的 ledger 重放对照与 A.1o 的多轮对照都用的这种方式，故结论成立）。
**教训**：pytest 下不能靠 `--rootdir`/`cwd` 切换源码，必须用 `PYTHONPATH`。

### 给用户的即时绕行（改动前就能用）

* `/new <目标>` —— 显式新建独立任务，绕过该拒绝；
* `/resume` —— 列出并恢复等待中的任务。

## A.1t 方法学更正：ledger 副本必须含 WAL；并在**正确副本**上确认 A.1s 的修复

### 我发现的自身方法错误

在 A.1s 的验证里我用 `shutil.copy(".agent/runtime.db", tmp)` 复制 ledger，然后在副本上读用户那个
任务，得到 `state=EXECUTING` —— 与直接读原库得到的 `AWAITING_USER` **矛盾**。

原因：**这个库是 WAL 模式**，`.agent/` 下除 `runtime.db` 还有 `runtime.db-wal`（4.3 MB）。
`shutil.copy` 只拿主文件，丢掉尚未 checkpoint 进主文件的 WAL 内容。实测：

| 复制方式 | tasks | events | sessions | 含 `post_mutation_command` 的 revised 事件 |
|---|---|---|---|---|
| 直接读原库（含 WAL） | 692 | **115,119** | 78 | 207 |
| `shutil.copy` 主文件（**我早先的做法**）| 692 | **115,089** | 78 | 207 |
| `sqlite3.backup()` | 692 | **115,119** | 78 | 207 |

差额是 **30 条事件**；用户那个任务的挂起（#107/#108）恰在其中，所以我在过期副本上把它读成了
`EXECUTING`。`sqlite3.backup()` 是正确做法（恢复文档 §8 当初就是用它，所以它的 687/114,840
数字是对的）。

### 对早先结论的影响：**结论成立，覆盖声明需要收窄**

A.1h / A.1l 的 ledger 重放对照（"692 任务 / 78 会话 / 8080 次读调用，删除前后逐类别完全一致的
36 次失败"）**用的是 `shutil.copy`**。评估：

* **不受影响**：任务数 692、会话数 78、以及 S5 依据的那 207 条 `post_mutation_command` 事件
  —— 三者在两种复制下**完全相同**，所以 A.1i 的墓碑/replay 结论不受影响。
* **仍有效但需收窄**：对照两侧跑的是**同一份**缺 WAL 的副本，所以"删除前后零回归"这个比较
  依然成立；但它验证的是**已 checkpoint 的子集**，有 30 条事件（含用户那个任务的挂起）
  在两份副本里都是以**更早的状态**被读的，未被真正覆盖。

### 在正确副本上重做 A.1s 的关键验证

`sqlite3.backup()` 副本上，逐项：

```
state            : AWAITING_USER
pending reason   : completion_exhausted_with_required_gaps
判定为死因        : True
修复后仍会拒绝吗   : False          ← 用户的会话被解开
全库：不再阻塞（死因）18 / 仍会阻塞（活因）17，用户那个任务在"不再阻塞"集合里
```

### 顺带确认：我**没有**写入用户的真实 ledger

* 全局最新事件时间 `2026-10-01T02:41:36`（即该任务自己的挂起），此后无任何新事件；
* 该任务 `last_event_sequence = max(events.sequence) = 108`；
* `runtime.db` mtime 未变；`-wal` 未增长。只有 `-shm` 的 mtime 变了（只读打开会触碰共享内存索引，
  不写入内容）。所有分析脚本走的都是副本。

## A.1u 运维留痕：真实 ledger 已按用户要求清空（2026-10-02）

用户要求清掉旧任务数据。**执行前的两个前提检查都很关键**：

1. **有进程正在持有它**：`lsof .agent/runtime.db` 显示 PID 96171
   （`uvicorn tsm_agt.web.app:app --port 8080`，pid 记在 `.agent/run/tsm-agt-web.pid`）。
   在活连接下删 SQLite 文件会造成**脑裂**（进程继续写已 unlink 的 inode，新进程另建一库）。
   **该进程当时其实已经卡死**（8080 无响应，`curl` 返回 000），所以 SIGTERM/SIGINT 都无法让它
   走完优雅关闭，最后 `kill -9` 才退出 —— 这一点本身值得留意。
2. **库里还存着项目信任**：`project_trust` 3 条（本仓库、`workSp/AppHitchMainH5`、
   `workSp/HellobikeAndroidAtlasApp`），`project_onboarding` 2 条。整库删除会让你必须重新
   `tsm-agt trust`，所以选择**只清台账、保留信任**。

### 做了什么

| 步骤 | 内容 |
|---|---|
| 停止持有者 | SIGTERM→60s 无效 → SIGINT 无效 → `kill -9 96171`；清掉陈旧 pid 文件 |
| 一致性备份 | `sqlite3.backup()` → **`.agent/ledger-backup-20261002-231900/runtime.db`（450 MB）**，含 695 任务 / 115,213 事件 / 78 会话 / 3 条信任。放在 `.agent/` 内因为家目录被沙箱拒写、且 `.agent/` 已 gitignore（`git status` 干净）|
| 清空 | `DELETE` 六张台账表：`runtime_events`(115,213) `session_events`(4,286) `runtime_tasks`(695) `runtime_sessions`(78) `runtime_commands`(1,148) `session_task_commands`(763) |
| 回收 | `wal_checkpoint(TRUNCATE)` + `VACUUM`：**450.0 MB → 0.1 MB**（回收 449.9 MB；磁盘当时 98% 已用）|
| 校验 | `PRAGMA integrity_check = ok`；`project_trust` 3 / `project_onboarding` 2 / `schema_version=2` 保留 |

### 清空后验证

* 新库可写：新建 session + task 成功（冒烟后已清掉，最终 0/0/0）。
* **三条项目信任全部保留**，实测都返回 `TRUSTED_BUILD`。
* 全量测试 `1246 passed / 12 skipped / 0 failed`。

### 未动的东西（超出授权范围，留给用户决定）

* `.agent/runtime/`（18 MB / 292 文件）—— 旧任务的**工作区快照与变更日志**，现已成孤儿；
  体积小、无功能影响。
* `.agent/memory.db`（24 KB + 4 MB WAL）—— **项目记忆**，不是任务台账。
* `.agent/ledger-backup-20261002-231900/`（429 MB）—— 安全备份，确认无误后可删以回收空间。

Web UI 启动器：`scripts/restart.sh web`（默认 127.0.0.1:8080）。

## A.1v 搜索层：先读 Codex/Hermes 源码，再按 O1 统一排除策略

**起因**：用户问"能否直接把 codex 或 Hermes 的搜索工具参考抄过来"。动手前先把两边源码读了。

### Codex 没有搜索工具可抄

`codex-rs/protocol/src/parse_command.rs`：

```rust
enum ParsedCommand { Read { .. }, ListFiles { .. }, Search { .. }, Unknown { .. } }
```

它是 **`shell` 工具 + 命令解析**（`ls`/`find`→`ListFiles`，`grep`/`rg`→`Search`，`cat`→`Read`），
用于审批与安全判定。模型靠 shell 里的 `rg` 搜索。**把它抄过来 = 给 tsm-agt 换一个架构**
（引入 shell 工具），远大于修排除策略这件事。⇒ **无可抄之物。**

### Hermes 正是那个参照，而且排除行为**不是**靠名单

`tools/file_tools.py` 定义、`tools/file_operations.py` 实现，**一个** `search_files` 工具
（`target='content' | 'files'`），描述里明写 *"Use this instead of grep/rg/find/**ls** in
terminal"*：

* `rg --line-number --no-heading --with-filename` + `--glob` + `-l`/`-c` + `| head -n N`（带 pipefail）
* **不加 `--hidden`** ⇒ `rg` 的默认语义生效：**跳过隐藏目录（`.git`/`.agent`）并尊重 `.gitignore`**
* 无 `rg` 退 `grep`（内容）/`find`（文件），并给安装提示；ripgrep 是声明依赖（`hermes_cli/dep_ensure.py:37`）
* 还主动往内部缓存目录写 `.ignore`（`tools/skills_hub.py:3175`）让搜索跳过自己

⇒ **Hermes 不会读到 `.agent` 的真正原因是它在用 rg 的默认ignore语义，不是它有一份更长的名单。**

### tsm-agt 的实情：**五份互相不一致的名单**

| 来源 | 内容 | 缺什么 |
|---|---|---|
| `workspace._EXCLUDED_DIRECTORIES` | `.git .agent .gradle .idea .venv .vscode __pycache__ node_modules target` | 缺 `.cxx build dist out vendor` |
| `onboarding._EXCLUDED_DIRECTORIES` | `… build dist vendor` | 缺 `__pycache__ .cxx out` |
| `core_tools._GENERATED_DIRECTORY_NAMES` | `… .cxx build dist out vendor` | **缺 `.git .agent .gradle .vscode`** ← 缺陷根因 |
| `text_code_intelligence._EXCLUDED_DIRS` | `… build dist vendor` | 缺 `.vscode .cxx out` |
| **`core_tools._walk()`** | **无** | **全部** ← `list_files(recursive=True)` 的直接缺陷 |

### 落地（用户选 O1）

* `core/workspace.py` 建立**唯一公开常量** `EXCLUDED_DIRECTORIES`（五份取并集，14 项：
  `.agent .cxx .git .gradle .idea .venv .vscode __pycache__ build dist node_modules out target vendor`）
* 四个 walker 站点全部改用它：`workspace.py:911`、`onboarding.py:265`、`core_tools.py:653/670/710`、
  `provider.py:274`；删掉三份局部名单（`_EXCLUDED_DIRECTORIES` ×2、`_GENERATED_DIRECTORY_NAMES`、
  `_EXCLUDED_DIRS`）
* **`_walk()` 补上过滤** —— 这是缺陷本体。注释写明它**只剪枝递归，不封锁显式路径**：
  `list_files(path=".agent")` 仍能列出（定向访问保留）
* 比较统一为 `str.casefold()`：`workspace`/`onboarding` 原先大小写敏感，在大小写不敏感文件系统上
  `.GIT` 会漏网 —— 顺带修掉（新增测试断言名单全小写，使 casefold 比较成立）

### 测试与对照

新增 `tests/test_core_readonly_tools.py::ExcludedDirectoryPolicyTest` 5 条。关键在于**每个被排除目录里都放同名
标记文件**，所以能证明"walk 根本没进去"，而不是"只是没提到"：

* 递归列目录不含任何被排除目录及其子项
* `find_files` 不返回被排除目录里的文件
* `search_text` 只命中真实源码（标记只存在于 `src/app.py` 与被排除目录里）
* **显式路径进入被排除目录仍能列出**（防止过度修复）
* 名单全小写（casefold 比较成立）

**对照（无修复的 HEAD，用 `PYTHONPATH` 强制换源码）**：**3 failed / 2 passed**，失败信息直接点名
旧代码漏掉的 `.agent`、`.gradle`、`.vscode` —— 与上表分析逐一吻合。修复后 5 passed。

### 真实运行验证

同一个 `/tmp/tsm-real-1` 项目（含 `.agent/`）、同一类提示词：

| | 修复前 | 修复后 |
|---|---|---|
| 工具结果 | 返回 `.agent` + 7 个 `.db*` 条目 | 只返回项目文件（4 项证据）|
| 回答里出现 `.agent` | 有（逐条点评 DB 文件）| **0 次** |
| 输入 token | 29,476 | **16,420（−44%）** |
| 模型调用 | 3 | 2 |

### 未做（即 O2，仍待决定）

* **ripgrep 支撑 + `.gitignore` 语义**：本机**没装 `rg`**，且引入外部进程会触碰"搜索工具是纯 Python R0、
  不需审批"这一现状。要做需要单独决策。
* **敏感文件名单**（`_SENSITIVE_NAMES` 在 workspace/onboarding/core_tools/provider/local_flow_export
  共存 5 份）**刻意未合并**：它是"文件是否敏感"，与"目录是否该遍历"是两件事，合并可能让某条路径
  的防护变弱。若要统一应作为独立任务（安全相关）。

## A.1w O2a 落地：ripgrep 作为**可见性过滤**（不是取数来源）+ doctor 探测

**结果：1255 passed / 12 skipped / 0 failed。**

### 第一版设计是错的，被测试当场否掉

最初把 `rg --files` 当作**文件清单来源**（三个 handler 共用）。**强制走 rg 路径**后
`tests/test_core_readonly_tools.py` **5 条失败** —— 因为 `_walk_searchable` 有一条**刻意的排序**
（注释：*"Do not exhaust the alphabetically first source root in a monorepo. Round-robin lets
codeBase/modules/packages all contribute candidates"*），而 `rg --files` 给的是它自己的遍历顺序。

⇒ **用 rg 取代 walker 会破坏排序契约**（`test_workspace_search_scans_source_roots_before_docs`、
`test_workspace_search_round_robins_multiple_source_roots` 正是钉住它的）。

### 最终设计："walker 给顺序，ripgrep 给可见性"

* walker 仍是**结构与顺序**的唯一来源（源码根优先、多根轮转、目录项、`generated_directories_skipped` 计数全部不变）
* ripgrep 只**剔除 `.gitignore` 掉的文件** —— 这是 walker 原理上不知道的事
* **两条策略必须同时生效**：命令是 `rg --files` **加** `--glob '!<EXCLUDED_DIRECTORIES>'`。
  否则 `build/`/`dist/`/`vendor/`/`out/` 这类**既非隐藏、也没被 gitignore** 的目录会
  **把 A.1v 的 O1 修复整个退回去**
* 无 rg → 不启用过滤 → 行为与 O1 完全一致（不存在降级风险）
* 显式指定路径（`search_text(path="generated/x.py")`、`list_files(path=".agent")`）**永不被过滤**：
  过滤只作用于"自动发现"，不封锁定向访问
* 结果新增 `ripgrep_filter: bool`，让调用方能看出这一份结果用了哪套策略

**对照（两种模式各跑一遍）**：默认 57 passed / 强制 `TSM_AGT_RIPGREP=/opt/homebrew/bin/rg` 41 passed，
**逐条一致**。

### 解析器放在 `core/configuration.py`，因为架构门禁拦了我的第一版

第一版把 `ripgrep_binary()` 定义在 adapter 里、`cli_setup.py` 直接 import 它 ——
`tests/architecture/test_dependencies.py::test_only_composition_root_imports_concrete_adapters`
立刻失败（只有 `bootstrap/composition.py` 能 import 具体 adapter）。

改为把解析器放**共享层** `core/configuration.py`（`ripgrep_binary()` + `RIPGREP_ENV`）：
adapter 与 doctor 都从那里取，**同一个实现**，且 `adapters → core`、`cli_setup → core` 都是合法方向。
不猜绝对路径：先读 `TSM_AGT_RIPGREP`，再 `shutil.which("rg")`。

### doctor 报告

```
[PASS] ripgrep: /opt/homebrew/bin/rg (.gitignore-aware search enabled)
[WARN] ripgrep: not found; search prunes state and generated directories but
       cannot see .gitignore entries
       fix: install ripgrep, or set TSM_AGT_RIPGREP to its path
```

`required=False` —— 可选依赖不该让安装变成 unhealthy（第一版漏了这个参数，2 条 doctor 测试因此变红）。

### 测试

新增 `RipgrepVisibilityFilterTest` 4 条，用**桩 rg 脚本**而不是真 binary，所以它验证的是**我们的接线**
（解析可执行文件、传 `--files` + 排除 glob、解析 stdout、套用过滤），不依赖本机装没装 rg：

* 被 gitignore 的文件在 rg 说话时消失
* 命令行确实带 `--files` 与**每一个** `!<EXCLUDED_DIRECTORIES>` glob（钉住"两条策略同时生效"）
* 无 rg 时 walker 的结果照旧（并**明确断言**：`generated/` 此时仍可见 —— 把 O2a 补上的缺口写进测试）
* 显式指定的被忽略文件仍能被搜到

### 本批我自己犯的三个错（都记下）

1. **按文本边界删块，连常量一起删了**：删 `#: ripgrep is optional.` 到 `def _ripgrep_exclude_globs()`
   之间，把 `_RIPGREP_TIMEOUT_SECONDS` / `_MAX_RIPGREP_ENTRIES` 一并删除 → 运行时 `NameError`
   （不是导入期，所以 `py_compile` 看不出来）。**又一次"删除块要先看区间内所有定义"。**
2. **改了引用忘了加 import**：`cli_setup.py` 调 `ripgrep_binary()` 但没有 import → doctor 运行期
   `NameError`。同样 `py_compile` 通过。
3. **漏 `required=False`** → 2 条 doctor 测试红。

### 未做的验证（环境原因，非代码）

真实运行验证**跳过**：模型端点此刻严重变慢 —— TCP 连接 8s 超时、最小真实调用 **77.26s**
（此前 6.17s，慢约 12 倍）。一次 agent 回合要 2–3 次模型调用，600s 内跑不完。
**这不是代码挂起**：判定依据是连通性与最小调用实测。O2a 的证据由桩测试 + 双模式对照给出。

## A.1x 修掉 O2a 引入的回归：必须带 `--hidden`（否则隐藏的**项目**文件全消失）

用户问"搜索工具还有哪些没完成"，我在清点时用真 rg 在**真 git 仓库**上验证，发现**我自己刚引入的
回归**：`rg --files` 不加 `--hidden` 时，ripgrep 会跳过**所有**隐藏路径，于是
`.github/workflows/ci.yml`、`.eslintrc.json`、`.gitlab-ci.yml`、`.env.example`、`.editorconfig`
—— 对编码 agent 恰恰是最常被问到的文件 —— 会从 `list_files` / `find_files` / `search_text`
里**静默消失**。这比原先的 `.agent` 污染更糟。

而单加 `--hidden` 又不安全：实测在真 git 仓库里 `rg --files --hidden` 会**漏出 18 个 `.git/` 条目**。

**正确组合（已实测）**：`--files --hidden` **加上** `EXCLUDED_DIRECTORIES` 的全部取反 glob。
三者同时成立：

| 检查 | 结果 |
|---|---|
| 隐藏的**项目**文件（`.github/workflows/ci.yml`、`.eslintrc.json`）| **保留** ✓ |
| `.git/` | **未漏出**（由 `!.git` 剪掉）✓ |
| `.gitignore` 掉的 `generated/art.py` | **仍被忽略** ✓ |

工具层端到端实测：

```
列出: ['.eslintrc.json', '.github', '.github/workflows', '.github/workflows/ci.yml',
       '.gitignore', 'generated', 'src', 'src/app.py']
搜索 NEEDLE: ['src/app.py']        ← generated/art.py 被过滤
```

**注意**：`.gitignore` **只在 git 仓库里生效** —— 我在非 git 目录做第一次探针时
`generated/art.py` 照样出现。所以这个特性在非 git 工作区只贡献"隐藏文件可见性"，
不贡献 gitignore 语义（记录在案，避免日后误判）。

测试同步加固：`test_the_command_gets_files_mode_and_every_exclude_glob` 现在同时断言
`--files`、**`--hidden`**、以及每一个 `!<EXCLUDED_DIRECTORIES>` glob —— 三者缺一都会红。

**教训（本会话第 7 次同类）**：引入一个"看起来正确"的能力时，**必须把它放到真实环境形态里
验证**（真 rg + 真 git 仓库），而不是只跑桩测试。桩测试全绿，真环境却会静默隐藏 CI/配置。

## A.1y web 搜索对齐（A）：引入搜索模式 + 失败显式

对比三家源码后按用户选择做 A 段（**对齐 Codex 的 `WebSearchMode` + Hermes 的 `success: false` 语义**）：

| | Codex | Hermes | tsm-agt 改前 | 改后 |
|---|---|---|---|---|
| 执行位置 | hosted（服务端） | 客户端 | 客户端 | 客户端 |
| 模式 | `Disabled/Cached`(**默认**)/`Indexed/Live` | 无 | 只有 Tavily 开关 | **`off`/`cached`(默认)/`live`** |
| 默认姿态 | **不主动出网** | 按凭据挑 | **默认就抓公开端点** | **不主动出网** |
| 全空/故障 | — | 顶层 `success: bool` | `ok=True` + 空列表 | **`ok=False` + 明确 error_code** |

落地：
* `search_configuration.py`：新增 `TSM_AGT_SEARCH_MODE`（`off`/`cached`/`live`，默认 `cached`），
  非法值记 issue 并留在默认；`sources` 记录 `search.mode` provenance。
* `retrieval/pipeline.py`：`search(..., allow_egress=False)` → 只读进程内缓存，未命中返回
  新标签 `cache-miss` 且**不写缓存**（未命中不是结果）。
* **顺带修一个 pre-existing bug**：降级/故障响应过去也会被缓存，导致 live 失败一次后**永不重试**、
  且 `cached` 模式会把瞬时网络故障当成"已 settled 的答案"。改为**故障不缓存**；"所有 provider 都
  应答但确实没匹配"仍会缓存（那是真结果）。
* `network_tools.py`：`search_mode` 参数 + 构造期校验；`web.search` 分支
  - `off` → `SEARCH_DISABLED`
  - `cache-miss` → `SEARCH_NOT_CACHED`（消息里点名 `TSM_AGT_SEARCH_MODE=live`）
  - 有 provider 报 error/timeout 且无结果 → `SEARCH_UNAVAILABLE`（`retryable=True`，消息列出失败后端）
  - 全部 `empty` 且无结果 → **仍是成功**（"确实没有结果"是合法答案，不能和"搜不到"混为一谈）
* `composition.py`：两个 `NetworkToolProvider` 站点都传 `search_mode=`。

测试 +12：模式三值、默认 `cached` **且断言不得开连接**（把 `urlopen` 换成会抛的桩）、`off` 拒绝、
非法模式构造期拒绝、四种结果映射、管线层 cached 命中/未命中/故障不缓存/真空会缓存。
**并改掉一条钉住旧错误行为的测试**（它把全后端超时断言为 `ok=True`）—— 正是本次要修的东西。

1267 passed / 12 skipped / 0 failed。

**行为变化须知**：默认 `cached` 意味着**开箱即用时 `web.search` 会以 `SEARCH_NOT_CACHED` 明确拒绝**，
直到设置 `TSM_AGT_SEARCH_MODE=live`。这是对齐 Codex 默认姿态的代价，已在错误消息里点名该设置。
