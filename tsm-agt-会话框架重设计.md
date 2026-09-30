# tsm-agt 会话框架重设计

> **行号引用已失效（2026 更正中）**：本文写作时 `core/kernel.py` 尚未分解。
> 此后轮循环已抽为 `core/turn_phases.py` / `core/turn_tool_batch.py`，
> 并按 Hermes 门面+Mixin 形状开始分解（第 1 刀 `core/kernel_session.py`，
> 见 `tsm-agt-按Hermes重构kernel映射.md`）。文中 `kernel.py:NNNN` 一律不再
> 指向所述代码；请按同处已写出的**方法名**重新定位。
> 保留原行号是为了让这份记录仍可追溯到当时的代码。

> ## ⚠️ 更正（2026-09-30）：本文档的**核心前提有误**
>
> 本文原先断言"多轮接不上"，依据是 `SessionSnapshot` 无 transcript、会话内容靠每轮重投影。**该诊断已被实测推翻。**
>
> 按正确路径驱动一轮（`submit_session_text` **之后必须调 `run_task`**；前者只创建 Task 不执行）后实测，第 3 轮的 `session-context-*` 消息的 `recent_messages` 依序含有：
>
> ```
> user      | instruction number 1
> assistant | instruction number 1
> user      | instruction number 2
> assistant | instruction number 2
> user      | instruction number 3
> ```
>
> **多轮文本连续性本来是工作的。** 先前的"现状断言"之所以失败，是因为任务从未执行、根本没发出模型请求——一个假 XFAIL。详见 `tests/test_multiturn_context.py` 的模块 docstring 与提交 `fix(test): 更正多轮上下文的错误诊断`。
>
> **仍然成立的部分**：
> * 会话内容是**有界渲染**（`session-context-*` JSON 块，实测 3726 字符），受 `recent_visible_message_limit` 等上限约束——长会话会丢旧轮，但这不是"接不上"。
> * **tool 调用与结果确实不跨轮**——但这是**刻意的安全决策**（`Kernel._session_task_summary @ core/kernel_session.py`），不是疏漏。
> * `SessionSnapshot` 确实没有一等 transcript；这影响的是**保真度与可审计性**，不是连续性。
>
> **下述"决定 1"的原始论证请以本节为准修正。** 决定 2/3/4/5 未受此错误影响（它们分别依据循环结构、完成判定与状态机，均独立核实过）。

> 回答两件事：**(1) 当前代码还能不能重构；(2) 会话框架怎么重新设计。**
> 所有判断都有数字或出处。已有 SPEC 的结论**优先于**我的主张（见 §6）。

---

## 1. 判定：能重构，而且不难

| 指标 | 实测 |
|---|---|
| 源文件 | **212 个 / 68,695 行** |
| **≤300 行的文件** | **168 个（79%）** |
| 测试 | **138 文件 / 45,764 行 / 1,417 passed** |
| 端口 | **39 个 / 4,431 行** |
| 适配器 | **43 个目录** |

**代码集中度**（决定重构难度的唯一指标）：

```
前  1 个文件  kernel.py        18,079 行  = 26%
前  4 个文件                   27,548 行  = 40%
前 10 个文件                   36,143 行  = 53%
前 20 个文件                   43,863 行  = 64%
其余 192 个文件                24,832 行  = 36%，平均 129 行/文件
```

**结论：这是一个健康的代码库，病灶集中在一个文件。**

- 79% 的文件已经足够小（Hermes 自己的拆分阈值是 2000 行 / 300 行，`AGENTS.md:249`）；
- 192 个文件平均 129 行——这是**教科书级的良好分布**；
- `ports/` 39 个端口是干净的契约面，**它们不需要改**；
- **1,417 个通过的测试是最重要的资产**——有它才谈得上重构。

**⇒ 没有任何理由重写。** 重写要丢掉 39 个端口、43 个适配器和 1,417 个测试，换来的只是重新犯一遍同样的错。

> 附带说明：仓库里有一份 `TSM-Agent生产级LangChain-LangGraph从零构建SPEC.md`（2,716 行）主张用 LangChain/LangGraph 从零构建。**按上面的数字，这条路在经济上不成立**：现有代码 79% 已经达到了"框架"该给你的分层水平，重写的成本远大于拆一个 god file。

