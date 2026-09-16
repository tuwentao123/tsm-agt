# 生产级工程 Agent 0→1 实现 SPEC

> 文档状态：Draft v2（Greenfield 全新实现版）
>
> 需求参考：现有 `tsm-agt` 的功能、场景和生产经验
>
> 新系统底座：LangChain + Middleware + LangGraph
>
> 实施原则：创建独立新项目；每个阶段实现后先验证，验证通过再进入下一阶段

---

## 1. 准确目标

本项目不是重构、迁移或继续开发现有 `tsm-agt`，而是从空目录创建一个完全独立的新 Agent：

```text
新仓库 / 新 Python 包 / 新数据库 / 新 API
                    │
                    ├─ 在集成层使用 LangChain 的模型、工具、消息和 Middleware
                    ├─ 在编排适配层使用 LangGraph 的状态图、中断和恢复
                    ├─ 参考 tsm-agt 已有功能建立产品需求清单
                    └─ 补齐多实例、安全、观测、评测和生产部署能力
```

现有 `tsm-agt` 只承担三个角色：

1. 功能样本：告诉新系统工程 Agent 应该具备哪些能力；
2. 场景样本：告诉新系统审批、恢复、修改和验证应该怎样表现；
3. 测试样本：把已经遇到的边界条件转成新系统回归用例。

明确不做：

- 不复用旧 Kernel、AgentLoop、Port 或 Adapter；
- 不兼容旧 SQLite Schema、Checkpoint、Event 和 CLI；
- 不做 Legacy/LangGraph 双引擎迁移；
- 不复制旧项目的大量自研运行时；
- 不要求新系统能恢复旧任务。

一句话目标：

> 利用 LangChain 和 LangGraph 已有框架能力，快速从 0 到 1 建立一个覆盖 `tsm-agt` 主要功能、并具备完整生产能力的新工程 Agent。

---

## 2. 为什么从 0 到 1 使用现成框架

LangChain/LangGraph 已经提供：

- 模型和消息抽象；
- Tool Call / Tool Message 协议；
- 模型—工具 Agent 循环；
- Middleware Hook；
- StateGraph、节点、条件边和子图；
- `interrupt()` 暂停；
- `Command(resume=...)` 恢复；
- Checkpointer 接口；
- 节点 Retry Policy；
- Streaming；
- 长短期记忆接入点；
- 多模型 Provider 生态。

这样新项目不再花大量时间重新发明通用 Agent Runtime，而是重点实现：

- 工程任务怎样规划和收口；
- 哪些行为需要审批；
- 文件和命令如何安全执行；
- 怎样避免重复副作用；
- 什么证据才算完成；
- 修改代码后怎样验证；
- 如何持久化、观测、评测和上线。

但以下代码只得到基础 Agent，不等于生产系统：

```python
agent = create_agent(model=model, tools=tools)
```

框架不会自动提供业务权限、Sandbox、幂等账本、Unknown Outcome、工程验收、多租户和生产运维，这些仍需新系统实现。

---

## 3. 产品定位与能力目标

新项目暂称 **TSM Agent Next**，最终名称在建仓时确定。它需要：

- 理解代码分析、修改、排障和执行目标；
- 识别项目语言、源码结构、构建系统、测试入口和项目规则；
- 生成结构化 Task Spec 和执行计划；
- 搜索、读取、理解和修改代码；
- 使用代码智能定位定义、引用、实现和诊断；
- 在受控 Sandbox 中运行命令和后台进程；
- 高风险操作先人工审批；
- 缺少关键输入时暂停并等待用户；
- 进程或容器重启后从数据库恢复；
- 区分模型重试、工具重试和节点重试；
- 非幂等操作结果未知时停止自动重放；
- 用证据和 Verification 决定是否真正完成；
- 支持 Task、Session 和 Project 三层记忆；
- 提供执行 Flow、Replay、Trace、Metrics 和日志；
- 每次修改 Prompt、模型或 Graph 后运行回归评测；
- 在多用户、多租户、多实例环境运行。

---

## 4. 功能范围

### 4.1 参考 `tsm-agt` 必须覆盖

| 能力域 | 新系统要求 |
|---|---|
| Session/Task/Turn | 持久化会话、独立任务、任务内多次模型/工具调用 |
| 工程闭环 | 理解、调查、计划、执行、验证、收尾 |
| 项目发现 | 语言、构建工具、源码根、测试、项目规则 |
| 文件工具 | 列表、查找、搜索、读取、Patch、写入、删除 |
| 代码智能 | 定义、引用、实现、符号、诊断、重命名预览 |
| 进程工具 | 前台命令、后台进程、日志、终止和超时 |
| 审批 | 风险分级、参数预览、批准、编辑、拒绝 |
| 工作区安全 | 路径边界、敏感文件、符号链接逃逸、并发保护 |
| 修改事务 | 前后 Hash、Patch、备份、回滚、审计 |
| 幂等恢复 | Tool Ledger、Checkpoint、重启恢复、未知结果保护 |
| 澄清 | 暂停等待用户，回答后继续同一任务 |
| Steering | 补充、替换目标、排队任务、状态查询 |
| 上下文 | Prompt 装配、Token 预算、压缩、历史摘要 |
| 记忆 | Working、Session、Project Memory |
| 证据 | 调查问题、证据来源、完成项和剩余工作 |
| 验证 | 测试/构建失败时不能冒充成功 |
| Flow/Replay | 模型、工具、审批、恢复、修改、验证全链路 |
| 接口 | CLI、SDK、HTTP API 和实时事件 |

### 4.2 新增生产能力

- PostgreSQL Checkpointer；
- 无状态 API + 多副本 Worker；
- Task Lease 和 fencing token；
- Outbox 可靠事件；
- 对象存储保存大型 Artifact；
- OpenTelemetry；
- 多租户 RBAC；
- Secret Manager；
- 出站网络与数据外传策略；
- Prompt Injection 防护；
- 模型 Fallback、熔断和成本配额；
- Golden Dataset、Pairwise Judge 和 CI 质量门禁；
- Canary 与自动回滚；
- Graph、State、Prompt、Tool 和 Policy 版本管理；
- MCP、Skill、Subagent 的受控扩展能力。

---

## 5. 框架与自研边界

### 5.1 在 Integration/Orchestration 层使用 LangChain

- `BaseChatModel` 和 Provider 集成；
- `BaseMessage`；
- `BaseTool` / `@tool`；
- Tool Call 与 Tool Message；
- `create_agent()` 基础 Agent；
- Structured Output；
- Model Retry、Tool Retry、Model Fallback 等预置 Middleware；
- Streaming 和 Callback；
- Summarization、PII、Call Limit 等可复用能力。

### 5.2 在 Orchestration Adapter 中使用 LangGraph

- `StateGraph`；
- 节点、边、条件路由和子图；
- `interrupt()`；
- `Command(resume=...)`；
- Checkpointer；
- Thread 和 Checkpoint 身份；
- Retry Policy；
- Graph State Reducer；
- Streaming、状态查询和重启恢复。

### 5.3 必须自行实现

- 工具风险和审批规则；
- Tenant/User/Workspace 权限；
- 文件系统和命令 Sandbox；
- 网络与数据外传控制；
- Tool Execution Ledger 和业务幂等；
- Unknown Outcome 对账；
- 文件 Mutation、备份和回滚；
- Project Instructions 可信边界；
- Task Spec、Evidence、Completion Readiness；
- Verification；
- 生产数据库、API、Worker、Lease 和 Outbox；
- 长期记忆治理；
- 评测、可观测性和生产运维。

原则：框架已经稳定提供的能力优先直接使用；只有通过 Spike/测试证明不满足要求时才自研替代。

LangChain/LangGraph 是重要底座，但不是整个系统的领域模型。禁止为了“直接使用框架”让框架类型穿透 Domain、Application、Persistence Contract 和公共 API。

### 5.4 架构宪法

以下规则优先级高于单个功能设计。任何实现和 Pull Request 都必须遵守。

#### 规则 A：稳定内核不认识具体场景

内核只认识通用概念：

```text
Task / Run / State / Command / Event
Capability / Tool / PolicyDecision
Approval / Execution / Artifact / Evidence
Memory / Verification / Failure / Recovery
```

内核不能认识：

```text
“Java 项目”
“修 Bug”
“发布 NPM”
“创建 GitHub PR”
“查询某公司内部平台”
“某个模型供应商的特殊字段”
```

这些属于插件、配置、Workflow 或 Adapter。

#### 规则 B：依赖只能指向稳定抽象

```text
domain ← application ← orchestration ← adapters/interfaces/bootstrap
             ↑              ↑
          contracts      LangGraph/LangChain
```

- `domain` 不导入 LangChain、LangGraph、FastAPI、SQLAlchemy、Kubernetes 或 Provider SDK；
- `application` 只依赖领域对象和 Port/Protocol；
- `orchestration` 可以依赖 LangGraph/LangChain，通过 Mapper 调用 Application Service；
- `adapters` 实现 Port，但 Adapter 之间不能互相直接导入；
- `interfaces` 只调用 Application Service，不直接写数据库和执行 Tool；
- `bootstrap` 是唯一选择具体实现、解析插件和组装依赖的位置。

#### 规则 C：通用协议优先于类型判断

禁止：

```python
if task.type == "bug_fix": ...
if project.language == "java": ...
if tool.name == "github.create_pr": require_approval()
if provider == "openai": state["messages"] = ...
```

应改为：

```text
TaskSpec 声明 Outcome 和所需 Capability
Project Analyzer 插件声明探测结果与候选 Capability
Tool Manifest 声明 risk/effect/network/idempotency
Policy Engine 根据 Metadata 做决定
Provider Adapter 完成协议转换
Workflow Registry 根据 Capability/Constraint 选择实现
```

#### 规则 D：机制和策略分离

- LangGraph 提供“暂停和恢复”的机制；Policy 决定“何时暂停”；
- Tool Ledger 提供“记录执行”的机制；Tool Manifest 决定“能否重试”；
- Sandbox 提供“隔离执行”的机制；Sandbox Profile 决定权限；
- Memory Store 提供“保存和检索”的机制；Memory Policy 决定写什么、保留多久；
- Verification Engine 提供执行框架；Verifier 插件决定某个 Outcome 如何验证；
- Router 提供选择机制；声明式 Rule/Capability 决定选哪个插件。

#### 规则 E：插件故障不能破坏内核

- 插件通过版本化契约通信；
- 插件输入输出必须 Schema 校验；
- 插件没有数据库、全局 Runtime 或其他插件实例的隐式访问权；
- 插件获得最小 Capability Context；
- 插件超时、异常和不兼容有统一错误；
- 第三方插件优先隔离进程/Sandbox/MCP，不直接加载到控制面进程；
- 插件卸载后，历史 Event 和 Task 仍能解释；等待任务按兼容规则处理。

#### 规则 F：配置是数据，不是散落的条件分支

以下内容必须通过版本化 Manifest/Policy/Workflow 定义：

- Tool 风险和权限；
- Model Capability 和 Fallback；
- Sandbox Profile；
- Prompt 组合；
- Memory Policy；
- Verification Rule；
- Workflow 节点选择；
- Tenant Feature；
- Retry/Timeout/Budget。

配置必须有 Schema、版本、Hash、签名、测试、发布和回滚，不能用数据库临时字段或环境变量堆出隐式业务逻辑。

#### 规则 G：可替换不等于任意动态加载

插拔能力必须受控：

- 平台内置插件：随版本发布，进程内运行；
- 受信任组织插件：签名、审核后部署到隔离 Worker；
- 第三方插件：优先 MCP/远程 Tool/Sandbox；
- 未知 Python 包不能在控制面通过 import 动态执行；
- 插件声明的权限只能缩小，不能绕过平台 Policy。

### 5.5 核心 Port/Protocol

新系统至少定义以下稳定扩展点：

```text
IdentityProviderPort
AuthorizationPolicyPort
TaskRepositoryPort / EventStorePort
CheckpointPort / LeasePort / CommandBusPort
ModelProviderPort / ModelRouterPort
PromptProviderPort / ContextProviderPort
ToolProviderPort / ToolExecutorPort / ToolPolicyPort
ApprovalProviderPort
WorkspaceProviderPort / RepositoryProviderPort
SandboxProviderPort / NetworkPolicyPort / SecretProviderPort
ArtifactStorePort
ProjectAnalyzerPort / CodeIntelligencePort
PlannerPort / CompletionPolicyPort
VerifierPort / EvidenceProviderPort
WorkingMemoryPort / LongTermMemoryPort / RetrieverPort
WorkflowProviderPort / NodeProviderPort / RouterPort
EventSinkPort / TracerPort / MetricSinkPort
EvaluatorPort / JudgePort / QualityGatePort
```

Port 应使用项目自己的 Pydantic/dataclass DTO，不把框架、ORM 或厂商对象暴露给内核。

### 5.6 插件 Manifest

```yaml
apiVersion: agent.tsm.dev/v1
kind: Plugin
metadata:
  name: github-tools
  version: 1.2.0
spec:
  contractVersion: "1.x"
  provides:
    - capability: scm.pull_request.create
      implementation: github.create_pull_request
  requires:
    - capability: network.https
    - secret: github_installation_token
  permissions:
    network:
      hosts: [api.github.com]
    workspace: read
    dataTransmission: repository_metadata
  isolation: remote
  schemas:
    input: schemas/create_pr.input.json
    output: schemas/create_pr.output.json
```

