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
