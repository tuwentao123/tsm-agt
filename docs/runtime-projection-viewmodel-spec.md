# Runtime → Web Projection → Renderer 最小改造 SPEC

## 1. 目标与边界

第一阶段只解决一个问题：

> Web UI 不再直接读取 Runtime domain model，也不再自行推导 Task、Trace、Interaction 和 Failure 状态。

建立一条最小稳定链路：

```text
RuntimeEvent / SessionEvent / TaskSnapshot
                 ↓
        WebSessionProjector
                 ↓
       WebSessionProjectionV1
                 ↓
        ViewModel Adapter
                 ↓
        Frontend Session Store
                 ↓
              Renderer
```

职责边界：

- Runtime 继续负责执行、持久化、审批和状态机。
- `WebSessionProjector` 是 Runtime 到 Projection 的唯一转换入口。
- `WebSessionProjectionV1` 是稳定 projection contract，而不是 Renderer 内部状态结构。
- ViewModel Adapter 负责 Projection DTO 到 Renderer ViewModel 的最终映射。
- Frontend Store 只保存最新完整 Projection 和纯 UI local state。
- Renderer 只消费 ViewModel，不直接消费 Runtime model 或 transport DTO。
- 页面刷新或进程重启后，可从 SQLite 重建相同 Projection。

第一阶段不做：

- 新 Timeline 存储或完整 Event Sourcing 改造。
- 修改 `RuntimeEvent`、`SessionEvent`、`TaskSnapshot`。
- Projection cache、增量 patch、虚拟列表。
- 分布式排序、跨设备同步。
- AI 标题、语义摘要、搜索和高级分析。

## 2. 复用与分层

复用现有事实和投影：

| 现有能力 | 用途 |
|---|---|
| `SessionEvent.sequence` | Session 内容的权威顺序 |
| `RuntimeEvent.sequence` | 单个 Task Trace 的权威顺序 |
| `SessionContextProjector.project()` | 重建 Session 消息和 Task summary |
| `FlowProjector` | 重建 Task 流程、节点状态和失败归因 |
| `TaskSnapshot` | 读取 pending approval / clarification 等执行事实 |
| `RuntimeProgressEnvelope` | 仅触发当前页面刷新，不作为历史事实 |

只新增一个后端组合层：

```python
WebSessionProjector.project(
    session_snapshot,
    session_events,
    task_snapshots,
    runtime_events_by_task,
) -> WebSessionProjectionV1
```

`WebSessionProjector` 负责：

- 排列 Session 内容和每个 Task 的 Trace。
- 将 Runtime 状态映射为稳定 UI 状态。
- 生成 UI-safe title、interaction 和 failure。
- 过滤 Prompt、完整 ToolResult、环境变量、凭据等内部数据。

它不执行 Tool、不改变 Task 状态、不自动重试，也不接受授权。

Frontend Store 第一阶段只做整份替换：

```text
收到 projection → replace store → render
```

Store 可以保存当前 Tab、展开项、滚动位置、输入草稿和本地 optimistic banner，但不得推导 Task 状态、配对 Runtime 事件、聚合 Trace 或判断失败原因。

状态 ownership：

| 状态类型 | Ownership |
|---|---|
| Task 状态、Trace 状态、Interaction 状态 | Projection |
| expanded/collapsed、selected trace、draft、scroll position | UI local state |

第一阶段允许 Projection DTO 直接映射为简单 ViewModel，但必须保留独立 adapter 边界，禁止 Renderer 直接依赖 Projection 内部结构。

## 3. Semantic Event Contract

Projection 内部必须基于 semantic event 构建，而不是直接输出 UI snapshot。

semantic event 与 UI event、transport payload 的区别：

| 类型 | 作用 | 稳定性 |
|---|---|---|
| RuntimeEvent | Runtime 内部执行事实 | 可演化 |
| ProjectionSemanticEvent | Projection 语义事件 | 必须稳定 |
| Transport DTO | API/SSE 传输格式 | 可版本化 |
| ViewModel | Renderer 展示模型 | UI 可演化 |

第一阶段最小 semantic event：

```ts
type ProjectionSemanticEvent =
  | 'task_created'
  | 'task_status_changed'
  | 'trace_started'
  | 'trace_finished'
  | 'interaction_requested'
  | 'interaction_resolved'
  | 'task_failed'
  | 'task_completed'
```

约束：