---

## 2. 病灶：唯一的问题是 `kernel.py`

| 文件 | 行数 | 性质 |
|---|---|---|
| `core/kernel.py` | **18,079** | 全部病灶的 68% |
| `core/session_context.py` | 1,485 | 会话投影 |
| `sdk/runtime.py` | 1,721 | 客户端门面 |
| `core/flow.py` | 1,622 | 流程 |
| `core/context.py` | 754 | 上下文 |
| `core/task.py` | 737 | 任务聚合 |
| `core/agent_loop.py` | 590 | 只有数据类型，没循环 |
| `core/runtime_input.py` | 464 | 输入路由 |
| `core/session.py` | 258 | 会话聚合（**无 transcript**） |
| `core/turn_outcome.py` | 206 | **形状错误，见 §6** |
| `core/session_resources.py` | 152 | |
| `core/session_handoff.py` | 117 | bounded handoff |
| `core/turn_finalizer.py` | 113 | **形状错误，见 §6** |
| `core/session_interaction.py` | 80 | |
| `core/turn.py` | 68 | |
| **合计** | **26,446** | 其中 kernel.py 占 **68%** |

**"重新设计会话框架" = "拆掉 kernel.py + 重组这 15 个文件"。** 这是一个有界工程，不是无底洞。

---

## 3. 重设计的五个决定

### 决定 1 · 过渡：会话记录应成为一等公民（~~否则多轮永远不通~~）

> **⚠️ 理由已被推翻，见文首更正。** 多轮文本连续性**是工作的**（前序 user 与 assistant 行都被 `session-context-*` 携带且保序）。本决定**降级为保真度/可审计性改进**，不再是"多轮能否工作"的前提。
>
> 仍然值得做的部分：会话内容目前是有界渲染（每次模型调用重建 JSON 块）而非持久记录，所以**长会话会丢旧轮**；且 tool 行不跨轮。前者是容量问题，后者是安全问题。

**现状**：`core/session.py:81-95` 的 `SessionSnapshot` 只有 `task_ids`/`active_task_id`/两个计数器，没有消息；会话内容靠 `SessionContextProjector.project()` 每轮重投影（`core/session_context.py:582`），再经 `for_prompt` 渲染进 `session-context-*`。

**依据**：Hermes 的会话就是一张 `messages` 表（`hermes_state_common.py:443-474`），逐轮追加、逐轮重放；组装顺序固定 13 步（`turn_request_assembly.py:111-113`）。

**动作**：`SessionSnapshot` 增加有序 transcript；`SessionConversationMessage` 补 `tool` 角色与 tool 配对字段；历史**按追加顺序全量重放**。

**已有 SPEC 支持**：`MULTITURN_CHAT_CORE_REFACTOR_SPEC.md` §6"Session Context 简化"、§8"Session Store 最小化"。

### 决定 2 · 一轮相位化（否则单轮永远改不动）

**现状**：`kernel.py::_continue_agent_turn`（`:12038-14170`，**2,133 行**）= 39 行序言 + **一个 2,074 行的 `while True:`**；状态是 26 个局部变量 + 反复 `replace()` 的 `checkpoint`。

**依据**：Hermes 一轮 = 一个 `_LoopState`（"新增相位输入输出只需在这里加字段"，`conversation_loop.py:1385-1420`）+ `_run_phase` 按名取参/按名写回（`:1480-1498`）+ 唯一 `action ∈ {ok,continue,break,return}`（`:1641-1678`）+ 主循环固定相位序（`:1636-1678`）。

**动作**：按 `tsm-agt-轮循环相位图.md` 已实测的五个宏观相位切成兄弟文件，`kernel.py` 只留门面。

### 决定 3 · 完成判定外移（否则闭环永远定不下来）

**现状**：完成判定在**轮内两处**——模型调用前 `:13242-13275`、响应后 `:13862/:13989/:14097`；不满足时 `messages.append(_completion_correction_message(...))` 然后**继续 while 转一圈**（`:14097-14099`）。

**依据**：Hermes 的判定在**轮外单点** `evaluate_after_turn`，产出 `should_continue` + `continuation_prompt`，把 prompt **塞回输入队列**，**不碰 `messages`**（`gateway/run_goals.py:296-314`）。

