# tsm-agt 完成判定机制（M1 动作 1 / M2 的依据）

> ## ⚠️ 本文整篇描述的机制已从代码中删除（2026-10，证明层删除）
>
> 题目里的"完成判定"指 verdict（`AcceptanceStatus`）→ Task 终态的三值判定。该机制已删除：
> `Kernel.verify_task_acceptance`、`Kernel.finalize_acceptance`、`_evaluate_final_acceptance`、
> `core/verification.py` 的 `TERMINAL_STATE_BY_VERDICT`、`core/turn_finalizer.py`、
> `core/turn_outcome.py`，以及 readiness gap 生产、`ProofPlanCompiler`、
> `artifact_graph` / `proof_obligation` / `evidence_coverage` / `proof_projection` /
> `semantic_review` 五个模块，全部移除。
>
> **当前语义**：回合在**模型停止调用工具**时结束并写 `SUCCEEDED`（`Kernel.complete_task`），
> 没有任何"先证明再完成"的门；唯一的"领域已验证"标记来自人工复核 accept
> （`kernel_task.py:902`，`manual_verified: true`）。
>
> **本文仅作为历史依据保留** —— 当时的推理、事故分析与外部审计数据都在这里。
> 执行全过程与逐批证据见 `tsm-agt-移除证明层方案.md` A.1h–A.1m。

> **行号引用已失效（2026 更正中）**：本文写作时 `core/kernel.py` 尚未分解。
> 此后轮循环已抽为 `core/turn_phases.py` / `core/turn_tool_batch.py`，
> 并按 Hermes 门面+Mixin 形状开始分解（第 1 刀 `core/kernel_session.py`，
> 见 `tsm-agt-按Hermes重构kernel映射.md`）。文中 `kernel.py:NNNN` 一律不再
> 指向所述代码；请按同处已写出的**方法名**重新定位。
> 保留原行号是为了让这份记录仍可追溯到当时的代码。

> 来源：`src/tsm_agt/ports/completion_readiness.py`（176 行，**已通读**）+ `Kernel._completion_correction_message`、`Kernel._completion_readiness_gaps`、完成门与出口分支（后两者见下方横幅说明）。
> 结论：**完成判定本身是干净的（Port + 数据类），但它的"续跑"实现违反了 Hermes 的一条硬不变量。**

---

## 1. 判定词汇：5 个 action（`ports/completion_readiness.py:22-35`）

| action | 语义（原文要点） |
|---|---|
| `COMPLETE` | 提议的最终答复可定稿。**INV-13：只要还有 required gap 打开，就绝不能发出此 action——完成是由 gap 证据挣来的，不是由"有结论"宣布的。** |
| `CONTINUE` | 继续做 |
| `REPORT_INCOMPLETE_RECOVERABLE` | 报告"未完成但可恢复" |
| `REPORT_BLOCKED` | 报告受阻 |
| `EXHAUSTED` | 有界纠正预算耗尽或剩余 gap 无法用任何可用能力关闭。**INV-14：刻意既非成功也非失败——轮在有界可恢复处结束，未满足的要求继续在 goal 轴上跟踪。** |

**`EXHAUSTED` 的 INV-14 与 Hermes 完全同构**：Hermes 的 `max_iterations_reached` / `budget_exhausted` **不在** `_EXIT_REASON_FAILURES` 表内，所以 `failed` 保持 False（`03` 册）。两边独立得出了同一个结论：**预算耗尽不是失败**。

---

## 2. 计数器：**两个尺度**（`:102-107`）

```python
continue_attempts: int = 0            ┐
disclosure_attempts: int = 0          ├─ 轮内：一次尝试内限定纠正次数
automatic_resume_attempts: int = 0    ┘
last_action: str = ""
stalled_continuations: int = 0        ← 跨轮：同一 gap 集合可被重试的总次数
```

docstring 原文（`:96-99`）：

> *"``stalled_continuations`` counts **consecutive user continuations** that ended with exactly the same required gap set and no completed Outcome. It is the one counter that **deliberately survives a capacity replenishment**: the other three bound corrections **inside a single attempt**, while this one bounds how many times the same unsatisfied requirement may be retried **at all**."*

**⇒ tsm-agt 已经有"轮内预算"与"会话级停滞预算"的区分**，只是没有像 Hermes 那样放在会话对象上（Hermes：`GoalState.consecutive_parse_failures` + `turns_used`/`max_turns`，`04` 册 §4）。

---

## 3. gap 携带能力语义（这在 Hermes 里没有对应物）

