# 网络搜索与网页访问能力规划

## 1. 目标与结论

本方案为 tsm-agt 增加一套通用、可替换的网络检索能力。目标不是绑定某个搜索服务，也不是复制 Codex、Kiro 或 Hermes 的私有实现，而是建立稳定的工具协议和 Provider 插件层，使以下能力可以按配置接入：

- 搜索公开互联网
- 读取指定网页并提取正文
- 使用模型服务商提供的 Hosted Search
- 使用本机网络访问网页
- 使用 MCP 或外部工具完成搜索
- 使用企业内部搜索服务
- 保留可追踪的来源和引用

完成 P0 和 P1 后，tsm-agt 可以具备与 Codex、Kiro、Hermes 同类的日常网络搜索体验。能否直接使用某个 Agent 的内部搜索后端，取决于对方是否公开 API、MCP Server 或兼容工具协议；没有公开接口时，只能实现同类能力，不能直接调用其私有服务。

### 本方案覆盖

- `web.search`：搜索并返回候选来源
- `web.fetch`：读取明确 URL
- `web.extract`：从已有内容中提取正文或结构化数据
- Provider 能力注册、发现、路由与失败降级
- Hosted、Local、MCP 等不同联网边界
- 引用、Evidence 和上下文投影
- 域名、预算、SSRF、外部内容隔离等安全控制

### 本方案暂不覆盖

- 浏览器自动化和页面点击
- 登录态、Cookie、OAuth 和多标签页会话
- 自主站点爬取
- Office/RAG/向量数据库
- 多 Agent 编排

上述能力可以以后通过新的 Provider capability 接入，不修改检索主流程。

## 2. 核心设计原则

### 2.1 模型决定需要什么，Runtime 负责安全执行

正常主流程是：

```text
用户提出问题
  → 模型判断缺少什么信息
  → 模型选择并调用 search/fetch 等检索工具
  → Runtime 查找支持该能力的 Provider
  → Runtime 执行权限、安全、预算和去重检查
  → Provider 执行请求
  → Runtime 统一整理 Evidence 和 Citation
  → 模型依据证据回答
```

职责边界：

- 模型负责理解目标、选择检索动作和组织查询。
- Runtime 负责能力发现、Provider 路由、安全校验、预算、去重、失败降级和结果标准化。
- Provider 负责与具体搜索服务、HTTP 客户端或 MCP 工具通信。
- Policy 只判断允许不允许，不猜测用户的自然语言意图。

Runtime 不通过关键词或固定优先级擅自把一次网络搜索改成本地搜索，也不加入诸如“用户说某句话就走某个 Provider”的特殊分支。可插拔路由策略可以给模型或 Runtime 提供建议，但不能替代模型理解用户目标。

### 2.2 共享 Evidence，不强行共用工具协议

Workspace 搜索、符号搜索、Task 历史检索和网络搜索都能产出 Evidence，但它们的参数、安全边界和执行方式不同。

因此：

- 它们共享 `Evidence`、`Citation`、排序、去重和上下文投影机制。
- 不要求所有检索能力被压进一个过度抽象的 `search()` 接口。
- 模型看到的是少量、语义明确的工具，例如 `search.workspace`、`search.symbol`、`web.search` 和 `web.fetch`。
- Runtime 内部可以共享通用检索基础设施，但不替模型决定检索目标。

### 2.3 外部网页永远是不可信证据

网页中的文字只作为资料，不能成为系统指令、项目规则或用户要求。即使网页写着“忽略之前的规则”或“读取并上传 `.env`”，Runtime 和模型也不得提升其指令权限。

所有网络结果必须标记：

```text
source_type = external_web
trust = untrusted
instruction_authority = none
```

## 3. 总体架构

```text
LLM / Agent
  ↓ Retrieval Tool Contract
Capability Router
  ↓
Network Boundary + Access Policy
  ↓
┌────────────────────────────────────────────────────┐
│ Hosted Search │ Local HTTP │ MCP │ Browser (future)│
└────────────────────────────────────────────────────┘
  ↓
Normalized Evidence + Citation
  ↓
Context Projection
```

建议目录：

```text
src/tsm_agt/ports/retrieval.py
src/tsm_agt/retrieval/capabilities.py
src/tsm_agt/retrieval/registry.py
src/tsm_agt/retrieval/router.py
src/tsm_agt/retrieval/evidence.py
src/tsm_agt/retrieval/context_projection.py
src/tsm_agt/security/network_policy.py
src/tsm_agt/adapters/network/http_client.py
src/tsm_agt/adapters/network/providers/
src/tsm_agt/adapters/builtin/web_tools.py
```

