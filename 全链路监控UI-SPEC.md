# tsm-agt 全链路监控 UI 实现 SPEC

> 文档状态：Draft v1
>
> 适用项目：`tsm-agt`
>
> 产品定位：本机、只读的 Agent 执行链观察器
>
> 实施方式：按里程碑逐步交付，每个里程碑独立实现、验证和停止

---

## 1. 背景

`tsm-agt` 已经能够把 Agent 执行过程写入 Runtime Event Log，并通过 `FlowProjector` 投影为 Task、Phase、Turn、Model、Tool、Approval、Process、Mutation、Memory、Verification 等流程节点。现有 CLI/TUI、Replay 和静态 HTML 已能展示部分执行链，但仍缺少一个持续运行、可实时刷新、可查看完整 Session 历史的 Web 页面。

当前用户只能在终端中看到连续输出。当执行步骤较多、发生改道、重试、中断、审批等待或最终验收失败时，很难快速回答：

1. 当前 Session 包含哪些 Task？
2. 当前 Task 执行到了哪个阶段？
3. 模型先后调用了哪些工具？
4. 某个节点为什么成功、失败、取消、被替代或等待？
5. 最终结果为什么被 Verification 接受或拦截？
6. Runtime 重启后，历史执行链是否还能完整恢复？

本 SPEC 在不改变 Agent 执行逻辑的前提下，为这些问题提供一个只读 Web 观察界面。

---

## 2. 产品目标

### 2.1 核心目标

用户可以从 Session、Task、Turn 三个层级查看一次 Agent 交互的完整执行过程，并且能够：

- 快速判断 Agent 当前执行到哪里；
- 按时间顺序查看模型调用、工具调用、Evidence、Completion 和 Verification；
- 展开任一节点查看状态、耗时、范围、结果摘要和失败原因；
- 在任务运行时看到实时进度；
- 在 Runtime 重启后恢复持久化历史；
- 从失败结果一键定位第一个可操作失败节点；
- 区分执行失败、策略拒绝、调用替代、用户等待和最终验收阻塞。

### 2.2 成功标准

第一版完成后，用户不查看 SQLite、不阅读原始 Event，也能回答：

```text
这个 Session 里发生了几轮任务？
当前任务跑到了哪里？
每轮模型之后调用了什么工具？
哪个步骤耗时最长？
哪个节点首先失败？
为什么最终状态是 SUCCEEDED / FAILED / BLOCKED / INTERRUPTED？
```

---

## 3. 非目标

监控 UI 不是 Agent 操作台。第一版明确不提供：

- 创建 Task；
- 输入新的 Agent 指令；
- Interrupt；
- Steer；
- Replace；
- Queue；
- 审批或拒绝；
- 回答 Clarification；
- 恢复中断 Task；
- 修改 Task SPEC；
- 修改 Working Memory；
- 文件编辑、命令执行或任何工作区写操作；
- 远程访问；
- 多用户账号系统；
- 团队级集中监控平台；
- 模型隐藏思维链展示；
- 第一版中的复杂自由布局 DAG；
- 第一版中的跨 Task 指标大盘。

审批、Steering、恢复等行为仍可作为历史节点展示，但页面不能发起这些操作。

---

## 4. 设计参考与取舍

### 4.1 参考模式

参考 Codex、Qoder、OpenCode 等 Agent 的公开产品或协议设计，监控视图通常采用以下层级：

```text
Session / Thread
  └─ Task / Turn
      └─ Item / Part / Step
```

共同特征是：

- Session 是用户持续交互的容器；
- Turn 或 Task 表示一次目标执行；
- 模型调用、工具调用和结果是稳定 Item；
- `started / updated / completed` 更新同一个 Item；
- 页面消费领域投影，而不是自己解释底层日志；
- 持久化历史负责恢复，实时事件负责当前进度。

### 4.2 tsm-agt 的选择

`tsm-agt` 不再新增一套与 Flow 重复的 Monitor Node 模型。统一采用：

```text
Session
  └─ Task
      └─ Turn
          └─ FlowNode（页面中称为 Flow Item）
```

后端继续使用现有 `FlowProjection`、`FlowTimeline` 和 `FlowNodeDiagnostic`。Web 层只增加 Session 级投影、查询边界和展示 DTO，不重新实现 Flow 规则。

### 4.3 第一版默认视图

