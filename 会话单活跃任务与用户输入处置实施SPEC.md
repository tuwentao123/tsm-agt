# 会话单活跃任务与用户输入处置实施 SPEC v5

> v5 相对 v4 的修正：
> 1. 判断能力做成**通用端口 + 默认安全缺省**（默认返回"补充"= 追加到当前任务），后续按策略替换；
> 2. 关系取值明确为：补充 / 替换 / 无关 / 问状态 / 直接回答 / 未知；
> 3. 去掉"确认弹窗"（不做）。

---

## 1. 目标（一句话）

一个会话同一时刻只有一个任务在跑。用户输入要么**接到正在跑的任务上**（补充），要么**停掉它、开一个新的**（无关）；不做并发、不排队。

---

## 2. 任务状态与处理

| 类别 | 任务状态 | 用户发来一句话时怎么处理 |
|---|---|---|
| **在跑** | `EXECUTING` / `RUNNING_WORKFLOW` | 交给判断器：补充 / 替换 / 无关（§4） |
| **等你回话** | `AWAITING_APPROVAL`（等审批）/ `AWAITING_USER`（等你回答） | **优先当作对本次审批/提问的回复**；不作新话题 |
| **停了没结束** | `INTERRUPTED`（被中断）/ `CONFLICT`（卡冲突） | 交给判断器；判"补充"则先恢复该任务再追加 |
| **已结束** | `SUCCEEDED` / `CANCELLED` / `FAILED` | 新建任务 |

> `PLANNING/VERIFYING/FINALIZING/RESUMING/INTERRUPTING` 等瞬时态**不特殊处理**：沿用现有状态校验，遇到返回"任务处理中，请稍后重试"。

---

## 3. 统一入口（放最上层）

### 3.1 位置
`RuntimeClient.submit_user_input(...)`（`src/tsm_agt/sdk/runtime.py`）——它同时持有 kernel、store 和 runner 注册表（`_run_tasks`），是唯一能"既做决策、又停掉正在跑的 asyncio 任务"的层。

### 3.2 签名
```
async def submit_user_input(
    self, session_id: str, text: str, *, input_id: str,
    explicit_intent: RuntimeInputIntent | None = None,
    target_task_id: str | None = None,
    images: tuple[ImageBlock, ...] = (),
    workspace: Path | None = None,
) -> UserInputResult
```

### 3.3 返回
```
UserInputResult:
    kind: "task" | "answer" | "clarify" | "steered" | "replaced" | "interrupted" | "rejected"
    relation: str            # 判断器给出的关系（或 explicit）
    reason_code: str
    task: RuntimeTaskResult | None
    answer: str | None
    clarification: str | None
    routed_task_id: str | None
```

### 3.4 错误
| 情况 | 行为 |
|---|---|
| 会话不存在 | 新建会话 |
| 会话已关闭 | `SessionClosed` |
| `input_id` 为空 | `ValueError` |
| 显式意图但目标不是本会话任务 / 状态不允许 | `InvalidTurnState` |

### 3.5 客户端职责（只有两件）
1. 提供显式交互（按钮 / 命令），把结果填进 `explicit_intent`；
2. 选会话。

**"统一入口"指同一个方法，不是"新开一个会话"。** 新建的空会话、发第一句话，走的还是同一个入口，直接建任务。

---

## 4. 判断器：通用端口 + 默认安全缺省

### 4.1 设计

新增一个**通用**判断器端口，只回答一件事：**这条输入和会话历史是什么关系**。

- **端口**：`SessionInputRelationPort`（`src/tsm_agt/ports/`）
- **方法**：`judge(text: str, context: SessionInputRelationContext) -> SessionInputRelationJudgement`
- **输出**：
  ```
  relation    ∈ { SUPPLEMENT, REPLACE, UNRELATED, STATUS_QUERY, ANSWER, UNKNOWN }
  confidence  : 0..1
  reason_code : str
  ```
- **默认实现**：`DefaultSessionInputRelationJudge` —— 对任何输入返回 **`SUPPLEMENT`**（安全缺省：追加到当前任务，不打断）。
- **后续策略**：模型 / 规则实现同一端口，通过 composition 注册替换默认实现，**上层不动**。

### 4.2 关系取值与动作