**动作**：见 `tsm-agt-完成判定机制.md` §5 的映射表（5 个 action 全部对得上 Hermes 形状，不需要造新词）。**同时必须修掉"合成 USER 消息注入轮内"**——那违反 Hermes `agent/AGENTS.md:63-67` 的硬不变量。

### 决定 4 · 状态机 19 → 7（采纳已有 v2 SPEC）

**依据**：`FLOW_AND_STATE_MACHINE_REDESIGN_SPEC.md` §2。三条**可机械执行**的判据：

| 判据 | 含义 |
|---|---|
| 单一后继 → 合并 | 只有一个后继的状态不必独立存在 |
| 后继相同 → 合并 | 两个状态后继完全一样，说明没有区别 |
| 可用数据表达 → 不必是状态 | "在等什么"用 `pending_*` 数据表达即可 |

推出 7 个状态，**实际流转只有 4 个**（`PREPARING` / `RUNNING` / `WAITING` / `INTERRUPTED`），每个状态能唯一回答"下一步做什么"。同一份 SPEC 还决定 **去掉 Outcome 层，改用 Task 级验收标准**（`TaskAcceptanceCriterion` 已存在）。

**这条比我自己那份 M0-M3 方案好**——它有机械判据，我的没有。**采纳它。**

### 决定 5 · 保留 Task 级权威，但把它与上下文解耦

**这是 tsm-agt 唯一**真正**比 Hermes 复杂的地方**：Hermes 没有 Task 级 authority（审批在工具层 `tools/approval.py`），tsm-agt 有，且 `sdk/runtime.py:371-375` 明确"普通追问不原地续跑，RESUME 被规范化为全新 FOLLOW_UP Task + bounded authority-free handoff"。

**因果链**（全部实测）：
1. 为不继承 authority → 2. 会话内容必须按 Task 血缘过滤（`session_context.py:212-227`）→ 3. 历史只能有损投影 → 4. **多轮接不上**。

而且 `kernel.py:13437-13450` 的注释写明作者知道这个风险：*"Keeping an old session-context message lets stale Task summaries outlive later user input and dominate resumed turns."*

**动作**：把 `ContextScope` 收窄到"谁能 author 一个 goal / acceptance criterion"（这正是它 docstring 的本意，`session_context.py:173-181`）；**上下文按时间全量，权威单独判定**——两条通路。

> **本项目新增，非 Hermes。** Hermes 里没有对应物。这条风险最高，应放最后。

---

## 4. 新框架的结构（目标形态）

```
入口（CLI / web / SDK）
   └─ 一个输入队列  ← 真实输入 + 续跑提示 + 中断/重试，同一条通路
        ↓
   Session（有 transcript）
        ├─ transcript：追加式消息行（含 tool 角色 + api_content sidecar）
        ├─ 状态：PREPARING / RUNNING / WAITING / INTERRUPTED / DONE / FAILED / CANCELLED
        └─ 诊断数据：pending_clarification / pending_approval（不是状态）
        ↓
   一轮 = TurnState + 相位函数（对应 Hermes 的 _LoopState + _run_phase）
        ├─ 相位序固定，每个相位返回 action ∈ {ok, continue, break, return}
        └─ 出口 = 纯数据 + 一张前缀表
        ↓
   轮外判定（gate → judge → decision）
        ├─ should_continue + continuation_prompt
        └─ 塞回输入队列（不碰 transcript）
        ↓
   权威判定（Task 级，本项目独有）
        └─ 只决定"谁能 author 需求"，不参与上下文选择
```

对照 Hermes 的出处：队列（`cli_tui_runtime_mixin.py:31-47`）、transcript（`hermes_state_common.py:443-474`）、相位（`conversation_loop.py:1385-1498`）、轮外判定（`run_goals.py:296-314`）、出口表（`turn_failure_copy.py:80-160`）。

---

## 5. 爆炸半径与顺序