第一版默认展示“顺序执行流”，辅助展示“调用树”和“时间线”。不默认展示复杂 DAG。

原因：

- Agent 执行首先是时间序列；
- 顺序流更容易看清模型与工具交替过程；
- 大型 DAG 容易产生节点重叠；
- 现有父子关系足以生成调用树；
- DAG 可在后续确有需求时复用现有 Edge 增加。

---

## 5. 核心术语

### 5.1 Session

一次持续的用户交互会话。Session 可以包含多个 Task，并保存 Task 的先后关系、当前活跃 Task 和会话状态。

### 5.2 Task

一次具体目标的执行单元。Task 可能由独立请求创建，也可能由后续输入恢复、承接或替代旧任务。

### 5.3 Turn

Task 内的一次 Agent Loop。一个 Turn 可以包含多次模型调用和工具调用，直到得到最终回复、等待用户、意外中断或失败。

### 5.4 Flow Item

页面对 `FlowNode` 的用户友好称呼。一个 Flow Item 表示一个稳定动作或阶段，例如：

- Project Resolution；
- Context Assembly；
- Model Call；
- Tool Call；
- Evidence Question；
- Approval；
- Clarification；
- Checkpoint；
- Completion Readiness；
- Verification；
- Final Answer。

### 5.5 Snapshot

从持久化 Event Log 构建的完整只读投影。Snapshot 是页面历史状态的事实来源。

### 5.6 Live Progress

当前 Runtime 进程内的实时进度，包括完整目标、工具参数、路径、查询范围和耗时。它用于增强实时体验，不作为历史事实来源。

---

## 6. 核心不变量

以下规则必须由实现和测试共同保证。

### 6.1 一个动作只有一个稳定节点

同一次 Tool Call 的 requested、started、completed 必须更新同一个 Flow Item，不能在页面生成三个互不相关的节点。

### 6.2 已开始节点必须有明确状态

节点只能处于已定义状态，不允许永久停在无法解释的 `RUNNING/OPEN`：

```text
PENDING
RUNNING
WAITING_USER
SUCCEEDED
FAILED
BLOCKED
CANCELLED
REPLACED
INTERRUPTED
```

工具没有真正执行但被策略替代时，必须显示 `REPLACED`，不能显示为工具失败，也不能永久显示运行中。

### 6.3 持久化 Event 是历史事实来源

Runtime 重启后，页面仅依赖 SQLite Event Log 就能重建已发生的执行链。进程内 Progress 丢失不能改变历史节点状态。

### 6.4 FlowProjector 是唯一 Event 解释器

前端不读取原始 Event 后自行判断状态。CLI、HTML 和 Web 必须消费同一个 Flow 投影语义。

### 6.5 监控查询不能改变状态

任何 Monitoring Query 都不得：

- 写入 Runtime Event；
- 更新 Task 或 Session；
- 创建 Checkpoint；
- 触发模型；
- 执行工具；
- 修改 SQLite；
- 修改工作区文件。

### 6.6 实时明文与持久化审计分离

- Snapshot：可恢复、可回放、内容受控；
- Progress：当前进程实时明文、不可恢复；
- 两者都不包含隐藏思维链、API Key 或环境变量。

---

## 7. 当前能力盘点

### 7.1 已有能力

| 能力 | 现状 | 监控 UI 用法 |
|---|---|---|
| Runtime Event Log | 已实现 | 历史事实来源 |
| SQLite Runtime Store | 已实现 | 持久化与重启恢复 |
| Session/Task 关系 | 已实现 | Session 详情基础 |
| FlowProjector | 已实现 | 唯一 Task Flow 投影器 |
| FlowProjection | 已实现 | 调用树、节点状态、边 |
| FlowTimeline | 已实现 | 时间线与泳道 |
| Node Diagnostic | 已实现 | 节点详情与失败归因 |
| Replay | 已实现 | 历史时点回放 |
| Cursor 增量读取 | 已实现 | SSE 断线续传 |
| Local HTTP/SSE | 已实现 | 可复用传输基础 |
| AgentProgress | 已实现 | 当前任务实时显示 |
| CLI/TUI/HTML | 已实现 | 展示逻辑与验收参考 |

### 7.2 主要缺口

