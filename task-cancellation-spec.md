# tsm-agt Task Cancellation 技术规格说明（v2 · 对齐现状）

版本：v2
目标系统：当前事件溯源 + asyncio + FastAPI + streaming runtime 架构
适用范围：web session、local API、kernel、agent loop、model streaming、tool execution、process supervisor、runtime lifecycle

## 0. v2 改版说明（相对 v1 的关键修正）

v1 把取消当成"建立一套新的运行时所有权系统"，与本项目的事件溯源、可恢复、Ports/Adapters 架构冲突。本版修正五处：

| v1 主张 | v2 修正 |
|---|---|
| 新增生命周期 `PENDING/RUNNING/CANCELLING/CANCELLED/...` | **复用现有 `TaskState`**（已含 `INTERRUPTING`/`INTERRUPTED`/`CANCELLED`），不新增状态机 |
| `asyncio.Task.cancel()` 是任务级主机制 | 任务级 = **持久化取消意图 + 协作检查**；`asyncio.cancel()` 仅用于**单轮内在途 IO** |
| `TaskRuntimeContext` 在内存里持有整棵 asyncio tree 与全部资源 | 拆成 **durable 意图（事件）** 与 **in-process scope（仅进程内资源）** 两层 |
| 客户端断连 → 自动取消任务 | disconnect / network loss / browser refresh / runtime crash 默认属于 **interruption/interrupted**，而非 cancel |
| cancel 必须先进入 `INTERRUPTING` | cancel 是**显式终止意图**，runtime 可从任意 non-terminal state 直接进入 `CANCELLED` |
| 新增 `POST /tasks/{id}/cancel` 与新状态机 | **补公共入口**，映射到已有 `CancelTaskInput` → 现有 `CANCELLED` |

---

# 1. 背景与现状核实

## 1.1 已有能力（均已核实，附位置）

| 能力 | 位置 | 说明 |
|---|---|---|
| 任务状态机含取消三态 | `core/task.py:35-57` | `INTERRUPTING` / `INTERRUPTED`（可恢复）/ `CANCELLED`（终态）；`is_terminal = {SUCCEEDED, CANCELLED, FAILED}` |
| 取消转换已定义 | `core/task.py:144-160` | `INTERRUPTING → {INTERRUPTED, CANCELLED, FAILED}`；`INTERRUPTED → {RESUMING, CONFLICT, CANCELLED, FAILED}` |
| 显式取消指令 | `core/runtime_input.py:307-314` | `CancelTaskInput(task_id, reason)`，`channel=STRUCTURED_EVENT` |
| 取消已接线到 kernel | `core/kernel.py:3045-3055` | 幂等：已 `CANCELLED` 直接返回；终态报 `InvalidTurnState`；否则 `transition_task(CANCELLED, reason)` |
| 可恢复中断 | `core/kernel.py:10489+` | `interrupt_agent_turn` → `INTERRUPTING → INTERRUPTED` |
| 取消的 HTTP 入口（仅 interrupt） | `local_api.py:239` | `POST /v1/tasks/{id}/interrupt` |
| 进程取消意图事件 | `core/kernel.py:3978`、`9261` | `process.cancel_requested` |
| 前台命令取消 | `core/kernel.py:3843-3857` | 超时/取消 → `executor.stop_process(..., CANCELLED)`，且用 `asyncio.shield` 保证清理不被二次取消 |
| 子进程组终止 | `adapters/local_process/executor.py:136`、`301` | `start_new_session=True` + `os.killpg(pgid, sig)` |
| 模型流协作停止 | `adapters/openai_compatible/model.py:200-216` | **后台线程** + `response.close()` + `stopped` Event（不是 asyncio） |
| `CancelledError` 不吞 | `core/kernel.py:3843-3857`、`sdk/runtime.py:421-422` | shield 清理后 `raise`；SDK 直接 re-raise |
| 局部 in-process 归属 | `core/kernel.py:4091` | `_background_deadline_tasks[process_id]` |
| UI 已识别取消态 | `web/app.py:1140-1262` | `cancelled/CANCELLED → failed` 展示映射 |