- semantic event 必须 append-only。
- semantic event 不允许携带 Renderer layout 信息。
- UI event 不得回写 semantic event。
- SSE payload、REST payload 与 semantic event schema 必须解耦。

## 4. Projection Contract

第一阶段只定义一个顶层 DTO：

```ts
type WebSessionProjectionV1 = {
  schemaVersion: 1
  sessionId: string
  sessionCursor: number
  title: string
  activeTaskId?: string
  items: ConversationItemVM[]
}

type ConversationItemVM =
  | {
      kind: 'user_message' | 'assistant_message'
      id: string
      sequence: number
      text: string
      taskId?: string
    }
  | {
      kind: 'task'
      id: string
      sequence: number
      task: TaskCardVM
    }

type TaskCardVM = {
  taskId: string
  title: string
  status: TaskDisplayStatus
  statusLabel: string
  updatedAt: string
  traceCursor: number
  trace: TraceItemVM[]
  interaction?: InteractionVM
  failure?: FailureVM
}
```

Renderer 禁止读取 `TaskSnapshot`、`RuntimeTaskResult`、`RuntimeEvent` 或 `task.goal`。

DTO 与 adapter 分层：

```text
Runtime Model
  ↓ Runtime → Projection Adapter
Projection DTO
  ↓ Projection → Transport Adapter
REST/SSE DTO
  ↓ DTO → ViewModel Adapter
Renderer ViewModel
```

约束：

- Projection DTO 不得直接复用 Runtime domain object。
- Transport DTO 不得暴露 Runtime 内部字段。
- Renderer package 不得依赖 Runtime domain package。
- Renderer/ViewModel 不得 import Runtime DTO namespace。
- Projection 层负责稳定语义；transport 层负责兼容与版本化。

Task 标题不使用 AI，按以下顺序确定：

1. 与 Task 关联的首条 User Message 首行，裁剪到 80 字符。
2. 没有可见 User Message 时，使用 `任务 <taskId 前 8 位>`。

## 5. 顺序与状态

系统只有两套独立顺序：

| 范围 | Cursor | 用途 |
|---|---|---|
| Session | `SessionEvent.sequence` | 排列 User Message、Task Card、Assistant Message |
| Task | `RuntimeEvent.sequence` | 排列该 Task 的 Trace |

规则：

- Task 卡片位置来自 `session.task_attached.sequence`，失败后也不得移到末尾。
- Interaction 嵌在所属 Task 卡片内，不参与 Session 顶层排序。
- `eventId` 只用于身份和去重，`occurredAt` 只用于展示。
- 禁止用 Task Trace sequence 做跨 Task 排序，也禁止按 UUID 或时间排序。

状态由 `WebSessionProjector` 唯一映射：

```ts
type TaskDisplayStatus =
  | 'pending'
  | 'running'
  | 'waiting'
  | 'interrupted'
  | 'failed'
  | 'cancelled'
  | 'completed'
```

| Runtime 状态 | UI 状态 |
|---|---|
| `CREATED` 到 `ROUTING` | `pending` |
| `EXECUTING`、`RUNNING_WORKFLOW`、`VERIFYING`、`FINALIZING` | `running` |
| `AWAITING_APPROVAL`、`AWAITING_USER`、`CONFLICT` | `waiting` |
| `INTERRUPTED`、`INTERRUPTING` | `interrupted` |
| `FAILED` | `failed` |
| `CANCELLED` | `cancelled` |
| `SUCCEEDED` | `completed` |

Task 终态覆盖页面总状态；Trace 节点状态来自 `FlowProjector`。Renderer 不得从 progress 文本推导状态。

## 6. Trace、Interaction 与 Failure

Trace 由 `FlowProjector` 转换，浏览器不再配对 started/completed 事件：

```ts
type TraceItemVM = {
  id: string
  sequenceStart: number
  sequenceEnd?: number
  kind: 'phase' | 'model' | 'tool' | 'process' | 'interaction'
  title: string
  status: TaskDisplayStatus
  summary?: string
  occurredAt: string
  finishedAt?: string
}
```

第一阶段只展示阶段、Model、Tool、Process、等待操作和最终结果。

审批、澄清和普通续作必须保持不同语义：

```ts
type InteractionVM =
  | {
      kind: 'approval'
      requestId: string
      risk: string
      action: string
      target: string
      preview: string
      networkAccess: boolean
      dataTransmission: string
      rollback: string
    }
  | {
      kind: 'clarification'
      requestId: string
      question: string
      inputMode: 'text' | 'choice'
      choices?: { value: string; label: string }[]
    }
  | {
      kind: 'continuation'
      summary: string
      action: 'derive_follow_up'
      sourceTaskId: string
    }
```

