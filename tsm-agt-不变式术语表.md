# tsm-agt 不变式术语表

> **为什么存在这份文件**：27 份 SPEC 已按决定从仓库删除，但**代码里仍有 40 处引用这些记号**
> （`INV-3` 7 · `INV-10` 5 · `INV-13` 5 · `INV-14` 5 · `AG-11` 3 · `P0/P1` 3 · `SPEC P2` 2 · `ProofEnforcementMode` 10）。
> 本文只保留**记号的定义**，不保留任何 SPEC 正文、方案或实施计划。
> 定义原文取自 `Artifact Graph与Proof Obligation任务证明闭环实施SPEC.md` 与 `完成判定反面漏洞修复SPEC.md`（均已在 HEAD 中删除）。

---

## 1. 架构不变量 `AG-*`

> 出处：`Artifact Graph…SPEC.md` §2.3 核心不变量

| 编号 | 不变量 |
|---|---|
| **AG-1** | 完成由 Proof Obligation 覆盖挣得，不能由 Worker 文本、工具成功标志或 UI 状态声明。 |
| **AG-2** | 每一条 required obligation 必须可追溯到 Task Contract 中的一条需求、验收 criterion 或 required outcome。 |
| **AG-3** | Evidence 只在它声明的能力、Scope、Artifact 版本与关系均匹配时，才能覆盖 obligation。 |
| **AG-4** | Artifact 被写入、删除或版本变化后，依赖旧版本的 Evidence 必须标为 stale，不可继续覆盖。 |
| **AG-5** | 未绑定 obligation 的发现型操作可以扩展 Artifact Graph，但不能关闭 required obligation。 |
| **AG-6** | `SUCCEEDED` 必须蕴含所有 required obligation 均为 `COVERED`，且现有确定性验收通过。 |
| **AG-7** | Runtime 资源耗尽、能力缺失、证据冲突或 Scope 不完整时，必须进入 `NEEDS_REVIEW` 或可恢复执行，不能伪造成功。 |
| **AG-8** | 独立小模型只能复核已结构化的合同/义务/图/证据包；它不能读取 Worker 私有上下文、写工作区或直接改变终态。 |
| **AG-9** | **不可关闭的 required 要求不得静默降级。** 若一条 required 要求**能追溯到真实 Contract 要求**（criterion / outcome / required-effect）却在当前能力清单下无法关闭，必须进入 `UNFULFILLABLE_REQUIRED → NEEDS_REVIEW`（交人复核），**绝不**把它悄悄改成可选项——那等于把用户的要求丢掉并可能报成功。只有**编译器自己过度发挥造出来的**（无法追溯到 Contract）才允许不产生，且应由编译期修正而不是运行期降级。 |
| **AG-10** | **obligation 身份确定性且语义敏感**：`obligation_id` 由 `(contract_ref, predicate, 规范化要求内容摘要, 去版本化 selector)` 确定性派生。**必须包含要求内容摘要**（否则要求改了编号不变、旧证据错误覆盖新要求）；**selector 不得包含版本/hash/artifact_id**（否则每改一次文件编号就变，清单爆炸、证据无法累积）。TaskSpec 修订只允许新增或退役 obligation，不复用旧 id 表示不同要求；数量有界。 |
| **AG-11** | **Coverage Gap 就是就绪 Gap**：required coverage 缺口必须作为既有 `CompletionReadiness` 的 **required `CompletionGap`** 产出（INV-13 / INV-16）。**不得新增第二套就绪事件**，也不得绕过门直接进入验收或终态。 |
| **AG-12** | **`FAILED` 只表示确定性反证**（要求的命令验证失败、要求的副作用被明确证伪、工作区完整性不符）。**"没有记录 / 未发生 / 未观测"在覆盖层是 `MISSING`，流向 `NEEDS_REVIEW`，不得被当作反证。** |
| **AG-13** | **零结果需要可辩护的 Scope**：`QUERY_CARDINALITY` 判定"应为 N 个"（含 N=0）时，Scope 根必须是**已观测的 Artifact**，枚举必须 `complete=true`，且 Scope 的扩大/收窄必须留下事件。否则一律 `BLOCKED`，不得据零结果宣布"不存在"。 |
| **AG-14** | **归属可以多、充分性必须严**：一次 Observation 可以同时归属到**所有**能力/版本/Scope 兼容的 obligation（不要求唯一匹配），但是否 `COVERED` 必须由独立的 Sufficiency 判定给出。**窄证据不得关闭宽义务**：读取一个文件不能关闭"整个 Scope 都满足"的义务；枚举 `complete=false` 不能关闭任何要求完整性的义务。 |
| **AG-15** | **编译器只消费结构化字段**：编译 Proof Plan 时只能读取 TaskSpec 的结构化字段（`verification_kind`、`evidence_reference` 前缀、outcome `kind`、`required_effects`、`atomic_action`）。**禁止解析 `description` 等自然语言**来决定 predicate 或 selector。散文类判据只能编译为 advisory，或（仅 `RUBRIC` / `GOAL_ALIGNMENT`）编译为 `SEMANTIC_REVIEW`。 |
| **AG-16** | **终态映射唯一**：verdict（判定结论）→ Task 终态的映射必须是**单一函数**，由 CLI / SDK / Web / Local API 共用。同一个判定结论在任何入口必须得到同一个终态；**尤其 `BLOCKED` / 不可关闭 / 需复核类结论不得在某些入口被报成 `FAILED`。** |