- 缺少稳定的 Session Monitor Projection；
- Local API 没有开放 Flow/Timeline/Node/Replay 查询；
- 现有 Local API 同时包含写路由，不适合作为纯监控安全边界；
- 缺少 Web 静态资源服务；
- 缺少 Session 列表、顺序执行流、调用树和节点详情页面；
- 缺少浏览器端 SSE 自动重连和 Cursor 管理；
- 部分新增 Runtime Event 需要补充 Flow 映射；
- 缺少查询无副作用测试和浏览器端视觉回归。

---

## 8. 目标架构

```text
Agent Kernel
  ├─ Runtime Event ───────────────→ SQLite Runtime Store
  └─ AgentProgress ───────────────→ In-memory Progress Channel
                                             │
SQLite Event Log                             │
  ├─ Session Projector                       │
  └─ FlowProjector                           │
          │                                  │
          └──────── MonitoringQueryPort ─────┘
                            │
              ReadonlyMonitoringWebAdapter
                            │
                    GET + SSE + Static Assets
                            │
                       Monitoring Web UI
```

### 8.1 依赖方向

```text
Web UI → Monitoring HTTP Contract
HTTP Adapter → MonitoringQueryPort
Monitoring Query Adapter → Kernel/Store read APIs + FlowProjector
Kernel/Core → 不依赖 HTTP、React 或 Web 资源
```

禁止：

```text
Core → Web Adapter
Core → React/Vite
Web UI → SQLite
Web UI → RuntimeStorePort
HTTP Adapter → 直接拼装 Flow 状态
```

---

## 9. 后端领域模型

### 9.1 复用现有模型

以下模型不得复制：

- `FlowProjection`；
- `FlowNode`；
- `FlowEdge`；
- `FlowTimeline`；
- `FlowNodeDiagnostic`；
- `FlowReplay`。

如 Web 缺少字段，应优先扩展通用 Flow 模型或增加只读展示 DTO，不能在前端重新推导。

### 9.2 新增 Session Monitor Projection

建议新增：

```python
@dataclass(frozen=True, slots=True)
class SessionMonitorSummary:
    session_id: str
    state: str
    active_task_id: str | None
    task_count: int
    created_at: str
    updated_at: str
    total_model_calls: int
    total_tool_calls: int
    total_input_tokens: int
    total_output_tokens: int


@dataclass(frozen=True, slots=True)
class SessionTaskMonitorItem:
    task_id: str
    goal_summary: str
    state: str
    relation: str
    source_task_id: str | None
    created_at: str
    updated_at: str
    model_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    first_failure_node_id: str | None


@dataclass(frozen=True, slots=True)
class SessionMonitorProjection:
    summary: SessionMonitorSummary
    tasks: tuple[SessionTaskMonitorItem, ...]
    cursor: int
```

`relation` 第一版只使用 Runtime 已持久化的确定事实，例如：

- `NEW_TASK`；
- `RESUMED`；
- `REBASED`；
- `REPLACED`；
- `UNKNOWN`。

不得通过用户中文关键词猜测关系。

### 9.3 页面状态映射

页面使用统一状态，但必须保留原始状态供详情展示：

| 页面状态 | 可映射的底层状态 |
|---|---|
| `PENDING` | CREATED、INTAKE、未开始节点 |
| `RUNNING` | EXECUTING、RUNNING_WORKFLOW、started |
| `WAITING_USER` | AWAITING_APPROVAL、AWAITING_USER |
| `SUCCEEDED` | SUCCEEDED、passed、completed success |
| `FAILED` | FAILED、tool failure、verification failed |
| `BLOCKED` | policy denied、verification blocked |
| `CANCELLED` | CANCELLED、action cancelled |
| `REPLACED` | action disposed as REPLACE |
| `INTERRUPTED` | INTERRUPTED、safe checkpoint preserved |

状态映射属于 Core Flow Projection 或可插拔只读 Projector，不属于 React 组件。

---

## 10. Monitoring Query Port

新增只读 Port：

```python
class MonitoringQueryPort(RuntimeAdapter, Protocol):
    async def list_sessions(
        self, *, cursor: str | None = None, limit: int = 50,
        state: str | None = None,
    ) -> SessionMonitorPage: ...

    async def get_session(
        self, session_id: str,
    ) -> SessionMonitorProjection: ...

    async def get_task_flow(
        self, task_id: str,
    ) -> FlowProjection: ...

    async def get_task_timeline(
        self, task_id: str,
    ) -> FlowTimeline: ...

    async def get_node(
        self, task_id: str, node_id: str,
    ) -> FlowNodeDiagnostic: ...

    async def get_replay(
        self, task_id: str, sequence: int,
    ) -> FlowProjection: ...
```