Manifest 只声明请求，最终权限由平台 Policy 与用户审批决定。

### 5.7 Capability Registry

核心按 Capability 找实现，不按类名或产品名写分支：

```text
capability: workspace.file.read
capability: workspace.file.patch
capability: process.command.run
capability: code.symbol.definition
capability: scm.pull_request.create
capability: memory.project.retrieve
capability: verification.test.python
```

选择输入包括 tenant policy、TaskSpec、环境、权限、数据分类、成本、健康状态和版本兼容。选择结果进入 Effective Manifest，保证恢复时不会静默换实现。

### 5.8 无特殊场景编码的判定标准

新增一个业务场景时，如果必须修改以下位置，说明抽象可能失败：

- Domain Entity；
- 顶层 Task Graph 固定节点；
- Tool Gateway 核心；
- Approval 核心；
- Checkpoint/State 基础 Schema；
- API 通用 Task Command；
- 其他无关插件。

理想情况下，新增场景只需要：

```text
新增/配置 Capability Provider
+ 可选 Workflow/Subgraph
+ Prompt/Policy/Verifier Manifest
+ 契约测试和 Golden Task
+ Bootstrap/Registry 注册
```

如果新需求改变了所有场景共同拥有的领域语义，才允许修改内核，并需要 ADR 和架构评审。

### 5.9 插件类型与生命周期

插件类型统一，不为每种业务另造加载机制：

| 插件类型 | 提供内容 | 示例仅用于理解 |
|---|---|---|
| Model Provider | 通用模型调用能力 | 某云模型、本地模型 |
| Tool Provider | 一个或多个 Tool Capability | 文件、SCM、数据库 |
| Workflow Provider | Workflow/Subgraph | 工程任务、文档任务 |
| Node/Router Provider | 可复用节点或路由策略 | 规划、完成度判断 |
| Project Analyzer | 项目/资源静态分析 | 语言、构建系统识别 |
| Verifier | Outcome 验证 | 测试、Schema、策略验证 |
| Context Provider | 上下文片段 | 项目规则、检索资料 |
| Memory Provider | 记忆存储/检索 | PostgreSQL、向量索引 |
| Sandbox Provider | 隔离执行环境 | 本地、容器、远程 Sandbox |
| Repository Provider | 仓库和 Workspace | GitHub、GitLab、本地目录 |
| Policy Provider | 声明式决策 | 工具风险、网络、审批 |
| Event/Telemetry Provider | 事件和观测输出 | OTel、审计存储 |
| Evaluator | 离线评测和门禁 | 确定性评分、Judge |

生命周期：

```text
DISCOVERED
→ MANIFEST_VALIDATED
→ SIGNATURE_VERIFIED
→ DEPENDENCIES_RESOLVED
→ PERMISSIONS_EVALUATED
→ INITIALIZED
→ HEALTHY
→ DRAINING
→ STOPPED
  └─ FAILED / QUARANTINED / INCOMPATIBLE
```

- Discovery 只读取 Manifest，不执行插件代码；
- Dependency Resolution 基于 Capability 和契约版本，不基于 Python import 猜测；
- 初始化获得最小 `PluginContext`，不获得全局容器；
- 健康检查是显式协议；
- 升级前先 Drain 使用旧版本的 Run；
- 插件版本和配置进入 Effective Manifest；
- 插件异常统一映射为 `PluginFailure`，不能泄漏厂商异常到领域层；
- Quarantine 插件不能被新 Task 选择。

### 5.10 插件契约版本兼容

每个 Port 有独立契约版本，而不是跟随整个应用版本：

```text
ToolProviderContract 1.x
VerifierContract 2.x
SandboxProviderContract 1.x
WorkflowContract 1.x
```

- Patch/Minor 兼容规则写入 Contract；
- Major 不兼容必须提供并行版本或迁移器；
- Contract Test Kit 由平台提供，插件必须通过；
- Consumer-driven Contract 验证平台真实使用的字段；
- 未知字段按协议处理，禁止随意 `dict.get()` 掩盖不兼容；
- 历史 Event 保存标准化结果和插件身份，不能依赖旧插件才能显示。

### 5.11 Composition Root

只有 `bootstrap/composition` 可以：

- 读取部署配置；
- 发现并校验 Manifest；
- 构建 Capability Registry；
- 选择具体 Adapter；
- 组装 Middleware 顺序；
- 编译 LangGraph；
- 管理插件启动/停止。

业务代码禁止调用全局 Service Locator。依赖使用构造函数/显式参数注入；Task Run 使用冻结后的 `RuntimeComposition`，不能运行中偷偷读取当前全局默认实现。

### 5.12 架构自动化门禁

CI 必须运行架构测试：

- Domain 禁止导入 LangChain/LangGraph/FastAPI/ORM/Provider SDK；
- Application 禁止导入 Adapters/Interfaces；
- Adapter 之间禁止直接导入；
- 只有 Bootstrap 可以实例化具体 Adapter；
- 禁止跨模块直接访问 Repository 表模型；
- 禁止 Core 中出现已登记的语言名、平台名、Tool 名条件分支；
- 禁止 Tool 绕过 `ToolExecutionGateway`；
- 禁止 Workflow 直接持有 Secret/DB Connection；
- 公共 DTO 必须在 `contracts` 中版本化；
- 插件必须通过 Contract Test Kit；
- 每个新 Capability 必须有 Manifest、Owner、Schema、权限、超时和契约测试。

可使用 import-linter、自定义 AST Test、dependency-cruiser 类规则或等价工具实现。架构测试失败和单元测试失败同等阻断合并。

### 5.13 哪些应留在内核，哪些应做插件

不是所有类都要变成插件。判断标准是“它是所有场景共同的不变量，还是会随供应商/环境/业务能力变化”。

#### 留在微内核

- Task/Run/Command/Event 的身份与状态不变量；
- Approval 绑定参数和主体的基本语义；
- Tool Execution Ledger 的 Claim/Commit/Unknown Outcome；
- Artifact/Evidence 的来源和完整性；
- Verification 必须发生在成功之前；
- Tenant/Subject/Workspace 的安全上下文；
- Failure/Recovery 的标准分类；
- Capability 和 Manifest 的解析规则；
- Plugin Lifecycle 和 Contract Compatibility。

#### 做成策略或 Manifest

- 哪个风险等级需要几人审批；
- 某租户允许哪些模型和网络目标；
- 某 Tool 的超时、重试、数据传输类别；
- 记忆 TTL、召回数量和写入审核；
- Workflow 由哪些通用阶段组合；
- 哪些 Verifier 满足某个 Outcome。

#### 做成插件/Adapter

- 模型供应商；
- 仓库供应商；
- Sandbox 实现；
- 文件、进程、SCM、知识库等 Tool Provider；
- Project Analyzer、Code Intelligence、Verifier；
- Memory/Vector/Artifact/Persistence 后端；
- 第三方系统和企业内部平台。

如果一个抽象永远只有一个实现、没有独立变化轴，也不需要运行时替换，就先保留为内核内部模块；不要为了形式上的“插拔”制造无意义接口。

### 5.14 禁止的架构反模式

- **上帝 Registry**：所有代码随时按字符串取任意服务；
- **万能 Context**：把数据库、Secret、Runtime、用户和所有 Adapter 塞给插件；
- **字典驱动领域**：所有输入输出都是无约束 `dict[str, Any]`；
- **插件互调**：插件 A 直接 import/调用插件 B；
- **按名称做策略**：通过 Tool 名、语言名、模型名硬编码审批和路由；
- **巨型 Workflow**：一个 Graph 包含全部场景的条件分支；
- **框架泄漏**：Domain/Application 返回 LangChain Message、LangGraph Command、ORM Model；
- **配置即代码执行**：Manifest 允许任意 Python/模板表达式；
- **隐式降级**：插件失败后偷偷换实现、降低权限或跳过验证；
- **接口爆炸**：每个类一个 Port，导致抽象比实现更复杂；
- **循环依赖**：Plugin、Adapter、Application 互相引用；
- **双事实源**：插件私库和平台任务状态都声称自己是最终状态。

### 5.15 架构质量指标

每个版本评审：

- 新增场景修改了多少内核文件；
- 新增 Provider 是否只增加插件、Manifest 和测试；
- Domain/Application 的第三方框架依赖数是否为 0；
- Port 的实现数量和真实变化理由；
- 插件契约兼容测试覆盖率；
- 插件替换时是否需要数据库/API 变更；
- Workflow 条件分支和场景枚举是否持续增长；
- Effective Manifest 是否能完整复现实例组合；
- 是否存在绕过 Gateway/Policy/Ledger 的执行路径。

目标不是代码行最少，而是变化被限制在正确边界内。

### 5.16 中文注释与代码可读性规范

实施阶段新增的自研类必须使用中文类注释。这里的“类注释”是紧跟在类声明后的 Docstring，不是只在类上方写一行 `#`，因为 Docstring 可以被 IDE、`help()` 和文档生成工具识别。

最低要求：

- 自研的 `class`、`Protocol`、抽象基类、数据类、枚举和自定义异常都必须有中文 Docstring；
- 类注释至少说明“负责什么”和“不负责什么”；领域核心类还要说明关键不变量；
- Port/Protocol 说明能力契约、输入输出语义和主要失败情况，不能描述某个具体实现；
- Adapter/Plugin 说明它连接的外部系统、隔离边界以及错误如何转换成平台标准错误；
- State、DTO 和配置类说明其生命周期及字段含义；字段无法从名称直接理解时，补充中文字段说明；
- 公共方法、复杂算法、恢复逻辑、安全判断和副作用边界使用中文 Docstring 或行内注释解释“为什么这样做”；
- 简单私有方法不强制逐行注释，禁止把代码逐字翻译成没有额外信息的注释；
- 注释必须随代码一同维护，过期或与行为不一致的注释视为缺陷；
- 自动生成代码、第三方代码、数据库迁移快照可以豁免，但必须在检查配置中按明确目录豁免，不能在源码中随意使用忽略标记。

推荐格式：

```python
class ToolExecutionGateway:
    """统一承接工具执行请求，并强制经过策略、审批和执行账本。

    本类负责组织执行顺序和返回标准结果，不实现具体工具，也不自行
    判断某个业务场景是否允许执行；具体权限由 ToolPolicyPort 决定。
    任何有副作用的调用都必须先取得 Ledger Claim，不能绕过该入口。
    """


class WorkspaceProviderPort(Protocol):
    """定义工作区生命周期能力，不绑定本地目录或 Kubernetes 实现。

    实现需要保证租户、用户和任务之间的挂载隔离；创建失败、租约失效
    和清理失败必须转换为平台定义的 WorkspaceError。
    """
```

不合格示例：

```python
class TaskService:
    """任务服务类。"""  # 只重复类名，没有说明职责、边界和约束
```

CI 同时执行两类检查：

1. Ruff/pydocstyle 或等价规则检查必须存在类 Docstring；
2. 自定义 AST 测试检查受管源码中的类 Docstring 至少包含中文字符，并对核心类检查职责/边界模板。

中文注释检查失败与类型检查、架构测试和单元测试一样阻断合并。代码评审还需判断注释是否准确，因为自动检查只能发现“没有写”，不能证明“写得正确”。

---

## 6. 总体架构

```text
Web / CLI / SDK
       │
       ▼
Agent API Service
  Auth / Tenant / RBAC / Idempotency
  Task / Approval / Clarification / Steering
       │
       ▼
Durable Dispatcher / Queue
       │
       ▼
Agent Worker
       │
       ▼
Lifecycle Orchestrator（LangGraph Adapter）
  ├─ Workflow Resolver
  ├─ Capability Registry
  ├─ Node / Router Providers
  ├─ LangChain Agent Adapter
  ├─ Middleware Composition
  └─ Verifier Composition
       │
       ▼
Application Services + Domain Microkernel
  ├─ Task / Run / Command / Event
  ├─ Policy / Approval / Execution Ledger
  ├─ Artifact / Evidence / Verification
  └─ Memory / Failure / Recovery
       │
       ├─ PostgreSQL Checkpointer / Business Store Adapters
       ├─ Sandbox / Repository / Tool Adapters
       ├─ Artifact / Memory Adapters
       └─ Event / Trace / Metric Adapters
```

本地模式可以是：

```text
CLI → 同一套 Graph/Middleware/Tools → SQLite → Local Sandbox
```

本地和生产只允许替换 Adapter，不允许使用两套流程语义。

### 6.1 生产系统必须拆成四个平面

```text
控制面 Control Plane
  用户、组织、租户、工作区、RBAC、策略、模型配置、配额、审计

编排面 Orchestration Plane
  Session、Task、LangGraph、Checkpoint、审批、恢复、队列、Lease

执行面 Execution Plane
  Sandbox、文件系统、命令、网络、代码仓库、工具、Artifact

观测与治理面 Observability & Governance Plane
  Event、Trace、Metrics、Log、评测、成本、数据保留、告警、灾备
```

不能把四个平面全部塞进一个 API 进程。尤其是执行用户代码的 Sandbox，必须和保存租户数据、Secret 及平台权限的控制面隔离。

### 6.2 推荐后端服务拓扑