## 1.2 真实缺口（本 spec 要解决的）

1. **没有公共 cancel 入口**：`CancelTaskInput` 可被派发，但全仓没有任何 HTTP/SDK/CLI 构造它。
2. **没有断连处理**：全仓无 `is_disconnected` / `WebSocketDisconnect` / `ClientDisconnect`，SSE 生成器与响应对象在客户端断开后缺少释放路径。
3. **`INTERRUPTING` 没有崩溃收敛**：进程在取消中途退出后，重启任务停留在中间态，无对账规则。
4. **没有统一的 in-process 资源归属**：stream / worker thread / async generator / process handle 分散在各层，无法一次性收拢清理（仅 `_background_deadline_tasks` 一处）。
5. **`web.search` 的 `asyncio.to_thread` 不可取消**：runtime 的 `asyncio.timeout` 只取消 await，worker 线程会继续跑完（已确认）。
6. **没有文档化的取消契约**：来源（用户/断连/预算）语义、交互矩阵、幂等规则都未成文。

## 1.3 术语

- **cancel**：用户/API 明确表达"放弃当前 execution 结果"的终止意图；属于 terminal transition，而不是 graceful interrupt。
- **interrupt**：请求协作式暂停/停止 execution；通常用于可恢复场景。
- **interrupted**：execution 因 disconnect、runtime crash、worker 丢失、transport error 等非主动原因被中断，但未来仍可能恢复。
- **取消意图（CancellationIntent）**：一条持久化事实，表示"有人要求停止这个 Task"。可审计、可对账、跨重启存活。
- **协作检查（cooperative check）**：在执行边界（轮次开始、工具调用前、每次流式分片之间）读取意图并主动停止。
- **硬中断（hard interrupt）**：`asyncio.Task.cancel()` / 关闭 socket / 终止子进程，只作用于**进程内在途资源**。
- **收敛（settle）**：把任务状态落到一个稳定态（`INTERRUPTED` 或 `CANCELLED`）。

---

# 2. 目标

用户请求取消一个 Task 时，系统保证：

1. 取消意图被**持久化**（事件日志），可审计、可在重启后继续生效；
2. 意图沿 **web → local API → SDK → kernel → agent loop → model stream → tool → IO** 协作传播；
3. 进程内在途资源被释放：模型流、SSE 连接、async generator、worker thread、子进程；
4. 任务落入稳定态：非主动 interruption 进 `INTERRUPTED`，显式 cancel 进 `CANCELLED`；
5. 清理是**幂等**的，重复取消、取消中再断连都不产生额外副作用；
6. 不吞 `asyncio.CancelledError`，清理段不被二次取消打断；
7. 架构上不新增状态机、不新增第二套真相。

# 3. 非目标（Non Goals）

- 分布式 / 跨机取消；
- exactly-once 取消语义（只保证幂等与收敛）；
- 持久化工作流引擎、K8s/Job 编排；
- 抢占式 CPU 中断、VM/容器强制销毁；
- **回滚已提交的 mutation**：取消是"中止后续"，不是"撤销已发生的事实"。

MVP 覆盖：**单进程内**的任务取消与资源释放。

# 4. 核心设计原则

## 4.1 cancel 与 interrupt 是不同语义

- cancel = 显式终止当前 execution；
- interrupt = 请求协作式暂停/停止；
- interrupted = execution 被非主动因素打断；
- disconnect / browser refresh / runtime crash / network loss 默认不等于 cancel；
- cancel 不要求必须经过 `INTERRUPTING`；
- 所有 non-terminal states 都允许直接转入 `CANCELLED`；
- terminal states（`SUCCEEDED` / `FAILED` / `CANCELLED`）不再接受 cancel。

cancel 是否需要额外 cleanup，由 runtime 自行决定，但 cleanup 不改变 cancel 的 terminal intent 语义。

## 4.2 取消是三段式，不是一次调用

```text
① durable intent     取消意图写成事件（唯一真相，跨重启）
        ↓
② cooperative stop   执行边界读取意图并主动停下（可恢复、可审计）
        ↓
③ hard interrupt     对进程内在途资源做硬中断（最后一公里，尽力而为）
```