## 2. 就绪不变量 `INV-*`

| 编号 | 不变量 | 出处 |
|---|---|---|
| **INV-3** | **required gap 必须可关闭**。判据（P1.5 升级后）：**`effective_required_effects` 为空 _且_ 该 gap 无法追溯到 Contract 来源** → 降级为可选，并记 `completion.invariant_violated`。**只降级编译器/运行期自己造出来的、无 Contract 来源的 gap**；能追溯到 Contract 的必须保持 required，走有界逃生。豁免集合：`PROOF_COVERAGE_*` 另立 `_ESCALATED_GAP_KINDS`（与 `_JUDGED_GAP_KINDS` **并列不合并**）；`UNRESOLVED_EFFECT_FAILURE` 已并入 `_JUDGED_GAP_KINDS`。原始触发案例：`task-2c8f8fbb75714516a94ab442995626b5` 的死锁。 | AG SPEC §4.5.1 / §12.6 D3；`kernel.py:8298-8324` |
| **INV-4** | **有界逃生**：不要永远重试同一个无法满足的要求。到 stall cap 由 `_finalize_needs_review` 转入 `NEEDS_REVIEW`。 | `完成判定反面漏洞修复SPEC.md:340,393` |
| **INV-10** | **判据无判官 = 配置 gap**：Task SPEC 含 judged criterion（`RUBRIC` / `GOAL_ALIGNMENT`）但未配置 rubric judge 时，该判据**既不能通过也不能被工作关闭**。它是**配置 gap 而非工作**，因此**不得被 INV-3 降级**；Task 经 stall cap 有界进入 `NEEDS_REVIEW`。`gap_id="judged-criterion-without-judge"`，`kind="JUDGED_CRITERION_WITHOUT_JUDGE"`，`required=True, recoverable=False`。**在 per-turn 门也生效。** | AG SPEC §4.5；`kernel.py:8274-8296` |
| **INV-13** | **完成由 gap 集合决定（completion is gap-determined）**：`action == COMPLETE ⇒ 无 required gap`。计数器（continue / disclosure / stalled / renewal）只能在"继续 / 可恢复挂起 / 交人复核"之间选择，**永远不能产生"成功"**。⇒ 更准确的表述：**完成必须由正向证据"挣得"（earned by evidence），不能由标记"声明"（declared by a marker）。** | `完成判定反面漏洞修复SPEC.md:135,181` |
| **INV-14** | **终止权决定终态（stop authority decides the terminal state）**：**模型自行收尾**且必需工作缺失 ⇒ `FAILED`；**runtime 主动停下**且必需工作缺失 ⇒ `NEEDS_REVIEW`（三值语义下的"未决"，**不是产品失败**）。 | `完成判定反面漏洞修复SPEC.md:136` |
| **INV-15** | **动作→结果全覆盖（total action→outcome mapping）**：每个 readiness action 必须**恰好映射到一个出口**——final answer / continue / resumable suspend / blocked suspend / human review。**不存在"落到循环尾部"的动作。** | `完成判定反面漏洞修复SPEC.md:137` |
| **INV-16** | **验收层不得是"未完成"的第一个发现者（acceptance is never the first detector of open work）**：只要还有 required 工作未完成，就必须由**就绪层/目标层**先记录并给出可恢复状态；验收层（PASSED/FAILED 词表）只应看到"自称已完成"的任务。**若验收层成为第一个发现者，说明状态分层失效。** | `完成判定反面漏洞修复SPEC.md:138` |

**层级关系**（`完成判定反面漏洞修复SPEC.md:152`）：*"INV-13 是 L1+L2 的通用形式，INV-14 是 L4 的边界，INV-15 是 L3 的通用形式。"*

## 3. 逃生链（一次自我终止的完整路径）