| relation | 含义 | 动作 | 是否新建 |
|---|---|---|---|
| `SUPPLEMENT` | 补充 / 接着说 | 追加到当前任务（不改目标） | 否 |
| `REPLACE` | 替换需求 | 当前任务换目标（重签合约、重规划） | 否 |
| `UNRELATED` | 无关 | 停掉当前任务、新建 | 是 |
| `STATUS_QUERY` | 只是问状态 | 只读返回 | 否 |
| `ANSWER` | 独立问答 | 直接回答 | 否 |
| `UNKNOWN` | 判不出（策略失败/低置信） | **安全缺省 = 追加到当前任务** | 否 |

### 4.3 判断器输入
```
SessionInputRelationContext:
    text: str
    session_id: str
    recent_messages: [...]            # 会话历史（现有）
    active_task: { task_id, state, goal } | None
```

### 4.4 采纳规则
- 只有 `confidence >= 0.85` 的非缺省结果才采纳；
- 低置信 / `UNKNOWN` / 调用失败 / 超时 → 一律按 **`SUPPLEMENT`**（追加，不打断）；
- 判断器只在"**存在运行中任务**"时需要；没有运行中任务时直接按会话路由新建。

### 4.5 为什么是"通用"
判断器只回答"输入与历史的关系"，不区分入口、客户端、任务类型；具体怎么判（模型、规则、关键词）是可替换实现，不写死在端口里。

> ⚠️ 已知代价（本次明确接受）：默认实现恒返回 `SUPPLEMENT`。所以**策略上线前，运行中收到的任何输入（包括无关消息）都会被追加到当前任务**，不会自动新建；要开新任务必须显式 `/new`。策略上线后，只有识别出 `UNRELATED` 才会自动"停旧建新"。

### 4.6 与现有代码的关系（重要）
- **现状**：Web 每次输入都**新建任务**——全库 `session.task_attached` 273 次全是 `CREATE_TASK`（少数 `ANSWER`），**`STEER` 为 0**；steer 只存在于 SDK/CLI 的 `route_input` 路径。
- **本 SPEC 上线后**：Web 在"有运行中任务"时默认**追加**（`SUPPLEMENT`）。这是**行为变化**，同时修掉"每句话都新建任务"的现状。

---

## 5. 处置矩阵

设当前存在运行态任务 T。

| relation | 处置 | 对 T |
|---|---|---|
| `SUPPLEMENT` | 追加到 T（安全边界生效） | 继续跑 |
| `REPLACE` | T 重签合约 + 重规划 | 继续跑（新目标） |
| `UNRELATED` | 停掉 T、新建 | **中断**（过渡，可恢复） |
| `STATUS_QUERY` | 只读返回 | 不受影响 |
| `ANSWER` | 直接回答，不建任务 | 不受影响 |
| `UNKNOWN` | 安全缺省 = 追加到 T | 继续跑 |

### 5.1 补充（steer）怎么生效、以及任务先结束了怎么办

- 追加**只在安全点生效**：模型调用之间、工具批次闭合之后；**绝不插在工具执行中间**。
- `steering.queued` 事件只是"**已收到并落库**"的记录（用于幂等、顺序、崩溃恢复），**不是业务状态**。
- **若到生效时任务已经不在跑了**（已结束 / 被中断 / 被顶掉）：这条输入**按普通输入处理**（走正常路由 → 新建任务），**不报错、不挂起**；不存在需要清理的"待生效 steering"。
- 因此对调用方的语义是：收到时回"已接收"；真正生效时任务仍在跑 → 记 `steering.applied`；任务已停 → 返回"已按新输入处理（新建任务）"。

---

## 6. 显式命令

| 命令 | 动作 | 旧任务 |
|---|---|---|
| `/steer <文本>` | 追加到当前任务 | 继续跑 |
| `/replace <文本>` | 当前任务换目标 | 继续跑 |
| `/new <文本>` | 停掉当前任务、新建 | **中断**（过渡；真取消上线后改 `CANCELLED`） |
| `/stop` | 中断当前任务 | 中断（可恢复） |
| `/cancel` | **暂不开放**（真取消未实现） | 过渡期用 `/stop` |
| `/resume <任务>` | 切到指定任务 | 当前任务默认**中断** |
| `/queue <文本>` | 排队（暂缓） | 继续跑 |

显式命令优先级最高，**不经过判断器**。

---

## 7. "新任务顶掉旧任务"的动作（过渡：用"中断"）