①决定"要不要停、停到哪个稳定态"；②决定"在哪里停、怎样保住已完成工作"；③只负责"别让 socket/线程/子进程泄漏"。

## 4.3 任务级取消不是 `asyncio.Task.cancel()`

`asyncio.cancel()` 会丢栈、丢 checkpoint 语义，无法 resume。任务级取消必须走 `TaskState` 转移；`asyncio.cancel()` 只在**单轮内部**用于中断在途 IO（现状已在 `cli.py:1624/1774/1791` 这么用）。

## 4.4 复用而非新增

- 状态：复用 `TaskState`（`INTERRUPTING` = 协作式停止过程，`INTERRUPTED` = 可恢复中断态，`CANCELLED` = 显式终止终态）。
- 信号：复用 `CancelTaskInput` / `process.cancel_requested` / 模型流 `stopped` Event，统一为一个门面（见 §5），不新增第四套。
- 进程：复用 `executor.stop_process` 与 `killpg`（已实现）。

## 4.5 durable 与 in-process 分层

- **durable**：意图、状态转移、收敛结果 → 事件日志；
- **in-process**：stream / thread / generator / process handle / cleanup callbacks → 仅进程内，进程退出自然消失。
- 禁止把 asyncio 对象放进持久化结构；禁止只靠内存判断"是否已取消"。

## 4.6 清理是强制责任，且必须幂等

`CancelledError` 处理模式固定为：`清理（shield）→ re-raise`。

## 4.7 不按业务分派

判定只依赖状态机、事件与 `ToolEffect`，不认识 git / 部署 / HTTP / 搜索。

---

# 5. 取消信号统一门面

现状有三处同类信号，v1 提出第四套 `CancellationToken` 属于重复建设。本版定义**门面**而非新原语：

```python
class CancellationSignal(Protocol):
    def request(self, reason: str) -> None: ...      # 产生意图（写事件）
    def requested(self) -> bool: ...                  # 同步读取（内存快照）
    async def wait(self) -> None: ...                 # 等待意图出现
```

实现归属：

| 场景 | 底层信号（复用） |
|---|---|
| 任务级取消 | `CancelTaskInput` → `task.state_changed(CANCELLED, reason=...)` |
| 进程级取消 | `process.cancel_requested` 事件 |
| 模型流停止 | 现有 `stopped` Event |
| 单轮内 IO | `asyncio.Task.cancel()`（不进事件日志） |

规则：**任务级取消必须走事件**；`asyncio.cancel()` 永远不写事件、也不改变 `TaskState`。

## 5.1 in-process 作用域：`CancellationScope`（D-接口）

只登记**进程内**资源，不持久化；进程退出即整体消失。接口固定如下：

```python
@dataclass(slots=True)
class CleanupReport:
    closed: list[str]                 # 成功释放的资源名
    timed_out: list[str]              # 超时未释放
    failed: list[tuple[str, str]]     # (资源名, 错误摘要)
    duration_seconds: float

class CancellationScope:
    """One Task execution generation's in-process resources. Not persisted, not shared."""

    def register_task(self, task: asyncio.Task, *, name: str) -> None: ...
    def register_thread(self, stopped: threading.Event, *, name: str) -> None: ...
    def register_process(self, handle: ProcessHandle) -> None: ...
    def register_generator(self, agen: AsyncGenerator, *, name: str) -> None: ...
    def register_stream(self, closable: ClosingStream, *, name: str) -> None: ...
    def register_cleanup(
        self, name: str, fn: Callable[[], Awaitable[None]],
    ) -> None: ...

    async def close_all(self, *, timeout_seconds: float = 10.0) -> CleanupReport: ...
```

Ownership / lifetime 规则：

- root scope 由 kernel 在 execution boundary 创建；
- adapter / tool / web layer 不拥有 root scope，只允许 register resource；
- follow-up task 不继承 parent scope；
- resume 会创建新的 scope generation；
- verification/finalization phase 可以拥有独立 scope；
- `close_all()` 只能关闭当前 generation 的资源；
- scope 生命周期结束后不得继续 register 新资源。