```python
@dataclass(frozen=True, slots=True)
class CompletionGap:
    gap_id: str; kind: str; description: str; status: str
    required: bool = True
    recoverable: bool = False
    evidence_reference: str = ""
    expected_scope: str = ""
    required_effects: tuple[ToolEffect, ...] = ()      # ← 关闭该 gap 需要哪类能力
    candidate_tools: tuple[str, ...] = ()              # ← 具体候选工具
    observed: str = ""
```
`ports/completion_readiness.py:38-58`

`required_effects` 用 **`ToolEffect`**（`OBSERVE`/`MUTATE`/`EXECUTE`/…）而不是工具名 —— 与 `ToolSpec.effect` 同一套词汇，因此 gap 与工具面是**同一套语言**。这是 tsm-agt 比 Hermes 强的地方：**Hermes 的判官只说 `{"verdict": "done", "reason": "…"}`，说不出"需要哪类能力"**（`04` 册 §5）。

判定入口是 **Port**（可替换适配器）：

```python
class CompletionReadinessPolicyPort(RuntimeAdapter, Protocol):
    """Judge a proposed final answer from persisted, inspectable facts only."""
    async def evaluate(self, probe: CompletionReadinessProbe, state: CompletionReadinessState) ...
```
`ports/completion_readiness.py:171-176`

`CompletionReadinessProbe`（`:144-160`）传入：`goal` `gaps` `remaining_model_calls` `remaining_tool_calls` `available_read_tools` `forced_wrap_up` `evidence_item_count` `successful_tool_calls` `available_effects` `available_tools` `unresolved_failures`。
**判定只吃"持久化的、可检查的事实"** —— 这条纪律与 Hermes 的判官（只吃 goal + final_response）相比更严。

---

## 4. 关键冲突：纠正靠**合成 USER 消息注入轮内**

`Kernel._completion_correction_message`（`core/kernel.py`）全文要点：

```python
@staticmethod
def _completion_correction_message(decision) -> Message:
    body = {
        "boundary": "completion_readiness",
        "action": decision.action.value,
        "instruction": (...三种文案...),
        "gaps": [gap.to_data() for gap in decision.gaps],
    }
    return Message(
        f"completion-readiness-{uuid4().hex}", MessageRole.USER,          # ← 合成 USER 消息
        (TextBlock(json.dumps(body, sort_keys=True, separators=(",", ":"))),),
    )
```

三种 instruction：
* `CONTINUE`：*"The proposed final answer was not accepted. Perform the remaining required in-scope work now using an available capability or candidate tool listed in the gaps… **Do not merely offer to continue later.**"*
* `REPORT_INCOMPLETE_RECOVERABLE`：*"Do not call tools. Report that this Turn is incomplete but resumable, identify the last safe checkpoint and remaining required work, and do not describe it as blocked or successful."*
* `REPORT_BLOCKED`：*"Do not call tools. Give the user the exact blocker, completed evidence, and unverified requirement. Do not claim success."*

然后在 `:14097-14099`：

```python
if readiness.action is not CompletionReadinessAction.COMPLETE:
    correction = self._completion_correction_message(readiness)
    messages.append(correction)      # ← 注入后继续 while，转下一圈
```

### 这直接违反 Hermes 的消息流不变量

Hermes `agent/AGENTS.md:63-67` 原文：

> *"**Strict role alternation.** Never two same-role messages in a row; **never a synthetic user message injected mid-loop**. The one exception is ``/steer``, delivered as a **standalone user row after a tool result** … never smeared onto the already-persisted tool row, which append-only persistence would leave divergent from the live request."*

**tsm-agt 干的正是被明令禁止的那件事。** 后果是可预期的：
* 合成 USER 行会**落进 `messages` 并持久化**（`_commit_model_response_checkpoint` `:14050`），于是它成为"历史"的一部分；
* 而历史又会在下一次模型调用前被 `session-context-*` 重建逻辑牵扯（`kernel.py:13437-13450`）；
* 两者叠加 ⇒ **历史里混进运行时自己造的用户话**，这正是"多轮接不上"的第二个来源。

Hermes 的同位机制是 `evaluate_after_turn` 产出 `continuation_prompt` 后**塞回输入队列**（`gateway/run_goals.py:306-314`；`cli_loops_mixin.py:722-728`），**不碰 `messages`**。

---

## 5. 映射表：tsm-agt action → Hermes 形状（M1 动作 1 的可行性证据）