| 步 | 动作 | 触及 | 验收 |
|---|---|---|---|
| 1 | 加 transcript，现状断言先写 | `core/session.py`、`session_context.py` | 连续 3 条消息，第 3 轮 wire 副本**逐字节包含**前两轮 |
| 2 | 拆 `_continue_agent_turn`：先按五相位切兄弟文件，**不改行为** | `kernel.py` | 1,417 测试不变；棘轮下降 |
| 3 | 完成判定外移 + 删掉合成 USER 消息 | `kernel.py`、`sdk/runtime.py` | 判定单点；transcript 里不出现运行时自造 user 行 |
| 4 | 状态机 19→7（按 v2 SPEC 判据） | `core/task.py`、`kernel.py` | 状态转换表确定性校验 |
| 5 | 上下文/权威解耦 | `session_context.py` | 投影不再丢历史 |

**第 2 步是唯一的大动作**，但它是纯机械拆分（不改行为），且 1,417 测试全程兜底。**先做第 1 步**——它是唯一能让"多轮是否真接上"变成可断言的。

---

## 6. 与已删除 SPEC 的关系（已定：全部删除）

仓库里原有 **27 份 / 17,918 行 SPEC**，现**已按决定全部删除**（含 `docs/runtime-projection-viewmodel-spec.md`）。本方案与它们的关系：

| 原 SPEC | 本方案的态度 |
|---|---|
| `FLOW_AND_STATE_MACHINE_REDESIGN_SPEC.md`（v2, 364 行） | **采纳** §2 状态机 19→7 与"去掉 Outcome 层"（决定 4） |
| `MULTITURN_CHAT_CORE_REFACTOR_SPEC.md`（392 行） | **部分采纳** §6/§8（决定 1） |
| `用户输入到输出全链路梳理.md`（1,297 行） | **作为事实基准**（它描述的链路与我实测一致） |
| `Artifact Graph与Proof Obligation…SPEC.md`（1,136 行）、`完成判定反面漏洞修复SPEC.md`（594 行） | 定义已抽入 **`tsm-agt-不变式术语表.md`**（AG-1~16、INV-3/4/10/13/14/15/16、ProofEnforcementMode、P0/P1/P2、实施状态） |
| `验收判定改造实施SPEC.md`（1,168 行）、`完成判定改造SPEC.md`（666 行）等 | 未读。**其结论不再可得**；本方案第 3 步按代码实测重新推导，并受术语表的不变式约束 |
| `TSM-Agent生产级…从零构建SPEC.md`（2,716 行） | **不采纳**（见 §1 末） |

**为什么保留术语表**：代码里有 **40 处**引用 `INV-3/INV-10/INV-13/INV-14/AG-11/P0/P1/SPEC P2/ProofEnforcementMode`，删掉 SPEC 后这些引用会悬空。术语表**只留定义，不留方案**。

**⚠️ 由此产生的约束**：删除 SPEC 后，**代码里那 40 处引用成为唯一线索，而它们的完整定义只剩术语表**。后续任何改动若触碰这些记号，必须同步更新术语表，否则会再次出现"记号无定义"。

---

## 7. 状态

1. ~~27 份 SPEC 怎么处理~~ → **已定：全部删除**（含 `docs/runtime-projection-viewmodel-spec.md`）。定义抽入 `tsm-agt-不变式术语表.md`。
2. ~~要不要先读完剩下 20 份~~ → **不适用**：已删除，其结论不再可得。本方案第 3 步改为按代码实测重新推导。
3. **第 1 步（加 transcript）尚未开工** —— 它不依赖任何未读文档，且是唯一能把"多轮接上没有"变成可断言的一步。

---

## 8. 核实边界

**已实测**：全部代码规模与集中度数字、`core/session.py` 全文、`session_context.py:145-234`、`ports/completion_readiness.py` 全文、`kernel.py:8007-8324, 8593-8625, 12038-12164, 13424-13473, 14062-14099`、`sdk/runtime.py:355-424, 838-897`、`kernel.py` 全部函数行数分布。

**已读**：`FLOW_AND_STATE_MACHINE_REDESIGN_SPEC.md:1-95`、四份 SPEC 的目录、27 份的行数清单。

**未读（不得据此下结论）**：`kernel.py` 其余约 15,000 行；`session_context.py` 其余 1,250 行；27 份 SPEC 的正文（其中 `验收判定改造实施SPEC.md` 1,168 行与 `完成判定改造SPEC.md` 666 行和第 3 步直接重叠）；`core/flow.py`(1,622)、`core/context.py`(754)、`core/task.py`(737) 全文。