```
EXHAUSTED
  → incomplete_recovery_boundary              （kernel.py:14046,14070）
  → _suspend_incomplete_recoverable           （kernel.py:14523）
  → 有界可恢复挂起
  → 到 stall cap 由 INV-4 转 NEEDS_REVIEW      （_finalize_needs_review）
```
出处：`完成判定反面漏洞修复SPEC.md:340`。**"不引入新终态"**是这条链的约束。

## 4. 灰度开关与阶段

| 记号 | 定义 |
|---|---|
| **`ProofEnforcementMode`** | 三值：`OBSERVE`（**只记录不拦截**，生产默认）、`ENFORCE_NEW_TASKS`（**需显式开启**）、`ENFORCE_ALL`（另行评审）。模式变化**必须写 `proof.enforcement_changed` 事件**，便于事后区分"当时是否强制"。 |
| **P0** | 只编译**结构化字段**（AG-15）；散文类判据降为 advisory。观察模式。 |
| **P1** | 观察模式；P1.5 为 INV-3 判据升级与 exemption 收敛的必做内容。 |
| **P2** | **只对新 Task 强制**。 |

出处：AG SPEC §4.4、§8、§11。配套指标：`advisory_obligation_rate`。

## 5. 与 Hermes 的关系（为什么这些记号不是凭空发明的）

`完成判定反面漏洞修复SPEC.md:498` 记载外部参考：**Hermes #122173 / #122176；Codex `TurnStatus` × `ThreadGoalStatus`**。

其中 `INV-14`（终止权决定终态）与 Hermes 的"预算耗尽**不是**失败"（`max_iterations_reached` / `budget_exhausted` **不在** `_EXIT_REASON_FAILURES` 表内）是**同一个结论**，两边独立得出。参见 `hermes-架构梳理/03-循环与出口.md`。

## 6. 一条已记载的现存缺陷（AG-16 违反）

> `Artifact Graph…SPEC.md:1016` 原文：
> *"其中 CLI/SDK 映射**是一个可验证的现存 bug**：**`cli.py` 把任何未通过验收映射为 `FAILED`**，`sdk/runtime.py` 已把 `BLOCKED` 映射为 `NEEDS_REVIEW`"*
> 处置：*"**采纳**：…终态映射单一化（R2-5 / AG-16 / §6.3）；**CLI 修复不依赖本 SPEC，建议先独立落地**"*

**这与独立审计数据吻合**：161 个 `FAILED` 任务里 **115 个（71%）是 `AcceptanceStatus.BLOCKED → FAILED`**。按 **AG-12**（`FAILED` 只表示确定性反证）与 **AG-16**（终态映射唯一），这 115 个的正确终态应是 `NEEDS_REVIEW`。

⇒ **这是一个定义明确、边界清晰、可独立落地的修复，且不依赖任何已删 SPEC。**

## 7. 实施状态（删除前记载）

| 项 | 内容 | 不变量 | 状态 |
|---|---|---|---|
| F1 | 动作词表补 `EXHAUSTED` | INV-13 | ✅ 已实施 |
| F2 | Policy 改 gap-determined 四分支，删 in-turn 计数器 | INV-13 | ✅ 已实施 |
| F3.1 | 消费者侧归一化：`COMPLETE ∧ required gap ⇒ EXHAUSTED` | INV-13/16 | ✅ 已实施 |
| F3.2 | `EXHAUSTED` 接回 `_suspend_incomplete_recoverable` | INV-14/16 | ✅ 已实施 |
| F3.4 | 续量判定去 `reason` 字符串，改判 `action is EXHAUSTED` | INV-15 | ✅ 已实施 |
| F3.5 | 工具 `effective_recovery_kind` 穿进 `ExecutionFact` → `CompletionGap.recoverable` | INV-14 | ✅ 已实施 |
| F6 | 删除 in-turn 纠正计数器 | INV-13/14 | ✅ 已实施 |
| F4 | 判据与 gap 同源 | — | ☐ 暂不做 |
| F5 | disclosure 轮允许工具 | — | ☐ 已降级为产品取舍 |

`完成判定反面漏洞修复SPEC.md` 的契约用例 `tests/test_completion_readiness.py`：**31 passed**。

---

## 8. 边界说明

* 本文**只保留定义**，不含 SPEC 的方案、实施步骤、风险矩阵与评审记录——那些随 27 份 SPEC 一并删除。
* `INV-3` 与 `AG-9` 是**同一件事的两面**（`AG-9` 是编译期、`INV-3` 是运行期）。
* 未能找到完整定义的记号：`INV-1` / `INV-2` / `INV-5`~`INV-9` / `INV-11` / `INV-12`（在已删文档中未出现或被引用）。**若代码后续引用到它们，需重新定义。**