| tsm-agt（轮内） | Hermes（轮外） | 出处 |
|---|---|---|
| `CONTINUE` + `gaps`（带 `required_effects`/`candidate_tools`） | `should_continue=True` + `continuation_prompt` | `goals.py:1077` `_decision` |
| `REPORT_BLOCKED` | judge verdict `"blocked"` | `04` 册 §4 |
| `REPORT_INCOMPLETE_RECOVERABLE` | judge verdict `"wait"`（+ `waiting_*` 屏障，不耗轮次） | `04` 册 §4 |
| `EXHAUSTED` | `max_iterations_reached` —— **不在失败表内**（INV-14 同构） | `03` 册 |
| `stalled_continuations`（跨轮） | `GoalState.consecutive_parse_failures` + `turns_used >= max_turns` | `goals.py:1502` |
| `continue_attempts`/`disclosure_attempts`/`automatic_resume_attempts`（轮内） | `agent.max_iterations`（一轮内 API 预算） | `agent/AGENTS.md:15` |

**⇒ 外移是"映射"而不是"重写"**：5 个 action 全部能对上 Hermes 的既有形状，且 tsm-agt 的 gap 语义**更丰富**（Hermes 的判官说不出"需要哪类能力"）。

**唯一需要真正改的是**：把消费点从 `:14097`（append 合成消息 + 继续 while）搬到轮之后（产出 `should_continue` + `continuation_prompt`），并让 `_completion_correction_message` 的内容变成 **continuation prompt 的正文**，而不是一条合成 USER 消息。

---

## 6. 判定被调用两次（不只是出口）

| 时点 | 位置 | 用途 |
|---|---|---|
| **模型调用前** | `:13242 _completion_readiness_gaps` → `:13245 available_effects_for_turn` → `:13252 recoverable_required_work` → `:13275 can_finish_required_work` | 决定是否 `wrap_up` / `disclosure_only`；决定这一轮**还能不能**关闭 gap |
| **模型响应后** | `:13862 CompletionReadinessDecision` → `:13989` 完成门 → `:14097` 出口分支 | 决定 `final_response` 还是注入纠正 |

**⇒ M1 动作 1 不是"搬一个 if"，而是"把一个跨两次调用的判定整体外移"。** 这解释了为什么它看起来简单、实际牵动 371 行（相位 C）+ 381 行（相位 E）。

---

## 7. 对 M1 / M2 的直接结论

1. **M2 的出口词汇可以直接用这 5 个 action 收敛**，不需要新造词——把它们映射到 Hermes 的前缀表形状（`(code, retryable, fails_turn)`）即可，其中 `EXHAUSTED` 与 `REPORT_INCOMPLETE_RECOVERABLE` 的 `fails_turn=False`。
2. **M1 动作 1 可行且是映射**，但必须连带搬两处：出口分支 `:14097` **和** 轮前 gap 计算 `:13242-13275`。
3. **必须同时修掉"合成 USER 消息"**——否则外移之后历史里仍然会残留运行时自造的 user 行，多轮问题只解决一半。
4. **`CompletionReadinessPolicyPort` 是 Port，不要动它的形状**；要动的是它的**消费方式**。

---

## 7.5 gap 是怎么造出来的：`_completion_readiness_gaps`（`:8007-8324`，318 行）

docstring 原文：*"Build required completion gaps from **durable, inspectable facts**."*

### 四个 gap 来源

| # | 来源 | 位置 | 说明 |
|---|---|---|---|
| 0 | **每个验收标准一条 gap** | `:8052-8160` | `gap_id = f"task-spec:{criterion.criterion_id}"`，按 `criterion.verification_kind` 分派 |
| 0a | ↳ `WORKSPACE_INTEGRITY` | `:8054-8086` | **重算 hash**：取 `mutation_journal` 里未被回滚的突变，逐个 `file_sha256` 比对 `mutation.after_hash`；不匹配的路径 → gap，`required_effects=(MUTATE,)`、`candidate_tools=mutate_tools`、`recoverable=bool(mutate_tools)` |
| 0b | ↳ `POST_MUTATION_COMMAND` | `:8087-` | 突变后的命令验证 |
| 1 | **Evidence Question 记录** | `:8161-8191` | `status in {OPEN, BLOCKED…}` 的记录 → gap |
| 2 | **post-mutation 命令验证** | `:8192-8220` | 未通过 → gap |
| 3 | **未解决的执行失败事实** | `:8228-8272` | 来自 `project_unresolved_failures(...)`；按 `fact.required_effect` 决定 `candidate_tools`；`:8237` 会跳过已被更强的 post-mutation gap 覆盖的 `EXECUTE` |

**注意 `:8045-8050` 的注释**：*"**Outcomes stay in old snapshots for replay, but they no longer form gaps directly.**"* —— 旧的 Outcome 通道已被明确废弃，改由 criteria 承载。

### 两条命名的运行时不变式