```text
API Gateway / Ingress
        │
        ▼
Agent API（无状态，多副本）
  ├─ Auth / Tenant / RBAC
  ├─ Session / Task Command
  ├─ Approval / Clarification
  └─ Query / SSE
        │
        ├──────────→ PostgreSQL
        ├──────────→ Durable Queue
        └──────────→ Object Storage
                         │
                         ▼
Agent Worker（多副本）
  ├─ LangGraph Runtime
  ├─ Model Gateway Client
  ├─ Tool Policy Client
  └─ Sandbox Client
                         │
                         ▼
Sandbox Manager
  ├─ 每 Task/Run 独立 Sandbox
  ├─ Workspace Volume
  ├─ Network Proxy
  ├─ Resource Limits
  └─ Artifact Uploader
```

### 6.3 生产就绪能力矩阵

后面的阶段只能说明实施顺序，真正判断“是否生产就绪”使用以下矩阵：

| 能力域 | 必须解决的问题 | 最低生产验收 |
|---|---|---|
| 身份认证 | 用户是谁、Token 是否有效 | OIDC/OAuth2、短期 Token、服务身份可轮换 |
| 租户隔离 | A 租户不能访问 B 租户数据 | API、DB、Cache、Queue、Object Key 全链路隔离测试 |
| 工作区隔离 | 不同用户项目不能互相读写 | Workspace ACL + 独立挂载 + 逃逸测试 |
| Sandbox | 用户代码不能危害宿主或控制面 | 非 root、资源限额、网络默认拒绝、无宿主敏感挂载 |
| 编排恢复 | 服务重启不能丢任务 | PostgreSQL Checkpoint 跨进程恢复 |
| 副作用安全 | 不能重复写文件、发请求、执行发布 | Ledger、幂等键、Unknown Outcome、对账 |
| 审批 | 批准的动作和真正执行动作一致 | 参数 Hash、版本、主体、有效期和二次校验 |
| Secret | 密钥不能进入模型或日志 | Secret Manager、最小注入、扫描与轮换 |
| 记忆 | 不串用户、不保存不该保存的内容 | namespace、来源、TTL、删除、注入防护 |
| 数据治理 | 明确数据放哪里、保留多久 | 分类、加密、备份、删除和审计策略 |
| 可观测性 | 能定位一次任务在哪里失败 | Task/Run/Node/Tool 统一关联 ID |
| 质量评测 | 修改 Prompt/Graph 后知道是否退化 | Golden Dataset + 确定性门禁 + Pairwise |
| 容量与成本 | 流量增长时不拖垮平台 | 配额、背压、自动扩缩、预算与压测 |
| 灾备 | 数据库或可用区故障可恢复 | 备份恢复演练、RPO/RTO 达标 |
| 发布 | 新版本不能破坏等待中的 Task | Schema/Graph 兼容检查、Canary、回滚 |

### 6.4 环境隔离

至少分为：

```text
local      本地开发
test       自动化测试
staging    类生产集成与故障演练
production 真实租户数据
```

不同环境必须使用不同的数据库、对象存储、队列、加密密钥、OAuth Client、模型凭据和网络策略。禁止测试环境读取生产 Secret 或生产 Workspace。

### 6.5 架构依赖顺序

工程实施顺序不能按“用户最容易看到什么”排列，而要按“后续能力依赖什么安全基础”排列：

```text
威胁模型 / SLO / 数据分类
  ↓
工程基线 / 测试骨架 / 最小观测
  ↓
LangGraph + PostgreSQL 恢复 Spike
  ↓
身份 / Tenant / RBAC / Workspace Ownership
  ↓
API / Task / Event / Outbox / Idempotency
  ↓
Tool Manifest / Policy / Approval / Ledger / Cancellation
  ↓
Repository & Workspace Lifecycle
  ↓
Sandbox / Egress / Secret Injection
  ↓
只读 Agent
  ↓
Task Spec / Context / Session Memory
  ↓
文件 Mutation
  ↓
命令 / 后台进程 / Verification
  ↓
崩溃恢复 / Steering / 多实例
  ↓
长期记忆 / 数据治理 / SRE / 评测 / GA
```

硬规则：

- 没有身份和 Workspace Ownership，不开放任何真实 Workspace；
- 没有 Policy、Approval 和 Ledger，不开放任何副作用 Tool；
- 没有 Sandbox，不运行用户仓库代码和依赖脚本；
- 没有 Verification，不允许写入 Task 进入 `SUCCEEDED`；
- 没有 Tenant Scope，不启用共享向量检索、缓存和对象存储；
- 没有恢复与幂等测试，不部署多个 Worker；
- 没有最小观测和回归 Fixture，不进入下一个功能阶段。

### 6.6 纵向切片而不是一次建完整平台

每个里程碑都交付一个端到端可运行切片：

| 里程碑 | 可交付能力 | 明确限制 |
|---|---|---|
| M0 技术可行 | PostgreSQL 跨进程暂停/恢复 | 无真实用户和工具 |
| M1 内部只读 Alpha | 单/多用户授权 Workspace 只读问答 | 无写入、无命令 |
| M2 受控写入 Alpha | 审批后的 Patch + Verification | 内部用户、小流量 |
| M3 工程闭环 Beta | 命令、后台进程、恢复、短期记忆 | 受限网络和工具集 |
| M4 多租户 Beta | 多实例、配额、长期记忆、控制台 | Canary 租户 |
| M5 GA | 安全、灾备、SLO、评测和回滚全部达标 | 按公开能力边界服务 |
| M6 Post-GA | MCP、Skill、Subagent、多模态 | 单独安全评审 |

这样既不会为了“先把平台做全”长期没有用户价值，也不会为了快速演示跳过安全前置。

---

## 7. 通用生命周期图、Workflow 和内层 Agent

顶层 Graph 只能表达所有 Task 都具备的稳定生命周期，不能把某种语言、业务平台或任务类型写进固定图。具体流程由 `WorkflowProviderPort` 和 Capability Registry 装配。

```text
START
  ↓
load_runtime_context
  ↓
resolve_workflow
  ↓
prepare
  ↓
execute_capabilities
  ↓
evaluate_completion
  ├─ NEED_MORE_WORK ─────────→ execute_capabilities
  ├─ NEED_USER_INPUT ────────→ clarification_interrupt
  ├─ BLOCKED ────────────────→ finalize_blocked
  └─ READY
       ↓
run_verifiers
  ├─ FIXABLE_FAILURE ────────→ execute_capabilities
  ├─ FAILED ────────────────→ finalize_failed
  └─ PASSED ────────────────→ finalize_success → END
```

主要节点：

- `load_runtime_context`：加载身份、租约、Task 和 Effective Manifest；
- `resolve_workflow`：按 TaskSpec 的 Capability/Constraint 选择 Workflow；
- `prepare`：调用已注册的 Context/Workspace/Analyzer/Planner Provider；
- `execute_capabilities`：运行选中的 Agent/Subgraph/Deterministic Node；
- `evaluate_completion`：调用 Completion Policy 判断是否还有缺口；
- `run_verifiers`：遍历 TaskSpec 绑定的 Verifier 插件；
- `finalize_*`：通过通用 Result/Memory/Event Provider 收口。

这个结构保证模型不能绕过 Verification，同时允许“代码分析”“文档处理”“运维诊断”等场景通过 Workflow 和 Capability 插件加入，而不修改顶层生命周期。

### 7.1 Workflow Manifest

```yaml
apiVersion: agent.tsm.dev/v1
kind: Workflow
metadata:
  name: engineering-task-default
  version: 1.0.0
spec:
  accepts:
    capabilities:
      - workspace.file.read
  phases:
    - id: prepare
      providerCapability: project.analyze
      optional: true
    - id: execute
      providerCapability: agent.reason_and_act
    - id: verify
      providersFrom: task_spec.verifiers
  transitions:
    - from: verify
      on: FAILED_FIXABLE
      to: execute
      maxIterations: 2
```

Workflow Manifest 描述组合，不允许包含任意 Python 表达式。复杂计算由版本化 Node/Router 插件完成，输出标准 `NodeOutcome`。

### 7.2 Workflow 组合层次

```text
Lifecycle Graph（平台固定，极少变化）
  └─ Workflow（声明式组合）
      └─ Subgraph / Node Provider（可插拔实现）
          └─ Middleware / Tool / Model / Verifier（可插拔能力）
```

场景差异只允许出现在 Workflow、Provider 和 Manifest，禁止沿调用链扩散成多个 `if scenario == ...`。

---

## 8. 框架无关状态协议与 LangGraph 映射

领域/应用层定义自己的状态 DTO，不直接使用 `BaseMessage` 或 LangGraph Reducer：

```python
class TaskRuntimeState(BaseModel):
    schema_version: int
    task_id: str
    session_id: str
    tenant_id: str
    lifecycle_state: str
    goal_revision: int
    task_spec_ref: str
    conversation_ref: str
    working_memory_ref: str
    effective_manifest_ref: str
    pending_interaction_ref: str | None
    execution_refs: list[str]
    evidence_refs: list[str]
    artifact_refs: list[str]
    verification_ref: str | None
    failure: dict | None
```

`orchestration/langgraph` 再定义框架专用 State，并通过 Mapper 转换：

```python
from typing import Annotated, TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class LangGraphTaskState(TypedDict, total=False):
    schema_version: int
    graph_version: str

    tenant_id: str
    user_id: str
    session_id: str
    task_id: str
    workspace_id: str

    goal: str
    goal_revision: int
    task_spec: dict
    task_spec_revision: int
    current_phase: str

    # 仅编排 Adapter 使用。领域层只保存 conversation_ref。
    messages: Annotated[list[BaseMessage], add_messages]

    project_profile_ref: str
    working_memory_ref: str
    context_manifest: dict
    prompt_manifest: dict

    pending_tool_calls: list[dict]
    pending_approval_id: str | None
    pending_clarification_id: str | None
    tool_execution_refs: list[str]
    mutation_refs: list[str]

    evidence_refs: list[str]
    completed_outcomes: list[str]
    remaining_outcomes: list[str]
    verification_ref: str | None

    budget: dict
    last_error: dict | None
    recovery_action: dict | None

    effective_config_hash: str
    toolset_hash: str
    policy_version: str
    workspace_fingerprint: str
```

身份映射：

```text
thread_id            = task_id
checkpoint_ns        = graph_version / subgraph
session_id           = 多任务对话容器
run_id               = 一次 Worker 执行
tool_execution_id    = 一次具体工具执行
```

State 不保存 Secret、连接对象、线程、隐藏思维链、大型日志或二进制。大内容存入 Artifact Store，只保留引用和 Hash。

### 8.1 状态边界规则

- PostgreSQL 业务状态使用 `TaskRuntimeState`；
- LangGraph Checkpoint 使用 `LangGraphTaskState`；
- 两者通过稳定引用和版本关联，不允许任意字典互相复制；
- LangChain Message 只出现在 Model/Orchestration Adapter；
- Provider 原始响应只出现在 Provider Adapter，持久化前转换为通用 Message/Event；
- ORM Model 不离开 Persistence Adapter；
- API DTO 不直接成为 Domain Entity；
- Mapper 必须有双向契约测试和版本迁移测试。

### 8.2 Effective Manifest

每次 Run 解析并冻结：

```text
Workflow Version
Node/Router Provider Versions
Model Adapter + Model Policy
Tool Providers + Tool Manifest Hash
Middleware Stack + Order
Verifier Providers
Prompt/Context/Memory Policies
Sandbox/Network/Secret Profiles
```

恢复时按 Effective Manifest 找相同实现；找不到或不兼容时进入 `REBASE/CONFLICT/RESTART`，不能由 Registry 静默换成当前默认插件。

---

## 9. LangChain Agent 方案

第一版优先使用 `create_agent()` 构建内层 Agent，并作为顶层 Graph 节点或子图。以下需求无法可靠表达时，才改成显式 Agent `StateGraph`：

- 多 Tool Call 精确批次；
- 每个 Tool Call 独立审批；
- 工具之间有复杂依赖；
- Tool Ledger 与 Checkpoint 需要精确提交顺序；
- 特殊错误恢复和补偿路由。

支持 OpenAI、Anthropic、Google、OpenAI-compatible 等 Provider，业务代码不依赖具体响应对象。能力通过配置声明：

```text
tool_calling / parallel_tools / structured_output / vision
prompt_cache / stream_cancel / context_window / reasoning_effort
```

---

## 10. Middleware 体系

### 10.1 清单

| Middleware | 作用 | 来源 |
|---|---|---|
| Identity | tenant、user、workspace 权限 | 自定义 |
| Prompt Manifest | 版本化 Prompt 与 Hash | 自定义 |
| Context | Session、Working、Project Memory | 自定义 |
| Summarization | 超窗压缩 | 框架能力 + 约束 |
| Model Retry | 429、5xx、超时退避 | 框架能力 + 错误分类 |
| Model Fallback | 主模型故障后切备用 | 框架能力 + 显式策略 |
| Model Call Limit | 防无限循环和成本失控 | 框架能力 |
| Tool Call Limit | 限制工具次数 | 框架能力 |
| Tool Selection | 根据阶段裁剪工具集 | 框架/自定义 |
| Tool Policy | Schema、权限、风险、网络、路径 | 自定义 |
| Approval | 高风险调用前暂停 | 自定义 + `interrupt()` |
| Tool Ledger | Claim、幂等、Unknown Outcome | 自定义 |
| Tool Retry | 仅安全条件满足时重试 | 框架外包自定义条件 |
| Tool Result | 截断、脱敏、Artifact、错误标准化 | 自定义 |
| PII/Secret | 检测敏感输入输出 | 框架 + 自定义 |
| Memory | 召回和异步候选 | 自定义 |
| Steering | 在安全边界处理补充/替换 | 自定义 |
| Observability | Event、Trace、Metrics、Usage | 自定义 + OTel |
| Cost/Quota | Token、费用、并发、租户额度 | 自定义 |