### 10.1 默认实现

默认 Adapter：

```text
builtin.kernel-monitoring-query
```

职责：

- 调用已有 Kernel/Store 只读方法；
- 组合 Session Monitor Projection；
- 返回 Flow/Timeline/Diagnostic/Replay；
- 做分页和查询边界校验；
- 不保存任何新状态。

### 10.2 查询一致性

同一 SQLite Snapshot 上：

- CLI Flow 与 Web Flow 的 node ID、状态和 cursor 必须一致；
- Runtime 重启前后查询结果必须一致；
- 查询顺序不能影响结果；
- 重复查询不能产生 Event；
- Session 总计必须来自持久化事实，不能依赖当前进程计数器。

---

## 11. 只读 HTTP/SSE 协议

### 11.1 独立命名空间

所有监控路由使用：

```text
/monitoring/v1
```

避免与现有 Agent 操作 API 混淆。

### 11.2 快照接口

```http
GET /monitoring/v1/health
GET /monitoring/v1/sessions?limit=50&cursor=...&state=...
GET /monitoring/v1/sessions/{session_id}
GET /monitoring/v1/tasks/{task_id}/flow
GET /monitoring/v1/tasks/{task_id}/timeline
GET /monitoring/v1/tasks/{task_id}/nodes/{node_id}
GET /monitoring/v1/tasks/{task_id}/replay?sequence=100
```

### 11.3 实时接口

```http
GET /monitoring/v1/tasks/{task_id}/events?after=100&wait=30
GET /monitoring/v1/tasks/{task_id}/progress?after=20&wait=30
```

第一版复用现有通道语义：

- Events SSE：持久化、脱敏、可续传；
- Progress SSE：进程内、详细、不可恢复。

### 11.4 第一版刷新协议

第一版不设计 Flow Delta：

```text
收到 Events SSE 新 cursor
  → 前端防抖 100~300ms
  → 重新 GET /tasks/{task_id}/flow
  → 按 node_id 更新页面
```

当真实性能数据证明全量 Snapshot 成为瓶颈后，再新增：

```http
GET /monitoring/v1/tasks/{task_id}/flow/delta?after=100
```

### 11.5 HTTP 方法限制

Monitoring Adapter 只注册：

```text
GET
HEAD（仅静态资源和健康检查，可选）
OPTIONS（仅必要的同源能力声明，可选）
```

对 `POST / PUT / PATCH / DELETE` 统一返回：

```http
405 Method Not Allowed
Allow: GET, HEAD
```

### 11.6 错误协议

```json
{
  "error": {
    "code": "TASK_NOT_FOUND",
    "message": "task was not found",
    "request_id": "request-..."
  }
}
```

错误响应不得返回 Stack Trace、绝对敏感路径、Token、环境变量或原始数据库内容。

---

## 12. Web 页面信息架构

### 12.1 页面一：Session 列表

展示：

- Session ID；
- 状态；
- Task 数；
- 当前活跃 Task；
- 创建时间；
- 最近活动时间；
- 总耗时；
- 模型调用数；
- 工具调用数；
- Token 总计。

支持：

- 状态筛选；
- 时间排序；
- 分页；
- 打开 Session。

不支持任何写操作。

### 12.2 页面二：Session 详情

以时间顺序展示：

```text
Session
  ├─ Task A：目标摘要 [SUCCEEDED]
  ├─ Task B：目标摘要 [INTERRUPTED]
  └─ Task C：从 B 恢复 [RUNNING]
```

每个 Task 卡片显示：

- 目标摘要；
- Task 状态；
- 与前序 Task 的关系；
- 开始/结束/耗时；
- 模型和工具调用数；
- Token；
- 首个失败节点；
- 是否发生中断、恢复或 Rebase。

### 12.3 页面三：Task 执行详情

布局：

```text
┌──────────────────────────────────────────────────────────┐
│ Task 摘要：状态、耗时、模型/工具调用、Token、Evidence    │
├───────────────────────┬──────────────────────────────────┤
│ 执行流 / 调用树 / 时间线 │ 节点详情                         │
│                         │                                  │
│ Turn 1                  │ 状态、耗时、范围、结果、失败原因 │
│  Model 1                │ Evidence、上下游、Event Cursor   │
│   Tool A                │                                  │
│  Model 2                │                                  │
└───────────────────────┴──────────────────────────────────┘
```