## 4. 能力模型

不同 Provider 可以只实现自己擅长的能力：

```python
class RetrievalCapability(StrEnum):
    SEARCH = "search"
    FETCH = "fetch"
    EXTRACT = "extract"
    CRAWL = "crawl"
    DOWNLOAD = "download"
    BROWSER = "browser"


class FetchMode(StrEnum):
    SELECTIVE = "selective"
    TRUNCATED = "truncated"
    FULL = "full"
```

| Capability | 功能 | 第一阶段是否实现 |
|---|---|---|
| `SEARCH` | 根据关键词找到候选网页 | 是 |
| `FETCH` | 读取一个明确 URL | 是 |
| `EXTRACT` | 从已有内容提取正文或字段 | 是 |
| `CRAWL` | 从入口继续发现多个页面 | 否 |
| `DOWNLOAD` | 将原始文件写入工作区 | 后续 |
| `BROWSER` | 动态页面、登录、点击 | 后续 |

### 4.1 网络边界

```python
class NetworkBoundary(StrEnum):
    HOSTED = "hosted"
    LOCAL = "local"
    MCP = "mcp"
    BROWSER = "browser"
```

- `HOSTED`：请求由模型或搜索服务商的服务器发出。
- `LOCAL`：请求从运行 tsm-agt 的机器发出，遵守本机代理和网络权限。
- `MCP`：请求由 MCP Server 或其他外部工具发出。
- `BROWSER`：请求由带页面状态的浏览器环境发出。

这个字段必须进入审批信息、审计记录和结果元数据。用户需要知道请求从哪里发出，而不能只看到笼统的“需要联网”。

### 4.2 Provider 描述与能力协商

```python
@dataclass(frozen=True)
class ProviderDescriptor:
    name: str
    capabilities: frozenset[RetrievalCapability]
    boundary: NetworkBoundary
    supported_search_modes: frozenset[WebSearchMode]
    supports_domain_filter: bool = False
    supports_citations: bool = True


class RetrievalProvider(Protocol):
    @property
    def descriptor(self) -> ProviderDescriptor:
        ...

    def is_available(self) -> bool:
        """只做廉价的本地配置检查，不发起网络请求。"""
        ...
```

具体操作协议按能力拆分：

```python
class SearchProvider(RetrievalProvider, Protocol):
    async def search(self, request: SearchRequest) -> SearchResponse:
        ...


class FetchProvider(RetrievalProvider, Protocol):
    async def fetch(self, request: FetchRequest) -> FetchResponse:
        ...


class ExtractProvider(RetrievalProvider, Protocol):
    async def extract(self, request: ExtractRequest) -> ExtractResponse:
        ...
```

这样可以让搜索、网页读取和正文提取分别使用不同 Provider，更换服务商时不修改 Agent Kernel。

### 4.3 Provider 注册、发现和路由

Provider 来源可以包括：

- tsm-agt 内置 Provider
- 用户配置目录中的插件
- Python entry point 插件
- MCP Tool Adapter
- 模型服务商声明的 Hosted Tool

配置建议：

```toml
[web]
search_backend = "auto"
fetch_backend = "local_http"
extract_backend = "local_extract"
search_fallback_backends = ["mcp_search"]
fetch_fallback_backends = []
search_mode = "live"

[web.policy]
allowed_domains = []
blocked_domains = []
allow_private_network = false
```

自动选择流程：

1. 根据请求的 capability 筛选 Provider。
2. 排除网络边界或策略不允许的 Provider。
3. 检查 Provider 本地配置是否可用。
4. 使用显式配置的首选 Provider。
5. 首选 Provider 遇到可降级错误时，只在同一 capability 的 fallback 列表中继续尝试。
6. 返回实际使用的 Provider 和降级原因。

路由只依据结构化能力、配置和运行状态，不解析用户自然语言。

## 5. 工具协议

### 5.1 `web.search`

用途：搜索最新信息、官方文档、错误信息或其他外部资料。

请求：

```json
{
  "query": "Python asyncio gather cancellation behavior",
  "limit": 5,
  "freshness": "default",
  "allowed_domains": ["docs.python.org"],
  "provider": "auto",
  "evidence_question": {
    "question_id": "Q1",
    "question": "官方文档如何说明 gather 的取消行为？"
  }
}
```

响应：