### 10.2 概念顺序

```text
Identity
→ Config / Prompt
→ Context / Memory / Summarization
→ Budget / Model Retry / Fallback
→ Model
→ Tool Selection / Policy
→ Approval
→ Tool Ledger
→ Tool Retry / Execution
→ Result Sanitization / Artifact
→ Observability
```

必须用自动化测试验证真实 Hook 嵌套和异常传播，尤其保证：

- `interrupt()` 不被普通异常处理吞掉；
- 审批一定在执行前；
- Ledger Claim 一定在外部副作用前；
- Retry 不绕过 Ledger；
- 错误和结果只提交一次。

项目发现、任务规划、Verification 和补偿属于 Graph 节点，不塞进 Middleware。

---

## 11. 审批与澄清

审批请求必须包含：工具和版本、规范化参数、风险、目标、网络访问、数据传输、回滚、Workspace、参数 Hash、Task/Turn/Tool Call ID、有效期。

支持：

- `approve`：原参数执行；
- `edit`：修改后重新校验并生成新 Hash；
- `reject`：不执行，返回受控反馈；
- `respond`：只用于用户回答工具，不能代替副作用拒绝。

流程：

```text
模型 Tool Call
→ Tool Policy
→ 持久化 Approval Record
→ interrupt(public_payload)
→ Checkpoint 保存
→ API 显示 WAITING_APPROVAL
→ 审批决定
→ Command(resume=decision) + 相同 task_id
→ 再校验身份、权限、参数 Hash 和版本
→ Tool Ledger Claim
→ 执行工具
```

澄清使用相同暂停/恢复机制，但不授予工具权限。普通聊天输入不能绕过待处理审批。

---

## 12. 工具系统

### 12.1 工具组

- 文件只读：`list_files/find_files/search_text/read_file`；
- 文件修改：`apply_patch/write_file/delete_file/rollback`；
- 代码智能：定义、引用、实现、符号、诊断、重命名预览；
- 进程：`run/start/poll/read_output/terminate`；
- 交互：`request_input`；
- 记忆：`list/remember/verify/forget`；
- 工作状态：`working_memory.read/update`、`task_spec.read/update`。

审批是 Middleware/Graph 能力，不能给模型一个可以自行调用并伪造通过的“approve”工具。

### 12.2 权威 Metadata

```python
class ToolPolicyMetadata(BaseModel):
    risk: Literal["R0", "R1", "R2", "R3", "R4"]
    effect: Literal["observe", "mutate", "execute", "interact", "internal"]
    idempotency: Literal["idempotent", "keyed", "non_idempotent"]
    concurrency_safe: bool
    requires_network: bool
    data_transmission: str
    rollback: str
    result_authority: str
    max_result_tokens: int
```

服务端注册表是权威来源，不能信任模型参数里的风险声明。

第一版只允许 R0、只读、并发安全、互不依赖且不修改内部状态的工具并行；文件写入、命令、审批和状态更新串行。

---

## 13. Tool Ledger、重试和恢复

每次工具执行保存：

```text
tool_execution_id / tenant_id / task_id / turn_id / tool_call_id
tool_name / tool_version / arguments_hash / idempotency_key
status / attempt / started_at / committed_at
result_ref / error_category
```

状态：

```text
PENDING → CLAIMED → RUNNING → COMMITTED
                         ├─ FAILED_RETRYABLE
                         ├─ FAILED_TERMINAL
                         └─ UNKNOWN_OUTCOME
```

三类重试：

| 类型 | 对象 | 实现 |
|---|---|---|
| Model Retry | 模型 API 请求 | LangChain Model Retry |
| Tool Retry | 具体工具调用 | Ledger + Tool Retry |
| Node Retry | Graph 节点 | LangGraph RetryPolicy |

规则：

- `IDEMPOTENT`：临时错误可重试；
- `KEYED`：使用同一业务幂等键查询或重试；
- `NON_IDEMPOTENT`：只有确定未发送才可重试，已发送但响应丢失进入 `UNKNOWN_OUTCOME`。

恢复包括审批恢复、用户回答恢复、Worker 崩溃恢复、版本兼容恢复和 Unknown Outcome 对账。已产生副作用后的逆向处理用显式补偿/Saga 节点，不能靠从头重试。

### 13.1 取消、超时和截止时间

取消不是一个布尔字段，而是一套状态协议：

```text
CANCEL_REQUESTED
→ CANCELLING
→ CANCELLED_CLEAN
   或 CANCELLED_WITH_EFFECTS
   或 UNKNOWN_OUTCOME
   或 CANCELLATION_FAILED
```

需要区分：

- 用户取消整个 Task；
- 用户取消当前模型调用；
- 用户取消等待审批；
- 平台超时；
- 租户预算耗尽；
- Worker 优雅 drain；
- 管理员强制终止 Sandbox；
- 外部 Tool 自身提供撤销 API。

规则：

- 取消请求首先阻止新节点和新 Tool 调度；
- 模型 Streaming 可以协作取消，但已计费 Token 不能回滚；
- 尚未发送的 Tool 标记取消；
- 已发送的 Tool 依据 Ledger 状态查询结果，不能直接标记未执行；
- 正在运行的命令先发协作信号，再按宽限期终止进程组；
- 强制 Kill Sandbox 后，如果外部请求可能已发出，进入 `UNKNOWN_OUTCOME`；
- 已提交 Mutation 根据用户选择保留或走显式回滚，不能因 Task 取消自动抹掉用户可见修改；
- Approval 取消只关闭审批请求，不自动取消其他并行工作；
- Deadline 必须持久化，恢复后继续生效；
- 所有取消动作幂等并写审计 Event。

验收：在模型、只读 Tool、文件写入、长命令、网络副作用、等待审批和 Verification 各阶段发出取消，最终状态与真实副作用一致，不能一律显示简单 `CANCELLED`。

### 13.2 熔断、降级和失败预算

- Provider、Tool、Sandbox 和外部系统分别设置 Circuit Breaker；
- 熔断状态不只存在单个 Worker 内存，应在多实例间共享或快速收敛；
- Fallback 只能切换到满足工具、上下文和数据区域要求的模型；
- 降级时可以关闭非必要记忆、减少并行或转只读模式，但不能降低权限和审批；
- 达到错误预算后暂停高风险自动化，保留人工恢复；
- 重试必须有总时间预算，避免多层 Middleware + Graph + SDK 叠加形成重试风暴。

---

## 14. 工作区与 Sandbox

### 14.1 文件边界

- 路径规范化；
- 防 `..`、symlink、Junction 逃逸；
- 默认拒绝 `.git` 内部、`.ssh`、密钥、证书、凭据；
- 工作区外读取需独立、限目录、限 Task 的只读授权；
- 额外读取授权不自动授予写入和命令权限。

每次 Mutation 保存前后 Hash、Patch、备份引用、Tool Execution、Approval、主体和时间。提交前检查原 Hash，防止覆盖用户或其他 Agent 的并发修改。

### 14.2 命令边界

- 结构化 argv，默认不使用 Shell 字符串；
- 显式 cwd 和环境白名单；
- 超时、输出上限、Artifact 化；
- 子进程组终止；
- 网络和文件系统由 Sandbox Profile 控制；
- 生产优先容器/微虚拟机隔离；
- 不向 Agent 暴露宿主 Docker Socket、全局 SSH Key 或云主账号凭据。

### 14.3 Sandbox 不是一个 `subprocess` 包装器

生产 Sandbox 必须是独立执行边界。推荐隔离粒度：

```text
一个 Task Run 一个 Sandbox
或
一个 Task 一个可续租 Sandbox
```

不能让不同租户的命令在同一个可写容器中运行，也不能让 Agent Worker 自己直接执行用户仓库代码。

### 14.4 Sandbox 权限模型

| 权限域 | 默认策略 | 可提升方式 |
|---|---|---|
| 用户身份 | 非 root、无 sudo、独立 UID/GID | 不允许模型提升 |
| 根文件系统 | 只读 | 平台镜像构建时确定 |
| Workspace | 只挂载当前 Task 授权目录 | Workspace Policy + Approval |
| 临时目录 | 独立、限额、Task 结束清理 | 不跨租户复用 |
| 进程 | PID/进程数限制，禁止宿主命名空间 | 平台配置 |
| CPU/内存 | cgroup/等价机制硬限制 | 租户配额 |
| 磁盘 | Workspace/临时盘配额 | 租户配额 |
| 网络 | 默认拒绝出站 | 域名、端口、协议、目的和数据类型审批 |
| 入站网络 | 默认禁止 | 特殊预览服务临时端口代理 |
| Linux Capability | 全部移除 | 平台白名单，模型不可申请任意 Capability |
| Device | 不挂载宿主设备 | GPU 等专用池显式调度 |
| Secret | 默认无 Secret | 短期、任务级、工具级注入 |
| Docker/Kubernetes API | 禁止 | 只能通过受控平台 Tool |

Linux 优先使用 seccomp、AppArmor/SELinux、user namespace、只读 rootfs、no-new-privileges 和 cgroup。更高风险的多租户场景应评估 gVisor、Kata Containers、Firecracker 或独立远程 Sandbox 服务。

### 14.5 网络出口

所有 Sandbox 出站流量经过 Egress Proxy：

```text
Sandbox
→ DNS/URL/IP Policy
→ Tenant/Task/Tool Identity
→ 数据外传检查
→ Egress Proxy
→ 外部目标
```

需要解决：

- DNS rebinding；
- 私网、Metadata Service、localhost 和控制面地址访问；
- URL 重定向后目标变化；
- 非 HTTP 协议；
- 大流量上传；
- Prompt/文件中诱导上传 Secret；
- 审批目标与实际连接目标不一致。

默认拒绝云 Metadata 地址、集群内部控制面、数据库私网和其他租户 Sandbox。

### 14.6 依赖安装

Agent 不得默认在宿主或长期共享环境执行 `pip install`、`npm install` 等操作。支持三种模式：

1. 预构建语言镜像；
2. Sandbox 内临时安装，使用只读公共镜像源/代理和缓存；
3. 用户批准后访问指定私有制品仓库。

安装过程受网络、磁盘、时间和供应链策略限制，保存锁文件、来源、包 Hash 和扫描结果。生产构建不得把未锁定依赖悄悄带入最终产物。

### 14.7 Sandbox 生命周期

```text
REQUESTED → PROVISIONING → READY → LEASED
→ RUNNING → DRAINING → SNAPSHOTTING → DESTROYED
                         └─ QUARANTINED
```

- 创建和销毁都要幂等；
- Worker 只持有短期 Sandbox Lease；
- Task 暂停审批时可以冻结或销毁并保留 Workspace Snapshot；
- 恢复时验证 Snapshot、Workspace 和镜像版本；
- 异常 Sandbox 进入隔离，不直接复用；
- Task 结束后销毁计算环境，按策略保留 Artifact。

### 14.8 Sandbox 生产验收

至少自动验证：

- 无法读取宿主 `/etc`、进程、环境和其他挂载；
- 无法访问其他租户 Workspace；
- 无法通过 symlink、mount、namespace 或 `/proc` 逃逸；
- 无法提权或调用宿主 Docker Socket；
- CPU、内存、进程数、磁盘和超时限制生效；
- 默认无法访问公网、私网和 Metadata Service；
- 仅批准的网络目标可访问；
- Secret 不出现在日志、Artifact、Checkpoint 和模型消息；
- Sandbox Kill 后 Task 能按 Ledger/Checkpoint 安全恢复；
- 镜像签名、漏洞扫描和 SBOM 达标。

### 14.9 代码仓库与 Workspace 生命周期

工程 Agent 操作的不是抽象目录，而是有来源、身份、分支和提交语义的 Workspace。必须定义：

```text
Repository Connection
→ Workspace Provision
→ Checkout / Snapshot
→ Agent Read/Mutation
→ Verification
→ Deliverable（Patch / Commit / PR）
→ Retention / Cleanup
```

#### Repository Connection

- 支持本地目录、GitHub/GitLab 等托管仓库；
- 生产优先使用 GitHub App/GitLab Application 等安装级授权，不保存用户长期 PAT；
- Clone/Fetch 使用短期、仓库级凭据；
- 校验仓库归属、Tenant、Workspace 和允许的分支；
- 记录 `repo_id`、remote origin、commit SHA 和授权主体；
- Fork、私有仓库和组织 SSO 状态进入授权判断。

#### Workspace Provision

- 每个 Task 创建隔离 Worktree/Clone/Snapshot；
- 固定基线 commit SHA，不能只记录 branch 名；
- 用户本地脏工作区需要显式策略：拒绝、快照或隔离复制；
- Git submodule、Git LFS、稀疏检出和大仓库需要明确支持范围；
- 禁止在 Clone/Checkout 时自动执行不可信 Hook；
- 文件模式、符号链接、大小写敏感性和换行符必须保持；
- Workspace 创建和销毁均幂等。

#### Mutation 和交付

- Agent 默认写隔离工作分支，不能直接推主分支；
- Patch/Commit 必须绑定基线 SHA 和 Mutation Manifest；
- 创建 Commit/Push/PR 是独立高风险 Tool，单独审批；
- 不自动使用 `--force`、`reset --hard`、`clean -f` 或跳过 Hook；
- 用户在 Agent 执行期间更新远端分支时，必须检测基线漂移；
- 合并冲突进入显式状态，不能由模型静默覆盖；
- PR 描述只使用可审计结果，不泄漏 Prompt、Secret 或隐藏推理。