默认标签：`执行流`。

### 12.4 顺序执行流

按实际发生顺序展示：

```text
用户输入
模型调用
工具调用
工具结果
模型调用
Completion Readiness
最终回答
Verification
```

相同 Tool Call 的 started/updated/completed 必须显示为一个卡片的状态更新。

### 12.5 调用树

```text
Task
└─ Turn 1
   ├─ Model Call 1
   │  └─ Tool Call 1
   ├─ Model Call 2
   │  ├─ Tool Call 2
   │  └─ Tool Call 3
   ├─ Completion Readiness
   └─ Verification
```

支持展开/折叠和状态筛选。

### 12.6 时间线

基于现有 `FlowTimeline` 展示泳道：

- Runtime；
- Model；
- Tool；
- User Wait；
- Verification。

第一版支持：

- 时间比例条；
- 当前运行节点高亮；
- hover 查看耗时；
- 点击定位节点详情。

### 12.7 节点详情

通用字段：

- Node ID；
- 类型；
- 原始状态；
- 页面状态；
- 开始/结束时间；
- 耗时；
- 所属 Task/Turn；
- Parent；
- Children；
- Incoming/Outgoing Edge；
- Event Anchor。

按节点类型展示：

- Model：Token、finish reason、Provider 重试、上下文估算；
- Tool：工具名、参数摘要、操作范围、结果摘要、错误码；
- Evidence：问题、状态、新增证据计数、处置原因；
- Approval：等待原因、目标、最终状态，只读；
- Completion：缺口、决定和纠正次数；
- Verification：criterion、状态和失败原因；
- Failure：归因、置信度和下游影响。

---

## 13. 实时体验

### 13.1 页面打开

```text
GET Flow Snapshot
  → 渲染完整历史
  → 记录最新 Event Cursor
  → 连接 Events SSE
  → 连接 Progress SSE
```

### 13.2 运行中更新

- Progress 到达：立即更新“当前正在做什么”和运行耗时；
- Event 到达：触发 Flow Snapshot 刷新；
- 同一 node ID：原位更新，不追加重复节点；
- Task 终态：停止 Progress 重连，保留 Event/Flow 查询；
- 浏览器切回页面：先获取 Snapshot，再恢复 SSE。

### 13.3 断线恢复

- 浏览器保存最后成功处理的 Cursor；
- 重连使用 `after=<cursor>`；
- Event ID 用于去重；
- Cursor 过旧或服务重启时重新获取完整 Snapshot；
- Progress 丢失只影响当前细节，不影响历史 Flow。

### 13.4 运行耗时

对 RUNNING 节点，前端可根据已持久化开始时间显示本地递增耗时，但不能把浏览器计算值写回 Runtime 或当作最终耗时。最终耗时以后端完成时间为准。

---

## 14. 前端工程结构

前端放在独立目录，不进入 Python Core：

```text
web/
  package.json
  package-lock.json
  vite.config.ts
  tsconfig.json
  src/
    api/
      monitoringClient.ts
      sseClient.ts
      contracts.ts
    pages/
      SessionsPage.tsx
      SessionDetailPage.tsx
      TaskFlowPage.tsx
    components/
      SessionList.tsx
      SessionTaskStream.tsx
      FlowItemList.tsx
      FlowTree.tsx
      FlowTimeline.tsx
      NodeDetail.tsx
      LiveProgress.tsx
      StatusBadge.tsx
      FailureShortcut.tsx
    stores/
      monitoringStore.ts
    styles/
```

### 14.1 技术选型

建议：

- React；
- TypeScript；
- Vite；
- 轻量状态管理；
- CSS Grid + SVG；
- Fetch API 读取普通接口和 SSE。

第一版不引入 React Flow。所有依赖在真正进入 Web 实施步骤时选择并固定精确版本，本 SPEC 不提前写死可能过期的版本号。

### 14.2 SSE 客户端

由于 API 使用 Bearer Token，浏览器使用 `fetch()` 流式读取 SSE，而不是依赖不能稳定设置 Authorization Header 的原生 `EventSource`。

### 14.3 前端不保存敏感数据

- Bearer Token 默认只保存在页面内存；
- 不写入源码、Git、LocalStorage 或 URL；
- 刷新页面可由启动页重新注入或通过同源启动握手获得；
- 不缓存源码正文或完整工具结果。