```json
{
  "query": "Python asyncio gather cancellation behavior",
  "provider": "brave",
  "boundary": "hosted",
  "results": [
    {
      "title": "Coroutines and Tasks",
      "url": "https://docs.python.org/3/library/asyncio-task.html",
      "snippet": "Run awaitable objects concurrently...",
      "rank": 1,
      "citation_id": "cit_01"
    }
  ],
  "truncated": false
}
```

- 默认 `ToolEffect.OBSERVE`。
- 搜索结果只证明“搜索服务返回了这些候选项”，不自动证明网页内容真实。
- Provider 返回的摘要同样是不可信外部内容。

### 5.2 `web.fetch`

用途：读取一个明确 URL 的正文。

请求：

```json
{
  "url": "https://docs.python.org/3/library/asyncio-task.html",
  "mode": "selective",
  "focus": "gather cancellation",
  "max_chars": 20000,
  "provider": "auto",
  "evidence_question": {
    "question_id": "Q1",
    "question": "官方文档如何说明 gather 的取消行为？"
  }
}
```

`mode` 支持：

- `selective`：只返回与 `focus` 相关的段落和相邻内容，默认模式。
- `truncated`：从正文开头返回，超过限制时截断。
- `full`：返回完整正文，但仍受全局响应大小和上下文预算限制。

响应：

```json
{
  "requested_url": "https://docs.python.org/3/library/asyncio-task.html",
  "final_url": "https://docs.python.org/3/library/asyncio-task.html",
  "status": 200,
  "title": "Coroutines and Tasks",
  "content_type": "text/html",
  "text": "...",
  "mode": "selective",
  "truncated": false,
  "provider": "local_http",
  "boundary": "local",
  "citation_id": "cit_02"
}
```

- 默认 `ToolEffect.OBSERVE`。
- `full` 表示读取完整正文，不表示可以无视单次响应和上下文总量限制。
- HTML 提取建议优先使用 `trafilatura`，失败时回退 `BeautifulSoup`。

### 5.3 `web.extract`

用途：对已经取得的内容进行正文提取、Markdown 转换或字段抽取。默认不自行联网。这项能力与 `web.fetch` 分开，允许搜索、请求和内容解析分别替换 Provider。

### 5.4 `web.download`

用途：把公开文件保存到工作区。它与前三个只读工具不同：

- 使用 `ToolEffect.MUTATE`。
- 必须走正常写入审批。
- 必须记录 mutation journal 并支持 rollback。
- 只能写入审批允许的明确路径。
- 必须校验大小、文件类型和最终 URL。

`web.download` 不纳入第一阶段。

## 6. Evidence、Citation 与上下文

### 6.1 标准化引用

```python
@dataclass(frozen=True)
class Citation:
    citation_id: str
    provider: str
    boundary: NetworkBoundary
    query: str | None
    requested_url: str | None
    final_url: str
    title: str | None
    excerpt: str
    retrieved_at: datetime
    content_digest: str | None
    content_range: str | None
```

引用必须支持：

- CLI 和 WebUI 显示可点击来源。
- 回答中的事实关联到具体证据。
- 上下文压缩后仍保留来源、时间和摘要。
- 恢复会话后仍能知道此前检索过哪些页面。
- URL 跳转后同时保存原始 URL 与最终 URL。

### 6.2 标准化 Evidence

```python
@dataclass(frozen=True)
class RetrievalEvidence:
    evidence_id: str
    question_id: str | None
    source_type: str
    citation_id: str
    excerpt: str
    observed_at: datetime
    trust: str
    instruction_authority: str = "none"
```

不要仅用一个看似精确的 `trust_score` 判断真假。Runtime 可以记录来源类型、时效性和是否为官方域名，但事实是否可靠仍由模型根据多个证据判断。

### 6.3 上下文投影

工具原始返回不应无上限地进入模型上下文。Context Projection 负责：

- 保留原始结果的持久化引用。
- 对模型投影标题、相关摘录、来源和引用 ID。
- 合并重复 URL 和重复内容。
- 根据当前问题选择相关 Evidence。
- 压缩时保留 Citation，不把来源压没。
- 用户或模型明确需要时，允许按 `evidence_id` 展开。

上下文裁剪只是展示层优化，不能删除持久化的证据记录。

## 7. 网络安全与权限

### 7.1 NetworkAccessPolicy

```python
class NetworkAccessPolicy(Protocol):
    def validate_request(self, request: NetworkRequest) -> ValidationResult:
        ...

    def validate_resolved_address(self, address: str) -> ValidationResult:
        ...

    def validate_redirect(self, from_url: str, to_url: str) -> ValidationResult:
        ...

    def validate_response(self, response: NetworkResponse) -> ValidationResult:
        ...
```

