# 移除证明层（对齐 Hermes / Codex 语义）实施方案

> 决定：**不保留证明层**。目标语义与 Hermes/Codex 一致：
> 模型不再调用工具 ⇒ 回合结束 ⇒ 任务完成。唯一的"不成功"是异常或中止。
>
> 本文件只回答"怎么安全地删"。不再论证要不要删。

---

## 0. 必须你先定的一个岔路：删掉之后，**谁写 `SUCCEEDED`？**

删掉 `finalize_acceptance` 之后，今天写终态的唯一入口就没了。`TaskState.SUCCEEDED`
必须有人写，否则任务会永远停在 `EXECUTING`（这正是今天 `OBSERVE_ONLY` /
`AGENT_DECIDES` 两个模式的状态：**能返回结果但不闭合任务**，见 `sdk/runtime.py:1334-1337`）。

| 选项 | 做法 | 代价 |
|---|---|---|
| **a（推荐）** | SDK/CLI 收到 `AgentTurnResult` 时直接 `EXECUTING → FINALIZING → SUCCEEDED` | 需确认 `LEGAL_TRANSITIONS` 允许这条边（现在多半要求先过 `VERIFYING`） |
| b | 保留 `VERIFYING` 作为**不做任何判定的空转状态** | 最小改动，但留下一个没有意义的词汇 |
| c | 从状态机里删掉 `VERIFYING` / `FINALIZING` / `NEEDS_REVIEW` | 最彻底，改动最大；`NEEDS_REVIEW` 还挂着人工复核 API |

**只有你定了这个，后面的删除才有明确的落点。**

---

## 1. 删除顺序（每步独立可回退、每步全量绿）

### S1 —— 切断权威（行为立即达成，层变惰性）
* `sdk/runtime.py:1321-1337`：删 `LEGACY_GATE` 分支，改为按 §0 选定的终态路径。
* `cli.py:588-645`：同样合一。
* 效果：`verify_task_acceptance` / `finalize_acceptance` 不再被生产调用；证明层仍然计算、
  仍然记录，但**不再影响任何决定**。
* 验收：一次"猜错路径"的任务**正常结束且为 SUCCEEDED**；真实台账那种分析任务不再挂起。

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
