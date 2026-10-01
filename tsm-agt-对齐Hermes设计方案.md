# tsm-agt 循环对齐 Hermes / Codex 的设计方案

> ## ⚠️ 本文提到的终态判定机制已删除（2026-10）
>
> 文中 `TERMINAL_STATE_BY_VERDICT`、`finalize_acceptance`、`turn_finalizer`、
> `AcceptanceStatus → NEEDS_REVIEW/FAILED` 映射、以及"回合结束 → 验收 → 终态"这条链
> **已全部从代码删除**。当前完成语义是**模型停止调用工具即结束回合**（正是本文主张的
> Hermes/Codex 形状），唯一"领域已验证"标记来自人工复核 accept。
> 执行记录见 `tsm-agt-移除证明层方案.md` A.1h–A.1m。
> **本文保留原文作为设计依据；文中描述的行为不再生效。**

> 依据：本会话对两份源码的实测（Hermes `824f2279d`、Codex `4ef836f883`），
> 以及 tsm-agt 真实台账（687 Task / 114,840 事件）的核对。
> 所有 `path:line` 都是读过的，不是推断。

---

## 0. 一句话

**对齐循环，保留证明层——但把证明层从"控制器"降为"审判者"。**

| | Hermes / Codex | tsm-agt 现状 | 目标 |
|---|---|---|---|
| 回合何时结束 | 模型不再调工具 | readiness `COMPLETE` 且必需 gap 为空 | **模型不再调工具** |
| 失败调用的后果 | 一段错误文本回灌 | 产生 gap，**能阻止结束** | 产生 gap，**不能阻止结束** |
| 谁判"做成了" | 模型自己（机械判据） | 运行时证明层 | **运行时证明层（故意不同）** |
| 预算耗尽 | 停止 / aborted | **写 FAILED** | 停止（不是产品失败） |

---

## 1. 必须放弃的：证明层对"停"的指挥权

Hermes 唯一的证据门（verify-on-stop）**只看"改了代码但还没有新验证"**，上限 2，且
被两次配置迁移默认关掉（`hermes_cli/config.py:5473-5514`，理由 "more noise than signal"）；
Codex 没有证据门。失败的调用在两边都只是数据。

tsm-agt 让失败产生的 gap 能**阻止回合结束**（`turn_phases.py:972-1021` 挂起、
`:738-783` 重试批次挂起），这是两个框架都没有的机制，也是
`task-8d1ec232a0e54910852a029dcf11b03f`（一次猜错路径就把分析判死）的成因。

**设计决定**：回合结束只由"模型停止调用工具"决定。gap 仍然记录、仍然参与验收，
但**不再有权力让回合继续或挂起**。

## 2. 必须保留的：证明层决定终态

不放弃这一条，因为它是 tsm-agt 唯一的差异化价值，也是你台账里
"无法判定被报成失败"从 **115 → 0** 的原因：

```python
# core/verification.py:32-41  TERMINAL_STATE_BY_VERDICT
PASSED        → SUCCEEDED
BLOCKED       → NEEDS_REVIEW     # "Cannot decide" is not "the work failed"
FAILED        → FAILED
NOT_APPLICABLE→ NEEDS_REVIEW
```

Hermes / Codex 连"目标达成了吗"都不问（Hermes 的 `completed` 只查
"有无最终文本 + 有无异常"，`agent/turn_finalizer.py:124-132`）。它们把验收权放在
**循环之外**：终端前的人看得见 diff，运行时只负责"有答复、没报错"。

tsm-agt 的场景是**委派出去、没人在旁边看**，所以"模型说完成"不构成可接受的验收权威。
**这一条是故意不对齐**，并且应该写进 README 作为设计前提。

---

## 3. 六步改动（顺序不可换）

### 步 A —— 预算耗尽不等于失败（独立、最优先）

```python
turn_phases.py:1064/1074  超限 → raise AgentLoopLimitExceeded
kernel.py:5786            _record_agent_loop_failure → transition(TaskState.FAILED)
```

Hermes 撞上迭代上限时，只要最后是正常文本答复**仍算 completed**；Codex 是 aborted 或
带 error 的 completed。**都不是"产品失败"。**

tsm-agt 自己的词汇表已经写好了诊断：`turn_outcome.py:137-140` 把
`max_model_calls` / `max_tool_calls` 归为 `BUDGET_EXHAUSTED → interrupted`，并有
`FAILED_STATE_WITH_UNDECIDABLE_REASON` 检测。真实台账里这两个理由各出现 **4 次 / 1 次**。

* 改：`_record_agent_loop_failure` 的终态从 `FAILED` 改为可恢复边界，并让
  `sdk/runtime.py` 的 `_is_resumable_stop` 认它。
* 验证：撞上限的任务不再是 `FAILED`；`turn_outcome` 的一致性检测不再报
  `FAILED_STATE_WITH_UNDECIDABLE_REASON`。

### 步 B —— 给 `while True` 一个单点守卫

Hermes 的循环头是有界判断（`conversation_loop.py:613`）；tsm-agt 是
`while True`（`kernel.py:4927`），限额分散在相位里（`turn_phases.py:117`、`:1064`、`:1074`），
终止正确性依赖"每条相位路径都记得检查"。**我实测过一次由此产生的挂死。**

* 改：把"还能不能再开一轮"收敛成一个谓词（读 checkpoint 的计数），在循环头求值。
* 验证：任何"返回 CONTINUE 但不消耗预算"的路径都不再可能不收敛。

