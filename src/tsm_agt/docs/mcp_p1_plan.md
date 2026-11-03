# MCP P1 实施方案

日期：2026-11-03。状态：待实施。以当前工作区 P0 代码为基础；本文只定义方案，未增加
运行时功能。P0 的 owner Task、取消清理、完整分页和 UNKNOWN_OUTCOME 继续保留。

建议分为 P1-A（运行可靠性）和 P1-B（工具目录与结果管理），各自拆成可独立验收的提交。
先完成配置、状态和有界等待，再增加重连；先完成目录契约与恢复兼容，再开启动态刷新。

## 1. 代码职责

| 模块 | 改造职责 |
| --- | --- |
| `ports/mcp.py` | 配置、每服务器状态、连接 generation、目录 DTO 与生命周期契约；manager 仍通过注入的 client factory 工作，不依赖 SDK |
| `bootstrap/mcp_configuration.py`、`composition.py` | 严格解析配置、默认兼容策略、依赖注入；传输参数与运行策略分开 |
| `adapters/mcp_python_sdk_client.py` | SDK owner、可观察关闭/异常信号、通知与 progress 回调、请求超时与取消 |
| `adapters/mcp_tool_provider.py` | 按服务器隔离发现失败、发布完整目录、检查调用契约、结果转换；不判断用户业务意图 |
| `core/conversation_loop/loop.py`、checkpoint / approval 相关模块 | 模型采样边界更新目录；核对待执行工具契约与既有授权、checkpoint 兼容性 |
| 各 model adapter | 工具短别名、历史消息编码、媒体投影与明确的能力降级 |
| SDK / CLI / Web | 查询同一份服务器状态；调用进度进入已有任务事件链 |

P1-A 可在现有 manager 内增加每服务器运行记录，不必同时搬迁所有模块。SDK 上下文仍由
P0 owner 持有；manager 协调连接代次和策略，不直接退出 SDK 上下文。

## 2. P1-A：运行可靠性

### A1. 配置、状态、启动隔离

新增服务器配置：`required`、`startup_timeout_seconds`、`tool_timeout_seconds`、
`shutdown_timeout_seconds`、`reconnect`；全局新增 `max_parallel_startups`。

- `required` 默认 true，保留现有“启用的服务器失败则启动失败”的行为；用户明确标为
  false 的服务器失败只影响自身。
- 启动并发默认保持 1，显式配置后可开启受限并行；与当前工具批内并发上限 8 分别管理。
- 新增布尔、整数、有限正数和互斥字段校验。未知运行策略字段明确报错，避免配置拼错后
  看似生效；旧 `options.env/cwd/headers/timeout_seconds` 保持兼容，继续只传明确支持项。
- manager 为每个 server 保存状态、generation、启动/失败阶段、最后错误分类、最近成功
  时间、重连次数和目录 revision；状态查询不建立新连接。
- 建议状态：DISABLED、STARTING、READY、DEGRADED、RECONNECTING、AUTH_REQUIRED、
  FAILED、CLOSING、CLOSED、CLOSE_FAILED。

启动逻辑：

```text
验证配置
→ 受 semaphore 限制启动每个 server
→ initialize + 完整分页发现 + schema 校验/工具过滤
→ 为该 server 提交完整目录并标为 READY
→ 可选 server 失败：记录状态，不发布它的工具
→ 必需 server 失败：停止本次启动，清理已启动连接，汇总错误
```

启动预算覆盖连接、initialize 和首次完整工具发现。READY 必须表示目录也已可用，不能
只因握手成功就宣布服务就绪。可选服务的坏 schema、分页循环也应局部失败。
无工具的合法服务可以连接就绪，但界面应显示工具数为 0。

首次版本等待所有启用服务器各自在预算内完成，再开始首轮模型调用；可选慢服务后台
迟到加入属于下一步目录更新机制，暂不同时引入。

### A2. 调用预算、取消与关闭确认

调用预算取 `ToolInvocationContext.deadline` 剩余时间与服务器调用上限的较小值。等待
连接恢复也消耗同一份预算；重连不能给原调用重新计时。服务器上限未配置时继承 Kernel
预算；配置更大的上限不会突破 Kernel 已给的 deadline。

SDK 请求超时、外层调用超时与用户取消统一沿用 P0 的派发/结果语义。SDK 明确证明未
发送时可报告未派发；无法确认时保持保守分类。调用后超时或丢失响应仍为 UNKNOWN_OUTCOME。

关闭实现需要区分“停止接收新调用”“请求 owner 清理”“确认 transport 已关闭”：

1. 停止新调用，取消排队/重连等待，向 owner 发停止信号。
2. 在宿主 deadline 和服务器关闭预算内等待关闭确认。
3. 超时进入 CLOSE_FAILED，记录仍由 manager 跟踪的清理任务，禁止创建下一代连接。
4. 对持有明确句柄的本地子进程，可按该句柄执行 terminate/kill 并再次等待确认；禁止按
   进程名扫描清理。所有 SDK 上下文退出仍在 owner Task 内执行。