- Approval/Clarification 使用原 Task 的强类型接口和 request identity。
- 普通 continue/retry 创建新的 FOLLOW_UP Task。
- Projection 只返回当前有效 interaction；一次性 token 不进入 Store。

失败使用结构化数据：

```ts
type FailureVM = {
  code: string
  title: string
  summary: string
  stage: 'planning' | 'model' | 'tool' | 'verification' | 'runtime'
  recovery:
    | 'follow_up'
    | 'resume_interaction'
    | 'manual_restart'
    | 'none'
  action:
    | { kind: 'derive_follow_up'; sourceTaskId: string }
    | { kind: 'resume_request'; requestId: string }
    | { kind: 'none' }
}
```

失败来源依次为 Task 终态、`FlowProjection.first_actionable_failure()` 和最终 verification 结果。禁止扫描文本关键词、回退到 `task.goal`，或用通用 retry boolean 恢复旧 checkpoint。

Failure recovery semantics：

- `follow_up`：创建新的 FOLLOW_UP Task，不恢复原 Runtime checkpoint。
- `resume_interaction`：恢复原 approval/clarification request identity。
- `manual_restart`：用户显式重新执行 Task。
- `none`：只允许查看失败信息，不提供恢复入口。

Projection 必须可通过持久事件 replay 重建；单个 Task projection 失败时，不得阻塞整个 Session projection。

## 7. API 与刷新

新增只读接口：

```text
GET /v1/sessions/{sessionId}/projection
```

返回完整 `WebSessionProjectionV1`。刷新流程保持简单：

```text
页面打开或收到 SSE / 轮询变化信号
  → GET projection
  → Store replace
  → Renderer render
```

现有 `/sessions/{id}`、`/tasks/{id}` 和 progress SSE 暂时保留兼容。Projection 必须只依赖 SQLite 中的 Task/Session 状态和持久事件重建；进程内 progress 只能触发刷新。

## 8. 迁移步骤

### Phase 1：建立后端边界

- 新增 `WebSessionProjector`、`WebSessionProjectionV1` 和只读 API。
- 复用 `SessionContextProjector` 与 `FlowProjector`。
- 旧 Web 渲染保持不变。

### Phase 2：切换 Web 页面

- Store 保存完整 Projection。
- Conversation 和 Trace 改读 Projection。
- SSE/轮询只触发 refetch；旧接口作为回退。
- 新旧 timeline 并行校验 projection parity。

Projection parity validation：

- 新旧 timeline 的 task count 必须一致。
- interaction count 与 terminal state 必须一致。
- failure task 的最终状态与恢复动作必须一致。
- replay 后生成的 projection hash 必须稳定。

### Phase 3：稳定后删除旧推导

- 删除 `taskStateClass()`、`traceStateForProgress()`、`buildTraceSteps()`。
- 删除 `task.goal` 标题渲染和 message/task 混装恢复逻辑。
- 保留旧 Runtime/SDK 接口，不要求其他调用方同时迁移。

## 9. 第一阶段验收

1. Renderer 只读取 `WebSessionProjectionV1`，不读取 Runtime domain model。
2. Session 与 Task Trace 分别只按各自的 `sequence` 排序。
3. Task 状态、Trace 状态和失败原因不再由前端文本推导。
4. 进程重启后可从 SQLite 重建相同 Projection。
5. 已完成 Tool/Model 节点不会恢复成 running。
6. Task 终态不会被 progress 覆盖。
7. Approval/Clarification 精确绑定原 Task request identity。
8. 普通 continue/retry 只创建 FOLLOW_UP Task。
9. Projection 不包含 Prompt、完整 ToolResult、环境变量或凭据。
10. 旧 Web/Runtime/SDK 接口在迁移期仍可使用。
11. Renderer 与 Runtime domain package 之间不存在直接依赖。
12. Projection DTO、transport DTO 与 Renderer ViewModel 不共享同一数据结构。
13. Projection replay 与 migration parity validation 可验证新旧实现一致性。

第一阶段稳定后，再按实际性能问题决定是否增加 patch、cache、virtualization、selector、Trace compaction 或 Replay 优化；这些能力不进入当前实现范围。
