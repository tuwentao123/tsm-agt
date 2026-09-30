# tsm-agt 单轮 / 多轮会话重构方案

> 目标：**让一轮跑得通、让多轮接得上**。
> 纪律：每条设计都标出处。Hermes 侧写 `file:line`（分册见 `hermes-架构梳理/`）；**tsm-agt 侧凡是 Hermes 没有的东西，明确标"本项目新增，非 Hermes"**。
> 本方案不引入任何 Hermes 里不存在的新概念类型（`TurnOutcome` 那次的教训）。

---

## 0. 先纠正一件事

单轮和多轮**不是两个问题，是同一个问题的两层**：tsm-agt 没有"conversation"这个一等对象。

- Hermes 里会话 = `messages` 表（`hermes_state_common.py:443-474`），一轮一轮往同一张表追加，下一轮重放全部。
- tsm-agt 里 Session = **Task 的容器**（`core/session.py:81-95`：`task_ids` / `active_task_id` / `configuration_revision` / `context_revision`，**没有一条消息**），会话内容靠 `SessionContextProjector.project()` **每轮从事件重新投影**（`core/session_context.py:559,582`）。

**所以多轮丢信息不是 bug，是架构的必然结果**：投影 + 过滤必然有损，而 Hermes 的追加 + 重放不丢。

---

## 1. 诊断（全部有出处）

### 1.1 多轮为什么接不上

| 事实 | 出处 |
|---|---|
| 会话消息**只允许 user / assistant 两种角色**，`tool` 被显式拒绝 | `core/session_context.py:154-155` |
| 会话消息**没有** `tool_call_id` / `tool_calls` / `tool_name`，也**没有**"当时真实发出的字节"sidecar | `core/session_context.py:145-151`（只有 `message_id/role/text/task_id/turn_id/source_event_sequence`） |
| 消息选择**按 Task 血缘成员资格，明确声明"最近性永远不是资格标准"** | `core/session_context.py:212-227`（`ContextScope.allows` / `select_messages_by_scope`） |
| 会话内容**每轮重新投影**，不是持久记录 | `core/session_context.py:559-582`（`SessionContextProjector.project`） |
| 普通追问**不原地续跑**；RESUME 被规范化为**全新的 FOLLOW_UP Task** + "bounded authority-free handoff" | `sdk/runtime.py:371-375` 原文 |

对照 Hermes：

| Hermes | 出处 |
|---|---|
| `messages(session_id FK, role, content, tool_call_id, tool_calls, tool_name, api_content, active, compacted, …)` | `hermes_state_common.py:443-474` |
| **三份 messages**：durable transcript / wire 副本 / `api_content`（当时真实字节） | `hermes-架构梳理/01-…md` §1；`agent/turn_context.py:1197-1206` |
| 历史行**重放 `api_content`**，保证"前缀逐字节稳定" | `agent/turn_context.py:1255-1262` |
| 组装顺序固定 13 步，**顺序是承重的** | `agent/turn_request_assembly.py:111-113` |

### 1.2 单轮为什么跑不通

| 事实 | 出处 |
|---|---|
| 轮循环在 **18,079 行**的 `kernel.py` 里，`run_agent_turn` 从 `:10762` 起，主循环 `while True:` 在 `:12091` | `core/kernel.py` |
| 循环体内**交织**：取消边界、checkpoint 同步、steering 应用、tool batch 冻结、outcome 绑定、模型调用、审批、验证 | `core/kernel.py:12091-12164` |
| 循环的"状态"是散落的局部变量 + `checkpoint` 反复 `replace()` 重建 | `core/kernel.py:12101-12129` 同一组字段被反复重打包 |

对照 Hermes：

| Hermes | 出处 |
|---|---|
| 一轮 = **一个 `_LoopState`**，注释：*"a new helper input/output needs a field here and nothing else"* | `agent/conversation_loop.py:1385-1420` |
| 相位输入输出**按名字**传递：`_run_phase` 内省签名、从 state 取参、把 verdict 字段按名写回 | `agent/conversation_loop.py:1480-1498` |
| 控制流**只有一个枚举** `action ∈ {ok, continue, break, return}` | `agent/conversation_loop.py:1641-1681` |
| 主循环相位序固定：`begin_iteration → prepare_iteration → assemble_api_request → run_preflight_gate → announce_api_call → _run_api_retry_loop → apply_retry_restarts → normalize_model_response → (run_tool_round \| finish_text_response)` | `agent/conversation_loop.py:1636-1678` |
| **异常在最近一层翻译成同一枚举**，不穿透 | `agent/conversation_loop.py:1501-1530`（`_run_api_retry_loop`） |
| **出口唯一**：循环内 `break` 全部汇到 `finalize_turn`；所有 return 经 `run_conversation` 盖 turn 边界 | `agent/conversation_loop.py:1683-1692,1695-1742` |