---

## 15. 安全与隐私

### 15.1 网络边界

服务只允许绑定：

```text
127.0.0.1
::1
localhost
```

拒绝 `0.0.0.0`、局域网地址和公网地址。

### 15.2 认证

- 每次启动生成随机 Bearer Token；
- Token 只显示一次；
- Token 长度沿用现有 Local API 安全要求；
- 所有 Snapshot、SSE 和静态入口均受同一认证边界保护；
- 服务停止后 Token 失效。

### 15.3 内容边界

禁止展示：

- API Key；
- 环境变量值；
- `.env` 内容；
- 模型隐藏思维链；
- 未经设计允许的完整源码；
- 原始数据库内容；
- Mutation Backup；
- 审批 Token 或恢复 Token。

允许展示：

- 用户目标；
- 工具名称；
- 受控参数摘要；
- 当前操作路径；
- Evidence Question；
- 错误码和失败原因；
- Token/耗时/状态；
- 已经由 Flow/Progress 投影允许展示的字段。

### 15.4 只读强制

只读不是前端约定，而是后端能力约束：

- Monitoring Adapter 不依赖写命令接口；
- 不注册写路由；
- Query Port 不提供 mutation method；
- 查询测试比较 SQLite 和工作区前后 Hash；
- 架构测试禁止 Monitoring Adapter 导入 Tool Executor、Approval mutation 或 Workspace mutation 能力。

---

## 16. 性能与容量

### 16.1 第一版目标

- Session 列表默认 50 条；
- 单次最多返回 100 条 Session；
- Task Flow 目标支持 5,000 个节点；
- Snapshot JSON 默认限制响应体大小；
- SSE `wait` 最大 30 秒；
- 前端 Event 刷新防抖 100~300ms；
- 节点列表超过可视范围时使用虚拟滚动。

### 16.2 大 Flow 处理

若节点数量超过阈值：

- 默认折叠已成功的连续低价值内部节点；
- 允许按 Turn、类型、状态筛选；
- 节点详情按需查询；
- 不在第一版自动丢弃持久化历史；
- 后续基于真实性能数据决定是否增加 Flow Delta。

### 16.3 查询索引

M1 先测量现有 SQLite 查询。只有基准证明需要时，才增加 Session/Task/Cursor 相关索引；不能为了 Web 页面预先复制 Event 表。

---

## 17. macOS 与 Windows 兼容

### 17.1 通用实现

- HTTP 使用 Python 平台无关接口；
- 浏览器协议统一使用 JSON/SSE；
- 路径由现有 WorkspacePath/Flow 投影规范化；
- 前端不得按 `/` 或盘符自行解析权限；
- SQLite 查询使用现有 RuntimeStorePort；
- 静态资源路径使用 `pathlib.Path` 和现有平台边界。

### 17.2 验证要求

- macOS 执行完整自动化和浏览器验证；
- Windows 代码路径必须实现；
- Windows 原生验证条件未具备时，在里程碑结果中明确标注“已实现、暂未原生验证”；
- 不允许新增只在 POSIX 可用的启动脚本作为唯一入口。

---

## 18. 可观测性自身要求

Monitoring Server 自身只记录运行信息，不把自己的请求再次写进 Agent Runtime Event，避免观察行为污染被观察链路。

可以记录：

- 请求 ID；
- 路由模板；
- HTTP 状态；
- 响应耗时；
- 响应字节数；
- SSE 连接数；
- 查询失败类型。

不得记录：

- Bearer Token；
- 完整路径参数；
- 用户目标正文；
- 工具参数正文；
- 源码或模型输出正文。

---

## 19. 实施里程碑

### M0：Flow 完整性基线

目标：确认现有 Event 能稳定投影为 Web 所需节点。

实施内容：

1. 建立 Runtime Event → Flow Node 映射清单；
2. 盘点缺少终态或未映射的事件；
3. 为 `tool.action_disposed` 等新事件补 Flow 映射；
4. 固定节点 ID、Parent/Edge 和状态转换契约；
5. 建立真实 Session Golden Fixture；
6. 验证 SQLite 重启前后投影一致。

交付物：

- Flow 映射文档；
- Golden Event Fixture；
- Flow 完整性专项测试。

验收：