不能简单 `wait_for(aclose())` 后就认为已经清理：P0 会等待清理完成再传播取消，这可能
使 wait_for 超过声明时间。需要将请求停止与等待关闭分开，用完成信号和计时器竞争。
SDK stdio helper 当前没有向 wrapper 暴露进程句柄，若要承诺强制回收期限，必须补充可
终止的传输契约/受控 stdio 实现；在此之前仅能承诺上层等待有界和禁止重叠重连，不能
把 CLOSE_FAILED 写成 CLOSED。

### A3. 受控重连

在每服务器运行记录上实现单一重连协调者；同一故障只能启动一个恢复流程，其他调用
等待它的 ready 信号或返回未派发错误。重连默认关闭，配置明确开启后启用。

- SDK 连接终止通知或调用中的连接异常触发状态失效；工具返回 isError 不触发重连。
- 先确认旧连接关闭，再按有上限的指数退避与抖动创建新连接；达到尝试上限进入 FAILED。
- 凭据失效进入 AUTH_REQUIRED；配置、schema、命令错误不无限重试。P1 只展示状态，
  OAuth 登录流程仍属 P2。
- 每次连接使用新 generation；旧连接的通知、就绪和失败回调不能修改当前状态。
- 新连接完成 initialize 和全量目录校验后才能 READY；其目录变化交给 B1 统一处理。
- 重建连接用于后续调用。已派发且结果未知的写操作不重放，不自动解除原任务的核对暂停。
- 第一版不引入业务调用自动重试；只读重试须以后通过本地策略明确声明并单独验收，
  不把服务端 readOnlyHint 当作权限来源。

### A4. 状态、诊断与调用进度

提供只读状态 DTO，至少包含 `server_id/state/generation/failure_stage/error_code/
tool_count/catalog_revision/reconnect_attempts`。默认输出不含密钥、headers、完整带参数 URL。

SDK Runtime 暴露状态查询，CLI doctor 的 MCP 检查和 Web 状态面板复用该入口。读取正在
运行实例的状态不会额外启动服务器；独立 doctor 主动探测需是显式选项，并走同样预算
与清理路径，因为 stdio 探测会真实执行配置的启动命令。

当前 `AdapterContext.emit_event` 在 registry 中为 no-op，仅向它发送事件不会让 Web
收到进度。需要分开接入：连接生命周期走应用级诊断快照；调用进度通过有 task/turn/
call 身份的事件入口提交并发布 SSE。复用现有提交后通知机制，限制进度频率；模型文本
流与 MCP progress 保持不同事件类型。

## 3. P1-B：工具目录、名称与结果管理

### B1. 工具过滤、目录 revision 与调用契约

先增加本地允许/排除列表，建议先支持精确名称和简单 glob，排除优先。名称使用服务器
原生工具名匹配，在转换为模型别名前执行；过滤失败明确报告配置问题。

目录采用不可变快照：

```text
server_id + connection_generation + catalog_revision
+ 原生工具名、输入/输出 schema、说明、服务端 annotations
+ 本地有效权限策略与每工具 contract_hash
```

`catalog_revision` 根据规范化的语义内容生成；generation、时间戳和工具返回顺序不参与
语义 revision。换连接但契约不变不应导致 prompt 和审批无意义失效。

收到 list_changed 后标记 dirty、合并通知、串行取回全目录；验证成功才一次替换快照。
旧 generation 通知直接丢弃。刷新失败保留旧目录用于诊断，但将其标为 stale，不允许
未经重新确认的旧契约调用继续派发；不能伪装为目录仍然新鲜。

贯通主执行链：

1. 模型请求绑定当时的目录快照及可见工具契约；新工具仍需经过原工具暴露和权限过滤，
   不能因 provider 新增工具就自动授予当前任务全部能力。
2. 执行前核对目标 `contract_hash`；契约变化/工具撤销时返回明确的目录变化结果，原
   调用不发送，让下一次模型采样重新决策。
3. 原批次中的已接受调用仍按各自契约校验；正在执行的调用保留历史契约和真实结果。
   仅在安全采样边界采用新的模型可见快照。
4. 审批恢复核对目标契约和本地策略；不使用旧审批执行新语义。连接换代且目标契约相同
   可以继续，其他无关工具新增不应无条件使该审批失效。
5. checkpoint 增加版本化的目录/契约引用。将可解释的 MCP 目录变化与真正的模型、
   策略、适配器冲突分开协调，不能删除全局 `toolset_hash` 检查来绕过兼容性保护。

当前 loop 只识别 SKILL_CATALOG_CHANGED；应新增通用目录变更接口/结果处理，避免为
每一种 provider 持续增加硬编码分支。MCP annotations 仍是数据，本地配置决定权限。

### B2. 稳定短别名

在模型适配层引入共享 codec，保存内部真实名到 wire name 的映射。合法且不冲突的既有
编码尽量保留；超长或冲突名称采用短前缀加稳定摘要，仍检查完整映射的唯一性。