### 1.3 闭环为什么定不下来

| 事实 | 出处 |
|---|---|
| 5 套并行词汇表：`TaskState`(20) / `CompletionReadinessAction` / `AcceptanceStatus` / `CoverageStatus` / recovery actions | `tests/architecture/test_dependencies.py` 的 TerminalStateConvergenceTest 棘轮 |
| 56 个 terminal-state 耦合点：`cli.py` 7 + `core/kernel.py` 34 + `core/task_runtime_projection.py` 7 + `sdk/runtime.py` 8 | 同上 |

对照 Hermes：

| Hermes | 出处 |
|---|---|
| 出口 = **纯 dict + `ExitFailure` NamedTuple + 一张前缀表**（12 行，每行 `(prefix, code, retryable, fails_turn)`） | `hermes-架构梳理/03-…md`；`agent/turn_failure_copy.py:80-160` |
| `completed = final_response is not None and not failed and not interrupted and (…)` | 同册 |
| **没有** per-criterion 判据体系；机器可判的只有一条 shell 命令的退出码 | `hermes-架构梳理/04-…md` §3 |

---

## 2. 重构方案

分四期。**每期都可独立验收**，不要求一次性推翻。

### M0 · 会话记录成为一等公民（多轮的地基）

**做什么**：给会话一张真正的 transcript，把 `SessionConversationMessage` 补齐成 Hermes `messages` 的形状。

| 项 | 依据 |
|---|---|
| 消息模型补 `role ∈ {user, assistant, tool}`；补 `tool_call_id` / `tool_calls` / `tool_name` | Hermes `hermes_state_common.py:443-474` 的五列 |
| 补一条"当时真实发出的字节" sidecar（对应 Hermes `api_content`），**只写不改** | `agent/turn_context.py:1255-1262`；**本项目新增**：Hermes 存 DB 列，tsm-agt 可先存事件属性 |
| 历史行**按追加顺序全量重放**，不再"每轮重新投影" | `agent/turn_context.py:1197-1206` |
| 组装 wire 副本时**顺序固定**，且 cache 断点**最后**打 | `agent/turn_request_assembly.py:111-113` |
| 保留 `ContextScope`，但**只用于权威判定，不用于上下文选择**（见 M3） | 分册 01 §5；**本项目新增**：Hermes 无 Task 级权威，故此处是本项目独有设计 |

**为什么必须先做**：M1 的相位化一旦动了循环，如果没有稳定 transcript，改完仍然接不上；反过来先有 transcript，循环改坏了也能靠重放定位。

**验收**：给定同一会话连续 3 条用户消息，第 3 轮组装出的 wire 副本里必须**逐字节包含**第 1、2 轮的 `api_content`（可直接断言，不需要模型）。

### M1 · 单轮循环相位化

**做什么**：把 `kernel.py::run_agent_turn`（`:10762` 起，循环 `:12091`）按 Hermes 的相位协议抽出。

| 项 | 依据 |
|---|---|
| 建 `_TurnState`，**所有循环局部变量进 state**；注释照抄 Hermes 那句"新增相位输入输出只需在这里加字段" | `agent/conversation_loop.py:1385-1420` |
| 建 `_run_phase(fn, state)`：**内省签名、按名取参、把 verdict 字段按名写回** | `agent/conversation_loop.py:1480-1498` |
| 每个相位返回带 `action ∈ {ok, continue, break, return}` 的 verdict | `agent/conversation_loop.py:1641-1647`；verdict 类族见 `agent/turn_api_call.py:37,161,216`、`agent/turn_api_error.py:32,241` |
| 相位模块按 `<stem>_<topic>` 拆成兄弟文件，**不追加进 kernel.py** | `AGENTS.md:249-251`（文件 ~2000 行 / 函数 ~300 行 / CC 30 即拆） |
| **禁止** keyed on name/kind 的 `if/elif ≥ 4`，改表→handler | `AGENTS.md:252-253`（范例 `cli.py::_SLASH_DISPATCH`） |
| 异常不得穿透：API 类异常在最近一层翻译成 verdict | `agent/conversation_loop.py:1501-1530` |

**验收**：`kernel.py` 行数下降；新增一个相位**不需要改任何调用点**（只有主循环的相位顺序需要手改）——这正是 Hermes 的性质。棘轮测试（`tests/architecture/test_dependencies.py`）应随之下降。

### M2 · 出口收敛成一张表

**做什么**：把 5 套词汇表压成 Hermes 的形状。