#### Workspace 并发

- 同一 Workspace 可以有多个只读 Task；
- 写 Task 使用文件级/Workspace 级 Lease 与乐观 Hash；
- 两个 Task 修改同一文件时，后提交者必须重新基于最新基线验证；
- Verification 必须针对最终 Workspace Snapshot，而不是旧中间状态；
- Workspace Lease 过期不代表正在进行的外部副作用未发生。

#### 生命周期验收

- 私有仓库凭据只授权到目标仓库且 Task 结束后失效；
- 不执行不可信 Git Hook；
- 基线 SHA、Patch、Commit、PR 可以互相追溯；
- 脏工作区和并发修改不会静默丢数据；
- Submodule/LFS 未支持时明确阻止，不产生不完整修改；
- Task 取消或 Worker 崩溃后 Workspace 可回收，所需 Artifact 可恢复；
- Agent 无权直接推受保护分支；
- 仓库撤权后，等待中的 Task 在恢复和执行前重新校验。

---

## 15. Project Discovery、Task Spec 和证据

Project Discovery 只做有界静态扫描：语言、包管理器、Source Root、测试目录、构建配置、CI、候选命令和规则文件。发现命令不等于执行命令。外部文件仍是不可信数据，项目规则不能覆盖平台安全策略。

Task Spec：

```python
class TaskSpec(BaseModel):
    goal: str
    assumptions: list[str]
    constraints: list[str]
    outcomes: list[Outcome]
    verification_plan: list[VerificationStep]
    allowed_tool_groups: list[str]
```

每个 Outcome 包含完成标准、所需证据、当前状态和关联 Tool/Artifact/Mutation。

Working Memory 保存 Goal、约束、事实、决定、假设、开放问题、计划、已完成、剩余工作和证据引用；不保存隐藏思维链。

证据必须来自文件读取、代码智能、Tool Result、命令、Mutation、测试报告、用户确认或权威外部系统，不能只来自模型的自然语言断言。

---

## 16. 上下文和记忆

```text
模型调用 Context
  Prompt + 当前消息 + 工具 + 相关证据

Task Working Memory
  当前任务结构化进展

Session Short-term Memory
  同一会话历史任务和摘要

Project Long-term Memory
  跨会话稳定事实和明确偏好
```

区别：

- Checkpointer 保存任务执行到哪里；
- Store 保存跨 Thread 长期记忆；
- Artifact Store 保存大日志、正文、Patch 和报告；
- 业务数据库保存 Task、Approval、Ledger 等权威记录。

长期记忆：

- 用户显式“记住”通过受控 Memory Tool 写入；
- Task 完成后后台提炼隐式候选；
- 候选必须去重、脱敏、记录来源、置信度和 TTL；
- 项目指纹变化时相关事实 stale；
- 记忆作为不可信数据注入，不能提升系统权限；
- 不保存密码、Token、Cookie、隐藏思维链、完整日志和未经验证推测。

Embedding 只负责找相关内容，不证明内容正确；召回后校验租户、项目、来源、时间和有效性。

### 16.1 记忆分类不能混用

| 类型 | 生命周期 | 典型内容 | 推荐存储 |
|---|---|---|---|
| Model Context | 一次模型调用 | 当前 Prompt、消息、工具、证据 | Graph State 临时装配 |
| Task Working Memory | 一个 Task | 目标、计划、事实、待办、证据 | PostgreSQL/Graph 引用 |
| Session Memory | 一个 Session | 用户可见对话、Task 摘要、未完成事项 | PostgreSQL Store |
| User Preference Memory | 跨 Session | 输出风格、明确偏好 | 用户私有 Namespace |
| Project Memory | 跨 Session/成员 | 构建方式、架构决定、稳定项目事实 | Project Namespace |
| Organization Memory | 跨项目 | 明确批准的组织规范 | 受控知识库 |
| Episodic Memory | 历史任务经验 | 某类故障如何解决及结果 | 结构化事件/摘要 |
| Semantic Memory | 稳定事实 | 项目使用 Java 21 | 文档/向量/关系存储 |
| Artifact | 按保留策略 | 原始日志、Patch、报告 | Object Storage，不作为普通记忆注入 |

短期记忆不能无限追加消息；长期记忆也不能复制整段聊天。

### 16.2 Namespace

每条记忆至少绑定：

```text
tenant_id
scope_type: user | project | organization
scope_id
memory_type
memory_id
```

召回时先做权限和 Namespace 过滤，再做关键词/向量排序。严禁先全库向量搜索再在应用层过滤，否则可能通过相似度结果泄漏其他租户内容。

### 16.3 记忆 Schema

```python
class MemoryRecord(BaseModel):
    memory_id: str
    tenant_id: str
    scope_type: str
    scope_id: str
    memory_type: str
    content: str
    summary: str
    source_refs: list[str]
    confidence: float
    sensitivity: str
    status: Literal["candidate", "active", "stale", "rejected", "deleted"]
    valid_from: datetime
    valid_until: datetime | None
    created_by: str
    verified_by: str | None
    project_fingerprint: str | None
    version: int
```

不能只保存一段 `text` 和一个 embedding，否则无法知道它从哪里来、是否过期、谁能看、能否删除以及为什么可信。

### 16.4 写入触发

#### 显式记忆

用户明确说“记住”或调用 Memory Tool：

```text
用户请求
→ 内容分类和敏感检查
→ 确定 user/project/org scope
→ 权限和必要审批
→ 去重/合并
→ 保存 source、confidence、TTL 和版本
```

#### 隐式记忆

Task 完成后由异步 Memory Worker 提炼候选：

```text
Task Event / Verification
→ 候选提炼
→ 只保留稳定且未来可能复用的信息
→ 去重、冲突检测、敏感检查
→ 低风险候选自动激活或进入人工审核
```

不能在每轮模型回答后把所有内容自动写成长久记忆。失败任务、未经验证假设、外部恶意文本和模型自说自话默认不能成为 active memory。

### 16.5 召回流程

```text
当前 tenant/user/project/task
→ 权限与 Namespace Filter
→ 任务意图和查询构造
→ 关键词 + 向量 + 结构化过滤混合检索
→ 去重和 rerank
→ 时效、来源、项目指纹、冲突校验
→ Token Budget 截断
→ 以 untrusted_memory 数据块注入
```

每次模型调用只召回少量相关记忆，并记录使用了哪些 Memory ID。记忆与用户当前要求冲突时，以当前要求为准；记忆与权威系统冲突时，以权威系统为准。

### 16.6 冲突、更新与遗忘

- 同一事实新旧版本冲突时不直接拼接，产生 supersedes 关系；
- 项目依赖或配置变化后，相关记忆自动 stale；
- 低置信度记忆需要重新验证；
- TTL 到期后停止召回，但按审计策略决定是否物理删除；
- 用户可以查看、修改、删除、导出自己的记忆；
- Project Admin 可以管理项目共享记忆；
- 删除采用 Tombstone + 异步清除向量、缓存、备份过期副本；
- 被 Prompt Injection 污染的记忆可按来源批量隔离和重建索引。

### 16.7 上下文压缩

压缩必须保留：

- 当前 Goal 和 revision；
- Task Spec/Outcome；
- 关键约束；
- 已确认事实与来源；
- 待处理 Approval/Clarification；
- 未完成工作；
- Tool Call/Tool Result 原子关系；
- Mutation 和 Verification 引用。

压缩摘要需要版本、来源消息范围和 Hash。不得把旧工具输出中的指令在摘要后升级成 System Instruction。

### 16.8 记忆验收

- Session 重启后短期上下文可重建；
- 不同 Session 的私有内容不自动共享；
- User Preference 不泄漏给同租户其他用户；
- Project Memory 只对授权成员可见；
- 删除后在线检索、向量索引和缓存不再返回；
- 过期/stale/rejected 记忆不注入模型；
- 恶意记忆不能触发工具或提升权限；
- 长对话压缩后仍能继续未完成任务；
- 召回结果记录来源，可用于调试和评测；
- Memory Store 故障时 Agent 降级运行而不是跨租户错误召回。

---

## 17. Verification：唯一成功门禁

模型输出“完成了”只是结束建议，不直接把 Task 标为成功。

只读任务检查：

- 是否引用真实证据；
- 是否覆盖用户问题；
- 是否仍有关键假设；
- 是否声称执行了未执行的操作。

修改任务检查：

- 当前文件 Hash 与 Mutation 一致；
- 修改覆盖 Task Spec Outcome；
- 最后一次修改后运行了相关测试/构建/静态检查；
- 验证退出码为 0；
- 没有未解决 Tool Failure、Unknown Outcome 或后台进程；
- 没有未经审批的副作用。

结果：

```text
PASSED
FAILED_FIXABLE
FAILED_TERMINAL
BLOCKED
INVALID
```

`FAILED_FIXABLE` 可以携带结构化反馈回到 Agent，但必须限制修复轮数和成本。

---

## 18. 持久化与多实例

### 18.1 本地

- SQLite Checkpointer；
- SQLite 业务表；
- 本地 Artifact；
- Local Sandbox。

### 18.2 生产

PostgreSQL 保存：

```text
tenants/users/workspaces
sessions/tasks/task_runs
approvals/clarifications
tool_executions/idempotency_records
mutations/verification_reports
runtime_events/outbox_events
graph_checkpoints/checkpoint_writes/checkpoint_blobs
leases
memories/memory_sources
prompt_manifests/tool_manifests/policy_versions
```

对象存储保存大型 Tool Result、stdout/stderr、Patch、构建/测试报告和导出 Flow。

第一版优先用 LangGraph 官方 PostgreSQL Checkpointer，不在项目开始时自研。通过以下措施保证业务一致性：

- 副作用前先持久化 Ledger Claim；
- Tool Result 先提交 Ledger，再推进 Graph；
- Runtime Event 使用事务 Outbox；
- 写 API 持久化后才响应成功；
- 对每个崩溃窗口做故障注入。

若实测证明官方 Checkpointer 无法满足必要原子性，才在后续实现自定义 Checkpointer。

### 18.3 Lease

同一 Task 同时只允许一个 Worker：

```text
task_id / owner_worker_id / fencing_token / expires_at / heartbeat_at
```

旧 Worker 即使醒来，也不能用旧 fencing token 提交。系统不依赖 Sticky Session：创建、审批和恢复可以落到不同 API/Worker。

创建 Task、审批、回答、Resume、Cancel、Steer 均使用 `Idempotency-Key`。

---

## 19. 安全设计

### 19.1 多租户与 RBAC

所有查询至少带 `tenant_id + resource_id`。仅知道 Task/Approval/Artifact ID 不能访问资源。角色建议：Viewer、Operator、Approver、Workspace Admin、Platform Admin。

#### 19.1.1 身份模型

```text
Organization / Tenant
  ├─ User
  ├─ Service Account
  ├─ Team / Group
  ├─ Workspace
  ├─ Project
  └─ Role Binding
```

用户身份、服务身份和 Sandbox 工作负载身份必须分开。浏览器用户 Token 不能直接传进 Sandbox；Worker 也不能使用平台管理员身份执行普通租户 Tool。

#### 19.1.2 认证

- 用户登录优先 OIDC/OAuth2；
- API 使用短期 Access Token；
- Service Account 使用可轮换凭据或 Workload Identity；
- Sandbox 使用 Task 级短期身份；
- 禁止长期共享 API Key 代表所有用户；
- Token 必须校验 issuer、audience、expiry、tenant 和 subject；
- 撤权和用户禁用需要在可接受时间内生效。

#### 19.1.3 授权

授权判断至少包含：

```text
subject
tenant
role
resource
action
workspace
tool risk
environment
policy version
```

例子：一个用户可能有权查看 Task，但没有权读取原始 Artifact；有权运行只读工具，但没有权批准生产发布；有权审批 R1 文件修改，但无权审批 R4 数据外传。

审批必须执行职责分离。高风险情况下，发起人不能审批自己的动作，或需要双人审批。

#### 19.1.4 全链路 Tenant Scope

以下位置都必须包含或派生出不可伪造的 Tenant Scope：

| 位置 | 隔离要求 |
|---|---|
| HTTP/API | Token 中 tenant 与路径资源归属一致 |
| PostgreSQL | 复合主键/索引，Repository 强制 tenant 条件，可选 RLS |
| Redis/Cache | Key 带 tenant，禁止只用 task_id |
| Queue | 消息含 tenant、task、签名和 schema version |
| Object Storage | tenant/workspace/task 分层 Key，下载使用短期签名 URL |
| Vector Index | tenant + project namespace，查询时强制 Metadata Filter |
| Checkpointer | thread_id 外还要校验 tenant/task ownership |
| Sandbox | 独立身份、Workspace 挂载和网络策略 |
| Logs/Trace | 带 tenant 标签，但敏感字段脱敏 |
| Metrics | 避免把 user_id 等高基数字段直接当 Prometheus Label |

#### 19.1.5 用户级和项目级隔离

同一租户内部也不能默认全员共享：

- Private Session 只有创建者和授权协作者可见；
- Team Workspace 按组授权；
- Project Memory 默认只在 Project Namespace 内召回；
- User Preference Memory 只能属于该用户；
- Approval Queue 只能展示审批人有权处理的请求；
- Artifact 下载需要重新授权，不能只凭永久 URL；
- 管理员查看敏感内容必须产生审计事件。

#### 19.1.6 隔离验收

