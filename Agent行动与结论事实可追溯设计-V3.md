# Agent 行动与结论事实可追溯设计 V3

> 状态：待实施评审
>
> 本文整合 V1 的可实施改造路径与 V2 的引用生命周期、工件保留和回放要求。本文以现有 `tsm-agt` 代码结构为准，不新增平行 Runtime。

## 1. 目标、边界与非目标

### 1.1 目标

让模型在软件工程任务中可以基于新增事实动态选择下一步行动，同时让最终结论能够引用真实、可定位、可回放的执行事实。

目标效果：

```text
模型修改代码
→ Runtime 执行并记录事实
→ 模型读取事实后动态选择测试、接口检查、浏览器检查或请求人工确认
→ 模型最终结论声明“已实施 / 已检查 / 已验证范围 / 未验证范围”
→ 系统机械校验结论引用是否合法
→ 用户同时看到执行状态、领域检查状态、模型结论和引用校验结果
```

### 1.2 Runtime 与 Projector 硬边界

Runtime 只负责：

```text
工具执行
审批和安全控制
资源生命周期
事件顺序与持久化
执行事实和工件记录
事实引用的结构校验
```

Runtime 不负责：

```text
recovery state
completion 判定
recovery planning
业务修复语义判断
自动认定用户需求已满足
根据模型文本选择下一步业务动作
```

Projector 只负责：

```text
读取 TaskSnapshot、RuntimeEvent、SessionEvent
投影执行状态、领域检查观察、模型结论和引用校验结果
```

Projector 不允许：

```text
semantic reasoning
retry intelligence
从自然语言识别“已验证”
写入事件、调用模型、调用工具或迁移状态
```

### 1.3 非目标

本文不定义：

- 浏览器、开发服务、数据库、接口等资源如何执行；
- 业务需求如何被完整理解；
- 自动修复算法或业务重试策略；
- 视觉差异规则；
- 桌面自动化；
- 用户是否满意的语义判断。

浏览器执行和工件采集由《[浏览器执行与验证证据采集设计](浏览器执行与验证证据采集设计.md)》定义。

## 2. V1 / V2 取舍

| 来源 | 处理 | 原因 |
|---|---|---|
| V1 三层状态模型 | 保留 | 执行、领域检查、模型结论不应互相推导 |
| V1 规范事实引用 | 保留并细化 | 可校验任务归属、事件一致性、执行状态和作用域 |
| V1 三通道输出策略 | 保留 | 兼容不同模型提供者与旧记录 |
| V1 逐文件实施与原子提交 | 保留 | 与当前 `Kernel`、SQLite、SDK/CLI 真实结构匹配 |
| V2 引用生命周期 | 保留 | 工件过期、丢失和回放时需要诚实展示 |
| V2 保留策略和孤立引用 | 保留 | 支持浏览器截图、测试报告等长期事实管理 |
| V2 `TaskResolution` | 删除 | 将模型 claim 自动映射为 Runtime completion，违反硬边界 |
| V2 `derive_task_resolution()` | 删除 | 任一 VERIFIED 即成功的算法不具备业务覆盖语义 |
| V2 Runtime 自动弱结论推断 | 删除 | 不允许从自然语言推断验证强度 |
| V2 `projection_downgrade` 写入 | 删除 | 降级是校验/规范化，Projector 只能读取结果 |
| V2 虚构 transaction / ledger 接口 | 删除 | 当前应复用 `commit_session_and_task()`，首版无需新物理表 |

## 3. 三层状态模型

系统不使用单一“成功/失败”表达所有含义。

| 状态层 | 表达内容 | 产生者 | 不表达的内容 |
|---|---|---|---|
| 执行状态 | Task/Turn 是否运行、等待、终止、异常退出；最终回答是否已持久化 | Runtime | 用户需求是否满足 |
| 领域检查状态 | 测试、接口、浏览器、数据库等检查观察结果 | 领域检查组件，Runtime 记录 | 整体业务成功 |
| 模型结论状态 | 模型主张已实施、已检查、已验证、范围有限或需人工确认 | 模型 | Runtime 对业务结论的背书 |

`ConclusionReferenceValidation` 是模型结论的附属校验属性，不是第四种业务状态。它仅说明该结论引用的事实是否存在、属于当前任务、类型匹配且可使用。