约束：

- `close_all` **幂等**（重复调用返回同一份已关闭报告，不重复执行 cleanup）；
- `register_*` 在资源已关闭时应立即清理登记项，避免长期占用；
- `register_thread` 只登记停止信号（`threading.Event`），**不得 join 阻塞事件循环**；
- `close_all` 中每个资源独立超时，整体遵守 `timeout_seconds`；超时不抛异常，记入 `CleanupReport`；
- 本结构**只存在于内存**，禁止写入 checkpoint / 事件 payload。

---

# 6. 状态机（复用现有，仅补收敛规则）

## 6.1 来源 → 目标状态

| 触发来源 | 目标状态 | 可恢复 | 说明 |
|---|---|---|---|
| 用户显式取消（`CancelTaskInput`） | `CANCELLED` | 否 | cancel 是显式终止 intent；runtime 可从任意 non-terminal state 直接进入 `CANCELLED` |
| 用户中断（`InterruptTaskInput`） | `INTERRUPTED` | 是 | 保留 checkpoint，可 `RESUMING` |
| disconnect / network loss / browser refresh / runtime crash | `INTERRUPTED` 或保持原状态 | 是 | 属于 interruption/interrupted，不属于 cancel |
| 预算耗尽被迫收尾 | `SUCCEEDED` / `FAILED` | — | 走既有 `wrap_up`/`budget_wrap_up`，**不得标成 CANCELLED** |
| 进程/工具级取消 | 不变更 Task 状态 | — | 只是工具结果 `CANCELLED`，任务继续由模型决定 |

## 6.2 `INTERRUPTING` 的收敛规则（v1 缺失，D1/D-对账已定）

`INTERRUPTING` 是协作式停止过渡态，必须能崩溃恢复。

`CancelTaskInput` 不要求必须经过 `INTERRUPTING`。runtime 可以直接把任意 non-terminal state 转为 `CANCELLED`。`INTERRUPTING` 主要用于 interrupt、graceful settle、tool cleanup 或需要 checkpoint 收敛的场景。

**取消意图的载体（D1 决策）**：**不新增事件类型**，复用现有 `task.state_changed`；在转往 `INTERRUPTING` 的那条事件 payload 上新增字段 `intent`（见 §6.3）。

**收敛规则**：

- 正常路径：`INTERRUPTING → INTERRUPTED`（可恢复）或 `→ CANCELLED`（终态化）；
- 崩溃恢复：进入 `INTERRUPTING` 后进程退出，重启时按 `intent` 收敛：
  - `intent == "cancel"` → `CANCELLED`；
  - `intent == "interrupt"` → `INTERRUPTED`；
  - **字段缺失（历史记录）→ `INTERRUPTED`**（安全侧：保留工作、允许 resume）。

**对账触发点（D-对账决策）**：**采用方案 A**（启动阶段全量对账）。方案 B 仅保留为"实现期无法在启动阶段拿到 store"时的兜底，语义不变，不作为独立选项。

- **A（首选）**：runtime 启动阶段执行一次全量对账 —— 在 `registry.start_all()` 完成之后、接受任何用户输入之前，扫描状态为 `INTERRUPTING` 的 Task 并逐条收敛；
- **B（兜底）**：任何把 Task 载入内存的路径（`TaskSnapshot.from_data` 之后的 kernel 装配处）发现 `INTERRUPTING` 且进程内无对应活跃取消，即时收敛。

约束：

- 对账**幂等**：已收敛的任务不重复写事件；
- 收敛必须写一条 `task.state_changed`，`reason = "interrupting_settled_on_startup"`，便于审计与断言；
- 对账**以事件为准**，不得依赖内存标记；
- 历史 payload 无 `intent` 时不得报错，按 `interrupt` 处理。

## 6.3 事件表示（D-事件 schema）

取消意图写进现有的状态转移事件，payload 最小化、可回放：

```json
{
  "previous_state": "EXECUTING",
  "next_state": "INTERRUPTING",
  "reason": "user cancelled",
  "intent": "cancel",
  "schema_version": 1
}
```

字段约定：