至少覆盖：

- 使用 A 的 Token 访问 B 的 Task/Session/Approval/Artifact/Memory 全部拒绝；
- 猜测 UUID、修改 URL、复用签名 URL 不能越权；
- Cache、Queue 重试、向量查询和 Replay 不串租户；
- 删除用户后，其个人 Token、Session 和私有记忆按策略失效；
- Workspace 成员移除后，正在等待审批的任务重新校验权限；
- 审批人在批准后被撤权，工具执行前二次校验并拒绝；
- 数据导出、备份恢复和离线评测同样保持租户边界。

### 19.2 Prompt Injection

- 网页、文件、日志、RAG、Tool Result 和记忆均为不可信数据；
- 外部内容不能提升为 System 指令；
- 权限只由服务端 Policy 决定；
- 模型不能通过文本自行批准；
- 高风险动作执行前重验完整参数；
- 数据外传展示目的地和数据类型。

### 19.3 Secret

- 从 Secret Manager 按需注入；
- 不进入 Prompt、State、Checkpoint、Event 和 Trace；
- Tool 只获得完成当前动作所需最小 Secret；
- 输出和异常经过 Secret Scanner。

---

## 20. API 与实时事件

建议 API：

```text
POST /v1/sessions
POST /v1/sessions/{session_id}/tasks
GET  /v1/tasks/{task_id}
GET  /v1/tasks/{task_id}/events
GET  /v1/tasks/{task_id}/flow
POST /v1/tasks/{task_id}/steer
POST /v1/tasks/{task_id}/redirect
POST /v1/tasks/{task_id}/cancel
POST /v1/tasks/{task_id}/resume
POST /v1/approvals/{approval_id}/decisions
POST /v1/clarifications/{request_id}/answers
GET  /v1/artifacts/{artifact_id}
```

SSE/WebSocket 负责实时体验，持久化 Event 才是历史事实。事件包括 Task、Graph Node、Model、Tool、Approval、Clarification、Mutation、Verification 和 Completion。禁止传输隐藏思维链。

---

## 21. 可观测性与告警

OpenTelemetry Trace 层级：

```text
Task Run
  └─ Graph Node
      ├─ Model Attempt
      ├─ Middleware
      └─ Tool Execution
```

核心指标：Task 成功/失败/阻塞、阶段耗时、模型首 Token/429/费用、工具重试/拒绝/Unknown Outcome、审批等待、Checkpoint 延迟、Resume 成功率、Verification 通过率、Context 压缩、Lease/队列、租户配额。

核心告警：数据库/Checkpoint 失败、Unknown Outcome、Task 无进展、审批后未恢复、模型错误激增、工具重试风暴、Verification 失败激增、Lease 冲突、跨租户拒绝异常、Secret 泄漏和容量阈值。

LangSmith 可以辅助 Agent Trace 和评测，但不能成为任务恢复和审计的唯一事实来源。

---

## 22. 配置和版本兼容

每个 Task 固化：

```text
graph_version / state_schema_version
prompt_manifest_id/hash
model_provider/model/parameters
middleware_manifest_hash
tool_manifest_hash
policy_version / sandbox_profile / memory_policy_version
```

Task 暂停期间若 Tool 参数、Prompt、Policy、Workspace 指纹、Graph/State 或模型能力不兼容变化，不能静默继续。处理结果：

```text
RESUME    完全兼容
REBASE    无副作用状态转换后继续
CONFLICT  需要人工判断
RESTART   新建 Task，保留旧审计
```

依赖使用 Python 3.12 和 `uv` 精确锁定。LangChain/LangGraph/Provider/PostgreSQL/OTel 的具体版本在阶段 2 Spike 后确定；阶段 1 只建立可重复安装、锁定和升级校验机制，不能在 SPEC 中凭空选版本，也不能使用开放范围漂移。

### 22.1 模型治理与 Model Gateway

每个模型配置不仅包含名称和参数，还必须声明：

```text
provider / model / endpoint / region
data_retention_policy
training_opt_out
prompt_cache_scope
supported_modalities
tool_calling / structured_output / context_window
rate_limit / timeout / retry_classification
cost_table_version
allowed_tenants / data_classifications
fallback_group
```

规则：

- Provider API Key 保存在 Secret Manager，不进入 Agent State；
- 模型请求经过租户、数据分类和区域策略；
- 高敏感数据只能发送到允许的 Provider/区域；
- Fallback 前重新执行数据和能力策略，不能只选择“下一个可用模型”；
- Prompt Cache 必须确认租户隔离、TTL 和 Provider 行为；
- 图像、音频、文件等多模态数据单独分类和授权；
- 记录模型和参数版本、Token、价格版本、延迟和失败分类；
- 价格变化不能无痕改变运行中 Task 的预算判断；
- Provider 返回的 reasoning/隐藏推理不持久化和展示；
- Provider 故障时优先安全降级，不能降低审批、隐私或区域要求。

### 22.2 Prompt、Policy、Tool 和 Graph 发布

这些内容都属于可执行配置，必须像代码一样管理：

- Git/Registry 中版本化；
- Schema 校验和签名；
- Reviewer 和变更原因；
- 测试与离线评测；
- Staging/Canary/Promotion；
- 生效时间和目标租户；
- 回滚版本；
- 等待中 Task 的兼容矩阵。

禁止在生产数据库直接修改 Prompt 文本或 Tool 权限后立即全量生效。紧急策略变更可以阻止风险动作，但必须产生审计、通知和待恢复 Task 的重新校验。

### 22.3 Feature Flag 与 Kill Switch

需要按环境、租户、用户、工具、模型和 Graph Version 控制灰度。Kill Switch 至少支持：

- 停止创建新 Task；
- 将平台切到只读模式；
- 禁用某 Tool/Provider/Extension；
- 停止外部网络；
- 暂停某租户；
- 禁止新的副作用，但允许查询和人工对账；
- Drain Worker/Sandbox Pool。

Kill Switch 不能删除历史状态，必须可审计、可恢复，并在 Tool 执行前实时检查。

---

## 23. 评测系统

Golden Tasks 至少覆盖代码解释、项目分析、Bug 定位、单/多文件修改、测试/构建失败、审批通过/拒绝、澄清、重启恢复、Prompt Injection、路径逃逸、长上下文、Unknown Outcome 和多租户隔离。

Snapshot 保存输入、Fixture 版本、Prompt/Graph/Tool/Policy Manifest、模型、可见消息、Tool Call/Result、Diff、Verification、Token/费用/延迟、错误恢复和最终回答。

确定性指标优先：测试、文件、越权、重复副作用、必需步骤、恢复、工具次数和成本。LLM Judge 负责清晰度、方案符合度、证据合理性和代码质量。

Pairwise 交换 A/B 位置，保留：

```text
candidate_wins / baseline_wins / tie
both_bad / cannot_determine / inconsistent
```

CI 输出 `PASS / FAIL / INVALID`。任何安全不变量失败直接 `FAIL`，不能被 Judge 胜率抵消；数据不完整或不可比输出 `INVALID`。

---

## 24. 测试和故障注入

测试分为领域单测、Middleware 合约、Graph 节点、Graph 集成、Checkpointer、PostgreSQL 多实例、Sandbox、安全、E2E、故障注入和离线评测。

必测崩溃窗口：

1. 模型请求发送前；
2. 模型响应收到但 Checkpoint 未写；
3. Ledger Claim 前；
4. Claim 后、外部调用前；
5. 外部调用已发送、响应未收到；
6. Tool Result 收到、Ledger 未提交；
7. Ledger 已提交、Graph 未推进；
8. Approval 已创建、Checkpoint 未返回；
9. Approval 已通过、Tool 未启动；
10. 文件已修改、Mutation 未提交；
11. Verification 执行中；
12. Finalize 写总结时。

每个窗口必须定义恢复后唯一合法行为，并用自动化测试证明。

---

## 25. 生产部署

组件：Agent API、Agent Worker、PostgreSQL、队列、对象存储、Sandbox Runner、Secret Manager、OTel Collector 和监控后端。

Worker 收到 SIGTERM：停止领取新任务，请求 Graph 在安全边界 drain，等待当前节点到宽限期，保存 Checkpoint/Ledger，释放 Lease，由其他 Worker 接管。运行中的副作用不能简单视作未执行。

健康检查分 Liveness 和 Readiness；数据库、队列、Secret 或 Sandbox 不可用时 Worker 退出 Readiness。Provider 探测使用固定最小请求，不携带项目数据。

### 25.1 服务职责

#### Agent API

- 无状态、多副本；
- 身份认证、Tenant Context 和 RBAC；
- 接收 Command，校验 `Idempotency-Key`；
- 创建 Session/Task、审批、澄清、Steering 和取消；
- 查询 Task、Flow 和 Artifact；
- SSE/WebSocket 鉴权和断线续传；
- 不直接执行用户代码和高风险 Tool。

#### Agent Worker

- 消费 Durable Task Command；
- 获取 Task Lease/fencing token；
- 执行 LangGraph；
- 调用模型、Tool Policy、Sandbox 和 Artifact 服务；
- 写 Checkpoint、Ledger 和 Outbox；
- 不向公网暴露用户 API。

#### Sandbox Manager/Runner

- 创建、租赁、冻结、恢复和销毁 Sandbox；
- 管理镜像、Workspace Volume、资源限制和网络策略；
- 返回结构化 Execution Result；
- 不持有控制面数据库管理员权限。

#### Model Gateway

- 统一 Provider 认证、路由、超时、重试、熔断和降级；
- Tenant/Task 级 Token 和成本统计；
- Provider 数据保留策略；
- 可选 Prompt Cache；
- 不替代 Agent Graph 的业务重试。

### 25.2 Durable Dispatch

任务投递采用至少一次语义，因此 Consumer 必须幂等：

```text
API 事务写 Task + Outbox
→ Outbox Relay 发布 Command
→ Worker 消费并 Claim Task Lease
→ 重复消息通过 command_id / task version 去重
→ 成功推进后 ACK
```

队列不能作为唯一事实来源；消息丢失可由 Outbox 重发，消息重复不能导致重复副作用。需要 Dead Letter Queue、重试次数、毒消息隔离和人工重放工具。

### 25.3 PostgreSQL

- Multi-AZ/高可用；
- PITR；
- 自动备份和恢复演练；
- 连接池与每服务连接预算；
- 慢查询、锁等待、膨胀和容量监控；
- Schema Migration 使用 expand/migrate/contract；
- 迁移脚本向前兼容滚动发布；
- Checkpoint 大字段和 Blob 评估分区/对象存储；
- 租户规模增长后支持分区或分片路线。

### 25.4 Object Storage

- Key 带 tenant/workspace/task/artifact；
- 服务端加密；
- 短期签名 URL；
- 内容类型、大小、Hash 和恶意文件扫描；
- 不允许猜测公共 URL；
- 生命周期和删除策略；
- Artifact Metadata 保存在 PostgreSQL；
- 上传使用临时凭据，Sandbox 无桶级长期密钥。

### 25.5 Kubernetes/调度要求

- API、Worker、Sandbox Runner 独立 Deployment/Node Pool；
- 控制面和执行面 NetworkPolicy 默认隔离；
- Pod 使用非 root、只读 rootfs 和最小 ServiceAccount；
- PodDisruptionBudget；
- topology spread / anti-affinity；
- HPA 指标区分 API QPS、队列积压、运行 Task 和 Sandbox Provision 延迟；
- Worker 扩缩容时优雅 drain；
- Sandbox 使用独立节点池和更严格 RuntimeClass；
- ResourceQuota、LimitRange 和优先级；
- 禁止普通 Sandbox 访问 Kubernetes API。

### 25.6 发布策略

```text
开发环境验证
→ 集成测试
→ Staging 故障演练
→ 数据库 Expand Migration
→ API/Worker Canary
→ 观察新旧 Graph Version 指标
→ 分批放量
→ Contract Migration
```

等待中的 Task 固定 Graph/State/Prompt/Tool/Policy 版本。新 Worker 必须保留兼容执行包，或者将不兼容任务标记为 `REBASE/CONFLICT/RESTART`，不能因为镜像更新直接丢弃。

### 25.7 容量与背压

需要分别限制：

- 每租户并发 Task；
- 每用户请求速率；
- 全局模型并发；
- 单 Provider 并发与 RPM/TPM；
- Sandbox Provision 并发；
- 每 Task 模型、工具、Token、时间和费用；
- 队列最大长度和消息年龄；
- 数据库连接、Artifact 大小和日志流量。

达到上限时执行排队、降级、拒绝或取消，而不是无限创建 Task。高优先级人工交互和审批恢复应避免被低优先级批处理完全饿死。

### 25.8 灾备目标

部署前必须定义分级目标，例如：

| 数据/服务 | 建议目标 |
|---|---|
| Task/Approval/Ledger | RPO 接近 0，RTO 按业务等级制定 |
| Checkpoint | 允许回到最近安全边界，不能重复副作用 |
| Runtime Event | 与关键状态同事务/Outbox，可重建投影 |
| Artifact | 按等级跨区域复制或可重建 |
| Memory | 有备份和删除传播机制 |
| Metrics/Trace | 可降级，不阻塞核心任务 |

至少定期演练：数据库恢复、队列重建、对象存储恢复、区域/可用区故障、Secret 轮换、Worker 全量重启和 Sandbox 节点池故障。

### 25.9 后端部署验收