因此：

```text
TaskState.SUCCEEDED
= 当前 Task 执行流程已终止，最终回答已持久化。

领域检查 PASSED
= 某条已声明检查获得通过观察。

模型结论 VERIFIED
= 模型对明确范围作出的主张，且携带事实引用。
```

三者必须并列展示，禁止互相隐式推导。

## 4. 统一结论数据契约

### 4.1 新增模块

新增：

```text
src/tsm_agt/ports/conclusion.py
```

并在 `src/tsm_agt/ports/__init__.py` 导出。

```python
class ConclusionKind(StrEnum):
    IMPLEMENTED = "implemented"
    CHECKED = "checked"
    VERIFIED = "verified"
    VERIFICATION_FAILED = "verification_failed"
    VERIFICATION_LIMITED = "verification_limited"
    HUMAN_CONFIRMATION_REQUIRED = "human_confirmation_required"


@dataclass(frozen=True, slots=True)
class FactReference:
    task_id: str
    event_id: str
    sequence: int
    source_type: str
    source_id: str
    tool_call_id: str | None = None
    execution_id: str | None = None
    expected_authority: str | None = None
    expected_effect: str | None = None
    content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class ConclusionClaim:
    claim_id: str
    kind: ConclusionKind
    summary: str
    scope: tuple[str, ...] = ()
    fact_refs: tuple[FactReference, ...] = ()
    unverified_scope: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class AssistantConclusion:
    schema_version: int
    claims: tuple[ConclusionClaim, ...]
    overall_scope: tuple[str, ...] = ()
    origin: str = "native_structured"
```

### 4.2 规范引用身份

新写入的引用必须包含：

```text
task_id + event_id + sequence + source_type + source_id
```

其中：

| `source_type` | `source_id` | 权威来源 |
|---|---|---|
| `tool_result` | `execution_id` | `TaskSnapshot.tool_executions` 与 `tool.completed/tool.failed` |
| `runtime_event` | `event_id` | 当前 Task 的 RuntimeEvent |
| `artifact` | `artifact_id` | 工件事件与工件元数据 |
| `verification` | `criterion_id` 或检查结果编号 | `verify.*` 观察事件 |
| `mutation` | `mutation_id` | Mutation Journal 与变更事件 |

`call_id`、`event:<sequence>`、截图文件路径都不能单独作为新协议的唯一引用。旧记录可兼容读取旧格式，但不得被反向推断为 `VERIFIED`。

### 4.3 强结论结构约束

| 结论类型 | 最低结构约束 |
|---|---|
| IMPLEMENTED | 至少一个 mutation 或成功变更工具事实 |
| CHECKED | 至少一个检查工具或领域检查事实，可引用成功或失败结果 |
| VERIFIED | 至少一个已提交、成功且类型匹配的事实引用，并声明 `scope` |
| VERIFICATION_FAILED | 至少一个失败检查或失败观察事实 |
| VERIFICATION_LIMITED | 必须提供 `unverified_scope` 和 `reason` |
| HUMAN_CONFIRMATION_REQUIRED | 必须提供不能自动判断的原因 |

这些约束只校验数据完整性，不判断模型摘要是否真实反映用户需求。

## 5. 模型输出与适配器策略

### 5.1 统一内部消息

任何外部模型响应进入系统后统一为：

```text
Message(
  TextBlock(用户可见正文),
  ConclusionBlock(AssistantConclusion)  # 可选
)
```

新增 `ConclusionBlock` 到：

```text
src/tsm_agt/ports/model.py
```

并扩展 `MessageBlock`、`Message.to_data()`、`Message.from_data()`。

### 5.2 三通道输出

#### 通道一：原生结构化输出（首选）

模型提供者支持严格 JSON、受控工具调用、内容分段或等价能力时，请求模型同时返回：

```text
用户可见正文
+ 符合 AssistantConclusion schema 的结论对象
```

模型适配器直接构造 `TextBlock + ConclusionBlock`。这是默认首选。

#### 通道二：受控结构块（兼容）

不支持原生结构化输出但能稳定输出文本时，提示模型在正文末尾返回唯一、完整的受控结构块：