| 项 | 依据 |
|---|---|
| 出口 = 纯 dict + 一个 `ExitFailure(code, retryable, fails_turn)` NamedTuple + **一张前缀表** | `agent/turn_failure_copy.py:80-160`；`hermes-架构梳理/03-…md` |
| `completed` 用一条表达式判定，**不做 per-criterion 判据体系** | 分册 03 |
| 预算耗尽**不是失败**（Hermes 里 `max_iterations_reached` / `budget_exhausted` 不在失败表内） | 分册 03 |
| 「不可由 agent 修复的条件 → 暂停不重试」 | `hermes_cli/goals.py:374-394` 原文 *"no agent turn can fix it"* |

**验收**：`TerminalStateConvergenceTest` 的 56 个耦合点显著下降；出口解释只有一处。

### M3 · 权威与上下文解耦（本项目新增，非 Hermes）

**这是 tsm-agt 独有的问题，Hermes 没有对应物** —— Hermes 没有 Task 级 authority：它的审批在**工具层**（`tools/approval.py`），会话层只剩 goal/gate/judge（`hermes_cli/goals.py`）。

**现状的因果链**（全部有出处）：

1. 为了"CONTEXTUAL 不继承任何历史 Task 的 authority"（`sdk/runtime.py:410`），
2. 会话内容必须按 **Task 血缘**过滤（`session_context.py:212-220`），
3. 于是历史只能以**有损投影**的形式进入下一轮（`session_context.py:582`），
4. 于是**多轮接不上**。

**做什么**：
- 把 `ContextScope` 的职责**收窄到"谁能author一个 goal / acceptance criterion"**（这正是它 docstring 里写的本意，`session_context.py:173-181`）；
- 上下文选择改为**按时间全量**，与权威判定**分成两条独立通路**；
- handoff 的"bounded"不再需要以丢弃历史为代价。

**这一期风险最高**，因为它触及 tsm-agt 的信任模型。**建议放最后，且先只读不改**（先加一条断言把"投影丢了多少历史"量出来）。

---

## 3. 明确不做（避免重蹈覆辙）

| 不做 | 原因 |
|---|---|
| 新建 `TurnOutcome` 类 | Hermes 里不存在。出口是 **dict + NamedTuple + 前缀表**（`turn_failure_copy.py:80-160`）。上一版就是在这里凭空造类，被推翻 |
| 在 core 里加 per-tool 的 name 分派 | `AGENTS.md:252` 明令禁止；且 tsm-agt 自己的 `derive_evidence_capabilities` 注释也写了 *"no per-tool rule is added anywhere (no tool-name dispatch)"*（`ports/tool.py:114-116`） |
| 抄 Hermes 的 59 个工具 | 闭包实测：模块级 267 文件/135,835 行，运行时 1489 文件/631,224 行。且 Hermes `register()` **不携带** risk/effect/authority，抄进来会让 `derive_evidence_capabilities` 失去输入 |
| 把 `SessionContextProjector` 直接删掉 | 它承载权威判定，M3 之前不能动 |

---

## 4. 建议的推进顺序与理由

```
M0 会话 transcript  ──→ M1 相位化  ──→ M2 出口表  ──→ M3 权威/上下文解耦
   （多轮地基）          （单轮地基）      （闭环）       （高风险，最后）
```

- **M0 先行**：它是唯一能让"多轮是否真的接上了"变成**可断言的**（逐字节包含），不依赖模型判断。
- **M1 与 M2 可并行**：M1 改控制流形状，M2 改出口词汇表，两者在 `finalize_turn` 汇合。
- **M3 最后**：它是本项目独有设计，且必须先有 M0 的对照才能证明"解耦后多轮确实变好了"。

**每期都必须先写"现状断言"再动手**（例如 M0 之前先断言"现在第 3 轮看不到第 1 轮的内容"），否则无法证明重构有效——这正是当前 687 个任务里 71% 的 FAILED 之类问题能长期潜伏的原因。

---

## 5. 本方案的核实边界

**已逐行核实**：`core/session.py` 全文、`core/session_context.py:145-234` 与类清单、`sdk/runtime.py:355-424,838-897`、`core/kernel.py:12085-12164`、`ports/tool.py` 枚举与 `ToolSpec`/`ToolCall`/`ToolResult`、`adapters/builtin/` 全部工具名。

**未逐行核实（不得据此下结论）**：
1. `kernel.py` 其余 17,000 行 —— **M1 之前必须把 `run_agent_turn` 的相位边界真正读出来**，本方案的相位划分目前只是"按 Hermes 的形状推断"。
2. `session_context.py` 其余 1,250 行（`project()` / `for_prompt()` 的实际过滤逻辑）。
3. `SessionTextInput` → `dispatch_input_event` → `SessionInputDecision` 的路由实现（`core/runtime_input.py`）。
4. `core/session_handoff.py` 全文（"bounded authority-free handoff" 到底 bound 了什么）。
5. 我在本会话早前新建的 `core/turn_outcome.py` / `core/turn_finalizer.py` **形状是错的**（凭空造类 + if 链优先级），M2 时应按 `turn_failure_copy.py:80-160` 重做或删除。