- 每个关键动作在 Flow 中可见；
- 同一动作不生成重复节点；
- 已开始节点有明确终态或可解释等待态；
- CLI Flow 现有功能不回退；
- 不涉及 HTTP 和前端依赖。

### M1：Monitoring Query Port

目标：建立稳定的只读查询边界。

实施内容：

1. 新增 Session Monitor 数据模型；
2. 新增 `MonitoringQueryPort`；
3. 实现默认 Kernel/Store Query Adapter；
4. 接入 Flow、Timeline、Node Diagnostic 和 Replay；
5. 加入分页、状态过滤和输入校验；
6. 增加查询无副作用检查。

验收：

- 不启动 HTTP 即可查询所有 Monitor Projection；
- 重复查询无 Event、无 SQLite 写入、无工作区变化；
- Runtime 重启前后结果一致；
- 非当前本地 Subject 的 Session 不可读取；
- 架构依赖测试通过。

### M2：只读 Monitoring HTTP Adapter

目标：通过 GET/SSE 暴露 M1 查询。

实施内容：

1. 独立 `/monitoring/v1` 路由；
2. Session/Flow/Timeline/Node/Replay GET；
3. Events/Progress SSE；
4. Bearer Token 和 Loopback 限制；
5. 统一错误协议；
6. 强制拒绝全部写方法。

验收：

- 无 Token 为 401；
- 非本地 Subject 为 403；
- 未知资源为 404；
- 所有写方法为 405；
- 非回环地址无法启动；
- SSE Cursor 可续传且不重复；
- 查询前后 SQLite/工作区不变化。

### M3：Web UI 静态只读版

目标：可查看已结束 Session 和 Task。

实施内容：

1. 初始化独立 Web 工程；
2. Session 列表；
3. Session Task 执行流；
4. Task 顺序执行流；
5. 调用树；
6. Timeline；
7. 节点详情；
8. 状态、空态、错误态和大数据量处理。

验收：

- 能查看已结束 Session；
- 能从 Session 进入任意 Task；
- 能定位任意 Model/Tool/Completion/Verification 节点；
- 能显示首次失败原因；
- 页面无写按钮；
- 浏览器网络面板无写请求；
- Web 构建产物可由 Monitoring Adapter 提供。

### M4：实时监控

目标：页面跟随运行中 Task 更新。

实施内容：

1. Events SSE；
2. Progress SSE；
3. Cursor 持久到页面内存；
4. 自动重连和去重；
5. Snapshot 刷新防抖；
6. 当前节点高亮和实时耗时；
7. Runtime 重启降级。

验收：

- 新节点在合理延迟内出现；
- 同一节点状态原位更新；
- 断线恢复无重复节点；
- Runtime 重启后历史完整；
- Progress 丢失不改变历史；
- Task 终态后停止无意义轮询。

### M5：回放与排障体验

目标：支持复杂链路问题定位。

实施内容：

1. Sequence 回放；
2. 第一个可操作失败快捷入口；
3. 类型/状态/Turn 筛选；
4. 失败链路和下游影响高亮；
5. 时间线缩放；
6. URL 中保存非敏感视图状态。

验收：

- 可回到任意合法 Sequence；
- 可区分工具失败、策略替代、等待用户和验收阻塞；
- 回放不执行模型、工具或副作用；
- 复制只读链接不包含 Token 或敏感内容。

### M6：可选指标聚合（不属于首版）

在 M0~M5 稳定后，才能评估：

- Task 成功率；
- P50/P95 总耗时；
- 模型/工具耗时；
- Token 使用；
- Provider 重试；
- Completion/Verification 拦截类型；
- 工具错误率。

指标必须由后端独立 Projection 计算，前端不能拉取全部 Event 自行统计。

---

## 20. 测试策略

### 20.1 单元测试

- Event → Flow Item 状态转换；
- Session Monitor Projection；
- 状态映射；
- Pagination/Cursor；
- Node Diagnostic；
- Replay；
- DTO 序列化和兼容性。

### 20.2 契约测试

- MonitoringQueryPort 可替换；
- Query Adapter 无副作用；
- HTTP 状态码和 Schema；
- SSE Cursor/去重；
- 认证和方法限制。

### 20.3 集成测试

- 使用真实 SQLite Fixture 查询；
- Runtime 重启恢复；
- Running → Terminal 实时更新；
- Approval/Clarification 只读展示；
- Interrupted/Resumed/Rebased 链路；
- Tool Action Replaced/Cancelled 展示；
- Verification Blocked 展示。