```text
<tsm-conclusion-v1>
{严格 JSON 对象}
</tsm-conclusion-v1>
```

适配器必须：

1. 只解析唯一、完整、位置合法的结构块；
2. 严格校验 JSON 与 `AssistantConclusion` schema；
3. 从 `TextBlock` 移除结构块；
4. 转换为 `ConclusionBlock`；
5. 标记 `origin=controlled_text_block`。

不得扫描“已验证”“修复完成”等自然语言关键词；不得容错猜测缺失字段。

#### 通道三：纯文本降级（保底）

原生字段不可用、受控结构块缺失或解析失败时：

```text
保留 TextBlock；
不创建 ConclusionBlock；
记录 origin=legacy_unstructured；
不展示“已验证”或“引用校验通过”徽标。
```

旧模型、旧适配器和旧历史记录均走该通道。

### 5.3 模型适配器职责

“原生结构化输出”和“适配器解析专用字段”不是二选一：前者是模型能力，后者是 `tsm-agt` 的转换位置。

每个适配器按以下优先级工作：

```text
原生结构化字段
→ 受控结构块
→ 纯文本降级
→ 统一内部 Message
```

实际改造目标至少包括：

```text
src/tsm_agt/adapters/openai_compatible/model.py
测试用模型提供者与夹具
模型消息序列化与反序列化
流式模型响应处理
```

流式响应只能即时转发用户可见正文；受控结构块必须缓冲到响应完成后解析，绝不能展示给用户。

## 6. 事实引用校验

新增纯读取组件：

```text
src/tsm_agt/core/conclusion_reference_validator.py
```

输入：

```text
当前 TaskSnapshot
按序 RuntimeEvent
AssistantConclusion
```

输出：

```text
ConclusionReferenceValidation
- valid / invalid / incomplete
- 每条 claim 的校验结果
- 无效引用原因
```

校验范围：

1. `FactReference.task_id` 必须等于当前任务。
2. `event_id` 必须存在，且其 sequence 与引用一致。
3. `source_type` 必须与事件类型、执行账本或工件记录一致。
4. `tool_result` 必须能定位到对应 `execution_id`。
5. 强结论引用的执行必须为 `COMMITTED`。
6. `VERIFIED` 不得仅引用 failed、cancelled 或 unknown outcome。
7. `expected_authority`、`expected_effect` 与 `ToolSpec`、`ToolFactDescriptor` 一致。
8. 已记录的作用域不匹配事实不能支持强结论。
9. 生命周期为 ORPHANED、EXPIRED 或 PURGED 的引用不能支持新的强结论，但历史结论仍保留。

校验器不得：

```text
解释自然语言摘要
判断功能是否修复
修改模型正文
自动调用工具
自动重试
自动迁移 Task 状态
```

校验结果只以事实事件记录：

```text
conclusion.references_validated
conclusion.references_invalid
conclusion.references_incomplete
```

## 7. 工具事实、工件与生命周期

### 7.1 工具事实描述

在 `src/tsm_agt/ports/tool.py` 新增可选 `ToolFactDescriptor`，并加入 `ToolResult`：

```text
authority
scope
artifact_refs
observed_at
content_hash
```

它描述工具结果能证明的事实范围，不允许工具适配器返回“功能已修复”或“任务已完成”。

`tool.completed`、`tool.failed` 保留完整 `ToolResult`，同时保存标准化事实描述快照。

### 7.2 生命周期

引用与工件可处于：

```text
ACTIVE
REPLAYABLE
ORPHANED
EXPIRED
PURGED
```

规则：

- 工件或事件存在且可读取时为 ACTIVE；
- 可在回放或导出中重新解析时为 REPLAYABLE；
- 原始目标消失或无法再定位时为 ORPHANED；
- 保留期结束时为 EXPIRED；
- 主动清理时为 PURGED。

默认应可回放的工件元数据：

```text
领域检查结果
命令结果摘要
截图元数据
浏览器控制台快照摘要
结构化测试报告摘要
```

历史结论遇到孤立引用时不得删除或改写，只显示“原始证据不可回放”。

## 8. 最终回答原子提交

### 8.1 当前问题

当前 `Kernel._commit_model_response_checkpoint()` 写入：

```text
llm.completed
turn.completed
```