- API 任意副本可处理创建、查询和审批；
- Worker 任意重启不丢可恢复 Task；
- 队列重复投递不重复执行；
- 数据库主从切换后 Task 可继续；
- 滚动发布期间旧 Graph Task 有明确处理；
- Sandbox 节点故障不影响控制面安全；
- 扩容和背压策略在压测中生效；
- 备份能在隔离环境真正恢复，而非只有“备份成功”日志；
- 可用区故障、Provider 故障和对象存储短暂故障有演练记录；
- Runbook 能从告警定位到 tenant/task/run/node/tool。

---

## 26. 新项目目录

```text
tsm-agent-next/
  pyproject.toml
  uv.lock
  README.md

  src/tsm_agent/
    domain/                # 纯领域微内核，不依赖框架和 IO
      task/
      execution/
      approval/
      artifact/
      evidence/
      verification/
      memory/
      policy/

    contracts/             # 版本化 DTO、Command、Event、Plugin Contract
      api/
      events/
      plugins/
      manifests/

    application/           # 用例编排，只依赖 Domain + Ports
      task_service.py
      execution_service.py
      approval_service.py
      recovery_service.py
      query_service.py

    ports/                 # 稳定 Protocol/ABC，不放具体实现
      identity.py
      persistence.py
      model.py
      tool.py
      workflow.py
      sandbox.py
      repository.py
      memory.py
      verification.py
      telemetry.py

    orchestration/         # 框架集成层
      langgraph/           # State Mapper、Lifecycle Graph、Checkpoint Adapter
      langchain/           # Message/Model/Tool Adapter、Middleware
      workflow_runtime/    # Manifest Parser、Node/Router Composition

    adapters/              # Port 的基础设施实现
      persistence/
      queue/
      artifacts/
      identity/
      model_providers/
      sandbox_providers/
      repository_providers/
      memory_stores/
      telemetry/

    plugins/               # 内置插件包，仍通过同一 Contract
      builtin_workspace/
      builtin_process/
      builtin_project_analyzer/
      builtin_verifiers/

    plugin_sdk/            # Manifest、Test Kit、开发者 SDK
      manifests/
      contracts/
      testkit/

    interfaces/            # 入站适配器
      http_api/
      cli/
      worker/

    bootstrap/             # 唯一 Composition Root
      composition.py
      plugin_loader.py
      settings.py

    observability/         # 标准 Event/Trace/Metric 语义
    evaluation/            # Dataset、Snapshot、Judge、Gate

  tests/
    unit/
    architecture/
    contracts/
    plugins/
    middleware/
    graph/
    integration/
    production_journeys/
    fault_injection/
    evaluation/

  deploy/
    docker/
    kubernetes/
    migrations/
    dashboards/
```

### 26.1 模块依赖规则

```text
domain
  ↑
contracts + ports
  ↑
application
  ↑
orchestration / adapters / plugins / interfaces
  ↑
bootstrap
```

这里的箭头表示“外层依赖内层”。`domain` 位于最内层，不知道外面存在 LangChain、LangGraph、HTTP、PostgreSQL 或 Kubernetes。

### 26.2 一个能力的标准落点

以“增加一种代码托管平台”为例：

```text
contracts/plugins/scm.py             通用请求/响应
ports/repository.py                  RepositoryProviderPort
plugins/<provider>/manifest.yaml     Capability 与权限声明
plugins/<provider>/provider.py       实现
tests/contracts/                     Port 合约测试
tests/plugins/<provider>/            Provider 特有测试
bootstrap/plugin_loader.py           通过 Manifest 自动发现，无平台名分支
```

不应修改 Task Entity、Lifecycle Graph、Approval Core 或其他 SCM 插件。

---

## 27. 从 0 到 1 实施路线：按硬依赖重排

阶段 0 是开工门禁，阶段 1～18 是建设和上线阶段。顺序由安全和数据依赖决定，不代表每个阶段工期相同。每个阶段必须：

```text
SPEC 子章节确认
→ 实现
→ 专项测试 + 全部回归
→ 类型/Lint/Build
→ 中文类注释与代码可读性检查
→ 安全/数据/运维验收
→ Golden Journey / Snapshot 更新
→ 记录风险、ADR 和回滚方式
→ 停止，确认后进入下一阶段
```

### 阶段 0：产品边界、威胁模型、数据分类和 SLO

- 明确首批用户、任务类型和明确非目标；
- 明确仓库来源、可执行代码范围、联网和生产系统访问；
- 定义控制面、编排面、执行面、数据面信任边界；
- 定义租户模型、风险等级、审批职责和数据分类；
- 定义可用性、延迟、RPO、RTO、保留、删除和成本目标；
- 产出 Threat Model、Data Flow Diagram、Abuse Case 和 ADR。

门禁：没有威胁模型、数据分类和 Sandbox 边界，不开始实现真实 Tool。

### 阶段 1：独立建仓、工程基线、最小观测与评测骨架

- 新仓库、Python 3.12、uv 精确锁定；
- Ruff、类型检查、Pytest、Build、CI 和分支保护；
- Dockerfile、SBOM、依赖/Secret 扫描；
- 结构化日志和 correlation ID；
- Test Fixture、Golden Journey、Snapshot Schema 初版；
- ADR、Migration 和 Runbook 目录；
- local/test/staging/production 配置边界。
- 建立 Domain/Contracts/Application/Ports/Orchestration/Adapters/Plugins/Bootstrap 目录；
- 建立 import-linter/AST 架构规则；
- 建立中文类 Docstring 检查和注释评审清单；
- 建立 Plugin Contract Test Kit 骨架。

门禁：干净环境 `sync --frozen`、测试、类型、Lint、Build、容器、最小日志和基线 Fixture 全部通过；Domain 导入框架或 Adapter 时 CI 必须失败；新增自研类缺少有效中文 Docstring 时 CI 必须失败。

### 阶段 2：LangChain/LangGraph/PostgreSQL 技术 Spike

- 锁定 LangChain/LangGraph/Provider/PostgreSQL Checkpointer；
- 最小 `create_agent()`、StateGraph、Tool Call、Structured Output 和 Streaming；
- `interrupt()`、`Command(resume=...)`；
- Graph/State/Prompt/Tool Manifest Prototype；
- 最小 Trace、Usage 和 Checkpoint Inspection；
- 验证框架 Hook 顺序、Retry 和异常传播。
- 定义框架无关 `TaskRuntimeState` 和 LangGraph State Mapper；
- 验证 LangChain Message/Tool DTO 不穿透 Application Contract。

门禁：进程 A 暂停并退出，进程 B 从 PostgreSQL 恢复同一 Task；`interrupt()` 不被错误中间件吞掉；不能用 `InMemorySaver` 代替；更换 Fixture Model Adapter 不修改 Domain/Application。

### 阶段 3：身份、租户、用户与 Workspace Ownership

- OIDC/OAuth2、User/Service/Workload Identity；
- Organization/Tenant/User/Team/Workspace/Repository；
- RBAC、Policy Enforcement Point 和职责分离；
- PostgreSQL Tenant Scope/RLS 选型；
- Cache、Queue、Artifact、Vector、Checkpoint Namespace 规范；
- 权限撤销、审计和短期身份。

门禁：跨租户和同租户越权矩阵全部拒绝；不知道或不拥有 Workspace 的用户无法创建真实 Task；撤权在执行前生效。

### 阶段 4：后端控制面、Task 状态机和持久化骨架

- 无状态 Agent API；
- Session、Task、Run、Command、Event 数据模型；
- 明确 Task State Machine 和合法迁移；
- Idempotency-Key；
- PostgreSQL Repository 和 expand/migrate/contract；
- Transactional Outbox、Durable Queue、DLQ；
- SSE/Event Cursor；
- Artifact Metadata；
- API、Worker、Sandbox 的服务身份边界。
- Capability Registry、Plugin Manifest Parser 和 Composition Root；
- Plugin Lifecycle、Health、Drain、Quarantine；
- Contract Version 与 Effective Manifest。

门禁：API 重试不重复创建；Outbox/Queue 重放不重复推进；Task/Event 可重建；非法状态迁移被拒绝；两个同契约 Fixture Plugin 可以仅改配置完成切换。

### 阶段 5：Policy、Approval、Tool Ledger、取消和预算基础

- Tool Manifest、风险、幂等、网络、数据外传和回滚 Metadata；
- Tool Policy Middleware；
- Approval/Clarification Protocol；
- Tool Execution Ledger 和 Claim/Commit；
- Model/Tool/Node Retry 边界；
- Cancellation、Deadline、Budget 和 Kill Switch；
- 使用无真实副作用的 Fixture Tool 做崩溃窗口测试。
- Policy 依据 Manifest Metadata，而不是 Tool 名判断；
- 同一 Gateway 支持多个 Tool Provider；
- Workflow/Node/Verifier Fixture 插件和声明式组合。

门禁：参数改变后审批失效；重复 Resume/Queue 不重复提交 Fixture 副作用；取消状态反映真实结果；没有 Ledger 的 Tool 不能执行；新增一种 Fixture 场景不修改 Lifecycle Graph 和 Approval Core。

### 阶段 6：Repository 和 Workspace 生命周期

- Repository Connection 和短期仓库凭据；
- 基线 commit SHA、Worktree/Clone/Snapshot；
- 脏工作区、并发 Workspace、Lease 和乐观 Hash；
- Git Hook 禁止策略；
- Submodule/LFS/大仓库能力声明；
- Patch/Commit/PR 交付模型和保护分支策略；
- Workspace Retention 和清理。

门禁：完成第 14.9 节生命周期验收；仓库权限、基线和 Workspace 归属可追溯。

### 阶段 7：Sandbox、Egress 和 Secret 执行平台

- Sandbox Manager/Runner 和 Task Lease；
- 非 root、只读 rootfs、capability/seccomp/AppArmor；
- CPU、内存、进程、磁盘、时间限制；
- 独立 Workspace 挂载；
- Egress Proxy、默认拒绝网络和 SSRF 防护；
- Tool/Task 级短期 Secret；
- 镜像、SBOM、漏洞和依赖安装策略；
- Freeze/Resume/Destroy/Quarantine。

门禁：第 14.8 节逃逸、资源、网络、Secret、Kill 和恢复测试全部通过，才允许运行真实仓库代码。

### 阶段 8：最小只读工程 Agent（M1 Alpha）

- 顶层 Graph 骨架和 `create_agent()`；
- Model Gateway 基础和模型治理；
- 文件只读工具只通过 Sandbox/Workspace 服务；
- Project Discovery v1；
- Working Memory v1；
- 只读 Verification；
- Model/Tool/Graph Event 和 Trace；
- 第一批 Golden Tasks。
- 文件、项目分析、模型、验证均作为 Provider Plugin 接入；
- Workflow 通过所需 Capability 解析实现。

旅程：授权仓库 → 创建隔离 Workspace → 解析 Workflow/Capability → 发现项目 → 搜索/读取 → 带证据回答 → PASSED → 销毁 Sandbox。用第二个 Fixture Repository/Analyzer 替换实现时不修改内核。

### 阶段 9：Task Spec、证据、上下文和 Session 短期记忆

- Structured Task Spec、Outcome 和 Verification Plan；
- Evidence Question/Reference、Completion Readiness；
- Prompt/Context Manifest；
- Token、工具、时间和费用预算；
- Session Message/Task Summary；
- Working Memory 持久化；
- 压缩、来源、未完成任务继续；
- Session/User 隔离。

门禁：服务重启后 Session 可重建；压缩后保留 Goal、Outcome、证据、Tool 原子关系和剩余工作；私有 Session 不串用户。

### 阶段 10：安全文件 Mutation 与受控写入（M2 Alpha）

- Apply Patch/Write/Delete；
- Workspace/Sensitive Path 和 symlink/Junction Policy；
- Mutation、前后 Hash、备份、Rollback；
- 文件级/Workspace 级并发冲突；
- 写入审批复用阶段 5 协议；
- Patch Artifact 和交付预览。

门禁：Approval → Ledger Claim → Mutation → Ledger Commit 顺序经过故障注入；审批前不写；并发修改不被覆盖。

### 阶段 11：命令、后台进程、依赖和 Verification（M3 Beta）

- 前台/后台进程、超时、取消和进程组；
- 日志/报告 Artifact；
- 依赖安装与制品源策略；
- 构建、测试、Lint、诊断识别；
- 写入后的确定性 Verification；
- `FAILED_FIXABLE` 修复循环和上限；
- Commit/Push/PR 作为独立高风险 Tool。

门禁：最后一次修改后没有相关验证不能成功；普通 `echo`/`git status` 不能充当证据；强制 Kill 后副作用状态正确。

### 阶段 12：Steering、完整崩溃恢复和副作用对账

- Steer、Redirect、Queue、Status；
- Goal revision 和安全边界消费；
- Unknown Outcome 查询和人工对账；
- 补偿/Saga；
- Graph/State/Prompt/Tool/Policy 兼容；
- 12 个崩溃窗口；
- Worker Drain 和 Task Resume。

门禁：结果未知绝不盲目重放；Steering 不重放旧 Tool；不兼容任务进入 REBASE/CONFLICT/RESTART。

### 阶段 13：长期记忆、Embedding 和记忆治理

- User/Project/Organization Namespace；
- 显式 Memory Tool 和异步候选；
- 来源、置信度、TTL、版本、stale、supersedes；
- 关键词 + 向量混合检索，Tenant Filter 前置；
- 查看、修改、删除和导出；
- Memory Injection/污染隔离；
- 删除传播和 Store 降级。
- Memory Store、Embedding、Retriever、Reranker 分别通过 Port 插拔；
- 召回策略通过版本化 Memory Policy 组合。