### 20.4 前端测试

- API Contract 类型测试；
- Session 列表；
- Flow Item 原位更新；
- SSE 重连；
- 空态/错误态；
- 大节点列表虚拟滚动；
- 节点详情；
- 无写操作断言。

### 20.5 视觉验收

至少准备以下固定场景截图：

1. 简单成功 Task；
2. 多模型/多工具 Task；
3. 等待审批；
4. 工具失败；
5. Action 被替代；
6. Task 意外中断后恢复；
7. Completion Readiness 阻塞；
8. Verification 失败；
9. 大于 500 节点的长 Task。

### 20.6 全量回归

每个里程碑至少执行：

- 新增专项测试；
- Flow/Replay/SDK/Local API 相关测试；
- 架构依赖测试；
- Python compileall；
- 前端 typecheck、unit test、build；
- 全量 Python 回归；
- 需要回环端口的测试在允许本地端口的环境运行；
- Windows 原生状态单独记录。

---

## 21. 版本兼容

### 21.1 Event 兼容

- 旧 Event 必须继续可投影；
- 新 Event 采用向后兼容字段；
- 未识别 Event 可以忽略，但必须记录投影诊断；
- 旧 Runtime.db 不要求迁移才能打开监控页面；
- Schema 升级必须有 Fixture 测试。

### 21.2 HTTP 兼容

- 路径包含 `/v1`；
- 响应包含 `schema_version`；
- 新增字段向后兼容；
- 删除或改变语义必须升级 API 版本；
- 前端不得依赖未声明字段。

### 21.3 Web 与 Agent 版本不一致

静态 Web 资源由当前 Monitoring Server 提供，避免用户单独安装不兼容前端。页面启动时检查 API Schema Version，不兼容时显示明确错误，不尝试猜测。

---

## 22. 文件规划

建议新增或调整：

```text
src/tsm_agt/core/
  session_monitoring.py          # Session 只读投影模型/Projector

src/tsm_agt/ports/
  monitoring_query.py            # 只读查询 Port

src/tsm_agt/adapters/
  kernel_monitoring_query/       # 默认查询 Adapter
  readonly_monitoring_web/       # GET/SSE/静态资源 Adapter

src/tsm_agt/bootstrap/
  monitoring_composition.py      # 独立监控 Composition

tests/
  test_session_monitoring.py
  test_monitoring_query.py
  test_monitoring_http.py
  test_monitoring_readonly.py

web/
  ...                            # 独立前端工程
```

是否拆分 `monitoring_composition.py` 在 M1 实施时根据现有 Bootstrap 复杂度决定，但监控 Adapter 必须保持独立注册和只读能力集合。

---

## 23. 启动方式（目标形态）

建议最终命令：

```bash
tsm-agt monitor --workspace . --host 127.0.0.1 --port 8766
```

启动输出只显示：

```text
Monitoring UI: http://127.0.0.1:8766/
Bearer token: <shown-once>
Scope: read-only, loopback-only
Workspace: <current workspace>
```

该命令只启动 Monitoring Query、HTTP/SSE 和静态页面，不启动新的 Agent Task，不修改现有 Session。

---

## 24. Definition of Done

首版全链路监控 UI 完成需同时满足：

1. 用户可以浏览本工作区 Session；
2. 用户可以查看 Session 内所有 Task；
3. 用户可以查看 Task 内 Turn、Model、Tool、Evidence、Completion、Verification；
4. 同一动作只显示一个稳定节点；
5. 节点状态和原因可解释；
6. 页面支持顺序执行流、调用树和时间线；
7. 运行中 Task 可以实时刷新；
8. Runtime 重启后历史可以恢复；
9. 可以定位首个可操作失败节点；
10. Monitoring Adapter 没有任何写能力；
11. 查询不会修改 SQLite、Runtime 状态或工作区；
12. 不暴露密钥、环境变量和隐藏思维；
13. macOS 完整验证通过；
14. Windows 代码路径实现，原生验证状态明确；
15. Python 与 Web 全量回归无新增失败。

---

## 25. 当前实施状态

```text
SPEC：已定义
M0：未开始
M1：未开始
M2：未开始
M3：未开始
M4：未开始
M5：未开始
M6：可选，未开始
```

建议下一步只实施 **M0：Flow 完整性基线**。M0 完成并验证后停止，再决定是否进入 M1。