### 步 C —— 失败不再阻止结束（对齐的核心）

两处：

| 位置 | 现状 | 改为 |
|---|---|---|
| `turn_phases.py:972-1021` | 不可关闭/耗尽的必需 gap → 挂起 `AWAITING_USER` | 回合结束 → 验收 → `NEEDS_REVIEW` |
| `turn_phases.py:738-783` | 需要恢复的工具批次 → 挂起 | 把失败结果回灌给模型、继续（Hermes：任何工具调用都 `continue`） |

**已知代价（实测过）**：这两处是 INV-4 升级链（挂起 → 用户延续 2 次 →
`stalled_continuations ≥ 2` → `NEEDS_REVIEW`）**唯一**的入口。
`tests/test_completion_stall_terminal.py` 的 `_suspend_once` 刻意用"工具全无 ⇒ 不可关闭"
进挂起，所以要**把 fixture 改成可关闭的 gap**（链本身不关心可关闭性），
**断言一行都不用改**。

* 验证：猜错路径不再让任务挂起或失败；`task-8d1ec232…` 那种输入下，回合正常结束且
  终态是 `NEEDS_REVIEW`。

### 步 D —— 每个 nudge 都有自己的上限（补完步 1）

`109c9f9` 只修了一半：`policy.py:111-121` 的封顶**只在 `successful_tool_calls == 0` 时**生效，
所以一个"成功但无用"的调用序列仍可把 nudge 发到 40→58。Hermes **每一个** nudge 都封顶
（verify 2、pre_verify 3、intent 2、thinking 2、empty 3、length 3、bad tool 3、bad JSON 3）。

* 改：把封顶判据从"零成功"改为**"必需 gap 集合没有进展"**——即把 INV-4 已经在用的
  跨回合规则（`kernel.py:5117` `_continuation_made_no_progress`）**搬进回合内**。
* 这样同时满足两条既有规格：有进展 ⇒ 继续（`test_..._with_tool_progress_keeps_continuing`）；
  无进展 ⇒ 封顶。
* 验证：同一条输入下，nudge 次数与模型调用次数都收敛到常数。

### 步 E —— 词汇表收敛

1. **三模式矩阵 → 一种行为**，删 `CompletionReadinessMode` 与
   `TSM_AGT_COMPLETION_READINESS_MODE`。理由：它是**迁移期的灰度开关**
   （`tsm-agt-完成判定机制.md` §7 已诊断："旧通道/新通道并存，切换开关 =
   `proof_enforcement_mode`"），不是产品配置；而且只有 `LEGACY_GATE` 会走
   `finalize_acceptance`（`sdk/runtime.py:1323`）——两种模式**根本不闭合任务**。
   *连带*：`cli.py:588-645` 与 `sdk/runtime.py:1321-1337` 合一条路径。
2. **`ToolRecoveryKind` 六值 → 两值**。Hermes **零个**失败分类（错误就是文本，
   `display.py:1202` 只为 UI 分类），Codex **两个**（`FunctionCallError::{RespondToModel, Fatal}`）。
   gap 不再拦停后，六值里大部分失去策略权重，收敛到
   {模型可自行修正 / 需要人} 即可。
3. **删三个死值**：`PhaseAction.BREAK`（无人设、驱动也不处理）、
   `REPORT_BLOCKED` 与 `REPORT_INCOMPLETE_RECOVERABLE`（唯一内置策略从不产生）。
   这三者直接违反 Hermes `AGENTS.md` 的 *"no flags nobody sets"*，而且是
   判断"哪些路径是活的"时最容易骗人的东西（我前几轮就被它们误导过）。
4. **删续期**（`DiminishingModelCallBudgetPolicy`，`turn_phases.py:829-894`）：两个框架都没有；
   Hermes 是平的 90、Codex 是会话 token 预算。只有 1 个调用点。若平预算不够用，
   就把 `max_model_calls` 调到合理值（对齐 Hermes 的 90）。
5. `max_tool_calls` **保留**（防工具扇出失控，可辩护），但**超限不得写 FAILED**（步 A 覆盖）。

### 步 F —— 记录故意的不对齐

在 README / `AGENTS.md` 写明：tsm-agt 与 Hermes / Codex 的**唯一**设计差异是
"成功需要证明"，理由是委派场景下没有人类验收者。其余循环语义对齐。

---

## 4. "对齐完成"的可验收标准

1. 猜错一个路径**最多只损失一次工具调用**，不能让任务挂起或失败。
2. 撞上模型/工具上限的任务**永远不是 `FAILED`**。
3. 不存在"任务停在等待输入、而用户输入在原理上无法帮助"的状态。
4. `SUCCEEDED` 仍然只能由通过证明的判定产生（**不变**）——`terminal_state_for` 是唯一入口。
5. 完成策略**没有任何配置面**；只有一种行为。
6. 循环里没有"定义了但无人设／无人处理"的值。

## 5. 每步的纪律（本会话已付过学费）

* **禁用多行字符串拼接**：一律按行号切片 + **逐行内容断言**（我因此误删过 753 行）。
* 改完先 `ast.parse`，再跑聚焦测试，最后跑全量；**注意超时**（我因此挂死过一次，基线 27s）。
* **不先改规格**：先读相关测试，判断"仍成立 / 是 fixture 要重指 / 才是规格改写"，
  三种情况在提交信息里分别写明。
* 一步一提交；出问题 `git checkout HEAD -- <文件>`，绝不留半套。