门禁：第 16.8 节隔离、删除、时效、污染和降级测试全部通过；替换向量存储不修改 Agent、Task、Workflow 和 Memory Domain。

### 阶段 14：生产 Worker、多实例、弹性和容量（M4 Beta）

- API/Worker/Sandbox 独立部署；
- Durable Dispatch、DLQ、Lease/fencing/heartbeat；
- Object Storage 和 Model Gateway HA；
- 优雅 drain；
- 并发配额、背压、优先级和 HPA；
- 多可用区、连接池和容量模型；
- 负载、长稳和重试风暴测试。

门禁：无 Sticky Session；队列重复、Worker Kill、自动扩缩和可用区故障不重复执行或串租户。

### 阶段 15：数据治理、安全加固和合规

- Secret 轮换、PII/Secret Detection；
- 数据分类、区域、加密、保留、删除、导出；
- Provider 数据治理和 Fallback 合规；
- Prompt Injection、SSRF、路径逃逸和供应链红队；
- 管理员/审批审计；
- 渗透测试和安全响应 Runbook；
- Feature Flag 和紧急只读 Kill Switch 演练。

门禁：高危问题清零；删除传播完整；Secret 不进入模型/日志/Artifact；Fallback 不违反租户数据策略。

### 阶段 16：完整可观测性、控制台和 SRE

- 完整 OpenTelemetry、Flow Projection 和 Replay；
- Session/Task/Run/Node/Tool UI；
- Approval/Clarification/Unknown Outcome 控制台；
- Dashboard、Alert、On-call、Runbook；
- SLO/Error Budget、成本归因和租户用量；
- 事件 Schema 兼容和敏感字段治理。

门禁：无需查数据库即可解释任务；告警定位到 tenant/task/run/node/tool；观测后端故障不阻塞核心任务。

### 阶段 17：评测、灾备、灰度和 GA（M5）

- 扩充 Golden Dataset、Snapshot、Pairwise Judge；
- CI `PASS/FAIL/INVALID`；
- 性能、容量、长稳和质量回归；
- 数据库/队列/对象存储/Sandbox/Provider 故障演练；
- 备份恢复与 RPO/RTO；
- Alpha → Canary → 分批放量 → 自动/人工回滚；
- Graph/Schema/Prompt/Tool/Policy 升级演练；
- Production Readiness Review。

门禁：安全不变量 100%；完成率、恢复率、延迟、成本、SLO、RPO/RTO 达标；Runbook 和恢复演练有真实记录。

### 阶段 18（Post-GA）：MCP、Skill、Subagent 和多模态生态

- MCP Server/Client；
- Skill Manifest、安装和版本；
- 第三方 Tool 签名、权限和隔离；
- Subagent/Supervisor/并行任务；
- 更强代码智能、多模态文档和语音；
- 远程 Sandbox 集群。

门禁：扩展只能获得主 Task 权限子集；第三方扩展和 Subagent 不能绕过 Policy、Ledger、Sandbox、预算和租户隔离；接入一种新扩展不得修改 Domain 和 Lifecycle Graph。

### 27.1 Production Readiness Review 总清单

阶段完成不自动代表可以上线。正式开放真实用户前，由产品、应用、平台、安全、数据和 SRE 共同完成最终评审。以下任一关键项不满足，结论为 `NOT_READY`。

#### 产品与流程

- [ ] 支持的任务类型和明确不支持的任务已公开；
- [ ] Task 状态、审批、澄清、取消和恢复语义稳定；
- [ ] 所有成功状态都经过 Verification；
- [ ] 用户可以查看 Agent 当前动作、权限和结果；
- [ ] 高风险失败有人工处置入口。
- [ ] Lifecycle Graph 不包含语言、供应商或业务场景分支；
- [ ] 新增一个 Fixture Workflow 不修改 Domain、Task Lifecycle 和 Tool Gateway；
- [ ] Workflow、Provider、Policy、Verifier 均能从 Effective Manifest 追溯。

#### 架构与插件

- [ ] Domain/Application 不依赖 LangChain、LangGraph、ORM、HTTP 或 Provider SDK；
- [ ] Composition Root 是唯一具体实现装配位置；
- [ ] Adapter/Plugin 之间无直接依赖和循环依赖；
- [ ] Capability Registry 不使用场景名硬编码路由；
- [ ] Plugin Manifest 有版本、Schema、权限、Owner、签名和隔离级别；
- [ ] 插件 Contract Test Kit、兼容测试、健康检查和 Drain 通过；
- [ ] 第三方插件不在控制面进程任意动态 import；
- [ ] 更换 Model、Repository、Sandbox、Memory Store 不修改领域内核；
- [ ] 新增一种 Tool/Verifier/Workflow 只增加插件、Manifest、注册和测试；
- [ ] 架构依赖测试与普通测试一样阻断合并。
- [ ] 自研类均有准确的中文 Docstring，说明职责、边界和必要的不变量；
- [ ] 中文注释 AST 检查已纳入 CI，生成代码和第三方代码仅按明确目录豁免；

#### 身份与隔离

- [ ] OIDC/OAuth2、Service Identity 和 Workload Identity 可用；
- [ ] API、DB、Cache、Queue、Object、Vector、Checkpoint 全链路 Tenant Scope；
- [ ] 同租户用户、Team、Workspace 权限隔离通过；
- [ ] RBAC、撤权、职责分离和管理员审计通过；
- [ ] 跨租户自动化攻击测试无越权。

#### Sandbox

- [ ] 用户代码不在 API/Worker 宿主直接执行；
- [ ] 非 root、只读 rootfs、最小 capability 和资源限制生效；
- [ ] Workspace 挂载和 Task 生命周期隔离；
- [ ] 网络默认拒绝，Egress Proxy 和目的校验生效；
- [ ] Metadata、私网、控制面和其他 Sandbox 不可访问；
- [ ] Secret 为短期最小注入且不会出现在输出；
- [ ] 逃逸、DoS、供应链和镜像安全测试通过。

#### 编排、Checkpoint 和副作用

- [ ] PostgreSQL 跨进程 Checkpoint 恢复通过；
- [ ] Queue 至少一次投递不会重复执行；
- [ ] Task Lease 和 fencing token 生效；
- [ ] Tool Ledger 覆盖全部副作用；
- [ ] Unknown Outcome 有阻断、查询和人工对账流程；
- [ ] Approval 与实际参数、版本、身份绑定；
- [ ] 12 个崩溃窗口全部有唯一恢复行为。

#### 记忆与数据

- [ ] Working、Session、User、Project、Organization Memory 边界明确；
- [ ] Namespace 和向量 Metadata Filter 在检索前强制执行；
- [ ] 来源、置信度、TTL、stale、版本和冲突处理完整；
- [ ] 用户可以查看、修改、删除和导出授权记忆；
- [ ] 删除传播到数据库、向量、缓存和 Artifact 生命周期；
- [ ] Memory Injection 和污染隔离测试通过；
- [ ] 数据分类、加密、保留、备份和审计策略通过。

#### 后端与部署

- [ ] API、Worker、Sandbox 控制面/执行面隔离；
- [ ] PostgreSQL HA/PITR、Queue/DLQ、Object Storage 可用；
- [ ] Schema Migration 支持滚动升级；
- [ ] Graph/State/Prompt/Tool/Policy 版本兼容策略通过；
- [ ] HPA、背压、并发和成本配额经压测验证；
- [ ] SIGTERM Drain、滚动发布和节点故障恢复通过；
- [ ] 备份已在隔离环境真实恢复。

#### 安全

- [ ] Prompt Injection、路径逃逸、SSRF、权限提升和 Secret 泄漏测试通过；
- [ ] 依赖锁定、SBOM、镜像签名和漏洞扫描通过；
- [ ] Tool/Skill/MCP 权限 Manifest 和供应链校验通过；
- [ ] PII、数据外传和审计策略生效；
- [ ] 高危漏洞为 0，中低危有明确接受人和期限；
- [ ] 安全事件响应和密钥轮换演练完成。

#### 质量与运维

- [ ] Golden Tasks、确定性指标、Pairwise 和 CI Gate 通过；
- [ ] 性能、容量、长稳和并发测试达标；
- [ ] Task/Run/Node/Tool Trace 完整；
- [ ] Dashboard、Alert、On-call 和 Runbook 可用；
- [ ] SLO、Error Budget、RPO、RTO 已定义并验证；
- [ ] Provider、数据库、队列、对象存储和 Sandbox 故障演练完成；
- [ ] Canary 和自动/人工回滚经过演练。

评审结果只有三种：

```text
READY          所有关键门禁通过，可以按 Canary 计划上线
CONDITIONAL    只有明确低风险例外，带负责人和到期时间
NOT_READY      存在隔离、副作用、恢复、数据或安全关键缺口
```

---

## 28. 优先级

### P0：可靠工程 Agent 必需

架构宪法、Domain/Contracts/Ports 分层、Composition Root、Capability Registry、Plugin Contract Test Kit、通用 Lifecycle Graph、只读工具、安全修改、命令 Sandbox、Approval、PostgreSQL Checkpoint、Tool Ledger、Verification、Event/Trace、基础评测。

### P1：多人生产使用必需

多租户/RBAC、API/Worker、Lease/Outbox、Project Memory、成本配额、Flow UI、完整故障注入和 Canary。

### P2：生态增强

MCP、Skill、Subagent、更强代码智能、多模态文档、语音、远程 Sandbox 集群和自动评测样本。

---

## 29. 核心原则

1. 优先使用框架，不重写通用 Agent Runtime。
2. 框架不负责的安全和业务规则必须显式实现。
3. Graph State 不是业务数据库和大文件仓库。
4. 模型说完成不等于完成，Verification 才是门禁。
5. 所有副作用先记账、再执行、后提交。
6. 非幂等结果未知时停止自动化。
7. 审批绑定精确参数、身份、版本和工作区。
8. 本地和生产使用同一流程语义。
9. 关键状态不能只存在 Python 进程。
10. 先完成单 Agent 工程闭环，再扩展 Subagent。
11. 每个阶段都有测试和停止点。
12. 旧 `tsm-agt` 只作为需求与测试参考，不成为新架构负担。
13. 内核只表达跨场景不变量，场景差异通过 Capability、Workflow、Policy 和插件表达。
14. LangChain/LangGraph 只存在于集成与编排层，不能成为领域协议。
15. 插拔必须有契约、权限、版本、生命周期和隔离，不能等同于任意动态 import。
16. 新增场景原则上不修改 Lifecycle Graph、Approval Core、Tool Gateway 和领域实体。
17. 架构边界由 CI 自动检查，不依赖代码评审者记忆。

---

## 30. 完成定义

- 新系统是独立新项目，不依赖旧 Runtime；
- LangChain 负责模型、工具和 Middleware；
- LangGraph 负责流程、中断、Checkpoint 和恢复；
- 覆盖 `tsm-agt` 的主要工程功能；
- 多实例下可以暂停、重启和恢复；
- 审批、澄清和 Steering 可用；
- 文件和命令受 Sandbox 保护；
- 副作用具备 Ledger、幂等和 Unknown Outcome；
- 修改后必须经过 Verification；
- 具备 Working、Session、Project 三层记忆；
- Prompt、Graph、Tool、Policy、State 全部版本化；
- 多租户、RBAC、Secret 和数据外传策略完整；
- Flow、Replay、Trace、Metrics、Log 和告警完整；
- 回归评测、故障注入、负载测试和 Canary 达标；
- 不存在“本地 Demo 能运行，但线上重启丢任务或重复执行”的关键路径。
- Domain/Application 对 LangChain、LangGraph 和供应商 SDK 保持零依赖；
- Model、Tool、Repository、Sandbox、Memory、Verifier、Workflow 均有稳定插拔契约；
- 新增一个完整示例场景无需修改领域内核和固定 Lifecycle Graph；
- 插件发现、权限、健康、升级、Drain、隔离和契约兼容流程完整；
- 架构测试可以自动阻止框架泄漏、跨层依赖、场景硬编码和绕过 Gateway。
- 自研类具有准确的中文 Docstring，中文注释检查在 CI 中阻断不合格变更。

---

## 31. 推荐第一步

第一步仍然不修改现有 `tsm-agt`，也不立即创建新工程。先完成阶段 0 的开工门禁，避免代码已经写完，才发现租户边界、Sandbox 权限或数据保留方式无法满足生产要求。阶段 0 细分：

```text
0.1 确定首批用户、首批任务、明确不支持的任务和成功标准
0.2 画出控制面、编排面、执行面、数据面的数据流和信任边界
0.3 定义 Tenant、User、Workspace、Repository、Task 的所有权与隔离规则
0.4 为文件、命令、网络、Secret、模型和外部系统建立威胁/滥用场景
0.5 定义数据分类、保留/删除、审计、RPO/RTO、SLO 和成本边界
0.6 输出 Threat Model、Data Flow Diagram、Abuse Case、ADR 和阶段 0 验收记录
```

阶段 0 只产出边界和约束，不实现业务代码。验收通过后，先停下来确认，再进入阶段 1 创建全新的独立工程；阶段 2 才锁定 LangChain/LangGraph 依赖并完成 `interrupt + PostgreSQL + 跨进程 resume` Spike。只有真正证明“进程 A 暂停并退出，进程 B 能从 PostgreSQL 恢复同一 Task”，才进入后续真实 Agent 能力开发。