| 字段 | 必需 | 说明 |
|---|---|---|
| `previous_state` / `next_state` | 是 | 现有字段，不变 |
| `reason` | 是 | 现有字段；沿用 `CancelTaskInput.reason` |
| `intent` | 否 | **新增**。`"cancel"` \| `"interrupt"`；缺省视为 `"interrupt"`（历史兼容） |
| `schema_version` | 否 | 新增，缺省为 `1` |

约定：**不写请求者身份、不写原始命令、不写任意用户文本**（沿用现有事件最小化规范；`reason` 只保留短标签）。

## 6.4 收敛后的事件

`INTERRUPTING → CANCELLED|INTERRUPTED` 复用现有 `task.state_changed`，`reason` 取值：

- 显式取消收敛：`"task cancelled by user"`（沿用 `CancelTaskInput` 默认 reason）或调用方给出的 reason；
- 启动对账收敛：`"interrupting_settled_on_startup"`。

---

# 7. 传播链（修订）

```text
Web / CLI / SDK
   │  request(mode=cancel|interrupt)
   ▼
CancelTaskInput → task.state_changed(INTERRUPTING, intent="cancel")   ← durable intent
   │
   ▼
Kernel：轮次边界协作检查（每轮模型调用前 / 每次工具调用前）
   │  已有拦截点：_continue_agent_turn 循环、_invoke_agent_tool 入口
   ▼
Agent Loop：停止 step 调度 / retry loop / 新工具批次
   │
   ▼
Model Stream：stopped Event → response.close()（线程内 in-flight IO）
   │
   ▼
Tool Execution：asyncio.cancel()（在途 await）
   │
   ▼
Subprocess / IO：executor.stop_process → terminate → wait(grace) → killpg
   │
   ▼
cleanup(shield) → 状态收敛（INTERRUPTED | CANCELLED）
```

与 v1 的差别：取消意图先落库再传播；每一级都是"检查 + 停止"，不是"只有叶子被 cancel"。

---

# 8. 各层接入点

| 层 | 文件 | 改动 |
|---|---|---|
| HTTP（API） | `local_api.py` | 新增 `POST /v1/tasks/{id}/cancel`，构造 `CancelTaskInput`；与既有 `/interrupt` 并列，语义在文档与响应里区分 |
| SDK | `sdk/runtime.py` | 新增 `cancel(task_id, reason)`；`_run_submitted_task` 已 re-raise `CancelledError`（`:421`），保持 |
| Kernel | `core/kernel.py` | `CancelTaskInput` 派发已存在（`:3045`）；补①轮次边界检查（在 `_continue_agent_turn` 每轮开头、`_invoke_agent_tool` 入口读取意图）、②启动对账方法（§6.2 方案 A，装配期调用一次）、③与 pending batch 的交互（§9.3） |
| Agent Loop | `core/agent_loop.py` | 在 step 调度前读取意图；停止 retry/规划 |
| Model Stream | `adapters/openai_compatible/model.py` | 复用 `stopped` Event；把 worker 线程登记到 in-process scope |
| Tool（命令/进程） | `adapters/builtin/process_tools.py`、`adapters/local_process/executor.py` | 复用 `stop_process`/`killpg`；把 handle 登记到 scope |
| Tool（搜索等 IO） | `adapters/builtin/network_tools.py` | `to_thread` 不可取消：登记为 in-process 资源 + 保留 deadline 兜底（见 §16 决策点 D4） |
| Web | `web/app.py` | SSE/`StreamingResponse` 生成器：断连时释放流资源（§9.6）；可选 UI 取消按钮 → SDK `cancel` |

---

# 9. 交互矩阵（v1 缺失）

## 9.1 等审批中取消
允许。`AWAITING_APPROVAL → CANCELLED` 已在转换表内。语义：未决审批请求作废（不执行、不写入批准记录），任务终态化。

## 9.2 `AWAITING_USER`（追问/澄清）中取消
允许，转 `CANCELLED`，未回答的澄清请求标记为作废。

## 9.3 cancel 与 follow-up / runtime-input 的边界

cancel 只作用于当前 execution。

cancel 不自动：