必须检查：

- 禁止 `file://` 和未启用的协议。
- 默认禁止 localhost、私网、链路本地地址和云 metadata 地址。
- 标准化域名、端口和 URL 后再匹配规则。
- DNS 解析后的实际 IP 必须再次校验。
- 每一次重定向都重新校验 URL、DNS 和目标 IP。
- 防止 DNS rebinding、IPv4/IPv6 编码绕过和跨协议跳转。
- 限制响应大小、连接超时、总超时和重定向次数。
- 校验 Content-Type；不能只相信文件扩展名。
- 流式读取时超过上限立即终止，不能先全部下载到内存。

上述 DNS、实际 IP 和逐跳 Redirect 校验只适用于 Runtime 能控制连接的 `LOCAL` 边界。不同边界的安全责任不能假装完全相同：

| 边界 | tsm-agt 能直接执行的检查 | 还需要依赖什么 |
|---|---|---|
| `LOCAL` | URL、DNS、目标 IP、Redirect、响应大小和 Content-Type | 本机网络与代理配置 |
| `HOSTED` | 请求参数、允许域名、发送数据、Provider 能力与返回结果 | Hosted Provider 对远端连接的安全承诺 |
| `MCP` | MCP 工具权限、输入数据、结果边界和审计 | MCP Server 自身的网络策略 |
| `BROWSER` | 导航策略、下载策略、页面权限和结果隔离 | 浏览器沙箱与会话策略 |

Provider 必须声明自己真正支持哪些策略。比如 Hosted Provider 不支持域名过滤时，Runtime 不能只在请求 JSON 中增加 `allowed_domains` 就宣称已经限制成功，而应拒绝严格域名模式或提示能力不满足。

### 7.2 域名策略

```python
@dataclass(frozen=True)
class DomainPolicy:
    allowed_domains: tuple[str, ...]
    blocked_domains: tuple[str, ...]
    allow_subdomains: bool
    allow_private_network: bool = False
```

- `allowed_domains = []` 表示没有额外 allowlist 限制，而不是禁止全部域名；如需禁止联网，应使用 `WebSearchMode.DISABLED`。
- 精确域名和子域名匹配不能混淆。
- URL 中出现某个域名字符串不等于属于该域名。
- Redirect 后的域名必须重新审批或命中既有规则。
- Hosted Provider 是否真正支持域名过滤，必须由 capability 明确声明。

### 7.3 Prompt Injection 隔离

网络内容进入上下文时使用独立的数据块，明确标识边界和来源。Runtime 不执行网页中的任何指令，也不从网页内容自动生成高权限操作。

如果网页建议执行命令、上传文件或读取凭据，这些动作仍必须：

1. 由模型根据用户目标单独提出。
2. 经过原有 Tool Policy。
3. 按真实 ToolEffect 触发审批。

### 7.4 凭据和私有配置

- API Key 仅来自环境变量、系统凭据存储或用户目录的私有配置。
- 企业域名、代理地址和内部 Provider 地址不得写入可提交的项目配置。
- 日志、Evidence 和错误消息不得记录完整密钥。
- Provider 的 `is_available()` 只检查配置是否存在，不调用远端验证密钥。
- 可以提供单独的显式健康检查命令，由用户主动执行。

### 7.5 代理

Local HTTP Provider 应支持 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 和 `NO_PROXY`。是否读取系统代理必须进入 Effective Configuration，便于解释网络差异。代理不能绕过目标地址安全校验。

## 8. 搜索模式、预算与缓存

### 8.1 搜索模式

```python
class WebSearchMode(StrEnum):
    DISABLED = "disabled"
    INDEXED = "indexed"
    CACHED = "cached"
    LIVE = "live"
```

- `DISABLED`：不允许网络搜索。
- `INDEXED`：只使用 Provider 已建立的索引。
- `CACHED`：优先使用已有缓存，不保证最新。
- `LIVE`：允许发起实时搜索请求。

并非每个 Provider 都支持所有模式。Router 必须根据 capability 协商，不能悄悄把 `LIVE` 降成缓存结果；若发生降级，结果中必须明确说明。

### 8.2 预算

预算是安全护栏，不是固定完成条件：

```python
@dataclass
class RetrievalBudget:
    max_searches: int
    max_fetches: int
    max_download_bytes: int
    max_projected_chars: int
```