之后 `Kernel._record_session_task_result()` 单独写入：

```text
session.task_result_recorded
```

两次提交间存在退出窗口：Task 有模型结果，Session 却没有用户可见最终回答。

### 8.2 V3 实施方式

新增 `Kernel._commit_final_agent_result()`，在一个 `SessionTaskUnitOfWork` 中构造：

```text
Task 事件：
- llm.completed
- conclusion.references_validated / invalid / incomplete
- turn.completed

Session 事件：
- session.task_result_recorded
  - assistant_message
  - assistant_conclusion
  - conclusion_validation
  - answer_event_ref
```

使用现有：

```text
RuntimeStorePort.commit_session_and_task()
SQLiteRuntimeStore.commit_session_and_task()
```

稳定 `command_id` 使用：

```text
task_id + turn_id + canonical(assistant_message + conclusion + validation)
```

Projector 是纯读取计算，不参与事务写入。

首版不新增物理 `ConclusionLedger` 表；结论、校验和生命周期元数据保存于现有 Message、RuntimeEvent、SessionEvent JSON。只有跨任务高频索引和审计需求出现后，才单独设计索引表。

## 9. 现有代码改造清单

### 9.1 `src/tsm_agt/ports/conclusion.py`（新增）

实现结论、引用、校验结果和生命周期状态的不可变数据结构及序列化。

### 9.2 `src/tsm_agt/ports/model.py`

- 新增 `ConclusionBlock`；
- 扩展消息块联合类型及序列化；
- 旧消息缺结论块时保持兼容；
- 未识别块类型显式失败或受控降级，不静默丢弃。

### 9.3 `src/tsm_agt/ports/tool.py`

- 新增 `ToolFactDescriptor`；
- 扩展 `ToolResult.to_data()/from_data()`；
- 保持风险、幂等、授权、恢复种类和 `meta` 的既有语义。

### 9.4 模型提供者适配器

至少修改：

```text
src/tsm_agt/adapters/openai_compatible/model.py
模型夹具和测试提供者
流式响应适配层
```

实现三通道转换和能力协商。不能只改 `ports/model.py`。

### 9.5 `src/tsm_agt/core/conclusion_reference_validator.py`（新增）

实现第 6 章的纯读取校验，不调用模型、工具、状态迁移或 Projector。

### 9.6 `src/tsm_agt/core/kernel.py`

第一阶段：

1. `_validate_agent_response()` 接受 `ConclusionBlock` 的结构；
2. `_execute_authorized_tool()` 将 `ToolFactDescriptor` 写入工具完成/失败事件；
3. 最终无工具响应路径调用事实引用校验器；
4. 记录引用校验事件，不改写模型正文、不自动调用工具。

第二阶段：

1. 实现 `_commit_final_agent_result()`；
2. 替换最终 Task/Session 双写路径；
3. 让 checkpoint、审批恢复和中断恢复路径都使用相同最终提交入口。

第三阶段：

当前 `_completion_readiness_gaps()`、`CompletionReadinessPolicy` 和 `_continue_agent_turn()` 中存在的业务缺口续做、自动预算续期、恢复提示，应先保留在兼容模式，再逐步收缩为“事实完整性诊断”。

目标模式下：

```text
Kernel 可以生成结构化诊断事实；
Kernel 不把诊断转换为业务下一步；
Kernel 不给模型注入“必须继续/必须恢复/任务未完成”的业务指令；
模型读取诊断后自主选择继续、说明范围有限或请求用户确认。
```

审批、超时、未知结果、并发冲突、幂等和资源清理保留为 Runtime 硬安全边界。

### 9.7 `src/tsm_agt/core/session_context.py`

Session task summary 和后续提示上下文增加：

```text
assistant_conclusion 摘要
conclusion_validation
answer_event_ref
```

仅注入最近相关任务的摘要和引用标识；不注入原始敏感 ToolResult body；不把历史模型结论视为新任务指令、审批或业务成功事实。

### 9.8 `src/tsm_agt/core/task_runtime_projection.py`

保持纯读取，只增加：

```text
latest_answer_event_ref
conclusion_claims
conclusion_validation
```

现有 `verification_status` 在接口/UI 中命名为“领域检查状态”，不能与模型 `VERIFIED` claim 合并。