- 删除 queued follow-up；
- 取消 future session task；
- 清空 runtime input queue；
- 阻止未来用户再次调度；
- 传播到其它 execution generation。

follow-up 是否继续 dispatch，由 follow-up 自己的 lifecycle 与调度规则决定，而不是由当前 execution 的 cancel 隐式传播。

## 9.4 取消时有未闭合工具批次（pending batch）
顺序固定：

1. 先把意图落库（durable）；
2. 对该批次的在途调用做硬中断 / `stop_process`；
3. 批次按**已有** `_synchronize_tool_batch` / 恢复屏障语义收尾，产出 `CANCELLED` 的 `ToolResult`；
4. 若走显式取消 → 转 `CANCELLED`；若走中断 → 转 `INTERRUPTED` 并保留可恢复的 checkpoint。

禁止在批次未收尾时直接丢弃 Task，否则会留下不可解释的孤儿执行记录。

## 9.5 已提交 mutation
不回滚。取消只中止后续步骤；回滚是独立能力（`core.rollback_mutation*`），必须由用户显式发起。

## 9.6 重复取消 / 终态取消
幂等：已 `CANCELLED` 直接返回同一 Task（现状 `kernel.py:3047-3048` 已实现）；其它终态（`SUCCEEDED`/`FAILED`）报 `InvalidTurnState`，不静默改写结果。

## 9.7 客户端断连（D3/D6 已定）
**默认只释放流**：关闭 SSE 生成器 / `StreamingResponse` / websocket，任务继续按既有 checkpoint 语义运行或暂停。理由是项目核心价值是"会话持久化、刷新与重启可恢复"。

配置（D3）：`TSM_AGT_CANCEL_ON_DISCONNECT`，**默认 `false`**。设为 `true` 时断连等价于 `cancel`，并在文档中标注它会把耐用会话变成易失会话。

检测落点（D6）：在 SSE / `StreamingResponse` 的生成器内，**每次 `yield` 前**执行 `await request.is_disconnected()`；命中后先释放本连接的流资源，再按 `TSM_AGT_CANCEL_ON_DISCONNECT` 决定是否请求取消。逐事件检查的开销可忽略。

## 9.8 VERIFYING / FINALIZING 阶段的 cancel

VERIFYING / FINALIZING 期间仍属于 non-terminal state，因此允许直接 cancel。

规则：

- VERIFYING → CANCELLED：verification aborted；
- FINALIZING → CANCELLED：允许终止 finalize cleanup；
- 已开始的 verification cleanup 仍需遵守 cleanup contract；
- verification aborted 不得再写 `SUCCEEDED`；
- cancel 优先级高于 verification completion。

cancel 后的最终 terminal state 必须为 `CANCELLED`，而不是 `FAILED` 或 `SUCCEEDED`。

## 9.9 取消中的断连
先落库的意图优先；释放流资源不改变状态收敛结果。

---

# 10. Cleanup 契约

## 10.1 覆盖清单

- async generator → `await agen.aclose()`
- 流响应 → `await response.aclose()` / `response.close()`（按实现）
- websocket / SSE → 关闭
- 子进程 → `stop_process` → `terminate` → `wait(grace)` → `killpg`
- worker thread → 置 `stopped` 事件；不 `join` 阻塞事件循环
- pending future → `future.cancel()`
- sqlite 事务 / 锁 → 释放 / rollback
- 临时资源 → 删除

## 10.2 固定模式

```python
try:
    ...
except asyncio.CancelledError:
    await asyncio.shield(cleanup())   # 清理不被二次取消打断
    raise                              # 必须 re-raise
```

## 10.3 幂等与超时（D2 已定）
- `cleanup()` 必须可重入：重复调用不产生副作用；
- **整体预算 10s**（`CancellationScope.close_all(timeout_seconds=10.0)`）；
- **单资源 grace**：子进程沿用现状 `terminate` → `wait(2s)` → `killpg`（与 `core.process_stop` 的默认 `grace_seconds=2.0` 一致）；其余资源单资源超时 3s；
- 超时记录 `cleanup_timeout`，不阻塞整体收敛；
- cleanup 失败不吞异常：记录后仍要落到稳定态。