- 默认值可配置。
- 预算按 Task/Turn 记录，不能误当整个 Session 的永久上限。
- 已取得关键证据时可以收尾。
- 关键证据仍缺失且只读获取可继续时，Runtime 可根据配置进入聚焦模式或请求扩展预算。
- 达到硬上限时清楚说明缺少什么、已查什么以及如何继续，不能只输出“预算耗尽”。

### 8.3 缓存与去重

- 缓存键至少包含 Provider、查询、域名过滤、搜索模式和时间窗口。
- Fetch 缓存保存最终 URL、ETag、Last-Modified 和内容摘要。
- 同一次 Task 中避免重复查询和重复 URL。
- 缓存命中必须标记数据取得时间，不能伪装成实时结果。

## 9. Provider 失败、降级与可观测性

统一错误类型：

```text
PROVIDER_UNAVAILABLE
PROVIDER_RATE_LIMITED
NETWORK_POLICY_DENIED
DNS_RESOLUTION_FAILED
FETCH_TIMEOUT
RESPONSE_TOO_LARGE
UNSUPPORTED_CONTENT_TYPE
AUTHENTICATION_FAILED
CAPABILITY_NOT_SUPPORTED
```

Provider fallback 只处理可降级错误：

- Provider 未配置或暂时不可用：可以尝试 fallback。
- 限流或临时网络失败：可以按策略重试或 fallback。
- Policy 拒绝、用户拒绝授权、域名不允许：不能换 Provider 绕过。
- 请求语义无效：返回模型修正，不盲目重试。

每次检索记录：

```text
request_id
task_id / turn_id
capability
provider
network_boundary
search_mode
query 或 URL 的安全摘要
policy_decision
fallback_chain
duration_ms
result_count / response_size
cache_status
error_type
```

CLI/WebUI 默认显示简洁进度；详细诊断进入可展开的审计视图，不把整页网页或完整底层响应打印到终端。

## 10. 与现有 tsm-agt Runtime 的集成

复用现有：

- `ToolProviderPort`：注册内置或插件工具。
- `ToolSpec.requires_network`：声明需要联网。
- `ToolSpec.data_transmission`：声明是否向外部发送查询、URL 或其他数据。
- `ToolEffect`：区分只读检索与文件写入。
- `EvidenceQuestion`：记录本次检索要回答的问题。
- 审批、Checkpoint、ToolBatch 和恢复协议。
- Effective Configuration：显示最终启用的 Provider、边界、搜索模式、代理和域名策略。

需要新增：

- Provider capability registry。
- Hosted/Local/MCP/Browser 网络边界。
- Citation 持久化和上下文投影。
- Provider 配置发现和健康状态。
- MCP Tool Adapter；在 MCP 未实现前保留扩展接口，不伪装成已支持。

### 10.1 Tool 暴露

- 没有任何可用 SEARCH Provider 时，不暴露 `web.search`。
- 网络模式为 `DISABLED` 时，不暴露网络工具。
- 只有 FETCH Provider 时，可以暴露 `web.fetch`，无需伪造搜索能力。
- `web.download` 只有在写入策略启用时暴露。
- 不能仅因为处于某个自定义“阶段”就禁止模型获取完成目标所需的只读证据。

### 10.2 EvidenceQuestion

联网工具继续接收 `evidence_question`，用于记录检索目的和结果归属，但它不应该成为模型调用只读工具的脆弱格式门槛。兼容 Provider 不支持该字段时，由 Runtime 在外层关联，而不是把内部追踪字段发送给第三方。

### 10.3 数据传输提示

审批或策略提示应说明实际发送内容，例如：

```text
将通过 Hosted Search Provider 发送搜索词：
“Python asyncio gather cancellation behavior”
不会发送工作区文件。
```

如果查询包含选中的代码、日志或用户数据，应单独标识，而不是笼统显示“会联网”。

## 11. 与 Codex、Kiro、Hermes 的能力映射

| 参考能力 | 本方案对应设计 |
|---|---|
| Codex Hosted Web Search | `HOSTED + SEARCH` Provider |
| Codex cached/indexed/live | `WebSearchMode` 与 Provider 协商 |
| Codex 搜索和沙箱联网分离 | `NetworkBoundary` |
| Kiro `web_search` | `web.search` |
| Kiro `web_fetch` | `web.fetch` |
| Kiro selective/truncated/full | Fetch mode |
| Kiro 本机代理 | Local HTTP Provider 的代理配置 |
| Hermes Provider 插件 | capability registry 与插件发现 |
| Hermes search/extract 分离 | 分能力 Provider 和独立 backend 配置 |
| Hermes 自动发现与 fallback | `is_available()`、Router 和 fallback chain |