### 9.9 `src/tsm_agt/sdk/runtime.py` 与 `src/tsm_agt/cli.py`

对外结果新增可选字段：

```text
assistant_conclusion
conclusion_validation
answer_event_ref
```

当前 SDK/CLI 将最终回答后直接迁至 `VERIFYING → FINALIZING → SUCCEEDED/FAILED`。迁移期间保留兼容模式；目标状态下 SDK/CLI 应分开返回：

```text
最终回答已提交
领域检查观察
模型结论及引用校验
```

不得把领域检查或模型 claim 自动解释为业务成功。

### 9.10 `src/tsm_agt/web/app.py`

页面展示四个并列区域：

```text
执行状态
领域检查状态
模型结论
事实引用校验
```

页面不从回答文本或工具成功事件推断“功能修复完成”。

## 10. 迁移计划

### 阶段零：基线与开关

- 记录现有 Task/Session/Provider 兼容基线；
- 增加 `conclusion_protocol_mode`：`disabled`、`observe`、`require_structured`；
- 不改变 TaskState 或完成门控。

### 阶段一：协议只记录

- 新增 conclusion 数据结构和消息块；
- 适配器三通道输出；
- 写入引用校验事件；
- 旧文本统一为 `legacy_unstructured`；
- 页面只读展示新字段。

### 阶段二：原子最终提交

- 实现 `_commit_final_agent_result()`；
- 使用 `commit_session_and_task()`；
- 验证幂等、崩溃恢复和版本冲突。

### 阶段三：领域检查与执行状态分离

- SDK/CLI 返回独立的执行、检查、结论字段；
- UI 按三层状态展示；
- 保持既有完成门控兼容模式。

### 阶段四：收缩 Runtime 业务门控

- 将 completion readiness 输出收缩为结构化诊断事实；
- 移除业务续做和恢复指令；
- 模型根据诊断自主决定下一步或请求用户；
- 保留 Runtime 安全暂停。

## 11. 测试矩阵

### 11.1 协议与适配器

- 原生结构化字段转换为 `ConclusionBlock`；
- 受控结构块唯一、完整、合法时转换成功；
- 重复、不完整或非法结构块降级为纯文本；
- 纯文本不被扫描推断为 VERIFIED；
- 流式输出不泄漏受控结构块到用户界面；
- 旧 Message、旧 ToolResult 和旧 Session 事件可反序列化。

### 11.2 事实引用

- 引用不存在、跨任务、事件序号不匹配；
- `tool_result` 与 execution 不匹配；
- VERIFIED 引用 failed/cancelled/unknown outcome；
- authority/effect 不匹配；
- 作用域不匹配；
- ORPHANED/EXPIRED/PURGED 历史引用展示正确；
- 校验失败不自动重试、不自动改写文本、不自动迁移状态。

### 11.3 行动循环与原子提交

扩展：

```text
tests/test_agent_loop.py
tests/test_model_messages.py
tests/test_openai_compatible_model.py
tests/test_sdk_runtime.py
tests/test_sqlite_runtime_store.py
tests/test_session_context.py
tests/test_task_runtime_projection.py
```

重点断言：

- 同一 Turn 的 ToolResult 能被下一轮模型引用；
- 最终 Task 事件、结论校验事件和 Session 结果原子出现或原子缺失；
- 相同 `command_id` 重放不产生重复用户可见回答；
- Projector 纯读取，不产生事件或副作用；
- 旧完成门控兼容模式与新结论协议可并存；
- 在最终模式下 Runtime 不因业务缺口自动选择继续或恢复。

## 12. 验收条件

1. 模型可在运行中基于新增事实动态选择验证动作。
2. 所有新强结论都可引用当前任务中真实、规范化的事实。
3. Runtime 只能校验引用结构和事实属性，不能判断业务是否修复。
4. Task、Session 与最终用户回答在原子提交后不出现双写不一致。
5. 三层状态在 SDK、CLI、投影和页面中独立呈现。
6. 纯文本、旧模型和旧历史记录不会被伪造成“已验证”。
7. 回放保留历史结论；证据失效时降低可回放性，不改写历史。
8. 审批、超时、未知结果、幂等、并发冲突和资源清理安全边界保持有效。