1. 取会话里正在跑的任务（`session.active_task_id`；历史遗留多运行态取**最近活跃**的，见 §8）；
2. **中断**它：`EXECUTING → INTERRUPTING → INTERRUPTED`（保留断点、可恢复）；`runner.cancel()` 取消 asyncio 任务，释放 in-flight 资源（模型流、子进程、SSE）；写事件；
3. 新建任务，`active_task_id` 切过去；
4. 旧任务保留为 `INTERRUPTED`，可显式 `/resume`。

- **不做确认弹窗**；`UNRELATED` 只能由显式 `/new` 或策略高置信（≥0.85）产生。
- 中断/取消都**不回滚**已产生的副作用。
- 新建失败不做补偿。

---

## 8. 单活跃保证与会话锁

- `RuntimeClient` 内维护 `session_id -> runner`；`submit_user_input` 进入时：
  - 已有 runner → 按 §5 处置（默认追加，不并发）；
  - 无 runner → 正常建任务 / 恢复。
- **进程内会话锁**即可（当前单进程 asyncio），粒度 = `session_id`。
- **历史遗留多运行态**：以"最近变为运行态的任务"为 foreground，其余不主动处理。

---

## 9. 事件与持久化

每次输入处置在事件日志留痕，保证可回放、可幂等、可排障。**复用现有事件类型，不新增**：

| 动作 | 事件 |
|---|---|
| 输入被路由 | `runtime_input.routed`（`input_id` / `relation` / `reason_code` / `confidence` / `text_hash`） |
| 补充 | `steering.queued` → `steering.applied` |
| 替换 | `task_spec.revised`（writer=`runtime-steering`） |
| 停旧建新 | `task.state_changed`（INTERRUPTING/INTERRUPTED）+ `session.active_task_changed` + `task.created` |
| 排队 | `session.follow_up_queued` |

- **幂等键**：`input_id`。重复调用返回首次结果，不重复落事件。
- **命令类型**：统一 `user_input`（替代现有 `session_text` / `runtime_input`）。

---

## 10. 接口改动

### 10.1 `RuntimeClient`（SDK，最上层）
- 新增 `submit_user_input(...)`。
- `submit_session_text` / `route_input` 改造成它的内部路径（开发阶段不做兼容）。
- 保留 `steer` / `replace` / `interrupt` 便捷方法。

### 10.2 Kernel / 端口
- 新增 `SessionInputRelationPort` + `DefaultSessionInputRelationJudge`（返回 `SUPPLEMENT`），composition 默认注册。
- `SUPPLEMENT` / `REPLACE` 复用现有 `queue_steering` / `apply_pending_steering` / `task_spec.revised` 路径。
- `UNRELATED` 复用现有 `resolve_session_input` 路径（停旧建新）；`UNKNOWN` 走 `SUPPLEMENT`。
- 瞬时态收到输入 → 返回"处理中，请稍后"的可重试结果。

### 10.3 Web 端点
| 方法 | 路径 | 请求体 | 返回 |
|---|---|---|---|
| POST | `/session-input` | `{session_id, request_id, text, intent?}` | 202/200，`UserInputResult` |
| POST | `/tasks/{task_id}/stop` | `{reason?}` | 202，`{task_id, state}` |
| POST | `/tasks/{task_id}/input` | `{text, intent, request_id}` | 200，`UserInputResult` |

`intent`：`steer` / `replace` / `new`（不传 = 走判断器）。

### 10.4 CLI
- 删除 CLI 自己的路由判断（`ChatDispatcher` 包装），直接调 `application.kernel.route_runtime_input`。
- **CLI 不直接调 `submit_user_input`**：那个入口在 SDK 客户端上（因为它持有 runner 注册表），而 CLI 自己管理交互循环的 asyncio task。三方统一的是**决策源**（Kernel 路由内的共享判断器），执行仍各按自己的 runner 机制。
- 保留 `/steer` `/replace` `/new` `/stop` `/resume` `/queue`，映射到 `explicit_intent`。

---

## 11. 测试清单