这里是能力映射，不表示可以直接调用这些产品的私有服务。

## 12. 实施顺序与验收标准

### P0：先建立稳定骨架

实现：

1. `RetrievalCapability`、`NetworkBoundary` 和 Provider descriptor。
2. Provider registry 与按 capability 路由。
3. `web.search`、`web.fetch` 的稳定 Tool Contract。
4. Citation 和 RetrievalEvidence 数据模型。
5. 外部内容不可信标记与上下文隔离。
6. URL、DNS、实际 IP 和逐跳 Redirect 安全检查。
7. 私有配置和凭据隔离。

验收：

- 可替换两个不同 Search Provider，而不修改 Kernel。
- Search 和 Fetch 可以使用不同 Provider。
- 每条展示给用户的网络结论都能追溯到 Citation。
- 网页内的恶意指令不能改变 Tool Policy。
- Redirect 到私网地址会被阻止。
- Provider 密钥和私有域名不会进入 Git、日志或 Evidence。

### P1：达到日常生产可用

实现：

1. Hosted Search Provider 和 Local HTTP Fetch Provider。
2. `selective/truncated/full` Fetch 模式。
3. `disabled/indexed/cached/live` 搜索模式协商。
4. Provider 自动发现、显式配置、健康状态和失败降级。
5. 代理、超时、重试、缓存、去重和可配置预算。
6. CLI/WebUI 引用展示和可观测日志。

验收：

- 最新信息请求可以明确使用 Live Search。
- 普通网页读取从本机发出并遵守代理。
- 缓存结果不会被误标为实时结果。
- Provider 限流时可以按配置降级，Policy 拒绝时不能绕过。
- 长网页默认只投影相关部分，原始 Evidence 仍可追溯。

### P2：扩展生态

实现：

1. MCP Tool Adapter。
2. `web.download` 与 mutation journal。
3. Browser Provider。
4. Crawl、PDF 和其他内容解析 Provider。
5. 企业内部搜索 Provider。

验收：

- 接入 MCP 或 Browser 时不修改 Agent 主流程。
- Browser 登录态与普通 Fetch 的安全边界互不混淆。
- 下载文件经过审批、大小和类型检查，并能回滚。

## 13. 测试矩阵

### 功能测试

- Search Provider 返回正常结果、空结果和重复结果。
- Search 与 Fetch 使用不同 Provider。
- Provider 自动发现和显式选择。
- Live、Cached、Indexed 模式协商。
- selective、truncated、full 内容投影。
- Citation 在上下文压缩和会话恢复后仍然存在。

### 安全测试

- localhost、RFC1918、链路本地和 metadata 地址。
- 公网 URL 重定向到私网。
- DNS rebinding 和 IPv4/IPv6 编码变体。
- 超大响应、慢响应、重定向循环和错误 Content-Type。
- 网页中的 Prompt Injection。
- Provider 返回恶意标题、URL 或引用字段。
- Policy 拒绝后不能通过 fallback 绕过。

### 恢复和异常测试

- Provider 超时后重试。
- 限流后切换 fallback。
- Tool 执行中断后恢复，不重复计入已完成请求。
- 会话恢复后继续使用此前 Citation。
- 所有 Provider 不可用时输出明确、可行动的错误。

### 配置和隐私测试

- 环境变量、用户配置和项目配置的优先级。
- Effective Configuration 不暴露密钥。
- Git 敏感信息扫描。
- Hosted、Local 和 MCP 数据传输提示准确。

## 14. 最终流程示例

用户询问一个需要最新资料的问题：

```text
1. 模型判断现有上下文不足，调用 web.search。
2. Capability Router 查到 Hosted Search Provider 支持 SEARCH + LIVE。
3. Policy 检查联网模式、域名和发送数据。
4. Provider 返回候选结果和引用。
5. 模型选择官方来源并调用 web.fetch。
6. Router 选择 Local HTTP Fetch Provider。
7. Policy 对 URL、DNS、IP 和每次 Redirect 做校验。
8. Extract Provider 提取相关正文。
9. Runtime 生成 Evidence 和 Citation，并以不可信外部内容投影到上下文。
10. 模型基于证据回答，CLI/WebUI 显示来源。
```

这条流程没有针对某个搜索服务、某句话或某种项目类型写特殊判断。更换 Provider 只改变注册与配置，不改变模型、Kernel 和 Evidence 主流程。