## 10.4 收敛判据
"取消是否完成"以**状态转移事件**为准（`task.state_changed → INTERRUPTED|CANCELLED`），不以内存标记为准。

---

# 11. API / SDK / CLI

```http
POST /v1/tasks/{task_id}/cancel
Content-Type: application/json
{"reason": "user cancelled"}

200 {"task_id": "...", "state": "CANCELLED"}
200 {"task_id": "...", "state": "CANCELLED"}   # 幂等重复调用
409 {"detail": "terminal task ... cannot be cancelled"}
```

```python
# SDK
await client.cancel(task_id, reason="user cancelled")   # → CANCELLED
await client.interrupt(task_id, reason="...")           # → INTERRUPTED（可恢复，已存在）
```

CLI：`Ctrl+C` → 第一次映射 `interrupt`（可恢复，现状行为）；连续第二次或显式确认 → `cancel`。

权限（D5）：MVP **不设** cancel 权限门 —— 与现有本地单用户模型一致。后续接入 Trust/Approval 时，`POST /v1/tasks/{id}/cancel` 与 `interrupt` 共用同一权限判定，不在本 spec 范围内。

---

# 12. 验收场景（带可验证断言）

| # | 场景 | 断言 |
|---|---|---|
| 1 | 用户 cancel 运行中 Task | `task.state_changed` 出现 `CANCELLED`；且此前有一条 `INTERRUPTING` 事件带 `intent="cancel"` |
| 2 | cancel 已 CANCELLED | 返回同一 Task，不产生第二条状态转移事件 |
| 3 | cancel 已 SUCCEEDED | 抛 `InvalidTurnState`，状态不被改写 |
| 4 | 等审批中 cancel | 任务 `CANCELLED`；无新审批批准记录 |
| 5 | 取消时有 pending batch | 批次有终态 `ToolResult`；无孤儿 execution 记录；任务收敛 |
| 6 | 子进程工具在跑 | `terminate → kill`；进程组无残留；`ToolResult=CANCELLED` |
| 7 | 客户端断连（默认） | SSH/流被释放；`TaskState` 不变；无 generator 泄漏 |
| 8 | 断连 + `cancel_on_disconnect=true` | 任务最终 `CANCELLED` |
| 9 | 取消中途崩溃重启 | 启动对账后 `INTERRUPTING` 收敛为 `INTERRUPTED` 或 `CANCELLED`，不停留中间态 |
| 10 | `CancelledError` 处理 | 清理执行且异常被 re-raise，无 `except Exception: pass` 吞掉 |
| 11 | 预算耗尽 | 结果为 `SUCCEEDED/FAILED`，**不得**为 `CANCELLED` |
| 12 | 已提交 mutation 后取消 | mutation 记录保持，不出现回滚事件 |

---

# 13. 分阶段实施（对齐现状）

## Phase 0 — 契约与对账（无新机制）
- 落文档：本 spec 的交互矩阵与收敛规则；
- 实现 `INTERRUPTING` 启动对账（§6.2）；
- 补测试：§12 #2/#3/#9/#10。

## Phase 1 — 公共入口 + durable intent
- `POST /v1/tasks/{id}/cancel` → `CancelTaskInput`；
- SDK `cancel()`；
- 轮次边界协作检查（kernel/agent loop 读取意图）；
- 补测试：§12 #1/#4/#11。

## Phase 2 — in-process scope + 断连资源释放
- `CancellationScope`（进程内）：streams / threads / handles / generators / cleanups；
- 合并现有 `_background_deadline_tasks`；
- SSE / `StreamingResponse` 断连释放；`cancel_on_disconnect` 开关；
- 补测试：§12 #5/#6/#7/#8。

## Phase 3 — 可观测与泄漏检测
- metrics：active / cancelling / cleanup duration / cancel latency；
- 日志：`task_cancel_requested` / `task_cancel_propagated` / `model_stream_closed` / `subprocess_terminated` / `cleanup_completed` / `cleanup_timeout`；
- 泄漏检测：alive subprocess / alive async task / dangling stream / leaked generator。