| 文件 | 用例 |
|---|---|
| `tests/test_sdk_runtime.py` | 无任务→新建；运行中 + 默认判断器 → **追加到当前任务**（不新建、不打断）；显式 `/new`；`/stop`；幂等（同 input_id 同结果）；会话锁（并发两输入只一个 runner） |
| 新增 `tests/test_session_input_relation.py` | 默认实现恒返回 `SUPPLEMENT`；替身策略返回 `UNRELATED` → 停旧建新；返回 `REPLACE` → 换目标；低置信 → 落入 `SUPPLEMENT` |
| `tests/test_kernel_turns.py` | `SUPPLEMENT`/`REPLACE` 走 steering 路径；瞬时态返回可重试结果 |
| `tests/test_web_phase1_state.py` / `test_web_egress.py` | 三个 Web 接口的状态码与返回结构 |
| `tests/test_session_context.py` | 运行中任务出现在判断器上下文里，但不作为 `FOLLOW_UP` 源 |

---

## 12. 依赖：取消功能现状（2026-09 核实）

### 12.1 已有
- 状态机三态 `INTERRUPTING` / `INTERRUPTED`（可恢复）/ `CANCELLED`（终态），转换已定义（`core/task.py:35-57,144-160`）。
- `CancelTaskInput` 已接线到 kernel（`core/kernel.py:3045-3055`）。
- **中断已实现**：`sdk/runtime.py:657-666` `interrupt()` = `InterruptTaskInput`（→ INTERRUPTED）+ `runner.cancel()`；子进程由 process supervisor 在 `CancelledError` 时终止。

### 12.2 缺
- 公共取消入口、durable 取消意图 + 轮次边界协作检查、`INTERRUPTING` 对账、in-process 资源 scope（`task-cancellation-spec.md` Phase 1/2）。
- **本版用"中断"顶替取消**；真取消上线后只把 §7 第 2 步换成 `CANCELLED`，上层不动。

---

## 13. 暂缓项

1. `/queue` 的投递：目前只有 CLI 会取排队消息，Web 没有；暂定"排队仅 CLI 可用"。
2. 附件：`steer` 只支持文本；带图输入走新建任务。
3. `/resume` 当前任务的处置：过渡期默认中断；真取消上线后默认取消。
4. 判断器策略：默认恒返回 `SUPPLEMENT`（追加、不打断）；模型 / 规则策略后续补，上线后才会自动识别 `UNRELATED`。

---

## 14. 实施进度（第一刀）

已完成：

- **判断器端口**：`ports/session_input_relation.py`（`SessionInputRelation` / `SessionInputRelationJudgement` / `SessionInputRelationPort`）。
- **默认实现**：`adapters/default_session_input_relation/judge.py`（恒返回 `SUPPLEMENT`），`composition._kernel_dependencies` 默认注册（已有则不覆盖），并注入 `KernelDependencies.session_input_relation`。
- **统一入口**：`RuntimeClient.submit_user_input(...)`（`sdk/runtime.py`）——会话锁、显式意图优先、瞬时态返回"处理中"、运行态走判断器、无运行态走原会话路由；`UNRELATED` 时先中断旧任务再新建。
- **决策收口在 Core**：`kernel.route_runtime_input` 的语义来源由旧 `RuntimeInputClassifier` 换成**共享判断器**（`SUPPLEMENT→STEER`、`REPLACE→REPLACE`、`STATUS_QUERY→STATUS_QUERY`，置信度 ≥0.85）。SDK 的 `_steer_active` 也改为经 `route_runtime_input` 执行，三方（Web/CLI/SDK）**共用一个决策源**。
- **Web**：`/session-input` 改走统一入口（支持可选 `intent`）；新增 `POST /tasks/{task_id}/stop`、`POST /tasks/{task_id}/input`。
- **CLI**：删除 `ChatDispatcher` 包装，两处直接调 `application.kernel.route_runtime_input`；不再使用旧分类器，自动获得共享判断器与默认 `SUPPLEMENT`。
- **测试**：`tests/test_session_input_relation.py`（默认判断器、注册、新建、补充不新建、显式新建停旧、显式中断、并发输入只一个运行任务）；`tests/test_runtime_input_router.py` 的分类器用例改为关系判断器用例。

待办：

- **判断器策略实现**（模型 / 规则）：替换默认实现即可，上层不动。
- `/queue` 的 Web 投递。
- CLI 的 `UNRELATED` 自动"停旧建新"：CLI 交互循环内暂未实现，当前由判断器返回 `AMBIGUOUS` 时提示用户用显式命令；`SUPPLEMENT`/`REPLACE`/`STATUS_QUERY` 已生效。