- 内部调用、审批 payload、执行账本和 Web 始终保存/显示真实工具名。
- 名称分配对输入排序不敏感；故意冲突时明确拒绝或确定性扩展摘要，不能覆盖映射。
- 同一次请求同时编码当前工具和历史工具调用；已撤销工具的历史记录仍需正确序列化。
- codec 有版本，checkpoint 恢复保留可解释的映射；模型请求只接受当前广告中的反向映射，
  不能根据模型返回的摘要猜测路由。覆盖 OpenAI Chat / Responses 和 Anthropic 路径。

### B3. 大结果与媒体

先补协议无关的结果 artifact 存储/读取端口，再接 MCP renderer。当前只有 ImageBlock
输入和普通 JSON ToolResult，没有完整的 MCP 输出媒体与通用大结果取回机制。

1. 文本与 structuredContent 保留各自含义；只有能证明等价的文本副本才在模型投影去重。
2. 设置每结果字节/模型文本预算；预算内正常返回，超出时保存受治理的完整内容并返回
   片段、`truncated`、原始长度/摘要和 opaque artifact 引用。
3. 提供受 task/session 授权约束的有界分页读取入口；artifact 读不是普通文件路径读，
   权限不能通过猜 ID、跨任务引用或服务器返回的 file URI 绕过。
4. 存储有配额、保留期和可解释的失效状态；入库前执行适用的敏感数据治理，默认不另存
   未脱敏原始副本。存储失败必须显式说明不能取回，不能返回不可用的假引用。
5. 图片校验 MIME、编码和大小，再根据模型能力投影；需要扩展 ToolResult 的类型化内容
   和各 adapter 的工具结果序列化、checkpoint、context 计量。现有输入 ImageBlock
   不能直接证明工具输出图片已经支持。音频等不支持类型先返回附件引用及明确原因。
6. 远程 resource link 作为不可信引用呈现；不自动下载或解释为本机文件。

已收到可信成功响应、仅 artifact 存储或投影失败时，应保留操作成功的事实并单独报告
输出不可用；不能改成可重试的执行失败。SDK 校验失败等无法确认可信结果的情况仍沿用
P0 的结果未知语义。这两类错误需要分别测试，避免输出处理诱发写操作重放。

这部分优先实现文本/结构化大结果可取回，再支持图片；不重新加入全局无提示截断。
目录 schema 缓存和业务结果缓存分别考虑，后者不因“只读且参数相同”就自动启用。

## 4. 建议配置形态

以下为待新增字段的设计示例，当前解析器尚不支持这些新策略字段。数值用于说明，
不代表协议标准；默认预算上线前结合慢启动/关闭用例确定并记录依据。

```yaml
mcp:
  enabled: true
  max_parallel_startups: 2
  servers:
    docs:
      kind: executable
      command: [my-docs-mcp]
      required: false
      startup_timeout_seconds: 30
      tool_timeout_seconds: 30
      shutdown_timeout_seconds: 5
      tools:
        include: [search, read]
        exclude: []
      reconnect:
        enabled: true
        max_attempts: 3
        initial_delay_seconds: 1
        max_delay_seconds: 10
```

有效调用时间仍受 Kernel deadline 约束。旧配置保持 required=true、串行启动、重连关闭；
新增超时为旧配置补充有限预算时，应有升级说明和可配置覆盖，不能隐藏行为变化。

## 5. 交付顺序与验收

| 提交 | 内容 | 关键验收 |
| --- | --- | --- |
| 1 | 严格配置、状态 DTO、SDK 生命周期完成/故障信号 | 配置非法立即可见；ready/closed 可观察；原 owner 保持同 Task 清理 |
| 2 | 启动隔离、受限并行、启动/关闭/调用预算 | 一快一慢一坏服务器；可选失败不阻塞其他；必需失败清理全部；发现阶段受预算约束；关闭超时不误报 CLOSED |
| 3 | generation、有界重连和诊断查询 | 同一故障只重连一次；旧连接未关闭不重启；重连耗尽/认证失效可见；写操作不重放 |
| 4 | 工具过滤、版本快照与安全模型边界 | 通知风暴合并；旧代通知无效；半份目录不发布；schema 改变、旧审批、旧 checkpoint 都正确处理 |
| 5 | 名称 codec 与历史兼容 | 长名、冲突名、工具撤销后的历史、三个模型 wire 路径和恢复均可用 |
| 6 | artifact + renderer + 进度界面 | 大结果可分页取回；跨任务无权限；媒体不灌入 JSON；失败与未知语义不因投影改变；SSE 能看到实际进度 |

先交付 1–3，再做 4–6。只有在目录更新机制到位后，才让可选服务后台连接成功时向正在
运行的会话加入工具。P2 的 OAuth 登录、resources/prompts、按需连接和 schema cache
不混入这些提交。

测试复用 P0 真实 stdio 和本地 Streamable HTTP 基础；补可控时钟/随机源验证退避、实际
子进程计数验证无重叠、审批暂停后改变工具目录、断连后写入计数不增加，以及大结果
与 artifact 授权恢复。每个提交跑对应回归，最后做全量检查并记录环境限制及复跑结果。