---

# 14. 测试要求

- **状态层**：每个来源 → 目标状态的转移测试；幂等与终态拒绝；
- **对账层**：构造 `INTERRUPTING` + 有/无意图事件，断言收敛结果；
- **批次层**：取消时有 pending batch，断言批次收尾与无孤儿记录；
- **资源层**：子进程组终止、流释放、generator `aclose` 被调用（用 fake 断言调用次数）；
- **异常层**：断言 `CancelledError` 被 re-raise，且清理段用 `shield`（可用 fake cleanup 记录调用）；
- **回归**：不得新增既有套件失败。

---

# 15. 改动面汇总与回退

| 类型 | 内容 |
|---|---|
| 新增事件 | **无**。复用 `task.state_changed`，仅新增可选字段 `intent`（D1） |
| 新增字段 | `task.state_changed` payload：`intent`、`schema_version`（均有默认值，回放兼容） |
| 新增 API | `POST /v1/tasks/{id}/cancel` |
| 新增 SDK | `cancel(task_id, reason)` |
| 新增配置 | `TSM_AGT_CANCEL_ON_DISCONNECT`（默认 `false`） |
| 新增内存结构 | `CancellationScope` + `CleanupReport`（不持久化） |
| 新增启动逻辑 | `INTERRUPTING` 启动对账（幂等） |
| 不新增 | 状态枚举、完成判定体系、第二套取消信号、持久化实体 |
| 回退 | 关闭公共入口即可退回纯 `interrupt`；对账规则与 `intent` 字段可独立回滚（缺省即 `interrupt`） |

---

# 16. 已定决策（D1–D6）

以下决策已拍板，实现时不再讨论；如需变更必须回到本节更新。

| 编号 | 决策 | 落地位置 |
|---|---|---|
| **D1** | 取消意图**不新增事件**，复用 `task.state_changed(INTERRUPTING)`，payload 新增可选 `intent: "cancel"\|"interrupt"`；缺省视为 `interrupt` | §6.2、§6.3 |
| **D2** | cleanup 整体预算 **10s**；子进程 `terminate → wait(2s) → killpg`（与 `process_stop` 默认一致）；其余单资源 3s | §10.3 |
| **D3** | 断连**默认只释放流**；`TSM_AGT_CANCEL_ON_DISCONNECT=false` 为默认，`true` 时断连等价于取消 | §9.6 |
| **D4** | `web.search` 等 `to_thread` 暂不改造：登记为 in-process 资源 + 保留 deadline 兜底；可取消 HTTP 客户端列入后续迭代 | §8、§16.1 |
| **D5** | MVP **不做** cancel 权限门；复用现有 Trust/Approval 留作后续 | §11 |
| **D6** | 断连检测落点：SSE/`StreamingResponse` 生成器内**每次 yield 前** `await request.is_disconnected()` | §9.6 |

| **D-对账** | `INTERRUPTING` 收敛：**采用启动阶段全量对账**；幂等、写 `reason="interrupting_settled_on_startup"` | §6.2 |
| **D-接口** | `CancellationScope` / `CleanupReport` 接口签名固定 | §5.1 |
| **D-事件** | 事件 schema 固定（`intent` + `schema_version`） | §6.3 |

## 16.1 D4 的后续迭代（登记，不在 MVP）

`asyncio.to_thread` 无法被取消，本版只做两件事：把线程登记进 `CancellationScope`（停止信号可协作）、保留 `web.search` 的 deadline。真正的修复是**把检索 IO 换成可取消的异步 HTTP 客户端**（`httpx.AsyncClient` / `aiohttp`），届时 `asyncio.cancel()` 才能立即生效。该改造会触及 `retrieval` 全部 provider 与 `network_tools`，独立成一项工作，不混入本 spec。

## 16.2 本文档未覆盖（明确排除）

- `SUCCEEDED` 之后的取消（无意义，终态拒绝）；
- 已提交 mutation 的回滚（独立能力，见 §9.4）；
- 跨进程 / 跨机取消（§3 非目标）；
- 取消的审计报表与长期指标留存（Phase 3 只做最小 metrics/log）。