**INV-10（`:8274-8296`）**——判据无判官：

```python
if (self._dependencies.rubric_judge is None
        and any(c.verification_kind in {RUBRIC, GOAL_ALIGNMENT}
                for c in spec.acceptance_criteria)):
    gaps.append(CompletionGap(
        gap_id="judged-criterion-without-judge",
        kind="JUDGED_CRITERION_WITHOUT_JUDGE",
        status="MISSING", required=True, recoverable=False))
```
注释：*"a judged criterion with no judge can **neither pass nor be worked on**. It is a **configuration gap, not work**, so it must not be demoted by INV-3: the Task is bounded into **NEEDS_REVIEW via the stall cap** instead of silently succeeding or looping forever."*

**INV-3（`:8298-8305`）**——**required gap 必须可关闭**：

> *"a required gap must be closable. A required gap with no effective effects and no exempt kind can never be closed, which is exactly the dead-lock shape of **task-2c8f8fbb75714516a94ab442995626b5**. Demote it and record the violation instead of suspending the Task forever — **but only when it has no contract provenance**: a gap that names a real requirement must stay required and take the bounded escape (**EXHAUSTED → resumable boundary → NEEDS_REVIEW**) rather than being quietly dropped."*

**这条不变式是照着一次真实的死锁任务写的**（`task-2c8f8fbb75714516a94ab442995626b5`）。执行体：

```python
gaps, _violations = _apply_inv3_demotion(gaps)          # :8314
if _violations:                                          # :8315
    await self._append_events(task_id, (("completion.invariant_violated", {...}),))
```

### 决定性发现：这是**一次未完成的迁移**，不是设计混乱

`:8306-8313` 原文：

> *"AG-11 / SPEC P2: **required coverage gaps join *this* gate -- there is no second readiness channel.** Under **OBSERVE** the proof layer only records, so this branch must stay inert; **it is what keeps P0/P1 behaviour bit-identical**."*

```python
if self.proof_enforcement_mode is not ProofEnforcementMode.OBSERVE:
    gaps = list(gaps) + list(await self._proof_coverage_gaps(task_id, visible_tools))
```

**⇒ tsm-agt 的完成判定正处于 P0 → P1 → P2 的分阶段迁移中**，由一个 `proof_enforcement_mode` 开关控制：

| 阶段 | 行为 |
|---|---|
| `OBSERVE` | 证明层**只记录不执行**（`_proof_coverage_gaps` **不**参与），行为与 P0/P1 **逐位相同** |
| 其他 | 证明覆盖 gap **加入同一个 gate**——注释强调 *"there is no second readiness channel"* |

**这改变了 M2 的判断**：那"5 套并行词汇表"不是随手堆的，而是**迁移过程中新旧两条通道并存**的产物：

- 旧通道：`TaskState`(20) / `CompletionReadinessAction` / `AcceptanceStatus` / recovery actions
- 新通道：proof / coverage（`AG-11`、`SPEC P2`）
- 切换开关：`proof_enforcement_mode`

**⇒ M2 不能"删掉多余的词汇表"，只能"把迁移做完"**：让证明层成为唯一通道（`there is no second readiness channel`），然后把旧通道按 Hermes 的前缀表形状收敛。这比"56 个站点要收敛"精确得多，也解释了为什么它们删不动——**它们是兼容层，删了会打破逐位相同**。

### 出现的 SPEC 记号速查

`INV-3`（required gap 必须可关闭）、`INV-10`（判据无判官 = 配置 gap）、`INV-13`（有 required gap 时不得发出 `COMPLETE`）、`INV-14`（`EXHAUSTED` 既非成功也非失败）、`AG-11`、`SPEC P2`、`P0/P1/P2` 阶段、`ProofEnforcementMode`。
**这些记号的 SPEC 全文未找到**（见未核实项 4）——**M2 开工前必须先找到它**，否则会误删迁移中的兼容层。

---

## 8. 未核实项

1. `CompletionReadinessPolicyPort` 的具体实现（LEGACY_GATE / OBSERVE_ONLY / AGENT_DECIDES 三种 mode 各自的行为）。
2. `_completion_readiness_gaps`（`:8007-8324`，318 行）如何构造 gap 的 `required_effects` 与 `candidate_tools`。
3. `_suspend_incomplete_recoverable`（`:14523`）与 `_record_session_task_result`（`:14091`）的对接。
4. `INV-13` / `INV-14` 所在的 SPEC 全文（只在 docstring 里见到引用）。
5. `phase C` 的 `disclosure_only` 分支（`:13280-13415`）如何用 `disclosure_attempts`。
